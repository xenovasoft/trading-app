#!/usr/bin/env python
"""
Edge lab: does any of this actually predict anything?

The previous audit found the live engine's confluence score correlated +0.024
with trade outcome (n=451) and that gross expectancy before costs was +0.059R
at t=+0.77 -- statistically indistinguishable from zero. The detectors were
never the untested part; the CLAIM that stacking them produces signal was.

This module tests each model in isolation against forward returns, so
"accurate" becomes a measured number instead of an assertion. It deliberately
does NOT produce trade signals. Its only output is a ranked table of which
models carry information and which are decoration.

Three properties it was built to have, because their absence is what made the
old backtest unfalsifiable:

  1. STRICT CAUSALITY, MECHANICALLY CHECKED. Every feature at bar i uses bars
     <= i. Events whose definition needs later bars to confirm (a sweep is not
     a sweep until price reclaims) are stamped at the CONFIRMATION bar, not
     the bar the wick printed. --selftest asserts this by truncation.

  2. MULTIPLE-TESTING CORRECTION. Testing 30 models at p<0.05 yields ~1.5
     "discoveries" from pure noise. Benjamini-Hochberg FDR is applied across
     every test in the run and the raw-vs-corrected verdicts are both shown.

  3. A RANDOM BASELINE IN THE SAME TABLE. Shuffled-event rows are scored
     identically. If a real model does not clear the synthetic ones, the
     ranking is measuring variance, not edge.

Forward returns are expressed in ATR units, which is what makes them
comparable to R-multiples: a 1.0 ATR move with a 1.0 ATR stop is 1R.

Usage:
    python edgelab.py --asset XAUUSD --days 365
    python edgelab.py --asset BTCUSD --days 365 --tf 15M --horizons 12,48
    python edgelab.py --selftest
"""

import argparse
import math

import numpy as np
import pandas as pd

import config
import histdata
import indicators as ind
import volume as vol


# --------------------------------------------------------------- STATISTICS --

def _norm_cdf(x):
    """Standard normal CDF via erf. Avoids a scipy dependency (not installed
    in this repo's venv) for what is one function call."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _ttest_1samp(x):
    """(mean, t, two-sided p) against a null of zero mean.

    Normal approximation for p: every sample here is in the hundreds or
    thousands, where the t and normal tails agree to well past the precision
    that matters for a screening decision.
    """
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return (float(x.mean()) if n else 0.0), 0.0, 1.0
    sd = x.std(ddof=1)
    if sd <= 0:
        return float(x.mean()), 0.0, 1.0
    t = x.mean() / (sd / math.sqrt(n))
    return float(x.mean()), float(t), float(2.0 * (1.0 - _norm_cdf(abs(t))))


def benjamini_hochberg(pvals, q=0.10):
    """Return a boolean array: which hypotheses survive at FDR level q.

    BH rather than Bonferroni: with ~30 correlated models Bonferroni is so
    conservative it would reject a real effect, and the goal here is to
    control the share of false discoveries, not to never make one.
    """
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(p)
    ranked = p[order]
    thresh = q * (np.arange(1, n + 1) / n)
    passed = ranked <= thresh
    keep = np.zeros(n, dtype=bool)
    if passed.any():
        cutoff = np.max(np.flatnonzero(passed))
        keep[order[:cutoff + 1]] = True
    return keep


# ------------------------------------------------------------- CAUSAL SMC ----

def causal_swing_levels(df, left=None, right=None):
    """Most recent CONFIRMED swing high/low level as known at each bar.

    The distinction that matters: a fractal pivot at bar i is not knowable
    until bar i+right has closed. smc.swing_points() returns pivots by their
    pivot index; using that index directly as "known at i" would hand the lab
    `right` bars of foresight on every structural level. Here each pivot is
    published at i+right.
    """
    left = left or config.STRUCTURE["swing_left"]
    right = right or config.STRUCTURE["swing_right"]
    h, l = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    n = len(h)
    win = left + right + 1
    hi_lvl = np.full(n, np.nan)
    lo_lvl = np.full(n, np.nan)
    if n < win:
        return hi_lvl, lo_lvl

    sw_h = np.lib.stride_tricks.sliding_window_view(h, win)
    sw_l = np.lib.stride_tricks.sliding_window_view(l, win)
    hi_idx = np.flatnonzero(sw_h.argmax(axis=1) == left) + left
    lo_idx = np.flatnonzero(sw_l.argmin(axis=1) == left) + left

    for idx in hi_idx:                      # published at idx+right
        pub = idx + right
        if pub < n:
            hi_lvl[pub] = h[idx]
    for idx in lo_idx:
        pub = idx + right
        if pub < n:
            lo_lvl[pub] = l[idx]
    # Forward-fill: "the most recent level known as of bar i".
    hi_lvl = pd.Series(hi_lvl).ffill().to_numpy()
    lo_lvl = pd.Series(lo_lvl).ffill().to_numpy()
    return hi_lvl, lo_lvl


def sweep_events(df, hi_lvl, lo_lvl, reclaim=None):
    """Liquidity sweeps, stamped at the bar the reclaim CONFIRMS.

    Mirrors smc.detect_sweeps' definition (trade beyond a prior swing extreme,
    then close back inside within `reclaim` bars) but returns per-bar boolean
    arrays indexed by confirmation bar, which is the only index at which the
    event is tradeable. smc.detect_sweeps reports `bars_ago` from the wick;
    that is right for display and wrong for measurement.
    """
    reclaim = reclaim or config.LIQUIDITY["sweep_reclaim_bars"]
    h, l, c = (df["high"].to_numpy(dtype=float),
               df["low"].to_numpy(dtype=float),
               df["close"].to_numpy(dtype=float))
    n = len(df)
    bear = np.zeros(n, dtype=bool)   # swept highs then rejected -> short bias
    bull = np.zeros(n, dtype=bool)

    for i in range(n):
        lvl = hi_lvl[i]
        if np.isfinite(lvl) and h[i] > lvl:
            for k in range(i, min(n, i + reclaim + 1)):
                if c[k] < lvl:
                    bear[k] = True
                    break
        lvl = lo_lvl[i]
        if np.isfinite(lvl) and l[i] < lvl:
            for k in range(i, min(n, i + reclaim + 1)):
                if c[k] > lvl:
                    bull[k] = True
                    break
    return bear, bull


def bos_events(df, hi_lvl, lo_lvl, atr_arr):
    """Break of structure: close clears the last confirmed swing by
    bos_displacement_atr * ATR. Known at the breaking bar itself, so no
    confirmation lag applies."""
    c = df["close"].to_numpy(dtype=float)
    buf = config.STRUCTURE["bos_displacement_atr"] * np.asarray(atr_arr, float)
    up = np.isfinite(hi_lvl) & np.isfinite(buf) & (c > hi_lvl + buf)
    dn = np.isfinite(lo_lvl) & np.isfinite(buf) & (c < lo_lvl - buf)
    return up, dn


def fvg_events(df, atr_arr, min_size_atr=None):
    """Fair value gap printed by the 3-bar pattern ending at bar i.

    Stamped at i, the first bar at which the gap is fully observable: the
    pattern is bars i-2, i-1, i and needs no future bar. Direction is the
    displacement direction.
    """
    min_size_atr = min_size_atr or config.LIQUIDITY["fvg_min_size_atr"]
    h, l = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    a = np.asarray(atr_arr, dtype=float)
    n = len(df)
    up = np.zeros(n, dtype=bool)
    dn = np.zeros(n, dtype=bool)
    if n < 3:
        return up, dn
    # Bullish FVG: low[i] > high[i-2]  (gap between bar i-2 high and bar i low)
    gap_up = l[2:] - h[:-2]
    gap_dn = l[:-2] - h[2:]
    size_ok_up = np.isfinite(a[2:]) & (gap_up > min_size_atr * a[2:])
    size_ok_dn = np.isfinite(a[2:]) & (gap_dn > min_size_atr * a[2:])
    up[2:] = (gap_up > 0) & size_ok_up
    dn[2:] = (gap_dn > 0) & size_ok_dn
    return up, dn


# ------------------------------------------------------------- FORWARD LABEL --

def forward_labels(df, atr_arr, horizon):
    """Forward return over `horizon` bars, in ATR units, plus MFE/MAE.

    ATR normalisation is what makes these comparable across assets and across
    volatility regimes -- a 20-point gold move means something different in a
    quiet week than a violent one, and an unnormalised mean return is
    dominated by whichever period happened to be volatile.

    The last `horizon` bars get nan (no future to measure), which is correct
    and is why every test below reports its own n.
    """
    c = df["close"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    a = np.asarray(atr_arr, dtype=float)
    n = len(c)

    fwd = np.full(n, np.nan)
    mfe = np.full(n, np.nan)
    mae = np.full(n, np.nan)
    if n <= horizon:
        return fwd, mfe, mae

    fwd[:-horizon] = c[horizon:] - c[:-horizon]
    # Rolling forward max/min over the horizon window.
    hh = pd.Series(h).rolling(horizon).max().shift(-horizon).to_numpy()
    ll = pd.Series(l).rolling(horizon).min().shift(-horizon).to_numpy()
    mfe[:-horizon] = (hh - c)[:-horizon]
    mae[:-horizon] = (c - ll)[:-horizon]

    ok = np.isfinite(a) & (a > 0)
    for arr in (fwd, mfe, mae):
        arr[~ok] = np.nan
    fwd = np.where(ok, fwd / np.where(ok, a, 1), np.nan)
    mfe = np.where(ok, mfe / np.where(ok, a, 1), np.nan)
    mae = np.where(ok, mae / np.where(ok, a, 1), np.nan)
    return fwd, mfe, mae


# ------------------------------------------------------------------ TESTING --

def _welch(a, b, overlap=1):
    """Welch two-sample t-test with an OVERLAP correction.

    Two corrections, both of which the first version of this file got wrong
    and both of which flip the conclusions:

    1. TWO-SAMPLE, not one-sample-against-zero. Gold drifted up ~0.39 ATR per
       48-bar window over this period. Against a null of zero, every long
       "works" and every short "fails" -- you would be measuring the bull
       market, not the model. The null that matters is the OTHER bars: does
       this event select better-than-average bars?

    2. OVERLAP. Forward returns on adjacent bars share most of their window,
       so 35,000 bars at a 48-bar horizon carry ~730 independent observations,
       not 35,000. Ignoring that inflates every t-stat by ~sqrt(48) = 6.9x and
       manufactures significance out of nothing. Effective n = n / overlap.
    """
    a = np.asarray(a, float); a = a[np.isfinite(a)]
    b = np.asarray(b, float); b = b[np.isfinite(b)]
    na_eff = max(2.0, len(a) / max(1, overlap))
    nb_eff = max(2.0, len(b) / max(1, overlap))
    if len(a) < 3 or len(b) < 3:
        return 0.0, 0.0, 1.0
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = math.sqrt(va / na_eff + vb / nb_eff)
    diff = a.mean() - b.mean()
    if se <= 0:
        return float(diff), 0.0, 1.0
    t = diff / se
    return float(diff), float(t), float(2.0 * (1.0 - _norm_cdf(abs(t))))


def _test_event(name, mask, fwd, direction, cost_atr, overlap=1,
                rng=None, shuffle=False):
    """Score one boolean event as EXCESS return over the non-event bars.

    `mean_atr` in the output is excess-over-baseline net of cost, i.e. what
    the event adds beyond simply being in the market in that direction. A
    model that merely rides the prevailing drift scores ~0 here, which is the
    correct answer for it.
    """
    m = np.asarray(mask, dtype=bool).copy()
    if shuffle and rng is not None:
        rng.shuffle(m)
    fin = np.isfinite(fwd)
    sel = m & fin
    rest = (~m) & fin
    n = int(sel.sum())
    if n < 30 or int(rest.sum()) < 30:
        return None
    sig = direction * fwd[sel]
    base = direction * fwd[rest]
    excess, t, p = _welch(sig, base, overlap=overlap)
    return {"model": name, "n": n,
            "mean_atr": excess - cost_atr,
            "raw_atr": float(sig.mean()) - cost_atr,
            "base_atr": float(base.mean()),
            "t": t, "p": p,
            "hit": float((sig > 0).mean() * 100.0)}


def _test_quantile(name, feature, fwd, direction, cost_atr, overlap=1,
                   top_q=0.90):
    """Score the top decile of a continuous feature against the rest."""
    f = np.asarray(feature, dtype=float)
    ok = np.isfinite(f) & np.isfinite(fwd)
    if ok.sum() < 100:
        return None
    thresh = np.quantile(f[ok], top_q)
    return _test_event(name, ok & (f >= thresh), fwd, direction, cost_atr,
                       overlap=overlap)


def run(asset="XAUUSD", days=365, tf="15M", horizons=(12, 48), fdr_q=0.10,
        cost_pct=None, verbose=True):
    cost_pct = config.COST["round_turn_pct"] if cost_pct is None else cost_pct

    frames = histdata.load_history(asset, window_days=days)
    if tf not in frames:
        raise SystemExit(f"timeframe {tf} not in fetched frames: {list(frames)}")
    df = frames[tf].reset_index(drop=True)
    if len(df) < 500:
        raise SystemExit(f"only {len(df)} bars of {tf}; need >=500 to test")

    di, _ = ind.compute_all(df, tf)
    atr_arr = di["atr14"].to_numpy(dtype=float)
    obv_arr = di["obv"].to_numpy(dtype=float)

    zero_vol = int((df["volume"] <= 0).sum())
    if verbose:
        print(f"\n{'='*82}\nEDGE LAB   {asset} {tf}   {df['time'].iloc[0]} -> "
              f"{df['time'].iloc[-1]}\n{'='*82}")
        print(f"bars={len(df)}   zero-volume bars={zero_vol} "
              f"({100*zero_vol/len(df):.1f}%)")
        if asset == "XAUUSD":
            print("NOTE: gold volume here is PAXG token flow (~$20-30M/day), "
                  "not COMEX\n      gold volume (>$100B/day). Price tracks "
                  "spot; volume does not.")

    vf = vol.compute_all(df, obv_series=obv_arr)
    hi_lvl, lo_lvl = causal_swing_levels(df)
    sw_bear, sw_bull = sweep_events(df, hi_lvl, lo_lvl)
    bos_up, bos_dn = bos_events(df, hi_lvl, lo_lvl, atr_arr)
    fvg_up, fvg_dn = fvg_events(df, atr_arr)

    if verbose:
        print(f"events: sweeps {sw_bull.sum()} bull / {sw_bear.sum()} bear   "
              f"BOS {bos_up.sum()}/{bos_dn.sum()}   "
              f"FVG {fvg_up.sum()}/{fvg_dn.sum()}   "
              f"climax {vf['climax_bull'].sum()}/{vf['climax_bear'].sum()}")

    rng = np.random.default_rng(0)
    all_rows = []

    for hz in horizons:
        fwd, mfe, mae = forward_labels(df, atr_arr, hz)
        # Round-turn cost in ATR units, bar by bar, then a scalar median.
        cost_atr = float(np.nanmedian(
            (df["close"].to_numpy(dtype=float) * cost_pct) /
            np.where(np.isfinite(atr_arr) & (atr_arr > 0), atr_arr, np.nan)))

        tests = [
            # --- liquidity / structure models
            ("sweep_bullish", sw_bull, +1), ("sweep_bearish", sw_bear, -1),
            ("bos_up", bos_up, +1), ("bos_down", bos_dn, -1),
            ("fvg_up", fvg_up, +1), ("fvg_down", fvg_dn, -1),
            # --- volume models
            ("vol_climax_bull", vf["climax_bull"], +1),
            ("vol_climax_bear", vf["climax_bear"], -1),
            ("obv_div_bull", vf["obv_div_bull"], +1),
            ("obv_div_bear", vf["obv_div_bear"], -1),
            ("vol_dryup_long", vf["dryup"], +1),
            ("vol_dryup_short", vf["dryup"], -1),
        ]
        rows = []
        for name, mask, d in tests:
            r = _test_event(name, mask, fwd, d, cost_atr, overlap=hz)
            if r:
                rows.append(r)

        for name, feat, d in (
            ("rvol_top10_long", vf["rvol"], +1),
            ("rvol_top10_short", vf["rvol"], -1),
            ("volz_top10_long", vf["volume_z"], +1),
            ("absorption_long", -vf["effort_result"], +1),   # low range/vol
            ("cumdelta_top10_long", vf["cum_delta"], +1),
            ("cumdelta_bot10_short", -vf["cum_delta"], -1),
            ("vwapz_high_short", vf["vwap_z"], -1),          # stretched -> fade
            ("vwapz_low_long", -vf["vwap_z"], +1),
            ("above_vah_short", vf["va_position"], -1),
            ("below_val_long", -vf["va_position"], +1),
        ):
            r = _test_quantile(name, feat, fwd, d, cost_atr, overlap=hz)
            if r:
                rows.append(r)

        # Synthetic controls: same machinery, no information.
        for i, (src, d) in enumerate(((sw_bull, +1), (vf["climax_bear"], -1))):
            r = _test_event(f"[random_control_{i+1}]", src, fwd, d, cost_atr,
                            overlap=hz, rng=rng, shuffle=True)
            if r:
                rows.append(r)
        r = _test_event("[baseline_all_bars]", np.ones(len(df), dtype=bool),
                        fwd, +1, cost_atr)
        if r:
            rows.append(r)

        for r in rows:
            r["horizon"] = hz
        all_rows.extend(rows)

    if not all_rows:
        print("\nno model produced >=30 usable observations.")
        return []

    # FDR across EVERY test in the run -- all horizons, all models.
    real = [r for r in all_rows if not r["model"].startswith("[")]
    # One-sided: only a POSITIVE excess can be a discovery. The two-sided
    # version of this flagged models that significantly LOSE money as PASS,
    # which is the single most dangerous thing this table could do.
    keep = benjamini_hochberg([r["p"] for r in real], q=fdr_q)
    for r, k in zip(real, keep):
        r["survives_fdr"] = bool(k) and r["mean_atr"] > 0
    for r in all_rows:
        r.setdefault("survives_fdr", False)

    if verbose:
        _report(all_rows, horizons, fdr_q, cost_atr)
    return all_rows


def _report(rows, horizons, fdr_q, cost_atr):
    print(f"\nround-turn cost applied: {cost_atr:.3f} ATR per trade "
          f"(subtracted from every mean below)")
    for hz in horizons:
        sub = sorted([r for r in rows if r["horizon"] == hz],
                     key=lambda r: -r["t"])
        print(f"\n--- horizon {hz} bars "
              f"{'-'*(64 - len(str(hz)))}")
        print(f"{'model':<24}{'n':>6}{'mean(ATR)':>12}{'hit%':>8}"
              f"{'t':>8}{'p':>9}  FDR")
        for r in sub:
            flag = "PASS" if r["survives_fdr"] else ""
            print(f"{r['model']:<24}{r['n']:>6}{r['mean_atr']:>+12.4f}"
                  f"{r['hit']:>8.1f}{r['t']:>+8.2f}{r['p']:>9.3f}  {flag}")

    real = [r for r in rows if not r["model"].startswith("[")]
    surv = [r for r in real if r["survives_fdr"]]
    ctrl = [r for r in rows if r["model"].startswith("[random_control")]

    print("\n" + "=" * 82)
    print(f"{len(real)} models tested across {len(horizons)} horizons. "
          f"{len(surv)} survive BH-FDR at q={fdr_q}.")
    if ctrl:
        best_ctrl = max(abs(r["t"]) for r in ctrl)
        print(f"best |t| achieved by a SHUFFLED control: {best_ctrl:.2f} "
              "-- any real model below this is noise.")
    if not surv:
        print("\nNo model carries information that survives multiple-testing\n"
              "correction at these horizons. That is a real result, not a bug:\n"
              "it means these detectors describe the chart rather than predict\n"
              "it, which is consistent with the live engine's +0.024 confluence\n"
              "correlation. Do NOT trade any of them on this evidence.")
    else:
        print("\nSurviving models (still require out-of-sample confirmation on\n"
              "a period this run did not touch before any capital is risked):")
        for r in sorted(surv, key=lambda r: -abs(r["t"])):
            print(f"  {r['model']:<24} hz={r['horizon']:<4} "
                  f"mean={r['mean_atr']:+.4f} ATR  t={r['t']:+.2f}  n={r['n']}")
    print("=" * 82)


# ----------------------------------------------------------------- SELFTEST --

def selftest():
    """Assert causality by truncation: a feature computed on bars 0..N must be
    bit-identical whether or not bars N+1.. exist. This is the check whose
    absence let the old system's claims go unexamined."""
    rng = np.random.default_rng(7)
    n = 900
    px = 4000 + np.cumsum(rng.normal(0, 4, n))
    df = pd.DataFrame({
        "time": pd.date_range("2025-01-01", periods=n, freq="15min"),
        "open": px + rng.normal(0, 1, n),
        "high": px + np.abs(rng.normal(0, 5, n)),
        "low": px - np.abs(rng.normal(0, 5, n)),
        "close": px,
        "volume": np.abs(rng.normal(1000, 400, n)),
    })
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)

    cut = 700
    full, trunc = df, df.iloc[:cut].reset_index(drop=True)
    fails = []

    vf_f = vol.compute_all(full)
    vf_t = vol.compute_all(trunc)
    for k in vf_f:
        a = np.asarray(vf_f[k], dtype=float)[:cut]
        b = np.asarray(vf_t[k], dtype=float)
        bad = ~(np.isclose(a, b, equal_nan=True))
        # rolling_value_area needs `window` bars; both sides nan there alike.
        if bad.any():
            fails.append(f"volume.{k}: {int(bad.sum())} bars differ")
        else:
            print(f"  PASS  volume.{k}")

    hf, lf = causal_swing_levels(full)
    ht, lt = causal_swing_levels(trunc)
    for nm, a, b in (("swing_high", hf[:cut], ht), ("swing_low", lf[:cut], lt)):
        if not np.isclose(a, b, equal_nan=True).all():
            fails.append(f"{nm} differs under truncation")
        else:
            print(f"  PASS  causal_{nm}")

    sbf, sblf = sweep_events(full, hf, lf)
    sbt, sblt = sweep_events(trunc, ht, lt)
    # The final `reclaim` bars of the truncated frame legitimately lack their
    # confirmation window, so compare only bars that had room to confirm.
    edge = config.LIQUIDITY["sweep_reclaim_bars"]
    for nm, a, b in (("sweep_bear", sbf[:cut - edge], sbt[:cut - edge]),
                     ("sweep_bull", sblf[:cut - edge], sblt[:cut - edge])):
        if not (a == b).all():
            fails.append(f"{nm} differs under truncation "
                         f"({int((a != b).sum())} bars)")
        else:
            print(f"  PASS  {nm} confirmation-stamped causally")

    print()
    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        raise SystemExit(1)
    print("all causality checks passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="XAUUSD")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--tf", default="15M")
    ap.add_argument("--horizons", default="12,48")
    ap.add_argument("--fdr", type=float, default=0.10)
    ap.add_argument("--cost", type=float, default=None,
                    help="round-turn cost as a fraction, e.g. 0.0008")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        return

    rows = run(a.asset, days=a.days, tf=a.tf,
               horizons=tuple(int(x) for x in a.horizons.split(",")),
               fdr_q=a.fdr, cost_pct=a.cost)
    if a.csv and rows:
        pd.DataFrame(rows).to_csv(a.csv, index=False)
        print(f"\nwritten to {a.csv}")


if __name__ == "__main__":
    main()
