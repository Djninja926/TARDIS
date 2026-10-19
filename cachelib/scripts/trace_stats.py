#!/usr/bin/env python3
"""
trace_stats.py TRACE.oracleGeneral... [--max N]

What a trace looks like, for the one-hit-wonder screen (doc 19.10-19.11, 20.2)
and the zipf vs real-trace comparison (doc 39.2):

  requests, distinct objects
  one-hit wonders (objects requested once), as a share of objects and of requests
  accesses per object, share of requests going to the top 1% of objects
  hot-set churn: Jaccard distance between the objects of consecutive
  200k-request windows (about 0 = stable hot set, about 1 = it keeps moving)

--max N reads only the first N requests (the doc's churn numbers used 5M).
oracleGeneral records are 24 bytes: uint32 time, uint64 id, uint32 size, int64 next access.
"""
import os, sys
import numpy as np

REC = np.dtype([("t", "<u4"), ("oid", "<u8"), ("sz", "<u4"), ("nxt", "<i8")])
WINDOW = 200_000


def stats(path, limit=None):
    n = os.path.getsize(path) // REC.itemsize
    if limit:
        n = min(n, limit)
    oids = np.asarray(np.memmap(path, dtype=REC, mode="r", shape=(n,))["oid"])
    _, counts = np.unique(oids, return_counts=True)
    ohw = int(np.count_nonzero(counts == 1))
    top = np.sort(counts)[::-1][:max(1, counts.size // 100)].sum()
    churn = []
    prev = None
    for i in range(0, n, WINDOW):
        cur = np.unique(oids[i:i + WINDOW])
        if prev is not None:
            inter = np.intersect1d(prev, cur, assume_unique=True).size
            churn.append(1 - inter / (prev.size + cur.size - inter))
        prev = cur
    print(os.path.basename(path) + (f"  (first {n:,} requests)" if limit else ""))
    print(f"  requests             : {n:,}")
    print(f"  distinct objects     : {counts.size:,}")
    print(f"  one-hit wonders      : {ohw:,}  ({100 * ohw / counts.size:.1f}% of objects, "
          f"{100 * ohw / n:.1f}% of requests)")
    print(f"  accesses per object  : {n / counts.size:.2f}")
    print(f"  top 1% of objects    : {100 * top / n:.1f}% of requests")
    if churn:
        print(f"  hot-set churn        : {sum(churn) / len(churn):.3f}")


def main(argv):
    limit = None
    if "--max" in argv:
        i = argv.index("--max")
        limit = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if not argv:
        sys.exit(__doc__)
    for p in argv:
        stats(p, limit)


if __name__ == "__main__":
    main(sys.argv[1:])
