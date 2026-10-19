#!/usr/bin/env python3
"""Analyze the two final sweep CSVs and print paper tables.
Usage: python3 analyze_sweep.py <perthread.csv> <single-manager.csv>"""
import sys, csv
from collections import defaultdict

def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))

def fnum(x):
    try: return float(x)
    except (ValueError, TypeError): return None

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs)/len(xs) if xs else None

def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    rows = load(sys.argv[1]) + load(sys.argv[2])
    miss, tp = defaultdict(list), defaultdict(list)
    for r in rows:
        t = int(r['threads'])
        base = round(int(r['size_mb'])/t)
        k = (r['branch'], r['trace'], r['policy'], base, t)
        miss[k].append(fnum(r['miss'])); tp[k].append(fnum(r['tp']))
    def mm(b,tr,p,base,t): return mean(miss.get((b,tr,p,base,t),[]))
    def mt(b,tr,p,base,t): return mean(tp.get((b,tr,p,base,t),[]))
    traces = ['cache-t-00','msr_proj_0','cluster53','msr_prxy_1']
    sizes = defaultdict(set)
    for (b,tr,p,base,t) in miss: sizes[tr].add(base)
    def s(x): return f"{x:7.4f}" if x is not None else "     NA"
    def g(a,b): return f"{(a-b)*100:+6.2f}p" if (a is not None and b is not None) else "   NA "

    print("="*80)
    print("TABLE 1: LRUForgive miss gap vs LRU (per trace/base-size/threads)")
    print("="*80)
    for tr in traces:
        for base in sorted(sizes[tr]):
            print(f"\n{tr}  base={base}MB")
            print(f"  {'t':>3} | {'LRU':>7} | {'PT LF':>7} {'PTgap':>7} | {'SM LF':>7} {'SMgap':>7}")
            for t in [1,2,4,8,16]:
                lru=mm('perthread',tr,'lru',base,t)
                pt=mm('perthread',tr,'lruforgive',base,t)
                sm=mm('single-manager',tr,'lruforgive',base,t)
                print(f"  {t:>3} | {s(lru)} | {s(pt)} {g(lru,pt)} | {s(sm)} {g(lru,sm)}")

    print("\n"+"="*80)
    print("TABLE 2: S3FIFOForgive gap vs S3FIFO (t=1)")
    print("="*80)
    print(f"  {'trace':>12} {'base':>6} | {'S3FIFO':>7} | {'PT S3FF':>8} {'gap':>7} | {'SM S3FF':>8} {'gap':>7}")
    for tr in traces:
        for base in sorted(sizes[tr]):
            s3=mm('perthread',tr,'s3fifo',base,1)
            pt=mm('perthread',tr,'s3fifoforgive',base,1)
            sm=mm('single-manager',tr,'s3fifoforgive',base,1)
            print(f"  {tr:>12} {base:>6} | {s(s3)} | {s(pt)} {g(s3,pt)} | {s(sm)} {g(s3,sm)}")

    print("\n"+"="*80)
    print("TABLE 3: LRUForgive throughput (MQPS) scaling")
    print("="*80)
    for tr in traces:
        for base in sorted(sizes[tr]):
            print(f"\n{tr}  base={base}MB")
            print(f"  {'t':>3} | {'PT tp':>7} {'SM tp':>7}")
            for t in [1,2,4,8,16]:
                pt=mt('perthread',tr,'lruforgive',base,t)
                sm=mt('single-manager',tr,'lruforgive',base,t)
                ps=f"{pt:7.2f}" if pt is not None else "     NA"
                ss=f"{sm:7.2f}" if sm is not None else "     NA"
                print(f"  {t:>3} | {ps} {ss}")

    print("\n"+"="*80)
    print("TABLE 4: per-trace verdict (smallest base, t=1)")
    print("="*80)
    for tr in traces:
        base=min(sizes[tr])
        lru=mm('perthread',tr,'lru',base,1); lf=mm('perthread',tr,'lruforgive',base,1)
        if lru and lf:
            rel=(lru-lf)/lru*100
            v="WINS" if lf<lru-0.0005 else ("INVERTS" if lf>lru+0.0005 else "FLAT")
            print(f"  {tr:>12} base={base:>5}MB: LRU {lru:.4f} -> LF {lf:.4f} ({(lru-lf)*100:+.2f}pp, {rel:+.2f}% rel) [{v}]")
        else:
            print(f"  {tr:>12} base={base:>5}MB: missing")

    print("\n"+"="*80)
    print("TABLE 5: S3FIFOForgive per-trace verdict (t=1, smallest base)")
    print("="*80)
    for tr in traces:
        base=min(sizes[tr])
        s3=mm('perthread',tr,'s3fifo',base,1)
        pt=mm('perthread',tr,'s3fifoforgive',base,1)
        sm=mm('single-manager',tr,'s3fifoforgive',base,1)
        if s3 and pt:
            v="WINS" if pt<s3-0.0005 else ("INVERTS" if pt>s3+0.0005 else "FLAT")
            flag=" <-- PT/SM DISAGREE" if (sm and abs((s3-pt)-(s3-sm))>0.005) else ""
            print(f"  {tr:>12} base={base:>5}: S3FIFO {s3:.4f} -> PT {pt:.4f} ({(s3-pt)*100:+.2f}pp)[{v}] SM {sm:.4f} ({(s3-sm)*100:+.2f}pp){flag}")

    print("\n"+"="*80)
    print("TABLE 6: LRUForgive headline EXCLUDING t=2 (anomalous LRU baseline)")
    print("  operating-point size only; shows gap-hold vs gap-degrade cleanly")
    print("="*80)
    for tr in traces:
        base=min(sizes[tr])
        print(f"\n{tr} base={base}MB (t=2 excluded)")
        print(f"  {'t':>3} | {'PTgap':>7} {'SMgap':>7}")
        for t in [1,4,8,16]:
            lru=mm('perthread',tr,'lru',base,t)
            pt=mm('perthread',tr,'lruforgive',base,t)
            sm=mm('single-manager',tr,'lruforgive',base,t)
            pg=f"{(lru-pt)*100:+6.2f}p" if (lru and pt) else "   NA "
            sg=f"{(lru-sm)*100:+6.2f}p" if (lru and sm) else "   NA "
            print(f"  {t:>3} | {pg} {sg}")


if __name__=='__main__': main()
