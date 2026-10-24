"""Youform integration: parse submissions (webhook / relay / CSV), match them to partners, apply to the CRM."""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import db

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("PARTNER_CRM_YOUFORM_CFG", os.path.join(HERE, "data", "youform.json"))
FORM_SLUG = "geok4bom"
now = lambda: datetime.now().isoformat(timespec="seconds")

FREE_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com",
                "rediffmail.com", "icloud.com", "proton.me", "protonmail.com", "aol.com", "msn.com", "ymail.com", "me.com"}
STOP = {"pvt", "private", "ltd", "limited", "llp", "llc", "inc", "the", "and", "co", "company", "corp", "corporation", "of"}


# ---------------- config ----------------
def load_cfg():
    cfg = {"relay_url": "", "relay_token": "", "signing_secret": ""}
    try:
        with open(CFG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(upd):
    cfg = load_cfg()
    for k in ("relay_url", "relay_token", "signing_secret"):
        if k in upd and (upd[k] or k == "relay_url"):
            cfg[k] = str(upd[k]).strip()
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    fd = os.open(CFG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return cfg


def public_cfg():
    c = load_cfg()
    return {"relay_url": c["relay_url"], "has_relay_token": bool(c["relay_token"]), "has_signing_secret": bool(c["signing_secret"])}


# ---------------- parsing ----------------
def role(label):
    q = (label or "").lower()
    if "email" in q: return "email"
    if "phone" in q or "mobile" in q or "whatsapp" in q: return "phone"
    if "company" in q or "firm" in q or "organi" in q: return "company"
    if "applying as" in q or "applying" in q: return "applying_as"
    if "client" in q and "refer" in q or q.strip() in ("clients", "client type"): return "clients"
    if "product" in q: return "products"
    if any(k in q for k in ("website", "linkedin", "social", "profile")): return "profile"
    if "name" in q: return "name"
    if any(k in q for k in ("submitted", "completed", "created", "date", "time")): return "received_at"
    if "submission" in q and "id" in q: return "external_id"
    return None


def _iso_local(ts):
    if not ts:
        return now()
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone().replace(tzinfo=None)
        return d.isoformat(timespec="seconds")
    except ValueError:
        return str(ts)[:19]


def from_webhook(payload):
    """Youform submission payload -> application dict. Returns None if it isn't a submission."""
    if not isinstance(payload, dict) or "fields" not in payload:
        return None
    app = {k: "" for k in ("name", "email", "phone", "company", "applying_as", "clients", "products", "profile")}
    for f in payload.get("fields") or []:
        r = role(f.get("question"))
        if r in app and f.get("answer") not in (None, ""):
            ans = f["answer"]
            app[r] = ", ".join(map(str, ans)) if isinstance(ans, list) else str(ans).strip()
    app["external_id"] = str(payload.get("submission_id") or payload.get("event_id") or "")
    app["received_at"] = _iso_local(payload.get("completed_at") or payload.get("started_at"))
    app["raw"] = json.dumps(payload)
    app["form_id"] = payload.get("form_id", "")
    return app


def from_csv(text):
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for r in rows:
        app = {k: "" for k in ("name", "email", "phone", "company", "applying_as", "clients", "products", "profile", "received_at", "external_id")}
        for h, v in r.items():
            ro = role(h)
            if ro and v and not app.get(ro):
                app[ro] = v.strip()
        if not (app["email"] or app["name"]):
            continue
        app["received_at"] = _iso_local(app["received_at"]) if app["received_at"] else now()
        if not app["external_id"]:
            app["external_id"] = "csv:" + hashlib.sha1(f"{app['email'].lower()}|{app['name']}|{app['received_at'][:10]}".encode()).hexdigest()[:16]
        app["raw"] = json.dumps(r)
        out.append(app)
    return out


def verify_signature(raw_body, header, secret):
    if not secret:
        return True
    mac = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, (header or "").strip())


# ---------------- matching ----------------
def norm_company(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().replace("&", " and ").replace(".", ""))
    return " ".join(t for t in s.split() if t not in STOP)


def toks(s):
    return set(norm_company(s).split())


def digits10(s):
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else ""


def domain(e):
    e = (e or "").strip().lower()
    return e.split("@")[-1] if "@" in e else ""


def find_candidates(c, app):
    """Return [(partner_id, confidence, method)] best first. confidence: high | medium | low."""
    cands = {}

    def add(pid, conf, method):
        rank = {"high": 3, "medium": 2, "low": 1}
        if pid not in cands or rank[conf] > rank[cands[pid][0]]:
            cands[pid] = (conf, method)

    email = (app.get("email") or "").strip().lower()
    phone = digits10(app.get("phone"))
    comp = norm_company(app.get("company"))
    name_t = toks(app.get("name"))
    dom = domain(email)
    partners = c.execute("SELECT id, name, email, phone, contact_person FROM partners").fetchall()
    sent = {}
    if email:
        for r in c.execute("SELECT DISTINCT partner_id FROM emails WHERE status='sent' AND LOWER(to_addr)=?", (email,)):
            add(r[0], "high", "emailed this address")
    for p in partners:
        pe = (p["email"] or "").strip().lower()
        if email and pe == email:
            add(p["id"], "high", "email matches")
        if phone and digits10(p["phone"]) == phone:
            add(p["id"], "high", "phone matches")
        pn = norm_company(p["name"])
        if comp and pn and comp == pn:
            add(p["id"], "high", "company name matches")
        if dom and dom not in FREE_DOMAINS and domain(pe) == dom:
            add(p["id"], "medium", f"same email domain ({dom})")
        pt = set(pn.split())
        if len(name_t) >= 2 and pt == name_t:
            add(p["id"], "medium", "applicant name matches partner")
        if len(toks(p["contact_person"])) >= 2 and toks(p["contact_person"]) == name_t and len(name_t) >= 2:
            add(p["id"], "medium", "contact person matches")
        for label, t in (("company", toks(app.get("company"))), ("name", name_t)):
            if len(t) >= 2 and len(pt) >= 2:
                j = len(t & pt) / len(t | pt)
                if j >= 0.6 and p["id"] not in cands:
                    add(p["id"], "low", f"similar {label} ({int(j * 100)}%)")
    order = {"high": 0, "medium": 1, "low": 2}
    res = sorted(((pid, conf, m) for pid, (conf, m) in cands.items()), key=lambda x: order[x[1]])
    return res[:6]


# ---------------- ingest ----------------
def summary(app):
    bits = [f"Applied via Youform ({app.get('name') or 'unknown'})"]
    for k, lab in (("applying_as", "As"), ("clients", "Clients"), ("products", "Products"), ("profile", "Profile")):
        if app.get(k):
            bits.append(f"{lab}: {app[k]}")
    return " · ".join(bits)


def apply_link(c, app_id, partner_id, method, conf):
    a = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    p = c.execute("SELECT * FROM partners WHERE id=?", (partner_id,)).fetchone()
    ts = now()
    c.execute("UPDATE applications SET partner_id=?, status='linked', match_method=?, match_confidence=? WHERE id=?", (partner_id, method, conf, app_id))
    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,'application',?)", (partner_id, a["received_at"] or ts, summary(dict(a))))
    if p["do_not_contact"]:
        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,'note',?)",
                  (partner_id, ts, f"Applied despite do-not-contact flag ({p['dnc_reason'] or 'manual'}). Flag left in place — review before emailing."))
    new_stage = p["stage"]
    if p["stage"] in ("New", "Contacted", "Replied", "Not interested"):
        new_stage = "Applied"
        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,'stage',?)", (partner_id, ts, f"{p['stage']} → Applied"))
    fills = {"contact_person": a["name"], "phone": a["phone"], "email": a["email"], "linkedin": a["profile"]}
    sets, vals = ["stage=?", "updated_at=?"], [new_stage, ts]
    for col, v in fills.items():
        if v and not (p[col] or "").strip():
            sets.append(f"{col}=?"); vals.append(v)
    c.execute(f"UPDATE partners SET {', '.join(sets)} WHERE id=?", vals + [partner_id])
    if not c.execute("SELECT 1 FROM tasks WHERE partner_id=? AND done_at IS NULL", (partner_id,)).fetchone():
        import crm
        crm.add_task(c, partner_id, "Review application and reply", (date.today() + timedelta(days=1)).isoformat(), "", "system")


def create_partner(c, app_id):
    a = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    ts = now()
    seg = "Individual" if not a["company"] else "Corporate"
    cur = c.execute("""INSERT INTO partners(name,category,segment,country,source,contact_person,email,phone,linkedin,description,stage,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'New',?,?)""",
                    (a["company"] or a["name"], a["applying_as"] or "Inbound applicant", seg, "India", "Youform inbound", a["name"], a["email"], a["phone"],
                     a["profile"], f"Inbound application. Clients: {a['clients']}. Products: {a['products']}.", ts, ts))
    pid = cur.lastrowid
    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,'added','Created from Youform application')", (pid, ts))
    apply_link(c, app_id, pid, "new inbound partner", "new")
    c.execute("UPDATE applications SET status='new_partner' WHERE id=?", (app_id,))
    return pid


def _alert(c, app, status):
    try:
        import notify
        notify.send(f"\U0001F4DD New application \u2014 {app.get('company') or app.get('name') or 'unknown'}"
                    f" ({'needs review' if status == 'review' else 'linked' if status == 'linked' else 'new partner'})\nhttp://127.0.0.1:8765/#/applications")
    except Exception:  # noqa
        pass


def ingest(c, app):
    """Insert one application (idempotent on external_id), auto-link when confident. Returns status string."""
    if c.execute("SELECT 1 FROM applications WHERE external_id=?", (app["external_id"],)).fetchone():
        return "duplicate"
    cur = c.execute("""INSERT INTO applications(external_id,received_at,name,email,phone,company,applying_as,clients,products,profile,status,raw,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'review',?,?)""",
                    (app["external_id"], app["received_at"], app["name"], app["email"], app["phone"], app.get("company", ""), app.get("applying_as", ""),
                     app.get("clients", ""), app.get("products", ""), app.get("profile", ""), app.get("raw", ""), now()))
    aid = cur.lastrowid
    cands = find_candidates(c, app)
    c.execute("UPDATE applications SET candidates=? WHERE id=?", (json.dumps(cands), aid))
    highs = [x for x in cands if x[1] == "high"]
    if len(highs) == 1:
        apply_link(c, aid, highs[0][0], highs[0][2], "high")
        _alert(c, app, "linked")
        return "linked"
    if not cands:
        create_partner(c, aid)
        _alert(c, app, "new_partner")
        return "new_partner"
    _alert(c, app, "review")
    return "review"


def ingest_many(c, apps):
    res = {"linked": 0, "new_partner": 0, "review": 0, "duplicate": 0}
    for a in apps:
        res[ingest(c, a)] += 1
    return res


# ---------------- relay (Google Apps Script) ----------------
def _get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "partner-crm"}), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def pull_relay():
    cfg = load_cfg()
    if not (cfg["relay_url"] and cfg["relay_token"]):
        raise ValueError("Relay not configured")
    base = cfg["relay_url"] + ("&" if "?" in cfg["relay_url"] else "?")
    data = _get(base + urllib.parse.urlencode({"action": "pull", "token": cfg["relay_token"]}))
    if not data.get("ok"):
        raise ValueError(data.get("error", "relay error"))
    rows = data.get("rows", [])
    apps, ids = [], []
    for r in rows:
        ids.append(r["id"])
        a = from_webhook(r["payload"])
        if a and (not a["form_id"] or a["form_id"] == FORM_SLUG):
            apps.append(a)
    c = db.connect()
    try:
        res = ingest_many(c, apps)
        c.commit()
    finally:
        c.close()
    if ids:  # ack only after the CRM has safely stored them
        _get(base + urllib.parse.urlencode({"action": "ack", "token": cfg["relay_token"], "ids": ",".join(map(str, ids))}))
    res["pulled"] = len(rows)
    return res
