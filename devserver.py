#!/usr/bin/env python
"""
Local dev server for website/.

`python -m http.server --directory website` serves the page but not /api/*,
so the paper panel renders its "unavailable" state and you learn nothing about
whether it works. This serves the same static files AND backs the paper
endpoints with the real papertrader state on this machine.

Two differences from production, both deliberate:

  * Arm/stop take effect IMMEDIATELY here. In production the website can only
    write an intent that the 15-minute GitHub Actions cron picks up, because
    the page and the engine share no process. Locally they do, so there is
    nothing to wait for.

  * The Supabase-backed endpoints (/api/signals, /api/history, /api/ticker,
    /api/candles) return empty unless SUPABASE_URL and a key are configured,
    in which case they proxy through. Empty is honest: the page shows
    "Waiting for first v2 sync" rather than inventing prices.

    python devserver.py                 # real local state, whatever it is
    python devserver.py --demo          # seed a realistic session to look at
    python devserver.py --port 8934
"""

import argparse
import json
import os
import random
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import papertrader

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "website")

def _credentials():
    """Env vars first, then local_config.py -- the same order supa.py uses, so
    a machine already set up to run the engine needs no extra configuration to
    run this server. local_config.py is gitignored; nothing here is committed.
    """
    url = os.environ.get("SUPABASE_URL")
    key = (os.environ.get("SUPABASE_PUBLISHABLE_KEY")
           or os.environ.get("SUPABASE_SECRET_KEY"))
    if url and key:
        return url, key
    try:
        import supa
        return supa._config()
    except Exception:
        return None, None


SUPA_URL, SUPA_KEY = _credentials()


def seed_demo():
    """Build a paper session from real backtest trades.

    Uses actual rows from bt_trades.csv rather than invented numbers, so what
    the panel shows is a faithful picture of what this strategy does -- a
    losing drift toward the drawdown limit -- instead of a flattering mock.
    """
    import pandas as pd
    path = os.path.join(HERE, "bt_trades.csv")
    if not os.path.exists(path):
        return papertrader.new_session()
    rows = pd.read_csv(path).head(120).to_dict("records")
    st = papertrader.new_session({"max_trades": 60, "max_drawdown_r": 25.0})
    st["note"] = "demo session seeded from bt_trades.csv"
    for r in rows:
        st["trades"].append({
            "asset": r.get("asset", "XAUUSD"), "profile": r.get("profile", "SCALP"),
            "direction": r.get("direction", "LONG"),
            "entry": r.get("entry"), "exit": r.get("exit"),
            "exit_reason": r.get("exit_reason"), "bars_held": r.get("bars_held"),
            "r_multiple": float(r.get("r_multiple") or 0.0),
        })
        stop = papertrader.check_auto_stop(st)
        if stop:                      # let the real rule decide where it ends
            st["status"] = papertrader.STOPPED
            st["stop_reason"] = stop
            st["stopped_at"] = papertrader._iso(papertrader._now())
            break
    if st["status"] == papertrader.ARMED:
        st["open"]["XAUUSD|SCALP"] = {
            "asset": "XAUUSD", "profile": "SCALP", "direction": "LONG",
            "entry": 4361.20, "stop": 4348.90, "target": 4392.40,
            "risk": 12.30, "planned_rr": 2.5, "confluence": 66.4,
            "entry_time": "2026-09-21 09:15:00", "max_bars": 48,
        }
    return st


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB, **k)

    # -- helpers ------------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _supa(self, path, params):
        if not (SUPA_URL and SUPA_KEY):
            return None
        import requests
        try:
            r = requests.get(f"{SUPA_URL}/rest/v1/{path}", params=params,
                             headers={"apikey": SUPA_KEY,
                                      "Authorization": f"Bearer {SUPA_KEY}"},
                             timeout=10)
            return r.json() if r.ok else None
        except Exception:
            return None

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path == "/api/paper":
            st = papertrader.load_state()
            if st.get("status") == papertrader.DISARMED and not st.get("trades"):
                snap = papertrader.snapshot(st)
                snap["never_run"] = True
                return self._json(snap)
            return self._json(papertrader.snapshot(st))

        if u.path == "/api/signals":
            return self._json(self._supa("signals_v2", {"select": "*"}) or [])

        if u.path == "/api/history":
            return self._json(self._supa("signal_history",
                                         {"select": "*", "limit": "200"}) or [])

        if u.path == "/api/ticker":
            asset = (q.get("asset") or ["XAUUSD"])[0]
            return self._json({"asset": asset, "price": None,
                               "note": "no live feed configured locally"})

        if u.path == "/api/candles":
            return self._json([])

        if u.path.startswith("/api/"):
            return self._json({"error": "not implemented locally"}, 404)

        return super().do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/api/paper-control":
            return self._json({"error": "not implemented locally"}, 404)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "body must be JSON"}, 400)

        action = body.get("action")
        if action == "arm":
            papertrader.arm({"max_trades": 60, "max_drawdown_r": 25.0},
                            note="armed from local dev server")
            return self._json({"ok": True, "requested": "arm",
                               "detail": "session armed (immediate locally; "
                                         "in production this waits for the "
                                         "next 15-minute engine cycle)"})
        if action == "disarm":
            papertrader.disarm("stopped from local dev server")
            return self._json({"ok": True, "requested": "disarm",
                               "detail": "session stopped (immediate locally)"})
        return self._json({"error": "action must be arm or disarm"}, 400)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8934)
    ap.add_argument("--demo", action="store_true",
                    help="seed a paper session from bt_trades.csv to look at")
    a = ap.parse_args()

    if a.demo:
        papertrader.save_state(seed_demo())
        print("seeded a demo paper session from bt_trades.csv")

    print(f"website  -> http://localhost:{a.port}")
    print(f"paper api-> http://localhost:{a.port}/api/paper")
    if not (SUPA_URL and SUPA_KEY):
        print("note: no SUPABASE_URL/key in env, so signal data is empty -- "
              "the paper panel still works")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
