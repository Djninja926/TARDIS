#!/usr/bin/env python3
"""
build_tuned_params.py - turn collected tuning results into the params file the
worker reads (PARAMS_FILE), and print a readable summary of every study.

Input : merged tuning CSV from  orchestrate.py --collect --run tune
Output: tuned_params.json   {"<dataset>|<pct>|<policy>": "lr=..,ctx-speed=..,threshold=.."}

A study keeps the DEFAULT params (no entry written) when:
  * it did not finish OK, or
  * the best trial beat the defaults on the training traces by less than
    --min-gain (in points of mean relative miss-ratio reduction; default 0).
Any (dataset, size, policy) without an entry runs with the policy defaults.

The summary flags studies whose best trial forgives far less often than the
default did ("mostly off"): the tuner may have "fixed" a negative result by
raising the threshold until forgiveness rarely fires. That is a legitimate
outcome but it should be reported as forgiveness being switched off, not as
forgiveness working.

Usage:
  python build_tuned_params.py tune_results\\merged_all_nodes.csv --out tuned_params.json
"""
import argparse, csv, json


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--out", default="tuned_params.json")
    ap.add_argument("--min-gain", type=float, default=0.0)
    ap.add_argument("--off-ratio", type=float, default=0.2,
                    help="flag 'mostly off' if best forgive rate < this x default rate")
    args = ap.parse_args()

    rows = {}
    for path in args.csv:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("study_id") and r["study_id"] != "study_id":
                    rows[r["study_id"]] = r

    table, kept_default = {}, 0
    print(f"{'study':>52} | {'n':>2} {'default':>8} {'best':>8} {'gain':>7} | "
          f"{'fr dflt':>7} {'fr best':>7} | params")
    for sid in sorted(rows):
        r = rows[sid]
        d, b, g = f(r.get("default_value")), f(r.get("best_value")), f(r.get("gain_pp_of_rel"))
        fd, fb = f(r.get("default_forgive_rate")), f(r.get("best_forgive_rate"))
        status = r.get("status", "")
        note = ""
        use = status.startswith("OK") and b is not None and r.get("best_params") \
            and (g is None or g > args.min_gain)
        if use:
            key = f"{r['dataset']}|{r['cache_pct']}|{r['policy']}"
            table[key] = r["best_params"].replace(";", ",")
            if fd is not None and fb is not None and fd > 0 and fb < args.off_ratio * fd:
                note = "  <- mostly off"
        else:
            kept_default += 1
            note = f"  (default kept: {status or 'no result'})" if not status.startswith("OK") \
                else "  (default kept: no gain)"
        fmt = lambda x, w: f"{x:>+{w}.3f}" if x is not None else f"{'--':>{w}}"
        fr = lambda x: f"{x:>6.2f}%" if x is not None else f"{'--':>7}"
        print(f"{sid:>52} | {r.get('n_train') or '-':>2} {fmt(d, 8)} {fmt(b, 8)} {fmt(g, 7)} | "
              f"{fr(fd)} {fr(fb)} | {r.get('best_params', '') if use else ''}{note}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=1, sort_keys=True)
    print(f"\n{len(table)} tuned entries -> {args.out}  ({kept_default} studies kept defaults)")
    print("values are mean relative miss-ratio reduction (%) on TRAINING traces; "
          "held-out results come from compare_tuned.py")


if __name__ == "__main__":
    main()
