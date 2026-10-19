"""
Orchestration: turns validated frames into a decision for each trading profile
(SWING and SCALP), long or short.

Honest naming: the field previously called `probability` was
passed_checks / total_checks * 100. That is a confirmation-density ratio, not
a probability — nothing in the system has ever been calibrated against
realised outcomes. It is called `confluence_score` here, and the report
carries an explicit note saying so.
"""

import numpy as np
import pandas as pd

import config
import dataio
import indicators as ind
import risk as risk_mod
import smc


# ------------------------------------------------------------- PRIMITIVES ---

def _last(df, col):
    if col not in df or not len(df):
        return None
    v = df[col].iloc[-1]
    return None if pd.isna(v) else float(v)


def ema_trend(df):
    e20, e50 = _last(df, "ema20"), _last(df, "ema50")
    e100, e200 = _last(df, "ema100"), _last(df, "ema200")
    have = [x for x in (e20, e50, e100, e200) if x is not None]
    if len(have) < 2:
        return "UNKNOWN"
    stack = [x for x in (e20, e50, e100, e200) if x is not None]
    if all(stack[i] > stack[i + 1] for i in range(len(stack) - 1)):
        return "BULLISH"
    if all(stack[i] < stack[i + 1] for i in range(len(stack) - 1)):
        return "BEARISH"
    return "MIXED"


def candle_trigger(df, direction):
    """Confirmed reversal/continuation candle on the last CLOSED bar."""
    if len(df) < 2:
        return None
    o, h, l, c = (df["open"].iloc[-1], df["high"].iloc[-1],
                  df["low"].iloc[-1], df["close"].iloc[-1])
    po, pc = df["open"].iloc[-2], df["close"].iloc[-2]
    body = abs(c - o)
    rng = (h - l) or 1e-9
    upper, lower = h - max(c, o), min(c, o) - l

    if direction == "LONG":
        if c > po and o < pc and c > o and pc < po:
            return "bullish engulfing"
        if lower > 2 * body and upper < body and c >= o:
            return "hammer / long lower wick rejection"
        if c > o and body / rng > 0.6:
            return "strong bullish close"
    else:
        if c < po and o > pc and c < o and pc > po:
            return "bearish engulfing"
        if upper > 2 * body and lower < body and c <= o:
            return "shooting star / long upper wick rejection"
        if c < o and body / rng > 0.6:
            return "strong bearish close"
    return None


# ------------------------------------------------------------ BIAS/REGIME ---

def determine_bias(frames_i, timeframes):
    votes, detail = [], {}
    for tf in timeframes:
        d = frames_i.get(tf)
        if d is None or not len(d):
            continue
        et = ema_trend(d)
        highs, lows = smc.swing_points(d)
        st = smc.structure_state(highs, lows)["trend"]
        v = 0
        if et == "BULLISH":
            v += 1
        elif et == "BEARISH":
            v -= 1
        if st == "UPTREND":
            v += 1
        elif st == "DOWNTREND":
            v -= 1
        votes.append(v)
        detail[tf] = {"ema": et, "structure": st, "vote": v}

    if not votes:
        return "UNKNOWN", detail
    total = sum(votes)
    bias = "BULLISH" if total >= 2 else "BEARISH" if total <= -2 else "MIXED"
    return bias, detail


# The SMC structures that actually justify an ENTRY. Everything else in a
# zone's `kinds` (equal levels, session extremes, VWAP, round numbers, HVN)
# is confluence around that structure, or liquidity to target -- not a
# reason to enter on its own.
ENTRY_STRUCTURES = ("order_block", "unfilled_fvg", "breaker")


def smc_first_kinds(kinds, n=3):
    """Label a zone with the structure that qualified it FIRST.

    A zone's `kinds` list is built by merging overlapping detections, so the
    order block or FVG that made it a valid entry can end up anywhere in it.
    Labelling with a raw kinds[:n] slice then hides the actual reason --
    an FVG-anchored zone rendering as "anchored_vwap/equal_lows/session_vwap"
    reads like the engine entered at a VWAP line. Shared by the checklist
    note and the entry_type string so the two can't drift apart.
    """
    structure = next((k for k in kinds if k in ENTRY_STRUCTURES), None)
    if not structure:
        return list(kinds[:n])
    return [structure] + [k for k in kinds if k != structure][:max(0, n - 1)]


# ------------------------------------------------------------ EVALUATION ----

def evaluate_direction(asset, direction, frames, frames_i, zones, price,
                       atr_val, atr_tf, profile, profile_name, bias,
                       bias_detail, structure, regime, pd_zone, sweeps):
    """Build the confirmation checklist for one direction and score it."""
    want_bull = direction == "LONG"
    checks = []

    def add(name, ok, weight, note=""):
        checks.append({"name": name, "passed": bool(ok),
                       "weight": weight, "note": note})

    # 1. Higher-timeframe bias
    aligned = (bias == "BULLISH") if want_bull else (bias == "BEARISH")
    add("HTF bias alignment", aligned, 18,
        f"bias={bias} from {'/'.join(profile['bias_timeframes'])}")

    # 2. Structure event
    ev = structure["event"]
    want_ev = ("BOS_BULLISH", "CHOCH_BULLISH") if want_bull else ("BOS_BEARISH", "CHOCH_BEARISH")
    add("Structure break (BOS/CHoCH)", ev in want_ev, 16,
        f"{ev} on {structure['timeframe']}" +
        (f" @ {structure['level']:.4f}" if structure.get("level") else ""))

    # 3. Liquidity sweep
    want_impl = "bullish" if want_bull else "bearish"
    sweep = next((s for s in sweeps if s["implication"] == want_impl), None)
    add("Liquidity sweep + reclaim", sweep is not None, 16,
        (f"{sweep['type']} @ {sweep['level']:.4f}, {sweep['bars_ago']} bars ago, "
         f"pen {sweep['penetration_atr']} ATR") if sweep else "none in window")

    # 4. Entry near a qualified zone on the correct side
    #
    # SMC-proper: an ENTRY happens at an order block or FVG (the actual
    # footprint of the move that created the imbalance) or a breaker (a
    # failed order block, same family, flipped polarity). Equal highs/lows,
    # swing points and session extremes are LIQUIDITY -- price gets drawn
    # toward them, which is exactly why select_targets() already uses this
    # same `zones` list for targets. Letting those same marks also qualify
    # as an ENTRY (the pre-fix behaviour) meant a setup could get "entered"
    # at a pool it should have been aiming AT. Live evidence before this fix:
    # 2 of 6 signals running right now were anchored at a bare
    # session-extreme/equal-level zone with no order block or FVG anywhere
    # in it (e.g. "asia_session_low/equal_lows"), not a real SMC entry
    # structure. A zone that merges an OB/FVG/breaker WITH other confluence
    # marks (equal levels, VWAP, round numbers, session extremes) still
    # qualifies and is still preferred by strength -- those marks remain
    # valid confluence, just not sufficient on their own.
    want_side = "sell_side" if want_bull else "buy_side"
    near = [z for z in zones
            if z["side"] == want_side and abs(z["distance_atr"]) <= 1.5
            and any(k in ENTRY_STRUCTURES for k in z["kinds"])]
    best_zone = max(near, key=lambda z: z["strength"]) if near else None
    add("Price at qualified order block / FVG", best_zone is not None, 15,
        (f"{'/'.join(smc_first_kinds(best_zone['kinds'], 3))} "
         f"str={best_zone['strength']} "
         f"@{best_zone['low']:.4f}-{best_zone['high']:.4f}") if best_zone else "no order block/FVG/breaker within 1.5 ATR")

    # 5. Premium / discount
    pdl = pd_zone.get("label")
    pd_ok = (pdl in ("DISCOUNT", "EQUILIBRIUM")) if want_bull else (pdl in ("PREMIUM", "EQUILIBRIUM"))
    add("Premium/discount position", pd_ok, 10,
        f"{pdl} (pos={pd_zone.get('position')})")

    # 6. Regime
    reg_ok = regime["regime"] in ("TRENDING", "TRANSITIONAL", "COMPRESSION")
    add("Regime supportive", reg_ok, 8,
        f"{regime['regime']} (ADX {regime['adx']})")

    # 7-9. Momentum / trend on entry timeframe
    etf = profile["entry_timeframes"][0]
    ed = frames_i.get(etf)
    if ed is not None and len(ed):
        rsi_v = _last(ed, "rsi14")
        if rsi_v is not None:
            rsi_ok = (45 <= rsi_v <= 72) if want_bull else (28 <= rsi_v <= 55)
            add("RSI not exhausted", rsi_ok, 8, f"RSI({etf})={rsi_v:.1f}")
        macd_v, macd_s = _last(ed, "macd"), _last(ed, "macd_signal")
        if macd_v is not None and macd_s is not None:
            add("MACD alignment", (macd_v > macd_s) if want_bull else (macd_v < macd_s),
                7, f"macd={macd_v:.4f} signal={macd_s:.4f}")
        st_tr = _last(ed, "st_trend")
        if st_tr is not None:
            add("SuperTrend agreement", (st_tr > 0) if want_bull else (st_tr < 0),
                6, f"{'UP' if st_tr > 0 else 'DOWN'} on {etf}")

    # 10. Volume / OBV
    if ed is not None and "obv" in ed and len(ed) > 25:
        o_now, o_then = ed["obv"].iloc[-1], ed["obv"].iloc[-20]
        zero_vol = int((ed["volume"].tail(50) == 0).sum())
        ok = (o_now > o_then) if want_bull else (o_now < o_then)
        add("Volume/OBV support", ok and zero_vol < 25, 7,
            f"OBV {'rising' if o_now > o_then else 'falling'}"
            + (f"; {zero_vol}/50 zero-volume bars — degraded" if zero_vol else ""))

    # 11. Session context
    sess = ind.session_of(frames[etf]["time"].iloc[-1]) if etf in frames else "Unknown"
    add("Active session", sess in ("London", "NewYork"), 5, f"last bar in {sess}")

    # 12. Candle trigger
    trig = candle_trigger(frames[etf], direction) if etf in frames else None
    add("Entry candle trigger", trig is not None, 9, trig or "no confirmed trigger")

    passed_w = sum(c["weight"] for c in checks if c["passed"])
    total_w = sum(c["weight"] for c in checks) or 1
    score = 100.0 * passed_w / total_w

    counter_trend = (bias in ("BULLISH", "BEARISH") and not aligned)
    if counter_trend:
        score -= config.COUNTER_TREND_SCORE_PENALTY

    return {
        "checks": checks,
        "passed": sum(1 for c in checks if c["passed"]),
        "total": len(checks),
        "confluence_score": round(float(np.clip(score, 0, 100)), 1),
        "counter_trend": counter_trend,
        "best_zone": best_zone,
        "sweep": sweep,
        "trigger": trig,
        "session": sess,
    }


def build_setup(asset, direction, frames, frames_i, zones, price, atr_val,
                atr_tf, profile, profile_name, evaluation, structure, sweeps):
    """Compute entry, stops, targets, sizing and the final decision."""
    etf = profile["entry_timeframes"][0]
    ed = frames.get(etf)

    zone = evaluation["best_zone"]
    if zone:
        entry = (zone["low"] + zone["high"]) / 2
        # The zone was qualified above because it contains an order block,
        # FVG or breaker -- but that mark can sit anywhere in zone["kinds"]
        # after merging with other confluence (session extremes, equal
        # levels, VWAP, round numbers), and kinds[:2] doesn't necessarily
        # include it. A label like "asia_session_low/equal_lows" for a zone
        # that IS anchored on a real order block hides the actual reason the
        # entry qualified. Put the SMC structure that justified the entry
        # first, then up to one more piece of confluence for context.
        entry_kind = f"limit at {'/'.join(smc_first_kinds(zone['kinds'], 2))} zone"
    else:
        entry = price
        entry_kind = "market at current price"

    extended = abs(price - entry) > config.EXECUTION["entry_zone_atr"] * atr_val
    struct_level = structure.get("level")
    if struct_level is None:
        highs, lows = smc.swing_points(frames_i[etf])
        if direction == "LONG" and lows:
            struct_level = lows[-1][1]
        elif direction == "SHORT" and highs:
            struct_level = highs[-1][1]

    sweep = evaluation.get("sweep")
    swept = sweep["level"] if sweep else None
    rvol = risk_mod.realized_vol(ed)

    cands = risk_mod.stop_candidates(asset, direction, entry, atr_val,
                                     struct_level, swept, rvol, profile,
                                     entry_zone=zone)
    stop_info = risk_mod.select_stop(asset, direction, entry, cands, zones,
                                     atr_val, profile)
    if not stop_info:
        return None, ["could not derive any stop candidate"]

    targets, primary_rr, tnote = risk_mod.select_targets(
        direction, entry, stop_info["stop"], zones, atr_val, profile["min_rr"],
        max_target_atr=profile.get("max_target_atr"))

    sizing = risk_mod.position_size(asset, entry, stop_info["stop"])
    max_risk_stop = None
    if sizing.get("units"):
        max_risk_stop = sizing["risk_amount"]

    cost = risk_mod.cost_check(entry, targets[0]["price"], sizing) if targets else \
        {"ok": False, "reason": "no target"}

    # ---- decision gates ----
    blockers = []
    if evaluation["confluence_score"] < profile["min_confluence_score"]:
        blockers.append(
            f"confluence {evaluation['confluence_score']} < "
            f"{profile['min_confluence_score']} required")
    if evaluation["passed"] < profile["min_confirmations"]:
        blockers.append(
            f"only {evaluation['passed']} confirmations "
            f"(need {profile['min_confirmations']})")
    if stop_info["too_wide"]:
        blockers.append(
            f"stop {stop_info['distance_atr']} ATR exceeds max "
            f"{profile['max_stop_atr']} ATR")
    if not targets:
        blockers.append(tnote or "no valid targets")
    elif primary_rr < profile["min_rr"]:
        blockers.append(
            f"T1 R:R {primary_rr} below minimum {profile['min_rr']}")
    if targets and not cost.get("ok"):
        blockers.append(cost.get("reason") or "target does not clear costs")
    if evaluation["counter_trend"] and not profile["allow_counter_htf"]:
        blockers.append("counter-trend to HTF bias, disallowed for this profile")

    setup = {
        "direction": direction,
        "entry": round(float(entry), 6),
        "entry_type": entry_kind,
        "entry_is_extended": bool(extended),
        "structural_stop": round(float(cands.get("structural", stop_info["stop"])), 6),
        "atr_stop": round(float(cands.get("atr_2x")), 6) if "atr_2x" in cands else None,
        "volatility_stop": round(float(cands["volatility_2sigma"]), 6)
                           if "volatility_2sigma" in cands else None,
        "sweep_stop": round(float(cands["beyond_sweep"]), 6)
                      if "beyond_sweep" in cands else None,
        "selected_stop": stop_info["stop"],
        "basis": stop_info.get("basis"),
        "stop_distance": stop_info["distance"],
        "stop_distance_atr": stop_info["distance_atr"],
        "stop_moved_off_liquidity": stop_info["moved_off_liquidity"],
        "stop_move_reason": stop_info["moved_reason"],
        "max_risk_amount": max_risk_stop,
        "targets": targets,
        "primary_rr": primary_rr,
        "target_note": tnote,
        "position_size": sizing,
        "cost_check": cost,
        "invalidation": (
            f"a confirmed {etf} close "
            f"{'below' if direction == 'LONG' else 'above'} "
            f"{stop_info['stop']:.4f} invalidates the idea; the structural "
            f"premise fails if "
            f"{'support' if direction == 'LONG' else 'resistance'} at "
            f"{struct_level:.4f} is lost" if struct_level else
            f"a confirmed {etf} close beyond {stop_info['stop']:.4f}"),
        "expected_hold": profile["expected_hold"],
        "blockers": blockers,
    }
    return setup, blockers


# ------------------------------------------------------------------ MAIN ----

def analyze_asset(name):
    """Live path: fetch the current market data, then run the shared analysis."""
    frames, reports, price, meta = dataio.load_asset(name)
    hard, soft = dataio.data_blockers(reports)
    return analyze_frames(name, frames, price, meta=meta, reports=reports,
                          hard=hard, soft=soft)


def analyze_frames(name, frames, price, meta=None, reports=None, hard=None,
                   soft=None, frames_i=None, availability=None):
    """Run the analysis on caller-supplied frames.

    This is the seam the backtester replays through, so a historical run
    exercises the IDENTICAL decision logic as the live loop rather than a
    parallel reimplementation that could silently drift from it.

    `frames_i` (precomputed indicators) may be supplied to skip recomputation.
    That is only sound because indicators here are strictly causal — the
    no-lookahead suite asserts every indicator's value at bar N is identical
    whether or not later bars exist — so slicing a once-computed indicator
    frame is equivalent to recomputing it on the truncated frame, just far
    cheaper across thousands of replay steps.

    Historical replay passes hard=[] deliberately: the live data gate rejects
    bars that are old relative to NOW, which is true of all history by
    definition and would refuse every backtest bar.
    """
    reports = {} if reports is None else reports
    hard = [] if hard is None else hard
    soft = [] if soft is None else soft

    if frames_i is None:
        frames_i, availability = {}, {}
        for tf, df in frames.items():
            if len(df) >= 30:
                frames_i[tf], availability[tf] = ind.compute_all(df, tf)
    else:
        availability = availability or {}

    meta = meta or {"asset": name}

    result = {
        "asset": name,
        "meta": meta,
        "current_price": round(float(price), 6),
        "data_quality": {
            "per_timeframe": reports,
            "hard_blockers": hard,
            "warnings": sorted(set(soft)),
            "indicator_availability": availability,
        },
        "unavailable_data": config.UNAVAILABLE_DATA,
        "profiles": {},
        "scoring_note": (
            "confluence_score is a weighted confirmation-density ratio. It is "
            "NOT a calibrated probability — no component has been validated "
            "against realised trade outcomes."),
    }

    if hard:
        for pname in config.PROFILES:
            result["profiles"][pname] = {
                "decision": "NO TRADE",
                "reason": "data integrity gate failed",
                "blockers": hard,
            }
        return result

    for pname, profile in config.PROFILES.items():
        atr_tf = profile["atr_timeframe"]
        adf = frames_i.get(atr_tf)
        atr_val = _last(adf, "atr14") if adf is not None else None
        if not atr_val:
            result["profiles"][pname] = {
                "decision": "NO TRADE",
                "reason": f"ATR unavailable on {atr_tf}",
                "blockers": [f"ATR unavailable on {atr_tf}"]}
            continue

        bias, bias_detail = determine_bias(frames_i, profile["bias_timeframes"])

        stf = profile["structure_timeframes"][0]
        sdf, sdf_i = frames[stf], frames_i[stf]
        highs, lows = smc.swing_points(sdf)
        st_state = smc.structure_state(highs, lows)
        st_event = smc.bos_choch(sdf, highs, lows, atr_val)
        st_event["timeframe"] = stf
        regime = smc.market_regime(sdf_i)
        pd_zone = smc.premium_discount(sdf, highs, lows, price)
        sweeps = smc.detect_sweeps(sdf, highs, lows, atr_val)

        zones, znotes = smc.build_liquidity_zones(
            name, frames, frames_i, price, atr_val, atr_tf)
        above, below = smc.nearest_zones(zones, price)
        draw = smc.liquidity_draw(zones, price, st_state["trend"], atr_val)

        best = None
        for direction in ("LONG", "SHORT"):
            ev = evaluate_direction(name, direction, frames, frames_i, zones,
                                    price, atr_val, atr_tf, profile, pname,
                                    bias, bias_detail, st_event, regime,
                                    pd_zone, sweeps)
            setup, blockers = build_setup(name, direction, frames, frames_i,
                                          zones, price, atr_val, atr_tf,
                                          profile, pname, ev, st_event, sweeps)
            cand = {"direction": direction, "evaluation": ev, "setup": setup,
                    "blockers": blockers}
            if best is None or ev["confluence_score"] > best["evaluation"]["confluence_score"]:
                best = cand

        # Vocabulary:
        #   LONG/SHORT — every gate passed AND price is at the entry.
        #   WAIT       — every gate passed but price has not reached the entry
        #                zone yet, so the order is resting, not live.
        #   NO TRADE   — at least one gate failed.
        if best["setup"] is None:
            decision = "NO TRADE"
        elif best["blockers"]:
            decision = "NO TRADE"
        elif best["setup"].get("entry_is_extended"):
            decision = "WAIT"
        else:
            decision = best["direction"]

        reasons_for = [f"{c['name']}: {c['note']}"
                       for c in best["evaluation"]["checks"] if c["passed"]]
        reasons_against = [f"{c['name']}: {c['note']}"
                           for c in best["evaluation"]["checks"] if not c["passed"]]

        result["profiles"][pname] = {
            "decision": decision,
            "direction_considered": best["direction"],
            "expected_hold": profile["expected_hold"],
            "market_regime": regime,
            "htf_bias": bias,
            "htf_bias_detail": bias_detail,
            "structure": st_state,
            "structure_event": st_event,
            "premium_discount": pd_zone,
            "sweeps": sweeps,
            "nearest_buy_side": above,
            "nearest_sell_side": below,
            "strongest_zone": zones[0] if zones else None,
            "expected_draw": draw,
            "liquidity_zones": zones,
            "zone_notes": znotes,
            "confluence_score": best["evaluation"]["confluence_score"],
            "confirmations_passed": best["evaluation"]["passed"],
            "confirmations_total": best["evaluation"]["total"],
            "counter_trend": best["evaluation"]["counter_trend"],
            "setup": best["setup"],
            "blockers": best["blockers"],
            "reasons_for": reasons_for,
            "reasons_against": reasons_against,
        }

    return result
