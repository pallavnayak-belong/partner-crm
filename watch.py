"""Watches for things that look wrong and tells you: many partners changed at once, a spike of unsubscribes or bounces, partners disappearing,
failed sends, or test data in the live database. Runs every few minutes inside the CRM; it only reads data and sends alerts."""
import json
import os
import threading
import time
from datetime import datetime, timedelta

import alerts
import db

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("PARTNER_CRM_WATCH_STATE", os.path.join(HERE, "data", "watch.json"))
EVERY_SECONDS = 300
BULK_CHANGE = 10          # partners edited within 10 minutes (normal "New -> Contacted" moves from a send don't count)
UNSUB_24H = 5
BOUNCE_24H = 5
FAILED_1H = 5
since = lambda **kw: (datetime.now() - timedelta(**kw)).isoformat(timespec="seconds")


def _state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)


def check(c, notify=True):
    """Returns the list of findings [(kind, subject, body)]; sends each as an alert (throttled) unless notify=False."""
    found = []

    # 1. many partners edited at once (by a person or a script)
    rows = c.execute("""SELECT partner_id, actor, field, old, new FROM change_log WHERE ts>=? AND undone=0
                        AND NOT (field='stage' AND old='New' AND new='Contacted') AND field IN ('stage','do_not_contact','archived','email','owner','name')""", (since(minutes=10),)).fetchall()
    partners = {r["partner_id"] for r in rows}
    if len(partners) >= BULK_CHANGE:
        who = {}
        for r in rows:
            who[r["actor"] or "system"] = who.get(r["actor"] or "system", 0) + 1
        top = max(who, key=who.get)
        found.append(("bulk-change", f"{len(partners)} partners were edited at once",
                      f"{len(partners)} partners changed within 10 minutes, mostly by {('the CRM itself (automatic)' if top == 'system' else top)}. If that wasn't intended, open Backups > Recent changes and undo it."))

    # 2. unsubscribes / do-not-contact spike
    n = c.execute("SELECT COUNT(DISTINCT partner_id) FROM change_log WHERE field='do_not_contact' AND new='1' AND ts>=?", (since(hours=24),)).fetchone()[0]
    if n >= UNSUB_24H:
        found.append(("unsub-spike", f"{n} partners went do-not-contact in 24 hours",
                      "That is unusually many. If they are unsubscribe replies, the last email may have landed badly. Open the partners, or Backups > Recent changes, to check."))

    # 3. bounces
    nb = c.execute("SELECT COUNT(*) FROM activities WHERE type='bounce' AND ts>=?", (since(hours=24),)).fetchone()[0]
    sent = c.execute("SELECT COUNT(*) FROM emails WHERE status='sent' AND sent_at>=?", (since(hours=24),)).fetchone()[0]
    if nb >= BOUNCE_24H or (sent >= 20 and nb / sent >= 0.10):
        found.append(("bounce-spike", f"{nb} emails bounced in 24 hours", f"{nb} bounces out of {sent} emails sent. Many bounces hurt the mailbox's reputation, so pause bulk sends until the address list is checked."))

    # 4. failed sends
    nf = c.execute("SELECT COUNT(*) FROM emails WHERE status='failed' AND sent_at>=?", (since(hours=1),)).fetchone()[0]
    if nf >= FAILED_1H:
        found.append(("send-failures", f"{nf} emails failed to send in the last hour", "Check the Send log for the reason (login, size, or the mail server)."))

    # 5. partners disappearing
    st = _state()
    total = c.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
    last = st.get("partners")
    if last is not None and total <= last - 3:
        found.append(("partners-dropped", f"partners dropped from {last} to {total}", "Partners were deleted, merged or lost. If that wasn't you, restore from Backups (Bring back one partner, or Compare)."))
    st["partners"] = total

    # 6. test data in live data (fake message ids from the test suite)
    fake = c.execute("SELECT COUNT(*) FROM replies_seen WHERE message_id LIKE '<%@x>'").fetchone()[0]
    if fake:
        found.append(("test-data", "test data found in the live database", f"{fake} fake reply record(s) from the automated tests are in your real data. Tell whoever runs the tests; they should never touch live data."))

    st["checked"] = datetime.now().isoformat(timespec="seconds")
    _save(st)
    if notify:
        for kind, subject, body in found:
            alerts.send(kind, subject, body, throttle_hours=6)
    return found


def scheduler():
    time.sleep(60)
    while True:
        try:
            with db.connect() as c:
                check(c)
        except Exception:  # noqa
            pass
        time.sleep(EVERY_SECONDS)
