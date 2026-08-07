import json
import os
import sys
import traceback

from engine import analyze
from notify import push
from supa import upsert_signal, insert_alert

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

DEFAULT_ASSET_STATE = {
    "status": "IDLE",   # IDLE -> PENDING -> ACTIVE -> CLOSED -> IDLE
    "direction": None,
    "entry": None, "sl": None, "tp1": None, "tp2": None, "tp3": None,
    "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
    "last_bos": None,
    "last_sweep": None,
}

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def fmt(x):
    return f"{x:,.2f}" if isinstance(x, (int, float)) else x

def notify(asset, type_, title, message, **push_kwargs):
    push(title, message, **push_kwargs)
    insert_alert(asset, type_, title, message)

def process_asset(asset, state):
    st = state.get(asset, dict(DEFAULT_ASSET_STATE))
    sig = analyze(asset)
    price = sig["price"]

    # --- structure / liquidity events (fire regardless of trade state) ---
    bos = sig["h1_bos_choch"]
    if bos not in ("NO_BREAK", "INSUFFICIENT_DATA") and bos != st.get("last_bos"):
        notify(asset, "BOS_CHOCH", f"{asset}: {bos.replace('_', ' ').title()}",
               f"1H {bos.replace('_', ' ').lower()}. Price: {fmt(price)} (ATR: {fmt(sig.get('atr_1h'))})",
               tags=["chart_with_upwards_trend"])
    st["last_bos"] = bos

    sweep = sig["h1_liquidity_sweep"]
    sweep_type = sweep[0] if sweep else None
    if sweep_type and sweep_type != st.get("last_sweep"):
        notify(asset, "LIQUIDITY_SWEEP", f"{asset}: Liquidity Sweep",
               f"{sweep[0].replace('_', ' ')} at {fmt(sweep[1])}. Price now {fmt(price)}",
               tags=["mag"])
    st["last_sweep"] = sweep_type

    # --- trade state machine ---
    if st["status"] in ("IDLE", "CLOSED"):
        if sig["trade_valid"]:
            st.update({
                "status": "ACTIVE" if not sig["chasing"] else "PENDING",
                "direction": sig["direction"],
                "entry": sig["entry"], "sl": sig["sl"],
                "tp1": sig["tp1"], "tp2": sig["tp2"], "tp3": sig["tp3"],
                "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
            })
            emoji = "🟢" if sig["direction"] == "BUY" else "🔴"
            zone_note = (f"Limit zone {fmt(sig['zone_low'])}-{fmt(sig['zone_high'])} (extended, wait for pullback)"
                         if sig["chasing"] else f"Market entry ~{fmt(sig['entry'])}")
            notify(asset, "NEW_SIGNAL", f"{emoji} {asset} {sig['direction']} ALERT ({sig['probability']}% confidence)",
                   f"{zone_note}\nSL: {fmt(sig['sl'])}  TP1: {fmt(sig['tp1'])}  TP2: {fmt(sig['tp2'])}  TP3: {fmt(sig['tp3'])}\n"
                   f"RR: {sig['rr']}  Confirmations: {sig['passed']}/{sig['total']}",
                   priority="high", tags=["rotating_light"])
        else:
            st["status"] = "IDLE"

    elif st["status"] == "PENDING":
        if sig["direction"] != st["direction"] or not sig["trade_valid"]:
            st["status"] = "IDLE"
            notify(asset, "INVALIDATED", f"{asset}: Setup Invalidated",
                   f"Prior {st['direction']} setup no longer valid. Price: {fmt(price)}", tags=["x"])
        else:
            in_zone = (st["direction"] == "BUY" and price <= st["entry"] * 1.001) or \
                      (st["direction"] == "SELL" and price >= st["entry"] * 0.999)
            if in_zone:
                st["status"] = "ACTIVE"
                notify(asset, "ZONE_REACHED", f"{asset}: Entry Zone Reached",
                       f"Price {fmt(price)} tapped the {st['direction']} entry zone.",
                       priority="high", tags=["dart"])

    elif st["status"] == "ACTIVE":
        d = st["direction"]
        hit_sl = (d == "BUY" and price <= st["sl"]) or (d == "SELL" and price >= st["sl"])
        if hit_sl:
            notify(asset, "SL_HIT", f"{asset}: Stop Loss Hit", f"Price {fmt(price)} hit SL {fmt(st['sl'])}.",
                   priority="high", tags=["skull"])
            st["status"] = "CLOSED"
        else:
            for tp_key, tag in [("tp1", "1"), ("tp2", "2"), ("tp3", "3")]:
                hit_flag = f"{tp_key}_hit"
                tp_val = st[tp_key]
                reached = (d == "BUY" and price >= tp_val) or (d == "SELL" and price <= tp_val)
                if reached and not st[hit_flag]:
                    st[hit_flag] = True
                    notify(asset, f"TP{tag}_HIT", f"{asset}: TP{tag} Reached", f"Price {fmt(price)} hit TP{tag} {fmt(tp_val)}.",
                           priority="high", tags=["moneybag"])
                    if tp_key == "tp3":
                        st["status"] = "CLOSED"

    state[asset] = st

    upsert_signal({
        "asset": asset,
        "price": sig["price"],
        "bias": sig["bias"],
        "direction": sig["direction"],
        "passed": sig["passed"],
        "total": sig["total"],
        "probability": sig["probability"],
        "entry": sig["entry"],
        "sl": sig["sl"],
        "tp1": sig["tp1"], "tp2": sig["tp2"], "tp3": sig["tp3"],
        "rr": sig["rr"],
        "zone_low": sig["zone_low"], "zone_high": sig["zone_high"],
        "chasing": sig["chasing"],
        "status": st["status"],
        "trade_valid": sig["trade_valid"],
    })

    return sig

def main():
    state = load_state()
    results = {}
    for asset in ["XAUUSD", "XAGUSD", "BTCUSD"]:
        try:
            results[asset] = process_asset(asset, state)
        except Exception as e:
            print(f"[{asset}] ERROR: {e}", file=sys.stderr)
            traceback.print_exc()
    save_state(state)
    for asset, sig in results.items():
        print(f"{asset}: price={fmt(sig['price'])} bias={sig['bias']} direction={sig['direction']} "
              f"valid={sig['trade_valid']} conf={sig['passed']}/{sig['total']} status={state[asset]['status']}")

if __name__ == "__main__":
    main()
