#!/usr/bin/env python
"""
Live loop for the v2 engine. Replaces run_check.py.

Runs the SWING and SCALP analysis for every asset, writes a row per
(asset, profile) to signals_v2, tracks trade state in bot_state_v2, and pushes
notifications on genuine transitions only.

Alert discipline matters here: this runs every 15 minutes, and gold/silver are
shut all weekend. Anything that fires on state rather than on transition would
send ~200 identical weekend notifications. Every push below is gated on a
change against the previous run's stored state.
"""

import sys
import traceback

import analysis
import config
import dataio
from notify import push
from supa import (insert_alert, load_state_v2, save_state_v2, upsert_signal_v2)

# entry-timeframe minutes, used to convert setup_expiry_bars into wall time
TF_MIN = {"5M": 5, "15M": 15, "1H": 60, "4H": 240}

DEFAULT_STATE = {
    "status": "IDLE",
    "direction": None,
    "entry": None, "stop": None, "tp1": None, "tp2": None, "tp3": None,
    "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
    "last_decision": None,
    "last_structure_event": None,
    "last_sweep": None,
    "last_gate_ok": None,
    "bars_pending": 0,
    "opened_at": None,
}


def fmt(x, dp=2):
    return f"{x:,.{dp}f}" if isinstance(x, (int, float)) else str(x)


def notify(asset, kind, title, message, priority="default", tags=None):
    push(title, message, priority=priority, tags=tags)
    insert_alert(asset, kind, title, message)
    print(f"  PUSH [{kind}] {title}")


def _expiry_runs(profile_cfg):
    """How many 15-minute runs a PENDING setup may survive."""
    etf = profile_cfg["entry_timeframes"][0]
    minutes = profile_cfg["setup_expiry_bars"] * TF_MIN.get(etf, 15)
    return max(1, round(minutes / 15))


def signal_row(asset, profile, res, p):
    """Flatten one profile's analysis into a signals_v2 row."""
    s = p.get("setup") or {}
    tg = s.get("targets") or []
    dq = res["data_quality"]
    gate_ok = not dq["hard_blockers"]

    def t(i, k, default=None):
        return tg[i][k] if len(tg) > i else default

    return {
        "asset": asset, "profile": profile,
        "decision": p.get("decision"),
        "direction_considered": p.get("direction_considered"),
        "price": res["current_price"],
        "confluence_score": p.get("confluence_score"),
        "confirmations_passed": p.get("confirmations_passed"),
        "confirmations_total": p.get("confirmations_total"),
        "counter_trend": p.get("counter_trend"),
        "regime": (p.get("market_regime") or {}).get("regime"),
        "adx": (p.get("market_regime") or {}).get("adx"),
        "htf_bias": p.get("htf_bias"),
        "structure_trend": (p.get("structure") or {}).get("trend"),
        "structure_event": (p.get("structure_event") or {}).get("event"),
        "pd_label": (p.get("premium_discount") or {}).get("label"),
        "pd_position": (p.get("premium_discount") or {}).get("position"),
        "entry": s.get("entry"),
        "entry_type": s.get("entry_type"),
        "entry_extended": s.get("entry_is_extended"),
        "stop": s.get("selected_stop"),
        "stop_atr": s.get("stop_distance_atr"),
        "stop_basis": s.get("basis"),
        "tp1": t(0, "price"), "tp1_rr": t(0, "rr"), "tp1_anchor": t(0, "anchor"),
        "tp2": t(1, "price"), "tp2_rr": t(1, "rr"),
        "tp3": t(2, "price"), "tp3_rr": t(2, "rr"),
        "primary_rr": s.get("primary_rr"),
        "lots": (s.get("position_size") or {}).get("lots"),
        "units": (s.get("position_size") or {}).get("units"),
        "risk_amount": (s.get("position_size") or {}).get("risk_amount"),
        "invalidation": s.get("invalidation"),
        "expected_hold": p.get("expected_hold"),
        "blockers": p.get("blockers") or [],
        "reasons_for": p.get("reasons_for") or [],
        "reasons_against": p.get("reasons_against") or [],
        "zones": p.get("liquidity_zones") or [],
        "nearest_buy": p.get("nearest_buy_side"),
        "nearest_sell": p.get("nearest_sell_side"),
        "expected_draw": p.get("expected_draw"),
        "data_gate_ok": gate_ok,
        "data_blockers": dq["hard_blockers"],
        "data_warnings": dq["warnings"][:8],
    }


def process(asset, profile, res, p, state_all, dp):
    key = (asset, profile)
    st = dict(DEFAULT_STATE)
    st.update({k: v for k, v in (state_all.get(key) or {}).items()
               if k in DEFAULT_STATE})

    price = res["current_price"]
    gate_ok = not res["data_quality"]["hard_blockers"]
    decision = p.get("decision")
    s = p.get("setup") or {}
    pcfg = config.PROFILES[profile]

    # ---- data gate transitions (once each way, never every run) ----
    if st["last_gate_ok"] is not None and st["last_gate_ok"] != gate_ok:
        if not gate_ok:
            reason = res["data_quality"]["hard_blockers"][0]
            notify(asset, "DATA_GATE_CLOSED", f"⏸ {asset} {profile}: trading suspended",
                   f"{reason}\nNo signals will be issued until data is fresh.",
                   tags=["pause_button"])
            # a suspended market cannot manage an open idea
            if st["status"] in ("PENDING", "ACTIVE"):
                st["status"] = "IDLE"
        else:
            notify(asset, "DATA_GATE_OPEN", f"▶ {asset} {profile}: data live again",
                   f"Fresh data restored. Price {fmt(price, dp)}.", tags=["arrow_forward"])
    st["last_gate_ok"] = gate_ok

    if not gate_ok:
        st["last_decision"] = decision
        save_state_v2(asset, profile, st)
        return st

    # ---- structure / sweep transitions ----
    ev = (p.get("structure_event") or {}).get("event")
    if ev and ev not in ("NO_BREAK", "INSUFFICIENT_DATA") and ev != st["last_structure_event"]:
        notify(asset, "BOS_CHOCH", f"{asset} {profile}: {ev.replace('_', ' ').title()}",
               f"{ev.replace('_', ' ').lower()} on "
               f"{(p.get('structure_event') or {}).get('timeframe')}. "
               f"Price {fmt(price, dp)}", tags=["chart_with_upwards_trend"])
    st["last_structure_event"] = ev

    sweeps = p.get("sweeps") or []
    sweep_key = f"{sweeps[0]['type']}@{sweeps[0]['level']:.4f}" if sweeps else None
    if sweep_key and sweep_key != st["last_sweep"]:
        sw = sweeps[0]
        notify(asset, "LIQUIDITY_SWEEP", f"{asset} {profile}: Liquidity Sweep",
               f"{sw['type'].replace('_', ' ')} @ {fmt(sw['level'], dp)} "
               f"({sw['bars_ago']} bars ago, {sw['penetration_atr']} ATR) "
               f"→ {sw['implication']}", tags=["mag"])
    st["last_sweep"] = sweep_key

    # ---- trade state machine ----
    if st["status"] in ("IDLE", "CLOSED", None):
        if decision in ("LONG", "SHORT") and s.get("selected_stop"):
            st.update({"status": "ACTIVE", "direction": decision,
                       "entry": s["entry"], "stop": s["selected_stop"],
                       "tp1": s.get("tp1"), "tp2": s.get("tp2"), "tp3": s.get("tp3"),
                       "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                       "bars_pending": 0})
            tg = s.get("targets") or []
            for i, t in enumerate(tg[:3]):
                st[f"tp{i+1}"] = t["price"]
            emoji = "🟢" if decision == "LONG" else "🔴"
            notify(asset, "NEW_SIGNAL",
                   f"{emoji} {asset} {profile} {decision} ({p['confluence_score']}/100)",
                   f"Entry {fmt(s['entry'], dp)} ({s.get('entry_type')})\n"
                   f"Stop {fmt(s['selected_stop'], dp)} ({s.get('stop_distance_atr')} ATR)\n"
                   + "\n".join(f"TP{i+1} {fmt(t['price'], dp)} ({t['rr']}R @ {t['anchor']})"
                               for i, t in enumerate(tg[:3]))
                   + f"\nSize {(s.get('position_size') or {}).get('lots')} lots · "
                     f"{p['confirmations_passed']}/{p['confirmations_total']} confirmations",
                   priority="high", tags=["rotating_light"])
        elif decision == "WAIT" and s.get("selected_stop"):
            st.update({"status": "PENDING", "direction": p["direction_considered"],
                       "entry": s["entry"], "stop": s["selected_stop"],
                       "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                       "bars_pending": 0})
            tg = s.get("targets") or []
            for i, t in enumerate(tg[:3]):
                st[f"tp{i+1}"] = t["price"]
            notify(asset, "SETUP_ARMED",
                   f"⏳ {asset} {profile} {p['direction_considered']} setup armed",
                   f"Resting limit {fmt(s['entry'], dp)} — price is "
                   f"{fmt(price, dp)}, not there yet.\n"
                   f"Stop {fmt(s['selected_stop'], dp)}  ·  "
                   f"T1 {fmt((tg[0]['price'] if tg else None), dp)} "
                   f"({tg[0]['rr'] if tg else '—'}R)",
                   tags=["hourglass_flowing_sand"])
        else:
            st["status"] = "IDLE"

    elif st["status"] == "PENDING":
        st["bars_pending"] = (st.get("bars_pending") or 0) + 1
        d = st["direction"]
        still_valid = (decision in ("WAIT", d) and
                       p.get("direction_considered") == d)
        reached = st["entry"] is not None and (
            (d == "LONG" and price <= st["entry"]) or
            (d == "SHORT" and price >= st["entry"]))

        if reached and still_valid:
            st["status"] = "ACTIVE"
            notify(asset, "ZONE_REACHED", f"🎯 {asset} {profile}: entry zone reached",
                   f"Price {fmt(price, dp)} tapped the {d} entry "
                   f"{fmt(st['entry'], dp)}. Stop {fmt(st['stop'], dp)}.",
                   priority="high", tags=["dart"])
        elif not still_valid:
            st["status"] = "IDLE"
            notify(asset, "INVALIDATED", f"{asset} {profile}: setup invalidated",
                   f"{d} setup no longer qualifies "
                   f"({'; '.join((p.get('blockers') or ['conditions changed'])[:2])}). "
                   f"Price {fmt(price, dp)}.", tags=["x"])
        elif st["bars_pending"] > _expiry_runs(pcfg):
            st["status"] = "IDLE"
            notify(asset, "EXPIRED", f"{asset} {profile}: setup expired",
                   f"{d} limit at {fmt(st['entry'], dp)} was not reached within "
                   f"{pcfg['expected_hold']} window. Cancelled.", tags=["hourglass"])

    elif st["status"] == "ACTIVE":
        d = st["direction"]
        hit_sl = st["stop"] is not None and (
            (d == "LONG" and price <= st["stop"]) or
            (d == "SHORT" and price >= st["stop"]))
        if hit_sl:
            notify(asset, "SL_HIT", f"🛑 {asset} {profile}: stop loss hit",
                   f"Price {fmt(price, dp)} hit stop {fmt(st['stop'], dp)}.",
                   priority="high", tags=["skull"])
            st["status"] = "CLOSED"
        else:
            for i in (1, 2, 3):
                tp, flag = st.get(f"tp{i}"), f"tp{i}_hit"
                if tp is None or st.get(flag):
                    continue
                reached = (price >= tp) if d == "LONG" else (price <= tp)
                if reached:
                    st[flag] = True
                    notify(asset, f"TP{i}_HIT", f"💰 {asset} {profile}: TP{i} reached",
                           f"Price {fmt(price, dp)} hit TP{i} {fmt(tp, dp)}.",
                           priority="high", tags=["moneybag"])
                    if i == 3:
                        st["status"] = "CLOSED"

    st["last_decision"] = decision
    save_state_v2(asset, profile, st)
    return st


def main():
    state_all = load_state_v2()
    failures = []

    for asset in dataio.ASSETS:
        dp = config.CONTRACT_SPECS[asset]["price_dp"]
        try:
            res = analysis.analyze_asset(asset)
        except Exception as e:
            failures.append(asset)
            print(f"[{asset}] ANALYSIS FAILED: {e}", file=sys.stderr)
            traceback.print_exc()
            continue

        gate = "GATE-OK" if not res["data_quality"]["hard_blockers"] else "GATE-BLOCKED"
        print(f"{asset} @ {fmt(res['current_price'], dp)}  {gate}")

        for profile, p in res["profiles"].items():
            try:
                upsert_signal_v2(signal_row(asset, profile, res, p))
                st = process(asset, profile, res, p, state_all, dp)
                print(f"  {profile:6s} decision={p.get('decision'):9s} "
                      f"considered={p.get('direction_considered')} "
                      f"score={p.get('confluence_score')} state={st['status']}")
            except Exception as e:
                failures.append(f"{asset}/{profile}")
                print(f"[{asset}/{profile}] FAILED: {e}", file=sys.stderr)
                traceback.print_exc()

    if failures:
        print(f"\ncompleted with failures: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
