"""Backup alerts: Slack (if connected) plus an email to you. Never raises, never blocks, and never repeats the same alert too often."""
import json
import smtplib
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage

import backup
import mail
import notify

now = lambda: datetime.now().isoformat(timespec="seconds")
_lock = threading.Lock()
SYNC = False          # tests set this so alerts are delivered before send() returns


def alert_address():
    cfg = backup.load_cfg()
    return (cfg.get("alert_email") or mail.load_cfg().get("address") or "").strip()


def _send_email(to, subject, body):
    m = mail.load_cfg()
    if not (m.get("address") and m.get("password") and to):
        return False
    msg = EmailMessage()
    msg["From"] = f"Partner CRM <{m['address']}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(m["smtp_host"], 465, timeout=30) as s:
            s.login(m["address"], m["password"])
            s.send_message(msg)
        return True
    except Exception:  # noqa
        return False


def send(kind, subject, body, throttle_hours=12, wait=False):
    """Tell you about a backup event. `kind` groups repeats: the same kind is sent at most once per `throttle_hours`."""
    with _lock:
        cfg = backup.load_cfg()
        seen = dict(cfg.get("alerts_sent") or {})
        last = seen.get(kind)
        if last and datetime.now() - datetime.fromisoformat(last) < timedelta(hours=throttle_hours):
            return False
        seen[kind] = now()
        backup.save_cfg({"alerts_sent": seen})

    def work():
        notify.send(f"{subject}\n{body}", wait=True)
        _send_email(alert_address(), "Partner CRM: " + subject, body + "\n\nOpen Backups in the CRM for details: http://127.0.0.1:8765/#/backups\n")
    if wait or SYNC:
        work()
    else:
        threading.Thread(target=work, daemon=True).start()
    return True


def clear(kind):
    """Forget a kind so the next failure alerts straight away (called when things recover)."""
    cfg = backup.load_cfg()
    seen = dict(cfg.get("alerts_sent") or {})
    if kind in seen:
        seen.pop(kind)
        backup.save_cfg({"alerts_sent": seen})
