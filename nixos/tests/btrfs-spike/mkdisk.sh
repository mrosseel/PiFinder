#!/usr/bin/env bash
# mkdisk.sh <name> <data-profile>: btrfs (zstd) with root/ as content, placed as
# partition 2 of an MBR disk image, like mmc 0:2 on the PiFinder.
set -euo pipefail
name=$1 data=$2
rm -f "$name.fs" "$name.img"
truncate -s 400M "$name.fs"
mkfs.btrfs -q -L NIXOS_SD -d "$data" -m dup --rootdir root --compress zstd:3 "$name.fs"
truncate -s 452M "$name.img"
printf 'label: dos\nstart=2048, size=100352, type=c\nstart=102400, type=83\n' | sfdisk -q "$name.img"
dd if="$name.fs" of="$name.img" bs=512 seek=102400 conv=notrunc status=none
