# Usage: SPIKE_FLAKE=git+file://<repo>?rev=<rev> SPIKE_KERNEL=latest nix build --impure -f kernelwrite.nix
# Input: single.img from mkdisk.sh. Test the output disk.img with uboot_btrfs_read.py.
# Rewrite /boot/extlinux/extlinux.conf through the Linux kernel on a btrfs
# mounted with compress=zstd, as the NixOS extlinux builder does on every
# upgrade (temporary file, then mv). The result is a kernel-written,
# compressed inline extent. The changed disk image is the build output.
let
  kernel = builtins.getEnv "SPIKE_KERNEL";
  flake = builtins.getFlake (builtins.getEnv "SPIKE_FLAKE");
  pkgs = import flake.inputs.nixpkgs { system = "x86_64-linux"; };
  img = builtins.path { path = ./single.img; name = "single.img"; };
in
pkgs.testers.runNixOSTest {
  name = "btrfs-kernel-write";
  nodes.m = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.btrfs-progs pkgs.util-linux ];
    virtualisation.memorySize = 1024;
    virtualisation.diskSize = 2048;
    # SPIKE_KERNEL=latest: the bug of upstream 6f0719e4c4 needs a kernel
    # that compresses the whole block (7.x); 6.18 does not.
    boot.kernelPackages = pkgs.lib.mkIf (kernel == "latest") pkgs.linuxPackages_latest;
  };
  testScript = ''
    m.wait_for_unit("multi-user.target")
    m.copy_from_host("${img}", "/root/disk.img")
    m.succeed("losetup -P /dev/loop7 /root/disk.img && mkdir -p /mnt && mount -o compress=zstd:1 /dev/loop7p2 /mnt")
    conf = m.succeed("cat /mnt/boot/extlinux/extlinux.conf")
    m.succeed("sed 's/^TIMEOUT 0$/TIMEOUT 1/' /mnt/boot/extlinux/extlinux.conf > /mnt/boot/extlinux/.extlinux.conf.tmp")
    m.succeed("mv -f /mnt/boot/extlinux/.extlinux.conf.tmp /mnt/boot/extlinux/extlinux.conf && sync")
    ino = m.succeed("stat -c %i /mnt/boot/extlinux/extlinux.conf").strip()
    m.succeed("umount /mnt && losetup -d /dev/loop7")
    tree = m.succeed("dd if=/root/disk.img bs=512 skip=102400 of=/root/p2.fs status=none && btrfs inspect-internal dump-tree -t 5 /root/p2.fs")
    import re
    item = re.search(r"key \(" + ino + r" EXTENT_DATA 0\).*?\n(.*?)\n", tree, re.S)
    print("extlinux.conf extent:", item.group(0) if item else "not found")
    m.copy_from_vm("/root/disk.img", "")
  '';
}
