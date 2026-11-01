#!/usr/bin/env python
"""
User accounts for the paper terminal: a login, one demo trading account per
user, and a demo wallet (deposits and payouts) that funds it.

WHY THIS EXISTS
There used to be one paper account, row id=1, shared by everyone who opened
the site. Whoever pressed Start last replaced it for everybody, so nothing a
tester set up stayed put. Each user now owns an account and a wallet stored
server-side, so it is the same on every page, tab and device.

MONEY FLOW (all pretend)
    demo card --deposit--> wallet --start--> trading account
    trading account --stop--> wallet --payout--> demo bank

The wallet is a ledger (wallet_tx) of signed amounts: deposit +, payout -,
allocate - (moved into a trading account), return + (the trading account's
final balance coming back). The balance is their sum. Rows are only ever
added, so any balance can be explained line by line.

The trading account stays engine-owned, exactly like the shared one: the
website writes an intent (arm/disarm) and the next engine cycle acts on it.
Deposits and payouts never touch the trading account, which is why they can
take effect immediately.

NO REAL MONEY
A deposit is either an instant top-up that asks for no payment details at
all, or a checkout with the published demo card number. Any other card
number is refused, so a real one is never collected, and only the last four
digits are kept. Payouts go to a fixed "demo bank account" and send nothing.

The same rules run in three places: website/api/account.js (production),
devserver.py (local), and user_cycle() below (the engine). Passwords and
session tokens use PBKDF2-SHA256 and HMAC-SHA256 with identical formats in
Python and Node, so a user made by this script can log in on Vercel.

    python accounts.py seed              # test_users.local.json -> local store
    python accounts.py seed --remote     # same users -> Supabase (needs the secret key)
    python accounts.py add NAME [--remote]
"""

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time

import papertrader

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_STORE = os.path.join(HERE, "accounts.local.json")
TEST_USERS = os.path.join(HERE, "test_users.local.json")

USER_TABLE = "user_paper"
WALLET_TABLE = "wallet_tx"
USERS_TABLE = "app_users"

PBKDF2_ITER = 210_000            # OWASP's 2023 figure for PBKDF2-SHA256
SESSION_DAYS = 30
COOKIE = "se_session"
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")

# Stripe's published test numbers, so the flow feels like a real checkout
# while being unable to accept a real card.
DEMO_CARD = "4242424242424242"
DECLINE_CARD = "4000000000000002"
MAX_DEPOSIT = 1_000_000.0


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------- AUTH -----

def _b64(b):
    return base64.b64encode(b).decode()


def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64url(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password, iterations=PBKDF2_ITER):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, 32)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password, stored):
    try:
        algo, it, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 base64.b64decode(salt), int(it), 32)
        return hmac.compare_digest(dk, base64.b64decode(want))
    except Exception:
        return False


def sign_session(secret, user_id, username, days=SESSION_DAYS):
    exp = int(time.time()) + days * 86400
    body = f"v1.{int(user_id)}.{_b64url(username.encode())}.{exp}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def read_session(secret, token):
    """{'id', 'username'} for a valid unexpired token, else None."""
    try:
        v, uid, name, exp, sig = (token or "").split(".")
        body = f"{v}.{uid}.{name}.{exp}"
        want = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if v != "v1" or not hmac.compare_digest(_unb64url(sig), want):
            return None
        if int(exp) < time.time():
            return None
        return {"id": int(uid), "username": _unb64url(name).decode()}
    except Exception:
        return None


def check_username(raw):
    """(normalised, error|None). Lower-cased so 'Ali' and 'ali' are one user."""
    u = (raw or "").strip().lower()
    if not USERNAME_RE.match(u):
        return u, "username must be 3-32 characters: letters, digits, . _ or -"
    return u, None


def check_password(pw):
    if not isinstance(pw, str) or len(pw) < 8:
        return "password must be at least 8 characters"
    if len(pw) > 128:
        return "password must be at most 128 characters"
    return None


# -------------------------------------------------------------- WALLET -----

def parse_amount(v, hi, what="amount"):
    """(amount rounded to cents, error|None)."""
    try:
        a = round(float(v), 2)
    except (TypeError, ValueError):
        return None, f"{what} must be a number"
    if not (a == a) or a < 1:
        return None, f"{what} must be at least $1"
    if a > hi:
        return None, f"{what} can be at most ${hi:,.2f}"
    return a, None


def check_card(card):
    """(method label, http code, error|None) for a demo deposit."""
    digits = re.sub(r"\D", "", str(card or ""))
    if digits == DECLINE_CARD:
        return None, 402, "card declined (demo decline card)"
    if digits != DEMO_CARD:
        return None, 400, ("demo mode accepts only the test card "
                           "4242 4242 4242 4242 -- never enter a real card")
    return f"Demo card •••• {digits[-4:]}", 200, None


def deposit_method(body):
    """(method label, http code, error|None). method "instant" adds demo
    money with no payment details; anything else goes through the card."""
    if body.get("method") == "instant":
        return "Instant demo top-up", 200, None
    return check_card(body.get("card"))


def wallet_balance(txs):
    return round(sum(float(t.get("amount") or 0) for t in txs), 2)


def in_trading(state, requested):
    """Dollars currently out of the wallet: the running account's balance,
    or the amount of a start the engine hasn't picked up yet."""
    acct = (state or {}).get("account")
    if (state or {}).get("status") == papertrader.ARMED and acct:
        return round(acct["balance"], 2)
    if requested and requested.split(":", 1)[0] == "arm":
        return papertrader.parse_arm(requested)[0]
    return 0.0


def wallet_view(txs, state, requested, limit=50):
    newest = sorted(txs, key=lambda t: t.get("id") or 0, reverse=True)
    return {"balance": wallet_balance(txs),
            "in_trading": in_trading(state, requested),
            "transactions": newest[:limit]}


# ------------------------------------------------------- TRADING STATE -----

def blank_state():
    return {"status": papertrader.DISARMED, "trades": [], "open": {}, "seen": [],
            "limits": dict(papertrader.DEFAULTS), "started_at": None,
            "stopped_at": None, "stop_reason": None, "note": "",
            "last_step": None}


def stop(state, reason):
    st = dict(state)
    if st.get("status") != papertrader.STOPPED:
        st["status"] = papertrader.STOPPED
        st["stopped_at"] = _now_iso()
        st["stop_reason"] = reason
    return st


def settle(state):
    """(state, amount|None): the balance to hand back to the wallet if this
    session has ended and hasn't been paid back yet. Covers every way a
    session ends -- Stop, a replacement, and the engine's own auto-stops
    (drawdown, sample size, age, stale feed)."""
    acct = state.get("account")
    if state.get("status") != papertrader.STOPPED or not acct or state.get("settled_at"):
        return state, None
    amt = round(max(acct["balance"], 0.0), 2)
    st = dict(state, settled_at=_now_iso(), settled_usd=amt)
    return st, amt


def settle_ref(user_id, state):
    # One return per session, ever: the ledger's unique ref makes a retried
    # engine cycle unable to pay the same session back twice.
    return f"return:{user_id}:{state.get('started_at')}"


def user_snapshot(state, requested):
    """What /api/account returns as `paper`: the same shape as /api/paper,
    so the terminal renders a user's account with the code it already has."""
    snap = papertrader.snapshot(state)
    if state.get("status") == papertrader.DISARMED and not state.get("trades"):
        snap["never_run"] = True
    snap["pending_request"] = requested or None
    snap["settled_usd"] = state.get("settled_usd")
    return snap


# ---------------------------------------------------- LOCAL STORE (dev) -----

class LocalStore:
    """accounts.local.json: the three Supabase tables, for devserver.py.
    Gitignored -- it holds password hashes and the session-signing secret."""

    def __init__(self, path=LOCAL_STORE):
        self.path = path
        self.lock = threading.RLock()
        if os.path.exists(path):
            with open(path) as f:
                self.db = json.load(f)
        else:
            self.db = {"secret": secrets.token_urlsafe(32), "users": [],
                       "paper": {}, "tx": [], "next_user": 1, "next_tx": 1}
            self.save()

    @property
    def secret(self):
        return self.db["secret"]

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.db, f, indent=2, default=str)
        os.chmod(tmp, 0o600)      # password hashes and the signing secret
        os.replace(tmp, self.path)

    def find_user(self, username):
        return next((u for u in self.db["users"] if u["username"] == username), None)

    def add_user(self, username, password):
        """(user, error|None)."""
        username, err = check_username(username)
        err = err or check_password(password)
        if err:
            return None, err
        with self.lock:
            if self.find_user(username):
                return None, "that username is taken"
            u = {"id": self.db["next_user"], "username": username,
                 "password_hash": hash_password(password), "created_at": _now_iso()}
            self.db["next_user"] += 1
            self.db["users"].append(u)
            self.save()
            return u, None

    def get(self, user_id):
        row = self.db["paper"].get(str(user_id)) or {}
        return row.get("state") or blank_state(), row.get("requested")

    def put(self, user_id, state, requested=None):
        with self.lock:
            self.db["paper"][str(user_id)] = {"state": state, "requested": requested,
                                              "updated_at": _now_iso()}
            self.save()

    def txs(self, user_id):
        return [t for t in self.db["tx"] if t["user_id"] == user_id]

    def add_tx(self, user_id, kind, amount, method=None, note=None, ref=None):
        with self.lock:
            if ref and any(t.get("ref") == ref for t in self.db["tx"]):
                return None
            t = {"id": self.db["next_tx"], "user_id": user_id, "kind": kind,
                 "amount": round(float(amount), 2), "method": method, "note": note,
                 "ref": ref, "created_at": _now_iso()}
            self.db["next_tx"] += 1
            self.db["tx"].append(t)
            self.save()
            return t


# ------------------------------------------------ REMOTE (engine side) -----

def _remote():
    url, key, headers = papertrader._supa()
    if not url:
        return None, None
    import requests
    return url, (requests, headers)


def pull_user_rows():
    url, h = _remote()
    if not url:
        return []
    requests, headers = h
    try:
        r = requests.get(f"{url}/rest/v1/{USER_TABLE}",
                         params={"select": "user_id,payload,requested"},
                         headers=headers, timeout=15)
        if not r.ok:
            # Until supabase/user_accounts.sql has been run the table doesn't
            # exist. Say so and carry on: the shared account still runs.
            print(f"  user accounts unavailable: {r.status_code} {r.text[:160]}")
            return []
        return r.json()
    except Exception as e:
        print(f"  user accounts unavailable: {e}")
        return []


def push_user(user_id, state):
    """Write the state back WITHOUT touching `requested`, so a Stop pressed
    while this cycle ran is not wiped out by it."""
    url, h = _remote()
    if not url:
        return False
    requests, headers = h
    row = {"user_id": user_id,
           "payload": {**papertrader.snapshot(state),
                       "settled_usd": state.get("settled_usd"), "_state": state},
           "updated_at": _now_iso()}
    hh = dict(headers, Prefer="resolution=merge-duplicates")
    r = requests.post(f"{url}/rest/v1/{USER_TABLE}", json=[row], headers=hh, timeout=15)
    if not r.ok:
        print(f"  user {user_id}: push failed {r.status_code} {r.text[:160]}")
    return r.ok


def clear_request(user_id, req):
    """Clear the intent only if it is still the one this cycle acted on."""
    url, h = _remote()
    if not url:
        return
    requests, headers = h
    requests.patch(f"{url}/rest/v1/{USER_TABLE}",
                   params={"user_id": f"eq.{user_id}", "requested": f"eq.{req}"},
                   json={"requested": None}, headers=headers, timeout=15)


def insert_tx(user_id, kind, amount, method=None, note=None, ref=None):
    url, h = _remote()
    if not url:
        return False
    requests, headers = h
    hh = dict(headers, Prefer="resolution=ignore-duplicates")
    r = requests.post(f"{url}/rest/v1/{WALLET_TABLE}", params={"on_conflict": "ref"},
                      json=[{"user_id": user_id, "kind": kind,
                             "amount": round(float(amount), 2), "method": method,
                             "note": note, "ref": ref}],
                      headers=hh, timeout=15)
    if not r.ok:
        print(f"  user {user_id}: wallet write failed {r.status_code} {r.text[:160]}")
    return r.ok


def _settle_remote(user_id, state):
    st, amt = settle(state)
    if amt is None:
        return state
    # Ledger first: if the push below fails, the next cycle settles again
    # and the unique ref turns the repeat into a no-op.
    if not insert_tx(user_id, "return", amt, "Trading account",
                     st.get("stop_reason") or "account stopped",
                     settle_ref(user_id, state)):
        return state
    print(f"  user {user_id}: returned ${amt:,.2f} to wallet")
    return st


def user_cycle(signals, frames_by_asset):
    """Step every user's account by one engine cycle. Called from
    run_check_v2.paper_cycle() after the shared account. One user's failure
    never stops the others."""
    for row in pull_user_rows():
        uid = row.get("user_id")
        try:
            req = row.get("requested")
            state = (row.get("payload") or {}).get("_state") or blank_state()
            if (req and req.split(":", 1)[0] == "arm"
                    and state.get("status") == papertrader.ARMED):
                # The website refuses Start while an account runs; this is
                # the backstop, so a replaced account's money isn't lost.
                state = _settle_remote(uid, stop(state, "replaced by a new account"))
            if req:
                state, note = papertrader.apply_intent(state, req, source="user")
                if note:
                    print(f"  user {uid}: {note}")
            state, events = papertrader.step(state, signals, frames_by_asset)
            for e in events:
                if not e.startswith("session "):
                    print(f"  user {uid}: {e}")
            state = _settle_remote(uid, state)
            if push_user(uid, state) and req:
                clear_request(uid, req)
        except Exception as e:
            print(f"  user {uid}: cycle failed: {e}", file=sys.stderr)


def create_remote_user(username, password):
    url, h = _remote()
    if not url:
        return "no SUPABASE_URL / SUPABASE_SECRET_KEY configured"
    requests, headers = h
    username, err = check_username(username)
    err = err or check_password(password)
    if err:
        return err
    r = requests.post(f"{url}/rest/v1/{USERS_TABLE}",
                      json=[{"username": username,
                             "password_hash": hash_password(password)}],
                      headers=headers, timeout=15)
    if r.status_code == 409:
        return "already exists"
    return None if r.ok else f"{r.status_code} {r.text[:200]}"


# ------------------------------------------------------------------ CLI ----

def seed_local(store=None, quiet=False):
    """Create every user in test_users.local.json that the local store lacks."""
    if not os.path.exists(TEST_USERS):
        return []
    store = store or LocalStore()
    with open(TEST_USERS) as f:
        made = []
        for u in json.load(f):
            if store.find_user(check_username(u["username"])[0]):
                continue
            user, err = store.add_user(u["username"], u["password"])
            if not quiet:
                print(f"{u['username']}: {err or 'created'}")
            if user:
                made.append(user["username"])
        return made


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="create the users in test_users.local.json")
    s.add_argument("--remote", action="store_true", help="in Supabase, not locally")
    a = sub.add_parser("add", help="create one user (asks for the password)")
    a.add_argument("username")
    a.add_argument("--remote", action="store_true")
    args = ap.parse_args()

    if args.cmd == "seed":
        if not args.remote:
            made = seed_local()
            print(f"{len(made)} new local user(s)")
            return
        with open(TEST_USERS) as f:
            for u in json.load(f):
                print(f"{u['username']}: {create_remote_user(u['username'], u['password']) or 'created'}")
        return

    pw = getpass.getpass(f"password for {args.username}: ")
    if args.remote:
        print(create_remote_user(args.username, pw) or "created")
    else:
        _, err = LocalStore().add_user(args.username, pw)
        print(err or "created")


if __name__ == "__main__":
    main()
