{ config, lib, pkgs, options, ... }:

# Optional btrfs root filesystem with zstd compression (NixOS ADR 0009,
# proposed). ext4 stays the default. With pifinder.rootFs = "btrfs" the root
# partition (label PIFINDER_SD, holding /boot, see ADR 0007) is btrfs, the SD
# image is built with mkfs.btrfs, and the first boot grows btrfs instead of
# ext4. U-Boot reads it with CONFIG_FS_BTRFS (ubootSD in flake.nix).

let
  cfg = config.pifinder;
  btrfs = cfg.rootFs == "btrfs";
  hasSdImage = options ? sdImage;
in
{
  options.pifinder = {
    rootFs = lib.mkOption {
      type = lib.types.enum [ "ext4" "btrfs" ];
      default = "ext4";
      description = "Filesystem of the root partition (NIXOS_SD on ext4, PIFINDER_SD on btrfs).";
    };
    btrfsDataProfile = lib.mkOption {
      type = lib.types.enum [ "single" "dup" ];
      default = "single";
      description = ''
        btrfs data profile. "dup" keeps two copies of all file data on the
        card, so btrfs scrub can repair a bad block. Metadata is always dup.
      '';
    };
  };

  config = lib.mkIf btrfs (lib.mkMerge [
    {
      # A btrfs root is always newly created (SD image or migration), so it
      # gets the PiFinder label; ext4 roots keep NIXOS_SD.
      fileSystems."/" = lib.mkForce {
        device = "/dev/disk/by-label/PIFINDER_SD";
        fsType = "btrfs";
        options = [ "compress=zstd:1" "noatime" ];
      };
      boot.initrd.supportedFilesystems = [ "btrfs" ];
      environment.systemPackages = [ pkgs.btrfs-progs ];
    }

    (lib.optionalAttrs hasSdImage {
      sdImage.rootVolumeLabel = "PIFINDER_SD";
      sdImage.rootFilesystemImage = pkgs.callPackage ./make-btrfs-fs.nix {
        inherit (config.sdImage) storePaths compressImage;
        populateImageCommands = config.sdImage.populateRootCommands;
        volumeLabel = config.sdImage.rootVolumeLabel;
        dataProfile = cfg.btrfsDataProfile;
      };

      # The sd-image module grows the root with resize2fs (ext4 only). This is
      # the same first-boot step for btrfs.
      sdImage.expandOnBoot = false;
      systemd.services.expand-root-btrfs = {
        description = "Grow the root partition and btrfs to fill the SD card";
        unitConfig = {
          DefaultDependencies = false;
          ConditionPathExists = config.sdImage.nixPathRegistrationFile;
        };
        wantedBy = [ "sysinit.target" ];
        before = [ "sysinit.target" "shutdown.target" "register-nix-paths.service" ];
        after = [ "local-fs.target" ];
        conflicts = [ "shutdown.target" ];
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        script = ''
          rootPart=$(${lib.getExe' pkgs.util-linux "findmnt"} -n -o SOURCE /)
          bootDevice=$(${lib.getExe' pkgs.util-linux "lsblk"} -npo PKNAME $rootPart)
          partNum=$(${lib.getExe' pkgs.util-linux "lsblk"} -npo MAJ:MIN $rootPart | ${lib.getExe pkgs.gawk} -F: '{print $2}')
          echo ",+," | ${lib.getExe' pkgs.util-linux "sfdisk"} -N$partNum --no-reread $bootDevice
          ${lib.getExe' pkgs.parted "partprobe"}
          ${lib.getExe' pkgs.btrfs-progs "btrfs"} filesystem resize max /
        '';
      };
    })
  ]);
}
