# Grow a btrfs partition the way expand-root-btrfs does: sfdisk -N2 ",+,",
# partprobe, btrfs filesystem resize max. The disk is a 452 MiB image with
# the btrfs at partition 2, attached with 2 GiB of extra space.
let
  flake = builtins.getFlake (builtins.getEnv "SPIKE_FLAKE");
  pkgs = import flake.inputs.nixpkgs { system = "x86_64-linux"; };
  img = builtins.path { path = ./single.img; name = "single.img"; };
in
pkgs.testers.runNixOSTest {
  name = "btrfs-grow";
  nodes.m = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.btrfs-progs pkgs.parted pkgs.util-linux ];
    virtualisation.memorySize = 1024;
  };
  testScript = ''
    m.wait_for_unit("multi-user.target")
    m.copy_from_host("${img}", "/tmp/disk.img")
    m.succeed("truncate -s +2G /tmp/disk.img && losetup -P /dev/loop7 /tmp/disk.img")
    m.succeed("mkdir -p /mnt && mount /dev/loop7p2 /mnt")
    before = int(m.succeed("findmnt -bno SIZE /mnt"))
    m.succeed('echo ",+," | sfdisk -N2 --no-reread /dev/loop7')
    m.succeed("partprobe /dev/loop7 || losetup -c /dev/loop7")
    m.succeed("btrfs filesystem resize max /mnt")
    after = int(m.succeed("findmnt -bno SIZE /mnt"))
    print(f"size before {before >> 20} MiB, after {after >> 20} MiB")
    assert after > before + (1800 << 20), "btrfs did not grow"
    m.succeed("btrfs scrub start -B /mnt")
  '';
}
