#!/usr/bin/env bash
# End-to-end simulation of the Pi OS 2.6.x -> NixOS migration (ADR 0039).
#
#   nixos/tests/migration-e2e/run.sh <pi-os.img> <pi-os-checkout> [stage]
#
# <pi-os.img>       a released Pi OS image, for example PiFinder_2.6.3.img
# <pi-os-checkout>  a checkout of the Pi OS line with the migration scripts to
#                   test (python/scripts/nixos_migration*.sh)
# [stage]           start at this stage (card, prepare, init, firstboot, full)
#
# Stages:
#   card       reflink copy of the image, grown to a 16 GB card
#   prepare    put the migration scripts and the tarball of this flake on the
#              card; run nixos_migration.sh in an aarch64 chroot (network on)
#   init       QEMU raspi4b boots the Pi OS kernel with the migration
#              initramfs; the card is converted to btrfs
#   firstboot  QEMU boots U-Boot from the new FAT partition; U-Boot starts the
#              migration system; first boot switches to the full system
#   full       QEMU boots the full system to multi-user.target
#
# QEMU raspi4b has no network, so the full system's closure is copied into
# the card's store before first boot, and first boot falls back to the baked
# first-boot target. QEMU puts the SD card on the controller that the Pi 4
# device tree calls mmc1, so the device trees get mmc0 and mmc1 swapped;
# then the card is mmcblk0, as on a real Pi 4. Nothing else of the code under
# test changes.
#
# Needs: sudo, nix with aarch64 emulation (binfmt), about 40 GB free.
# Work files: $MIGRATION_E2E_DIR (default /var/tmp/pifinder-migration-e2e).
#
# Host safety: the script re-runs itself in a private mount namespace, so no
# mount it makes is visible on the host, and the prepare chroot is a bwrap
# sandbox with its own /dev and /proc. Never bind host /dev, /proc or /sys.
set -euo pipefail

if [ -z "${MIGRATION_E2E_NS:-}" ]; then
  exec sudo unshare --mount --propagation private -- \
    sudo -u "$(id -un)" MIGRATION_E2E_NS=1 MIGRATION_E2E_DIR="${MIGRATION_E2E_DIR:-}" \
    PATH="$PATH" NIX_PATH="${NIX_PATH:-}" bash "$0" "$@"
fi

PIOS_IMG=$(readlink -f "$1")
PIOS_SRC=$(readlink -f "$2")
START=${3:-card}
HERE=$(cd "$(dirname "$0")" && pwd)
FLAKE=$(cd "$HERE/../../.." && pwd)
DIR=${MIGRATION_E2E_DIR:-/var/tmp/pifinder-migration-e2e}
CARD=$DIR/card.img
MNT=$DIR/mnt
mkdir -p "$DIR" "$MNT"

SUBS="https://cache.pifinder.eu/pifinder https://cache.nixos.org"
KEYS="pifinder:8UU/O3oLkaJHHUyqEcPGl+9F1m4MqDca39Ewl49jBmE= cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
nixb() { nix build --no-link --print-out-paths --option extra-substituters "$SUBS" --option extra-trusted-public-keys "$KEYS" "$@"; }
tool() { echo "$(nix build --no-link --print-out-paths "nixpkgs#$1")/bin/$2"; }

QEMU=$(tool qemu qemu-system-aarch64)
FDTPUT=$(tool dtc fdtput)
FDTGET=$(tool dtc fdtget)

log() { echo "[e2e $(date +%T)] $*"; }
die() { log "FAIL: $*"; exit 1; }

stage_index() {
  case $1 in card) echo 1 ;; prepare) echo 2 ;; init) echo 3 ;; firstboot) echo 4 ;; full) echo 5 ;; *) die "unknown stage $1" ;; esac
}
run_stage() { [ "$(stage_index "$1")" -ge "$(stage_index "$START")" ]; }

# Partition offsets of the card, in bytes.
part_offset() { sfdisk -J "$CARD" | jq -r ".partitiontable.partitions[$(($1 - 1))].start * 512"; }
part_size() { sfdisk -J "$CARD" | jq -r ".partitiontable.partitions[$(($1 - 1))].size * 512"; }

LOOPS=()
mount_part() { # <part> <dir> [fstype]
  local loop
  loop=$(sudo losetup -f --show -o "$(part_offset "$1")" --sizelimit "$(part_size "$1")" "$CARD")
  LOOPS+=("$loop")
  sudo mkdir -p "$2"
  sudo mount ${3:+-t "$3"} "$loop" "$2"
}
cleanup() {
  # Only the card partitions are mounted, and only in this namespace.
  for m in "$MNT"/chroot/boot "$MNT"/chroot "$MNT"/boot "$MNT"/root; do
    mountpoint -q "$m" 2>/dev/null && sudo umount "$m"
  done
  for l in "${LOOPS[@]:-}"; do [ -n "$l" ] && sudo losetup -d "$l" 2>/dev/null; done
  LOOPS=()
}
trap cleanup EXIT

# Swap the mmc0 and mmc1 aliases of a Pi 4 device tree (see header).
patch_dtb() { # <in> <out>
  local mmc0 mmc1
  cp "$1" "$2"; chmod u+w "$2"
  mmc0=$("$FDTGET" -t s "$2" /aliases mmc0)
  mmc1=$("$FDTGET" -t s "$2" /aliases mmc1)
  "$FDTPUT" -t s "$2" /aliases mmc0 "$mmc1"
  "$FDTPUT" -t s "$2" /aliases mmc1 "$mmc0"
}

# Run QEMU raspi4b until the guest reboots or powers off (-no-reboot), until
# the serial log shows <stop-text> (empty: no text), or until the timeout.
# The serial console goes to <log>.
qemu_run() { # <log> <timeout-s> <stop-text> <qemu args...>
  local logf=$1 limit=$2 stop=$3 pid waited=0; shift 3
  log "QEMU: $* (log $logf, limit ${limit}s)"
  : > "$logf"
  "$QEMU" -machine raspi4b -m 2G -no-reboot -nographic \
    -serial "file:$logf" -monitor none -display none \
    -drive "format=raw,file=$CARD,if=sd" "$@" >/dev/null 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$limit" ]; do
    if [ -n "$stop" ] && grep -q "$stop" "$logf"; then
      sleep 20  # let the journal reach the card
      break
    fi
    sleep 10; waited=$((waited + 10))
  done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

# --------------------------------------------------------------------------
if run_stage card; then
  log "card: copy $PIOS_IMG and grow it to 16 GB"
  cp --reflink=auto "$PIOS_IMG" "$CARD"
  truncate -s 16G "$CARD"
fi

# --------------------------------------------------------------------------
if run_stage prepare; then
  log "prepare: build the migration tarball and the full system"
  TARBALL=$(nixb "$FLAKE#packages.aarch64-linux.migration-tarball")/pifinder-migration.tar.zst
  SHA=$(sha256sum "$TARBALL" | cut -d' ' -f1)

  mount_part 2 "$MNT/chroot" ext4
  mount_part 1 "$MNT/chroot/boot" vfat
  R=$MNT/chroot
  KVER=""
  for k in "$R"/lib/modules/*-v8*; do KVER=$(basename "$k"); break; done
  log "prepare: Pi OS kernel $KVER"

  # The migration scripts under test.
  sudo cp "$PIOS_SRC"/python/scripts/nixos_migration.sh "$PIOS_SRC"/python/scripts/nixos_migration_init.sh "$R/home/pifinder/PiFinder/python/scripts/"
  sudo cp "$PIOS_SRC"/python/PiFinder/nixos_migration_wifi.py "$R/home/pifinder/PiFinder/python/PiFinder/"
  sudo cp "$TARBALL" "$R/home/pifinder/pifinder-nixos-migration.tar.zst"

  # Simulation stand-ins, removed again below: the pre-flight check needs a
  # real Pi (model, SD device, WiFi mode); uname must name the Pi OS kernel;
  # reboot must not reach the host.
  sudo cp "$R/home/pifinder/PiFinder/python/scripts/nixos_migration_calc.py" "$DIR/calc.py.orig" 2>/dev/null || true
  printf 'import json\nprint(json.dumps({"all_ok": True}))\n' | sudo tee "$R/home/pifinder/PiFinder/python/scripts/nixos_migration_calc.py" >/dev/null
  printf '#!/bin/sh\n[ "$1" = -r ] && echo %s && exit 0\nexec /bin/uname "$@"\n' "$KVER" | sudo tee "$R/usr/local/bin/uname" >/dev/null
  printf '#!/bin/sh\necho "reboot (simulated)"\n' | sudo tee "$R/usr/local/sbin/reboot" >/dev/null
  sudo chmod +x "$R/usr/local/bin/uname" "$R/usr/local/sbin/reboot"

  # bwrap sandbox: own /dev, /proc and /tmp; the aarch64 emulator is
  # registered without the F flag, so the sandbox needs the host's
  # /run/binfmt link and the store it points into, both read-only.
  BWRAP=$(tool bubblewrap bwrap)
  sudo mkdir -p "$R/run/binfmt" "$R/nix/store"
  log "prepare: run nixos_migration.sh in a bwrap sandbox"
  sudo "$BWRAP" --bind "$R" / --bind "$R/boot" /boot \
    --dev /dev --proc /proc --perms 1777 --tmpfs /tmp \
    --ro-bind /nix/store /nix/store --ro-bind /run/binfmt /run/binfmt \
    --ro-bind /etc/resolv.conf /etc/resolv.conf \
    --unshare-pid --die-with-parent \
    /usr/bin/env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
    bash /home/pifinder/PiFinder/python/scripts/nixos_migration.sh \
    "file:///e2e-no-download" "$SHA" /tmp/nixos_migration_progress DisplaySSD1351 128x128 \
    | tee "$DIR/prepare.log" || true
  grep -q '"Rebooting in 5s..."\|Migration staged' "$DIR/prepare.log" || die "nixos_migration.sh did not stage the migration"

  sudo rmdir "$R/run/binfmt" "$R/nix/store" "$R/nix"
  sudo rm -f "$R/usr/local/bin/uname" "$R/usr/local/sbin/reboot"
  [ -f "$DIR/calc.py.orig" ] && sudo cp "$DIR/calc.py.orig" "$R/home/pifinder/PiFinder/python/scripts/nixos_migration_calc.py"
  [ -f "$R/boot/initramfs-migration.gz" ] || die "no initramfs-migration.gz on the boot partition"
  grep -q '^initramfs initramfs-migration.gz' "$R/boot/config.txt" || die "config.txt has no initramfs line"
  cp "$R/boot/kernel8.img" "$R/boot/bcm2711-rpi-4-b.dtb" "$R/boot/initramfs-migration.gz" "$DIR/"
  cleanup
  patch_dtb "$DIR/bcm2711-rpi-4-b.dtb" "$DIR/pios.dtb"
fi

# --------------------------------------------------------------------------
if run_stage init; then
  log "init: boot the migration initramfs"
  qemu_run "$DIR/init.log" 10800 "" -kernel "$DIR/kernel8.img" -dtb "$DIR/pios.dtb" \
    -initrd "$DIR/initramfs-migration.gz" -append "console=ttyAMA0,115200"
  grep -q "Rebooting into NixOS" "$DIR/init.log" || { tail -30 "$DIR/init.log"; die "migration init did not finish"; }

  log "init: check the card"
  [ "$(sudo blkid -o value -s TYPE -p -O "$(part_offset 2)" "$CARD")" = btrfs ] || die "partition 2 is not btrfs"
  [ "$(sudo blkid -o value -s LABEL -p -O "$(part_offset 2)" "$CARD")" = PIFINDER_SD ] || die "partition 2 label is not PIFINDER_SD"
  [ "$(sudo blkid -o value -s LABEL -p -O "$(part_offset 1)" "$CARD")" = FIRMWARE ] || die "partition 1 label is not FIRMWARE"
  mount_part 2 "$MNT/root" btrfs
  sudo btrfs subvolume list "$MNT/root" | tee "$DIR/subvolumes.txt"
  grep -q 'path home/pifinder/PiFinder_data$' "$DIR/subvolumes.txt" || die "no PiFinder_data subvolume"
  IMAGES=$(sudo find "$MNT/root/home/pifinder/PiFinder_data/catalog_images" -type f | wc -l)
  log "init: $IMAGES catalog images kept"
  [ "$IMAGES" -gt 1000 ] || die "catalog images were not kept"
  [ -f "$MNT/root/home/pifinder/PiFinder_data/observations.db" ] || die "observations.db was not kept"
  [ "$(sudo cat "$MNT/root/var/lib/pifinder/camera-type" 2>/dev/null)" = imx462 ] || die "camera-type is not imx462"
  [ -f "$MNT/root/boot/extlinux/extlinux.conf" ] || die "no extlinux.conf on the btrfs root"
  [ ! -e "$MNT/root/usr/bin/apt" ] || die "Pi OS files are still there"

  # No network in QEMU raspi4b: put the full system in the card's store.
  FULL=$(sudo cat "$MNT/root/var/lib/pifinder/first-boot-target")
  log "init: copy the full system $FULL into the card's store"
  nixb "$FULL" >/dev/null
  sudo nix copy --no-check-sigs --to "local?root=$MNT/root" "$FULL"
  for d in $(sudo find "$MNT/root/boot/nixos" -name bcm2711-rpi-4-b.dtb); do
    patch_dtb "$d" "$DIR/tmp.dtb" && sudo cp "$DIR/tmp.dtb" "$d"
  done
  cleanup

  mount_part 1 "$MNT/boot" vfat
  sudo cp "$MNT/boot/u-boot-rpi4.bin" "$DIR/u-boot.bin"
  sudo cp "$MNT/boot/bcm2711-rpi-4-b.dtb" "$DIR/fw.dtb"
  cleanup
  patch_dtb "$DIR/fw.dtb" "$DIR/uboot.dtb"
fi

# --------------------------------------------------------------------------
if run_stage firstboot; then
  log "firstboot: U-Boot -> migration system -> switch to the full system"
  qemu_run "$DIR/firstboot.log" 5400 "" -kernel "$DIR/u-boot.bin" -dtb "$DIR/uboot.dtb"
  mount_part 2 "$MNT/root" btrfs
  sudo journalctl -D "$MNT/root/var/log/journal" -u pifinder-first-boot --no-pager -o cat 2>&1 | tee "$DIR/firstboot-journal.txt" >/dev/null || true
  tail -20 "$DIR/firstboot-journal.txt"
  grep -q "Rebooting into full PiFinder system" "$DIR/firstboot-journal.txt" || die "first boot did not switch to the full system"
  sudo readlink "$MNT/root/nix/var/nix/profiles/system" | tee "$DIR/profile.txt"
  cleanup
fi

# --------------------------------------------------------------------------
if run_stage full; then
  log "full: boot the full system"
  # NixOS has no serial console (the Pi's UART belongs to the GPS), so run
  # for a fixed time and read the journal on the card afterwards.
  qemu_run "$DIR/full.log" 1500 "" -kernel "$DIR/u-boot.bin" -dtb "$DIR/uboot.dtb"
  mount_part 2 "$MNT/root" btrfs
  # The last boot on the card must be the full system: only it has
  # pifinder.service.
  sudo journalctl -D "$MNT/root/var/log/journal" -b 0 --no-pager -o short-monotonic 2>&1 | tee "$DIR/full-journal.txt" >/dev/null || true
  grep -q "Reached target Multi-User System" "$DIR/full-journal.txt" || die "the full system did not reach multi-user.target"
  grep -q "Start.* PiFinder\b" "$DIR/full-journal.txt" || die "the last boot did not start pifinder.service"
  cleanup
  log "PASS: Pi OS -> NixOS migration boots the full system"
fi
