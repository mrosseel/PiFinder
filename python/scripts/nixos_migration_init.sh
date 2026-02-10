#!/bin/busybox sh
# nixos_migration_init.sh - Initramfs init for NixOS bootstrap migration
#
# Runs entirely from RAM. Strategy:
#   1. Save WiFi credentials to RAM FIRST (critical for recovery)
#   2. Shrink root FS + partition → frees space at end of SD
#   3. Copy tarball + backup from root to the freed staging area
#   4. Format both partitions
#   5. Extract minimal NixOS from staging area
#   6. Migrate WiFi IMMEDIATELY (before user data - enables network recovery)
#   7. Write resume metadata (allows phase 3 to resume if interrupted)
#   8. Restore user data, expand partition, reboot
#
# Phase 3 (bootstrap NixOS) will then:
#   - Resume user data restore if interrupted
#   - Run nixos-rebuild switch to become full PiFinder NixOS

set -e

/bin/busybox --install -s /bin 2>/dev/null || true

mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true

# Shared lib path for dynamically linked tools (e2fsck, mkfs, etc.)
export LD_LIBRARY_PATH=/lib:/usr/lib:/lib/aarch64-linux-gnu:/usr/lib/aarch64-linux-gnu

BOOT_DEV="/dev/mmcblk0p1"
ROOT_DEV="/dev/mmcblk0p2"
SD_DEV="/dev/mmcblk0"

# Wait for SD card device to appear
n=0
while [ ! -b "${BOOT_DEV}" ] && [ "${n}" -lt 30 ]; do
    sleep 1
    n=$((n + 1))
done
[ ! -b "${BOOT_DEV}" ] && fail "SD card not found after 30s: ${BOOT_DEV}"
MOUNT_ROOT="/mnt/root"
MOUNT_NEW="/mnt/new"
MOUNT_BOOT="/mnt/boot"
PROGRESS="/bin/migration_progress"

# Migration state directory on new root (for phase 3 resume)
MIGRATION_STATE_DIR="/var/lib/pifinder-migration"

STAGE_NUM=0
STAGE_TOTAL=25

show() {
    local pct="$1"
    local msg="$2"
    STAGE_NUM=$((STAGE_NUM + 1))
    echo "[${pct}%] ${msg}"
    [ -x "${PROGRESS}" ] && "${PROGRESS}" "${pct}" "${STAGE_NUM}" "${STAGE_TOTAL}" "${msg}" 2>/dev/null || true
}

fail() {
    [ -x "${PROGRESS}" ] && "${PROGRESS}" 0 0 0 "FAILED: $1" 2>/dev/null || true
    echo "[FAILED] $1"
    echo "MIGRATION FAILED: $1" > /dev/console 2>/dev/null || true
    echo "Dropping to shell for debugging..."
    exec /bin/sh
}

show 30 "Initramfs started"

# -------------------------------------------------------------------
# Phase 1: Validate and save WiFi credentials to RAM FIRST
# -------------------------------------------------------------------

# Check migration flag on boot partition
mkdir -p /mnt/bootchk
mount -t vfat -o ro "${BOOT_DEV}" /mnt/bootchk || fail "Cannot mount boot"
if [ ! -f /mnt/bootchk/nixos_migration ]; then
    umount /mnt/bootchk
    fail "No migration flag — aborting"
fi
umount /mnt/bootchk

# RAM check
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
MEM_MB=$((MEM_KB / 1024))
[ "${MEM_MB}" -lt 1800 ] && fail "Insufficient RAM: ${MEM_MB}MB (need 2048)"

# Read metadata written by pre-migration script
if [ ! -f /migration_meta ]; then
    fail "migration_meta not found in initramfs"
fi
. /migration_meta
# Now we have: TARBALL_PATH, PIFINDER_DATA_PATH, TARBALL_SIZE, BACKUP_SIZE_EST, STAGING_SIZE_MB

show 31 "Saving WiFi to RAM"

# Mount old root EARLY to save WiFi credentials before any destructive operations
mkdir -p "${MOUNT_ROOT}"
mount -t ext4 -o ro "${ROOT_DEV}" "${MOUNT_ROOT}" || fail "Cannot mount root"

# Save WiFi credentials to RAM FIRST - critical for recovery
mkdir -p /tmp/wifi
WPA_FILE="${MOUNT_ROOT}/etc/wpa_supplicant/wpa_supplicant.conf"
if [ -f "${WPA_FILE}" ]; then
    cp "${WPA_FILE}" /tmp/wifi/wpa_supplicant.conf
fi

# Also check for existing NetworkManager connections
NM_SRC="${MOUNT_ROOT}/etc/NetworkManager/system-connections"
if [ -d "${NM_SRC}" ]; then
    mkdir -p /tmp/wifi/nm-connections
    cp -a "${NM_SRC}/." /tmp/wifi/nm-connections/ 2>/dev/null || true
fi

# Verify required files exist
TARBALL_ON_ROOT="${MOUNT_ROOT}${TARBALL_PATH}"
PIFINDER_DATA_ON_ROOT="${MOUNT_ROOT}${PIFINDER_DATA_PATH}"

[ ! -f "${TARBALL_ON_ROOT}" ] && { umount "${MOUNT_ROOT}"; fail "Tarball not found: ${TARBALL_PATH}"; }
[ ! -d "${PIFINDER_DATA_ON_ROOT}" ] && { umount "${MOUNT_ROOT}"; fail "PiFinder_data not found: ${PIFINDER_DATA_PATH}"; }

umount "${MOUNT_ROOT}"

show 32 "Validated"

# -------------------------------------------------------------------
# Phase 2: Check root filesystem
# -------------------------------------------------------------------

show 34 "Checking filesystem"
e2fsck -f -y "${ROOT_DEV}" || fail "e2fsck failed on root"

# -------------------------------------------------------------------
# Phase 3: Shrink root FS + partition to free staging area
# -------------------------------------------------------------------

show 36 "Shrinking root FS"

SD_BYTES=$(blockdev --getsize64 "${SD_DEV}")
SD_SECTORS=$(blockdev --getsz "${SD_DEV}")

# Get current p2 start sector
P2_START=$(sfdisk -d "${SD_DEV}" 2>/dev/null | grep 'mmcblk0p2' | sed 's/.*start= *//' | sed 's/,.*//')
[ -z "${P2_START}" ] && fail "Cannot read p2 start sector"

# Calculate new p2 size: current size minus staging area
STAGING_SECTORS=$(( STAGING_SIZE_MB * 1024 * 1024 / 512 ))
P2_CURRENT_SECTORS=$(( SD_SECTORS - P2_START ))
P2_NEW_SECTORS=$(( P2_CURRENT_SECTORS - STAGING_SECTORS ))

[ "${P2_NEW_SECTORS}" -le 0 ] && fail "SD card too small for staging area"

# Shrink the ext4 filesystem first
BLOCK_SIZE=4096
P2_NEW_BLOCKS=$(( P2_NEW_SECTORS * 512 / BLOCK_SIZE ))

resize2fs "${ROOT_DEV}" "${P2_NEW_BLOCKS}" || fail "resize2fs failed"

show 38 "Shrinking partition"

echo "${P2_START}, ${P2_NEW_SECTORS}" | sfdisk -N 2 "${SD_DEV}" --no-reread 2>/dev/null || fail "sfdisk failed"
partprobe "${SD_DEV}" 2>/dev/null || blockdev --rereadpt "${SD_DEV}" 2>/dev/null || true
sleep 1

# Staging area starts right after the shrunk partition
STAGING_START_BYTE=$(( (P2_START + P2_NEW_SECTORS) * 512 ))

show 40 "Staging area ready"

# -------------------------------------------------------------------
# Phase 4: Copy tarball + backup to staging area
# -------------------------------------------------------------------

show 42 "Copying to staging"

mount -t ext4 -o ro "${ROOT_DEV}" "${MOUNT_ROOT}" || fail "Cannot mount shrunk root"

TARBALL_ON_ROOT="${MOUNT_ROOT}${TARBALL_PATH}"
PIFINDER_DATA_ON_ROOT="${MOUNT_ROOT}${PIFINDER_DATA_PATH}"

# Data layout in staging area:
#   offset 0:                    header (4096 bytes)
#   offset 4096:                 tarball (TARBALL_SIZE bytes)
#   offset 4096+TARBALL_ALIGNED: backup
TARBALL_ALIGNED=$(( (TARBALL_SIZE + 4095) / 4096 * 4096 ))

TARBALL_STAGING_BYTE=$(( STAGING_START_BYTE + 4096 ))
BACKUP_STAGING_BYTE=$(( TARBALL_STAGING_BYTE + TARBALL_ALIGNED ))

show 44 "Copying tarball"

dd if="${TARBALL_ON_ROOT}" of="${SD_DEV}" bs=4096 \
    seek=$(( TARBALL_STAGING_BYTE / 4096 )) conv=notrunc 2>/dev/null || fail "Tarball staging failed"

show 48 "Creating backup"

# Create backup in tmpfs (RAM) then copy to staging
# Busybox tar doesn't support --exclude, so we selectively include:
# - All files in PiFinder_data root (observations.db, config, etc.)
# - obslists directory
# - Truncated log (last 1000 lines)
# Skip: captures, screenshots, dumps, logs (ephemeral/large data)
BACKUP_TMP="/tmp/pifinder_backup.tar.gz"
BACKUP_STAGE="/tmp/backup_stage/PiFinder_data"
rm -rf /tmp/backup_stage
mkdir -p "${BACKUP_STAGE}"

# Copy root-level files (observations.db, configs, etc.)
for f in "${PIFINDER_DATA_ON_ROOT}"/*; do
    [ -f "$f" ] && cp "$f" "${BACKUP_STAGE}/" 2>/dev/null || true
done

# Truncate log to last 1000 lines
if [ -f "${BACKUP_STAGE}/pifinder.log" ]; then
    tail -n 1000 "${BACKUP_STAGE}/pifinder.log" > "${BACKUP_STAGE}/pifinder.log.tmp"
    mv "${BACKUP_STAGE}/pifinder.log.tmp" "${BACKUP_STAGE}/pifinder.log"
fi

# Copy obslists directory
if [ -d "${PIFINDER_DATA_ON_ROOT}/obslists" ]; then
    cp -a "${PIFINDER_DATA_ON_ROOT}/obslists" "${BACKUP_STAGE}/obslists"
fi

tar czf "${BACKUP_TMP}" -C /tmp/backup_stage PiFinder_data || fail "Backup creation failed"
rm -rf /tmp/backup_stage

BACKUP_SIZE=$(wc -c < "${BACKUP_TMP}")
show 50 "Backup: ${BACKUP_SIZE} bytes"

show 51 "Copying backup to staging"
dd if="${BACKUP_TMP}" of="${SD_DEV}" bs=4096 \
    seek=$(( BACKUP_STAGING_BYTE / 4096 )) conv=notrunc 2>/dev/null || fail "Backup staging failed"
rm -f "${BACKUP_TMP}"

umount "${MOUNT_ROOT}"

# Write header with actual backup size (PFMIGRATE2 = bootstrap flow)
HEADER_FILE="/tmp/staging_header"
dd if=/dev/zero of="${HEADER_FILE}" bs=4096 count=1 2>/dev/null
printf "PFMIGRATE2\ntarball_size=%s\nbackup_size=%s\n" \
    "${TARBALL_SIZE}" "${BACKUP_SIZE}" | dd of="${HEADER_FILE}" conv=notrunc 2>/dev/null
dd if="${HEADER_FILE}" of="${SD_DEV}" bs=4096 \
    seek=$(( STAGING_START_BYTE / 4096 )) conv=notrunc 2>/dev/null

show 53 "Staging complete"

# Verify header
MAGIC=$(dd if="${SD_DEV}" bs=4096 skip=$(( STAGING_START_BYTE / 4096 )) count=1 2>/dev/null | head -1 || true)
[ "${MAGIC}" != "PFMIGRATE2" ] && fail "Staging header verification failed"

# ===================================================================
# POINT OF NO RETURN
# ===================================================================

show 54 "FORMATTING"

# Format boot (FAT32)
mkfs.vfat -F 32 -n FIRMWARE "${BOOT_DEV}" || fail "mkfs.vfat failed"

show 56 "Format root"
mkfs.ext4 -F -L NIXOS_SD "${ROOT_DEV}" || fail "mkfs.ext4 failed"

# -------------------------------------------------------------------
# Phase 5: Extract minimal NixOS from staging area
# -------------------------------------------------------------------

show 58 "Extracting NixOS"

mkdir -p "${MOUNT_NEW}"
mount -t ext4 "${ROOT_DEV}" "${MOUNT_NEW}" || fail "Cannot mount new root"

TARBALL_SKIP_BLOCKS=$(( TARBALL_STAGING_BYTE / 4096 ))
TARBALL_COUNT_BLOCKS=$(( (TARBALL_SIZE + 4095) / 4096 ))

dd if="${SD_DEV}" bs=4096 skip="${TARBALL_SKIP_BLOCKS}" count="${TARBALL_COUNT_BLOCKS}" 2>/dev/null | \
    gunzip | tar xf - -C "${MOUNT_NEW}" || fail "Tarball extraction failed"

# Move boot/ contents to boot partition
mkdir -p "${MOUNT_BOOT}"
mount -t vfat "${BOOT_DEV}" "${MOUNT_BOOT}" || fail "Cannot mount new boot"

if [ -d "${MOUNT_NEW}/boot" ]; then
    cp -a "${MOUNT_NEW}/boot/." "${MOUNT_BOOT}/"
    rm -rf "${MOUNT_NEW}/boot"
fi

# Move rootfs/ contents to actual root (tarball has rootfs/ prefix)
if [ -d "${MOUNT_NEW}/rootfs" ]; then
    for item in "${MOUNT_NEW}/rootfs"/*; do
        [ -e "$item" ] && mv "$item" "${MOUNT_NEW}/" 2>/dev/null || true
    done
    for item in "${MOUNT_NEW}/rootfs"/.*; do
        case "$(basename "$item")" in .|..) continue;; esac
        mv "$item" "${MOUNT_NEW}/" 2>/dev/null || true
    done
    rmdir "${MOUNT_NEW}/rootfs" 2>/dev/null || true
fi

rm -f "${MOUNT_NEW}/manifest.json"

# -------------------------------------------------------------------
# Phase 6: Migrate WiFi IMMEDIATELY (before user data)
# -------------------------------------------------------------------
# This is critical: if we crash during user data restore, minimal NixOS
# will still have network access for phase 3 to complete the migration.
#
# Bootstrap NixOS uses iwd (not NetworkManager) for minimal size.
# iwd stores networks in /var/lib/iwd/{SSID}.psk files.

show 60 "Migrating WiFi (EARLY)"

IWD_DIR="${MOUNT_NEW}/var/lib/iwd"
mkdir -p "${IWD_DIR}"
chmod 700 "${IWD_DIR}"

# Parse wpa_supplicant.conf and create iwd network files
if [ -f /tmp/wifi/wpa_supplicant.conf ]; then
    SSID=""
    PSK=""
    IN_NET=0

    while IFS= read -r line; do
        line=$(echo "${line}" | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')

        case "${line}" in
            network=*)
                IN_NET=1
                SSID=""
                PSK=""
                ;;
            "}")
                if [ "${IN_NET}" = "1" ] && [ -n "${SSID}" ]; then
                    # iwd filename format: encode special chars as =XX (hex)
                    # For simplicity, replace common problematic chars
                    IWD_FN=$(printf '%s' "${SSID}" | sed 's/ /=20/g; s/\//=2f/g; s/\\/=5c/g')

                    if [ -n "${PSK}" ]; then
                        # WPA-PSK network
                        IWD_FILE="${IWD_DIR}/${IWD_FN}.psk"
                        cat > "${IWD_FILE}" <<IWDEOF
[Security]
Passphrase=${PSK}
IWDEOF
                    else
                        # Open network (no PSK)
                        IWD_FILE="${IWD_DIR}/${IWD_FN}.open"
                        cat > "${IWD_FILE}" <<IWDEOF
[Settings]
AutoConnect=true
IWDEOF
                    fi

                    chmod 600 "${IWD_FILE}"
                fi
                IN_NET=0
                ;;
            ssid=*)
                [ "${IN_NET}" = "1" ] && SSID=$(echo "${line}" | sed 's/^ssid="//' | sed 's/"$//')
                ;;
            psk=*)
                [ "${IN_NET}" = "1" ] && PSK=$(echo "${line}" | sed 's/^psk="//' | sed 's/"$//')
                ;;
        esac
    done < /tmp/wifi/wpa_supplicant.conf
fi

# Force WiFi configs to disk immediately
sync

show 61 "WiFi migrated"

# -------------------------------------------------------------------
# Phase 7: Write resume metadata for phase 3
# -------------------------------------------------------------------
# If we crash during user data restore, phase 3 can read this metadata
# and resume the restore from the staging area.

show 62 "Writing resume metadata"

STAGING_STATE_DIR="${MOUNT_NEW}${MIGRATION_STATE_DIR}"
mkdir -p "${STAGING_STATE_DIR}"

# Calculate offsets for phase 3 to use
STAGING_OFFSET_BLOCKS=$(( STAGING_START_BYTE / 4096 ))
BACKUP_OFFSET_BLOCKS=$(( BACKUP_STAGING_BYTE / 4096 ))

cat > "${STAGING_STATE_DIR}/staging-meta" <<EOF
SD_DEV=${SD_DEV}
STAGING_OFFSET_BLOCKS=${STAGING_OFFSET_BLOCKS}
BACKUP_OFFSET_BLOCKS=${BACKUP_OFFSET_BLOCKS}
BACKUP_SIZE=${BACKUP_SIZE}
EOF

cat > "${STAGING_STATE_DIR}/state" <<EOF
PHASE=2
PERCENT=62
STATUS=Restoring user data
EOF

sync

# -------------------------------------------------------------------
# Phase 8: Restore user data
# -------------------------------------------------------------------

show 63 "Restoring user data"

mkdir -p "${MOUNT_NEW}/home/pifinder"

BACKUP_SKIP_BLOCKS=$(( BACKUP_STAGING_BYTE / 4096 ))
BACKUP_COUNT_BLOCKS=$(( (BACKUP_SIZE + 4095) / 4096 ))

dd if="${SD_DEV}" bs=4096 skip="${BACKUP_SKIP_BLOCKS}" count="${BACKUP_COUNT_BLOCKS}" 2>/dev/null | \
    gunzip | tar xf - -C "${MOUNT_NEW}/home/pifinder/" || fail "User data restore failed"

# Mark restore complete (so phase 3 doesn't try to resume)
touch "${STAGING_STATE_DIR}/restore-complete"

show 66 "User data restored"

# -------------------------------------------------------------------
# Phase 9: Finalize
# -------------------------------------------------------------------

show 67 "Setting permissions"

# pifinder user: UID 1000, GID 100 (users) on NixOS
chown -R 1000:100 "${MOUNT_NEW}/home/pifinder" 2>/dev/null || true

# Update state
cat > "${STAGING_STATE_DIR}/state" <<EOF
PHASE=2
PERCENT=68
STATUS=Expanding partition
EOF

umount "${MOUNT_NEW}"

show 68 "Expanding partition"

# Expand partition to fill card
echo ", +" | sfdisk -N 2 "${SD_DEV}" --no-reread 2>/dev/null || true
partprobe "${SD_DEV}" 2>/dev/null || blockdev --rereadpt "${SD_DEV}" 2>/dev/null || true
sleep 1

# Expand filesystem
resize2fs "${ROOT_DEV}" 2>/dev/null || true

show 69 "Syncing"
sync

umount "${MOUNT_BOOT}" 2>/dev/null || true

show 70 "Ready for phase 3"
sleep 2

echo "Rebooting into bootstrap NixOS..."
echo "Phase 3 will run nixos-rebuild switch to complete migration."
reboot -f
