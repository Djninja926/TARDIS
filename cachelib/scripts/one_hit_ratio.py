#!/usr/bin/env python3
# One-hit-wonder counter for oracleGeneral traces (24-byte packed: <I Q I q>).
import struct, sys, os
from collections import defaultdict
REC = struct.Struct("<IQIq"); assert REC.size == 24
def analyze(path):
    fsize = os.path.getsize(path)
    if fsize % REC.size != 0:
        print(f"WARNING {path}: size {fsize} not divisible by 24; format may differ.")
    counts = defaultdict(int)
    with open(path, "rb") as f:
        while True:
            buf = f.read(REC.size * 200000)
            if not buf: break
            end = len(buf) - (len(buf) % REC.size)
            for i in range(0, end, REC.size): counts[REC.unpack_from(buf, i)[1]] += 1
    distinct = len(counts); total = sum(counts.values())
    ohw = sum(1 for c in counts.values() if c == 1)
    print(f"{path}")
    print(f"  requests            : {total:,}")
    print(f"  distinct objects    : {distinct:,}")
    print(f"  one-hit-wonders     : {ohw:,}")
    print(f"  OHW / distinct objs : {100*ohw/distinct:.1f}%")
    print(f"  OHW / requests      : {100*ohw/total:.1f}%")
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: one_hit_ratio.py <trace.oracleGeneral> [more...]"); sys.exit(1)
    for p in sys.argv[1:]: analyze(p)
