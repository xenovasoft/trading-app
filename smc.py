"""
Market structure and liquidity mapping.

Terminology (used consistently throughout):
  buy-side liquidity  = resting buy orders / short stops -> sits ABOVE price
                        (built by highs). Price is drawn UP into it.
  sell-side liquidity = resting sell orders / long stops -> sits BELOW price
                        (built by lows). Price is drawn DOWN into it.

Every function here operates on CLOSED bars supplied by dataio. A swing point
requires `swing_right` bars after it before it is considered confirmed, so no
swing is ever reported until the market has actually validated it.
"""

import numpy as np
import pandas as pd

import config
import indicators as ind


# ------------------------------------------------------------- STRUCTURE ---

def swing_points(df, left=None, right=None):
    """Confirmed fractal swings. A pivot at i is only emitted once `right`
    further bars exist, so this cannot look ahead of the current bar."""
    left = left or config.STRUCTURE["swing_left"]
    right = right or config.STRUCTURE["swing_right"]
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    win = left + right + 1
    if len(h) < win:
        return [], []

    # Vectorised equivalent of the original per-bar loop. Testing
    # `argmax == left` alone is exactly the old `h[i] == win.max() and
    # argmax == left` pair, because argmax already returns the FIRST maximum:
    # an equal-or-higher value earlier in the window fails both forms alike.
    # This is the backtester's hot path (millions of window scans) and the
    # scalar df["time"].iloc[i] lookup it replaces dominated the profile.
    times = df["time"].to_numpy()
    sw_h = np.lib.stride_tricks.sliding_window_view(h, win)
    sw_l = np.lib.stride_tricks.sliding_window_view(l, win)
    hi_idx = np.flatnonzero(sw_h.argmax(axis=1) == left) + left
    lo_idx = np.flatnonzero(sw_l.argmin(axis=1) == left) + left

    highs = [(int(i), float(h[i]), pd.Timestamp(times[i])) for i in hi_idx]
    lows = [(int(i), float(l[i]), pd.Timestamp(times[i])) for i in lo_idx]
    return highs, lows


def structure_state(highs, lows):
    """Classify the recent swing sequence into a trend label."""
    hs = [v for _, v, _ in highs[-4:]]
    ls = [v for _, v, _ in lows[-4:]]
    if len(hs) < 2 or len(ls) < 2:
        return {"trend": "UNDEFINED", "highs": [], "lows": [], "detail": "too few swings"}

    h_seq = ["HH" if hs[i] > hs[i - 1] else "LH" for i in range(1, len(hs))]
    l_seq = ["HL" if ls[i] > ls[i - 1] else "LL" for i in range(1, len(ls))]

    up = h_seq[-1] == "HH" and l_seq[-1] == "HL"
    down = h_seq[-1] == "LH" and l_seq[-1] == "LL"
    trend = "UPTREND" if up else "DOWNTREND" if down else "RANGE/TRANSITION"
    return {"trend": trend, "highs": h_seq, "lows": l_seq,
            "detail": f"highs {'>'.join(h_seq)} / lows {'>'.join(l_seq)}"}


def bos_choch(df, highs, lows, atr_val):
    """Break of structure vs change of character, with a displacement filter.

    A close must clear the level by `bos_displacement_atr` * ATR. Without this
    a one-tick poke counted as a structural break, which is the single biggest
    source of false structure signals in the original implementation.
    """
    if len(highs) < 2 or len(lows) < 2 or not atr_val or np.isnan(atr_val):
        return {"event": "INSUFFICIENT_DATA", "level": None, "bars_ago": None}

    buf = config.STRUCTURE["bos_displacement_atr"] * atr_val
    close = df["close"].to_numpy()
    last_close = close[-1]

    last_h, prev_h = highs[-1][1], highs[-2][1]
    last_l, prev_l = lows[-1][1], lows[-2][1]

    if last_close > last_h + buf:
        event = "BOS_BULLISH" if last_h >= prev_h else "CHOCH_BULLISH"
        return {"event": event, "level": last_h,
                "bars_ago": len(df) - 1 - highs[-1][0]}
    if last_close < last_l - buf:
        event = "BOS_BEARISH" if last_l <= prev_l else "CHOCH_BEARISH"
        return {"event": event, "level": last_l,
                "bars_ago": len(df) - 1 - lows[-1][0]}
    return {"event": "NO_BREAK", "level": None, "bars_ago": None}


def premium_discount(df, highs, lows, price):
    """Where price sits within the most recent confirmed dealing range."""
    if not highs or not lows:
        return {"position": None, "label": "UNKNOWN", "range_high": None,
                "range_low": None}
    rh = max(v for _, v, _ in highs[-3:])
    rl = min(v for _, v, _ in lows[-3:])
    if rh <= rl:
        return {"position": None, "label": "UNKNOWN", "range_high": rh, "range_low": rl}
    pos = (price - rl) / (rh - rl)
    label = ("DISCOUNT" if pos < 0.4 else
             "PREMIUM" if pos > 0.6 else "EQUILIBRIUM")
    return {"position": round(float(pos), 3), "label": label,
            "range_high": float(rh), "range_low": float(rl),
            "equilibrium": float((rh + rl) / 2)}


def market_regime(d):
    """Trend / range / compression, from ADX + Bollinger bandwidth."""
    adx_v = d["adx14"].iloc[-1] if "adx14" in d else np.nan
    bw = d["bb_bandwidth"].iloc[-1] if "bb_bandwidth" in d else np.nan
    bw_med = d["bb_bandwidth"].tail(120).median() if "bb_bandwidth" in d else np.nan

    compressed = (pd.notna(bw) and pd.notna(bw_med) and bw_med > 0 and
                  bw < config.STRUCTURE["bb_squeeze_pct"] * bw_med)
    if pd.isna(adx_v):
        regime = "UNKNOWN"
    elif adx_v >= config.STRUCTURE["regime_adx_trend"]:
        regime = "TRENDING"
    elif adx_v <= config.STRUCTURE["regime_adx_range"]:
        regime = "RANGING"
    else:
        regime = "TRANSITIONAL"
    if compressed and regime != "TRENDING":
        regime = "COMPRESSION"
    return {"regime": regime,
            "adx": None if pd.isna(adx_v) else round(float(adx_v), 1),
            "bandwidth": None if pd.isna(bw) else round(float(bw), 5),
            "compressed": bool(compressed)}


# ------------------------------------------------------- LIQUIDITY EVENTS ---

def detect_sweeps(df, highs, lows, atr_val):
    """Find sweeps in the recent window: price trades beyond a prior swing
    extreme then CLOSES back inside within `sweep_reclaim_bars`.

    The original checked only the single most recent bar, so any sweep more
    than one bar old was invisible.
    """
    if not atr_val or np.isnan(atr_val):
        return []
    look = config.LIQUIDITY["sweep_lookback_bars"]
    reclaim = config.LIQUIDITY["sweep_reclaim_bars"]
    n = len(df)
    start = max(1, n - look)
    out = []
    high, low, close = (df["high"].to_numpy(), df["low"].to_numpy(),
                        df["close"].to_numpy())

    for i in range(start, n):
        for idx, lvl, ts in reversed(highs):
            if idx >= i:
                continue
            if high[i] > lvl:
                window = close[i:min(n, i + reclaim + 1)]
                if len(window) and window.min() < lvl:
                    out.append({
                        "type": "buy_side_sweep",   # swept highs, rejected -> bearish
                        "level": float(lvl), "bars_ago": n - 1 - i,
                        "penetration_atr": round(float((high[i] - lvl) / atr_val), 2),
                        "reclaimed": True,
                        "implication": "bearish",
                    })
                break
        for idx, lvl, ts in reversed(lows):
            if idx >= i:
                continue
            if low[i] < lvl:
                window = close[i:min(n, i + reclaim + 1)]
                if len(window) and window.max() > lvl:
                    out.append({
                        "type": "sell_side_sweep",  # swept lows, rejected -> bullish
                        "level": float(lvl), "bars_ago": n - 1 - i,
                        "penetration_atr": round(float((lvl - low[i]) / atr_val), 2),
                        "reclaimed": True,
                        "implication": "bullish",
                    })
                break

    seen, dedup = set(), []
    for s in sorted(out, key=lambda x: x["bars_ago"]):
        key = (s["type"], round(s["level"], 4))
        if key not in seen:
            seen.add(key)
            dedup.append(s)
    return dedup[:6]


def detect_fvgs(df, atr_val):
    """Fair value gaps with mitigation tracking. Only UNFILLED gaps are
    returned as live liquidity — the original reported gaps price had already
    traded back through."""
    if not atr_val or np.isnan(atr_val):
        return []
    look = config.LIQUIDITY["fvg_lookback_bars"]
    min_size = config.LIQUIDITY["fvg_min_size_atr"] * atr_val
    n = len(df)
    start = max(2, n - look)
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    out = []

    for i in range(start, n):
        h0, l0 = high[i - 2], low[i - 2]
        h2, l2 = high[i], low[i]
        gap = None
        if l2 > h0 and (l2 - h0) >= min_size:
            gap = {"direction": "bullish", "low": float(h0), "high": float(l2)}
        elif h2 < l0 and (l0 - h2) >= min_size:
            gap = {"direction": "bearish", "low": float(h2), "high": float(l0)}
        if not gap:
            continue

        after_low = low[i + 1:]
        after_high = high[i + 1:]
        if len(after_low):
            if gap["direction"] == "bullish":
                filled_frac = ((gap["high"] - after_low.min()) /
                               (gap["high"] - gap["low"]))
            else:
                filled_frac = ((after_high.max() - gap["low"]) /
                               (gap["high"] - gap["low"]))
        else:
            filled_frac = 0.0
        filled_frac = float(np.clip(filled_frac, 0.0, 1.0))

        if filled_frac < 0.9:
            gap.update({
                "bars_ago": n - 1 - i,
                "filled_fraction": round(filled_frac, 2),
                "size_atr": round((gap["high"] - gap["low"]) / atr_val, 2),
                "time": str(df["time"].iloc[i]),
            })
            out.append(gap)

    return sorted(out, key=lambda g: g["bars_ago"])[:8]


def detect_order_blocks(df, atr_val):
    """Order blocks requiring a genuine displacement candle, plus breaker
    detection and mitigation state.

    The original accepted any candle whose successor had a larger body — a
    condition met constantly by ordinary noise.
    """
    if not atr_val or np.isnan(atr_val):
        return []
    look = config.LIQUIDITY["ob_lookback_bars"]
    disp_min = config.LIQUIDITY["ob_displacement_atr"] * atr_val
    n = len(df)
    start = max(1, n - look)
    o, h, l, c = (df["open"].to_numpy(), df["high"].to_numpy(),
                  df["low"].to_numpy(), df["close"].to_numpy())
    out = []

    for i in range(start, n - 1):
        body = c[i] - o[i]
        nxt_move = c[i + 1] - o[i + 1]
        nxt_range = h[i + 1] - l[i + 1]
        if nxt_range < disp_min:
            continue

        kind = None
        if body < 0 and nxt_move > 0 and (c[i + 1] > h[i]):
            kind = "bullish"
        elif body > 0 and nxt_move < 0 and (c[i + 1] < l[i]):
            kind = "bearish"
        if not kind:
            continue

        zlo, zhi = float(l[i]), float(h[i])
        after_low, after_high = l[i + 2:], h[i + 2:]
        mitigated, broken = False, False
        if len(after_low):
            if kind == "bullish":
                mitigated = bool(after_low.min() <= zhi)
                broken = bool(c[i + 2:].min() < zlo) if len(c[i + 2:]) else False
            else:
                mitigated = bool(after_high.max() >= zlo)
                broken = bool(c[i + 2:].max() > zhi) if len(c[i + 2:]) else False

        out.append({
            "direction": kind,
            "low": zlo, "high": zhi,
            "bars_ago": n - 1 - i,
            "mitigated": mitigated,
            "broken": broken,
            "is_breaker": broken,          # a failed OB flips polarity
            "displacement_atr": round(float(nxt_range / atr_val), 2),
            "time": str(df["time"].iloc[i]),
        })

    return sorted(out, key=lambda b: b["bars_ago"])[:8]


def equal_levels(highs, lows, atr_val):
    """Equal highs/lows using an ATR-relative tolerance. The original used a
    fixed 0.15% relative tolerance, which means something completely
    different on silver ($0.09) than on BTC ($97)."""
    if not atr_val or np.isnan(atr_val):
        return [], []
    tol = config.LIQUIDITY["equal_level_atr_tol"] * atr_val

    def _pairs(pts):
        """All (a, b) with b > a whose levels sit within tol.

        np.triu_indices(k=1) enumerates in the same row-major order the old
        nested a/b loops did, so the trailing [-6:] slice keeps selecting the
        same six pairs. The pairwise comparison was O(n^2) in Python and
        showed up as ~50M abs() calls in the backtest profile.
        """
        if len(pts) < 2:
            return []
        idx = np.fromiter((i for i, _ in pts), dtype=np.int64, count=len(pts))
        val = np.fromiter((v for _, v in pts), dtype=float, count=len(pts))
        a, b = np.triu_indices(len(pts), k=1)
        keep = np.flatnonzero(np.abs(val[b] - val[a]) <= tol)
        return [{"price": float((val[a[k]] + val[b[k]]) / 2), "count": 2,
                 "indices": [int(idx[a[k]]), int(idx[b[k]])]} for k in keep]

    hv = [(i, v) for i, v, _ in highs]
    lv = [(i, v) for i, v, _ in lows]
    return _pairs(hv)[-6:], _pairs(lv)[-6:]


# --------------------------------------------------------- ZONE ASSEMBLY ---

def _round_levels(asset, price, atr_val):
    steps = config.LIQUIDITY["round_number_steps"].get(asset, [])
    out = []
    for step in steps:
        base = round(price / step) * step
        for k in (-1, 0, 1):
            lvl = base + k * step
            if lvl > 0 and abs(lvl - price) <= 6 * (atr_val or step):
                out.append((float(lvl), step))
    return out


def _count_tests(df, level, tol, from_idx=0):
    seg = df.iloc[from_idx:]
    if not len(seg):
        return 0
    touched = ((seg["low"] <= level + tol) & (seg["high"] >= level - tol))
    # count contiguous touch groups rather than raw bars
    return int((touched & ~touched.shift(1, fill_value=False)).sum())


def build_liquidity_zones(asset, frames, indi, price, atr_val, atr_tf):
    """Detect, merge, score and rank liquidity zones across timeframes."""
    if not atr_val or np.isnan(atr_val) or atr_val <= 0:
        return [], {"error": "ATR unavailable; cannot build zones"}

    W = config.ZONE_WEIGHTS
    tol = config.LIQUIDITY["cluster_merge_atr"] * atr_val
    raw = []

    def add(price_lo, price_hi, side, kind, weight, tf, meta=None):
        raw.append({
            "low": float(min(price_lo, price_hi)),
            "high": float(max(price_lo, price_hi)),
            "side": side, "kinds": [kind], "weight": float(weight),
            "timeframes": [tf], "meta": meta or {},
        })

    for tf in ["15M", "1H", "4H", "Daily"]:
        if tf not in frames or len(frames[tf]) < 30:
            continue
        df = frames[tf]
        htf_bonus = W["htf_confluence"] if tf in ("4H", "Daily") else 0
        highs, lows = swing_points(df)

        for _, v, _ in highs[-5:]:
            add(v, v, "buy_side", "swing_high", W["swing_point"] + htf_bonus, tf)
        for _, v, _ in lows[-5:]:
            add(v, v, "sell_side", "swing_low", W["swing_point"] + htf_bonus, tf)

        eqh, eql = equal_levels(highs, lows, atr_val)
        for e in eqh:
            add(e["price"], e["price"], "buy_side", "equal_highs",
                W["equal_levels"] + htf_bonus, tf, {"count": e["count"]})
        for e in eql:
            add(e["price"], e["price"], "sell_side", "equal_lows",
                W["equal_levels"] + htf_bonus, tf, {"count": e["count"]})

        for g in detect_fvgs(df, atr_val):
            side = "sell_side" if g["direction"] == "bullish" else "buy_side"
            add(g["low"], g["high"], side, "unfilled_fvg",
                W["unfilled_fvg"] + htf_bonus, tf,
                {"filled_fraction": g["filled_fraction"], "bars_ago": g["bars_ago"]})

        for b in detect_order_blocks(df, atr_val):
            if b["is_breaker"]:
                side = "buy_side" if b["direction"] == "bullish" else "sell_side"
                add(b["low"], b["high"], side, "breaker", W["breaker"] + htf_bonus,
                    tf, {"bars_ago": b["bars_ago"]})
            else:
                side = "sell_side" if b["direction"] == "bullish" else "buy_side"
                w = W["order_block"] + htf_bonus
                if not b["mitigated"]:
                    w += W["untested_bonus"]
                add(b["low"], b["high"], side, "order_block", w, tf,
                    {"mitigated": b["mitigated"], "bars_ago": b["bars_ago"],
                     "displacement_atr": b["displacement_atr"]})

    # Previous period extremes
    for tf, name in [("Daily", "prev_day"), ("Weekly", "prev_week")]:
        df = frames.get(tf)
        if df is not None and len(df) >= 2:
            prev = df.iloc[-2]
            add(prev["high"], prev["high"], "buy_side", f"{name}_high",
                W["prev_period_extreme"], tf)
            add(prev["low"], prev["low"], "sell_side", f"{name}_low",
                W["prev_period_extreme"], tf)

    # Session extremes from the last 3 days of 15M data
    df15 = frames.get("15M")
    if df15 is not None and len(df15) > 50:
        recent = df15[df15["time"] >= df15["time"].max() - pd.Timedelta(days=3)].copy()
        if len(recent):
            recent["sess"] = recent["time"].apply(ind.session_of)
            for sess, grp in recent.groupby("sess"):
                if sess == "Offhours" or len(grp) < 4:
                    continue
                add(grp["high"].max(), grp["high"].max(), "buy_side",
                    f"{sess.lower()}_session_high", W["session_extreme"], "15M")
                add(grp["low"].min(), grp["low"].min(), "sell_side",
                    f"{sess.lower()}_session_low", W["session_extreme"], "15M")

    # Volume profile (approximation — see indicators.volume_profile docstring)
    vp_note = None
    df1h = frames.get("1H")
    if df1h is not None and len(df1h) > 100:
        vp = ind.volume_profile(df1h.tail(500))
        if vp:
            vp_note = "HVN/LVN approximated from OHLCV, not true volume-at-price"
            for lvl in vp["hvn"][:6]:
                side = "buy_side" if lvl > price else "sell_side"
                add(lvl, lvl, side, "hvn", W["hvn"], "1H")
            for lvl in vp["lvn"][:4]:
                side = "buy_side" if lvl > price else "sell_side"
                add(lvl, lvl, side, "lvn", W["lvn"], "1H")
            if vp.get("poc"):
                side = "buy_side" if vp["poc"] > price else "sell_side"
                add(vp["poc"], vp["poc"], side, "poc", W["hvn"], "1H")

    # VWAP + anchored VWAP
    if df15 is not None and len(df15) > 20:
        vw = ind.session_vwap(df15)
        if vw:
            add(vw, vw, "buy_side" if vw > price else "sell_side",
                "session_vwap", W["vwap"], "15M")
    if df1h is not None and len(df1h) > 50:
        h1_highs, h1_lows = swing_points(df1h)
        anchor = None
        if h1_highs and h1_lows:
            anchor = max(h1_highs[-1][0], h1_lows[-1][0])
        av = ind.anchored_vwap(df1h, anchor)
        if av:
            add(av, av, "buy_side" if av > price else "sell_side",
                "anchored_vwap", W["anchored_vwap"], "1H",
                {"anchor_bar": anchor})

    # Round numbers
    for lvl, step in _round_levels(asset, price, atr_val):
        add(lvl, lvl, "buy_side" if lvl > price else "sell_side",
            "round_number", W["round_number"], "-", {"step": step})

    if not raw:
        return [], {"error": "no zones detected"}

    # ---- merge overlapping/nearby zones of the same side ----
    # A merge is REJECTED if it would push the zone past max_width. Without
    # this cap, merging is transitive: each merge widens the zone, the wider
    # zone then overlaps more neighbours, and the whole book collapses into
    # one multi-thousand-point "zone" that every stop appears to sit inside.
    max_width = config.LIQUIDITY["max_zone_width_atr"] * atr_val
    raw.sort(key=lambda z: (z["side"], (z["low"] + z["high"]) / 2))
    merged = []
    for z in raw:
        placed = False
        for m in merged:
            if m["side"] != z["side"]:
                continue
            if (z["low"] - tol) <= m["high"] and (z["high"] + tol) >= m["low"]:
                new_lo, new_hi = min(m["low"], z["low"]), max(m["high"], z["high"])
                if (new_hi - new_lo) > max_width:
                    continue          # would over-widen: keep them separate
                m["low"], m["high"] = new_lo, new_hi
                m["weight"] += z["weight"]
                m["kinds"] += z["kinds"]
                m["timeframes"] += z["timeframes"]
                m["meta"].update(z["meta"])
                placed = True
                break
        if not placed:
            z = dict(z)
            if (z["high"] - z["low"]) > max_width:   # clamp a single wide zone
                mid = (z["low"] + z["high"]) / 2
                z["low"], z["high"] = mid - max_width / 2, mid + max_width / 2
            merged.append(z)

    # ---- score, measure, label ----
    ref = frames.get("1H", frames.get("15M"))
    max_w = max(m["weight"] for m in merged) or 1.0
    zones = []
    for m in merged:
        mid = (m["low"] + m["high"]) / 2
        kinds = sorted(set(m["kinds"]))
        tfs = sorted(set(t for t in m["timeframes"] if t != "-"))
        confirmations = len(kinds)

        score = 100.0 * m["weight"] / max_w
        if len(tfs) > 1:
            score = min(100.0, score * 1.10)      # multi-timeframe agreement
        score = float(np.clip(score, 0, 100))

        tests = _count_tests(ref, mid, tol) if ref is not None else 0
        dist = mid - price
        prob = "high" if score >= 70 else "medium" if score >= 45 else "low"

        zones.append({
            "low": round(m["low"], 6), "high": round(m["high"], 6),
            "mid": round(mid, 6),
            "side": m["side"],
            "kinds": kinds,
            "confirmations": confirmations,
            "timeframes": tfs,
            "strength": round(score, 1),
            "probability": prob,
            "tests": tests,
            "distance_points": round(float(dist), 6),
            "distance_atr": round(float(dist / atr_val), 2),
            "invalidation": (
                f"a {('4H' if '4H' in tfs or 'Daily' in tfs else '1H')} close "
                f"{'above' if m['side'] == 'buy_side' else 'below'} "
                f"{round(m['high'] if m['side'] == 'buy_side' else m['low'], 4)} "
                f"consumes this zone"),
            "meta": m["meta"],
        })

    zones.sort(key=lambda z: -z["strength"])
    notes = {"atr_used": round(float(atr_val), 6), "atr_timeframe": atr_tf}
    if vp_note:
        notes["volume_profile"] = vp_note
    return zones[:config.LIQUIDITY["max_zones_reported"]], notes


def nearest_zones(zones, price):
    above = [z for z in zones if z["mid"] > price]
    below = [z for z in zones if z["mid"] < price]
    above.sort(key=lambda z: z["mid"] - price)
    below.sort(key=lambda z: price - z["mid"])
    return (above[0] if above else None), (below[0] if below else None)


def liquidity_draw(zones, price, structure_trend, atr_val):
    """Which pool price is most likely being drawn toward.

    Heuristic: strength discounted by distance, with a modest bias toward
    continuation of the prevailing structure. This is a ranking device, not a
    forecast — it carries no calibrated probability.
    """
    if not zones or not atr_val:
        return None
    best, best_score = None, -1e9
    for z in zones:
        d = abs(z["distance_atr"]) or 0.01
        s = z["strength"] / (1.0 + 0.35 * d)
        if structure_trend == "UPTREND" and z["side"] == "buy_side":
            s *= 1.15
        elif structure_trend == "DOWNTREND" and z["side"] == "sell_side":
            s *= 1.15
        if s > best_score:
            best, best_score = z, s
    return best
