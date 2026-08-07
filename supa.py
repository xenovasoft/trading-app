import os
import requests

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
