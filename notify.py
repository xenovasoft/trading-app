import requests

NTFY_TOPIC = "phazes-signals-172ce65ef9b0"
NTFY_URL = "https://ntfy.sh"

def push(title, message, priority="default", tags=None):
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": {"default": 3, "high": 4}.get(priority, 3),
    }
    if tags:
        payload["tags"] = tags
    try:
        requests.post(NTFY_URL, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"ntfy push failed: {e}")
