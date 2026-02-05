#!/bin/busybox sh
# nixos_migration_init.sh - Initramfs init for NixOS migration
#
# Runs entirely from RAM. Strategy:
#   1. Shrink root FS + partition → frees space at end of 32GB SD
#   2. Copy tarball + backup from root to the freed staging area (raw SD)
#   3. Format both partitions
#   4. Extract NixOS from staging area
#   5. Restore user data, migrate WiFi, reboot
#
# This avoids the chicken-and-egg problem (tarball on root, need to format root)
# by creating a safe staging area beyond the filesystem.

set -e

/bin/busybox --install -s /bin 2>/dev/null || true

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev

# Shared lib path for dynamically linked tools (e2fsck, mkfs, etc.)
export LD_LIBRARY_PATH=/lib:/usr/lib:/lib/aarch64-linux-gnu:/usr/lib/aarch64-linux-gnu

BOOT_DEV="/dev/mmcblk0p1"
ROOT_DEV="/dev/mmcblk0p2"
SD_DEV="/dev/mmcblk0"
MOUNT_ROOT="/mnt/root"
MOUNT_NEW="/mnt/new"
MOUNT_BOOT="/mnt/boot"
PROGRESS="/bin/migration_progress"

show() {
    local pct="$1"
    local msg="$2"
    echo "[${pct}%] ${msg}"
    [ -x "${PROGRESS}" ] && "${PROGRESS}" "${pct}" "${msg}" 2>/dev/null || true
}

fail() {
    show 0 "FAILED: $1"
    echo "MIGRATION FAILED: $1" > /dev/console 2>/dev/null || true
    echo "Dropping to shell for debugging..."
    exec /bin/sh
}

show 0 "Starting migration"

# -------------------------------------------------------------------
# Phase 1: Validate
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
# Now we have: TARBALL_PATH, BACKUP_PATH, TARBALL_SIZE, BACKUP_SIZE, STAGING_SIZE_MB

show 3 "Validated"

# -------------------------------------------------------------------
# Phase 2: Check root filesystem, read data locations
# -------------------------------------------------------------------

show 5 "Checking filesystem"

e2fsck -f -y "${ROOT_DEV}" || fail "e2fsck failed on root"

# Mount old root to verify files exist
mkdir -p "${MOUNT_ROOT}"
mount -t ext4 -o ro "${ROOT_DEV}" "${MOUNT_ROOT}" || fail "Cannot mount root"

TARBALL_ON_ROOT="${MOUNT_ROOT}${TARBALL_PATH}"
BACKUP_ON_ROOT="${MOUNT_ROOT}${BACKUP_PATH}"

[ ! -f "${TARBALL_ON_ROOT}" ] && { umount "${MOUNT_ROOT}"; fail "Tarball not found: ${TARBALL_PATH}"; }
[ ! -f "${BACKUP_ON_ROOT}" ] && { umount "${MOUNT_ROOT}"; fail "Backup not found: ${BACKUP_PATH}"; }

# Save WiFi credentials to RAM before we lose access to old root
WPA_FILE="${MOUNT_ROOT}/etc/wpa_supplicant/wpa_supplicant.conf"
mkdir -p /tmp/wifi
if [ -f "${WPA_FILE}" ]; then
    cp "${WPA_FILE}" /tmp/wifi/wpa_supplicant.conf
fi

umount "${MOUNT_ROOT}"

show 10 "Files verified"

# -------------------------------------------------------------------
# Phase 3: Shrink root FS + partition to free staging area at end of SD
# -------------------------------------------------------------------
# 32GB card. We shrink root by STAGING_SIZE_MB to create raw space at the
# end of the SD card. The staging area will hold the tarball + backup
# while we reformat the partitions.

show 12 "Shrinking root FS"

SD_BYTES=$(blockdev --getsize64 "${SD_DEV}")
SD_SECTORS=$(blockdev --getsz "${SD_DEV}")

# Get current p2 start sector from partition table
P2_START=$(sfdisk -d "${SD_DEV}" 2>/dev/null | awk '/mmcblk0p2/ {
    for (i=1; i<=NF; i++) {
        if ($i ~ /^start=/) { gsub(/start=/, "", $i); gsub(/,/, "", $i); print $i }
    }
}')
[ -z "${P2_START}" ] && fail "Cannot read p2 start sector"

# Calculate new p2 size: current size minus staging area
STAGING_SECTORS=$(( STAGING_SIZE_MB * 1024 * 1024 / 512 ))
P2_CURRENT_SECTORS=$(( SD_SECTORS - P2_START ))
P2_NEW_SECTORS=$(( P2_CURRENT_SECTORS - STAGING_SECTORS ))

[ "${P2_NEW_SECTORS}" -le 0 ] && fail "SD card too small for staging area"

# Shrink the ext4 filesystem first (resize2fs wants block count)
# ext4 block size is typically 4096
BLOCK_SIZE=4096
P2_NEW_BLOCKS=$(( P2_NEW_SECTORS * 512 / BLOCK_SIZE ))

resize2fs "${ROOT_DEV}" "${P2_NEW_BLOCKS}" || fail "resize2fs failed"

show 18 "Shrinking partition"

# Shrink the partition table entry to match
# sfdisk -N 2: modify partition 2, keep start, set new size
echo "${P2_START}, ${P2_NEW_SECTORS}" | sfdisk -N 2 "${SD_DEV}" --no-reread 2>/dev/null || fail "sfdisk failed"

# Force kernel to re-read partition table
partprobe "${SD_DEV}" 2>/dev/null || blockdev --rereadpt "${SD_DEV}" 2>/dev/null || true
sleep 1

# Staging area starts right after the shrunk partition
STAGING_START_BYTE=$(( (P2_START + P2_NEW_SECTORS) * 512 ))

show 20 "Staging area ready"

# -------------------------------------------------------------------
# Phase 4: Copy tarball + backup from root to staging area
# -------------------------------------------------------------------

show 22 "Copying to staging"

# Mount the (now smaller) root FS read-only
mount -t ext4 -o ro "${ROOT_DEV}" "${MOUNT_ROOT}" || fail "Cannot mount shrunk root"

# Write staging header (4096 bytes, one block)
# Format: magic line, then key=value pairs, zero-padded to 4096 bytes
HEADER_FILE="/tmp/staging_header"
dd if=/dev/zero of="${HEADER_FILE}" bs=4096 count=1 2>/dev/null
printf "PFMIGRATE1\ntarball_size=%s\nbackup_size=%s\n" \
    "${TARBALL_SIZE}" "${BACKUP_SIZE}" | dd of="${HEADER_FILE}" conv=notrunc 2>/dev/null
dd if="${HEADER_FILE}" of="${SD_DEV}" bs=4096 \
    seek=$(( STAGING_START_BYTE / 4096 )) conv=notrunc 2>/dev/null

# Data layout in staging area:
#   offset 0:                    header (4096 bytes)
#   offset 4096:                 tarball (TARBALL_SIZE bytes)
#   offset 4096+TARBALL_ALIGNED: backup (BACKUP_SIZE bytes)
TARBALL_ALIGNED=$(( (TARBALL_SIZE + 4095) / 4096 * 4096 ))

TARBALL_STAGING_BYTE=$(( STAGING_START_BYTE + 4096 ))
BACKUP_STAGING_BYTE=$(( TARBALL_STAGING_BYTE + TARBALL_ALIGNED ))

show 25 "Copying tarball"

dd if="${TARBALL_ON_ROOT}" of="${SD_DEV}" bs=4096 \
    seek=$(( TARBALL_STAGING_BYTE / 4096 )) conv=notrunc 2>/dev/null || fail "Tarball staging failed"

show 35 "Copying backup"

dd if="${BACKUP_ON_ROOT}" of="${SD_DEV}" bs=4096 \
    seek=$(( BACKUP_STAGING_BYTE / 4096 )) conv=notrunc 2>/dev/null || fail "Backup staging failed"

umount "${MOUNT_ROOT}"

show 40 "Staging complete"

# Verify header readback
MAGIC=$(dd if="${SD_DEV}" bs=4096 skip=$(( STAGING_START_BYTE / 4096 )) count=1 2>/dev/null | head -1)
[ "${MAGIC}" != "PFMIGRATE1" ] && fail "Staging header verification failed"

show 42 "Staging verified"

# ===================================================================
# POINT OF NO RETURN
# ===================================================================

show 45 "FORMATTING"

# --- Format boot (FAT32) ---
show 48 "Format boot"
mkfs.vfat -F 32 -n FIRMWARE "${BOOT_DEV}" || fail "mkfs.vfat failed"

# --- Format root (ext4) ---
# Recreate p2 at full original size (reclaim staging area — it's raw, survives)
# Actually: we must NOT expand p2 yet, staging data is in that space.
# Format only the shrunk p2 — staging area is beyond it and safe.
show 50 "Format root"
mkfs.ext4 -F -L NIXOS_SD "${ROOT_DEV}" || fail "mkfs.ext4 failed"

show 55 "Formatted"

# -------------------------------------------------------------------
# Phase 5: Extract NixOS from staging area
# -------------------------------------------------------------------

show 57 "Extracting NixOS"

mkdir -p "${MOUNT_NEW}"
mount -t ext4 "${ROOT_DEV}" "${MOUNT_NEW}" || fail "Cannot mount new root"

# Extract tarball from staging area → new root
# The .tar.gz contains: boot/, rootfs/, manifest.json
# Extract everything first, then move boot/ to the boot partition.
TARBALL_SKIP_BLOCKS=$(( TARBALL_STAGING_BYTE / 4096 ))
TARBALL_COUNT_BLOCKS=$(( (TARBALL_SIZE + 4095) / 4096 ))

dd if="${SD_DEV}" bs=4096 skip="${TARBALL_SKIP_BLOCKS}" count="${TARBALL_COUNT_BLOCKS}" 2>/dev/null | \
    gunzip | tar xf - -C "${MOUNT_NEW}" || fail "Tarball extraction failed"

show 70 "Rootfs extracted"

# Move boot/ contents to boot partition
mkdir -p "${MOUNT_BOOT}"
mount -t vfat "${BOOT_DEV}" "${MOUNT_BOOT}" || fail "Cannot mount new boot"

if [ -d "${MOUNT_NEW}/boot" ]; then
    cp -a "${MOUNT_NEW}/boot/." "${MOUNT_BOOT}/"
    rm -rf "${MOUNT_NEW}/boot"
fi

show 75 "Boot extracted"

# Move rootfs/ contents to actual root (tarball has rootfs/ prefix)
if [ -d "${MOUNT_NEW}/rootfs" ]; then
    # Move everything from rootfs/ up one level
    cd "${MOUNT_NEW}/rootfs"
    for item in .* *; do
        [ "${item}" = "." ] || [ "${item}" = ".." ] && continue
        mv "${item}" "${MOUNT_NEW}/" 2>/dev/null || cp -a "${item}" "${MOUNT_NEW}/" && rm -rf "${item}"
    done
    cd /
    rmdir "${MOUNT_NEW}/rootfs" 2>/dev/null || true
fi

# Clean up manifest from root
rm -f "${MOUNT_NEW}/manifest.json"

show 78 "Layout complete"

# -------------------------------------------------------------------
# Phase 6: Restore user data
# -------------------------------------------------------------------

show 80 "Restoring user data"

mkdir -p "${MOUNT_NEW}/home/pifinder"

BACKUP_SKIP_BLOCKS=$(( BACKUP_STAGING_BYTE / 4096 ))
BACKUP_COUNT_BLOCKS=$(( (BACKUP_SIZE + 4095) / 4096 ))

dd if="${SD_DEV}" bs=4096 skip="${BACKUP_SKIP_BLOCKS}" count="${BACKUP_COUNT_BLOCKS}" 2>/dev/null | \
    gunzip | tar xf - -C "${MOUNT_NEW}/home/pifinder/" || fail "User data restore failed"

show 85 "User data restored"

# -------------------------------------------------------------------
# Phase 7: Migrate WiFi credentials (wpa_supplicant → NetworkManager)
# -------------------------------------------------------------------

show 88 "Migrating WiFi"

if [ -f /tmp/wifi/wpa_supplicant.conf ]; then
    NM_DIR="${MOUNT_NEW}/etc/NetworkManager/system-connections"
    mkdir -p "${NM_DIR}"

    # Parse each network block and create an NM connection file
    SSID=""
    PSK=""
    KEY_MGMT=""
    IN_NET=0

    while IFS= read -r line; do
        # Trim whitespace
        line=$(echo "${line}" | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')

        case "${line}" in
            network=*)
                IN_NET=1
                SSID=""
                PSK=""
                KEY_MGMT=""
                ;;
            "}")
                if [ "${IN_NET}" = "1" ] && [ -n "${SSID}" ]; then
                    # Sanitize SSID for filename
                    FN=$(echo "${SSID}" | sed 's/[^a-zA-Z0-9_-]/_/g')
                    CONN_FILE="${NM_DIR}/${FN}.nmconnection"

                    cat > "${CONN_FILE}" <<NMEOF
[connection]
id=${SSID}
type=wifi
autoconnect=true

[wifi]
ssid=${SSID}
mode=infrastructure

NMEOF

                    if [ -n "${PSK}" ]; then
                        cat >> "${CONN_FILE}" <<NMEOF
[wifi-security]
key-mgmt=wpa-psk
psk=${PSK}

NMEOF
                    fi

                    cat >> "${CONN_FILE}" <<NMEOF
[ipv4]
method=auto

[ipv6]
method=auto
NMEOF

                    chmod 600 "${CONN_FILE}"
                fi
                IN_NET=0
                ;;
            ssid=*)
                if [ "${IN_NET}" = "1" ]; then
                    SSID=$(echo "${line}" | sed 's/^ssid="//' | sed 's/"$//')
                fi
                ;;
            psk=*)
                if [ "${IN_NET}" = "1" ]; then
                    PSK=$(echo "${line}" | sed 's/^psk="//' | sed 's/"$//')
                fi
                ;;
            key_mgmt=*)
                if [ "${IN_NET}" = "1" ]; then
                    KEY_MGMT=$(echo "${line}" | sed 's/^key_mgmt=//')
                fi
                ;;
        esac
    done < /tmp/wifi/wpa_supplicant.conf
fi

show 92 "WiFi migrated"

# -------------------------------------------------------------------
# Phase 8: Finalize
# -------------------------------------------------------------------

show 95 "Setting permissions"

# pifinder user: UID 1000, GID 100 (users) on NixOS
chown -R 1000:100 "${MOUNT_NEW}/home/pifinder" 2>/dev/null || true

# Now expand root partition back to full SD card size
# (reclaiming the staging area which we no longer need)
umount "${MOUNT_NEW}"

# Expand partition to fill card
echo "${P2_START}," | sfdisk -N 2 "${SD_DEV}" --no-reread 2>/dev/null || true
partprobe "${SD_DEV}" 2>/dev/null || blockdev --rereadpt "${SD_DEV}" 2>/dev/null || true
sleep 1

# Expand filesystem to fill expanded partition
resize2fs "${ROOT_DEV}" 2>/dev/null || true

show 98 "Syncing"
sync

umount "${MOUNT_BOOT}" 2>/dev/null || true

show 100 "Migration complete!"
sleep 3

echo "Rebooting into NixOS..."
reboot -f
