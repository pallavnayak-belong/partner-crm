"""CRM core: extra contacts, tasks, tags, saved views, team, archive / lost reason, activity edits, merge, delete (erase) and export."""
import json
import os
import re
import shutil
from datetime import date, datetime, timedelta

import backup
import db
import geo

now = lambda: datetime.now().isoformat(timespec="seconds")
LOST_REASONS = ["No response", "Not interested", "Using another platform", "Not a fit (size / type)", "Compliance or regulatory", "Unsubscribed", "Other"]
EDITABLE_ACTIVITY_TYPES = {"note", "call", "meeting", "whatsapp", "email"}
SYSTEM_EMAIL_PREFIX = "Sent “"
EMAIL_RE = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}")


def _log(c, pid, typ, body):
    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, now(), typ, body))


def _partner(c, pid):
    p = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
    if not p:
        raise LookupError(f"partner {pid}")
    return p


# ---------------- tasks (partners.follow_up caches the earliest open due date so lists and counts stay cheap) ----------------
def sync_follow_up(c, pid):
    r = c.execute("SELECT MIN(due) FROM tasks WHERE partner_id=? AND done_at IS NULL", (pid,)).fetchone()[0]
    c.execute("UPDATE partners SET follow_up=?, updated_at=? WHERE id=?", (r or "", now(), pid))


def add_task(c, pid, title, due, assignee="", by=""):
    _partner(c, pid)
    title = str(title or "").strip()[:200]
    if not title:
        raise ValueError("Describe the task")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due or "")):
        raise ValueError("Choose a due date")
    cur = c.execute("INSERT INTO tasks(partner_id,title,due,assignee,created_at,created_by) VALUES(?,?,?,?,?,?)", (pid, title, due, (assignee or "").strip(), now(), by))
    sync_follow_up(c, pid)
    return {"id": cur.lastrowid}


def update_task(c, tid, body, by=""):
    t = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        raise LookupError("task")
    ch = {}
    if "title" in body:
        if not str(body["title"]).strip():
            raise ValueError("Describe the task")
        ch["title"] = str(body["title"]).strip()[:200]
    if "due" in body:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body["due"] or "")):
            raise ValueError("Choose a due date")
        ch["due"] = body["due"]
    if "assignee" in body:
        ch["assignee"] = str(body["assignee"] or "").strip()
    if "done" in body:
        ch["done_at"] = now() if body["done"] else None
    if ch:
        c.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in ch)} WHERE id=?", [*ch.values(), tid])
        if ch.get("done_at"):
            _log(c, t["partner_id"], "note", f"Task done: {t['title']}")
        sync_follow_up(c, t["partner_id"])
    return {"ok": True}


def delete_task(c, tid):
    t = c.execute("SELECT partner_id FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        raise LookupError("task")
    c.execute("DELETE FROM tasks WHERE id=?", (tid,))
    sync_follow_up(c, t["partner_id"])
    return {"ok": True}


def close_open_tasks(c, pid):
    c.execute("UPDATE tasks SET done_at=? WHERE partner_id=? AND done_at IS NULL", (now(), pid))
    sync_follow_up(c, pid)


def partner_tasks(c, pid):
    return [dict(r) for r in c.execute("SELECT * FROM tasks WHERE partner_id=? ORDER BY (done_at IS NOT NULL), due, id", (pid,))]


def open_tasks(c):
    return [dict(r) for r in c.execute("""SELECT t.id, t.partner_id, t.title, t.due, t.assignee, p.name, p.stage, p.contact_person, p.email
        FROM tasks t JOIN partners p ON p.id=t.partner_id WHERE t.done_at IS NULL AND COALESCE(p.archived,0)=0 ORDER BY t.due, t.id""")]


# ---------------- extra contacts ----------------
def _contact_fields(body):
    out = {}
    for k in ("name", "designation", "email", "phone", "role"):
        if k in body:
            out[k] = str(body[k] or "").strip()[:160]
    if out.get("email") and not EMAIL_RE.fullmatch(out["email"]):
        raise ValueError("That email address doesn't look right")
    return out


def add_contact(c, pid, body):
    _partner(c, pid)
    f = _contact_fields(body)
    if not (f.get("name") or f.get("email") or f.get("phone")):
        raise ValueError("Add at least a name, email or phone")
    cur = c.execute("INSERT INTO partner_contacts(partner_id,name,designation,email,phone,role,created_at) VALUES(?,?,?,?,?,?,?)",
                    (pid, f.get("name", ""), f.get("designation", ""), f.get("email", ""), f.get("phone", ""), f.get("role", ""), now()))
    _log(c, pid, "note", f"Contact added: {f.get('name') or f.get('email') or f.get('phone')}" + (f" ({f['role']})" if f.get("role") else ""))
    return {"id": cur.lastrowid}


def update_contact(c, cid, body):
    k = c.execute("SELECT * FROM partner_contacts WHERE id=?", (cid,)).fetchone()
    if not k:
        raise LookupError("contact")
    f = _contact_fields(body)
    if "email" in f and f["email"].lower() != (k["email"] or "").lower():
        f["bounced"] = 0
    if f:
        c.execute(f"UPDATE partner_contacts SET {', '.join(x + '=?' for x in f)} WHERE id=?", [*f.values(), cid])
    return {"ok": True}


def delete_contact(c, cid):
    k = c.execute("SELECT * FROM partner_contacts WHERE id=?", (cid,)).fetchone()
    if not k:
        raise LookupError("contact")
    c.execute("DELETE FROM partner_contacts WHERE id=?", (cid,))
    _log(c, k["partner_id"], "note", f"Contact removed: {k['name'] or k['email'] or k['phone']}")
    return {"ok": True}


def make_primary(c, cid):
    """Swap: the chosen contact becomes the partner's primary, and the old primary becomes an additional contact."""
    k = c.execute("SELECT * FROM partner_contacts WHERE id=?", (cid,)).fetchone()
    if not k:
        raise LookupError("contact")
    p = _partner(c, k["partner_id"])
    c.execute("UPDATE partner_contacts SET name=?,designation=?,email=?,phone=?,bounced=0 WHERE id=?",
              (p["contact_person"] or "", p["designation"] or "", p["email"] or "", p["phone"] or "", cid))
    c.execute("UPDATE partners SET contact_person=?,designation=?,email=?,phone=?,updated_at=? WHERE id=?",
              (k["name"], k["designation"], k["email"], k["phone"], now(), p["id"]))
    _log(c, p["id"], "note", f"Primary contact is now {k['name'] or k['email']}")
    return {"ok": True}


def contacts_of(c, pid):
    return [dict(r) for r in c.execute("SELECT * FROM partner_contacts WHERE partner_id=? ORDER BY id", (pid,))]


def recipients(c, pid):
    """Everyone at this partner who can be emailed: primary first."""
    p = _partner(c, pid)
    out = []
    if (p["email"] or "").strip():
        out.append({"email": p["email"].strip(), "name": p["contact_person"] or "", "primary": True, "bounced": False})
    for k in contacts_of(c, pid):
        if (k["email"] or "").strip():
            out.append({"email": k["email"].strip(), "name": k["name"] or "", "primary": False, "bounced": bool(k["bounced"]), "role": k["role"] or ""})
    return out


# ---------------- tags ----------------
def _tag_id(c, name):
    name = " ".join(str(name or "").split())[:40]
    if not name:
        raise ValueError("Enter a tag")
    r = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
    return r[0] if r else c.execute("INSERT INTO tags(name) VALUES(?)", (name,)).lastrowid


def add_tag(c, pid, name):
    _partner(c, pid)
    tid = _tag_id(c, name)
    c.execute("INSERT OR IGNORE INTO partner_tags(partner_id,tag_id) VALUES(?,?)", (pid, tid))
    return {"tag_id": tid}


def remove_tag(c, pid, tid):
    c.execute("DELETE FROM partner_tags WHERE partner_id=? AND tag_id=?", (pid, tid))
    return {"ok": True}


def bulk_tag(c, ids, name, action="add"):
    if action not in ("add", "remove"):
        raise ValueError("bad action")
    tid = _tag_id(c, name)
    for pid in ids:
        if action == "add":
            c.execute("INSERT OR IGNORE INTO partner_tags(partner_id,tag_id) VALUES(?,?)", (int(pid), tid))
        else:
            c.execute("DELETE FROM partner_tags WHERE partner_id=? AND tag_id=?", (int(pid), tid))
    return {"ok": True, "n": len(ids)}


def tags_list(c):
    return [dict(r) for r in c.execute("SELECT t.id, t.name, COUNT(pt.partner_id) AS n FROM tags t LEFT JOIN partner_tags pt ON pt.tag_id=t.id GROUP BY t.id ORDER BY LOWER(t.name)")]


def partner_tags(c, pid):
    return [dict(r) for r in c.execute("SELECT t.id, t.name FROM partner_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.partner_id=? ORDER BY LOWER(t.name)", (pid,))]


def delete_tag(c, tid):
    c.execute("DELETE FROM partner_tags WHERE tag_id=?", (tid,))
    c.execute("DELETE FROM tags WHERE id=?", (tid,))
    return {"ok": True}


# ---------------- saved views ----------------
VIEW_KEYS = {"q", "segment", "region", "country", "tag", "owner", "mode", "archived"}


def views_list(c, page):
    return [{**dict(r), "filters": json.loads(r["filters"])} for r in c.execute("SELECT * FROM saved_views WHERE page=? ORDER BY LOWER(name)", (page,))]


def save_view(c, name, page, filters):
    name = str(name or "").strip()[:60]
    if not name:
        raise ValueError("Name this view")
    f = {k: str(v) for k, v in (filters or {}).items() if k in VIEW_KEYS}
    ex = c.execute("SELECT id FROM saved_views WHERE page=? AND LOWER(name)=LOWER(?)", (page, name)).fetchone()
    if ex:
        c.execute("UPDATE saved_views SET filters=? WHERE id=?", (json.dumps(f), ex[0]))
        return {"id": ex[0], "updated": True}
    return {"id": c.execute("INSERT INTO saved_views(name,page,filters,created_at) VALUES(?,?,?,?)", (name, page, json.dumps(f), now())).lastrowid}


def delete_view(c, vid):
    c.execute("DELETE FROM saved_views WHERE id=?", (vid,))
    return {"ok": True}


# ---------------- team ----------------
def team_list(c):
    return [dict(r) for r in c.execute("SELECT id, name FROM team_members ORDER BY LOWER(name)")]


def team_add(c, name):
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise ValueError("Enter a name")
    if c.execute("SELECT 1 FROM team_members WHERE name=?", (name,)).fetchone():
        raise ValueError("Already on the team")
    c.execute("INSERT INTO team_members(name) VALUES(?)", (name,))
    return {"rows": team_list(c)}


def team_remove(c, tid):
    c.execute("DELETE FROM team_members WHERE id=?", (tid,))
    return {"rows": team_list(c)}


# ---------------- archive / lost reason ----------------
def set_archived(c, pid, archived, reason=""):
    p = _partner(c, pid)
    c.execute("UPDATE partners SET archived=?, archive_reason=?, updated_at=? WHERE id=?", (1 if archived else 0, (reason or "").strip()[:200] if archived else None, now(), pid))
    _log(c, pid, "note", "Archived" + (f": {reason.strip()}" if archived and (reason or "").strip() else "") if archived else "Restored from archive")
    return {"ok": True}


def check_lost_reason(reason):
    if reason and reason not in LOST_REASONS:
        raise ValueError("Pick a reason from the list")
    return reason or ""


# ---------------- activity edits ----------------
def _activity(c, aid):
    a = c.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone()
    if not a:
        raise LookupError("activity")
    if a["type"] not in EDITABLE_ACTIVITY_TYPES or (a["type"] == "email" and (a["body"] or "").startswith(SYSTEM_EMAIL_PREFIX)):
        raise ValueError("System entries (sent emails, replies, stage changes) can't be changed. Only notes and calls you logged yourself can.")
    if (a["body"] or "").startswith(("Task done:", "Contact added:", "Contact removed:", "Primary contact is now", "Country:", "Marked do-not-contact", "Do-not-contact removed", "Archived", "Restored from archive")):
        raise ValueError("System entries can't be changed.")
    return a


def edit_activity(c, aid, body):
    a = _activity(c, aid)
    text = str(body or "").strip()
    if not text:
        raise ValueError("The entry can't be empty. Delete it instead.")
    c.execute("UPDATE activities SET body=?, edited=1 WHERE id=?", (text, aid))
    return {"ok": True, "partner_id": a["partner_id"]}


def delete_activity(c, aid):
    a = _activity(c, aid)
    c.execute("DELETE FROM activities WHERE id=?", (aid,))
    return {"ok": True, "partner_id": a["partner_id"]}


# ---------------- merge ----------------
def merge(c, keep_id, drop_id, by=""):
    """Fold `drop` into `keep`: history, emails, contacts, tasks, tags, applications and referred customers move across; empty fields are filled; `drop` is deleted."""
    if keep_id == drop_id:
        raise ValueError("Pick a different partner to merge")
    keep, drop = _partner(c, keep_id), _partner(c, drop_id)
    if c.execute("SELECT 1 FROM onboarding WHERE partner_id=?", (drop_id,)).fetchone():
        raise ValueError("The partner being merged away is in onboarding. Merge the other way round (keep the one with onboarding), or finish onboarding first.")
    backup.snapshot_before(f"merge-{drop_id}-into-{keep_id}")
    # fields: fill blanks on keep from drop; if the primary contact differs, keep the other person as an additional contact
    fill = {}
    for col in ("category", "segment", "city", "website", "linkedin", "other_social", "description", "owner", "priority", "remarks", "designation", "phone"):
        if not (keep[col] or "").strip() and (drop[col] or "").strip():
            fill[col] = drop[col]
    if not (keep["email"] or "").strip() and (drop["email"] or "").strip():
        fill["email"] = drop["email"]; fill["contact_person"] = drop["contact_person"]
    elif (drop["email"] or "").strip() and drop["email"].strip().lower() != (keep["email"] or "").strip().lower():
        c.execute("INSERT INTO partner_contacts(partner_id,name,designation,email,phone,role,created_at) VALUES(?,?,?,?,?,?,?)",
                  (keep_id, drop["contact_person"] or "", drop["designation"] or "", drop["email"], drop["phone"] or "", "from merged record", now()))
    if drop["universe_id"] and not keep["universe_id"]:
        c.execute("UPDATE partners SET universe_id=NULL WHERE id=?", (drop_id,))   # universe_id is UNIQUE
        fill["universe_id"] = drop["universe_id"]
    if drop["do_not_contact"] and not keep["do_not_contact"]:
        fill["do_not_contact"] = 1; fill["dnc_reason"] = drop["dnc_reason"] or "manual"
    if drop["partner_code"] and not keep["partner_code"]:
        c.execute("UPDATE partners SET partner_code=NULL WHERE id=?", (drop_id,))
        fill["partner_code"] = drop["partner_code"]
    order = ["New", "Contacted", "Replied", "Applied", "In discussion", "Onboarded"]
    if drop["stage"] in order and (keep["stage"] not in order or order.index(drop["stage"]) > order.index(keep["stage"])):
        fill["stage"] = drop["stage"]
    lc = max(filter(None, [keep["last_contacted"], drop["last_contacted"]]), default=None)
    if lc and lc != keep["last_contacted"]:
        fill["last_contacted"] = lc
    if fill:
        c.execute(f"UPDATE partners SET {', '.join(k + '=?' for k in fill)}, updated_at=? WHERE id=?", [*fill.values(), now(), keep_id])
    for tbl in ("activities", "emails", "applications", "referred_customers", "partner_contacts", "tasks"):
        c.execute(f"UPDATE {tbl} SET partner_id=? WHERE partner_id=?", (keep_id, drop_id))
    c.execute("UPDATE replies_seen SET partner_id=? WHERE partner_id=?", (keep_id, drop_id))
    c.execute("INSERT OR IGNORE INTO partner_tags(partner_id,tag_id) SELECT ?, tag_id FROM partner_tags WHERE partner_id=?", (keep_id, drop_id))
    c.execute("DELETE FROM partner_tags WHERE partner_id=?", (drop_id,))
    c.execute("DELETE FROM commercials WHERE partner_id=? AND ?=0", (drop_id, 1))
    c.execute("DELETE FROM partners WHERE id=?", (drop_id,))
    sync_follow_up(c, keep_id)
    _log(c, keep_id, "note", f"Merged with duplicate record “{drop['name']}” (#{drop_id}); its history, emails and contacts were moved here" + (f" by {by}" if by else ""))
    return {"ok": True, "kept": keep_id}


# ---------------- delete / erase ----------------
def has_history(c, pid):
    return bool(c.execute("SELECT 1 FROM emails WHERE partner_id=? AND status='sent'", (pid,)).fetchone() or
                c.execute("SELECT 1 FROM onboarding WHERE partner_id=?", (pid,)).fetchone())


def delete_partner(c, pid, confirm_name, reason="", by=""):
    """Permanent deletion of a partner and everything about them, including uploaded documents. Typed-name confirmation is required."""
    import onboarding as onb
    p = _partner(c, pid)
    if " ".join(str(confirm_name or "").split()).lower() != " ".join((p["name"] or "").split()).lower():
        raise ValueError("Type the partner's exact name to confirm")
    backup.snapshot_before(f"delete-{pid}")
    c.execute("DELETE FROM onboarding_files WHERE partner_id=?", (pid,))
    shutil.rmtree(os.path.join(onb.DIR, str(pid)), ignore_errors=True)
    backup.forget_documents(pid)
    for tbl in ("onboarding_items", "onboarding", "commercial_lines", "commercials", "partner_tags", "partner_contacts", "tasks", "activities", "emails",
                "applications", "referred_customers", "replies_seen"):
        c.execute(f"DELETE FROM {tbl} WHERE partner_id=?", (pid,))
    c.execute("DELETE FROM partners WHERE id=?", (pid,))
    c.execute("INSERT INTO erasure_log(partner_id,erased_at,by,reason) VALUES(?,?,?,?)", (pid, now(), by, (reason or "").strip()[:200]))
    try:
        import gdrive
        gdrive.forget_partner(pid)      # also remove their documents from the Google Drive backup
    except Exception:  # noqa
        pass
    return {"ok": True}


# ---------------- export (a partner's data, e.g. for a data request) ----------------
def export_partner(c, pid):
    p = _partner(c, pid)
    rows = lambda sql: [dict(r) for r in c.execute(sql, (pid,))]
    ob = c.execute("SELECT * FROM onboarding WHERE partner_id=?", (pid,)).fetchone()
    return {
        "exported_at": now(), "partner": dict(p), "country": geo.norm_country(p["country"]), "region": geo.region_of(p["country"]),
        "additional_contacts": rows("SELECT name,designation,email,phone,role FROM partner_contacts WHERE partner_id=?"),
        "tags": [t["name"] for t in partner_tags(c, pid)],
        "tasks": rows("SELECT title,due,assignee,done_at FROM tasks WHERE partner_id=?"),
        "activity": rows("SELECT ts,type,body,actor FROM activities WHERE partner_id=? ORDER BY ts"),
        "emails_sent": rows("SELECT sent_at,to_addr,cc,subject,status FROM emails WHERE partner_id=? ORDER BY sent_at"),
        "applications": rows("SELECT received_at,name,email,phone,company,applying_as,clients,products,profile FROM applications WHERE partner_id=?"),
        "onboarding": dict(ob) if ob else None,
        "onboarding_documents": rows("SELECT item_key,orig_name,size,uploaded_at FROM onboarding_files WHERE partner_id=?"),
        "commercials": {"terms": dict(c.execute("SELECT * FROM commercials WHERE partner_id=?", (pid,)).fetchone() or {}), "lines": rows("SELECT product,basis,rate,note FROM commercial_lines WHERE partner_id=?")},
        "referred_customer_count": c.execute("SELECT COUNT(*) FROM referred_customers WHERE partner_id=?", (pid,)).fetchone()[0],
    }


# ---------------- change history + undo ----------------
UNDOABLE = ["stage", "do_not_contact", "dnc_reason", "lost_reason", "owner", "name", "email", "phone", "contact_person", "designation", "country", "city",
            "category", "segment", "archived", "archive_reason", "priority", "website"]
INT_FIELDS = {"do_not_contact", "archived", "priority"}
FIELD_LABEL = {"do_not_contact": "do not contact", "dnc_reason": "do-not-contact reason", "lost_reason": "lost reason", "contact_person": "contact person", "archive_reason": "archive reason"}


def recent_changes(c, partner_id=None, limit=80, hours=None):
    """Edits to partner details grouped per partner and moment (so a stage move + do-not-contact flag is one undoable item)."""
    sql, args = "SELECT l.*, p.name AS partner FROM change_log l LEFT JOIN partners p ON p.id=l.partner_id", []
    cond = []
    if partner_id:
        cond.append("l.partner_id=?"); args.append(int(partner_id))
    if hours:
        cond.append("l.ts>=?"); args.append((datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds"))
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY l.id DESC LIMIT ?"
    args.append(int(limit) * 6)
    groups, order = {}, []
    for r in c.execute(sql, args):
        k = (r["partner_id"], r["ts"], r["actor"])
        if k not in groups:
            groups[k] = {"partner_id": r["partner_id"], "partner": r["partner"], "ts": r["ts"], "actor": r["actor"] or "system", "changes": []}
            order.append(k)
        groups[k]["changes"].append({"id": r["id"], "field": r["field"], "label": FIELD_LABEL.get(r["field"], r["field"]), "old": r["old"], "new": r["new"], "undone": bool(r["undone"])})
    out = [groups[k] for k in order][:limit]
    for g in out:
        g["undoable"] = any(not x["undone"] for x in g["changes"])
    return out


def undo_changes(c, ids, by=""):
    """Put fields back to the value they had before the given change_log rows. A field someone changed again since is left alone and reported."""
    done, skipped = 0, []
    for cid in [int(i) for i in ids][:200]:
        ch = c.execute("SELECT * FROM change_log WHERE id=?", (cid,)).fetchone()
        if not ch or ch["undone"] or ch["field"] not in UNDOABLE:
            continue
        p = c.execute("SELECT * FROM partners WHERE id=?", (ch["partner_id"],)).fetchone()
        if not p:
            skipped.append("that partner no longer exists"); continue
        cur = p[ch["field"]]
        if (None if cur is None else str(cur)) != ch["new"]:
            skipped.append(f"{p['name']}: {FIELD_LABEL.get(ch['field'], ch['field'])} was changed again since, so it was left as it is"); continue
        old = ch["old"]
        if ch["field"] in INT_FIELDS:
            old = int(old) if old not in (None, "") else 0
        c.execute(f"UPDATE partners SET {ch['field']}=?, updated_at=? WHERE id=?", (old, now(), ch["partner_id"]))
        c.execute("UPDATE change_log SET undone=1 WHERE id=?", (cid,))
        c.execute("INSERT INTO activities(partner_id,ts,type,body,actor) VALUES(?,?,?,?,?)",
                  (ch["partner_id"], now(), "note", f"Undid a change: {FIELD_LABEL.get(ch['field'], ch['field'])} {ch['new'] if ch['new'] not in (None, '') else '(empty)'} → {ch['old'] if ch['old'] not in (None, '') else '(empty)'}", by or db.current_actor()))
        done += 1
    return {"undone": done, "skipped": skipped}


def prune_change_log(c, days=120):
    c.execute("DELETE FROM change_log WHERE ts<?", ((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"),))


# ---------------- reachability: who can actually be emailed ----------------
def _active_unblocked():
    return "COALESCE(archived,0)=0 AND do_not_contact=0"


def reach_summary(c):
    import mail
    total = valid = missing = invalid = 0
    by_stage = {}
    for r in c.execute(f"SELECT stage, email FROM partners WHERE {_active_unblocked()}"):
        total += 1
        e = (r["email"] or "").strip()
        if not e:
            missing += 1
            by_stage[r["stage"]] = by_stage.get(r["stage"], 0) + 1
        elif not mail.valid_email(e):
            invalid += 1
            by_stage[r["stage"]] = by_stage.get(r["stage"], 0) + 1
        else:
            valid += 1
    return {"total": total, "reachable": valid, "missing": missing, "invalid": invalid, "needs_work_by_stage": by_stage}


def reach_rows(c, show="needs", q="", segment="", stage="", limit=300):
    """Partners that can't be emailed yet (no address, or one that fails the format check), with whatever links help find one."""
    import mail
    w, a = [_active_unblocked()], []
    if segment:
        w.append("segment=?"); a.append(segment)
    if stage:
        w.append("stage=?"); a.append(stage)
    if q:
        w.append("(name LIKE ? OR contact_person LIKE ? OR city LIKE ?)"); a += [f"%{q}%"] * 3
    out = []
    for r in c.execute(f"SELECT id,name,category,segment,stage,city,country,contact_person,phone,website,linkedin,email,source FROM partners WHERE {' AND '.join(w)} ORDER BY (stage='New') DESC, name", a):
        e = (r["email"] or "").strip()
        kind = "missing" if not e else ("invalid" if not mail.valid_email(e) else "ok")
        if kind == "ok" or (show == "missing" and kind != "missing") or (show == "invalid" and kind != "invalid"):
            continue
        out.append({**dict(r), "kind": kind})
    return {"rows": out[:limit], "shown": min(len(out), limit), "matching": len(out)}


def set_emails(c, rows, by=""):
    """Save addresses for many partners at once (from the worklist or a pasted spreadsheet). Bad ones are reported, never half-saved."""
    import mail
    saved, errors, warnings = 0, [], []
    for item in list(rows)[:500]:
        try:
            pid = int(item.get("id"))
        except (TypeError, ValueError):
            errors.append({"id": item.get("id"), "why": "that isn't a partner number"}); continue
        e = (item.get("email") or "").strip().strip("<>").strip()
        p = c.execute("SELECT id,name,email,do_not_contact FROM partners WHERE id=?", (pid,)).fetchone()
        if not p:
            errors.append({"id": pid, "why": "no such partner"}); continue
        if not e:
            continue
        if not mail.valid_email(e):
            errors.append({"id": pid, "name": p["name"], "why": f"“{e[:40]}” is not a valid email address"}); continue
        other = c.execute("SELECT name FROM partners WHERE LOWER(TRIM(email))=LOWER(?) AND id<>? LIMIT 1", (e, pid)).fetchone()
        if other:
            warnings.append({"id": pid, "name": p["name"], "why": f"the same address is already on {other['name']}"})
        if (p["email"] or "").strip().lower() == e.lower():
            continue
        c.execute("UPDATE partners SET email=?, updated_at=? WHERE id=?", (e, now(), pid))
        c.execute("INSERT INTO activities(partner_id,ts,type,body,actor) VALUES(?,?,?,?,?)", (pid, now(), "note", f"Email address {'changed to' if (p['email'] or '').strip() else 'added:'} {e}", by or db.current_actor()))
        saved += 1
    return {"saved": saved, "errors": errors, "warnings": warnings}
