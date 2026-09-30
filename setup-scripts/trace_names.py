#!/usr/bin/env python3
"""
trace_names.py - ONE rule for turning a trace URL into (dataset, trace name).

Every script that reads a trace list (analyzer, tuning planner, tuner,
comparison) uses this, and node_cachesim_worker.sh implements the same rule in
bash. If the rule ever changes, change it here AND in the worker.

  CMU mirror    .../cacheDatasets/alibabaBlock/new/1M/alibabaBlock_9.oracleGeneral.zst
                -> dataset "alibabaBlock_new_1M", name "alibabaBlock_9"
  Wikimedia     .../published/datasets/caching/2019/text/cache-t-00.gz
                -> dataset "wikimedia_2019_text", name "cache-t-00"

dataset = the path under the root, minus the filename, with "/" -> "_"
          (Wikimedia gets a "wikimedia_" prefix so it can never collide).
name    = the filename up to ".oracleGeneral"; for files that are not
          oracleGeneral (the Wikimedia TSVs), the filename with .gz / .zst /
          .tsv / .csv / .txt stripped.
"""

ROOTS = (("/cacheDatasets/", ""),
         ("/published/datasets/caching/", "wikimedia_"))
EXTS = (".gz", ".zst", ".tsv", ".csv", ".txt")


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
    """[(dataset, name, url)] from a list of '<dataset>\\t<url>' or bare-url lines.
    The dataset is always re-derived from the URL, so every script agrees."""
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = line.split("\t")[-1]
            out.append((dataset_of(url), trace_name(url), url))
    return out


if __name__ == "__main__":
    for u in ("https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets/alibabaBlock/new/1M/alibabaBlock_9.oracleGeneral.zst",
              "https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets/cloudphysics/w01.oracleGeneral.bin.zst",
              "https://analytics.wikimedia.org/published/datasets/caching/2019/text/cache-t-00.gz",
              "https://analytics.wikimedia.org/published/datasets/caching/2019/upload/cache-u-07.tsv.gz"):
        print(f"{dataset_of(u):>26}  {trace_name(u):<18} convert={needs_conversion(u)}")
