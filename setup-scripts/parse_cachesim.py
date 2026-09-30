#!/usr/bin/env python3
"""
parse_cachesim.py - turn cachesim stdout into sweep CSV rows.

VERIFIED against a real build of Aryan's fork (embeddings branch). When cachesim
runs several (algo, size) cells, results are printed by bin/cachesim/main.c:

  <trace_path> <cache_name> cache size <N><unit>, <n> req, miss ratio <m>, \
byte miss ratio <b>

Three properties of that printer drive this parser's design:

1. There is NO throughput field on this path (that only exists in sim.c's
   single-cell printer). Requiring it matched zero lines.
2. The size is printed as an INTEGER in ONE unit chosen from the FIRST cache
   size and applied to every row, truncated. With sizes 0.1,0.001 on a 1.5GiB
   footprint the small cell prints as "0GiB". So the printed size is never used
   for bytes. Exact bytes come from the sizes passed to cachesim: absolute
   tokens are exact already, fractional tokens are frac * wss_byte, where
   wss_byte is read from cachesim's own working-set log line on stderr.
3. Rows are printed in a FIXED order: result[i*n_sizes + j] for algo i, size j
   (cli_parser.c), all printed together after every cell finishes. So row k
   maps to size index k % n_sizes exactly, with no need to rank rounded sizes.

cache_name is the DETAILED name, e.g. "LRUForgiveEmbCache-th0.40-lr0.20" or
"S3FIFO-0.1000-2"; only some hyperparameters appear in it, so the full
--eviction-params string is recorded in its own column.

Modes:
  --header                    print the CSV header
  --wss-from ERRFILE          print "wss_obj wss_byte sample_ratio" and exit
  --lookup-params JSON        print the tuned --eviction-params for
                              --dataset/--pct/--policy (empty = defaults). Keys are
                              "<dataset>|<pct>|<policy>", with "*" wildcards for
                              dataset and/or pct as fallbacks.
  (default)                   parse --raw into CSV rows on stdout
"""
import argparse, re, sys

COLUMNS = ("node,dataset,trace,policy,cache_name,cache_pct,size_bytes,"
           "requests,miss,byte_miss,params")

# main.c:70  "%s %s cache size %8ld%s, %lld req, miss ratio %.4lf, byte miss ratio %.4lf"
# throughput is optional so sim.c's single-cell format also parses.
LINE_RE = re.compile(
    r"^(?P<path>\S+)\s+(?P<name>.+?)\s+cache size\s+(?P<size>\d+)(?P<unit>TiB|GiB|MiB|KiB|B)?,\s+"
    r"(?P<req>\d+)\s+req,\s+miss ratio\s+(?P<miss>[\d.eE+-]+),\s+"
    r"byte miss ratio\s+(?P<bmiss>[\d.eE+-]+)"
    r"(?:,\s+throughput\s+[\d.eE+-]+\s+MQPS)?\s*$"
)

WSS_RE = re.compile(r"(estimated )?working set size(?: \(([\d.]+) sample ratio\))?:\s*"
                    r"(\d+)\s*object\s*(\d+)\s*byte")

# longest prefix first: "S3FIFOForgiveEmbCache" must not normalize to "s3fifo"
POLICY_PREFIXES = [
    ("S3FIFOForgiveEmbCache", "s3fifoforgive-embcache"),
    ("LRUForgiveEmbCache", "lruforgiveembcache"),
    ("S3FIFO", "s3fifo"),
    ("LRU", "lru"),
]


def normalize_policy(cache_name):
    for prefix, norm in POLICY_PREFIXES:
        if cache_name.startswith(prefix):
            return norm
    return cache_name.lower()


def read_wss(path):
    """(wss_obj, wss_byte, sample_ratio) from cachesim stderr, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    m = None
    for m in WSS_RE.finditer(text):
        pass                                   # keep the last occurrence
    if not m:
        return None
    return int(m.group(3)), int(m.group(4)), m.group(2) or "1"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", help="file holding cachesim stdout")
    ap.add_argument("--node", default="")
    ap.add_argument("--dataset", default="")
    ap.add_argument("--trace", default="")
    ap.add_argument("--sizes", default="",
                    help="the exact cache-size argument passed to cachesim, e.g. 0.1,0.001")
    ap.add_argument("--pcts", default="",
                    help="labels for --sizes, same order, e.g. 10,0.1")
    ap.add_argument("--wss-err", default="",
                    help="cachesim stderr, for exact bytes of fractional sizes")
    ap.add_argument("--params", default="default")
    ap.add_argument("--header", action="store_true")
    ap.add_argument("--wss-from", metavar="ERRFILE")
    ap.add_argument("--lookup-params", metavar="JSON")
    ap.add_argument("--pct", default="")
    ap.add_argument("--policy", default="")
    args = ap.parse_args()

    if args.lookup_params:
        import json
        with open(args.lookup_params, encoding="utf-8") as f:
            table = json.load(f)
        d, c, p = args.dataset, args.pct, args.policy
        for key in (f"{d}|{c}|{p}", f"{d}|*|{p}", f"*|{c}|{p}", f"*|*|{p}"):
            if key in table:
                print(table[key] or "")
                return
        print("")
        return

    if args.header:
        print(COLUMNS)
        return

    if args.wss_from:
        w = read_wss(args.wss_from)
        if not w:
            print("# no working-set line found", file=sys.stderr)
            sys.exit(2)
        print(f"{w[0]} {w[1]} {w[2]}")
        return

    sizes = [s.strip() for s in args.sizes.split(",") if s.strip()]
    pcts = [p.strip() for p in args.pcts.split(",") if p.strip()]
    if not sizes or len(sizes) != len(pcts):
        print(f"# --sizes and --pcts must be non-empty and the same length "
              f"(got {sizes} / {pcts})", file=sys.stderr)
        sys.exit(2)

    # exact bytes per size index
    wss = read_wss(args.wss_err) if args.wss_err else None
    exact = []
    for tok in sizes:
        if "." in tok:                         # cachesim's own rule for "fraction"
            exact.append(str(int(wss[1] * float(tok))) if wss else "NA")
        else:
            exact.append(tok)

    rows = []
    with open(args.raw, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE_RE.match(line.strip())
            if m:
                rows.append(m.groupdict())

    tag = f"{args.dataset}/{args.trace}"
    if not rows:
        print(f"# NO PARSABLE CELLS for {tag}", file=sys.stderr)
        sys.exit(2)
    if len(rows) % len(sizes) != 0:
        print(f"# CELL COUNT {len(rows)} is not a multiple of {len(sizes)} sizes for {tag}; "
              f"refusing to guess the size mapping", file=sys.stderr)
        sys.exit(2)

    # Cross-check the positional mapping against the printed (truncated) sizes:
    # within one algo's block, printed values must be ordered like the requested
    # byte sizes. Same unit on every row, so integer comparison is valid.
    n = len(sizes)
    for b in range(0, len(rows), n):
        block = rows[b:b + n]
        if len({r["name"] for r in block}) != 1:
            print(f"# BLOCK {b // n} mixes cache names for {tag}; order assumption broken",
                  file=sys.stderr)
            sys.exit(2)
        if all(e != "NA" for e in exact):
            for j in range(n - 1):
                big = int(exact[j]) >= int(exact[j + 1])
                pbig = int(block[j]["size"]) >= int(block[j + 1]["size"])
                if big != pbig and block[j]["size"] != block[j + 1]["size"]:
                    print(f"# SIZE ORDER MISMATCH in block {b // n} for {tag}", file=sys.stderr)
                    sys.exit(2)

    for k, r in enumerate(rows):
        j = k % n
        print(",".join([
            args.node, args.dataset, args.trace,
            normalize_policy(r["name"]), r["name"],
            pcts[j], exact[j],
            r["req"], r["miss"], r["bmiss"],
            args.params.replace(",", ";"),     # "lr=0.1,th=0.5" would split the row
        ]))

    print(f"# {tag}: {len(rows)} cells", file=sys.stderr)


if __name__ == "__main__":
    main()
