import requests

# Rotated ahead of making the repo public: ntfy.sh topics are unauthenticated,
# so the old topic (readable in git history once public) could let anyone
# read or spoof alerts. This is a fresh 24-hex-char random topic, effectively
# unguessable. Re-subscribe in the ntfy app to keep receiving pushes.
NTFY_TOPIC = "phazes-signals-e5531977802944c3814698c5"
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
