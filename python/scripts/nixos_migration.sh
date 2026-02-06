#!/bin/bash
# nixos_migration.sh - Pre-migration: validate, download, backup, stage initramfs
#
# Called by PiFinder app (sys_utils.start_nixos_migration).
# Runs on RPi OS before rebooting into initramfs for the actual migration.
#
# The initramfs will:
#   1. Shrink root FS + partition to free space at end of 32GB SD
#   2. Copy tarball + backup from root to the freed staging area
#   3. Format both partitions
#   4. Extract NixOS from staging area
#   5. Restore user data and WiFi credentials
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
TARBALL="${PIFINDER_HOME}/pifinder-nixos-migration.tar.gz"
BACKUP_TAR="${PIFINDER_HOME}/pifinder_backup.tar.gz"
BOOT_PARTITION="/boot"
INITRAMFS_DIR="/tmp/nixos_initramfs"
PROGRESS_BIN="${SCRIPT_DIR}/migration_progress"
INIT_SCRIPT="${SCRIPT_DIR}/nixos_migration_init.sh"

# 8GB staging area at end of SD card (holds ~900MB tarball + full backup with images)
STAGING_SIZE_MB=8192

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

# Copy a binary and all its shared library dependencies into the initramfs.
copy_with_libs() {
    local bin_path="$1"
    local dest="$2"

    cp "${bin_path}" "${dest}/bin/"

    ldd "${bin_path}" 2>/dev/null | grep -oP '/\S+' | while read -r lib; do
        local dir
        dir=$(dirname "${lib}")
        mkdir -p "${dest}${dir}"
        cp -n "${lib}" "${dest}${dir}/" 2>/dev/null || true
    done
}

# --- Phase 1: Pre-flight checks ---
progress 0 "Running pre-flight checks"

if ! python3 "${SCRIPT_DIR}/nixos_migration_calc.py" --json > /tmp/migration_checks.json 2>&1; then
    fail 1 "Pre-flight checks failed"
fi

WIFI_MODE=$(python3 -c "import json; print(json.load(open('/tmp/migration_checks.json'))['wifi_mode'])")
if [ "${WIFI_MODE}" != "Client" ]; then
    fail 1 "WiFi must be in Client mode"
fi

progress 5 "Pre-flight OK"

# --- Phase 2: Download tarball ---
progress 10 "Downloading..."

if ! curl -L -f -o "${TARBALL}" \
    --progress-bar \
    "${MIGRATION_URL}" 2>&1 | while IFS= read -r line; do
        if [[ "$line" =~ ([0-9]+)\.[0-9]% ]]; then
            dl_pct="${BASH_REMATCH[1]}"
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

# --- Phase 4: Calculate sizes (backup created in initramfs to save space) ---
progress 68 "Calculating sizes"

TARBALL_SIZE=$(stat -c%s "${TARBALL}")

# Estimate backup size: PiFinder_data compressed ~50% typically
# Initramfs will stream backup directly to staging, so we just need estimate for header
PIFINDER_DATA="${PIFINDER_HOME}/PiFinder_data"
if [ -d "${PIFINDER_DATA}" ]; then
    DATA_SIZE=$(du -sb "${PIFINDER_DATA}" 2>/dev/null | cut -f1)
    BACKUP_SIZE_EST=$(( DATA_SIZE / 2 ))  # conservative compression estimate
else
    BACKUP_SIZE_EST=0
fi

progress 75 "Sizes calculated"

# --- Phase 5: Build initramfs ---
progress 78 "Building initramfs"

rm -rf "${INITRAMFS_DIR}"
mkdir -p "${INITRAMFS_DIR}"/{bin,lib,dev,proc,sys,mnt,tmp}

# Busybox (provides sh, mount, umount, dd, tar, gunzip, awk, sed, etc.)
if command -v busybox >/dev/null 2>&1; then
    copy_with_libs "$(command -v busybox)" "${INITRAMFS_DIR}"
else
    fail 5 "busybox not found"
fi

# Filesystem tools (required for shrink + reformat)
for tool in e2fsck resize2fs mke2fs mkfs.vfat sfdisk; do
    tool_path=$(command -v "${tool}" 2>/dev/null || true)
    if [ -z "${tool_path}" ]; then
        fail 5 "${tool} not found — install e2fsprogs dosfstools util-linux"
    fi
    copy_with_libs "${tool_path}" "${INITRAMFS_DIR}"
done

# mkfs.ext4 is typically a symlink to mke2fs
ln -sf mke2fs "${INITRAMFS_DIR}/bin/mkfs.ext4" 2>/dev/null || true

# OLED progress display (static binary, no libs needed)
cp "${PROGRESS_BIN}" "${INITRAMFS_DIR}/bin/" 2>/dev/null || true

# Dynamic linker — needed for non-busybox tools
LD_PATH=$(find /lib /lib64 /usr/lib -name "ld-linux-*" -type f 2>/dev/null | head -1)
if [ -n "${LD_PATH}" ]; then
    mkdir -p "${INITRAMFS_DIR}$(dirname "${LD_PATH}")"
    cp "${LD_PATH}" "${INITRAMFS_DIR}${LD_PATH}"
fi

# Init script
cp "${INIT_SCRIPT}" "${INITRAMFS_DIR}/init"
chmod +x "${INITRAMFS_DIR}/init"

# Metadata: paths + sizes so init script knows where to find things
# Note: backup is created by initramfs directly to staging (saves space on full disks)
cat > "${INITRAMFS_DIR}/migration_meta" <<METAEOF
TARBALL_PATH=${TARBALL}
PIFINDER_DATA_PATH=${PIFINDER_HOME}/PiFinder_data
TARBALL_SIZE=${TARBALL_SIZE}
BACKUP_SIZE_EST=${BACKUP_SIZE_EST}
STAGING_SIZE_MB=${STAGING_SIZE_MB}
METAEOF

progress 85 "Staging initramfs"

# --- Phase 6: Create and stage initramfs ---
cd "${INITRAMFS_DIR}"
find . | cpio -o -H newc 2>/dev/null | gzip > /tmp/nixos_migration_initramfs.gz

sudo cp /tmp/nixos_migration_initramfs.gz "${BOOT_PARTITION}/initramfs-migration.gz"

# Migration flag on boot partition (survives root format)
sudo touch "${BOOT_PARTITION}/nixos_migration"

progress 92 "Configuring boot"

# --- Phase 7: Configure boot to use migration initramfs ---
if [ -f "${BOOT_PARTITION}/config.txt" ]; then
    sudo cp "${BOOT_PARTITION}/config.txt" "${BOOT_PARTITION}/config.txt.premigration"

    # Add initramfs directive to config.txt for next boot
    echo "initramfs initramfs-migration.gz followkernel" | \
        sudo tee -a "${BOOT_PARTITION}/config.txt" > /dev/null
fi

progress 100 "Ready to reboot"

echo "Migration staged. Tarball: ${TARBALL_SIZE} bytes, Backup: ${BACKUP_SIZE} bytes"
echo "Reboot to begin NixOS migration."
