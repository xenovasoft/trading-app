"""
Stop placement, target selection and position sizing.

Two design decisions that differ materially from the original system:

1. Targets are placed at REAL LIQUIDITY, not at fixed multiples of risk. The
   original computed tp1 = entry + 1.5 * risk and then "measured"
   rr = (tp1-entry)/(entry-sl), which is 1.5 by construction. Every signal it
   ever produced reported R:R 1.5 — the number carried zero information about
   the setup. Here R:R is an output of where liquidity actually sits, so it
   varies and can legitimately fail the minimum.

2. Stops are pushed OUT of liquidity clusters. A stop resting inside an
   obvious pool is precisely where the market is drawn to trade.
"""

import numpy as np

import config


def _tick(asset):
    return config.CONTRACT_SPECS[asset]["tick_size"]


def stop_candidates(asset, direction, entry, atr_val, structure_level,
                    swept_level, recent_close_std, profile, entry_zone=None):
    """Return every stop method with its price level, before selection."""
    tick = _tick(asset)
    buf = (profile["stop_buffer_atr"] * atr_val +
           config.EXECUTION["stop_buffer_ticks"] * tick)
    sign = -1 if direction == "LONG" else 1
    cands = {}

    # When entry is a limit at a zone, the zone's far edge IS the invalidation.
    if entry_zone:
        edge = entry_zone["low"] if direction == "LONG" else entry_zone["high"]
        cands["beyond_entry_zone"] = edge + sign * buf
    if structure_level is not None:
        cands["structural"] = structure_level + sign * buf
    if swept_level is not None:
        cands["beyond_sweep"] = swept_level + sign * buf
    cands["atr_2x"] = entry + sign * (2.0 * atr_val)
    if recent_close_std and recent_close_std > 0:
        # volatility-adjusted: 2 sigma of recent bar-to-bar movement
        cands["volatility_2sigma"] = entry + sign * (2.0 * recent_close_std)

    return {k: float(v) for k, v in cands.items() if v is not None}


def avoid_liquidity(asset, direction, stop, zones, atr_val):
    """If the stop sits inside a liquidity zone, push it just beyond that zone."""
    tick = _tick(asset)
    pad = 0.15 * atr_val + 2 * tick
    moved_from, reason = None, None
    for z in zones:
        if z["low"] <= stop <= z["high"]:
            moved_from = stop
            stop = (z["low"] - pad) if direction == "LONG" else (z["high"] + pad)
            reason = (f"original stop sat inside a {z['strength']:.0f}-strength "
                      f"{'/'.join(z['kinds'][:2])} zone "
                      f"({z['low']:.4f}-{z['high']:.4f})")
            break
    return float(stop), moved_from, reason


def select_stop(asset, direction, entry, cands, zones, atr_val, profile):
    """Pick the TIGHTEST structurally-defensible stop, floor it at one ATR of
    noise, push it clear of liquidity, then reject if still too wide.

    Taking the widest candidate (the previous behaviour) meant a distant old
    swing dictated the stop, producing 20-40 ATR stops on scalps that always
    tripped the max-width gate. The invalidation that matters is the nearest
    one that actually breaks the premise.
    """
    if not cands:
        return None

    # Preference order: the level whose violation genuinely kills the idea.
    for key in ("beyond_entry_zone", "beyond_sweep", "structural", "atr_2x"):
        if key in cands:
            stop, basis = cands[key], key
            break
    else:
        stop, basis = list(cands.values())[0], list(cands.keys())[0]

    # Never tighter than one ATR — below that we are inside normal noise.
    min_dist = 1.0 * atr_val
    if abs(entry - stop) < min_dist:
        stop = entry - min_dist if direction == "LONG" else entry + min_dist
        basis += " (widened to 1 ATR noise floor)"

    stop, moved_from, reason = avoid_liquidity(asset, direction, stop, zones, atr_val)
    dist = abs(entry - stop)
    dist_atr = dist / atr_val if atr_val else None

    return {
        "stop": round(stop, 6),
        "basis": basis,
        "distance": round(dist, 6),
        "distance_atr": round(dist_atr, 2) if dist_atr else None,
        "methods": {k: round(v, 6) for k, v in cands.items()},
        "moved_off_liquidity": bool(moved_from),
        "moved_reason": reason,
        "too_wide": bool(dist_atr and dist_atr > profile["max_stop_atr"]),
    }


def select_targets(direction, entry, stop, zones, atr_val, min_rr,
                   max_target_atr=None):
    """T1/T2/T3 taken from opposing liquidity pools, ordered by distance."""
    risk = abs(entry - stop)
    if risk <= 0:
        return [], None, "stop distance is zero"

    want = "buy_side" if direction == "LONG" else "sell_side"
    # The zone must sit ENTIRELY on the profitable side of entry. Filtering on
    # the zone midpoint alone let zones that straddle entry through, and the
    # abs() below then reported a target behind entry as a positive-R target.
    if direction == "LONG":
        pool = [z for z in zones if z["side"] == want and z["low"] > entry]
    else:
        pool = [z for z in zones if z["side"] == want and z["high"] < entry]
    if not pool:
        return [], None, "no opposing liquidity found on the profitable side of entry"

    pool.sort(key=lambda z: abs(z["mid"] - entry))
    targets = []
    dropped_unreachable = 0
    for z in pool[:6]:
        # aim just short of the pool: front-run the crowd, don't queue behind it
        edge = z["low"] if direction == "LONG" else z["high"]
        tp = edge - (0.1 * atr_val) if direction == "LONG" else edge + (0.1 * atr_val)
        # signed reward — never abs(), so a mis-sided target scores <= 0 and is dropped
        reward = (tp - entry) if direction == "LONG" else (entry - tp)
        if reward <= 0:
            continue
        # Reachability: a pool 40 ATR away is not a target for this horizon.
        if max_target_atr and (reward / atr_val) > max_target_atr:
            dropped_unreachable += 1
            continue
        targets.append({
            "price": round(float(tp), 6),
            "rr": round(float(reward / risk), 2),
            "anchor": "/".join(z["kinds"][:2]),
            "zone_strength": z["strength"],
            "distance_atr": round(float(reward / atr_val), 2),
        })

    if not targets:
        if dropped_unreachable:
            return [], None, (
                f"{dropped_unreachable} liquidity pool(s) found but all sit "
                f"beyond {max_target_atr} ATR — too far to be a realistic "
                f"objective for this holding period")
        return [], None, "all candidate targets resolve behind entry"
    targets = targets[:3]

    primary_rr = targets[0]["rr"]
    note = None
    if primary_rr < min_rr:
        note = (f"nearest liquidity gives only {primary_rr:.2f}R "
                f"(minimum {min_rr}) — not enough room to the first pool")
    elif primary_rr > 10:
        # High R:R from a very tight stop is a warning, not a green light: it
        # implies a low probability of surviving noise to reach the target.
        # The system models no win rate, so R:R alone cannot justify the trade.
        note = (f"T1 R:R of {primary_rr:.1f} comes from a "
                f"{abs(entry-stop)/atr_val:.1f} ATR stop — verify the stop is "
                f"not tighter than normal noise before trusting this ratio")
    return targets, primary_rr, note


def position_size(asset, entry, stop, equity=None, risk_pct=None):
    """Size from risk budget and stop distance. Returns units AND lots, plus
    an explicit flag if equity is still the placeholder value."""
    spec = config.CONTRACT_SPECS[asset]
    equity = equity if equity is not None else config.ACCOUNT["equity"]
    risk_pct = risk_pct if risk_pct is not None else config.ACCOUNT["risk_pct_per_trade"]
    dist = abs(entry - stop)
    if dist <= 0:
        return {"error": "zero stop distance"}

    risk_amount = equity * (risk_pct / 100.0)
    units = risk_amount / dist
    lots = units / spec["units_per_lot"]
    notional = units * entry
    cost = notional * config.COST["round_turn_pct"]

    return {
        "equity_used": equity,
        "risk_pct": risk_pct,
        "risk_amount": round(risk_amount, 2),
        "stop_distance": round(dist, 6),
        "units": round(units, 6),
        "lots": round(lots, 4),
        "units_per_lot": spec["units_per_lot"],
        "notional": round(notional, 2),
        "est_round_turn_cost": round(cost, 2),
        "cost_pct_of_risk": round(100 * cost / risk_amount, 1) if risk_amount else None,
        "equity_is_placeholder": config.ACCOUNT["equity_is_placeholder"],
        "spread_is_estimated": config.COST["spread_is_estimated"],
    }


def cost_check(entry, target_price, sizing, min_multiple=None):
    """Does the first target clear estimated round-turn cost by enough?"""
    min_multiple = min_multiple or config.MIN_COST_MULTIPLE
    if not sizing or "units" not in sizing:
        return {"ok": False, "reason": "sizing unavailable"}
    gross = abs(target_price - entry) * sizing["units"]
    cost = sizing["est_round_turn_cost"]
    if cost <= 0:
        return {"ok": True, "multiple": None}
    mult = gross / cost
    return {
        "ok": bool(mult >= min_multiple),
        "multiple": round(mult, 1),
        "gross_at_t1": round(gross, 2),
        "est_cost": round(cost, 2),
        "reason": None if mult >= min_multiple else
                  f"T1 gross {gross:.2f} is only {mult:.1f}x estimated cost "
                  f"{cost:.2f} (need {min_multiple}x)",
    }


def realized_vol(df, lookback=50):
    """Std-dev of bar-to-bar close changes, in price units."""
    if df is None or len(df) < lookback + 1:
        return None
    return float(df["close"].diff().tail(lookback).std())
