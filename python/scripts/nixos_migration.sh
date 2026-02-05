#!/bin/bash
# nixos_migration.sh - Pre-migration: validate, download, backup, stage initramfs
#
# Called by PiFinder app (sys_utils.start_nixos_migration).
# Runs on RPi OS before rebooting into initramfs for the actual migration.
#
# Usage: nixos_migration.sh <migration_url> <sha256> [progress_file]
#
# Exit codes:
#   0 - Success (initramfs staged, ready to reboot)
#   1 - Pre-flight check failure
#   2 - Download failure
#   3 - Checksum mismatch
#   4 - Backup failure
#   5 - Initramfs staging failure

set -euo pipefail

MIGRATION_URL="${1:?Usage: nixos_migration.sh <url> <sha256> [progress_file]}"
MIGRATION_SHA256="${2:?Usage: nixos_migration.sh <url> <sha256> [progress_file]}"
PROGRESS_FILE="${3:-/tmp/nixos_migration_progress}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIFINDER_HOME="/home/pifinder"
PIFINDER_DATA="${PIFINDER_HOME}/PiFinder_data"
DOWNLOAD_DIR="${PIFINDER_HOME}"
TARBALL="${DOWNLOAD_DIR}/pifinder-nixos-migration.tar.zst"
BOOT_PARTITION="/boot"
MIGRATION_FLAG="/boot/nixos_migration"
INITRAMFS_DIR="/tmp/nixos_initramfs"
PROGRESS_BIN="${SCRIPT_DIR}/migration_progress"
INIT_SCRIPT="${SCRIPT_DIR}/nixos_migration_init.sh"

# SD card raw offset for backup (beyond normal partitions, 14GB in)
BACKUP_OFFSET=$((14 * 1024 * 1024 * 1024))
BACKUP_DEVICE="/dev/mmcblk0"

progress() {
    local pct="$1"
    local msg="$2"
    echo "{\"percent\": ${pct}, \"status\": \"${msg}\"}" > "${PROGRESS_FILE}"
    echo "[${pct}%] ${msg}"
}

fail() {
    local code="$1"
    local msg="$2"
    progress 0 "FAILED: ${msg}"
    echo "ERROR: ${msg}" >&2
    exit "${code}"
}

# --- Phase 1: Pre-flight checks ---
progress 0 "Running pre-flight checks"

if ! python3 "${SCRIPT_DIR}/nixos_migration_calc.py" --json > /tmp/migration_checks.json 2>&1; then
    fail 1 "Pre-flight checks failed"
fi

# Verify WiFi is client mode
WIFI_MODE=$(python3 -c "import json; print(json.load(open('/tmp/migration_checks.json'))['wifi_mode'])")
if [ "${WIFI_MODE}" != "Client" ]; then
    fail 1 "WiFi must be in Client mode"
fi

progress 5 "Pre-flight OK"

# --- Phase 2: Download tarball ---
progress 10 "Downloading tarball"

# Use curl with progress output
if ! curl -L -f -o "${TARBALL}" \
    --progress-bar \
    "${MIGRATION_URL}" 2>&1 | while IFS= read -r line; do
        # Parse curl progress (rough percentage extraction)
        if [[ "$line" =~ ([0-9]+)\.[0-9]% ]]; then
            dl_pct="${BASH_REMATCH[1]}"
            # Map download 10-60%
            mapped_pct=$(( 10 + dl_pct * 50 / 100 ))
            progress "${mapped_pct}" "Downloading ${dl_pct}%"
        fi
    done; then
    fail 2 "Download failed"
fi

progress 60 "Verifying checksum"

# --- Phase 3: Verify checksum ---
ACTUAL_SHA256=$(sha256sum "${TARBALL}" | awk '{print $1}')
if [ "${ACTUAL_SHA256}" != "${MIGRATION_SHA256}" ]; then
    rm -f "${TARBALL}"
    fail 3 "Checksum mismatch"
fi

progress 65 "Checksum OK"

# --- Phase 4: Backup user data ---
progress 70 "Backing up user data"

# Tar PiFinder_data and write to raw SD offset (survives partition reformat)
if ! tar czf - -C "${PIFINDER_HOME}" PiFinder_data | \
    sudo dd of="${BACKUP_DEVICE}" bs=1M seek=$((BACKUP_OFFSET / 1024 / 1024)) \
    status=none 2>/dev/null; then
    fail 4 "Backup failed"
fi

# Write backup size marker (first 8 bytes at offset = tar size in bytes)
BACKUP_SIZE=$(tar czf - -C "${PIFINDER_HOME}" PiFinder_data | wc -c)
echo "${BACKUP_SIZE}" | sudo dd of="${BACKUP_DEVICE}" \
    bs=1 seek=$((BACKUP_OFFSET - 64)) count=20 conv=notrunc status=none

progress 78 "Backup complete"

# --- Phase 4b: Stage tarball to raw SD ---
# The initramfs can't access the root filesystem after reformatting,
# so we write the tarball to raw SD at an offset after the backup.
progress 80 "Staging tarball to SD"

BACKUP_ALIGNED=$(( (BACKUP_SIZE + 1048575) / 1048576 * 1048576 ))
TARBALL_OFFSET=$((BACKUP_OFFSET + BACKUP_ALIGNED + 1048576))
TARBALL_SIZE=$(stat -c%s "${TARBALL}")

# Write tarball size marker
echo "${TARBALL_SIZE}" | sudo dd of="${BACKUP_DEVICE}" \
    bs=1 seek=$((TARBALL_OFFSET - 64)) count=20 conv=notrunc status=none

# Write tarball to raw SD
sudo dd if="${TARBALL}" of="${BACKUP_DEVICE}" \
    bs=1M seek=$((TARBALL_OFFSET / 1048576)) status=none || fail 4 "Tarball staging failed"

progress 85 "Tarball staged"

# --- Phase 5: Build and stage initramfs ---
progress 85 "Staging initramfs"

rm -rf "${INITRAMFS_DIR}"
mkdir -p "${INITRAMFS_DIR}"/{bin,dev,proc,sys,mnt,tmp,boot}

# Copy essential binaries
for bin in busybox; do
    if command -v "${bin}" >/dev/null 2>&1; then
        cp "$(command -v "${bin}")" "${INITRAMFS_DIR}/bin/"
    fi
done

# Copy migration-specific binaries
cp "${PROGRESS_BIN}" "${INITRAMFS_DIR}/bin/" 2>/dev/null || true
cp "${INIT_SCRIPT}" "${INITRAMFS_DIR}/init"
chmod +x "${INITRAMFS_DIR}/init"

# Write metadata for init script
cat > "${INITRAMFS_DIR}/migration_meta.json" <<METAEOF
{
    "tarball": "${TARBALL}",
    "backup_offset": ${BACKUP_OFFSET},
    "backup_size": ${BACKUP_SIZE},
    "backup_device": "${BACKUP_DEVICE}"
}
METAEOF

# Create initramfs cpio archive
cd "${INITRAMFS_DIR}"
find . | cpio -o -H newc 2>/dev/null | gzip > /tmp/nixos_migration_initramfs.gz

# Stage to boot partition
sudo cp /tmp/nixos_migration_initramfs.gz "${BOOT_PARTITION}/initramfs-migration.gz"

# Create migration flag
sudo touch "${MIGRATION_FLAG}"

progress 95 "Initramfs staged"

# --- Phase 6: Configure boot ---
# Set tryboot to load migration initramfs on next boot
# This uses the RPi tryboot mechanism for safe boot
if [ -f "${BOOT_PARTITION}/config.txt" ]; then
    # Backup current config
    sudo cp "${BOOT_PARTITION}/config.txt" "${BOOT_PARTITION}/config.txt.bak"

    # Add initramfs line for migration
    echo "initramfs initramfs-migration.gz followkernel" | \
        sudo tee -a "${BOOT_PARTITION}/tryboot.txt" > /dev/null
fi

progress 100 "Ready to reboot"

echo "Migration staged successfully. Reboot to begin migration."
echo "The migration initramfs will take over and flash NixOS."
