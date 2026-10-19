#!/usr/bin/env python3
# Fast one-hit-wonder counter for oracleGeneral traces.
# Vectorized: reads the obj_id column with numpy, counts via bincount-style unique.
# oracleGeneral record = 24 bytes: <u4 time, u8 obj_id, u4 size, i8 next_vtime>
import sys, os
import numpy as np

REC = 24
# obj_id is the 8-byte field at offset 4 within each 24-byte record.
DTYPE = np.dtype([('t', '<u4'), ('oid', '<u8'), ('sz', '<u4'), ('nxt', '<i8')])

def analyze(path):
    fsize = os.path.getsize(path)
    if fsize % REC != 0:
        print(f"WARNING {path}: size {fsize} not divisible by 24; format may differ.")
    n = fsize // REC
    # memory-map; pull just the oid column
    arr = np.memmap(path, dtype=DTYPE, mode='r', shape=(n,))
    oids = np.asarray(arr['oid'])
    total = oids.size
    # counts of each unique id
    _, counts = np.unique(oids, return_counts=True)
    distinct = counts.size
    ohw = int(np.count_nonzero(counts == 1))
    print(f"{os.path.basename(path)}")
    print(f"  requests            : {total:,}")
    print(f"  distinct objects    : {distinct:,}")
    print(f"  one-hit-wonders     : {ohw:,}")
    print(f"  OHW / distinct objs : {100*ohw/distinct:.1f}%")
    print(f"  OHW / requests      : {100*ohw/total:.1f}%")
    sys.stdout.flush()

if __name__ == "__main__":
    for p in sys.argv[1:]:
        analyze(p)
