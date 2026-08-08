"""Renders an analysis result in the requested fixed output format."""


def _z(z, dp=2):
    if not z:
        return "none detected"
    return (f"{z['low']:.{dp}f}-{z['high']:.{dp}f} "
            f"[{'/'.join(z['kinds'][:3])}] str={z['strength']:.0f} "
            f"prob={z['probability']} tests={z['tests']} "
            f"dist={z['distance_atr']:+.2f} ATR")


def render_profile(asset, res, pname, dp=2):
    d = res["profiles"][pname]
    L = []
    A = L.append
    A(f"────────── {asset} · {pname} ──────────")

    if d.get("decision") == "NO TRADE" and "market_regime" not in d:
        A(f"Decision: NO TRADE — {d.get('reason', 'gate failed')}")
        for b in d.get("blockers", []):
            A(f"  · {b}")
        return "\n".join(L)

    s = d.get("setup") or {}
    reg = d.get("market_regime", {})
    st = d.get("structure", {})
    ev = d.get("structure_event", {})
    pdz = d.get("premium_discount", {})

    A(f"Market regime:            {reg.get('regime')} (ADX {reg.get('adx')}"
      f"{', compressed' if reg.get('compressed') else ''})")
    A(f"Higher-timeframe bias:    {d.get('htf_bias')}")
    A(f"Current price:            {res['current_price']:.{dp}f}")
    A(f"Nearest buy-side liq:     {_z(d.get('nearest_buy_side'), dp)}")
    A(f"Nearest sell-side liq:    {_z(d.get('nearest_sell_side'), dp)}")
    A(f"Strongest liquidity zone: {_z(d.get('strongest_zone'), dp)}")
    A(f"Expected liquidity draw:  {_z(d.get('expected_draw'), dp)}")
    A(f"Structure:                {st.get('trend')} — {st.get('detail')}")
    A(f"                          event: {ev.get('event')} on {ev.get('timeframe')}")
    A(f"Premium/discount:         {pdz.get('label')} (pos {pdz.get('position')})")

    if s:
        A(f"Entry trigger:            {d['reasons_for'] and s.get('entry_type') or s.get('entry_type')}")
        A(f"Potential entry:          {s['entry']:.{dp}f} ({s['entry_type']})")
        A(f"Structural stop:          {s.get('structural_stop', float('nan')):.{dp}f}")
        A(f"ATR-adjusted stop:        "
          f"{s['atr_stop']:.{dp}f}" if s.get("atr_stop") else "ATR-adjusted stop:        n/a")
        if s.get("volatility_stop"):
            A(f"Volatility stop (2σ):     {s['volatility_stop']:.{dp}f}")
        A(f"SELECTED stop:            {s['selected_stop']:.{dp}f} "
          f"({s.get('stop_distance_atr')} ATR, basis: {s.get('basis','-')})")
        if s.get("stop_moved_off_liquidity"):
            A(f"   ↳ moved off liquidity: {s['stop_move_reason']}")
        A(f"Invalidation:             {s['invalidation']}")
        tg = s.get("targets") or []
        for i, t in enumerate(tg, 1):
            A(f"Target T{i}:               {t['price']:.{dp}f}  "
              f"R:R {t['rr']}  @ {t['anchor']} (str {t['zone_strength']:.0f})")
        if not tg:
            A(f"Targets:                  none — {s.get('target_note')}")
        A(f"Reward-to-risk (T1):      {s.get('primary_rr')}")
        ps = s.get("position_size", {})
        A(f"Position size:            {ps.get('lots')} lots "
          f"({ps.get('units')} units) risking ${ps.get('risk_amount')} "
          f"of ${ps.get('equity_used')}")
        if ps.get("equity_is_placeholder"):
            A("   ⚠ equity is still the PLACEHOLDER value — size is illustrative only")
        cc = s.get("cost_check", {})
        if cc.get("multiple") is not None:
            A(f"Cost check:               T1 gross is {cc['multiple']}x est. round-turn cost")

    A(f"Confluence score:         {d.get('confluence_score')}/100  "
      f"({d.get('confirmations_passed')}/{d.get('confirmations_total')} checks)"
      f"{'  [COUNTER-TREND]' if d.get('counter_trend') else ''}")
    A("Reasons supporting:")
    for r in d.get("reasons_for", []):
        A(f"  + {r}")
    A("Reasons against:")
    for r in d.get("reasons_against", []):
        A(f"  − {r}")
    if d.get("blockers"):
        A("Gate failures:")
        for b in d["blockers"]:
            A(f"  ✗ {b}")
    A(f"Expected holding time:    {d.get('expected_hold')}")
    A(f"DECISION:                 {d.get('decision')} "
      f"({d.get('direction_considered')} side evaluated)")
    return "\n".join(L)


def render(res):
    dp = {"XAUUSD": 2, "XAGUSD": 3, "BTCUSD": 2}.get(res["asset"], 2)
    L = [f"╔══ {res['asset']} @ {res['current_price']:.{dp}f} "
         f"({res['meta']['instrument_note']})",
         f"║  fetched {res['meta']['fetched_at_utc']} UTC"]
    dq = res["data_quality"]
    if dq["hard_blockers"]:
        L.append("║  DATA GATE FAILED:")
        for b in dq["hard_blockers"]:
            L.append(f"║    ✗ {b}")
    for w in dq["warnings"][:6]:
        L.append(f"║    ! {w}")
    L.append("╚" + "═" * 60)
    for p in res["profiles"]:
        L.append(render_profile(res["asset"], res, p, dp))
        L.append("")
    L.append("Missing / unavailable data (cannot be inferred from OHLCV):")
    for u in res["unavailable_data"]:
        L.append(f"  · {u}")
    L.append("")
    L.append("NOTE: " + res["scoring_note"])
    return "\n".join(L)
