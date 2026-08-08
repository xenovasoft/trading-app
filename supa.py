import os
import datetime
import requests

def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _config():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if url and key:
        return url, key
    try:
        from local_config import SUPABASE_URL, SUPABASE_SECRET_KEY
        return SUPABASE_URL, SUPABASE_SECRET_KEY
    except ImportError:
        return None, None

def _headers(key):
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

def upsert_signal(row):
    url, key = _config()
    if not url:
        return
    row = dict(row)
    row["updated_at"] = _now_iso()
    headers = _headers(key)
    headers["Prefer"] = "resolution=merge-duplicates"
    try:
        r = requests.post(f"{url}/rest/v1/signals", json=[row], headers=headers, timeout=10)
        if not r.ok:
            print(f"supabase upsert_signal failed: {r.status_code} {r.text}")
    except requests.RequestException as e:
        print(f"supabase upsert_signal failed: {e}")

def insert_alert(asset, type_, title, message):
    url, key = _config()
    if not url:
        return
    row = {"asset": asset, "type": type_, "title": title, "message": message}
    try:
        r = requests.post(f"{url}/rest/v1/alerts", json=[row], headers=_headers(key), timeout=10)
        if not r.ok:
            print(f"supabase insert_alert failed: {r.status_code} {r.text}")
    except requests.RequestException as e:
        print(f"supabase insert_alert failed: {e}")

def load_all_state():
    """Returns {asset: state_dict} for every row in bot_state. Empty dict on failure."""
    url, key = _config()
    if not url:
        return {}
    try:
        r = requests.get(f"{url}/rest/v1/bot_state?select=*", headers=_headers(key), timeout=10)
        if not r.ok:
            print(f"supabase load_all_state failed: {r.status_code} {r.text}")
            return {}
        return {row["asset"]: row for row in r.json()}
    except requests.RequestException as e:
        print(f"supabase load_all_state failed: {e}")
        return {}

def _post(table, rows, upsert=False):
    url, key = _config()
    if not url:
        return False
    headers = _headers(key)
    if upsert:
        headers["Prefer"] = "resolution=merge-duplicates"
    try:
        r = requests.post(f"{url}/rest/v1/{table}", json=rows, headers=headers,
                          timeout=15)
        if not r.ok:
            print(f"supabase {table} write failed: {r.status_code} {r.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"supabase {table} write failed: {e}")
        return False


def upsert_signal_v2(row):
    row = dict(row)
    row["updated_at"] = _now_iso()
    return _post("signals_v2", [row], upsert=True)


def load_state_v2():
    """Returns {(asset, profile): row}."""
    url, key = _config()
    if not url:
        return {}
    try:
        r = requests.get(f"{url}/rest/v1/bot_state_v2?select=*",
                         headers=_headers(key), timeout=15)
        if not r.ok:
            print(f"supabase load_state_v2 failed: {r.status_code} {r.text[:200]}")
            return {}
        return {(x["asset"], x["profile"]): x for x in r.json()}
    except requests.RequestException as e:
        print(f"supabase load_state_v2 failed: {e}")
        return {}


def save_state_v2(asset, profile, state):
    row = dict(state)
    row["asset"] = asset
    row["profile"] = profile
    row["updated_at"] = _now_iso()
    return _post("bot_state_v2", [row], upsert=True)


def save_state(asset, state):
    url, key = _config()
    if not url:
        return
    row = dict(state)
    row["asset"] = asset
    row["updated_at"] = _now_iso()
    headers = _headers(key)
    headers["Prefer"] = "resolution=merge-duplicates"
    try:
        r = requests.post(f"{url}/rest/v1/bot_state", json=[row], headers=headers, timeout=10)
        if not r.ok:
            print(f"supabase save_state failed: {r.status_code} {r.text}")
    except requests.RequestException as e:
        print(f"supabase save_state failed: {e}")
