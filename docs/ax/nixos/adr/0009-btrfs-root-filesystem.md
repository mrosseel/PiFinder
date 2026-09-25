# The root partition moves from ext4 to btrfs with zstd compression, after a boot spike passes (proposed)

**Status:** proposed. The decision waits for the spike below.

The root partition (`mmc 0:2`, label `NIXOS_SD`) holds `/nix/store`, the user data and `/boot` with `extlinux.conf` and the kernels ([0007](./0007-boot-on-ext4-fat-firmware-only.md)). We propose to format it as btrfs with transparent zstd compression instead of ext4. The FAT firmware partition does not change.

## Why

- **Space and read time.** The Nix store holds text, Python bytecode and uncompressed libraries, which compress well. Compression reduces SD space and the number of bytes read from the card at boot and during an upgrade. A PiFinder SD card is 32 GB. On pifinder-mr (26.05, its current generations and user data) the root partition uses 8.1 GB.
- **Checksums.** btrfs checksums data and metadata. A bad SD card block gives a read error instead of silently wrong bytes. On ext4, a corrupt store path is found only by `nix-store --verify`.
- **Crash safety.** btrfs is copy-on-write: after a power cut a file has its old or its new content. Renames are atomic, as on ext4, so the rule from 0007 (the watchdog rewrites `/boot` without supervision) still holds.

Rollback is **not** a reason. Nix generations give rollback ([0005](./0005-self-arming-watchdog-confirmed-generations.md)), so btrfs snapshots are not used.

## The spike (must pass before this ADR is accepted)

On one Pi 4B (PiFinder rev 3) and one CM4 (PiFinder v4):

1. Build `ubootSD` with `CONFIG_FS_BTRFS=y` (and `CONFIG_CMD_BTRFS=y` for manual checks). `sysboot mmc 0:2 any …` detects the filesystem type, so the boot command does not change.
2. Format the root partition as btrfs, mount it with `compress=zstd`, and install a PiFinder system on it.
3. Pass criteria:
   - U-Boot reads `extlinux.conf` and loads a **compressed** kernel and initrd. Force this with `btrfs property set /boot compression zstd` before the files are written.
   - Boot to the PiFinder UI on both boards, 20 cold boots each.
   - An upgrade, a watchdog rollback and a camera switch each rewrite `/boot`, and the next boot works.
   - 20 power cuts during an upgrade, then a boot: the system boots the old or the new generation, and `btrfs scrub` reports no errors.
   - Record the compressed size of the store and the boot time, compared with ext4.

If U-Boot cannot read compressed extents, keep `/boot` uncompressed (`chattr +m` or a no-compression property on `/boot`) and compress only the rest.

## Consequences if accepted

- **Filesystem layout.** One btrfs filesystem, no subvolumes, so U-Boot reads `/boot` from the default subvolume. Mount options `compress=zstd:1,noatime`. `fileSystems."/".fsType = "btrfs"`, and the initrd needs the btrfs module.
- **SD image.** The image build creates an ext4 filesystem image today (the nixpkgs sd-image module). It must create a btrfs image instead. Check what nixpkgs 26.05 offers for this before the implementation.
- **Migration from Raspbian.** This changes the init script in brickbots/PiFinder#657, which collects the Raspbian-side migration changes:
  - `mkfs.btrfs` instead of `mkfs.ext4`. Raspbian does not ship `btrfs-progs` by default, so the migration initramfs must carry a static `mkfs.btrfs`, and the pre-flight check must confirm that it runs before the point of no return.
  - `btrfs filesystem resize max /` on the mounted root instead of `e2fsck` + `resize2fs`.
  - The pre-flight minimum SD size can rise from 16 GB to 32 GB, because PiFinders ship with 32 GB cards.
- **Full disk.** btrfs recovers badly from a completely full filesystem. Keep a reserve: the upgrade refuses to start below a free-space floor, and the weekly GC keeps running. On a 32 GB card this is a small risk.
- **Existing NixOS devices.** Devices that already run NixOS on ext4 stay on ext4. An in-place conversion (`btrfs-convert`) is not planned. Only new images and new migrations get btrfs.
- **Recovery tools.** The recovery path and the SSH troubleshooting notes must cover `btrfs scrub` and `btrfs check` in place of `e2fsck`.

## Fault tolerance

btrfs can keep two copies of each block on the same device (the DUP profile). When a read or `btrfs scrub` finds a checksum error, btrfs repairs the block from the second copy. Metadata and data have separate profiles.

- **Metadata: DUP.** This is the btrfs default on a single device. Metadata is small, so the cost is low, and it protects the filesystem structure itself.
- **Data: single, unless the spike shows DUP is worth it.** Data DUP doubles the space that data uses (compression gives back part of it) and doubles every data write, which means more SD wear and slower upgrades. It survives a bad block, but not a dead card or a failed card controller. The btrfs documentation also warns that some flash controllers de-duplicate identical writes internally. Then both copies can end up in the same physical block, and DUP gives no protection.
- **The Nix store without data DUP.** A bad block in a store path gives a checksum error instead of wrong bytes. `nix-store --repair-path` then downloads a good copy from the binary cache, so the system can be repaired while the device has internet. The recovery notes must describe this step.
- **User data.** Observations, `config.json`, locations, equipment and observing lists are the only data that cannot be downloaded again. They are small, so a backup protects them better than DUP: a copy in a second place on the card, and a download through the web UI's Data page.

Spike addition: format a second card with `-d dup` and record the used space, the upgrade time and a `btrfs scrub` repair after a deliberately damaged data block, next to the single-data card. Choose the data profile from those numbers.

## Considered options

- **Data DUP on every device, not chosen yet.** Repairs a bad data block on its own, but at twice the data space and writes, and with no protection when the card controller de-duplicates. The spike decides.
- **Stay on ext4, rejected if the spike passes.** Simplest and proven with U-Boot, but no compression and no data checksums.
- **f2fs, rejected.** Made for flash and supports compression, but U-Boot has no f2fs reader, so `/boot` would need its own partition, against 0007.
- **ext4 root with a separate compressed partition for `/nix`, rejected.** Keeps U-Boot on ext4, but splits the card into fixed sizes and adds a partition to the migration.
- **btrfs subvolumes (`@`, `@home`) with snapshots, rejected.** Snapshots duplicate what Nix generations do, and subvolumes make U-Boot's path to `/boot` depend on the subvolume layout.
