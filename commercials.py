"""Custom commercials per partner: free-text terms (structures differ for every partner) plus the standard payout fields,
with a Draft -> Proposed -> Agreed workflow. Partner_id 0 holds the standard text new onboardings start from."""
import re
from datetime import datetime

now = lambda: datetime.now().isoformat(timespec="seconds")

FREQUENCIES = ["Monthly", "Quarterly", "Half-yearly", "Yearly", "On each client event"]
STATUSES = ["Draft", "Proposed", "Agreed"]
CURRENCIES = ["INR", "USD", "AED", "SGD", "GBP", "EUR"]
FIELDS = ("terms_text", "currency", "effective_from", "valid_until", "payout_frequency", "payment_days", "min_payout", "tax_note", "notes")


def _clean_fields(f):
    out = {}
    if "terms_text" in f:
        out["terms_text"] = str(f["terms_text"] or "").strip()[:8000]
    if "currency" in f:
        cur = str(f["currency"] or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", cur):
            raise ValueError("Currency must be a 3-letter code, e.g. INR or USD")
        out["currency"] = cur
    for k in ("effective_from", "valid_until"):
        if k in f:
            v = str(f[k] or "").strip()
            if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise ValueError("Dates must be valid")
            out[k] = v
    if "payout_frequency" in f:
        v = str(f["payout_frequency"] or "").strip()
        if v and v not in FREQUENCIES:
            raise ValueError("Pick a payout frequency from the list")
        out["payout_frequency"] = v
    if "payment_days" in f:
        v = f["payment_days"]
        out["payment_days"] = None if v in (None, "") else max(0, int(v))
    if "min_payout" in f:
        v = f["min_payout"]
        out["min_payout"] = None if v in (None, "") else max(0.0, float(v))
    for k in ("tax_note", "notes"):
        if k in f:
            out[k] = str(f[k] or "").strip()[:2000]
    if out.get("effective_from") and out.get("valid_until") and out["valid_until"] < out["effective_from"]:
        raise ValueError("'Valid until' is before 'Effective from'")
    return out


def get(c, pid):
    h = c.execute("SELECT * FROM commercials WHERE partner_id=?", (pid,)).fetchone()
    return dict(h) if h else {"partner_id": pid, "status": "Draft", "currency": "INR", "terms_text": ""}


def describe(com):
    """The commercials as short lines, for the timeline and the partner page."""
    return [ln.strip() for ln in (com.get("terms_text") or "").splitlines() if ln.strip()]


def save(c, pid, body, *, locked_check=None):
    """Replace the given fields. Editing a Proposed/Agreed set drops it back to Draft so it gets re-confirmed."""
    if pid and locked_check:
        locked_check()
    fields = _clean_fields({k: body[k] for k in FIELDS if k in body})
    cur = c.execute("SELECT * FROM commercials WHERE partner_id=?", (pid,)).fetchone()
    if not cur:
        c.execute("INSERT INTO commercials(partner_id,status,updated_at) VALUES(?,?,?)", (pid, "Draft", now()))
        cur = c.execute("SELECT * FROM commercials WHERE partner_id=?", (pid,)).fetchone()
    changed = any((cur[k] or "") != (v if v is not None else "") for k, v in fields.items())
    if fields:
        c.execute(f"UPDATE commercials SET {', '.join(k + '=?' for k in fields)}, updated_at=? WHERE partner_id=?", [*fields.values(), now(), pid])
    reset = False
    if pid and changed and cur["status"] in ("Proposed", "Agreed"):
        c.execute("UPDATE commercials SET status='Draft', agreed_by=NULL, agreed_on=NULL WHERE partner_id=?", (pid,))
        reset = True
    return {"ok": True, "reset_to_draft": reset, "changed": changed}


def set_status(c, pid, status, by=""):
    if status not in STATUSES:
        raise ValueError("bad status")
    com = get(c, pid)
    if status in ("Proposed", "Agreed"):
        if not (com.get("terms_text") or "").strip():
            raise ValueError("Type the commercials first")
        if not com.get("payout_frequency"):
            raise ValueError("Choose a payout frequency first")
    if status == "Agreed":
        if not str(by or "").strip():
            raise ValueError("Enter who confirmed these terms with the partner")
        if not com.get("effective_from"):
            raise ValueError("Set the effective-from date first")
    c.execute("UPDATE commercials SET status=?, agreed_by=?, agreed_on=?, updated_at=? WHERE partner_id=?",
              (status, by.strip() if status == "Agreed" else None, now() if status == "Agreed" else None, now(), pid))
    return {"ok": True, "summary": describe(com)}


def copy_from(c, pid, source):
    """source: 'standard' or a partner id. Copies the text and payout terms (not dates, status or agreement)."""
    sid = 0 if source == "standard" else int(source)
    src = c.execute("SELECT * FROM commercials WHERE partner_id=?", (sid,)).fetchone()
    if not src or not (src["terms_text"] or "").strip():
        raise ValueError("There's nothing to copy from there yet")
    body = {k: src[k] for k in FIELDS if k not in ("effective_from", "valid_until")}
    r = save(c, pid, body)
    c.execute("UPDATE commercials SET based_on=? WHERE partner_id=?", ("standard" if sid == 0 else f"partner:{sid}", pid))
    return r


def options():
    return {"frequencies": FREQUENCIES, "currencies": CURRENCIES}
