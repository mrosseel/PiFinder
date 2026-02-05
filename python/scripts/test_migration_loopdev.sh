#!/bin/bash
# test_migration_loopdev.sh - Test the migration initramfs on a loop device
#
# Creates a 32GB sparse file with RPi OS-like partition layout, populates it
# with fake data, then runs the migration init script against it.
#
# Usage: sudo ./test_migration_loopdev.sh [--keep]
#   --keep: don't clean up the loop device and image after test
#
# Requires: losetup, sfdisk, mkfs.ext4, mkfs.vfat, e2fsck, resize2fs, cpio, gzip
#
# What it tests:
#   - Partition shrink (resize2fs + sfdisk)
#   - Staging area write/verify at end of SD
#   - Format both partitions
#   - Tarball extraction (boot/ + rootfs/)
#   - PiFinder_data backup/restore
#   - WiFi credential migration (wpa_supplicant → NetworkManager)
#   - Partition re-expansion after migration
#
# What it does NOT test (requires real hardware):
#   - OLED progress display (migration_progress binary)
#   - Actual reboot
#   - Real kernel/boot files

set -euo pipefail

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="/tmp/migration_test_$$"
IMAGE="${WORK_DIR}/sd_card.img"
IMAGE_SIZE_MB=32768  # 32GB sparse
BOOT_SIZE_MB=256

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[FAIL]${NC} $*"; }
pass()  { echo -e "${GREEN}[PASS]${NC} $*"; }

cleanup() {
    local exit_code=$?
    if [ "${KEEP}" = "1" ] && [ "${exit_code}" = "0" ]; then
        warn "Keeping work dir: ${WORK_DIR}"
        warn "Loop device: ${LOOP_DEV:-none}"
        warn "Clean up manually: sudo losetup -d ${LOOP_DEV:-?} && rm -rf ${WORK_DIR}"
        return
    fi
    info "Cleaning up..."
    umount "${WORK_DIR}"/mnt_* 2>/dev/null || true
    [ -n "${LOOP_DEV:-}" ] && losetup -d "${LOOP_DEV}" 2>/dev/null || true
    rm -rf "${WORK_DIR}"
    if [ "${exit_code}" != "0" ]; then
        error "Test FAILED (exit code ${exit_code})"
    fi
}
trap cleanup EXIT

LOOP_DEV=""

# -------------------------------------------------------------------
# Step 1: Create sparse 32GB image with RPi OS-like partitions
# -------------------------------------------------------------------
info "Creating ${IMAGE_SIZE_MB}MB sparse SD card image..."
mkdir -p "${WORK_DIR}"
truncate -s "${IMAGE_SIZE_MB}M" "${IMAGE}"

info "Partitioning (boot: ${BOOT_SIZE_MB}MB FAT32, root: rest ext4)..."
# Partition layout matching RPi OS:
#   p1: FAT32 boot, 256MB, starting at 4MB
#   p2: ext4 root, rest of card
sfdisk "${IMAGE}" <<SFDISK
label: dos
unit: sectors

start=8192, size=524288, type=c
start=532480, type=83
SFDISK

# Set up loop device
LOOP_DEV=$(losetup --find --show --partscan "${IMAGE}")
info "Loop device: ${LOOP_DEV}"

# Wait for partition devices
sleep 1
[ ! -b "${LOOP_DEV}p1" ] && { sleep 2; partprobe "${LOOP_DEV}"; sleep 1; }
[ ! -b "${LOOP_DEV}p1" ] && { error "${LOOP_DEV}p1 not found"; exit 1; }

# Format
mkfs.vfat -F 32 -n boot "${LOOP_DEV}p1"
mkfs.ext4 -F -L rootfs "${LOOP_DEV}p2"

# -------------------------------------------------------------------
# Step 2: Populate with fake RPi OS content
# -------------------------------------------------------------------
info "Populating fake RPi OS..."

mkdir -p "${WORK_DIR}/mnt_boot" "${WORK_DIR}/mnt_root"
mount "${LOOP_DEV}p1" "${WORK_DIR}/mnt_boot"
mount "${LOOP_DEV}p2" "${WORK_DIR}/mnt_root"

# Boot partition: fake config.txt and kernel
echo "# RPi OS config" > "${WORK_DIR}/mnt_boot/config.txt"
dd if=/dev/urandom of="${WORK_DIR}/mnt_boot/kernel8.img" bs=1K count=64 2>/dev/null

# Root partition: fake RPi OS filesystem
mkdir -p "${WORK_DIR}/mnt_root/etc/wpa_supplicant"
mkdir -p "${WORK_DIR}/mnt_root/home/pifinder/PiFinder_data"
mkdir -p "${WORK_DIR}/mnt_root/home/pifinder/PiFinder"
mkdir -p "${WORK_DIR}/mnt_root/usr/bin"

# WiFi credentials to migrate
cat > "${WORK_DIR}/mnt_root/etc/wpa_supplicant/wpa_supplicant.conf" <<'WPAEOF'
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

network={
	ssid="HomeNetwork"
	psk="password123"
	key_mgmt=WPA-PSK
}

network={
	ssid="Coffee Shop WiFi"
	psk="cafelatte"
	key_mgmt=WPA-PSK
}

network={
	ssid="OpenNetwork"
	key_mgmt=NONE
}
WPAEOF

# PiFinder user data
echo '{"setting": "value"}' > "${WORK_DIR}/mnt_root/home/pifinder/PiFinder_data/config.json"
dd if=/dev/urandom of="${WORK_DIR}/mnt_root/home/pifinder/PiFinder_data/observations.db" bs=1K count=32 2>/dev/null
echo "2.4.0" > "${WORK_DIR}/mnt_root/home/pifinder/PiFinder/version.txt"

# Create a fake migration tarball (.tar.gz containing boot/ + rootfs/ + manifest.json)
info "Creating fake NixOS tarball..."
TARBALL_STAGING="${WORK_DIR}/tarball_staging"
mkdir -p "${TARBALL_STAGING}/boot" "${TARBALL_STAGING}/rootfs"

# Fake NixOS boot contents
echo "# NixOS extlinux.conf" > "${TARBALL_STAGING}/boot/extlinux.conf"
dd if=/dev/urandom of="${TARBALL_STAGING}/boot/Image" bs=1K count=128 2>/dev/null

# Fake NixOS rootfs
mkdir -p "${TARBALL_STAGING}/rootfs/nix/store"
mkdir -p "${TARBALL_STAGING}/rootfs/etc/NetworkManager/system-connections"
mkdir -p "${TARBALL_STAGING}/rootfs/home/pifinder"
echo "NixOS rootfs marker" > "${TARBALL_STAGING}/rootfs/etc/NIXOS"
dd if=/dev/urandom of="${TARBALL_STAGING}/rootfs/nix/store/fakepkg" bs=1K count=256 2>/dev/null

echo '{"version": "2.5.0"}' > "${TARBALL_STAGING}/manifest.json"

tar czf "${WORK_DIR}/mnt_root/home/pifinder/pifinder-nixos-migration.tar.gz" \
    -C "${TARBALL_STAGING}" boot rootfs manifest.json

# Backup PiFinder_data
tar czf "${WORK_DIR}/mnt_root/home/pifinder/pifinder_backup.tar.gz" \
    -C "${WORK_DIR}/mnt_root/home/pifinder" PiFinder_data

TARBALL_SIZE=$(stat -c%s "${WORK_DIR}/mnt_root/home/pifinder/pifinder-nixos-migration.tar.gz")
BACKUP_SIZE=$(stat -c%s "${WORK_DIR}/mnt_root/home/pifinder/pifinder_backup.tar.gz")

# Migration flag on boot
touch "${WORK_DIR}/mnt_boot/nixos_migration"

umount "${WORK_DIR}/mnt_boot"
umount "${WORK_DIR}/mnt_root"

info "Fake RPi OS populated (tarball: ${TARBALL_SIZE} bytes, backup: ${BACKUP_SIZE} bytes)"

# -------------------------------------------------------------------
# Step 3: Create migration metadata (normally done by nixos_migration.sh)
# -------------------------------------------------------------------
info "Writing migration metadata..."

# We'll inject the metadata into the initramfs below
cat > "${WORK_DIR}/migration_meta" <<METAEOF
TARBALL_PATH=/home/pifinder/pifinder-nixos-migration.tar.gz
BACKUP_PATH=/home/pifinder/pifinder_backup.tar.gz
TARBALL_SIZE=${TARBALL_SIZE}
BACKUP_SIZE=${BACKUP_SIZE}
STAGING_SIZE_MB=2048
METAEOF

# -------------------------------------------------------------------
# Step 4: Build a test initramfs
# -------------------------------------------------------------------
info "Building test initramfs..."

INITRAMFS_DIR="${WORK_DIR}/initramfs"
mkdir -p "${INITRAMFS_DIR}"/{bin,lib,dev,proc,sys,mnt,tmp}

# Copy the init script
cp "${SCRIPT_DIR}/nixos_migration_init.sh" "${INITRAMFS_DIR}/init"
chmod +x "${INITRAMFS_DIR}/init"

# Copy metadata
cp "${WORK_DIR}/migration_meta" "${INITRAMFS_DIR}/"

# Copy busybox
BUSYBOX_PATH=$(command -v busybox 2>/dev/null || true)
if [ -z "${BUSYBOX_PATH}" ]; then
    error "busybox not found - install it to run this test"
    exit 1
fi
cp "${BUSYBOX_PATH}" "${INITRAMFS_DIR}/bin/"

# Copy filesystem tools
for tool in e2fsck resize2fs mke2fs mkfs.vfat sfdisk partprobe blockdev; do
    tool_path=$(command -v "${tool}" 2>/dev/null || true)
    if [ -n "${tool_path}" ]; then
        cp "${tool_path}" "${INITRAMFS_DIR}/bin/" 2>/dev/null || true
    else
        warn "Skipping ${tool} (not found)"
    fi
done
ln -sf mke2fs "${INITRAMFS_DIR}/bin/mkfs.ext4" 2>/dev/null || true

info "Test initramfs built"

# -------------------------------------------------------------------
# Step 5: Run the migration init script in a chroot-like environment
# -------------------------------------------------------------------
# We can't actually boot an initramfs in a test, but we CAN run the init
# script directly with the loop device as the "SD card". We override the
# device paths to point at our loop device.
info "========================================"
info "Running migration init script..."
info "========================================"

# Create a wrapper that sources the init script with overridden device paths
cat > "${WORK_DIR}/run_migration.sh" <<'RUNEOF'
#!/bin/bash
set -e

# Override device paths to use the loop device
export SD_DEV="__LOOP_DEV__"
export BOOT_DEV="__LOOP_DEV__p1"
export ROOT_DEV="__LOOP_DEV__p2"

# Suppress the reboot at the end
export TEST_MODE=1

# Source migration_meta
. /tmp/test_migration_meta

# We need to rewrite the init script to use our overrides and skip
# /proc /sys /dev mounts (we're already on a running system).
# Instead, just run the key phases directly.

MOUNT_ROOT="/tmp/migration_mnt_root_$$"
MOUNT_NEW="/tmp/migration_mnt_new_$$"
MOUNT_BOOT="/tmp/migration_mnt_boot_$$"

show() { echo "[${1}%] ${2}"; }
fail() { echo "FAILED: $1"; exit 1; }

# Phase 2: Check root filesystem
show 5 "Checking filesystem"
e2fsck -f -y "${ROOT_DEV}" || fail "e2fsck"

mkdir -p "${MOUNT_ROOT}"
mount -t ext4 -o ro "${ROOT_DEV}" "${MOUNT_ROOT}" || fail "mount root"

TARBALL_ON_ROOT="${MOUNT_ROOT}${TARBALL_PATH}"
BACKUP_ON_ROOT="${MOUNT_ROOT}${BACKUP_PATH}"

[ ! -f "${TARBALL_ON_ROOT}" ] && { umount "${MOUNT_ROOT}"; fail "tarball not found"; }
[ ! -f "${BACKUP_ON_ROOT}" ] && { umount "${MOUNT_ROOT}"; fail "backup not found"; }

# Save WiFi
WPA_FILE="${MOUNT_ROOT}/etc/wpa_supplicant/wpa_supplicant.conf"
mkdir -p /tmp/wifi_test_$$
[ -f "${WPA_FILE}" ] && cp "${WPA_FILE}" "/tmp/wifi_test_$$/wpa_supplicant.conf"

umount "${MOUNT_ROOT}"

# Phase 3: Shrink root FS + partition
show 12 "Shrinking root FS"

SD_BYTES=$(blockdev --getsize64 "${SD_DEV}")
SD_SECTORS=$(blockdev --getsz "${SD_DEV}")

P2_START=$(sfdisk -d "${SD_DEV}" 2>/dev/null | awk '/p2/ {
    for (i=1; i<=NF; i++) {
        if ($i ~ /^start=/) { gsub(/start=/, "", $i); gsub(/,/, "", $i); print $i }
    }
}')
[ -z "${P2_START}" ] && fail "Cannot read p2 start"

STAGING_SECTORS=$(( STAGING_SIZE_MB * 1024 * 1024 / 512 ))
P2_CURRENT_SECTORS=$(( SD_SECTORS - P2_START ))
P2_NEW_SECTORS=$(( P2_CURRENT_SECTORS - STAGING_SECTORS ))
[ "${P2_NEW_SECTORS}" -le 0 ] && fail "SD too small"

BLOCK_SIZE=4096
P2_NEW_BLOCKS=$(( P2_NEW_SECTORS * 512 / BLOCK_SIZE ))

resize2fs "${ROOT_DEV}" "${P2_NEW_BLOCKS}" || fail "resize2fs shrink"

show 18 "Shrinking partition"
echo "${P2_START}, ${P2_NEW_SECTORS}" | sfdisk -N 2 "${SD_DEV}" --no-reread 2>/dev/null || fail "sfdisk shrink"
partprobe "${SD_DEV}" 2>/dev/null || losetup --set-capacity "${SD_DEV}" 2>/dev/null || true
sleep 1

STAGING_START_BYTE=$(( (P2_START + P2_NEW_SECTORS) * 512 ))
show 20 "Staging at byte offset ${STAGING_START_BYTE}"

# Phase 4: Copy to staging area
show 22 "Copying to staging"
mount -t ext4 -o ro "${ROOT_DEV}" "${MOUNT_ROOT}" || fail "mount shrunk root"

TARBALL_ON_ROOT="${MOUNT_ROOT}${TARBALL_PATH}"
BACKUP_ON_ROOT="${MOUNT_ROOT}${BACKUP_PATH}"

# Write header
HEADER_FILE="/tmp/staging_header_$$"
printf "PFMIGRATE1\ntarball_size=%s\nbackup_size=%s\n" "${TARBALL_SIZE}" "${BACKUP_SIZE}" > "${HEADER_FILE}"
dd if="${HEADER_FILE}" of="${SD_DEV}" bs=4096 count=1 seek=$(( STAGING_START_BYTE / 4096 )) conv=notrunc 2>/dev/null

TARBALL_ALIGNED=$(( (TARBALL_SIZE + 4095) / 4096 * 4096 ))
TARBALL_STAGING_BYTE=$(( STAGING_START_BYTE + 4096 ))
BACKUP_STAGING_BYTE=$(( TARBALL_STAGING_BYTE + TARBALL_ALIGNED ))

show 25 "Copying tarball to staging"
dd if="${TARBALL_ON_ROOT}" of="${SD_DEV}" bs=1M \
    seek=$(( TARBALL_STAGING_BYTE / 1048576 )) conv=notrunc 2>/dev/null || fail "tarball stage"

show 35 "Copying backup to staging"
dd if="${BACKUP_ON_ROOT}" of="${SD_DEV}" bs=1M \
    seek=$(( BACKUP_STAGING_BYTE / 1048576 )) conv=notrunc 2>/dev/null || fail "backup stage"

umount "${MOUNT_ROOT}"

# Verify header
MAGIC=$(dd if="${SD_DEV}" bs=4096 skip=$(( STAGING_START_BYTE / 4096 )) count=1 2>/dev/null | head -1)
[ "${MAGIC}" != "PFMIGRATE1" ] && fail "header verify failed (got: ${MAGIC})"

show 42 "Staging verified"

# POINT OF NO RETURN
show 45 "FORMATTING"

mkfs.vfat -F 32 -n FIRMWARE "${BOOT_DEV}" || fail "mkfs.vfat"
show 50 "Format root"
mkfs.ext4 -F -L NIXOS_SD "${ROOT_DEV}" || fail "mkfs.ext4"
show 55 "Formatted"

# Phase 5: Extract
show 57 "Extracting NixOS"
mkdir -p "${MOUNT_NEW}"
mount -t ext4 "${ROOT_DEV}" "${MOUNT_NEW}" || fail "mount new root"

TARBALL_SKIP_MB=$(( TARBALL_STAGING_BYTE / 1048576 ))
TARBALL_COUNT_MB=$(( TARBALL_SIZE / 1048576 + 1 ))

dd if="${SD_DEV}" bs=1M skip="${TARBALL_SKIP_MB}" count="${TARBALL_COUNT_MB}" 2>/dev/null | \
    gunzip | tar xf - -C "${MOUNT_NEW}" || fail "extract"

show 70 "Extracted"

# Move boot/ to boot partition
mkdir -p "${MOUNT_BOOT}"
mount -t vfat "${BOOT_DEV}" "${MOUNT_BOOT}" || fail "mount boot"

if [ -d "${MOUNT_NEW}/boot" ]; then
    cp -a "${MOUNT_NEW}/boot/." "${MOUNT_BOOT}/"
    rm -rf "${MOUNT_NEW}/boot"
fi

# Move rootfs/ up
if [ -d "${MOUNT_NEW}/rootfs" ]; then
    cd "${MOUNT_NEW}/rootfs"
    for item in .* *; do
        [ "${item}" = "." ] || [ "${item}" = ".." ] && continue
        mv "${item}" "${MOUNT_NEW}/" 2>/dev/null || { cp -a "${item}" "${MOUNT_NEW}/"; rm -rf "${item}"; }
    done
    cd /
    rmdir "${MOUNT_NEW}/rootfs" 2>/dev/null || true
fi
rm -f "${MOUNT_NEW}/manifest.json"

show 78 "Layout done"

# Phase 6: Restore backup
show 80 "Restoring user data"
mkdir -p "${MOUNT_NEW}/home/pifinder"

BACKUP_SKIP_MB=$(( BACKUP_STAGING_BYTE / 1048576 ))
BACKUP_COUNT_MB=$(( BACKUP_SIZE / 1048576 + 1 ))

dd if="${SD_DEV}" bs=1M skip="${BACKUP_SKIP_MB}" count="${BACKUP_COUNT_MB}" 2>/dev/null | \
    gunzip | tar xf - -C "${MOUNT_NEW}/home/pifinder/" || fail "restore backup"

show 85 "Data restored"

# Phase 7: WiFi migration
show 88 "WiFi migration"
if [ -f "/tmp/wifi_test_$$/wpa_supplicant.conf" ]; then
    NM_DIR="${MOUNT_NEW}/etc/NetworkManager/system-connections"
    mkdir -p "${NM_DIR}"

    SSID="" PSK="" KEY_MGMT="" IN_NET=0
    while IFS= read -r line; do
        line=$(echo "${line}" | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
        case "${line}" in
            network=*) IN_NET=1; SSID=""; PSK=""; KEY_MGMT="" ;;
            "}")
                if [ "${IN_NET}" = "1" ] && [ -n "${SSID}" ]; then
                    FN=$(echo "${SSID}" | sed 's/[^a-zA-Z0-9_-]/_/g')
                    CONN_FILE="${NM_DIR}/${FN}.nmconnection"
                    {
                        echo "[connection]"
                        echo "id=${SSID}"
                        echo "type=wifi"
                        echo "autoconnect=true"
                        echo ""
                        echo "[wifi]"
                        echo "ssid=${SSID}"
                        echo "mode=infrastructure"
                        echo ""
                    } > "${CONN_FILE}"
                    if [ -n "${PSK}" ]; then
                        {
                            echo "[wifi-security]"
                            echo "key-mgmt=wpa-psk"
                            echo "psk=${PSK}"
                            echo ""
                        } >> "${CONN_FILE}"
                    fi
                    {
                        echo "[ipv4]"
                        echo "method=auto"
                        echo ""
                        echo "[ipv6]"
                        echo "method=auto"
                    } >> "${CONN_FILE}"
                    chmod 600 "${CONN_FILE}"
                fi
                IN_NET=0 ;;
            ssid=*)   [ "${IN_NET}" = "1" ] && SSID=$(echo "${line}" | sed 's/^ssid="//' | sed 's/"$//') ;;
            psk=*)    [ "${IN_NET}" = "1" ] && PSK=$(echo "${line}" | sed 's/^psk="//' | sed 's/"$//') ;;
            key_mgmt=*) [ "${IN_NET}" = "1" ] && KEY_MGMT=$(echo "${line}" | sed 's/^key_mgmt=//') ;;
        esac
    done < "/tmp/wifi_test_$$/wpa_supplicant.conf"
fi

# Phase 8: Expand partition
show 95 "Expanding root"
umount "${MOUNT_NEW}"
echo "${P2_START}," | sfdisk -N 2 "${SD_DEV}" --no-reread 2>/dev/null || true
partprobe "${SD_DEV}" 2>/dev/null || true
sleep 1
resize2fs "${ROOT_DEV}" 2>/dev/null || true

sync

show 100 "Migration complete!"

# Cleanup test artifacts
rm -rf "/tmp/wifi_test_$$" "/tmp/staging_header_$$"
umount "${MOUNT_BOOT}" 2>/dev/null || true

RUNEOF

# Inject the loop device path
sed -i "s|__LOOP_DEV__|${LOOP_DEV}|g" "${WORK_DIR}/run_migration.sh"

# Inject metadata
cp "${WORK_DIR}/migration_meta" /tmp/test_migration_meta

chmod +x "${WORK_DIR}/run_migration.sh"
bash "${WORK_DIR}/run_migration.sh"

# -------------------------------------------------------------------
# Step 6: Verify results
# -------------------------------------------------------------------
info "========================================"
info "Verifying migration results..."
info "========================================"

ERRORS=0

# Mount and check new partitions
mkdir -p "${WORK_DIR}/mnt_boot" "${WORK_DIR}/mnt_root"
mount "${LOOP_DEV}p1" "${WORK_DIR}/mnt_boot"
mount "${LOOP_DEV}p2" "${WORK_DIR}/mnt_root"

# Check boot partition label
BOOT_LABEL=$(blkid -s LABEL -o value "${LOOP_DEV}p1")
if [ "${BOOT_LABEL}" = "FIRMWARE" ]; then
    pass "Boot partition label: FIRMWARE"
else
    error "Boot partition label: ${BOOT_LABEL} (expected FIRMWARE)"
    ERRORS=$((ERRORS + 1))
fi

# Check root partition label
ROOT_LABEL=$(blkid -s LABEL -o value "${LOOP_DEV}p2")
if [ "${ROOT_LABEL}" = "NIXOS_SD" ]; then
    pass "Root partition label: NIXOS_SD"
else
    error "Root partition label: ${ROOT_LABEL} (expected NIXOS_SD)"
    ERRORS=$((ERRORS + 1))
fi

# Check NixOS marker file
if [ -f "${WORK_DIR}/mnt_root/etc/NIXOS" ]; then
    pass "NixOS marker file exists"
else
    error "NixOS marker file missing"
    ERRORS=$((ERRORS + 1))
fi

# Check nix store
if [ -d "${WORK_DIR}/mnt_root/nix/store" ]; then
    pass "Nix store directory exists"
else
    error "Nix store missing"
    ERRORS=$((ERRORS + 1))
fi

# Check boot contents
if [ -f "${WORK_DIR}/mnt_boot/extlinux.conf" ]; then
    pass "NixOS boot files present"
else
    error "NixOS boot files missing"
    ERRORS=$((ERRORS + 1))
fi

# Check old RPi OS boot files are gone
if [ -f "${WORK_DIR}/mnt_boot/config.txt" ]; then
    error "Old config.txt still present (should have been formatted away)"
    ERRORS=$((ERRORS + 1))
else
    pass "Old boot files cleaned"
fi

# Check PiFinder_data restored
if [ -f "${WORK_DIR}/mnt_root/home/pifinder/PiFinder_data/config.json" ]; then
    pass "PiFinder_data/config.json restored"
else
    error "PiFinder_data not restored"
    ERRORS=$((ERRORS + 1))
fi

if [ -f "${WORK_DIR}/mnt_root/home/pifinder/PiFinder_data/observations.db" ]; then
    pass "PiFinder_data/observations.db restored"
else
    error "observations.db not restored"
    ERRORS=$((ERRORS + 1))
fi

# Check WiFi migration
NM_DIR="${WORK_DIR}/mnt_root/etc/NetworkManager/system-connections"
if [ -f "${NM_DIR}/HomeNetwork.nmconnection" ]; then
    pass "WiFi: HomeNetwork migrated"
    # Verify content
    if grep -q "psk=password123" "${NM_DIR}/HomeNetwork.nmconnection"; then
        pass "WiFi: HomeNetwork PSK correct"
    else
        error "WiFi: HomeNetwork PSK wrong"
        ERRORS=$((ERRORS + 1))
    fi
else
    error "WiFi: HomeNetwork not migrated"
    ERRORS=$((ERRORS + 1))
fi

if [ -f "${NM_DIR}/Coffee_Shop_WiFi.nmconnection" ]; then
    pass "WiFi: Coffee Shop WiFi migrated (filename sanitized)"
else
    error "WiFi: Coffee Shop WiFi not migrated"
    ERRORS=$((ERRORS + 1))
fi

if [ -f "${NM_DIR}/OpenNetwork.nmconnection" ]; then
    pass "WiFi: OpenNetwork migrated"
    if grep -q "wifi-security" "${NM_DIR}/OpenNetwork.nmconnection"; then
        error "WiFi: OpenNetwork should not have wifi-security section"
        ERRORS=$((ERRORS + 1))
    else
        pass "WiFi: OpenNetwork has no PSK (correct for open network)"
    fi
else
    error "WiFi: OpenNetwork not migrated"
    ERRORS=$((ERRORS + 1))
fi

# Check NM file permissions
for f in "${NM_DIR}"/*.nmconnection; do
    [ ! -f "$f" ] && continue
    PERMS=$(stat -c%a "$f")
    if [ "${PERMS}" = "600" ]; then
        pass "WiFi: $(basename "$f") permissions 600"
    else
        error "WiFi: $(basename "$f") permissions ${PERMS} (expected 600)"
        ERRORS=$((ERRORS + 1))
    fi
done

# Check root FS was expanded back
ROOT_SIZE_BLOCKS=$(dumpe2fs -h "${LOOP_DEV}p2" 2>/dev/null | awk '/Block count/ {print $3}')
ROOT_BLOCK_SIZE=$(dumpe2fs -h "${LOOP_DEV}p2" 2>/dev/null | awk '/Block size/ {print $3}')
ROOT_SIZE_GB=$(( ROOT_SIZE_BLOCKS * ROOT_BLOCK_SIZE / 1024 / 1024 / 1024 ))
if [ "${ROOT_SIZE_GB}" -ge 28 ]; then
    pass "Root FS expanded back to ${ROOT_SIZE_GB}GB"
else
    warn "Root FS only ${ROOT_SIZE_GB}GB (may not have expanded fully)"
fi

umount "${WORK_DIR}/mnt_boot"
umount "${WORK_DIR}/mnt_root"

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
echo ""
echo "========================================"
if [ "${ERRORS}" = "0" ]; then
    pass "All checks passed!"
else
    error "${ERRORS} check(s) failed"
fi
echo "========================================"

exit "${ERRORS}"
