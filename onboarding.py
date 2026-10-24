"""Partner onboarding: due-diligence checklist, document storage, signed agreement, AppsFlyer link, onboard gate."""
import os
import re
import sqlite3
import urllib.parse
import uuid
from datetime import datetime

import attribution
import commercials
import crm
import geo

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.environ.get("PARTNER_CRM_ONB_DIR", os.path.join(HERE, "data", "onboarding"))
MAX_BYTES = 25 * 1024 * 1024
BLOCKED_EXT = {".html", ".htm", ".svg", ".js", ".exe", ".sh", ".bat", ".app", ".dmg", ".jar", ".php"}
INLINE_EXT = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
STATUSES = ["Collecting", "In review", "On hold", "Rejected", "Onboarded"]
ITEM_STATUSES = ["pending", "received", "verified", "rejected", "na"]
ENTITY_TYPES = ["Individual", "Corporate", "Bank"]
now = lambda: datetime.now().isoformat(timespec="seconds")

# (key, label, required). The signed agreement is always required.
COMMON = [
    ("registration", "Registration certificate (AMFI ARN / SEBI registration)", 1),
    ("pan", "PAN card (partner / entity)", 1),
    ("kyc_id", "ID proof of partner or authorised signatory", 1),
    ("address", "Address proof", 1),
    ("bank", "Cancelled cheque / bank proof", 1),
]
CORPORATE = [
    ("incorporation", "Incorporation / constitution documents", 1),
    ("authorisation", "Board resolution or authorisation letter", 1),
    ("ubo", "Directors / partners & beneficial owners list", 1),
]
FINAL = [
    ("agreement", "Signed partner agreement (final)", 1),
    ("other", "Other supporting documents", 0),
]


# Foreign partners don't have an Indian ARN/PAN; same keys, different wording, plus tax self-certification and a sanctions/AML check.
FOREIGN_LABELS = {
    "registration": "Regulatory licence / registration in home country",
    "pan": "Tax ID (TIN / equivalent) of partner or entity",
    "kyc_id": "Passport or national ID of partner / authorised signatory",
    "address": "Address proof",
    "bank": "Bank proof (statement or letter with SWIFT / IBAN)",
}
FOREIGN_EXTRA = [("tax_cert", "FATCA / CRS self-certification", 1), ("aml", "Sanctions / AML questionnaire or declaration", 1)]


def default_items(entity, region="Indian"):
    items = COMMON + (CORPORATE if entity in ("Corporate", "Bank") else [])
    if region == "Foreign":
        items = [(k, FOREIGN_LABELS.get(k, label), req) for k, label, req in items] + FOREIGN_EXTRA
    return items + FINAL


def _entity_for(p):
    return p["segment"] if p["segment"] in ENTITY_TYPES else "Individual"


def _log(c, pid, text):
    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, now(), "onboarding", text))


def _region(c, pid):
    r = c.execute("SELECT country FROM partners WHERE id=?", (pid,)).fetchone()
    return geo.region_of(r["country"] if r else "India")


def _seed(c, pid, entity):
    """Add any missing default items and re-word existing defaults for the partner's region."""
    region = _region(c, pid)
    have = {r[0] for r in c.execute("SELECT key FROM onboarding_items WHERE partner_id=?", (pid,))}
    for i, (k, label, req) in enumerate(default_items(entity, region)):
        if k not in have:
            # agreement/other always sort after everything else, including items added later
            c.execute("INSERT INTO onboarding_items(partner_id,key,label,required,sort,updated_at) VALUES(?,?,?,?,?,?)",
                      (pid, k, label, req, 1000 + i if k in ("agreement", "other") else i, now()))
        else:
            c.execute("UPDATE onboarding_items SET label=? WHERE partner_id=? AND key=?", (label, pid, k))


def refresh_region(c, pid):
    """Called when a partner's country changes mid-onboarding."""
    o = c.execute("SELECT entity_type, status FROM onboarding WHERE partner_id=?", (pid,)).fetchone()
    if o and o["status"] != "Onboarded":
        _seed(c, pid, o["entity_type"])


def start(c, pid):
    p = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
    if not p:
        raise ValueError("partner not found")
    if c.execute("SELECT 1 FROM onboarding WHERE partner_id=?", (pid,)).fetchone():
        return {"ok": True, "existing": True}
    ent = _entity_for(p)
    c.execute("INSERT INTO onboarding(partner_id,entity_type,started_at,updated_at) VALUES(?,?,?,?)", (pid, ent, now(), now()))
    _seed(c, pid, ent)
    reg = geo.region_of(p["country"])
    try:
        commercials.copy_from(c, pid, "standard")
    except ValueError:
        commercials.save(c, pid, {"currency": "INR" if reg == "Indian" else "USD"})
    c.execute("UPDATE commercials SET currency=COALESCE(NULLIF(currency,''),'INR') WHERE partner_id=?", (pid,))
    _log(c, pid, "Onboarding started")
    if p["stage"] in ("New", "Contacted", "Replied", "Applied", "Not interested"):
        c.execute("UPDATE partners SET stage='In discussion', updated_at=? WHERE id=?", (now(), pid))
        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, now(), "stage", f"{p['stage']} → In discussion"))
    return {"ok": True}


# ---------------- AppsFlyer link ----------------
def check_link(link, partner, cfg=None):
    """Parse a pasted OneLink and say whether the CRM's attribution sync will be able to track it."""
    cfg = cfg or attribution.load_cfg()
    link = (link or "").strip()
    if not link:
        return None
    u = urllib.parse.urlparse(link)
    q = urllib.parse.parse_qs(u.query)
    code = (q.get("c") or [""])[0]
    pid_ = (q.get("pid") or [""])[0]
    if attribution.is_path_style(link) and not code:
        code = u.path.rstrip("/").rsplit("/", 1)[-1]
        pid_ = cfg["media_source"]
    issues = []
    if u.scheme != "https" or not u.netloc:
        issues.append("Not an https:// link.")
    if not code:
        issues.append("No campaign (c=…) in the link, so installs can't be matched to this partner.")
    elif not attribution.SAFE.match(code):
        issues.append("Campaign value has unsupported characters.")
    if not pid_:
        issues.append("No pid=… (media source) in the link.")
    elif cfg["media_source"] and pid_ != cfg["media_source"]:
        issues.append(f"pid is “{pid_}” but the CRM tracks media source “{cfg['media_source']}” (Settings → Partner attribution). Installs won't be counted until they match.")
    return {"code": code, "pid": pid_, "trackable": not issues, "issues": issues,
            "matches_code": bool(code) and code.lower() == attribution.code_for(partner).lower()}


def _apply_link(c, pid, link):
    """A link whose c= differs from the partner's code makes that value the partner code, so the sync counts it."""
    p = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
    chk = check_link(link, p)
    if chk and chk["code"] and attribution.SAFE.match(chk["code"]) and not chk["matches_code"]:
        if c.execute("SELECT 1 FROM partners WHERE id<>? AND LOWER(partner_code)=LOWER(?)", (pid, chk["code"])).fetchone():
            raise ValueError(f"campaign code “{chk['code']}” is already used by another partner")
        c.execute("UPDATE partners SET partner_code=?, updated_at=? WHERE id=?", (chk["code"], now(), pid))
        _log(c, pid, f"Referral code set to {chk['code']} from the AppsFlyer link")


# ---------------- reads ----------------
def blockers(c, pid):
    out = []
    items = c.execute("SELECT * FROM onboarding_items WHERE partner_id=?", (pid,)).fetchall()
    nfiles = {r[0]: r[1] for r in c.execute("SELECT item_key, COUNT(*) FROM onboarding_files WHERE partner_id=? GROUP BY 1", (pid,))}
    open_ = [i for i in items if i["required"] and i["status"] not in ("verified", "na")]
    if open_:
        out.append(f"{len(open_)} required item{'s' if len(open_) != 1 else ''} not verified: " + ", ".join(i["label"].split(" (")[0] for i in open_[:4]) + ("…" if len(open_) > 4 else ""))
    if not nfiles.get("agreement"):
        out.append("Signed agreement not uploaded")
    if commercials.get(c, pid)["status"] != "Agreed":
        out.append("Commercials not agreed with the partner")
    return out


def progress(c, pid):
    items = c.execute("SELECT required,status FROM onboarding_items WHERE partner_id=?", (pid,)).fetchall()
    req = [i for i in items if i["required"] and i["status"] != "na"]
    return {"done": sum(1 for i in req if i["status"] == "verified"), "total": len(req)}


def list_all(c):
    rows = c.execute("""SELECT o.*, p.name, p.segment, p.city, p.country, p.stage, p.contact_person FROM onboarding o JOIN partners p ON p.id=o.partner_id
                        ORDER BY CASE o.status WHEN 'In review' THEN 0 WHEN 'Collecting' THEN 1 WHEN 'On hold' THEN 2 WHEN 'Onboarded' THEN 3 ELSE 4 END, o.updated_at DESC""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["progress"] = progress(c, r["partner_id"])
        d["has_agreement"] = bool(c.execute("SELECT 1 FROM onboarding_files WHERE partner_id=? AND item_key='agreement'", (r["partner_id"],)).fetchone())
        d["has_link"] = bool((r["appsflyer_link"] or "").strip())
        d["commercials"] = commercials.get(c, r["partner_id"])["status"]
        d["country"] = geo.norm_country(r["country"]); d["region"] = geo.region_of(r["country"])
        d.pop("notes", None)
        out.append(d)
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] += 1
    return {"rows": out, "counts": counts}


def candidates(c, q=""):
    """Partners not yet in onboarding, furthest along first."""
    w, a = "", []
    if q:
        w, a = " AND (p.name LIKE ? OR p.email LIKE ? OR p.contact_person LIKE ?)", [f"%{q}%"] * 3
    return [dict(r) for r in c.execute(f"""SELECT p.id,p.name,p.segment,p.city,p.stage,p.email FROM partners p
        WHERE NOT EXISTS (SELECT 1 FROM onboarding o WHERE o.partner_id=p.id){w}
        ORDER BY CASE p.stage WHEN 'Applied' THEN 0 WHEN 'In discussion' THEN 1 WHEN 'Replied' THEN 2 WHEN 'Contacted' THEN 3 WHEN 'New' THEN 4 ELSE 5 END, p.name LIMIT 40""", a)]


def detail(c, pid):
    o = c.execute("SELECT * FROM onboarding WHERE partner_id=?", (pid,)).fetchone()
    if not o:
        return None
    p = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
    files = {}
    for f in c.execute("SELECT id,item_key,orig_name,size,uploaded_at FROM onboarding_files WHERE partner_id=? ORDER BY id", (pid,)):
        files.setdefault(f["item_key"], []).append(dict(f))
    items = [{**dict(i), "files": files.get(i["key"], [])} for i in c.execute("SELECT * FROM onboarding_items WHERE partner_id=? ORDER BY sort,id", (pid,))]
    cfg = attribution.load_cfg()
    return {"onboarding": dict(o), "partner": {k: p[k] for k in ("id", "name", "segment", "city", "stage", "email", "contact_person", "do_not_contact")},
            "items": items, "blockers": [] if o["status"] == "Onboarded" else blockers(c, pid), "progress": progress(c, pid),
            "link_check": check_link(o["appsflyer_link"], p, cfg), "code": attribution.code_for(p), "distributor_code": p["distributor_code"] or "",
            "generated_link": attribution.link_for(p, cfg), "commercials": commercials.get(c, pid), "commercial_options": commercials.options(),
            "country": geo.norm_country(p["country"]), "region": geo.region_of(p["country"]),
            "log": [dict(r) for r in c.execute("SELECT ts,body FROM activities WHERE partner_id=? AND type='onboarding' ORDER BY ts DESC,id DESC", (pid,))]}


# ---------------- writes ----------------
def patch(c, pid, body):
    o = c.execute("SELECT * FROM onboarding WHERE partner_id=?", (pid,)).fetchone()
    if not o:
        raise ValueError("onboarding not started")
    ch = {}
    for k in ("reviewer", "agreement_date", "notes", "appsflyer_link", "entity_type", "status"):
        if k in body:
            ch[k] = (body[k] or "").strip() if isinstance(body[k], str) else body[k]
    if "status" in ch:
        if ch["status"] not in STATUSES or ch["status"] == "Onboarded":
            raise ValueError("use Mark onboarded to complete onboarding")
        if o["status"] == "Onboarded":
            raise ValueError("already onboarded — use Reopen first")
    if o["status"] == "Onboarded" and set(ch) - {"notes", "reviewer", "appsflyer_link"}:
        raise ValueError("onboarding is complete — Reopen to change these")
    if "entity_type" in ch and ch["entity_type"] not in ENTITY_TYPES:
        raise ValueError("bad entity type")
    if ch.get("appsflyer_link"):
        if not re.match(r"^https://\S+$", ch["appsflyer_link"]):
            raise ValueError("AppsFlyer link must be an https:// URL")
        _apply_link(c, pid, ch["appsflyer_link"])
    if not ch:
        return {"ok": True}
    c.execute(f"UPDATE onboarding SET {', '.join(k + '=?' for k in ch)}, updated_at=? WHERE partner_id=?", [*ch.values(), now(), pid])
    if "entity_type" in ch and ch["entity_type"] != o["entity_type"]:
        _seed(c, pid, ch["entity_type"])
    if "status" in ch and ch["status"] != o["status"]:
        _log(c, pid, f"Onboarding status: {o['status']} → {ch['status']}")
    if "appsflyer_link" in ch and ch["appsflyer_link"] != (o["appsflyer_link"] or ""):
        _log(c, pid, "AppsFlyer link " + ("saved" if ch["appsflyer_link"] else "removed"))
    return {"ok": True}


def _locked(c, pid):
    if c.execute("SELECT status FROM onboarding WHERE partner_id=?", (pid,)).fetchone()["status"] == "Onboarded":
        raise ValueError("onboarding is complete — Reopen to change documents")


def patch_item(c, pid, key, body):
    it = c.execute("SELECT * FROM onboarding_items WHERE partner_id=? AND key=?", (pid, key)).fetchone()
    if not it:
        raise LookupError(key)
    _locked(c, pid)
    ch = {}
    if "status" in body:
        if body["status"] not in ITEM_STATUSES:
            raise ValueError("bad status")
        if body["status"] == "verified" and not c.execute("SELECT 1 FROM onboarding_files WHERE partner_id=? AND item_key=?", (pid, key)).fetchone():
            raise ValueError("upload a document before marking this verified (or use N/A)")
        if key == "agreement" and body["status"] == "na":
            raise ValueError("the signed agreement can't be N/A")
        ch["status"] = body["status"]
        ch["verified_at"] = now() if body["status"] == "verified" else None
    if "note" in body:
        ch["note"] = str(body["note"] or "").strip()
    if "required" in body and key != "agreement":
        ch["required"] = 1 if body["required"] else 0
    if ch:
        c.execute(f"UPDATE onboarding_items SET {', '.join(k + '=?' for k in ch)}, updated_at=? WHERE id=?", [*ch.values(), now(), it["id"]])
        if "status" in ch and ch["status"] != it["status"]:
            _log(c, pid, f"{it['label'].split(' (')[0]}: {it['status']} → {ch['status']}")
    return {"ok": True}


def add_item(c, pid, body):
    _locked(c, pid)
    label = str(body.get("label") or "").strip()[:120]
    if not label:
        raise ValueError("item name required")
    key = "x_" + uuid.uuid4().hex[:8]
    top = c.execute("SELECT COALESCE(MAX(sort),0) FROM onboarding_items WHERE partner_id=? AND sort<1000", (pid,)).fetchone()[0]
    c.execute("INSERT INTO onboarding_items(partner_id,key,label,required,sort,updated_at) VALUES(?,?,?,?,?,?)", (pid, key, label, 1 if body.get("required", True) else 0, top + 1, now()))
    return {"ok": True, "key": key}


def delete_item(c, pid, key):
    _locked(c, pid)
    if not key.startswith("x_"):
        raise ValueError("standard items can be marked N/A or optional, not deleted")
    if c.execute("SELECT 1 FROM onboarding_files WHERE partner_id=? AND item_key=?", (pid, key)).fetchone():
        raise ValueError("remove its documents first")
    c.execute("DELETE FROM onboarding_items WHERE partner_id=? AND key=?", (pid, key))
    return {"ok": True}


def distributor_prefix(p):
    """FD = foreign distributor, II = Indian influencer, ID = every other Indian partner (MFDs, advisers)."""
    if geo.region_of(p["country"]) == "Foreign":
        return "FD"
    return "II" if (p["category"] or "").lower().startswith("community") else "ID"


def assign_distributor_code(c, pid, when=None):
    """<FD|ID|II><MMYY of onboarding><3-digit running number, per type and month>. Kept for good once assigned."""
    p = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
    if (p["distributor_code"] or "").strip():
        return p["distributor_code"]
    when = when or now()
    head = distributor_prefix(p) + when[5:7] + when[2:4]
    used = [int(r[0][len(head):]) for r in c.execute("SELECT distributor_code FROM partners WHERE distributor_code LIKE ?", (head + "%",))
            if r[0][len(head):].isdigit()]
    n = max(used, default=0) + 1
    while True:                      # the database also enforces uniqueness; skip ahead if another request took this number
        code = f"{head}{n:03d}"
        try:
            c.execute("UPDATE partners SET distributor_code=?, updated_at=? WHERE id=?", (code, now(), pid))
            break
        except sqlite3.IntegrityError:
            n += 1
    _log(c, pid, f"Distributor code {code} assigned")
    return code


def backfill_distributor_codes(c):
    """Give a code to every already-onboarded partner that has none, in the order they were onboarded."""
    done = []
    for r in c.execute("""SELECT o.partner_id, o.onboarded_at FROM onboarding o JOIN partners p ON p.id=o.partner_id
                          WHERE o.status='Onboarded' AND COALESCE(p.distributor_code,'')='' ORDER BY o.onboarded_at, o.partner_id""").fetchall():
        done.append((r["partner_id"], assign_distributor_code(c, r["partner_id"], r["onboarded_at"] or now())))
    return done


def complete(c, pid):
    if not c.execute("SELECT 1 FROM onboarding WHERE partner_id=?", (pid,)).fetchone():
        raise ValueError("onboarding not started")
    b = blockers(c, pid)
    if b:
        raise ValueError("Can't onboard yet: " + "; ".join(b))
    p = c.execute("SELECT stage FROM partners WHERE id=?", (pid,)).fetchone()
    c.execute("UPDATE onboarding SET status='Onboarded', onboarded_at=?, updated_at=? WHERE partner_id=?", (now(), now(), pid))
    c.execute("UPDATE partners SET stage='Onboarded', updated_at=? WHERE id=?", (now(), pid))
    crm.close_open_tasks(c, pid)
    assign_distributor_code(c, pid)
    if p["stage"] != "Onboarded":
        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, now(), "stage", f"{p['stage']} → Onboarded"))
    _log(c, pid, "Onboarded: required documents verified, agreement and AppsFlyer link on file; commercials agreed")
    return {"ok": True}


def reopen(c, pid, reason=""):
    o = c.execute("SELECT status FROM onboarding WHERE partner_id=?", (pid,)).fetchone()
    if not o or o["status"] != "Onboarded":
        raise ValueError("not onboarded")
    c.execute("UPDATE onboarding SET status='In review', onboarded_at=NULL, updated_at=? WHERE partner_id=?", (now(), pid))
    c.execute("UPDATE partners SET stage='In discussion', updated_at=? WHERE id=?", (now(), pid))
    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, now(), "stage", "Onboarded → In discussion"))
    _log(c, pid, "Onboarding reopened" + (f": {reason}" if reason else ""))
    return {"ok": True}


# ---------------- files ----------------
def _safe_name(n):
    n = os.path.basename(urllib.parse.unquote(n or "file")).replace("\x00", "")
    n = re.sub(r"[^\w.\- ()]+", "_", n).strip(" .") or "file"
    return n[-120:]


def save_file(c, pid, key, name, data):
    if not c.execute("SELECT 1 FROM onboarding WHERE partner_id=?", (pid,)).fetchone():
        raise ValueError("onboarding not started")
    if not c.execute("SELECT 1 FROM onboarding_items WHERE partner_id=? AND key=?", (pid, key)).fetchone():
        raise ValueError("unknown checklist item")
    _locked(c, pid)
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_BYTES:
        raise ValueError("file is larger than 25 MB")
    name = _safe_name(name)
    ext = os.path.splitext(name)[1].lower()
    if ext in BLOCKED_EXT:
        raise ValueError("this file type isn't allowed")
    d = os.path.join(DIR, str(pid))
    os.makedirs(d, mode=0o700, exist_ok=True)
    stored = uuid.uuid4().hex + ext
    fd = os.open(os.path.join(d, stored), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    cur = c.execute("INSERT INTO onboarding_files(partner_id,item_key,orig_name,stored_name,size,uploaded_at) VALUES(?,?,?,?,?,?)", (pid, key, name, stored, len(data), now()))
    it = c.execute("SELECT * FROM onboarding_items WHERE partner_id=? AND key=?", (pid, key)).fetchone()
    if it["status"] in ("pending", "rejected"):
        c.execute("UPDATE onboarding_items SET status='received', updated_at=? WHERE id=?", (now(), it["id"]))
    c.execute("UPDATE onboarding SET updated_at=? WHERE partner_id=?", (now(), pid))
    _log(c, pid, f"Uploaded “{name}” for {it['label'].split(' (')[0]}")
    return {"ok": True, "id": cur.lastrowid}


def file_path(c, fid):
    f = c.execute("SELECT * FROM onboarding_files WHERE id=?", (fid,)).fetchone()
    if not f:
        return None, None
    fp = os.path.realpath(os.path.join(DIR, str(f["partner_id"]), f["stored_name"]))
    if not fp.startswith(os.path.realpath(DIR) + os.sep) or not os.path.isfile(fp):
        return None, None
    return fp, f


def delete_file(c, fid):
    f = c.execute("SELECT * FROM onboarding_files WHERE id=?", (fid,)).fetchone()
    if not f:
        raise LookupError("file")
    _locked(c, f["partner_id"])
    fp, _ = file_path(c, fid)
    if fp:
        os.remove(fp)
    c.execute("DELETE FROM onboarding_files WHERE id=?", (fid,))
    left = c.execute("SELECT COUNT(*) FROM onboarding_files WHERE partner_id=? AND item_key=?", (f["partner_id"], f["item_key"])).fetchone()[0]
    it = c.execute("SELECT * FROM onboarding_items WHERE partner_id=? AND key=?", (f["partner_id"], f["item_key"])).fetchone()
    if it and not left and it["status"] in ("received", "verified"):
        c.execute("UPDATE onboarding_items SET status='pending', verified_at=NULL, updated_at=? WHERE id=?", (now(), it["id"]))
    _log(c, f["partner_id"], f"Removed “{f['orig_name']}”")
    return {"ok": True}


# ---------------- commercials ----------------
def save_commercials(c, pid, body):
    if not c.execute("SELECT 1 FROM onboarding WHERE partner_id=?", (pid,)).fetchone():
        raise ValueError("onboarding not started")
    _locked(c, pid)
    r = commercials.save(c, pid, body)
    if r["changed"]:
        _log(c, pid, "Commercials edited" + (" (back to Draft, re-confirm with the partner)" if r["reset_to_draft"] else ""))
    return r


def commercials_status(c, pid, status, by=""):
    _locked(c, pid)
    r = commercials.set_status(c, pid, status, by)
    if status == "Agreed":
        _log(c, pid, f"Commercials agreed (confirmed by {by.strip()}): " + " / ".join(r["summary"])[:600])
    elif status == "Proposed":
        _log(c, pid, "Commercials proposed to the partner: " + " / ".join(r["summary"])[:600])
    return {"ok": True}


def commercials_copy(c, pid, source):
    _locked(c, pid)
    r = commercials.copy_from(c, pid, source)
    _log(c, pid, "Commercials copied from " + ("the standard rate card" if source == "standard" else f"partner #{int(source)}"))
    return r
