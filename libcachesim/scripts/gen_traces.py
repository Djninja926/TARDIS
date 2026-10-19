#!/usr/bin/env python3
"""
gen_traces.py - build the trace list the sweep runs on.

Crawls the two public sources and writes one "<dataset>\\t<url>" line per trace:

  CMU PDL mirror   every *.oracleGeneral*.zst under cacheDatasets/, in every
                   subfolder (e.g. alibabaBlock/new/1M, twitter/sample10),
                   except folders named:
                     old   an older processing of the same Alibaba/Tencent volumes
                     stat  per-volume summary files, not request traces
  Wikimedia 2019   text/ and upload/ (one gzipped TSV per day; node_worker.sh
                   converts them to oracleGeneral on the node)

Dataset names follow common.dataset_of (the folder path with "/" -> "_").
The list we ran on (6,352 traces) is ../traces.tsv.

Usage:
  python gen_traces.py [--out traces.tsv]
"""
import argparse, re, sys, urllib.parse, urllib.request
from collections import Counter

from common import dataset_of

CMU = "https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets/"
WIKIMEDIA = "https://analytics.wikimedia.org/published/datasets/caching/2019/"
SKIP_DIRS = {"old", "stat"}

HREF_RE = re.compile(r'href="([^"?#]+)"', re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tardis-trace-lister"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")


def entries(url):
    """(subfolders, files) directly inside a directory listing."""
    dirs, files = [], []
    for h in HREF_RE.findall(fetch(url)):
        full = urllib.parse.urljoin(url, h)
        rest = full[len(url):] if full.startswith(url) else ""
        if not rest or "/" in rest.rstrip("/"):
            continue                           # parent link, sort links, deeper paths
        (dirs if rest.endswith("/") else files).append(rest)
    return sorted(set(dirs)), sorted(set(files))


def crawl_cmu(url=None):
    url = url or CMU
    dirs, files = entries(url)
    for f in files:
        if ".oracleGeneral" in f and f.endswith(".zst"):
            yield url + f
    for d in dirs:
        if d.rstrip("/") not in SKIP_DIRS:
            yield from crawl_cmu(url + d)


def crawl_wikimedia():
    for sub in ("text/", "upload/"):
        _, files = entries(WIKIMEDIA + sub)
        for f in files:
            if f.endswith(".gz"):
                yield WIKIMEDIA + sub + f


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="traces.tsv")
    args = ap.parse_args()

    try:
        urls = list(crawl_cmu()) + list(crawl_wikimedia())
    except OSError as e:
        sys.exit(f"ERROR: could not list a source: {e}")
    rows = [(dataset_of(u), u) for u in urls]
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for ds, u in rows:
            f.write(f"{ds}\t{u}\n")
    counts = Counter(ds for ds, _ in rows)
    for ds in sorted(counts, key=lambda d: -counts[d]):
        print(f"{ds:>28}  {counts[ds]:>5}")
    print(f"{'TOTAL':>28}  {len(rows):>5}  -> {args.out}")


if __name__ == "__main__":
    main()
