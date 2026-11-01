"""
Active demo trading for user accounts: a simple, busy strategy on live
5-minute candles, so a tester sees orders open and close on BTC, gold and
silver within minutes to hours instead of waiting days for a real setup.

THIS IS NOT THE SIGNAL ENGINE
The shared account still trades only the engine's real setups (papertrader).
This module exists to exercise the product -- orders, exits, P&L, wallet --
and the terminal labels it as such. The rule is deliberately plain:

  entry   BUY when the 9-candle EMA crosses above the 21-candle EMA on a
          closed 5-minute candle, SELL when it crosses below; one open order
          per asset at a time. The first order on each market goes in the
          trend's direction on the first candle after Start, so a tester
          sees trading within minutes instead of waiting for a cross.
  stop    1.5 x ATR(14) from the entry, but never closer than 3 x the
          round-turn cost (config.MIN_COST_MULTIPLE): on a quiet weekend
          market ATR alone put the stop inside the spread, and every order
          was stopped out on the next candle
  target  2 x the stop distance (2R)
  time    closes at market after 48 candles (4 hours)

Orders are real fills on real bars: entries at the close of the crossing
candle, exits where the bar's high or low first touched the stop or target
(the stop wins if one bar touched both -- the conservative read), timed
at the close of that candle. Costs and
P&L use the same formula as papertrader.step(), so the terminal's floating
P&L and the realised figure agree.

NEVER STOPS BY ITSELF
No drawdown, trade-count, age or stale-feed stop. The account runs until the
user presses Stop, which closes every open order at the latest price.

CATCH-UP
Each step replays every closed candle since the last one it saw (per asset),
so it doesn't matter whether it runs every minute (local dev server) or
whenever the engine's cron fires (production): orders open and close at the
bar they would have, as long as the feed still holds those bars (~60h of
5-minute candles on Kraken).

Bars are plain dicts {time (unix, candle open), open, high, low, close},
the shape devserver.fetch_candles() and /api/candles already use.
"""

import datetime as dt

import config
import papertrader

ASSETS = ("BTCUSD", "XAUUSD", "XAGUSD")
PROFILE = "DEMO"
BAR = 300                  # seconds in a 5-minute candle
FAST, SLOW, ATR_N = 9, 21, 14
STOP_ATR = 1.5
TARGET_R = 2.0
MAX_HOLD = 48              # candles: 4 hours
MAX_TRADES_KEPT = 2000     # bounds the stored state; stats cover these

RULE = ("Active demo: opens a first order with the trend on each market, then "
        "buys or sells when the 9- and 21-candle averages cross on 5-minute "
        "charts. Stop 1.5x the recent range (ATR), target 2x the stop, closes "
        "after 4 hours. For testing, not the signal engine.")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _ts(u):
    """Naive-UTC string, the format the engine and terminal already use."""
    return dt.datetime.fromtimestamp(u, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _unix(s):
    if not s:
        return None
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return int(t.timestamp())


def _ema(xs, n):
    k, out, e = 2.0 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def _atr(bars, n):
    out, a, prev = [], None, None
    for b in bars:
        tr = b["high"] - b["low"] if prev is None else max(
            b["high"] - b["low"], abs(b["high"] - prev), abs(b["low"] - prev))
        a = tr if a is None else a + (tr - a) / n      # Wilder smoothing
        out.append(a)
        prev = b["close"]
    return out


def closed_bars(bars, now_u):
    """Sorted candles that have finished: the forming one never trades."""
    ok = [b for b in (bars or []) if b and b.get("close") is not None
          and int(b["time"]) + BAR <= now_u]
    return sorted(ok, key=lambda b: b["time"])


def size(acct, entry, stop, used_notional):
    """(sizing, None) or (None, why). Risk is risk_pct of the balance, as in
    papertrader.size_order(), but each asset may use at most a third of the
    buying power so one order can't take the margin the other two need."""
    bal, lev = acct["balance"], acct["max_leverage"]
    if bal <= 0:
        return None, "account balance is zero"
    dist = abs(entry - stop)
    if dist <= 0:
        return None, "stop is at the entry"
    room = min(bal * lev / len(ASSETS), bal * lev - used_notional)
    if room < bal * 0.01:
        return None, f"insufficient free margin: ${max(room, 0):,.2f} left"
    units = bal * acct["risk_pct"] / 100.0 / dist
    capped = units * entry > room
    if capped:
        units = room / entry
    notional = units * entry
    return {"units": units, "notional_usd": notional, "risk_usd": units * dist,
            "fee_usd": notional * config.COST["round_turn_pct"],
            "leverage": notional / bal, "size_capped": capped}, None


def _close(state, key, exit_px, reason, exit_u, events):
    pos = state["open"].pop(key)
    move = (exit_px - pos["entry"]) if pos["direction"] == "LONG" else (pos["entry"] - exit_px)
    cost = pos["entry"] * config.COST["round_turn_pct"]
    pnl = (move - cost) * pos["units"]
    acct = state["account"]
    acct["balance"] += pnl
    pos.update({"exit": exit_px, "exit_reason": reason, "exit_time": _ts(exit_u),
                "r_multiple": (move - cost) / pos["risk"] if pos["risk"] else 0.0,
                "net_move": move - cost, "pnl_usd": pnl,
                "balance_after": acct["balance"]})
    state.setdefault("trades", []).append(pos)
    del state["trades"][:-MAX_TRADES_KEPT]
    events.append(f"CLOSE {pos['asset']} {pos['direction']} {reason} @ {exit_px:.2f}  "
                  f"${pnl:+,.2f}  balance ${acct['balance']:,.2f}")


def step(state, bars_by_asset, now=None):
    """Advance one user's demo account to `now`. Mutates and returns
    (state, events), like papertrader.step()."""
    events = []
    if state.get("status") != papertrader.ARMED or not state.get("account"):
        return state, events
    now_u = int((now or _now()).timestamp())
    start_u = _unix(state.get("started_at")) or now_u
    cursor = state.setdefault("demo_cursor", {})
    entered = state.setdefault("demo_entered", [])     # assets with a first order
    state.setdefault("open", {})

    for asset in ASSETS:
        bars = closed_bars(bars_by_asset.get(asset), now_u)
        if len(bars) < SLOW + 2:
            continue
        closes = [b["close"] for b in bars]
        fast, slow, atr = _ema(closes, FAST), _ema(closes, SLOW), _atr(bars, ATR_N)
        key = f"{asset}|{PROFILE}"
        seen = cursor.get(asset)
        for i in range(SLOW + 1, len(bars)):
            b = bars[i]
            # Only candles that closed after the account started, and only
            # once each: the cursor is what makes catch-up safe to repeat.
            if b["time"] + BAR <= start_u or (seen is not None and b["time"] <= seen):
                continue

            pos = state["open"].get(key)
            if pos and b["time"] >= pos["entry_u"]:
                pos["bars_held"] = pos.get("bars_held", 0) + 1
                long_ = pos["direction"] == "LONG"
                hit_stop = b["low"] <= pos["stop"] if long_ else b["high"] >= pos["stop"]
                hit_tgt = b["high"] >= pos["target"] if long_ else b["low"] <= pos["target"]
                # Timed at the candle's close: the bar says it happened
                # within those 5 minutes, not when.
                if hit_stop:
                    _close(state, key, pos["stop"], "SL", b["time"] + BAR, events)
                elif hit_tgt:
                    _close(state, key, pos["target"], "TP", b["time"] + BAR, events)
                elif pos["bars_held"] >= MAX_HOLD:
                    _close(state, key, b["close"], "TIME", b["time"] + BAR, events)

            if key not in state["open"] and atr[i] > 0:
                up = fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]
                down = fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
                if asset not in entered and not (up or down):
                    up, down = fast[i] >= slow[i], fast[i] < slow[i]
                if up or down:
                    direction = "LONG" if up else "SHORT"
                    sgn = 1 if up else -1
                    slip = b["close"] * config.COST["round_turn_pct"] / 2
                    entry = b["close"] + sgn * slip
                    dist = max(STOP_ATR * atr[i],
                               config.MIN_COST_MULTIPLE * entry * config.COST["round_turn_pct"])
                    used = sum(o.get("notional_usd") or 0.0 for o in state["open"].values())
                    sized, why = size(state["account"], entry, entry - sgn * dist, used)
                    if sized is None:
                        events.append(f"SKIP  {asset} {direction} -- {why}")
                    else:
                        fill_u = b["time"] + BAR      # filled at this candle's close
                        state["open"][key] = dict(
                            sized, asset=asset, profile=PROFILE, direction=direction,
                            entry_time=_ts(fill_u), entry_u=fill_u, entry=entry,
                            stop=entry - sgn * dist, target=entry + sgn * TARGET_R * dist,
                            risk=dist, planned_rr=TARGET_R, max_bars=MAX_HOLD, bars_held=0)
                        if asset not in entered:
                            entered.append(asset)
                        events.append(f"OPEN  {asset} {direction} @ {entry:.2f}  "
                                      f"{sized['units']:.4f} units  risking ${sized['risk_usd']:,.2f}")
            cursor[asset] = b["time"]

    state["last_step"] = papertrader._iso(_now() if now is None else now)
    return state, events


def last_prices(bars_by_asset):
    """Latest price per asset, forming candle included: what Stop closes at."""
    out = {}
    for asset, bars in (bars_by_asset or {}).items():
        good = [b for b in (bars or []) if b and b.get("close") is not None]
        if good:
            b = max(good, key=lambda x: x["time"])
            out[asset] = (b["close"], int(b["time"]))
    return out


def close_all(state, prices, reason="STOP", now=None):
    """Close every open order at the latest price. An asset with no price
    (feed down) closes at its entry, for zero gain before costs, rather than
    being left open on an account that is stopping."""
    events = []
    now_u = int((now or _now()).timestamp())
    for key in list((state.get("open") or {})):
        pos = state["open"][key]
        px, _ = prices.get(pos["asset"], (pos["entry"], now_u))
        _close(state, key, px, reason, now_u, events)
    return state, events


def frames_to_bars(frames_by_asset):
    """The engine's 5M DataFrames (time, open, high, low, close) -> bar dicts."""
    import pandas as pd
    out = {}
    for asset, df in (frames_by_asset or {}).items():
        if df is None or not len(df):
            continue
        rows = []
        for t, o, h, l, c in zip(df["time"], df["open"], df["high"], df["low"], df["close"]):
            ts = pd.Timestamp(t)
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
            rows.append({"time": int(ts.timestamp()), "open": float(o), "high": float(h),
                         "low": float(l), "close": float(c)})
        out[asset] = rows
    return out
