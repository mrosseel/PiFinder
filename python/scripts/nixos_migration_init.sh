#!/bin/busybox sh
# nixos_migration_init.sh - Initramfs init script for NixOS migration.
#
# Runs from RAM after reboot. Reformats boot+root partitions and
# extracts NixOS from the pre-downloaded tarball.
#
# Key insight: both RPi OS and NixOS use 2 partitions (boot FAT32 + root ext4),
# so NO repartition is needed — just reformat in place.
#
# This script is copied to /init in the initramfs.

set -e

# Install busybox symlinks
/bin/busybox --install -s /bin 2>/dev/null || true

# Mount virtual filesystems
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev

BOOT_DEV="/dev/mmcblk0p1"
ROOT_DEV="/dev/mmcblk0p2"
SD_DEV="/dev/mmcblk0"
MOUNT_OLD="/mnt/old"
MOUNT_NEW="/mnt/new"
MOUNT_BOOT="/mnt/boot"
PROGRESS="/bin/migration_progress"

# Backup offset (14GB in, matches nixos_migration.sh)
BACKUP_OFFSET=$((14 * 1024 * 1024 * 1024))

show_progress() {
    local pct="$1"
    local msg="$2"
    echo "[${pct}%] ${msg}"
    if [ -x "${PROGRESS}" ]; then
        "${PROGRESS}" "${pct}" "${msg}" 2>/dev/null || true
    fi
}

fail() {
    local msg="$1"
    show_progress 0 "FAILED: ${msg}"
    echo "MIGRATION FAILED: ${msg}" > /dev/console
    # Drop to shell for debugging
    exec /bin/sh
}

show_progress 0 "Starting migration"

# --- Verify migration flag ---
mkdir -p "${MOUNT_OLD}"
mount -t ext4 -o ro "${ROOT_DEV}" "${MOUNT_OLD}" || fail "Cannot mount old root"

if [ ! -f "${MOUNT_OLD}/boot/nixos_migration" ] && [ ! -f "/boot/nixos_migration" ]; then
    show_progress 0 "No migration flag"
    umount "${MOUNT_OLD}"
    fail "Migration flag not found - aborting"
fi

# Read tarball path from old root
TARBALL=""
if [ -f "${MOUNT_OLD}/migration_meta.json" ]; then
    # Simple JSON parsing with sed
    TARBALL=$(sed -n 's/.*"tarball".*:.*"\(.*\)".*/\1/p' "${MOUNT_OLD}/migration_meta.json")
fi
if [ -z "${TARBALL}" ]; then
    TARBALL="/home/pifinder/pifinder-nixos-migration.tar.zst"
fi

# Verify tarball exists on old root
if [ ! -f "${MOUNT_OLD}/${TARBALL#/}" ] && [ ! -f "${TARBALL}" ]; then
    umount "${MOUNT_OLD}"
    fail "Tarball not found: ${TARBALL}"
fi

# If tarball is on the root partition, copy to RAM first
TARBALL_PATH="${MOUNT_OLD}/${TARBALL#/}"
if [ ! -f "${TARBALL_PATH}" ]; then
    TARBALL_PATH="${TARBALL}"
fi

show_progress 5 "Verifying backup"

# --- Safety checks ---
# Check RAM
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
MEM_MB=$((MEM_KB / 1024))
if [ "${MEM_MB}" -lt 1800 ]; then
    umount "${MOUNT_OLD}"
    fail "Insufficient RAM: ${MEM_MB}MB"
fi

# Read backup size marker
BACKUP_SIZE=$(dd if="${SD_DEV}" bs=1 skip=$((BACKUP_OFFSET - 64)) count=20 2>/dev/null | tr -d '\0')
if [ -z "${BACKUP_SIZE}" ] || [ "${BACKUP_SIZE}" -le 0 ] 2>/dev/null; then
    umount "${MOUNT_OLD}"
    fail "No valid backup found"
fi

# Save WiFi credentials from old system before unmount
WPA_CONF=""
if [ -f "${MOUNT_OLD}/etc/wpa_supplicant/wpa_supplicant.conf" ]; then
    WPA_CONF=$(cat "${MOUNT_OLD}/etc/wpa_supplicant/wpa_supplicant.conf")
fi

umount "${MOUNT_OLD}"

show_progress 10 "Checks passed"

# ============================================
# POINT OF NO RETURN
# ============================================
show_progress 15 "REFORMATTING"

# --- Format boot partition (FAT32) ---
show_progress 20 "Format boot (FAT32)"
mkfs.vfat -F 32 -n FIRMWARE "${BOOT_DEV}" || fail "mkfs.vfat failed"

# --- Format root partition (ext4) ---
show_progress 25 "Format root (ext4)"
mkfs.ext4 -F -L NIXOS_SD "${ROOT_DEV}" || fail "mkfs.ext4 failed"

show_progress 30 "Partitions formatted"

# --- Extract rootfs ---
show_progress 35 "Extracting rootfs"
mkdir -p "${MOUNT_NEW}"
mount -t ext4 "${ROOT_DEV}" "${MOUNT_NEW}" || fail "Cannot mount new root"

# The tarball is on the old (now formatted) root, but we staged it to RAM
# or it was read before formatting. Since the tarball lived on root which
# we just formatted, we need it from the raw SD backup area or RAM.
#
# Actually: the tarball was downloaded to /home/pifinder/ on the old root.
# Since we formatted root, we can't read it anymore. The pre-migration script
# should have copied the tarball to a safe location (raw SD area) or we need
# to keep the old root mounted during extraction.
#
# Revised approach: mount old root read-only, extract tarball, THEN format.
# But we already formatted... This is a design issue.
#
# Resolution: The tarball was staged before reboot. We need to either:
# a) Copy tarball to RAM in initramfs (but it's ~900MB, too big for RAM)
# b) Re-download in initramfs (no network stack in busybox initramfs)
# c) Store tarball on a separate partition or raw SD area
#
# Best approach: pre-migration script writes the tarball to a raw SD offset
# (after the backup). Initramfs reads it from the raw SD device.
#
# For now: read tarball from raw SD. The pre-migration script should have
# written it there. Offset = backup_offset + backup_size (aligned to 1MB).

# Calculate tarball offset (backup is at 14GB, tarball follows)
BACKUP_ALIGNED=$(( (BACKUP_SIZE + 1048575) / 1048576 * 1048576 ))
TARBALL_OFFSET=$((BACKUP_OFFSET + BACKUP_ALIGNED + 1048576))

# Read tarball size from metadata (stored 64 bytes before tarball offset)
TARBALL_SIZE=$(dd if="${SD_DEV}" bs=1 skip=$((TARBALL_OFFSET - 64)) count=20 2>/dev/null | tr -d '\0')
if [ -z "${TARBALL_SIZE}" ] || [ "${TARBALL_SIZE}" -le 0 ] 2>/dev/null; then
    fail "Tarball not found at raw offset"
fi

show_progress 40 "Extracting rootfs"

# Extract rootfs from tarball via raw SD read -> zstd decompress -> tar extract
dd if="${SD_DEV}" bs=1M skip=$((TARBALL_OFFSET / 1048576)) count=$((TARBALL_SIZE / 1048576 + 1)) 2>/dev/null | \
    zstd -d | tar xf - -C "${MOUNT_NEW}" --strip-components=1 rootfs/ || fail "rootfs extract failed"

show_progress 60 "Rootfs extracted"

# --- Extract boot ---
show_progress 65 "Extracting boot"
mkdir -p "${MOUNT_BOOT}"
mount -t vfat "${BOOT_DEV}" "${MOUNT_BOOT}" || fail "Cannot mount boot"

dd if="${SD_DEV}" bs=1M skip=$((TARBALL_OFFSET / 1048576)) count=$((TARBALL_SIZE / 1048576 + 1)) 2>/dev/null | \
    zstd -d | tar xf - -C "${MOUNT_BOOT}" --strip-components=1 boot/ || fail "boot extract failed"

show_progress 75 "Boot extracted"

# --- Restore PiFinder_data ---
show_progress 80 "Restoring user data"
mkdir -p "${MOUNT_NEW}/home/pifinder/PiFinder_data"
dd if="${SD_DEV}" bs=1M skip=$((BACKUP_OFFSET / 1048576)) count=$((BACKUP_SIZE / 1048576 + 1)) 2>/dev/null | \
    tar xzf - -C "${MOUNT_NEW}/home/pifinder/" || fail "User data restore failed"

show_progress 85 "User data restored"

# --- Migrate WiFi credentials ---
show_progress 88 "Migrating WiFi"

if [ -n "${WPA_CONF}" ]; then
    NM_DIR="${MOUNT_NEW}/etc/NetworkManager/system-connections"
    mkdir -p "${NM_DIR}"

    # Parse wpa_supplicant.conf networks and create NM connection files
    echo "${WPA_CONF}" | awk '
    /^[[:space:]]*network=\{/ { in_net=1; ssid=""; psk=""; key_mgmt="" }
    in_net && /ssid=/ { gsub(/.*ssid="/, ""); gsub(/".*/, ""); ssid=$0 }
    in_net && /psk=/ { gsub(/.*psk="/, ""); gsub(/".*/, ""); psk=$0 }
    in_net && /key_mgmt=/ { gsub(/.*key_mgmt=/, ""); key_mgmt=$0 }
    in_net && /^\}/ {
        in_net=0
        if (ssid != "") {
            fn = ssid
            gsub(/[^a-zA-Z0-9_-]/, "_", fn)
            file = "'"${NM_DIR}"'/" fn ".nmconnection"
            print "[connection]" > file
            print "id=" ssid > file
            print "type=wifi" >> file
            print "autoconnect=true" >> file
            print "" >> file
            print "[wifi]" >> file
            print "ssid=" ssid >> file
            print "mode=infrastructure" >> file
            print "" >> file
            if (psk != "") {
                print "[wifi-security]" >> file
                print "key-mgmt=wpa-psk" >> file
                print "psk=" psk >> file
                print "" >> file
            }
            print "[ipv4]" >> file
            print "method=auto" >> file
            print "" >> file
            print "[ipv6]" >> file
            print "method=auto" >> file
        }
    }
    '

    # Set permissions on NM connection files
    chmod 600 "${NM_DIR}"/*.nmconnection 2>/dev/null || true
fi

show_progress 92 "WiFi migrated"

# --- Set ownership ---
show_progress 95 "Setting permissions"
# pifinder user UID/GID is typically 1000:100 on NixOS
chown -R 1000:100 "${MOUNT_NEW}/home/pifinder" 2>/dev/null || true

# --- Cleanup ---
show_progress 98 "Syncing"
sync

umount "${MOUNT_BOOT}" 2>/dev/null || true
umount "${MOUNT_NEW}" 2>/dev/null || true

show_progress 100 "Migration complete!"
sleep 2

# Reboot into NixOS
echo "Rebooting into NixOS..."
reboot -f
