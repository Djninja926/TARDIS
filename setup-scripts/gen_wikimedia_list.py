#!/usr/bin/env python3
"""
gen_wikimedia_list.py - list the Wikimedia 2019 caching traces (text + upload).

Source: https://analytics.wikimedia.org/published/datasets/caching/2019/
  text/    21 files (cache-t), ~100MB .gz each, 24 hours per file
  upload/  21 files (cache-u), ~1.5GB .gz each, 24 hours per file
Format and columns:
  https://wikitech.wikimedia.org/wiki/Data_Platform/Data_Lake/Traffic/Caching

The file names are read from the directory listing, not assumed. Output is the
same "<dataset>\\t<url>" format as full_list.tsv, with datasets
wikimedia_2019_text and wikimedia_2019_upload (the same names the worker
derives from the URL). The workers convert each file to oracleGeneral on the
node (convert_wikimedia.sh), so nothing has to be converted locally.

Usage:
  python gen_wikimedia_list.py --output wikimedia_list.tsv
  python gen_wikimedia_list.py --only text --output wm_text.tsv
"""
import argparse, re, sys, urllib.parse, urllib.request

BASE = "https://analytics.wikimedia.org/published/datasets/caching/2019/"
SUBDIRS = ("text", "upload")
HREF_RE = re.compile(r'href="([^"?#]+\.gz)"', re.I)

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from trace_names import dataset_of, trace_name   # noqa: E402


def list_dir(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tardis-trace-lister"})
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    files = sorted({urllib.parse.urljoin(url, h) for h in HREF_RE.findall(html)})
    # keep only files that live directly in this directory
    return [f for f in files if f.startswith(url) and "/" not in f[len(url):]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="wikimedia_list.tsv")
    ap.add_argument("--only", choices=SUBDIRS, help="just one of text / upload")
    args = ap.parse_args()

    rows = []
    for sub in ([args.only] if args.only else SUBDIRS):
        url = BASE + sub + "/"
        try:
            files = list_dir(url)
        except Exception as e:
            sys.exit(f"ERROR: could not list {url}: {e}")
        ds = dataset_of(url + "x.gz")
        print(f"# {ds}: {len(files)} traces", file=sys.stderr)
        if len(files) != 21:
            print(f"#   WARN: expected 21 daily files, found {len(files)}", file=sys.stderr)
        for f in files:
            rows.append((ds, f))

    names = [(dataset_of(u), trace_name(u)) for _, u in rows]
    if len(set(names)) != len(names):
        sys.exit("ERROR: two files map to the same dataset/name; check the listing")

    with open(args.output, "w", encoding="utf-8", newline="\n") as f:
        for ds, u in rows:
            f.write(f"{ds}\t{u}\n")
    print(f"# TOTAL: {len(rows)} traces -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
