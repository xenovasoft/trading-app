#!/usr/bin/env python
"""
Historical replay of the v2 engine.

The point of this module is to answer the one question the rest of the system
cannot: does the engine's decision rule actually make money after costs, or
does it just produce plausible-looking commentary? Every other test here
checks correctness (no lookahead, deterministic structure). None of them check
edge.

Design commitments, because a backtest is only as honest as its assumptions:

  1. It replays through analysis.analyze_frames() — the SAME function the live
     loop calls. A reimplementation of the decision logic would be free to
     drift from production and would prove nothing about production.

  2. It steps every 15 minutes, matching the real GitHub Actions cron. The
     live system cannot react faster than that, so a backtest that evaluates
     every 5M bar would be crediting the strategy with reactions it can never
     have.

  3. Indicators are computed ONCE over full history and then sliced. This is
     sound only because every indicator is strictly causal, which
     test_no_lookahead.py asserts. Structure/liquidity (SMC) IS recomputed per
     step, since those depend on the visible window.

  4. Intrabar ambiguity resolves against the trade. When a single 5M bar's
     range contains both the stop and the target, the stop is assumed to have
     been hit first. OHLC cannot tell us the true sequence, and the optimistic
     assumption is how backtests manufacture fake edge.

  5. Costs are charged round-turn on every trade from config.COST. That figure
     is an ESTIMATE (spread is not in our OHLCV feed), so results are only as
     good as it is.

Known limitations, stated rather than buried:
  * Exits model TP1 only — the full position closes at the first target. TP2/TP3
    and partial scaling are not simulated, so this understates the upside of
    trades that ran and slightly overstates the frequency of clean wins.
  * Entries fill at the bar close plus slippage. Limit entries resting in a
    zone (the live WAIT state) are not simulated as resting orders.
  * BTC history is Binance BTCUSDT, not Kraken XBTUSD — see histdata.py.
"""

import argparse
import math

import numpy as np
import pandas as pd

import analysis
import config
import histdata
import indicators as ind

# One open position per (asset, profile), mirroring the live state machine.
MAX_HOLD_BARS_5M = {"SCALP": 48, "SWING": 1440}   # 4h, 5 days


def _slice(df, times, t):
    """Bars strictly at or before t. searchsorted keeps this O(log n)."""
    return df.iloc[:np.searchsorted(times, t, side="right")]


def simulate_exit(bars5, start_idx, direction, entry, stop, target, max_bars):
    """Walk 5M bars forward from start_idx and return (exit_price, reason, bars).

    Resolves an ambiguous bar (both levels touched) against the trade.
    """
    n = len(bars5)
    hi, lo = bars5["high"].values, bars5["low"].values
    end = min(n, start_idx + max_bars)
    for i in range(start_idx, end):
        if direction == "LONG":
            hit_stop, hit_tp = lo[i] <= stop, hi[i] >= target
        else:
            hit_stop, hit_tp = hi[i] >= stop, lo[i] <= target
        if hit_stop:                       # checked first: ambiguity loses
            return stop, "SL", i - start_idx + 1
        if hit_tp:
            return target, "TP", i - start_idx + 1
    if end <= start_idx:
        return None, "NO_DATA", 0
    return float(bars5["close"].values[end - 1]), "TIME", end - start_idx


def run(asset="BTCUSD", window_days=180, step_minutes=15, max_steps=None,
        verbose=True):
    frames = histdata.load_history(asset, window_days=window_days)

    # Indicators once over full history (causal -> slicing == recomputing).
    frames_i = {}
    for tf, df in frames.items():
        if len(df) >= 30:
            frames_i[tf], _ = ind.compute_all(df, tf)

    times = {tf: df["time"].values for tf, df in frames.items()}
    times_i = {tf: df["time"].values for tf, df in frames_i.items()}

    bars5 = frames["5M"]
    t5 = bars5["time"].values

    # Replay starts once the fast frames have enough depth to be meaningful.
    start_t = bars5["time"].iloc[500]
    end_t = bars5["time"].iloc[-1]
    timeline = pd.date_range(start_t, end_t, freq=f"{step_minutes}min")
    if max_steps:
        timeline = timeline[:max_steps]

    if verbose:
        print(f"\nreplaying {asset}: {timeline[0]} -> {timeline[-1]} "
              f"({len(timeline)} steps @ {step_minutes}min)\n")

    open_trade = {}          # profile -> dict
    trades = []
    skipped_while_open = 0

    for n, t in enumerate(timeline):
        tnp = np.datetime64(t)
        f_t = {tf: _slice(frames[tf], times[tf], tnp) for tf in frames}
        if len(f_t["5M"]) < 30:
            continue
        fi_t = {tf: _slice(frames_i[tf], times_i[tf], tnp) for tf in frames_i}
        price = float(f_t["5M"]["close"].iloc[-1])

        # Close out anything already open before considering a new entry.
        for profile in list(open_trade):
            tr = open_trade[profile]
            exit_px, reason, held = simulate_exit(
                bars5, tr["entry_idx"], tr["direction"], tr["entry"],
                tr["stop"], tr["target"], tr["max_bars"])
            if reason == "NO_DATA":
                continue
            # Only settle once the exit has actually occurred by time t.
            exit_idx = tr["entry_idx"] + held - 1
            if exit_idx >= len(t5) or t5[exit_idx] > tnp:
                continue
            move = (exit_px - tr["entry"]) if tr["direction"] == "LONG" \
                else (tr["entry"] - exit_px)
            cost = tr["entry"] * config.COST["round_turn_pct"]
            tr.update({
                "exit": exit_px, "exit_reason": reason, "bars_held": held,
                "exit_time": pd.Timestamp(t5[exit_idx]),
                "r_multiple": (move - cost) / tr["risk"] if tr["risk"] else 0.0,
                "net_move": move - cost,
            })
            trades.append(tr)
            del open_trade[profile]

        try:
            res = analysis.analyze_frames(asset, f_t, price, frames_i=fi_t,
                                          hard=[])
        except Exception as e:
            if verbose and n < 5:
                print(f"  step {t} failed: {type(e).__name__}: {e}")
            continue

        for profile, p in res["profiles"].items():
            if p.get("decision") not in ("LONG", "SHORT"):
                continue
            if profile in open_trade:
                skipped_while_open += 1
                continue
            s = p.get("setup") or {}
            tgts = s.get("targets") or []
            if not tgts or s.get("selected_stop") is None:
                continue

            direction = p["decision"]
            slip = price * config.COST["round_turn_pct"] / 2
            entry = price + slip if direction == "LONG" else price - slip
            stop, target = s["selected_stop"], tgts[0]["price"]
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            entry_idx = int(np.searchsorted(t5, tnp, side="right"))
            if entry_idx >= len(bars5):
                continue

            open_trade[profile] = {
                "asset": asset, "profile": profile, "direction": direction,
                "entry_time": pd.Timestamp(t), "entry": entry, "stop": stop,
                "target": target, "risk": risk, "entry_idx": entry_idx,
                "planned_rr": tgts[0]["rr"],
                "confluence": p.get("confluence_score"),
                "max_bars": MAX_HOLD_BARS_5M.get(profile, 288),
            }

        if verbose and n and n % 2000 == 0:
            print(f"  step {n}/{len(timeline)}  closed={len(trades)}  "
                  f"open={len(open_trade)}")

    return {"trades": trades, "steps": len(timeline),
            "skipped_while_open": skipped_while_open,
            "window": (timeline[0], timeline[-1])}


# ------------------------------------------------------------------ STATS ---

def stats(trades, label=""):
    if not trades:
        return {"label": label, "n": 0}
    r = np.array([t["r_multiple"] for t in trades], dtype=float)
    wins, losses = r[r > 0], r[r <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()

    equity, peak, max_dd = 1.0, 1.0, 0.0
    risk_frac = 0.01                      # fixed fractional 1% risk per trade
    for x in r:
        equity *= (1 + risk_frac * x)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    return {
        "label": label,
        "n": len(r),
        "win_rate": 100.0 * len(wins) / len(r),
        "expectancy_r": float(r.mean()),
        "median_r": float(np.median(r)),
        "total_r": float(r.sum()),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0
                         else float("inf"),
        "best_r": float(r.max()), "worst_r": float(r.min()),
        "std_r": float(r.std(ddof=1)) if len(r) > 1 else 0.0,
        "equity_mult": equity,
        "max_dd_pct": 100.0 * max_dd,
        "avg_bars": float(np.mean([t["bars_held"] for t in trades])),
    }


def _fmt(s):
    if not s["n"]:
        return f"{s['label']:<22} no trades"
    # t-stat on mean R: the honest guard against reading noise as edge.
    tstat = (s["expectancy_r"] / (s["std_r"] / math.sqrt(s["n"]))) \
        if s["std_r"] > 0 else 0.0
    return (
        f"{s['label']:<22} n={s['n']:<4d} win={s['win_rate']:5.1f}%  "
        f"expectancy={s['expectancy_r']:+.3f}R  PF={s['profit_factor']:.2f}  "
        f"totalR={s['total_r']:+.1f}  maxDD={s['max_dd_pct']:.1f}%  "
        f"t={tstat:+.2f}")


def report(result):
    trades = result["trades"]
    print("\n" + "=" * 78)
    print(f"BACKTEST RESULTS   {result['window'][0]} -> {result['window'][1]}")
    print("=" * 78)
    print(f"steps evaluated: {result['steps']}   "
          f"signals skipped (position already open): "
          f"{result['skipped_while_open']}")

    if not trades:
        print("\nNO TRADES TAKEN over the whole window. The engine's gates "
              "never all opened at once.\nThat is a finding in itself: the "
              "system as configured is effectively inert.")
        return

    print()
    print(_fmt(stats(trades, "ALL")))
    for profile in ("SWING", "SCALP"):
        sub = [t for t in trades if t["profile"] == profile]
        print(_fmt(stats(sub, profile)))
    for reason in ("TP", "SL", "TIME"):
        sub = [t for t in trades if t["exit_reason"] == reason]
        if sub:
            print(f"  exit={reason:<5} n={len(sub):<4d} "
                  f"avgR={np.mean([t['r_multiple'] for t in sub]):+.3f}")

    s = stats(trades, "ALL")
    print("\n" + "-" * 78)
    if s["n"] < 30:
        print(f"SAMPLE TOO SMALL ({s['n']} trades). Nothing here is "
              "statistically meaningful —\nan expectancy computed from this "
              "many trades is indistinguishable from noise.")
    else:
        tstat = s["expectancy_r"] / (s["std_r"] / math.sqrt(s["n"]))
        verdict = ("POSITIVE but NOT significant" if s["expectancy_r"] > 0
                   and abs(tstat) < 2 else
                   "POSITIVE and significant at ~95%" if s["expectancy_r"] > 0
                   else "NEGATIVE — no edge after costs")
        print(f"Expectancy {s['expectancy_r']:+.3f}R over {s['n']} trades, "
              f"t={tstat:+.2f}  =>  {verdict}")
        print("Note: a single window is not walk-forward validation, and "
              "these gates were\nnever re-optimised here — this measures the "
              "CURRENT config only.")
    print("-" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTCUSD")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--step", type=int, default=15)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--csv", default=None, help="write trade list to CSV")
    a = ap.parse_args()

    result = run(a.asset, window_days=a.days, step_minutes=a.step,
                 max_steps=a.max_steps)
    report(result)
    if a.csv and result["trades"]:
        pd.DataFrame(result["trades"]).to_csv(a.csv, index=False)
        print(f"\ntrades written to {a.csv}")


if __name__ == "__main__":
    main()
