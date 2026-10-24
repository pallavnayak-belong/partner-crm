"""Best-effort Slack incoming-webhook notifications. Never raises; never blocks the caller."""
import json
import os
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("PARTNER_CRM_SLACK_CFG", os.path.join(HERE, "data", "slack.json"))


def load_cfg():
    try:
        with open(CFG_PATH) as fh:
            return {"webhook_url": "", **json.load(fh)}
    except (OSError, ValueError):
        return {"webhook_url": ""}


def save_cfg(url):
    url = (url or "").strip()
    if url and not url.startswith("https://hooks.slack.com/"):
        raise ValueError("Expected a Slack incoming-webhook URL (https://hooks.slack.com/services/…)")
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    fd = os.open(CFG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"webhook_url": url}, f)


def _post(url, text):
    try:
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:  # noqa
        return False


def send(text, wait=False):
    url = load_cfg()["webhook_url"]
    if not url:
        return False
    if wait:
        return _post(url, text)
    threading.Thread(target=_post, args=(url, text), daemon=True).start()
    return True
