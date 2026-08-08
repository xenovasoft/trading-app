#!/usr/bin/env python
"""
Local dashboard server for the v2 analysis engine.

    python server.py            # http://localhost:8940
    python server.py 9000       # custom port

Analysis takes ~10s for all three assets, so results are cached and refreshed
by a background thread. The page never blocks on a live fetch; it shows the
cache age instead, so you can always tell how fresh what you're looking at is.

This runs the NEW engine (analysis.py). It does not touch Supabase, does not
send notifications, and does not interfere with the GitHub Actions cron.
"""

import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analysis
import dataio

REFRESH_SECONDS = 120
ASSETS = list(dataio.ASSETS.keys())

_cache = {"results": {}, "updated_at": None, "refreshing": False, "errors": {}}
_lock = threading.Lock()


def refresh_once():
    with _lock:
        if _cache["refreshing"]:
            return
        _cache["refreshing"] = True
    try:
        results, errors = {}, {}
        for asset in ASSETS:
            try:
                results[asset] = analysis.analyze_asset(asset)
            except Exception as e:
                errors[asset] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
        with _lock:
            if results:
                _cache["results"] = results
            _cache["errors"] = errors
            _cache["updated_at"] = time.time()
    finally:
        with _lock:
            _cache["refreshing"] = False


def refresher():
    while True:
        try:
            refresh_once()
        except Exception:
            traceback.print_exc()
        time.sleep(REFRESH_SECONDS)


# ------------------------------------------------------------------- HTML ---

PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Signal Engine v2</title>
<style>
:root{--bg:#0b0d12;--card:#151821;--card2:#1b1f2b;--bd:#262b38;--tx:#e6e9f0;
--dim:#8a90a2;--grn:#22c55e;--red:#ef4444;--gry:#6b7280;--amb:#f59e0b;--blu:#3b82f6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Roboto,Arial,sans-serif;
padding:max(16px,env(safe-area-inset-top)) 16px calc(env(safe-area-inset-bottom) + 32px)}
header{display:flex;justify-content:space-between;align-items:baseline;
flex-wrap:wrap;gap:8px;padding:8px 4px 16px}
h1{font-size:18px;margin:0;font-weight:650}
.sub{font-size:12px;color:var(--dim)}
.asset{background:var(--card);border:1px solid var(--bd);border-radius:14px;
padding:16px;margin-bottom:16px}
.arow{display:flex;justify-content:space-between;align-items:center;
flex-wrap:wrap;gap:8px;margin-bottom:4px}
.aname{font-size:17px;font-weight:650}
.price{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.inst{font-size:11px;color:var(--dim)}
.gate{background:rgba(239,68,68,.09);border:1px solid rgba(239,68,68,.35);
border-radius:10px;padding:10px 12px;margin:10px 0;font-size:12.5px}
.gate b{color:var(--red)}
.gate div{color:#fca5a5;margin-top:3px}
.warn{font-size:11.5px;color:var(--dim);margin-top:6px}
.profiles{display:grid;grid-template-columns:1fr;gap:12px;margin-top:12px}
@media(min-width:860px){.profiles{grid-template-columns:1fr 1fr}}
.prof{background:var(--card2);border:1px solid var(--bd);border-radius:12px;padding:14px}
.ptop{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.pname{font-weight:650;letter-spacing:.04em;font-size:12px;color:var(--dim)}
.badge{font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;letter-spacing:.03em}
.b-long{background:rgba(34,197,94,.16);color:var(--grn)}
.b-short{background:rgba(239,68,68,.16);color:var(--red)}
.b-wait{background:rgba(245,158,11,.16);color:var(--amb)}
.b-no{background:rgba(107,114,128,.2);color:var(--gry)}
.kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;font-size:12.5px}
.kv span:first-child{color:var(--dim)}
.kv span:last-child{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:5px;background:var(--bd);border-radius:3px;overflow:hidden;margin:6px 0 10px}
.bar div{height:100%;border-radius:3px}
.lvls{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
.lvls td{padding:3px 0}
.lvls td:last-child{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.lvls td:first-child{color:var(--dim)}
.sec{margin-top:10px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.rsn{font-size:12px;margin:2px 0;display:flex;gap:6px}
.ok{color:var(--grn)}.no{color:var(--red)}.blk{color:var(--amb)}
.rsn span:last-child{color:var(--tx);opacity:.85}
.zones{margin-top:12px}
.z{display:grid;grid-template-columns:1fr auto;gap:8px;padding:6px 0;
border-bottom:1px solid var(--bd);font-size:12px}
.z:last-child{border-bottom:none}
.zk{color:var(--dim);font-size:11px}
.zbar{height:4px;background:var(--bd);border-radius:2px;margin-top:4px;overflow:hidden}
.zbar div{height:100%;background:var(--blu)}
.side-buy{color:var(--grn)}.side-sell{color:var(--red)}
.foot{font-size:11px;color:var(--dim);margin-top:20px;padding:12px;
background:var(--card);border:1px solid var(--bd);border-radius:10px}
.foot b{color:var(--amb)}
details summary{cursor:pointer;color:var(--dim);font-size:11px;
text-transform:uppercase;letter-spacing:.06em;margin-top:10px}
.spin{color:var(--amb)}
</style></head><body>
<header><div><h1>Signal Engine v2</h1>
<div class="sub" id="sub">loading…</div></div>
<div class="sub" id="age"></div></header>
<div id="root"></div>
<div class="foot" id="foot"></div>
<script>
const DP={XAUUSD:2,XAGUSD:3,BTCUSD:2};
const f=(v,d)=>v==null?"—":Number(v).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

function badge(dec){
  const m={LONG:"b-long",SHORT:"b-short",WAIT:"b-wait","NO TRADE":"b-no"};
  return `<span class="badge ${m[dec]||"b-no"}">${esc(dec)}</span>`;
}

function zoneRow(z,dp){
  const side=z.side==="buy_side"?"side-buy":"side-sell";
  const lbl=z.side==="buy_side"?"buy-side ▲":"sell-side ▼";
  return `<div class="z"><div>
    <div><b>${f(z.low,dp)} – ${f(z.high,dp)}</b>
      <span class="${side}">${lbl}</span></div>
    <div class="zk">${esc(z.kinds.slice(0,3).join(" · "))} · ${z.confirmations} conf ·
      ${z.tests} tests · ${z.distance_atr>0?"+":""}${z.distance_atr} ATR</div>
    <div class="zbar"><div style="width:${z.strength}%"></div></div>
  </div><div style="text-align:right"><b>${Math.round(z.strength)}</b>
    <div class="zk">${esc(z.probability)}</div></div></div>`;
}

function profile(name,p,dp){
  if(!p) return "";
  if(!p.market_regime){
    return `<div class="prof"><div class="ptop"><span class="pname">${name}</span>
      ${badge(p.decision)}</div>
      <div class="rsn"><span class="blk">✗</span><span>${esc(p.reason||"gate failed")}</span></div>
      ${(p.blockers||[]).map(b=>`<div class="rsn"><span class="blk">✗</span><span>${esc(b)}</span></div>`).join("")}
    </div>`;
  }
  const s=p.setup||{}, sc=p.confluence_score??0;
  const col=sc>=70?"var(--grn)":sc>=50?"var(--amb)":"var(--red)";
  const tg=(s.targets||[]).map((t,i)=>
    `<tr><td>T${i+1} <span class="zk">${esc(t.anchor)}</span></td>
     <td>${f(t.price,dp)} &nbsp;<span class="zk">${t.rr}R</span></td></tr>`).join("");
  return `<div class="prof">
   <div class="ptop"><span class="pname">${name} · ${esc(p.expected_hold||"")}</span>${badge(p.decision)}</div>
   <div class="kv"><span>Confluence</span><span>${sc}/100 &nbsp;(${p.confirmations_passed}/${p.confirmations_total})
     ${p.counter_trend?'<span class="blk">counter-trend</span>':""}</span></div>
   <div class="bar"><div style="width:${sc}%;background:${col}"></div></div>
   <div class="kv"><span>Regime</span><span>${esc(p.market_regime.regime)} (ADX ${p.market_regime.adx??"—"})</span></div>
   <div class="kv"><span>HTF bias</span><span>${esc(p.htf_bias)}</span></div>
   <div class="kv"><span>Structure</span><span>${esc(p.structure.trend)} · ${esc(p.structure_event.event)}</span></div>
   <div class="kv"><span>Premium/discount</span><span>${esc(p.premium_discount.label)} (${p.premium_discount.position??"—"})</span></div>
   <div class="kv"><span>Expected draw</span><span>${p.expected_draw?f(p.expected_draw.mid,dp)+" ("+Math.round(p.expected_draw.strength)+")":"—"}</span></div>
   ${s.entry!=null?`<table class="lvls">
     <tr><td>Entry <span class="zk">${esc(s.entry_type||"")}</span></td><td>${f(s.entry,dp)}</td></tr>
     <tr><td>Stop <span class="zk">${s.stop_distance_atr} ATR</span></td><td>${f(s.selected_stop,dp)}</td></tr>
     ${tg}
     <tr><td>R:R (T1)</td><td>${s.primary_rr??"—"}</td></tr>
     <tr><td>Size</td><td>${s.position_size?.lots??"—"} lots</td></tr>
   </table>`:""}
   ${s.target_note?`<div class="warn">⚠ ${esc(s.target_note)}</div>`:""}
   ${s.stop_moved_off_liquidity?`<div class="warn">↳ ${esc(s.stop_move_reason)}</div>`:""}
   ${s.invalidation?`<div class="warn"><b>Invalidation:</b> ${esc(s.invalidation)}</div>`:""}
   ${(p.blockers||[]).length?`<div class="sec">Gate failures</div>`+
     p.blockers.map(b=>`<div class="rsn"><span class="blk">✗</span><span>${esc(b)}</span></div>`).join(""):""}
   <details><summary>Confirmations (${p.confirmations_passed}/${p.confirmations_total})</summary>
     ${(p.reasons_for||[]).map(r=>`<div class="rsn"><span class="ok">+</span><span>${esc(r)}</span></div>`).join("")}
     ${(p.reasons_against||[]).map(r=>`<div class="rsn"><span class="no">−</span><span>${esc(r)}</span></div>`).join("")}
   </details>
   <details><summary>Liquidity map (${(p.liquidity_zones||[]).length})</summary>
     <div class="zones">${(p.liquidity_zones||[]).map(z=>zoneRow(z,dp)).join("")}</div>
   </details>
  </div>`;
}

function render(d){
  const R=d.results||{};
  const keys=Object.keys(R);
  document.getElementById("sub").textContent =
    keys.length? `${keys.length} assets · refresh ${d.refresh_seconds}s` : "no data yet";
  document.getElementById("age").innerHTML = d.updated_at
    ? `updated ${d.age_seconds}s ago${d.refreshing?' <span class="spin">· refreshing…</span>':""}`
    : '<span class="spin">first analysis running…</span>';

  document.getElementById("root").innerHTML = keys.map(a=>{
    const r=R[a], dp=DP[a]??2, dq=r.data_quality||{};
    const hard=dq.hard_blockers||[];
    return `<div class="asset">
      <div class="arow"><div><div class="aname">${esc(a)}</div>
        <div class="inst">${esc(r.meta?.instrument_note||"")}</div></div>
        <div class="price">${f(r.current_price,dp)}</div></div>
      ${hard.length?`<div class="gate"><b>DATA GATE FAILED — trading suspended</b>
        ${hard.map(b=>`<div>✗ ${esc(b)}</div>`).join("")}</div>`:""}
      ${(dq.warnings||[]).length?`<details><summary>Data warnings (${dq.warnings.length})</summary>
        ${dq.warnings.map(w=>`<div class="warn">! ${esc(w)}</div>`).join("")}</details>`:""}
      <div class="profiles">
        ${profile("SWING",r.profiles?.SWING,dp)}
        ${profile("SCALP",r.profiles?.SCALP,dp)}
      </div></div>`;
  }).join("") || '<div class="asset">Waiting for the first analysis pass…</div>';

  const any=keys[0]&&R[keys[0]];
  document.getElementById("foot").innerHTML = any
    ? `<b>Not financial advice.</b> ${esc(any.scoring_note)}<br><br>
       <b>Unavailable data</b> (cannot be inferred from OHLCV):
       ${(any.unavailable_data||[]).map(esc).join(" · ")}`
    : "";
}

async function tick(){
  try{ const r=await fetch("/api/analysis",{cache:"no-store"}); render(await r.json()); }
  catch(e){ document.getElementById("age").textContent="server unreachable"; }
}
tick(); setInterval(tick,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/analysis":
            with _lock:
                payload = {
                    "results": _cache["results"],
                    "errors": _cache["errors"],
                    "updated_at": _cache["updated_at"],
                    "age_seconds": (int(time.time() - _cache["updated_at"])
                                    if _cache["updated_at"] else None),
                    "refreshing": _cache["refreshing"],
                    "refresh_seconds": REFRESH_SECONDS,
                }
            return self._send(200, json.dumps(payload, default=str),
                              "application/json; charset=utf-8")
        if path == "/api/refresh":
            threading.Thread(target=refresh_once, daemon=True).start()
            return self._send(202, json.dumps({"started": True}), "application/json")
        self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):
        pass   # keep the console readable; analysis logs still print


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8940
    threading.Thread(target=refresher, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Signal Engine v2 dashboard  ->  http://localhost:{port}")
    print(f"assets: {', '.join(ASSETS)}   refresh: {REFRESH_SECONDS}s")
    print("first analysis pass running in background (~10s)…")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
