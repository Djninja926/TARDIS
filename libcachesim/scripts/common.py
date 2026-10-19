#!/usr/bin/env python3
"""
common.py - shared helpers for every libCacheSim script, plus the small
command-line helpers the node worker calls.

  Naming    one rule turns a trace URL into (dataset, trace name)
  Lists     read a trace list ("<dataset>\\t<url>" or bare URLs)
  Parsing   turn cachesim output into result rows
  Workloads group datasets into the paper's workloads (Wiki Upload, Tencent Block, ...)
  Families  group near-identical traces (the near-copy check on held-out traces)

Naming:
  CMU mirror  .../cacheDatasets/alibabaBlock/new/1M/alibabaBlock_9.oracleGeneral.zst
              -> dataset "alibabaBlock_new_1M", trace "alibabaBlock_9"
  Wikimedia   .../published/datasets/caching/2019/text/cache-t-00.gz
              -> dataset "wikimedia_2019_text", trace "cache-t-00"
node_worker.sh implements the same rule in bash; change both together.

Command line (used by node_worker.sh):
  python3 common.py header
  python3 common.py wss ERRFILE                       -> "wss_obj wss_byte sample_ratio"
  python3 common.py lookup PARAMS_JSON DATASET PCT POLICY   -> tuned params or ""
  python3 common.py parse RAW ERRFILE NODE DATASET TRACE SIZES PCTS PARAMS
"""
import csv, json, re, sys
from collections import defaultdict

# ---------------------------------------------------------------- naming

ROOTS = (("/cacheDatasets/", ""),
         ("/published/datasets/caching/", "wikimedia_"))
EXTS = (".gz", ".zst", ".tsv", ".csv", ".txt")

POLICIES = ("lruforgiveembcache", "s3fifoforgive-embcache")
BASELINE = {"lruforgiveembcache": "lru", "s3fifoforgive-embcache": "s3fifo"}


def dataset_of(url):
    for root, prefix in ROOTS:
        if root in url:
            path = url.split(root, 1)[1].rsplit("/", 1)[0]
            return prefix + path.replace("/", "_") if path else "unknown"
    return "unknown"


def trace_name(url):
    base = url.rsplit("/", 1)[-1]
    if ".oracleGeneral" in base:
        return base.split(".oracleGeneral")[0]
    changed = True
    while changed:
        changed = False
        for ext in EXTS:
            if base.endswith(ext) and len(base) > len(ext):
                base = base[: -len(ext)]
                changed = True
    return base


def needs_conversion(url):
    """True for traces that are not oracleGeneral (the Wikimedia TSV.gz files)."""
    return ".oracleGeneral" not in url.rsplit("/", 1)[-1]


def read_list(path):
    """[(dataset, trace, url)] from '<dataset>\\t<url>' or bare-URL lines. The
    dataset is always re-derived from the URL, so every script agrees.
    utf-8-sig tolerates the BOM a PowerShell redirect adds."""
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = line.split("\t")[-1]
            out.append((dataset_of(url), trace_name(url), url))
    return out


# ---------------------------------------------------------------- cachesim output
#
# Verified against a real build of the fork. With several (algo, size) cells,
# bin/cachesim/main.c prints, after every cell has finished:
#   <trace> <cache_name> cache size <N><unit>, <n> req, miss ratio <m>, byte miss ratio <b>
# * no throughput field on this path (sim.c's single-cell line has one: optional)
# * with --ignore-obj-size 1 the single-cell line has no byte miss ratio (it
#   would equal the miss ratio): optional, filled with the miss ratio
# * the size is ONE unit chosen from the first size, truncated, so it is never
#   used for bytes; exact bytes come from the sizes we passed (fractions times
#   the working set cachesim logs on stderr)
# * rows come in a fixed order, result[i*n_sizes + j] for algo i, size j, so
#   row k belongs to size k % n_sizes

COLUMNS = ("node,dataset,trace,policy,cache_name,cache_pct,size_bytes,"
           "requests,miss,byte_miss,params")

LINE_RE = re.compile(
    r"^(?P<path>\S+)\s+(?P<name>.+?)\s+cache size\s+(?P<size>\d+)(?P<unit>TiB|GiB|MiB|KiB|B)?,\s+"
    r"(?P<req>\d+)\s+req,\s+miss ratio\s+(?P<miss>[\d.eE+-]+)"
    r"(?:,\s+byte miss ratio\s+(?P<bmiss>[\d.eE+-]+))?"
    r"(?:,\s+throughput\s+[\d.eE+-]+\s+MQPS)?\s*$"
)
WSS_RE = re.compile(r"(estimated )?working set size(?: \(([\d.]+) sample ratio\))?:\s*"
                    r"(\d+)\s*object\s*(\d+)\s*byte")
FORGIVE_RATE_RE = re.compile(r"(?:forgives|forgive_small)=\d+\(([\d.]+)%\)")

# longest prefix first: "S3FIFOForgiveEmbCache" must not normalize to "s3fifo"
POLICY_PREFIXES = [("S3FIFOForgiveEmbCache", "s3fifoforgive-embcache"),
                   ("LRUForgiveEmbCache", "lruforgiveembcache"),
                   ("S3FIFO", "s3fifo"), ("LRU", "lru")]


def normalize_policy(cache_name):
    for prefix, norm in POLICY_PREFIXES:
        if cache_name.startswith(prefix):
            return norm
    return cache_name.lower()


def read_wss(text):
    """(wss_obj, wss_byte, sample_ratio) from cachesim stderr text, or None."""
    m = None
    for m in WSS_RE.finditer(text):
        pass                                   # keep the last occurrence
    return (int(m.group(3)), int(m.group(4)), m.group(2) or "1") if m else None


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def parse_rows(raw_text, err_text, node, dataset, trace, sizes, pcts, params):
    """Result rows (lists of 11 strings) for one cachesim call. Raises ValueError
    when the output cannot be mapped to sizes safely."""
    sizes = [s.strip() for s in sizes.split(",") if s.strip()]
    pcts = [p.strip() for p in pcts.split(",") if p.strip()]
    if not sizes or len(sizes) != len(pcts):
        raise ValueError(f"sizes and pcts must be non-empty and the same length ({sizes} / {pcts})")
    wss = read_wss(err_text)
    exact = [(str(int(wss[1] * float(t))) if wss else "NA") if "." in t else t for t in sizes]

    rows = [m.groupdict() for m in (LINE_RE.match(l.strip()) for l in raw_text.splitlines()) if m]
    tag = f"{dataset}/{trace}"
    if not rows:
        raise ValueError(f"NO PARSABLE CELLS for {tag}")
    n = len(sizes)
    if len(rows) % n:
        raise ValueError(f"CELL COUNT {len(rows)} is not a multiple of {n} sizes for {tag}")
    # cross-check the positional mapping against the printed (truncated) sizes
    for b in range(0, len(rows), n):
        block = rows[b:b + n]
        if len({r["name"] for r in block}) != 1:
            raise ValueError(f"BLOCK {b // n} mixes cache names for {tag}")
        if all(e != "NA" for e in exact):
            for j in range(n - 1):
                big = int(exact[j]) >= int(exact[j + 1])
                pbig = int(block[j]["size"]) >= int(block[j + 1]["size"])
                if big != pbig and block[j]["size"] != block[j + 1]["size"]:
                    raise ValueError(f"SIZE ORDER MISMATCH in block {b // n} for {tag}")
    return [[node, dataset, trace, normalize_policy(r["name"]), r["name"], pcts[k % n],
             exact[k % n], r["req"], r["miss"], r["bmiss"] or r["miss"], params.replace(",", ";")]
            for k, r in enumerate(rows)]


def lookup_params(table, dataset, pct, policy):
    """Tuned --eviction-params for (dataset, pct, policy); '*' keys are fallbacks."""
    for key in (f"{dataset}|{pct}|{policy}", f"{dataset}|*|{policy}",
                f"*|{pct}|{policy}", f"*|*|{policy}"):
        if key in table:
            return table[key] or ""
    return ""


# ---------------------------------------------------------------- workloads
# The paper's workload types. A trace's workload comes from its dataset name
# (and, for the CMU "wiki" folder, the trace name: wiki_2016u = upload,
# wiki_2019t = text). First match wins. Workloads after the paper's eight are
# datasets the paper's table does not have. Tuning runs one study per workload
# and the analysis tables have one row per workload.
GROUPS = [
    ("Wiki Upload",   r"^wikimedia_.*upload", None),
    ("Wiki Upload",   r"(^|_)wiki($|_)", r"u$"),
    ("Wiki Text",     r"^wikimedia_.*text", None),
    ("Wiki Text",     r"(^|_)wiki($|_)", r"t$"),
    ("Meta KV",       r"metaKV", None),
    ("Tencent Photo", r"tencentPhoto", None),
    ("Tencent Block", r"tencentBlock", None),
    ("Meta CDN",      r"metaCDN", None),
    ("Alibaba Block", r"alibabaBlock", None),
    ("Twitter",       r"twitter", None),
    ("Meta Storage",  r"metaStorage", None),
    ("MSR",           r"(^|_)msr", None),
    ("CloudPhysics",  r"cloudphysics", None),
    ("FIU",           r"(^|_)fiu", None),
    ("SYSTOR",        r"systor", None),
]
PAPER = ["Wiki Upload", "Wiki Text", "Meta KV", "Tencent Photo", "Tencent Block",
         "Meta CDN", "Alibaba Block", "Twitter"]


def group_of(ds, trace):
    """Workload of a trace, or None for a dataset outside the list."""
    for label, dre, tre in GROUPS:
        if re.search(dre, ds) and (tre is None or re.search(tre, trace)):
            return label
    return None


# ---------------------------------------------------------------- families
#
# Some block datasets hold many near-clone volumes. The tuning planner marks a
# held-out trace as a NEAR COPY when it shares a family with a training trace,
# so results can be reported with and without them. A trace joins a family
# (same first key, the dataset or workload) when ALL hold against the
# family's running centroid:
#     requests within REQ_TOL, footprint within FP_TOL (both relative),
#     LRU miss at 10% within MISS_TOL (absolute).

REQ_TOL, FP_TOL, MISS_TOL = 0.03, 0.03, 0.02


def load_trace_stats(path, ref_pct="10", frac=0.1):
    """{(dataset, trace): {"requests", "footprint", "lru_miss"}} from LRU rows at ref_pct."""
    stats = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("policy") != "lru" or r.get("cache_pct") != ref_pct:
                continue
            try:
                stats[(r["dataset"], r["trace"])] = {
                    "requests": int(r["requests"]),
                    "footprint": float(r["size_bytes"]) / frac,
                    "lru_miss": float(r["miss"])}
            except (TypeError, ValueError):
                continue
    return stats


def families(stats):
    """{(dataset, trace): family_id}, singletons included. Centroid clustering:
    a trace joins a family only if it is close to the family's running mean, so
    a family cannot chain into a long continuum of slightly different traces."""
    by_ds = defaultdict(list)
    for key in stats:
        by_ds[key[0]].append(key)
    out = {}
    for ds, keys in by_ds.items():
        keys.sort(key=lambda k: (stats[k]["requests"], k[1]))
        fams = []
        for k in keys:
            s = stats[k]
            best, best_d = None, None
            for fm in reversed(fams):
                if fm["req"] * (1 + REQ_TOL) < s["requests"]:
                    break                      # sorted by requests: older ones are further away
                if abs(s["requests"] - fm["req"]) > REQ_TOL * fm["req"]:
                    continue
                if fm["fp"] > 0 and abs(s["footprint"] - fm["fp"]) > FP_TOL * fm["fp"]:
                    continue
                if abs(s["lru_miss"] - fm["miss"]) > MISS_TOL:
                    continue
                d = abs(s["requests"] / fm["req"] - 1) + abs(s["lru_miss"] - fm["miss"])
                if best_d is None or d < best_d:
                    best, best_d = fm, d
            if best is None:
                fams.append({"members": [k], "req": s["requests"],
                             "fp": s["footprint"], "miss": s["lru_miss"]})
            else:
                n = len(best["members"])
                best["req"] = (best["req"] * n + s["requests"]) / (n + 1)
                best["fp"] = (best["fp"] * n + s["footprint"]) / (n + 1)
                best["miss"] = (best["miss"] * n + s["lru_miss"]) / (n + 1)
                best["members"].append(k)
        members = sorted((sorted(fm["members"]) for fm in fams), key=lambda m: (-len(m), m[0][1]))
        for n, mem in enumerate(members):
            for key in mem:
                out[key] = f"{ds}#{n}"
    return out


# ---------------------------------------------------------------- command line

def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "header":
        print(COLUMNS)
    elif cmd == "wss":
        w = read_wss(read_text(argv[2]))
        if not w:
            sys.exit("# no working-set line found")
        print(f"{w[0]} {w[1]} {w[2]}")
    elif cmd == "lookup":
        with open(argv[2], encoding="utf-8") as f:
            print(lookup_params(json.load(f), argv[3], argv[4], argv[5]))
    elif cmd == "parse":
        raw, err, node, ds, tr, sizes, pcts, params = argv[2:10]
        try:
            rows = parse_rows(read_text(raw), read_text(err), node, ds, tr, sizes, pcts, params)
        except ValueError as e:
            sys.exit(f"# {e}")
        for r in rows:
            print(",".join(r))
        print(f"# {ds}/{tr}: {len(rows)} cells", file=sys.stderr)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
