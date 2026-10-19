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
import risk

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

# Dummy-dollar account. risk_pct is a PERCENT, matching risk.position_size()
# (which divides by 100) -- execution.py already had a 100x sizing bug from
# confusing the two, so the unit is stated here rather than assumed.
DEFAULT_BALANCE = 1000.0
DEFAULT_LEVERAGE = 1.0      # 1.0 = spot, no borrowing: notional <= balance
DEFAULT_RISK_PCT = 1.0      # percent of current balance risked per order


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.isoformat()


# ------------------------------------------------------------------ STATE ---

def new_account(balance=DEFAULT_BALANCE, max_leverage=DEFAULT_LEVERAGE,
                risk_pct=DEFAULT_RISK_PCT):
    return {"currency": "USD", "starting_balance": float(balance),
            "balance": float(balance), "max_leverage": float(max_leverage),
            "risk_pct": float(risk_pct)}


def new_session(limits=None, note="", balance=DEFAULT_BALANCE,
                max_leverage=DEFAULT_LEVERAGE, risk_pct=DEFAULT_RISK_PCT):
    lim = dict(DEFAULTS)
    lim.update(limits or {})
    return {
        "account": new_account(balance, max_leverage, risk_pct),
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


def arm(limits=None, note="", path=STATE_PATH, balance=DEFAULT_BALANCE,
        max_leverage=DEFAULT_LEVERAGE, risk_pct=DEFAULT_RISK_PCT):
    """Start a fresh session with a fresh dummy-dollar account. Deliberately
    discards any previous session's open positions -- carrying them across
    would mix two samples and make the resulting expectancy uninterpretable."""
    st = new_session(limits, note, balance, max_leverage, risk_pct)
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

def scored(trades):
    """Only trades carrying a real R. UNRESOLVABLE ones are counted in the
    record but must never enter expectancy, drawdown or the stop rules --
    a void trade is missing data, not a flat result."""
    return [t for t in (trades or []) if t.get("r_multiple") is not None]


def equity_curve_r(trades, risk_frac=0.01):
    """Equity multiple and peak-to-trough drawdown, in R and in %.

    Drawdown is tracked in R (not %) for the stop rule because R is the unit
    the limit is expressed in and is invariant to the account size the user
    happens to model.
    """
    cum, peak, dd_r = 0.0, 0.0, 0.0
    eq, eq_peak, dd_pct = 1.0, 1.0, 0.0
    for t in scored(trades):
        r = float(t["r_multiple"])
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

    acct = state.get("account")
    if acct is not None and acct.get("balance", 1) <= 0:
        return "account balance reached zero"

    eq = equity_curve_r(trades)
    if eq["max_dd_r"] >= lim["max_drawdown_r"]:
        return (f"drawdown {eq['max_dd_r']:.1f}R reached limit "
                f"{lim['max_drawdown_r']:.1f}R")

    n_scored = len(scored(trades))
    if n_scored >= lim["max_trades"]:
        return f"sample target reached ({n_scored} trades) -- evaluate now"

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


def size_order(acct, asset, entry, stop, used_notional=0.0):
    """(sizing, None) for one paper order, or (None, reason) if it can't be.

    The leverage cap applies to the WHOLE account, not to each order: the
    first version capped every order at balance x leverage independently, so
    a SWING and a SCALP open together held 2x the balance at "1x, no
    borrowing". Notional already held by open orders is subtracted first,
    and an order with no free margin left is refused, as MT5 refuses one.

    Risk is risk_pct of the CURRENT balance, so a losing streak shrinks
    orders and a winning one grows them, as a real fixed-fractional account
    would. At this strategy's typical 0.3% stop, 1% risk needs ~3.3x the
    balance in notional; with max_leverage 1.0 the order is capped at the
    balance and the risk actually taken is smaller. That cap is recorded on
    the order (size_capped, leverage) rather than applied silently, so a
    result is never mistaken for one taken at the full 1%.
    """
    if not acct or acct.get("balance", 0) <= 0:
        return None, "account balance is zero"
    sz = risk.position_size(asset, entry, stop, equity=acct["balance"],
                            risk_pct=acct["risk_pct"])
    if sz.get("error") or not sz.get("units"):
        return None, sz.get("error") or "position size is zero"
    free = acct["balance"] * acct["max_leverage"] - used_notional
    # Below 1% of the balance an order is noise that only pays fees.
    if free < acct["balance"] * 0.01:
        return None, (f"insufficient free margin: ${max(free, 0):,.2f} left of "
                      f"${acct['balance'] * acct['max_leverage']:,.2f}")
    units = float(sz["units"])
    capped = units * entry > free
    if capped:
        units = free / entry
    notional = units * entry
    return {
        "units": units,
        "notional_usd": notional,
        "risk_usd": units * abs(entry - stop),
        "fee_usd": notional * config.COST["round_turn_pct"],
        "leverage": notional / acct["balance"],
        "size_capped": capped,
    }, None


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

    # A profile whose hold exceeds the feed's window can never be resolved:
    # the entry ages out before the position completes, and every such trade
    # becomes UNRESOLVABLE. Kraken returns ~720 5M candles (60h) while SWING
    # wants 1440 (120h), so SWING is structurally un-forward-testable on this
    # feed. Say so once per step rather than leaving a trail of silent voids.
    for _asset, _b5 in (bars5_by_asset or {}).items():
        if _b5 is None or not len(_b5):
            continue
        window = len(_b5)
        for _prof, _need in backtest.MAX_HOLD_BARS_5M.items():
            if _need > window:
                events.append(
                    f"WARN  {_asset} {_prof} needs {_need} bars but the feed "
                    f"window holds {window} -- positions will age out "
                    "unresolved, not complete")
        break      # one warning is enough; the window is the same per asset

    # ---- 1. settle open positions first
    for key in list(state.get("open", {})):
        pos = state["open"][key]
        asset = pos["asset"]
        bars5 = bars5_by_asset.get(asset)
        if bars5 is None or not len(bars5):
            continue
        t5 = bars5["time"].values
        entry_np = np.datetime64(
            pd.Timestamp(pos["entry_time"]).tz_localize(None))

        # The live feed is a SLIDING WINDOW (Kraken returns ~720 candles, so
        # 60h at 5M). Once an entry scrolls off the front, searchsorted
        # returns 0 and simulate_exit would walk bars the trade never traded
        # through -- producing a confident, entirely fictional R. Settle it
        # as UNRESOLVABLE instead, with no r_multiple, so it is visible in the
        # count but cannot contaminate expectancy.
        if entry_np < t5[0]:
            pos.update({
                "exit": None, "exit_reason": "UNRESOLVABLE", "bars_held": None,
                "exit_time": str(pd.Timestamp(t5[0])), "r_multiple": None,
                "net_move": None,
                "note": ("entry scrolled out of the feed's sliding window "
                         f"before the position could be resolved; the window "
                         f"holds {len(t5)} bars and this needed "
                         f"{pos['max_bars']}"),
            })
            state["trades"].append(pos)
            del state["open"][key]
            events.append(f"VOID  {asset} {pos['profile']} unresolvable "
                          "(entry aged out of the data window)")
            continue

        entry_idx = int(np.searchsorted(t5, entry_np, side="right"))
        if entry_idx >= len(bars5):
            continue
        exit_px, reason, held = backtest.simulate_exit(
            bars5, entry_idx, pos["direction"], pos["entry"], pos["stop"],
            pos["target"], pos["max_bars"])
        if reason == "NO_DATA":
            continue

        # simulate_exit returns TIME both when a position genuinely ran its
        # full hold AND when it simply ran out of bars -- end = min(n,
        # start+max_bars). Treating the second as a settled exit closed
        # positions the moment the feed caught up to them: a SWING trade
        # exited at 82 of its 1440 bars (6.8h of an intended 120h), and a
        # SCALP at 35 of 48. The forward test exists to be comparable with
        # the backtest, and a truncated hold is not the same trade.
        if reason == "TIME" and held < pos["max_bars"]:
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
        # move and cost are per unit, so dollars are just times units. This
        # equals r_multiple * risk_usd by construction; computing it from
        # the fill rather than from R keeps the ledger auditable.
        usd = ""
        acct = state.get("account")
        if acct is not None and pos.get("units"):
            pnl = (move - cost) * pos["units"]
            acct["balance"] += pnl
            pos["pnl_usd"] = pnl
            pos["balance_after"] = acct["balance"]
            usd = f"  ${pnl:+,.2f}  balance ${acct['balance']:,.2f}"
        state["trades"].append(pos)
        del state["open"][key]
        events.append(f"CLOSE {asset} {pos['profile']} {reason} "
                      f"{pos['r_multiple']:+.2f}R{usd}")

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

            order = {
                "asset": asset, "profile": profile, "direction": direction,
                "entry_time": str(pd.Timestamp(bars5["time"].iloc[-1])),
                "entry": entry, "stop": stop, "target": target, "risk": risk,
                "planned_rr": tgts[0].get("rr"),
                "confluence": p.get("confluence_score"),
                "max_bars": backtest.MAX_HOLD_BARS_5M.get(profile, 288),
            }
            usd = ""
            if state.get("account") is not None:
                used = sum((o.get("notional_usd") or 0.0)
                           for o in state.get("open", {}).values())
                sized, why = size_order(state["account"], asset, entry, stop,
                                        used_notional=used)
                if sized is None:
                    events.append(f"SKIP  {asset} {profile} {direction} -- {why}")
                    continue
                order.update(sized)
                usd = (f"  {sized['units']:.4f} units  ${sized['notional_usd']:,.2f}"
                       f"  risking ${sized['risk_usd']:,.2f}"
                       + ("  (capped by leverage)" if sized["size_capped"] else ""))
            state.setdefault("open", {})[key] = order
            state.setdefault("seen", []).append(fp)
            events.append(f"OPEN  {asset} {profile} {direction} @ {entry:.2f} "
                          f"stop {stop:.2f} target {target:.2f}{usd}")

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
    trades = scored(state.get("trades"))
    n = len(trades)
    if n < 10:
        return {"n": n, "verdict": "insufficient sample",
                "detail": f"{n} trades; need >=10 before any comparison, "
                          ">=60 before the result means much"}

    r = np.array([float(t["r_multiple"]) for t in trades])
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
        "account": account_snapshot(state),
    }


def account_snapshot(state):
    """The dummy-dollar view: balance, P&L, drawdown, and the order book.

    None for a session armed before accounts existed, so an old session
    renders as R-only rather than inventing a balance it never had.
    """
    acct = state.get("account")
    if not acct:
        return None
    start = acct["starting_balance"]
    closed = [t for t in (state.get("trades") or []) if t.get("pnl_usd") is not None]
    curve = [start] + [t["balance_after"] for t in closed]
    peak, dd_usd, dd_pct = start, 0.0, 0.0
    for b in curve:
        peak = max(peak, b)
        dd_usd = max(dd_usd, peak - b)
        dd_pct = max(dd_pct, (peak - b) / peak if peak else 0.0)
    wins = [t for t in closed if t["pnl_usd"] > 0]
    open_orders = list((state.get("open") or {}).values())
    keep = ("asset", "profile", "direction", "entry_time", "entry", "stop",
            "target", "units", "notional_usd", "risk_usd", "leverage",
            "size_capped")
    return {
        "currency": acct["currency"],
        "starting_balance": round(start, 2),
        "balance": round(acct["balance"], 2),
        "pnl_usd": round(acct["balance"] - start, 2),
        "return_pct": round(100 * (acct["balance"] - start) / start, 2) if start else 0.0,
        "max_drawdown_usd": round(dd_usd, 2),
        "max_drawdown_pct": round(100 * dd_pct, 2),
        "risk_pct": acct["risk_pct"],
        "max_leverage": acct["max_leverage"],
        # The page recomputes floating P&L every few seconds from the live
        # candle close, so it needs the SAME round-turn rate step() charges
        # at close -- otherwise floating P&L and the realised figure it turns
        # into would disagree by the fee the moment an order closes.
        "fee_rate": config.COST["round_turn_pct"],
        # Margin as MT5 shows it: notional held divided by the leverage cap.
        "margin_usd": round(sum((o.get("notional_usd") or 0.0) for o in open_orders)
                            / (acct["max_leverage"] or 1.0), 2),
        "orders_closed": len(closed),
        "wins": len(wins),
        "fees_usd": round(sum(t.get("fee_usd", 0.0) for t in closed), 2),
        "open_risk_usd": round(sum(o.get("risk_usd", 0.0) for o in open_orders), 2),
        "open_orders": [{k: o.get(k) for k in keep} for o in open_orders],
        "closed_orders": [{k: t.get(k) for k in keep + ("exit", "exit_time",
                           "exit_reason", "r_multiple", "pnl_usd", "balance_after")}
                          for t in closed[-25:]],
        "equity_curve": [round(b, 2) for b in curve],
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
    """Persist the session to Supabase.

    TWO columns, for two different readers, and the distinction is what makes
    the session survive at all:

      payload            -- snapshot(), what the website renders.
      payload["_state"]  -- the COMPLETE state dict, which the engine reads
                            back next cycle to resume.

    The first version of this function wrote only the snapshot, which was a
    real bug rather than a shortcut: GitHub Actions runners are ephemeral and
    paper_state.json is gitignored, so load_state() sees no file and returns
    DISARMED on every run. A session armed from the website would have lasted
    exactly one cycle and then silently forgotten itself -- and because the
    snapshot carries a `closed_trades` COUNT rather than the `trades` list,
    reading it back could not have restored the session either.
    """
    url, key, headers = _supa()
    if not url:
        return False
    import requests
    # Full state rides INSIDE payload under "_state" rather than in its own
    # column, so no schema change is needed and the website -- which spreads
    # payload and reads named snapshot fields -- simply ignores the extra key.
    row = {"id": ROW_ID, "payload": {**snapshot(state), "_state": state},
           "requested": None, "updated_at": _iso(_now())}
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


def pull_state():
    """The full session state as last persisted, or None.

    None means "no session has ever been pushed", which is different from a
    DISARMED session and is why this returns None rather than a fresh
    new_session().
    """
    url, key, headers = _supa()
    if not url:
        return None
    import requests
    try:
        r = requests.get(f"{url}/rest/v1/{PAPER_TABLE}",
                         params={"id": f"eq.{ROW_ID}", "select": "payload"},
                         headers=headers, timeout=10)
        if not r.ok or not r.json():
            return None
        payload = (r.json()[0] or {}).get("payload") or {}
        return payload.get("_state") or None
    except Exception as e:
        print(f"paper state pull failed: {e}")
        return None


def load_state_remote_first(path=STATE_PATH):
    """Remote state wins over the local file.

    Remote is the source of truth because the engine runs on a throwaway
    runner while the session has to outlive it. Locally the two agree, since
    every step pushes; if the network is down we fall back to the file rather
    than silently starting a fresh session on top of a live one.
    """
    remote = pull_state()
    if remote:
        return remote
    return load_state(path)


def parse_arm(req):
    """('arm:2500:2') -> (2500.0, 2.0). Bad or missing values fall back to the
    defaults rather than raising: a malformed request from the website must
    not crash the engine cycle it rides on."""
    bal, lev = DEFAULT_BALANCE, DEFAULT_LEVERAGE
    parts = (req or "").split(":")
    try:
        if len(parts) > 1 and parts[1]:
            b = float(parts[1])
            if 1 <= b <= 1e9:
                bal = b
        if len(parts) > 2 and parts[2]:
            l = float(parts[2])
            if 1 <= l <= 10:
                lev = l
    except ValueError:
        pass
    return bal, lev


def apply_remote_intent(state):
    """Honour a pending website request. Returns (state, note|None).

    Operates on the state it was HANDED rather than reloading from disk.
    disarm() reads the local file, which on an ephemeral runner is empty --
    calling it here would answer a Stop request by throwing away the very
    session being stopped, losing every trade it had recorded.
    """
    req = pull_request()
    # "arm" or "arm:<balance>" or "arm:<balance>:<leverage>". The amount rides
    # in the intent string because the website and the engine share no
    # process -- this column is the only channel between them.
    if req and req.split(":", 1)[0] == "arm":
        bal, lev = parse_arm(req)
        return (new_session(state.get("limits"), note="armed from website",
                            balance=bal, max_leverage=lev),
                f"ARMED by website request with ${bal:,.2f} at {lev:g}x")
    if req == "disarm":
        st = dict(state)
        if st.get("status") != STOPPED:
            st["status"] = STOPPED
            st["stopped_at"] = _iso(_now())
            st["stop_reason"] = "stopped from website"
        return st, "STOPPED by website request"
    return state, None


# --------------------------------------------------------------- SIMULATE ---

def simulate(asset="XAUUSD", days=30, balance=DEFAULT_BALANCE,
             max_leverage=DEFAULT_LEVERAGE, step_minutes=15, verbose=True,
             log=print):
    """Replay past prices through the LIVE order engine with dummy dollars.

    This is not a second backtester. Every order is opened and closed by
    step() -- the same function the 15-minute production cron calls -- fed
    one replay step at a time with only the bars that existed at that moment.
    What it shows is what the live paper account would have done.

    One deliberate difference from live: each step is handed the full 5M
    history up to that moment, not Kraken's 720-bar window. Live, a SWING
    order (1440 bars) ages out of the window and voids; here it can finish.
    So SWING orders appear here that the live account cannot complete yet.
    """
    import analysis
    import histdata
    import indicators as ind

    frames = histdata.load_history(asset, window_days=days)
    frames_i = {tf: ind.compute_all(df, tf)[0]
                for tf, df in frames.items() if len(df) >= 30}
    times = {tf: df["time"].values for tf, df in frames.items()}
    times_i = {tf: df["time"].values for tf, df in frames_i.items()}

    def cut(df, tv, t):
        return df.iloc[:np.searchsorted(tv, t, side="right")]

    b5 = frames["5M"]
    start = b5["time"].iloc[min(500, len(b5) - 1)]
    timeline = pd.date_range(start, b5["time"].iloc[-1], freq=f"{step_minutes}min")

    st = new_session({"max_trades": 10**6, "max_drawdown_r": 10**6,
                      "max_days": 10**6}, note=f"simulation {asset} {days}d",
                     balance=balance, max_leverage=max_leverage)
    if verbose:
        log(f"\nSIMULATING {asset}: {timeline[0]} -> {timeline[-1]}  "
            f"({len(timeline)} steps)   start ${balance:,.2f} at {max_leverage:g}x\n")

    for n, t in enumerate(timeline):
        tnp = np.datetime64(t)
        f_t = {tf: cut(frames[tf], times[tf], tnp) for tf in frames}
        if len(f_t["5M"]) < 30:
            continue
        fi_t = {tf: cut(frames_i[tf], times_i[tf], tnp) for tf in frames_i}
        price = float(f_t["5M"]["close"].iloc[-1])
        try:
            res = analysis.analyze_frames(asset, f_t, price, frames_i=fi_t, hard=[])
        except Exception:
            continue
        st, events = step(st, {asset: res}, {asset: f_t["5M"]},
                          now=pd.Timestamp(t).tz_localize("UTC"))
        if verbose:
            for e in events:
                if e.startswith(("OPEN", "CLOSE", "VOID", "SKIP", "AUTO")):
                    log(f"  {t:%Y-%m-%d %H:%M}  {e}")
    # The replay runs with the auto-stop limits switched off so it covers the
    # whole window, but those million-trade limits must not leave this
    # function: a saved replay is displayed by the same panels as a live
    # account, which then read "3 / 1000000 trades" and "stops at 1000000R".
    st["limits"] = dict(DEFAULTS)
    return st


# -------------------------------------------------------------------- CLI ----

def main():
    ap = argparse.ArgumentParser(description="paper-trading forward test")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("arm", help="start a new paper session")
    a.add_argument("--max-drawdown-r", type=float, default=DEFAULTS["max_drawdown_r"])
    a.add_argument("--max-trades", type=int, default=DEFAULTS["max_trades"])
    a.add_argument("--max-days", type=int, default=DEFAULTS["max_days"])
    a.add_argument("--note", default="")
    a.add_argument("--balance", type=float, default=DEFAULT_BALANCE,
                   help="dummy dollars to start with")
    a.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE,
                   help="max notional as a multiple of balance (1 = no borrowing)")

    d = sub.add_parser("disarm", help="stop the session now")
    d.add_argument("--reason", default="manual")

    sub.add_parser("status", help="print the current snapshot")
    sub.add_parser("verify", help="compare forward result to the backtest")

    sm = sub.add_parser("simulate", help="replay past prices through the live "
                                         "order engine with dummy dollars")
    sm.add_argument("--asset", default="XAUUSD")
    sm.add_argument("--days", type=int, default=30)
    sm.add_argument("--balance", type=float, default=DEFAULT_BALANCE)
    sm.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE)
    sm.add_argument("--save", action="store_true",
                    help="write the replayed account to paper_state.json so "
                         "devserver.py and the terminal display it")

    s = sub.add_parser("step", help="advance one cycle using the live engine")
    s.add_argument("--assets", default="XAUUSD")

    args = ap.parse_args()

    if args.cmd == "arm":
        st = arm({"max_drawdown_r": args.max_drawdown_r,
                  "max_trades": args.max_trades,
                  "max_days": args.max_days}, note=args.note,
                 balance=args.balance, max_leverage=args.leverage)
        print(json.dumps(snapshot(st), indent=2, default=str))
    elif args.cmd == "disarm":
        print(json.dumps(snapshot(disarm(args.reason)), indent=2, default=str))
    elif args.cmd == "status":
        print(json.dumps(snapshot(load_state()), indent=2, default=str))
    elif args.cmd == "verify":
        print(json.dumps(verify(load_state()), indent=2, default=str))
    elif args.cmd == "simulate":
        st = simulate(args.asset, args.days, args.balance, args.leverage)
        if args.save:
            save_state(st)
            print(f"\nsaved to {STATE_PATH}")
        a = account_snapshot(st)
        print("\n" + "=" * 72)
        print(f"  start      ${a['starting_balance']:>12,.2f}")
        print(f"  end        ${a['balance']:>12,.2f}   ({a['return_pct']:+.2f}%)")
        print(f"  P&L        ${a['pnl_usd']:>+12,.2f}   after ${a['fees_usd']:,.2f} in fees")
        print(f"  orders     {a['orders_closed']} closed, {a['wins']} won, "
              f"{len(a['open_orders'])} still open")
        print(f"  worst dip  ${a['max_drawdown_usd']:,.2f} ({a['max_drawdown_pct']:.1f}%)")
        print("=" * 72)
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
