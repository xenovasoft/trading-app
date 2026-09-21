#!/usr/bin/env python
"""
A/B a config change against baseline, on the same bars, with a held-out slice.

WHY THIS EXISTS
The SCALP block in config.py records a change justified by "-30.8R -> +1.6R on
a 30-day out-of-sample slice". That is the right instinct and the right kind of
evidence. But it was produced by hand, once, and the numbers it cites are the
ones the live system then failed to reproduce. Re-running that comparison
should be one command, not a reconstruction, or the next config change gets
justified by argument instead of measurement.

WHAT IT DOES
Runs backtest.run() twice over the SAME cached history -- once with config as
committed, once with an override applied -- and prints both, split into a TUNE
slice and a VALIDATE slice that do not overlap.

THE RULE THIS ENFORCES
You are allowed to choose parameters on TUNE. VALIDATE is the number you
report. If you pick the variant that wins on VALIDATE, you have just used your
holdout as a tuning set and the split has told you nothing -- the output says
so rather than leaving it implicit.

WHAT IT CANNOT TELL YOU
Whether an edge exists. A variant that improves expectancy from -0.09R to
-0.02R is still losing. The verdict line reports the sign and the t-stat, not
an encouragement.

    python abtest.py --asset BTCUSD --days 180 \\
        --set 'PROFILES.SWING.min_confluence_score=55' \\
        --set 'PROFILES.SWING.allow_counter_htf=True'
"""

import argparse
import copy
import json
import math

import numpy as np
import pandas as pd

import backtest
import config


def _coerce(v):
    """'55' -> 55, 'True' -> True, '2.5' -> 2.5, else the string."""
    if v in ("True", "true"): return True
    if v in ("False", "false"): return False
    if v in ("None", "null"): return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def apply_overrides(pairs):
    """Apply 'A.B.C=value' overrides to the live config module.

    Returns the list of (path, old, new) actually changed, so the report can
    state what was tested rather than trusting the caller's description of it.
    A path that does not resolve is an error, not a silent no-op -- a typo'd
    override would otherwise 'prove' a change had no effect.
    """
    changed = []
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"override must be PATH=VALUE, got: {p}")
        path, raw = p.split("=", 1)
        parts = path.split(".")
        obj = getattr(config, parts[0], None)
        if obj is None:
            raise SystemExit(f"no such config attribute: {parts[0]}")
        for k in parts[1:-1]:
            if k not in obj:
                raise SystemExit(f"no such config key: {path} (missing {k})")
            obj = obj[k]
        leaf = parts[-1]
        if leaf not in obj:
            raise SystemExit(f"no such config key: {path} (missing {leaf})")
        old, new = obj[leaf], _coerce(raw)
        obj[leaf] = new
        changed.append((path, old, new))
    return changed


def snapshot_config():
    return {"PROFILES": copy.deepcopy(config.PROFILES),
            "COST": copy.deepcopy(config.COST),
            "ACCOUNT": copy.deepcopy(config.ACCOUNT)}


def restore_config(snap):
    config.PROFILES.clear(); config.PROFILES.update(snap["PROFILES"])
    config.COST.clear(); config.COST.update(snap["COST"])
    config.ACCOUNT.clear(); config.ACCOUNT.update(snap["ACCOUNT"])


def _summarise(trades, label):
    if not trades:
        return {"label": label, "n": 0, "swing": 0, "scalp": 0}
    r = np.array([t["r_multiple"] for t in trades], dtype=float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    t = r.mean() / (sd / math.sqrt(len(r))) if sd > 0 else 0.0
    cost_r = np.array([(t_["entry"] * config.COST["round_turn_pct"]) / t_["risk"]
                       for t_ in trades if t_.get("risk")], dtype=float)
    risk_pct = np.array([100 * t_["risk"] / t_["entry"] for t_ in trades
                         if t_.get("entry")], dtype=float)
    return {
        "label": label,
        "n": len(r),
        "swing": sum(1 for x in trades if x["profile"] == "SWING"),
        "scalp": sum(1 for x in trades if x["profile"] == "SCALP"),
        "expectancy_r": float(r.mean()),
        "t": float(t),
        "win_rate": float(100 * (r > 0).mean()),
        "total_r": float(r.sum()),
        "profit_factor": (float(r[r > 0].sum() / -r[r <= 0].sum())
                          if (r <= 0).any() and r[r <= 0].sum() < 0 else float("inf")),
        "cost_drag_r": float(cost_r.mean()) if len(cost_r) else None,
        "median_stop_pct": float(np.median(risk_pct)) if len(risk_pct) else None,
    }


def _fmt(s):
    if not s["n"]:
        return f"  {s['label']:<22} no trades"
    return (f"  {s['label']:<22} n={s['n']:<4d} "
            f"(sw {s['swing']:<3d} sc {s['scalp']:<4d}) "
            f"exp={s['expectancy_r']:+.4f}R  t={s['t']:+.2f}  "
            f"PF={s['profit_factor']:.2f}  "
            f"cost={s['cost_drag_r']:.3f}R  stop={s['median_stop_pct']:.2f}%")


def run_variant(asset, days, label, overrides, tune_end, verbose=False):
    changed = []
    snap = snapshot_config()
    try:
        if overrides:
            changed = apply_overrides(overrides)
        if verbose:
            print(f"  [{label}] replaying...", flush=True)
        res = backtest.run(asset, window_days=days, verbose=verbose)
        trades = res["trades"]
        tune = [t for t in trades if pd.Timestamp(t["entry_time"]) < tune_end]
        val = [t for t in trades if pd.Timestamp(t["entry_time"]) >= tune_end]
        return {
            "label": label,
            "changed": changed,
            "all": _summarise(trades, f"{label} ALL"),
            "tune": _summarise(tune, f"{label} TUNE"),
            "validate": _summarise(val, f"{label} VALIDATE"),
        }
    finally:
        restore_config(snap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTCUSD")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--set", action="append", default=[], dest="overrides",
                    help="PATH=VALUE, e.g. PROFILES.SWING.min_confluence_score=55")
    ap.add_argument("--tune-frac", type=float, default=0.7,
                    help="fraction of the window used for tuning (rest is holdout)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", dest="progress", action="store_false",
                    default=True, help="suppress per-step replay progress")
    a = ap.parse_args()

    if not a.overrides:
        raise SystemExit("nothing to test: pass at least one --set PATH=VALUE")

    # Validate the overrides BEFORE fetching history. A typo'd path is a
    # SystemExit either way, but discovering it after a multi-minute data load
    # wastes the run -- and worse, invites re-running with the typo still in
    # place because the failure looked like a data problem.
    _snap = snapshot_config()
    try:
        apply_overrides(a.overrides)
    finally:
        restore_config(_snap)

    frames = backtest.histdata.load_history(a.asset, window_days=a.days)
    t5 = frames["5M"]["time"]
    start, end = pd.Timestamp(t5.iloc[0]), pd.Timestamp(t5.iloc[-1])
    tune_end = start + (end - start) * a.tune_frac

    print(f"\n{'='*84}")
    print(f"A/B  {a.asset}  {a.days}d   tune < {tune_end:%Y-%m-%d} <= validate")
    print("=" * 84)

    # Progress on by default: a silent multi-hour run is indistinguishable
    # from a hung one, and the first version of this file had exactly that
    # failure -- 7.5 hours with no way to tell 10% done from 90%.
    base = run_variant(a.asset, a.days, "baseline", [], tune_end, a.progress)
    var = run_variant(a.asset, a.days, "variant", a.overrides, tune_end, a.progress)

    print("\noverrides applied:")
    for path, old, new in var["changed"]:
        print(f"  {path}: {old!r} -> {new!r}")

    for slice_name in ("tune", "validate", "all"):
        print(f"\n[{slice_name.upper()}]")
        print(_fmt(base[slice_name]))
        print(_fmt(var[slice_name]))

    bv, vv = base["validate"], var["validate"]
    print("\n" + "-" * 84)
    if not vv["n"] or not bv["n"]:
        print("VALIDATE slice has no trades for one arm -- the comparison is empty.")
    else:
        d = vv["expectancy_r"] - bv["expectancy_r"]
        print(f"VALIDATE expectancy  baseline {bv['expectancy_r']:+.4f}R  ->  "
              f"variant {vv['expectancy_r']:+.4f}R   ({d:+.4f}R)")
        print(f"VALIDATE SWING count baseline {bv['swing']}  ->  variant {vv['swing']}")
        if vv["cost_drag_r"] and bv["cost_drag_r"]:
            print(f"VALIDATE cost drag   baseline {bv['cost_drag_r']:.3f}R  ->  "
                  f"variant {vv['cost_drag_r']:.3f}R")
        if vv["expectancy_r"] <= 0:
            print("\nVariant is still LOSING on the holdout. A smaller loss is not an "
                  "edge:\nthis says the change did not make the strategy profitable, "
                  "whatever it did\nto the trade count.")
        elif abs(vv["t"]) < 2:
            print(f"\nVariant is positive on the holdout but t={vv['t']:+.2f} -- not "
                  "significant.\nWith n={} that is consistent with luck.".format(vv["n"]))
        else:
            print(f"\nVariant is positive on the holdout at t={vv['t']:+.2f}. That is "
                  "the strongest\nresult this harness can produce -- it still needs a "
                  "forward test before capital.")
        print("\nIf you now try other values and pick whichever wins on VALIDATE, the "
              "holdout\nhas become a tuning set and this number no longer means what "
              "it says.")
    print("-" * 84)

    if a.json:
        print(json.dumps({"baseline": base, "variant": var}, indent=2, default=str))


if __name__ == "__main__":
    main()
