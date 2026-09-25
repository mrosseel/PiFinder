"""corrupt.py <name>: damage mirror 1 of one compressed extent of the kernel
in <name>.fs, then rebuild <name>-bad.img. Prints what it changed."""
import re, subprocess, sys, shutil
name = sys.argv[1]
fs = f"{name}.fs"
tree = subprocess.run(["btrfs", "inspect-internal", "dump-tree", "-t", "5", fs],
                      capture_output=True, text=True).stdout
# The kernel is the only regular file of 33225216 bytes.
m = re.search(r"key \((\d+) INODE_ITEM 0\).*?\n\s+generation \d+ transid \d+ size 33225216", tree)
ino = m.group(1)
ext = re.findall(rf"key \({ino} EXTENT_DATA (\d+)\).*?\n\s+generation \d+ type 1 \(regular\)\n\s+extent data disk byte (\d+) nr (\d+)", tree)
off, bytenr, nr = ext[len(ext) // 2]
mp = subprocess.run(["btrfs-map-logical", "-l", bytenr, "-b", "4096", fs],
                    capture_output=True, text=True).stdout
phys = [int(x) for x in re.findall(r"mirror \d+ logical \d+ physical (\d+)", mp)]
print(f"kernel inode {ino}: {len(ext)} extents; damaging extent at file offset {off}, "
      f"logical {bytenr} ({nr} bytes); mirrors at physical {phys}")
shutil.copy(fs, f"{name}-bad.fs")
with open(f"{name}-bad.fs", "r+b") as f:
    f.seek(phys[0] + 100)
    f.write(b"\xde\xad\xbe\xef" * 64)
shutil.copy(f"{name}.img", f"{name}-bad.img")
subprocess.run(["dd", f"if={name}-bad.fs", f"of={name}-bad.img", "bs=512", "seek=102400",
                "conv=notrunc", "status=none"], check=True)
