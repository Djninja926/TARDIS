#!/usr/bin/env python3
"""
Characterize an oracleGeneral trace to compare access-pattern stationarity.
Evidence for why some traces hang S3-FIFO's eviction (bursty/non-stationary)
and others don't (smooth/stationary like zipf).

oracleGeneral record format (24 bytes, little-endian):
  uint32 timestamp, uint64 obj_id, uint32 obj_size, uint32 next_access_vtime, (pad)
We only need obj_id (offset 4, 8 bytes) here.

Usage: python3 characterize_trace.py <trace_path> [max_records]
"""
import sys, struct
from collections import Counter, defaultdict

REC = 24  # bytes per record; adjust if inventory shows otherwise

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    path = sys.argv[1]
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 5_000_000

    freq = Counter()
    n = 0
    # windowed unique-object churn: how much does the hot set shift over time?
    WINDOW = 200_000
    win_sets = []
    cur = set()
    with open(path, 'rb') as f:
        while n < cap:
            b = f.read(REC)
            if len(b) < REC:
                break
            obj = struct.unpack_from('<Q', b, 4)[0]  # obj_id at offset 4
            freq[obj] += 1
            cur.add(obj)
            n += 1
            if n % WINDOW == 0:
                win_sets.append(cur)
                cur = set()
    if cur:
        win_sets.append(cur)

    if n == 0:
        print("no records read; wrong REC size?"); return

    total = sum(freq.values())
    uniq = len(freq)
    counts = sorted(freq.values(), reverse=True)
    # popularity skew: what fraction of accesses go to the top 1% of objects?
    top1pct = max(1, uniq // 100)
    hot_access = sum(counts[:top1pct])
    # one-hit-wonders: objects accessed exactly once
    ohw = sum(1 for c in counts if c == 1)

    # inter-window churn: Jaccard distance between consecutive windows'
    # object sets. High churn = non-stationary (hot set moves). Low = stationary.
    churns = []
    for a, b in zip(win_sets, win_sets[1:]):
        inter = len(a & b)
        union = len(a | b)
        if union:
            churns.append(1 - inter / union)  # Jaccard distance
    avg_churn = sum(churns) / len(churns) if churns else float('nan')

    print(f"trace: {path}")
    print(f"records read: {n:,}")
    print(f"unique objects: {uniq:,}")
    print(f"accesses/object (mean): {total/uniq:.2f}")
    print(f"one-hit-wonders: {ohw:,} ({100*ohw/uniq:.1f}% of objects)")
    print(f"top-1% objects capture: {100*hot_access/total:.1f}% of accesses  (higher = more skewed/predictable)")
    print(f"windowed hot-set churn (Jaccard dist, {WINDOW//1000}k-req windows): {avg_churn:.3f}")
    print(f"  -> ~0 = stationary (hot set stable, like zipf); ~1 = highly non-stationary (hot set shifts, bursty)")
    print(f"windows: {len(win_sets)}")

if __name__ == '__main__':
    main()
