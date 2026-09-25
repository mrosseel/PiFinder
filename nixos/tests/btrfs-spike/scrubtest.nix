# Damaged-block test: single-bad.fs must give a read error, dup-bad.fs must
# read correctly and repair the bad copy. Make the images with mkdisk.sh and
# corrupt.py first. Run: SPIKE_FLAKE=git+file://<repo>?rev=<rev> nix build --impure -f scrubtest.nix
let
  flake = builtins.getFlake (builtins.getEnv "SPIKE_FLAKE");
  pkgs = import flake.inputs.nixpkgs { system = "x86_64-linux"; };
  single = builtins.path { path = ./single-bad.fs; name = "single-bad.fs"; };
  dup = builtins.path { path = ./dup-bad.fs; name = "dup-bad.fs"; };
in
pkgs.testers.runNixOSTest {
  name = "btrfs-dup-repair";
  nodes.m = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.btrfs-progs ];
    virtualisation.memorySize = 1024;
    virtualisation.qemu.options = [
      "-drive file=${single},format=raw,if=virtio,snapshot=on"
      "-drive file=${dup},format=raw,if=virtio,snapshot=on"
    ];
  };
  testScript = ''
    k = "/boot/nixos/3b8j4awz7vcvl50ff9cravsxj3sf6n4y-linux-rpi-6.12.47-stable_20250916-Image"
    m.wait_for_unit("multi-user.target")
    m.succeed("mkdir -p /single /dup && mount /dev/vdb /single && mount /dev/vdc /dup")
    # single: the damaged block must give a read error, not wrong bytes
    rc, out = m.execute(f"cat /single{k} > /dev/null")
    print("single read rc:", rc)
    assert rc != 0, "single: damaged kernel read without error"
    print(m.execute("btrfs scrub start -B /single 2>&1")[1])
    # dup: the read must succeed with the right content, and scrub must repair
    assert m.succeed(f"sha256sum /dup{k}").split()[0] == "570cc6742ff003d7ead233dcee20f2660f1df7440b3cfd95e10c37277e968e4b", "dup: wrong content"
    # The read above repairs the bad copy (kernel log: "read error corrected")
    assert "read error corrected" in m.succeed("dmesg"), "dup: no repair logged"
    out2 = m.succeed("btrfs scrub start -B /dup 2>&1")
    print(out2)
    assert "no errors found" in out2, "dup: errors left after scrub"
  '';
}
