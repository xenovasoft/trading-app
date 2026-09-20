#!/usr/bin/env python
"""
Paper-trading forward test with an automatic kill switch.

WHAT THIS IS FOR
The backtest said -0.092R over 451 trades and edgelab.py found 0 of 44 models
carrying information. Those are both statements about the PAST, measured by
code that could in principle be wrong. A forward test is the one check that
cannot be curve-fitted: it runs on bars nobody has seen, using the engine
exactly as deployed, and either reproduces the backtest's number or exposes
the backtest as broken.

Either outcome is worth having. If forward matches backtest, the measurement
is trustworthy and the conclusion (don't trade this) stands on two legs. If
forward diverges sharply, the backtest has a bug and every number in the audit
needs re-deriving.

PAPER ONLY, STRUCTURALLY
There is no order-placement path in this module. No broker client, no API key
lookup, no HTTP POST to anything but the state store. "Paper only" is not a
flag that could be flipped by a config change or a stray argument -- the code
to send a real order does not exist here. That is deliberate: a kill switch
that can fail open is not a kill switch.

COMPARABILITY IS THE WHOLE POINT
Exits resolve through backtest.simulate_exit() -- the SAME function, not a
reimplementation. Costs come from config.COST. Position limits mirror the live
state machine (one open trade per asset+profile). If this file re-derived any
of that, a forward/backtest gap would be ambiguous between "the strategy
changed" and "the simulator changed", and the test would answer nothing.

AUTO START / AUTO OFF
The session is a state machine: DISARMED -> ARMED -> STOPPED(reason). It stops
itself on any of:
  * drawdown past `max_drawdown_r`         -- the real risk control
  * `max_trades` reached                   -- sample target hit, go evaluate
  * `max_days` elapsed                     -- sessions do not run forever
  * stale data                             -- never paper-trade a frozen feed
Auto-stop is evaluated BEFORE new entries on every step, so a breach cannot
open one more position on its way out.
"""

import argparse
import datetime as dt
import json
import math
import os

import numpy as np
import pandas as pd

import backtest
import config

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "paper_state.json")

# Defaults chosen against the audit's own numbers: the backtest's worst
# 30-day stretch was -30.8R, so a 25R stop-out triggers before a repeat of
# that becomes a long silent bleed. 60 trades is the smallest sample where a
# -0.09R expectancy would be distinguishable from zero at all.
DEFAULTS = {
    "max_drawdown_r": 25.0,
    "max_trades": 60,
    "max_days": 45,
    "stale_minutes": 90,
}

DISARMED, ARMED, STOPPED = "DISARMED", "ARMED", "STOPPED"


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.isoformat()


# ------------------------------------------------------------------ STATE ---

def new_session(limits=None, note=""):
    lim = dict(DEFAULTS)
    lim.update(limits or {})
    return {
        "status": ARMED,
        "started_at": _iso(_now()),
        "stopped_at": None,
        "stop_reason": None,
        "note": note,
        "limits": lim,
        "open": {},        # "ASSET|PROFILE" -> position dict
        "trades": [],      # closed trades, same shape as backtest rows
        "seen": [],        # signal fingerprints already acted on
        "last_step": None,
    }


def load_state(path=STATE_PATH):
    if not os.path.exists(path):
        return {"status": DISARMED, "trades": [], "open": {}, "seen": [],
                "limits": dict(DEFAULTS), "started_at": None,
                "stopped_at": None, "stop_reason": None, "note": "",
                "last_step": None}
    with open(path) as f:
        return json.load(f)


def save_state(state, path=STATE_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)      # atomic: a crash mid-write cannot corrupt state


def arm(limits=None, note="", path=STATE_PATH):
    """Start a fresh session. Deliberately discards any previous session's
    open positions -- carrying them across would mix two samples and make the
    resulting expectancy uninterpretable."""
    st = new_session(limits, note)
    save_state(st, path)
    return st


def disarm(reason="manual", path=STATE_PATH):
    st = load_state(path)
    if st["status"] != STOPPED:
        st["status"] = STOPPED
        st["stopped_at"] = _iso(_now())
        st["stop_reason"] = reason
    save_state(st, path)
    return st


# ------------------------------------------------------------- AUTO-STOP ----

def equity_curve_r(trades, risk_frac=0.01):
    """Equity multiple and peak-to-trough drawdown, in R and in %.

    Drawdown is tracked in R (not %) for the stop rule because R is the unit
    the limit is expressed in and is invariant to the account size the user
    happens to model.
    """
    cum, peak, dd_r = 0.0, 0.0, 0.0
    eq, eq_peak, dd_pct = 1.0, 1.0, 0.0
    for t in trades:
        r = float(t.get("r_multiple") or 0.0)
        cum += r
        peak = max(peak, cum)
        dd_r = max(dd_r, peak - cum)
        eq *= (1 + risk_frac * r)
        eq_peak = max(eq_peak, eq)
        dd_pct = max(dd_pct, (eq_peak - eq) / eq_peak)
    return {"total_r": cum, "max_dd_r": dd_r,
            "equity_mult": eq, "max_dd_pct": 100 * dd_pct}


def check_auto_stop(state, data_age_minutes=None):
    """Return a stop reason string, or None to keep running.

    Evaluated before entries on every step. Order matters only for which
    reason gets recorded; any one of them ends the session.
    """
    lim = state.get("limits") or DEFAULTS
    trades = state.get("trades") or []

    eq = equity_curve_r(trades)
    if eq["max_dd_r"] >= lim["max_drawdown_r"]:
        return (f"drawdown {eq['max_dd_r']:.1f}R reached limit "
                f"{lim['max_drawdown_r']:.1f}R")

    if len(trades) >= lim["max_trades"]:
        return f"sample target reached ({len(trades)} trades) -- evaluate now"

    if state.get("started_at"):
        started = pd.Timestamp(state["started_at"]).to_pydatetime()
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.timezone.utc)
        days = (_now() - started).total_seconds() / 86400.0
        if days >= lim["max_days"]:
            return f"session expired after {days:.1f} days"

    if data_age_minutes is not None and data_age_minutes > lim["stale_minutes"]:
        return (f"data stale ({data_age_minutes:.0f} min old) -- refusing to "
                "paper-trade a frozen feed")
    return None


# ------------------------------------------------------------------- STEP ---

def _fingerprint(asset, profile, sig):
    """Identity of a signal, so the same standing setup is not entered twice.

    Keyed on the levels rather than a timestamp: the engine re-emits the same
    setup every 15 minutes while it remains valid, and entering it on each
    re-emission would manufacture a dozen correlated positions out of one idea.
    """
    s = sig.get("setup") or {}
    tg = (s.get("targets") or [{}])[0]
    return "|".join(str(x) for x in (
        asset, profile, sig.get("decision"), s.get("selected_stop"),
        tg.get("price")))


def step(state, signals, bars5_by_asset, now=None):
    """Advance the paper session by one engine cycle.

    `signals`        : {asset: analyze_frames() result}
    `bars5_by_asset` : {asset: 5M DataFrame} used to resolve exits exactly as
                       the backtest does.

    Returns (state, events) where events is a human-readable log of what
    happened, which is what the website renders.
    """
    now = now or _now()
    events = []
    if state.get("status") != ARMED:
        return state, [f"session {state.get('status')} -- no action"]

    # ---- 1. settle open positions first
    for key in list(state.get("open", {})):
        pos = state["open"][key]
        asset = pos["asset"]
        bars5 = bars5_by_asset.get(asset)
        if bars5 is None or not len(bars5):
            continue
        t5 = bars5["time"].values
        entry_idx = int(np.searchsorted(t5, np.datetime64(
            pd.Timestamp(pos["entry_time"]).tz_localize(None)), side="right"))
        if entry_idx >= len(bars5):
            continue
        exit_px, reason, held = backtest.simulate_exit(
            bars5, entry_idx, pos["direction"], pos["entry"], pos["stop"],
            pos["target"], pos["max_bars"])
        if reason == "NO_DATA":
            continue
        exit_idx = entry_idx + held - 1
        if exit_idx >= len(t5):
            continue
        # Only settle once the exit bar has actually closed.
        if pd.Timestamp(t5[exit_idx]) > pd.Timestamp(now).tz_localize(None):
            continue

        move = (exit_px - pos["entry"]) if pos["direction"] == "LONG" \
            else (pos["entry"] - exit_px)
        cost = pos["entry"] * config.COST["round_turn_pct"]
        pos.update({
            "exit": exit_px, "exit_reason": reason, "bars_held": held,
            "exit_time": str(pd.Timestamp(t5[exit_idx])),
            "r_multiple": (move - cost) / pos["risk"] if pos["risk"] else 0.0,
            "net_move": move - cost,
        })
        state["trades"].append(pos)
        del state["open"][key]
        events.append(f"CLOSE {asset} {pos['profile']} {reason} "
                      f"{pos['r_multiple']:+.2f}R")

    # ---- 2. auto-stop BEFORE any new entry
    ages = []
    for asset, bars5 in bars5_by_asset.items():
        if bars5 is not None and len(bars5):
            last = pd.Timestamp(bars5["time"].iloc[-1])
            if last.tzinfo is None:
                last = last.tz_localize("UTC")
            ages.append((now - last.to_pydatetime()).total_seconds() / 60.0)
    reason = check_auto_stop(state, min(ages) if ages else None)
    if reason:
        state["status"] = STOPPED
        state["stopped_at"] = _iso(now)
        state["stop_reason"] = reason
        state["last_step"] = _iso(now)
        events.append(f"AUTO-STOP: {reason}")
        return state, events

    # ---- 3. new entries
    for asset, res in (signals or {}).items():
        bars5 = bars5_by_asset.get(asset)
        if bars5 is None or not len(bars5):
            continue
        price = float(bars5["close"].iloc[-1])
        for profile, p in (res.get("profiles") or {}).items():
            if p.get("decision") not in ("LONG", "SHORT"):
                continue
            key = f"{asset}|{profile}"
            if key in state.get("open", {}):
                continue
            fp = _fingerprint(asset, profile, p)
            if fp in state.get("seen", []):
                continue
            s = p.get("setup") or {}
            tgts = s.get("targets") or []
            if not tgts or s.get("selected_stop") is None:
                continue

            direction = p["decision"]
            slip = price * config.COST["round_turn_pct"] / 2
            entry = price + slip if direction == "LONG" else price - slip
            stop = float(s["selected_stop"])
            target = float(tgts[0]["price"])
            risk = abs(entry - stop)
            if risk <= 0:
                continue

            state.setdefault("open", {})[key] = {
                "asset": asset, "profile": profile, "direction": direction,
                "entry_time": str(pd.Timestamp(bars5["time"].iloc[-1])),
                "entry": entry, "stop": stop, "target": target, "risk": risk,
                "planned_rr": tgts[0].get("rr"),
                "confluence": p.get("confluence_score"),
                "max_bars": backtest.MAX_HOLD_BARS_5M.get(profile, 288),
            }
            state.setdefault("seen", []).append(fp)
            events.append(f"OPEN  {asset} {profile} {direction} @ {entry:.2f} "
                          f"stop {stop:.2f} target {target:.2f}")

    state["last_step"] = _iso(now)
    if not events:
        events.append("no change")
    return state, events


# -------------------------------------------------------------- VERDICT -----

def verify(state, backtest_expectancy=-0.0923, backtest_n=451):
    """Compare the forward sample against the backtest's expectancy.

    The question is NOT "did paper trading make money" -- with 60 trades that
    is unanswerable either way. It is "is the forward result consistent with
    what the backtest predicted", which is a question about whether the
    backtest is trustworthy. A two-sample z-test on the difference answers it.

    A |z| above 2 means forward and backtest disagree beyond chance, and the
    backtest should be treated as suspect until the gap is explained.
    """
    trades = state.get("trades") or []
    n = len(trades)
    if n < 10:
        return {"n": n, "verdict": "insufficient sample",
                "detail": f"{n} trades; need >=10 before any comparison, "
                          ">=60 before the result means much"}

    r = np.array([float(t.get("r_multiple") or 0.0) for t in trades])
    sd = r.std(ddof=1)
    se_fwd = sd / math.sqrt(n) if sd > 0 else 0.0
    # Backtest SE from its own reported dispersion (std 1.645 over n=451).
    se_bt = 1.645 / math.sqrt(backtest_n)
    se = math.sqrt(se_fwd ** 2 + se_bt ** 2)
    diff = r.mean() - backtest_expectancy
    z = diff / se if se > 0 else 0.0

    eq = equity_curve_r(trades)
    if abs(z) < 2:
        verdict = "consistent with backtest"
        detail = ("forward expectancy agrees with the backtest within noise; "
                  "the -0.09R measurement is corroborated")
    elif diff > 0:
        verdict = "forward BETTER than backtest"
        detail = ("forward beat the backtest by more than chance -- either a "
                  "lucky window or the backtest is too pessimistic; do not "
                  "act on this without a second session")
    else:
        verdict = "forward WORSE than backtest"
        detail = ("forward underperformed the backtest beyond chance -- the "
                  "backtest is optimistic, most likely on costs or fills")

    return {
        "n": n,
        "expectancy_r": float(r.mean()),
        "win_rate": float(100.0 * (r > 0).mean()),
        "total_r": eq["total_r"],
        "max_dd_r": eq["max_dd_r"],
        "backtest_expectancy_r": backtest_expectancy,
        "difference_r": float(diff),
        "z": float(z),
        "verdict": verdict,
        "detail": detail,
    }


def snapshot(state):
    """Compact dict for the website: status, limits, progress, verdict."""
    trades = state.get("trades") or []
    eq = equity_curve_r(trades)
    lim = state.get("limits") or DEFAULTS
    return {
        "status": state.get("status", DISARMED),
        "started_at": state.get("started_at"),
        "stopped_at": state.get("stopped_at"),
        "stop_reason": state.get("stop_reason"),
        "last_step": state.get("last_step"),
        "note": state.get("note", ""),
        "limits": lim,
        "open_positions": list((state.get("open") or {}).values()),
        "closed_trades": len(trades),
        "total_r": round(eq["total_r"], 3),
        "max_dd_r": round(eq["max_dd_r"], 3),
        "equity_mult": round(eq["equity_mult"], 4),
        "progress": {
            "trades": f"{len(trades)}/{lim['max_trades']}",
            "drawdown": f"{eq['max_dd_r']:.1f}/{lim['max_drawdown_r']:.1f}R",
        },
        "verification": verify(state),
        "recent": trades[-10:],
    }


# ------------------------------------------------------------ REMOTE SYNC ---
# The engine runs on a 15-minute GitHub Actions cron; the website is a static
# Vercel page. They never share a process, so the button cannot call this code
# directly. Instead the page writes an INTENT ("arm" / "disarm") and the next
# engine cycle honours it and clears it.
#
# The consequence is worth stating plainly rather than hiding behind a spinner:
# pressing Stop takes effect at the next cycle, up to 15 minutes later. For a
# paper session that is harmless. It is also exactly why the drawdown and
# staleness limits live in check_auto_stop() on the engine side -- a kill
# switch that depends on a web request arriving is not a kill switch.

PAPER_TABLE = "paper_state"
ROW_ID = 1


def _supa():
    import supa
    url, key = supa._config()
    return url, key, (supa._headers(key) if key else None)


def push_remote(state):
    """Mirror the snapshot to Supabase for the website to read."""
    url, key, headers = _supa()
    if not url:
        return False
    import requests
    row = {"id": ROW_ID, "payload": snapshot(state), "requested": None,
           "updated_at": _iso(_now())}
    try:
        h = dict(headers); h["Prefer"] = "resolution=merge-duplicates"
        r = requests.post(f"{url}/rest/v1/{PAPER_TABLE}", json=[row],
                          headers=h, timeout=10)
        if not r.ok:
            print(f"paper push failed: {r.status_code} {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"paper push failed: {e}")
        return False


def pull_request(clear=True):
    """Read a pending arm/disarm intent from the website. Returns None or the
    string. Clearing it on read is what keeps a single button press from
    re-arming the session on every subsequent cycle."""
    url, key, headers = _supa()
    if not url:
        return None
    import requests
    try:
        r = requests.get(f"{url}/rest/v1/{PAPER_TABLE}",
                         params={"id": f"eq.{ROW_ID}", "select": "requested"},
                         headers=headers, timeout=10)
        if not r.ok or not r.json():
            return None
        req = (r.json()[0] or {}).get("requested")
        if req and clear:
            requests.patch(f"{url}/rest/v1/{PAPER_TABLE}",
                           params={"id": f"eq.{ROW_ID}"},
                           json={"requested": None}, headers=headers,
                           timeout=10)
        return req
    except Exception as e:
        print(f"paper pull failed: {e}")
        return None


def apply_remote_intent(state):
    """Honour a pending website request. Returns (state, note|None)."""
    req = pull_request()
    if req == "arm":
        return arm(state.get("limits"), note="armed from website"), \
            "ARMED by website request"
    if req == "disarm":
        return disarm("stopped from website"), "STOPPED by website request"
    return state, None


# -------------------------------------------------------------------- CLI ----

def main():
    ap = argparse.ArgumentParser(description="paper-trading forward test")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("arm", help="start a new paper session")
    a.add_argument("--max-drawdown-r", type=float, default=DEFAULTS["max_drawdown_r"])
    a.add_argument("--max-trades", type=int, default=DEFAULTS["max_trades"])
    a.add_argument("--max-days", type=int, default=DEFAULTS["max_days"])
    a.add_argument("--note", default="")

    d = sub.add_parser("disarm", help="stop the session now")
    d.add_argument("--reason", default="manual")

    sub.add_parser("status", help="print the current snapshot")
    sub.add_parser("verify", help="compare forward result to the backtest")

    s = sub.add_parser("step", help="advance one cycle using the live engine")
    s.add_argument("--assets", default="XAUUSD")

    args = ap.parse_args()

    if args.cmd == "arm":
        st = arm({"max_drawdown_r": args.max_drawdown_r,
                  "max_trades": args.max_trades,
                  "max_days": args.max_days}, note=args.note)
        print(json.dumps(snapshot(st), indent=2, default=str))
    elif args.cmd == "disarm":
        print(json.dumps(snapshot(disarm(args.reason)), indent=2, default=str))
    elif args.cmd == "status":
        print(json.dumps(snapshot(load_state()), indent=2, default=str))
    elif args.cmd == "verify":
        print(json.dumps(verify(load_state()), indent=2, default=str))
    elif args.cmd == "step":
        import analysis
        import dataio
        st = load_state()
        signals, bars = {}, {}
        for asset in args.assets.split(","):
            frames, _ = dataio.load_frames(asset)
            price = float(frames["5M"]["close"].iloc[-1])
            signals[asset] = analysis.analyze_frames(asset, frames, price)
            bars[asset] = frames["5M"]
        st, events = step(st, signals, bars)
        save_state(st)
        for e in events:
            print(e)
        print(json.dumps(snapshot(st), indent=2, default=str))


if __name__ == "__main__":
    main()
