"""Boot U-Boot (qemu_arm64, FS_BTRFS) on a disk image and check that it reads
the btrfs /boot on partition 2 byte for byte.

usage: uboot_btrfs_read.py <u-boot.bin> <disk.img> <root dir> <kernel name> <initrd name>
"""

import os
import re
import select
import subprocess
import sys
import time
import zlib

uboot, disk, root, kn, inn = sys.argv[1:6]
files = {
    "kernel": f"/boot/nixos/{kn}",
    "initrd": f"/boot/nixos/{inn}",
    "extlinux": "/boot/extlinux/extlinux.conf",
}
expected = {
    k: zlib.crc32(open(root + p, "rb").read()) & 0xFFFFFFFF for k, p in files.items()
}

qemu = subprocess.Popen(
    [
        "qemu-system-aarch64", "-M", "virt", "-cpu", "cortex-a72", "-m", "1024",
        "-nographic", "-monitor", "none", "-serial", "stdio",
        "-bios", uboot,
        "-drive", f"file={disk},if=none,format=raw,id=d0,snapshot=on,aio=threads",
        "-device", "virtio-blk-device,drive=d0",
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
out = b""


def read_until(pattern: bytes, timeout: float) -> bytes:
    global out
    end = time.monotonic() + timeout
    start = len(out)
    while time.monotonic() < end:
        r, _, _ = select.select([qemu.stdout], [], [], 0.2)
        if r:
            chunk = os.read(qemu.stdout.fileno(), 65536)
            if not chunk:
                break
            out += chunk
            if re.search(pattern, out[start:]):
                return out[start:]
    raise TimeoutError(f"waiting for {pattern!r}; got: {out[start:][-400:]!r}")


_seq = 0


def cmd(line: str, timeout: float = 60, until: bytes | None = None) -> str:
    """Run one U-Boot command. The output ends at a unique marker line that an
    `echo` after the command prints, so outputs never mix."""
    global _seq
    _seq += 1
    marker = f"@@{_seq}@@"
    qemu.stdin.write(f"{line}; echo {marker}\n".encode())
    qemu.stdin.flush()
    pattern = until or (rb"\n" + marker.encode() + rb"\r?\n")
    return read_until(pattern, timeout).decode(errors="replace")


ok = True
try:
    read_until(rb"=> $", 60)
    print(cmd("virtio scan").strip().splitlines()[-2:])
    print(cmd("ls virtio 0:2 /boot/nixos"))
    addr = {"kernel": "${kernel_addr_r}", "initrd": "${ramdisk_addr_r}",
            "extlinux": "${scriptaddr}"}
    for name, path in files.items():
        t0 = time.monotonic()
        res = cmd(f"load virtio 0:2 {addr[name]} {path}")
        loaded = re.search(r"(\d+) bytes read", res)
        if not loaded:
            print(f"--- U-Boot output for {name}:")
            print("\n".join(res.strip().splitlines()[-8:]))
        crc_out = cmd(f"crc32 {addr[name]} ${{filesize}}")
        m = re.search(r"==> ([0-9a-f]{8})", crc_out)
        got = int(m.group(1), 16) if m else None
        match = got == expected[name]
        ok &= match
        print(f"{name}: {loaded.group(1) if loaded else '?'} bytes in "
              f"{time.monotonic() - t0:.1f}s, crc32 {got and f'{got:08x}'} "
              f"expected {expected[name]:08x} -> {'OK' if match else 'MISMATCH'}")
    res = cmd("sysboot virtio 0:2 any ${scriptaddr} /boot/extlinux/extlinux.conf",
              timeout=120, until=rb"Starting kernel|\n@@\d+@@")
    started = "Starting kernel" in res
    ok &= started
    print("sysboot:", "reached 'Starting kernel'" if started else "FAILED")
    print("\n".join(line for line in res.splitlines() if "Retrieving" in line))
finally:
    qemu.kill()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
