# btrfs spike tests (NixOS ADR 0009)

Steps 1 and 2 of the ADR 0009 spike, runnable on an x86_64 dev machine.

- `mkdisk.sh <name> <single|dup>`: a btrfs (zstd) disk image with `root/` as
  content, at partition 2 like `mmc 0:2`. Put a real `/boot` into `root/boot`.
- `uboot_btrfs_read.py <u-boot.bin> <disk.img> <root> <kernel> <initrd>`: U-Boot
  (`ubootQemuAarch64` with `CONFIG_FS_BTRFS`) in QEMU loads the files from the
  btrfs, compares CRC32 values, and runs `sysboot`. Needs a QEMU that starts
  without io_uring in restricted environments (10.x works).
- `uboot_btrfs_read_image.py <u-boot.bin> <sd-image>`: the same against a real
  `images.pifinder-btrfs` SD image, with `FDTDIR` and the Pi 4 device tree.
- `corrupt.py <name>`: damages the first copy of one compressed kernel extent.
- `scrubtest.nix`: NixOS VM test of the damaged images (read error with single,
  read repair with dup).
- `resizetest.nix`: NixOS VM test of the first-boot grow commands.
