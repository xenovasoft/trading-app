#!/usr/bin/env python
"""
Regression tests for the properties that make this system safe to trade:
no look-ahead, no repainting, determinism.

Run:  python test_no_lookahead.py [ASSET]

These are the tests that must never go red. If a future change makes an
indicator or structure call depend on bars that had not closed yet, the
backtest will look excellent and the live results will not match it.
"""

import sys

import numpy as np

import dataio
import indicators as ind
import smc

FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def run(asset="BTCUSD"):
    print(f"no-lookahead suite · {asset}")
    frames, reports, price, meta = dataio.load_asset(asset)

    for tf in ["1H", "15M"]:
        df = frames.get(tf)
        if df is None or len(df) < 200:
            continue
        print(f"\n[{tf}] {len(df)} closed bars "
              f"(forming bar dropped: {reports[tf]['dropped_forming_bar']})")
        cut = len(df) - 40

        # 1. A confirmed swing must not appear/disappear when future bars arrive.
        full_h, full_l = smc.swing_points(df)
        tr_h, tr_l = smc.swing_points(df.iloc[:cut])
        full_hs = {(i, round(v, 8)) for i, v, _ in full_h}
        full_ls = {(i, round(v, 8)) for i, v, _ in full_l}
        leak_h = [x for x in ((i, round(v, 8)) for i, v, _ in tr_h) if x not in full_hs]
        leak_l = [x for x in ((i, round(v, 8)) for i, v, _ in tr_l) if x not in full_ls]
        check("swing highs stable under truncation", not leak_h, str(leak_h[:3]))
        check("swing lows stable under truncation", not leak_l, str(leak_l[:3]))

        # 2. Indicator value at bar N must be identical with and without
        #    knowledge of bars N+1..  (this is what repainting violates)
        d_full, _ = ind.compute_all(df, tf)
        d_tr, _ = ind.compute_all(df.iloc[:cut], tf)
        for col in ["ema20", "ema50", "rsi14", "atr14", "macd", "adx14",
                    "bb_mid", "supertrend"]:
            if col not in d_full:
                continue
            a, b = d_full[col].iloc[cut - 1], d_tr[col].iloc[-1]
            same = (np.isnan(a) and np.isnan(b)) or abs(float(a) - float(b)) < 1e-8
            check(f"{col} identical at bar N", same, f"{a} vs {b}")

        # 3. Ichimoku spans must not be forward-shifted then read at [-1],
        #    which would read a value plotted into the future.
        _, _, sa, _ = ind.ichimoku(df)
        check("ichimoku senkou readable at last bar (not future-shifted)",
              not np.isnan(sa.iloc[-1]))

        # 4. Determinism
        atr_v = float(d_full["atr14"].iloc[-1])
        a1 = smc.bos_choch(df, full_h, full_l, atr_v)
        a2 = smc.bos_choch(df, full_h, full_l, atr_v)
        check("bos_choch deterministic", a1 == a2)
        s1 = smc.detect_sweeps(df, full_h, full_l, atr_v)
        s2 = smc.detect_sweeps(df, full_h, full_l, atr_v)
        check("sweep detection deterministic", s1 == s2)

        # 5. FVGs reported must genuinely be unfilled
        for g in smc.detect_fvgs(df, atr_v):
            check(f"fvg @{g['low']:.2f}-{g['high']:.2f} unfilled",
                  g["filled_fraction"] < 0.9, str(g["filled_fraction"]))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "BTCUSD"))
