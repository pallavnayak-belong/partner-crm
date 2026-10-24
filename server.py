#!/usr/bin/env python3
"""Zero-dependency local server for the Partner CRM. Usage: python3 server.py [port]"""
import json
import mimetypes
import os
import re
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import db
import mail
import youform
import attribution
import notify
import onboarding
import attachments
import commercials
import crm
import autostart
import backup
import gdrive
import health
import snapshots
import vault
import alerts
import watch
import geo
import logging
import scoring

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
now = lambda: datetime.now().isoformat(timespec="seconds")
log = logging.getLogger("partner-crm")
LIVE = "COALESCE(partners.archived,0)=0"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
rows = lambda cur: [dict(r) for r in cur.fetchall()]

LAST_ACT = "(SELECT MAX(ts) FROM activities a WHERE a.partner_id=partners.id AND a.type NOT IN ('added','draft'))"
LOG_TYPES = {"note", "call", "meeting", "email", "whatsapp"}
CONTACT_TYPES = {"call", "meeting", "email", "whatsapp"}
PARTNER_EDITABLE = {"stage", "owner", "priority", "remarks", "contact_person", "designation", "email",
                    "phone", "segment", "last_contacted", "do_not_contact", "partner_code", "country"}


def universe_where(q):
    w, a = [], []
    g = lambda k: (q.get(k) or [""])[0]
    if g("kind"):
        w.append("u.kind=?"); a.append(g("kind"))
    if g("classification"):
        w.append("u.classification=?"); a.append(g("classification"))
    if g("city"):
        w.append("u.city=?"); a.append(g("city"))
    if g("category"):
        w.append("u.category=?"); a.append(g("category"))
    if g("top500") == "1":
        w.append("u.top_500=1")
    if g("has_email") == "1":
        w.append("COALESCE(u.email,'')<>''")
    if g("in_pipeline") == "0":
        w.append("p.id IS NULL")
    elif g("in_pipeline") == "1":
        w.append("p.id IS NOT NULL")
    if g("q"):
        w.append("(u.name LIKE ? OR u.key LIKE ? OR u.email LIKE ? OR u.city LIKE ?)")
        a += [f"%{g('q')}%"] * 4
    return (" WHERE " + " AND ".join(w)) if w else "", a


PARTNER_TYPES = ["Indian Mutual Fund Distributors", "SEBI - Investment Adviser", "SEBI - Research Analyst", "SEBI - Portfolio Manager", "Wealth manager / IFA", "Fintech / platform", "Community / influencer", "Other"]


def _digits(s):
    d = "".join(ch for ch in str(s or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else ""


def find_duplicates(c, name="", email="", phone=""):
    """Existing partners that look like the same firm: same email, same phone, or same normalised name."""
    norm = lambda s: " ".join("".join(ch if ch.isalnum() else " " for ch in str(s or "").lower()).split())
    nm, em, ph = norm(name), (email or "").strip().lower(), _digits(phone)
    out = {}
    for p in c.execute("SELECT id,name,email,phone,stage FROM partners"):
        why = []
        if em and (p["email"] or "").strip().lower() == em:
            why.append("same email")
        if ph and _digits(p["phone"]) == ph:
            why.append("same phone")
        if nm and norm(p["name"]) == nm:
            why.append("same name")
        if why:
            out[p["id"]] = {"id": p["id"], "name": p["name"], "stage": p["stage"], "why": ", ".join(why)}
    return list(out.values())


def create_partner(c, body):
    g = lambda k: str(body.get(k) or "").strip()
    name = g("name")
    if not name:
        raise ValueError("Name is required")
    if g("email") and not mail.valid_email(g("email")):
        raise ValueError("That email address doesn't look right")
    if g("segment") and g("segment") not in ("Individual", "Corporate", "Bank"):
        raise ValueError("Segment must be Individual, Corporate or Bank")
    dups = find_duplicates(c, name, g("email"), g("phone"))
    if dups and not body.get("force"):
        return {"duplicates": dups}
    t = now()
    uid = None
    if g("email"):  # exact registry email match links the registry record (rank, AUM, registration)
        r = c.execute("SELECT id FROM universe WHERE LOWER(email)=LOWER(?) AND id NOT IN (SELECT universe_id FROM partners WHERE universe_id IS NOT NULL) LIMIT 1", (g("email"),)).fetchone()
        uid = r[0] if r else None
    source = g("source") or "Added manually"
    cur = c.execute("""INSERT INTO partners(universe_id,name,category,segment,country,city,website,source,contact_person,designation,email,phone,linkedin,
                       owner,remarks,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uid, name, g("category") or "Other", g("segment") or "Individual", geo.norm_country(g("country")), g("city"), g("website"), source,
                     g("contact_person"), g("designation"), g("email"), g("phone"), g("linkedin"), g("owner"), g("remarks"),
                     g("stage") if g("stage") in db.STAGES else "New", t, t))
    pid = cur.lastrowid
    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, t, "added", f"Added manually ({source})" + (" · linked to registry record" if uid else "")))
    return {"id": pid, "linked_registry": bool(uid)}


def api(method, path, q, body):
    with db.connect() as c:
        if method == "GET" and path == "/api/universe":
            where, a = universe_where(q)
            base = "FROM universe u LEFT JOIN partners p ON p.universe_id=u.id" + where
            total = c.execute("SELECT COUNT(*) " + base, a).fetchone()[0]
            lim = min(int((q.get("limit") or [100])[0]), 500)
            off = int((q.get("offset") or [0])[0])
            order = "COALESCE(u.rank, 999999), u.name" if (q.get("sort") or [""])[0] == "rank" else "COALESCE(u.score, 0) DESC, COALESCE(u.rank, 999999), u.name"
            r = rows(c.execute(
                f"SELECT u.*, p.id AS partner_id, p.stage {base} ORDER BY {order} LIMIT ? OFFSET ?", a + [lim, off]))
            return {"total": total, "rows": r}
        if method == "GET" and path == "/api/facets":
            return {
                "kinds": [r[0] for r in c.execute("SELECT DISTINCT kind FROM universe ORDER BY 1")],
                "classifications": [r[0] for r in c.execute("SELECT DISTINCT classification FROM universe WHERE classification<>'' ORDER BY 1")],
                "categories": [r[0] for r in c.execute("SELECT DISTINCT category FROM universe ORDER BY 1")],
                "cities": [r[0] for r in c.execute("SELECT city FROM universe WHERE city<>'' GROUP BY city ORDER BY COUNT(*) DESC LIMIT 60")],
                "stages": db.STAGES,
            }
        if method == "POST" and path == "/api/partners":
            return create_partner(c, body)
        if method == "GET" and path == "/api/partners/types":
            return {"types": PARTNER_TYPES, "countries": geo.COUNTRIES}
        if method == "POST" and path == "/api/pipeline/add":
            ids = [int(i) for i in body.get("ids", [])]
            if body.get("filter"):  # add everything matching a filter
                where, a = universe_where({k: [v] for k, v in body["filter"].items()})
                ids = [r[0] for r in c.execute(
                    "SELECT u.id FROM universe u LEFT JOIN partners p ON p.universe_id=u.id" + where, a)]
            added = skipped = 0
            for uid in ids:
                u = c.execute("SELECT * FROM universe WHERE id=?", (uid,)).fetchone()
                if not u or c.execute("SELECT 1 FROM partners WHERE universe_id=?", (uid,)).fetchone():
                    skipped += 1
                    continue
                t = now()
                src = f"{'MFD' if u['kind'] == 'MFD' else 'SEBI'} universe ({u['key']})"
                cur = c.execute(
                    """INSERT INTO partners(universe_id,name,category,segment,country,city,website,source,contact_person,
                       email,phone,linkedin,other_social,stage,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'New',?,?)""",
                    (uid, u["name"], u["category"], u["classification"], "India", u["city"], u["website"], src,
                     u["contact_person"], u["email"], u["phone"], u["linkedin"], u["other_social"], t, t))
                c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)",
                          (cur.lastrowid, t, "added", f"Added from {u['kind']} universe"))
                added += 1
            return {"added": added, "skipped": skipped}
        if method == "GET" and path == "/api/partners":
            w, a = [], []
            g = lambda k: (q.get(k) or [""])[0]
            if g("stage"):
                w.append("stage=?"); a.append(g("stage"))
            if g("segment"):
                w.append("segment=?"); a.append(g("segment"))
            if g("region") in ("Indian", "Foreign"):
                w.append(geo.region_sql("partners") + "=?"); a.append(g("region"))
            if g("country"):
                w.append("LOWER(TRIM(partners.country))=LOWER(?)"); a.append(g("country"))
            if g("owner"):
                w.append("owner=?"); a.append(g("owner"))
            if g("tag"):
                w.append("EXISTS (SELECT 1 FROM partner_tags pt WHERE pt.partner_id=partners.id AND pt.tag_id=?)"); a.append(int(g("tag")))
            w.append("COALESCE(partners.archived,0)=" + ("1" if g("archived") == "1" else "0"))
            if g("email") == "missing":
                w.append("(COALESCE(TRIM(partners.email),'')='' OR partners.email NOT LIKE '%_@_%._%')")
            elif g("email") == "has":
                w.append("(COALESCE(TRIM(partners.email),'')<>'' AND partners.email LIKE '%_@_%._%')")
            if g("due") == "1":
                w.append("follow_up<>'' AND follow_up<=?"); a.append(date.today().isoformat())
            if g("q"):
                w.append("(name LIKE ? OR email LIKE ? OR contact_person LIKE ?)"); a += [f"%{g('q')}%"] * 3
            where = (" WHERE " + " AND ".join(w)) if w else ""
            total = c.execute("SELECT COUNT(*) FROM partners" + where, a).fetchone()[0]
            lim = min(int((q.get("limit") or [100])[0]), 5000)
            off = int((q.get("offset") or [0])[0])
            r = rows(c.execute(f"""SELECT partners.*, {LAST_ACT} AS last_activity, {geo.region_sql('partners')} AS region,
                (SELECT group_concat(t.name, '|') FROM partner_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.partner_id=partners.id) AS tags FROM partners{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?""", a + [lim, off]))
            countries = rows(c.execute(f"SELECT TRIM(country) AS country, COUNT(*) AS n FROM partners WHERE {LIVE} AND TRIM(COALESCE(country,''))<>'' GROUP BY 1 ORDER BY 2 DESC"))
            counts = {r_[0]: r_[1] for r_ in c.execute(f"SELECT stage, COUNT(*) FROM partners WHERE {LIVE} GROUP BY stage")}
            due = c.execute(f"SELECT COUNT(*) FROM partners WHERE {LIVE} AND follow_up<>'' AND follow_up<=?", (date.today().isoformat(),)).fetchone()[0]
            archived = c.execute("SELECT COUNT(*) FROM partners WHERE archived=1").fetchone()[0]
            res = {"total": total, "rows": r, "counts": counts, "due": due, "countries": countries, "archived": archived}
            if g("ids") == "1":      # every matching id, so "select all N" works across pages without loading every row
                res["ids"] = [x[0] for x in c.execute(f"SELECT partners.id FROM partners{where} ORDER BY updated_at DESC", a)]
            return res
        if method == "GET" and path == "/api/dashboard":
            today = date.today().isoformat()
            cnt = lambda sql, a=(): c.execute(sql, a).fetchone()[0]
            week = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
            return {
                "stages": {r[0]: r[1] for r in c.execute(f"SELECT stage, COUNT(*) FROM partners WHERE {LIVE} GROUP BY stage")},
                "segments": rows(c.execute("SELECT COALESCE(NULLIF(segment,''),'Unknown') AS k, COUNT(*) AS n FROM partners WHERE COALESCE(archived,0)=0 GROUP BY 1 ORDER BY 2 DESC")),
                "types": rows(c.execute("SELECT CASE WHEN category LIKE 'SEBI%' THEN category ELSE 'Mutual fund distributors' END AS k, COUNT(*) AS n FROM partners WHERE COALESCE(archived,0)=0 GROUP BY 1 ORDER BY 2 DESC")),
                "regions": rows(c.execute(f"SELECT {geo.region_sql()} AS k, COUNT(*) AS n FROM partners WHERE COALESCE(archived,0)=0 GROUP BY 1 ORDER BY 2 DESC")),
                "countries": rows(c.execute("SELECT TRIM(country) AS k, COUNT(*) AS n FROM partners WHERE COALESCE(archived,0)=0 AND LOWER(TRIM(COALESCE(country,''))) NOT IN ('','india') GROUP BY 1 ORDER BY 2 DESC LIMIT 6")),
                "universe": cnt("SELECT COUNT(*) FROM universe"),
                "in_pipeline": cnt("SELECT COUNT(*) FROM partners WHERE COALESCE(archived,0)=0"),
                "overdue": cnt("SELECT COUNT(*) FROM tasks t JOIN partners p ON p.id=t.partner_id WHERE t.done_at IS NULL AND COALESCE(p.archived,0)=0 AND t.due<?", (today,)),
                "due_today": cnt("SELECT COUNT(*) FROM tasks t JOIN partners p ON p.id=t.partner_id WHERE t.done_at IS NULL AND COALESCE(p.archived,0)=0 AND t.due=?", (today,)),
                "apps_pending": cnt("SELECT COUNT(*) FROM applications WHERE status='review'"),
                "reach": crm.reach_summary(c),
                "added_7d": cnt("SELECT COUNT(*) FROM partners WHERE created_at>=?", (week,)),
                "touches_7d": cnt("SELECT COUNT(*) FROM activities WHERE type IN ('call','meeting','email','whatsapp') AND ts>=?", (week,)),
                "tasks": crm.open_tasks(c)[:8],
                "recent": rows(c.execute("""SELECT a.id,a.ts,a.type,a.body,a.partner_id,p.name FROM activities a JOIN partners p ON p.id=a.partner_id
                    WHERE a.type NOT IN ('added','draft') ORDER BY a.ts DESC, a.id DESC LIMIT 12""")),
            }
        if method == "GET" and path == "/api/tasks":
            return {"rows": crm.open_tasks(c), "today": date.today().isoformat(), "team": crm.team_list(c)}
        if path.startswith("/api/tasks/") and method in ("PATCH", "DELETE"):
            tid = int(path.rsplit("/", 1)[1])
            return crm.delete_task(c, tid) if method == "DELETE" else crm.update_task(c, tid, body)
        if method == "GET" and path == "/api/tags":
            return {"rows": crm.tags_list(c)}
        if method == "POST" and path == "/api/tags/bulk":
            return crm.bulk_tag(c, [int(i) for i in body.get("ids", [])], body.get("name"), body.get("action", "add"))
        if method == "DELETE" and path.startswith("/api/tags/"):
            return crm.delete_tag(c, int(path.rsplit("/", 1)[1]))
        if path == "/api/views":
            if method == "POST":
                return crm.save_view(c, body.get("name"), body.get("page", "pipeline"), body.get("filters"))
            return {"rows": crm.views_list(c, (q.get("page") or ["pipeline"])[0])}
        if method == "DELETE" and path.startswith("/api/views/"):
            return crm.delete_view(c, int(path.rsplit("/", 1)[1]))
        if path == "/api/team":
            if method == "POST":
                return crm.team_add(c, body.get("name"))
            return {"rows": crm.team_list(c)}
        if method == "DELETE" and path.startswith("/api/team/"):
            return crm.team_remove(c, int(path.rsplit("/", 1)[1]))
        if path.startswith("/api/contacts/"):
            parts_ = path.strip("/").split("/")
            cid = int(parts_[2])
            if method == "DELETE":
                return crm.delete_contact(c, cid)
            if method == "PATCH":
                return crm.update_contact(c, cid, body)
            if method == "POST" and parts_[3:] == ["primary"]:
                return crm.make_primary(c, cid)
        if path.startswith("/api/activities/") and method in ("PATCH", "DELETE"):
            aid = int(path.rsplit("/", 1)[1])
            return crm.delete_activity(c, aid) if method == "DELETE" else crm.edit_activity(c, aid, body.get("body"))
        if path.startswith("/api/gdrive"):
            return gdrive_api(c, method, path, body)
        if path.startswith("/api/backup"):
            return backup_api(c, method, path, body)
        if path == "/api/reach" and method == "GET":
            g2 = lambda k: (q.get(k) or [""])[0]
            return {**crm.reach_summary(c), **crm.reach_rows(c, g2("show") or "needs", g2("q"), g2("segment"), g2("stage"), int(g2("limit") or 300))}
        if path == "/api/reach/emails" and method == "POST":
            return crm.set_emails(c, body.get("rows") or [], ACTOR.get())
        if path == "/api/sends" and method == "GET":
            return {"rows": rows(c.execute("SELECT id,started,finished,kind,by,template,total,sent,skipped,failed,state,stopped_reason FROM send_jobs ORDER BY started DESC LIMIT ?", (int((q.get("limit") or [60])[0]),)))}
        if path.startswith("/api/sends/") and method == "GET":
            r_ = c.execute("SELECT * FROM send_jobs WHERE id=?", (path.rsplit("/", 1)[1],)).fetchone()
            if not r_:
                raise LookupError("send")
            d_ = dict(r_)
            d_["details"] = json.loads(d_["details"] or "{}")
            return d_
        if path == "/api/changes" and method == "GET":
            return {"rows": crm.recent_changes(c, (q.get("partner_id") or [None])[0], int((q.get("limit") or [80])[0]), int(q["hours"][0]) if q.get("hours") else None)}
        if path == "/api/changes/undo" and method == "POST":
            return crm.undo_changes(c, body.get("ids") or [], ACTOR.get())
        if path == "/api/notify/config":
            if method == "POST":
                notify.save_cfg(body.get("webhook_url", ""))
            return {"configured": bool(notify.load_cfg()["webhook_url"])}
        if method == "POST" and path == "/api/notify/test":
            if not notify.send("\u2705 Partner CRM: Slack alerts are connected.", wait=True):
                raise ValueError("Slack didn't accept the test message \u2014 check the webhook URL")
            return {"ok": True}
        if path.startswith("/api/attribution/"):
            if path == "/api/attribution/config":
                if method == "POST":
                    attribution.save_cfg({k: body[k] for k in ("onelink_base", "media_source") if k in body})
                cfg = attribution.load_cfg()
                return {k: cfg[k] for k in ("onelink_base", "media_source", "last_sync", "last_sync_result")}
            if method == "POST" and path == "/api/attribution/sync":
                return dict(attribution.start_sync())
            if method == "GET" and path == "/api/attribution/sync":
                return dict(attribution.JOB)
            if method == "GET" and path == "/api/attribution/summary":
                st = attribution.stats_for(c)
                parts = {r["id"]: r for r in c.execute("SELECT id,name,stage,segment,country,partner_code FROM partners WHERE id IN (%s)" % (",".join(map(str, st)) or "0"))}
                prows = [{"id": pid, "name": parts[pid]["name"], "stage": parts[pid]["stage"], "segment": parts[pid]["segment"], "country": geo.norm_country(parts[pid]["country"]), "region": geo.region_of(parts[pid]["country"]), "code": attribution.code_for(parts[pid]), **v} for pid, v in st.items() if pid in parts]
                prows.sort(key=lambda r: (-r["funded"], -r["activated"], -r["installed"]))
                totals = {k: sum(r[k] for r in prows) for k, _ in attribution.STEPS}
                linked = c.execute("SELECT COUNT(*) FROM partners WHERE stage IN ('Applied','In discussion','Onboarded')").fetchone()[0]
                cfg = attribution.load_cfg()
                return {"steps": [{"key": k, "label": l} for k, l in attribution.STEPS], "rows": prows, "totals": totals, "partners_with_clients": len(prows),
                        "partners_applied_plus": linked, "configured": bool(cfg["onelink_base"]), "last_sync": cfg["last_sync"], "last_sync_result": cfg["last_sync_result"]}
            raise LookupError(path)
        if path.startswith("/api/applications") or path.startswith("/api/youform/"):
            return youform_api(c, method, path, q, body)
        if method == "GET" and path == "/api/sequence":
            return {"steps": mail.due_followups(c)}
        if method == "POST" and path == "/api/sequence":
            steps = body.get("steps") or []
            if len(steps) > 6:
                raise ValueError("max 6 steps")
            c.execute("DELETE FROM sequence_steps")
            for i, st in enumerate(steps, 1):
                if not c.execute("SELECT 1 FROM templates WHERE id=?", (int(st["template_id"]),)).fetchone():
                    raise ValueError("unknown template")
                c.execute("INSERT INTO sequence_steps(step_no,delay_days,template_id) VALUES(?,?,?)", (i, max(1, int(st["delay_days"])), int(st["template_id"])))
            return {"ok": True}
        if method == "GET" and path == "/api/analytics":
            return analytics(c)
        if path.startswith("/api/mail/") or path.startswith("/api/templates"):
            return mail_api(c, method, path, body)
        if path.startswith("/api/onboarding"):
            return onboarding_api(c, method, path, q, body)
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[1] == "partners":
            pid = int(parts[2])
            if method == "GET" and len(parts) == 3:
                p = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
                if not p:
                    return None
                u = c.execute("SELECT * FROM universe WHERE id=?", (p["universe_id"],)).fetchone() if p["universe_id"] else None
                cfg = attribution.load_cfg()
                st = attribution.stats_for(c, pid).get(pid, {"installed": 0, **{k: 0 for k, *_ in attribution.MILESTONES}})
                referral = {"distributor_code": p["distributor_code"] or "", "code": attribution.code_for(p), "custom_code": bool(p["partner_code"]), "link": attribution.link_for(p, cfg), "configured": bool(cfg["onelink_base"]), "path_style": attribution.is_path_style(cfg["onelink_base"]), "stats": st}
                ob = c.execute("SELECT status, appsflyer_link FROM onboarding WHERE partner_id=?", (pid,)).fetchone()
                if ob and (ob["appsflyer_link"] or "").strip():
                    referral.update(link=ob["appsflyer_link"].strip(), configured=True)
                cm = commercials.get(c, pid) if ob else None
                onb = {"status": ob["status"], **onboarding.progress(c, pid), "commercials": cm["status"], "terms": commercials.describe(cm)} if ob else None
                return {"referral": referral, "onboarding": onb, "region": geo.region_of(p["country"]), "countries": geo.COUNTRIES,
                        "contacts": crm.contacts_of(c, pid), "tags": crm.partner_tags(c, pid), "tasks": crm.partner_tasks(c, pid), "team": crm.team_list(c),
                        "lost_reasons": crm.LOST_REASONS, "recipients": crm.recipients(c, pid), "partner": dict(p), "universe": dict(u) if u else None, "activities": rows(c.execute(
                    "SELECT * FROM activities WHERE partner_id=? ORDER BY ts DESC, id DESC", (pid,)))}
            if method == "PATCH" and len(parts) == 3:
                old = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
                if not old:
                    return None
                ch = {k: v for k, v in body.items() if k in PARTNER_EDITABLE}
                if "stage" in ch and ch["stage"] not in db.STAGES:
                    raise ValueError("bad stage")
                lost = ""
                if ch.get("stage") == "Not interested" and old["stage"] != "Not interested":
                    lost = crm.check_lost_reason(str(body.get("lost_reason") or "").strip())
                    ch["lost_reason"] = lost
                elif "stage" in ch and ch["stage"] != "Not interested" and old["stage"] == "Not interested":
                    ch["lost_reason"] = None
                if ch.get("stage") == "Onboarded":
                    ob = c.execute("SELECT status FROM onboarding WHERE partner_id=?", (pid,)).fetchone()
                    if ob and ob["status"] != "Onboarded":
                        raise ValueError("Finish the onboarding checklist first (Onboarding page → Mark onboarded)")
                if "partner_code" in ch:
                    ch["partner_code"] = (ch["partner_code"] or "").strip()
                    if ch["partner_code"] and not attribution.SAFE.match(ch["partner_code"]):
                        raise ValueError("partner code: letters, numbers, space . _ - only")
                    if ch["partner_code"] and c.execute("SELECT 1 FROM partners WHERE id<>? AND LOWER(partner_code)=LOWER(?)", (pid, ch["partner_code"])).fetchone():
                        raise ValueError("that code is already used by another partner")
                if "country" in ch:
                    ch["country"] = geo.norm_country(ch["country"])
                    if ch["country"] != geo.norm_country(old["country"]):
                        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)",
                                  (pid, now(), "note", f"Country: {geo.norm_country(old['country'])} → {ch['country']} ({geo.region_of(ch['country'])})"))
                if "do_not_contact" in ch:
                    ch["do_not_contact"] = 1 if ch["do_not_contact"] in (1, "1", True, "true") else 0
                    if ch["do_not_contact"] != old["do_not_contact"]:
                        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)",
                                  (pid, now(), "note", "Marked do-not-contact" if ch["do_not_contact"] else "Do-not-contact removed"))
                        c.execute("UPDATE partners SET dnc_reason=? WHERE id=?", ("manual" if ch["do_not_contact"] else "", pid))
                for k, v in ch.items():
                    if old[k] != v and k == "stage":
                        note = str(body.get("lost_note") or "").strip()
                        extra = f" ({lost}{' — ' + note if note else ''})" if v == "Not interested" and (lost or note) else ""
                        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)",
                                  (pid, now(), "stage", f"{old['stage']} → {v}{extra}"))
                if ch:
                    sets = ", ".join(f"{k}=?" for k in ch)
                    c.execute(f"UPDATE partners SET {sets}, updated_at=? WHERE id=?", [*ch.values(), now(), pid])
                    if "country" in ch:
                        onboarding.refresh_region(c, pid)
                return {"ok": True}
            if len(parts) == 4 and parts[3] == "tasks" and method == "POST":
                return crm.add_task(c, pid, body.get("title"), body.get("due"), body.get("assignee", ""), ACTOR.get())
            if len(parts) == 4 and parts[3] == "contacts" and method == "POST":
                return crm.add_contact(c, pid, body)
            if len(parts) == 4 and parts[3] == "tags" and method == "POST":
                return crm.add_tag(c, pid, body.get("name"))
            if len(parts) == 5 and parts[3] == "tags" and method == "DELETE":
                return crm.remove_tag(c, pid, int(parts[4]))
            if len(parts) == 4 and parts[3] == "archive" and method == "POST":
                return crm.set_archived(c, pid, bool(body.get("archived", True)), body.get("reason", ""))
            if len(parts) == 4 and parts[3] == "merge" and method == "POST":
                return crm.merge(c, int(body["into"]), pid, ACTOR.get())
            if len(parts) == 4 and parts[3] == "export" and method == "GET":
                return crm.export_partner(c, pid)
            if len(parts) == 3 and method == "DELETE":
                return crm.delete_partner(c, pid, body.get("confirm"), body.get("reason", ""), ACTOR.get())
            if method == "POST" and len(parts) == 4 and parts[3] in ("notes", "activities"):
                text = (body.get("body") or "").strip()
                typ = body.get("type", "note") if parts[3] == "activities" else "note"
                if not text:
                    raise ValueError("empty entry")
                if typ not in LOG_TYPES:
                    raise ValueError("bad type")
                c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, now(), typ, text))
                if typ in CONTACT_TYPES:
                    c.execute("UPDATE partners SET last_contacted=?, updated_at=? WHERE id=?", (date.today().isoformat(), now(), pid))
                else:
                    c.execute("UPDATE partners SET updated_at=? WHERE id=?", (now(), pid))
                return {"ok": True}
    raise LookupError(path)


def onboarding_api(c, method, path, q, body):
    parts = path.strip("/").split("/")[2:]
    if not parts and method == "GET":
        return onboarding.list_all(c)
    if parts == ["candidates"] and method == "GET":
        return {"rows": onboarding.candidates(c, (q.get("q") or [""])[0])}
    if parts == ["standard"] and method == "GET":
        return {**commercials.get(c, 0), "options": commercials.options()}
    if parts == ["standard"] and method == "PUT":
        commercials.save(c, 0, body)
        c.execute("UPDATE commercials SET status='Draft' WHERE partner_id=0")
        return {"ok": True}
    if parts == ["partners-with-commercials"] and method == "GET":
        return {"rows": rows(c.execute("SELECT p.id,p.name FROM commercials m JOIN partners p ON p.id=m.partner_id WHERE m.partner_id>0 AND COALESCE(m.terms_text,'')<>'' ORDER BY p.name"))}
    if parts == ["start"] and method == "POST":
        return onboarding.start(c, int(body.get("partner_id")))
    if len(parts) == 2 and parts[0] == "files" and method == "DELETE":
        return onboarding.delete_file(c, int(parts[1]))
    if not parts or not parts[0].isdigit():
        raise LookupError(path)
    pid = int(parts[0])
    rest = parts[1:]
    if not rest and method == "GET":
        return onboarding.detail(c, pid)
    if not rest and method == "PATCH":
        return onboarding.patch(c, pid, body)
    if rest == ["items"] and method == "POST":
        return onboarding.add_item(c, pid, body)
    if len(rest) == 2 and rest[0] == "items" and method == "PATCH":
        return onboarding.patch_item(c, pid, rest[1], body)
    if len(rest) == 2 and rest[0] == "items" and method == "DELETE":
        return onboarding.delete_item(c, pid, rest[1])
    if rest == ["commercials"] and method == "PUT":
        return onboarding.save_commercials(c, pid, body)
    if rest == ["commercials", "status"] and method == "POST":
        return onboarding.commercials_status(c, pid, body.get("status"), str(body.get("by") or ""))
    if rest == ["commercials", "copy"] and method == "POST":
        return onboarding.commercials_copy(c, pid, body.get("from"))
    if rest == ["complete"] and method == "POST":
        return onboarding.complete(c, pid)
    if rest == ["reopen"] and method == "POST":
        return onboarding.reopen(c, pid, (body.get("reason") or "").strip())
    raise LookupError(path)


def youform_api(c, method, path, q, body):
    if method == "GET" and path == "/api/applications":
        st = (q.get("status") or [""])[0]
        sql = "SELECT a.*, p.name AS partner_name FROM applications a LEFT JOIN partners p ON p.id=a.partner_id" + (" WHERE a.status=?" if st else "") + " ORDER BY a.received_at DESC LIMIT 500"
        out = rows(c.execute(sql, (st,) if st else ()))
        for r in out:
            r.pop("raw", None)
            cands = json.loads(r.get("candidates") or "[]")
            r["candidates"] = []
            for pid, conf, why in cands:
                p = c.execute("SELECT id,name,email,stage,city FROM partners WHERE id=?", (pid,)).fetchone()
                if p:
                    r["candidates"].append({**dict(p), "confidence": conf, "why": why})
        return {"rows": out}
    if method == "POST" and path == "/api/applications/import-csv":
        apps = youform.from_csv(body.get("csv", ""))
        if not apps:
            raise ValueError("No applications found in that CSV \u2014 check it's the Youform responses export")
        res = youform.ingest_many(c, apps)
        res["rows"] = len(apps)
        return res
    parts = path.strip("/").split("/")
    if method == "POST" and len(parts) == 4 and parts[1] == "applications":
        aid, act = int(parts[2]), parts[3]
        a = c.execute("SELECT * FROM applications WHERE id=?", (aid,)).fetchone()
        if not a:
            return None
        if act == "link":
            pid = int(body["partner_id"])
            if not c.execute("SELECT 1 FROM partners WHERE id=?", (pid,)).fetchone():
                raise ValueError("unknown partner")
            if a["status"] == "linked" and a["partner_id"] == pid:
                return {"ok": True}
            youform.apply_link(c, aid, pid, "manual", "manual")
            return {"ok": True, "partner_id": pid}
        if act == "create-partner":
            if a["status"] in ("linked", "new_partner"):
                raise ValueError("already linked")
            return {"ok": True, "partner_id": youform.create_partner(c, aid)}
        if act == "ignore":
            c.execute("UPDATE applications SET status='ignored' WHERE id=?", (aid,))
            return {"ok": True}
    if path == "/api/youform/config":
        if method == "POST":
            youform.save_cfg(body)
        return youform.public_cfg()
    if method == "POST" and path == "/api/youform/pull":
        return youform.pull_relay()
    raise LookupError(path)


def analytics(c):
    cnt = lambda sql, a=(): c.execute(sql, a).fetchone()[0]
    order = db.STAGES
    stages = {r[0]: r[1] for r in c.execute("SELECT stage, COUNT(*) FROM partners GROUP BY stage")}
    pipeline = sum(stages.values())
    # "reached" = currently at that stage or further along the happy path
    path = ["New", "Contacted", "Replied", "Applied", "In discussion", "Onboarded"]
    reached = [{"stage": s_, "n": sum(stages.get(x, 0) for x in path[i:])} for i, s_ in enumerate(path)]
    reached[0]["n"] = pipeline  # everyone who entered the pipeline, including those later marked Not interested
    emailed = cnt("SELECT COUNT(DISTINCT partner_id) FROM emails WHERE status='sent'")
    real_reply = "type='reply' AND body NOT LIKE '(auto-reply)%'"
    replied = cnt(f"SELECT COUNT(DISTINCT partner_id) FROM activities WHERE {real_reply}")
    bounced = cnt("SELECT COUNT(DISTINCT partner_id) FROM activities WHERE type='bounce'")
    unsub = cnt("SELECT COUNT(*) FROM partners WHERE dnc_reason='unsubscribed'")
    dnc = cnt("SELECT COUNT(*) FROM partners WHERE do_not_contact=1")
    by_template = rows(c.execute(f"""
        SELECT t.name AS name, COUNT(DISTINCT e.partner_id) AS sent,
          COUNT(DISTINCT CASE WHEN EXISTS(SELECT 1 FROM activities a WHERE a.partner_id=e.partner_id AND a.{real_reply.replace(' AND ', ' AND a.')} AND a.ts>=e.sent_at) THEN e.partner_id END) AS replied,
          COUNT(DISTINCT CASE WHEN EXISTS(SELECT 1 FROM activities a WHERE a.partner_id=e.partner_id AND a.type='bounce' AND a.ts>=e.sent_at) THEN e.partner_id END) AS bounced
        FROM emails e JOIN templates t ON t.id=e.template_id WHERE e.status='sent' GROUP BY t.id ORDER BY sent DESC"""))
    by_segment = rows(c.execute(f"""
        SELECT COALESCE(NULLIF(p.segment,''),'Unknown') AS name, COUNT(DISTINCT e.partner_id) AS sent,
          COUNT(DISTINCT CASE WHEN EXISTS(SELECT 1 FROM activities a WHERE a.partner_id=e.partner_id AND a.{real_reply.replace(' AND ', ' AND a.')}) THEN e.partner_id END) AS replied
        FROM emails e JOIN partners p ON p.id=e.partner_id WHERE e.status='sent' GROUP BY 1 ORDER BY sent DESC"""))
    by_region = rows(c.execute(f"""
        SELECT {geo.region_sql('p')} AS name, COUNT(DISTINCT e.partner_id) AS sent,
          COUNT(DISTINCT CASE WHEN EXISTS(SELECT 1 FROM activities a WHERE a.partner_id=e.partner_id AND a.{real_reply.replace(' AND ', ' AND a.')}) THEN e.partner_id END) AS replied
        FROM emails e JOIN partners p ON p.id=e.partner_id WHERE e.status='sent' GROUP BY 1 ORDER BY sent DESC"""))
    by_step = rows(c.execute("SELECT step, COUNT(*) AS sent FROM emails WHERE status='sent' GROUP BY step ORDER BY step"))
    since = (datetime.now() - timedelta(days=13)).date().isoformat()
    days = [(datetime.now() - timedelta(days=i)).date().isoformat() for i in range(13, -1, -1)]
    sent_d = {r[0]: r[1] for r in c.execute("SELECT substr(sent_at,1,10), COUNT(*) FROM emails WHERE status='sent' AND sent_at>=? GROUP BY 1", (since,))}
    rep_d = {r[0]: r[1] for r in c.execute(f"SELECT substr(ts,1,10), COUNT(*) FROM activities WHERE {real_reply} AND ts>=? GROUP BY 1", (since,))}
    today = date.today().isoformat()
    attention = {
        "replied_waiting": rows(c.execute("""SELECT p.id, p.name, p.stage, (SELECT MAX(ts) FROM activities WHERE partner_id=p.id AND type='reply') AS replied_at
            FROM partners p WHERE p.stage='Replied' AND NOT EXISTS (SELECT 1 FROM activities a WHERE a.partner_id=p.id AND a.type IN ('call','meeting','whatsapp','note','email')
              AND a.ts > COALESCE((SELECT MAX(ts) FROM activities WHERE partner_id=p.id AND type='reply'),'')) ORDER BY replied_at LIMIT 10""")),
        "overdue": rows(c.execute("SELECT id, name, stage, follow_up FROM partners WHERE follow_up<>'' AND follow_up<? ORDER BY follow_up LIMIT 10", (today,))),
        "stale_discussion": rows(c.execute("""SELECT p.id, p.name, p.stage, (SELECT MAX(ts) FROM activities WHERE partner_id=p.id AND type NOT IN ('added','draft')) AS last_activity
            FROM partners p WHERE p.stage='In discussion' AND p.follow_up='' ORDER BY last_activity LIMIT 10""")),
    }
    lost_reasons = rows(c.execute("SELECT COALESCE(NULLIF(lost_reason,''),'Not recorded') AS name, COUNT(*) AS n FROM partners WHERE stage='Not interested' AND COALESCE(archived,0)=0 GROUP BY 1 ORDER BY 2 DESC"))
    apps_total = cnt("SELECT COUNT(*) FROM applications")
    apps_pending = cnt("SELECT COUNT(*) FROM applications WHERE status='review'")
    return {"applications": apps_total, "applications_pending": apps_pending, "pipeline": pipeline, "emailed": emailed, "replied": replied, "bounced": bounced, "unsubscribed": unsub, "dnc": dnc,
            "reached": reached, "not_interested": stages.get("Not interested", 0), "by_template": by_template, "by_segment": by_segment, "by_region": by_region, "lost_reasons": lost_reasons,
            "by_step": by_step, "trend": [{"d": d, "sent": sent_d.get(d, 0), "replies": rep_d.get(d, 0)} for d in days], "attention": attention}


def _age_hours(ts):
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def backup_health(c):
    return health.layers(c)


def backup_api(c, method, path, body):
    by = ACTOR.get() if hasattr(ACTOR, "get") else ""
    if path == "/api/backup/run" and method == "POST":
        return backup.run("manual")
    if path == "/api/backup/config" and method == "POST":
        backup.save_cfg(body)
    if path == "/api/backup/status":
        return health.overall(c, autostart.installed())
    if path == "/api/backup/snapshots":
        return {"rows": snapshots.listing()}
    if method == "POST" and path == "/api/backup/preview":
        return snapshots.preview(body.get("name", ""))
    if method == "POST" and path == "/api/backup/compare":
        return snapshots.compare(body.get("a", ""), body.get("b", "live"))
    if method == "POST" and path == "/api/backup/restore":
        if body.get("confirm") != "RESTORE":
            raise ValueError("Type RESTORE to confirm")
        return snapshots.restore_all(body.get("name", ""), by)
    if method == "POST" and path == "/api/backup/partners":
        return {"rows": snapshots.partners_in(body.get("name", ""), body.get("q", ""))}
    if method == "POST" and path == "/api/backup/restore-partner":
        return snapshots.restore_partner(body.get("name", ""), body.get("partner_id"), bool(body.get("replace")), bool(body.get("allow_erased")), by)
    if path == "/api/backup/vault":
        if method == "POST":
            if body.get("action") == "remove":
                vault.remove()
            elif body.get("action") == "check":
                try:
                    vault.check(body.get("passphrase", ""))
                except ValueError as e:
                    raise ValueError(str(e))
                return {"ok": True}
            else:
                vault.setup(body.get("passphrase", ""))
                p = vault.write_local(backup.load_cfg()["folder"])
                if gdrive.configured():
                    try:
                        gdrive.upload_settings()
                    except Exception:  # noqa
                        pass
        return {"configured": vault.configured()}
    cfg_ = backup.load_cfg()
    return {**cfg_, "snapshots": backup.listing()[:15], "default_folder": backup.default_folder()}


def gdrive_api(c, method, path, body):
    if path == "/api/gdrive/script" and method == "GET":
        return {"script": gdrive.script_text()}
    if path == "/api/gdrive/config" and method == "POST":
        gdrive.save_cfg({k: v for k, v in body.items() if k in ("url", "enabled", "include_documents", "include_code", "sheet_sync_hours", "keep_days")})
    if path == "/api/gdrive/test" and method == "POST":
        try:
            r = gdrive.test()
        except gdrive.RelayError as e:
            raise ValueError(str(e))
        return {"ok": True, **r}
    if path == "/api/gdrive/run" and method == "POST":
        parts = [p for p in body.get("parts", ["drive", "sheets"]) if p in ("drive", "sheets", "drill")] or ["drive", "sheets"]
        if not gdrive.configured():
            raise ValueError("Set up the Google connection first")
        return gdrive.start_job(tuple(parts))
    if path == "/api/gdrive/verify" and method == "POST":
        if not gdrive.configured():
            raise ValueError("Set up the Google connection first")
        try:
            r = gdrive.verify_drive(c, gdrive.load_cfg()["include_documents"])
        except gdrive.RelayError as e:
            raise ValueError(str(e))
        gdrive.save_cfg({"last_verify": datetime.now().isoformat(timespec="seconds"), "last_verify_result": "OK" if r["ok"] else "PROBLEMS: " + "; ".join(r["problems"][:3])})
        return r
    if path == "/api/gdrive/job" and method == "GET":
        return gdrive.JOB
    cfg = gdrive.load_cfg()
    return {"configured": gdrive.configured(cfg), "enabled": cfg["enabled"], "url": cfg["url"], "has_token": bool(cfg["token"]), "include_documents": cfg["include_documents"],
            "include_code": cfg["include_code"], "sheet_sync_hours": cfg["sheet_sync_hours"], "keep_days": cfg["keep_days"], "root_url": cfg["root_url"], "sheet_url": cfg["sheet_url"],
            "health": backup_health(c), "local": backup.load_cfg(), "snapshots": backup.listing()[:6], "default_folder": backup.default_folder(),
            "runs": rows(c.execute("SELECT ts,kind,ok,detail FROM backup_runs ORDER BY id DESC LIMIT 20")), "job": gdrive.JOB,
            "autostart": autostart.installed(), "last_verify": cfg["last_verify"], "last_verify_result": cfg["last_verify_result"],
            "last_drill": cfg["last_drill"], "last_drill_result": cfg["last_drill_result"], "vault": vault.configured(), "alert_email": backup.load_cfg().get("alert_email", ""), "alert_default": mail.load_cfg().get("address", "")}


def mail_api(c, method, path, body):
    P = lambda pid: c.execute("SELECT * FROM partners WHERE id=?", (int(pid),)).fetchone()
    if path == "/api/mail/config":
        if method == "POST":
            mail.save_cfg(body)
        return mail.public_cfg(c)
    if method == "POST" and path == "/api/mail/test-connection":
        return mail.test_connection()
    if method == "POST" and path == "/api/mail/preview":
        p = P(body["partner_id"])
        r = mail.render(c, p, body.get("template_id"))
        r["to"] = p["email"]
        fb = sum(f[2] for f in r.get("_files", []))
        r["est_seconds"] = mail.upload_seconds(fb)
        r["size_mb"] = round((len(r["html"]) + int(fb * 1.34)) / 1048576, 1)
        r.pop("_files", None)
        return r
    if method == "POST" and path == "/api/mail/send-test":
        to = (body.get("to") or mail.load_cfg()["address"]).strip()
        if not mail.valid_email(to):
            raise ValueError("bad recipient")
        return mail.start_test_job(to, body.get("partner_id"), body.get("template_id"))
    if method == "POST" and path == "/api/mail/plan":
        cfg = mail.public_cfg(c)
        all_ids = [int(i) for i in body["partner_ids"]]
        excluded = {int(i) for i in (body.get("exclude") or [])}
        ids = [i for i in all_ids if i not in excluded]
        sendable = noemail = dup = dnc = 0
        est_total, cfg_delay = 0, mail.load_cfg()["delay"]
        per, tids = {}, set()
        step = int(body.get("step") or 0)
        for pid in ids:
            p = P(pid)
            if not p:
                continue
            if p["do_not_contact"]:
                dnc += 1
                continue
            if not mail.valid_email(p["email"]):
                noemail += 1
                continue
            t = mail.pick_template(c, p, body.get("template_id"))
            if not body.get("force") and mail.already_sent(c, pid, t["id"], step):
                dup += 1
                continue
            sendable += 1
            tids.add(t["id"])
            est_total += mail.upload_seconds(sum(f[2] for f in attachments.files_for(c, t["id"]))) + cfg_delay
            per[t["name"]] = per.get(t["name"], 0) + 1
        remaining = max(cfg["daily_cap"] - cfg["sent_today"], 0) if cfg["daily_cap"] else None
        out = {"total": len(all_ids), "sendable": sendable, "no_email": noemail, "already_sent": dup, "dnc": dnc, "templates": per, "excluded": len(excluded),
               "cap_remaining": remaining, "configured": cfg["configured"], "from": cfg["address"], "est_seconds": est_total, "sizes": mail.size_info(c, tids)}
        if body.get("review"):
            out["review"] = mail.review(c, all_ids, body.get("template_id"), step, bool(body.get("force")), excluded)
        return out
    if path == "/api/mail/cc" and method == "GET":
        return {"rows": mail.cc_contacts(c)}
    if path == "/api/mail/cc" and method == "POST":
        email_, label = (body.get("email") or "").strip(), (body.get("label") or "").strip()[:60]
        if not mail.valid_email(email_):
            raise ValueError("enter a valid email address")
        if not label:
            raise ValueError("give this contact a name")
        if c.execute("SELECT 1 FROM cc_contacts WHERE email=?", (email_,)).fetchone():
            raise ValueError("that email is already saved")
        c.execute("INSERT INTO cc_contacts(label,email,auto) VALUES(?,?,?)", (label, email_, 1 if body.get("auto") else 0))
        return {"rows": mail.cc_contacts(c)}
    if path.startswith("/api/mail/cc/") and method in ("PATCH", "DELETE"):
        cid = int(path.rsplit("/", 1)[1])
        if method == "DELETE":
            c.execute("DELETE FROM cc_contacts WHERE id=?", (cid,))
        else:
            if "auto" in body:
                c.execute("UPDATE cc_contacts SET auto=? WHERE id=?", (1 if body["auto"] else 0, cid))
            if (body.get("label") or "").strip():
                c.execute("UPDATE cc_contacts SET label=? WHERE id=?", (body["label"].strip()[:60], cid))
        return {"rows": mail.cc_contacts(c)}
    if method == "POST" and path == "/api/mail/send":
        ids = [int(i) for i in body["partner_ids"]]
        if not ids or len(ids) > 500:
            raise ValueError("1–500 partners per send")
        if len(ids) > 1 and backup.latest_age_hours() > 1:
            try:
                backup.snapshot_before("bulk-send")      # cheap safety copy so a bulk send's changes can always be compared and undone
            except Exception:  # noqa
                pass
        to = None
        if body.get("to"):
            if len(ids) != 1:
                raise ValueError("A specific recipient can only be chosen when emailing one partner")
            ok = {r["email"].lower(): r for r in crm.recipients(c, ids[0])}
            r_ = ok.get(str(body["to"]).strip().lower())
            if not r_:
                raise ValueError("That address isn't one of this partner's contacts")
            if r_["bounced"]:
                raise ValueError("That address bounced earlier; update the contact first")
            to = r_["email"]
        return mail.start_send_job(ids, body.get("template_id"), bool(body.get("force")), int(body.get("step") or 0), mail.resolve_cc(c, body.get("cc"), to or ""), to)
    if method == "GET" and path.startswith("/api/mail/jobs/"):
        j = mail.JOBS.get(path.rsplit("/", 1)[1])
        if not j:
            raise LookupError(path)
        return j
    if method == "POST" and path == "/api/mail/check-replies":
        return mail.check_replies()
    if path == "/api/templates" and method == "GET":
        out = rows(c.execute("SELECT * FROM templates ORDER BY segment<>'' , id"))
        for t in out:
            t["attachments"] = attachments.listing(c, t["id"])
        return {"rows": out}
    if path == "/api/templates" and method == "POST":
        f = {k: str(body.get(k, "")).strip() if k != "html" and k != "plain" else str(body.get(k, "")) for k in ("name", "subject", "plain", "html", "segment", "region")}
        if f["region"] not in ("", "Indian", "Foreign"):
            raise ValueError("region must be Indian, Foreign or blank")
        if not f["name"] or not f["subject"] or not f["plain"].strip() or not f["html"].strip():
            raise ValueError("name, subject, plain and html are required")
        if body.get("id"):
            c.execute("UPDATE templates SET name=?,subject=?,plain=?,html=?,segment=?,region=?,updated_at=? WHERE id=?",
                      (f["name"], f["subject"], f["plain"], f["html"], f["segment"], f["region"], mail.now(), int(body["id"])))
            return {"id": int(body["id"])}
        cur = c.execute("INSERT INTO templates(name,subject,plain,html,segment,region,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (f["name"], f["subject"], f["plain"], f["html"], f["segment"], f["region"], mail.now()))
        return {"id": cur.lastrowid}
    mm = re.match(r"^/api/templates/(\d+)/duplicate$", path)
    if method == "POST" and mm:
        t = c.execute("SELECT * FROM templates WHERE id=?", (int(mm.group(1)),)).fetchone()
        if not t:
            return None
        cur = c.execute("INSERT INTO templates(name,subject,plain,html,segment,region,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (t["name"] + " (copy)", t["subject"], t["plain"], t["html"], "step" if t["segment"] == "" else t["segment"], t["region"], mail.now()))
        attachments.duplicate(c, t["id"], cur.lastrowid)
        return {"id": cur.lastrowid}
    mm = re.match(r"^/api/templates/attachments/(\d+)$", path)
    if method == "DELETE" and mm:
        return attachments.remove(c, int(mm.group(1)))
    if method == "DELETE" and path.startswith("/api/templates/"):
        tid = int(path.rsplit("/", 1)[1])
        if c.execute("SELECT COUNT(*) FROM templates").fetchone()[0] <= 1:
            raise ValueError("keep at least one template")
        if c.execute("SELECT 1 FROM sequence_steps WHERE template_id=?", (tid,)).fetchone():
            raise ValueError("this template is used by a sequence step \u2014 change the step first")
        attachments.remove_all(c, tid)
        c.execute("DELETE FROM templates WHERE id=?", (tid,))
        return {"ok": True}
    raise LookupError(path)


class _Actor(threading.local):
    name = ""

    def get(self):
        return self.name


ACTOR = _Actor()


def _guarded_api(method, path, q, body):
    """Run an API call; stamp the acting team member on the activity rows this request created."""
    with db.connect() as c0:
        before = c0.execute("SELECT COALESCE(MAX(id),0) FROM activities").fetchone()[0]
    res = api(method, path, q, body)
    if ACTOR.get() and method != "GET":
        with db.connect() as c1:
            c1.execute("UPDATE activities SET actor=? WHERE id>? AND actor IS NULL AND type IN ('note','call','meeting','whatsapp','email','stage','onboarding')", (ACTOR.get(), before))
    return res


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, data, ctype="application/json"):
        b = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _origin_ok(self):
        """Blocks other websites from driving this app: DNS-rebinding (Host) and cross-site requests (Origin), plus a custom header on every write."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower() if not (self.headers.get("Host") or "").startswith("[") else "[::1]"
        if host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and (urlparse(origin).hostname or "").lower().strip("[]") not in {h.strip("[]") for h in ALLOWED_HOSTS}:
            return False
        return True

    def handle_any(self, method):
        u = urlparse(self.path)
        db.CTX.name = ""            # keep-alive connections reuse threads: never inherit the previous request's person
        is_webhook = method == "POST" and u.path == "/api/youform/webhook"   # server-to-server, protected by its HMAC signature instead
        if not is_webhook:
            if not self._origin_ok():
                return self.send(403, {"error": "forbidden host or origin"})
            if method != "GET" and self.headers.get("X-CRM") != "1":
                return self.send(403, {"error": "missing X-CRM header"})
        if method == "POST" and u.path == "/api/youform/webhook":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                cfg = youform.load_cfg()
                if not cfg["signing_secret"] or not youform.verify_signature(raw, self.headers.get("Signature"), cfg["signing_secret"]):
                    return self.send(401, {"error": "bad signature"})
                app = youform.from_webhook(json.loads(raw))
                if not app or (app["form_id"] and app["form_id"] != youform.FORM_SLUG):
                    return self.send(200, {"ok": True, "ignored": True})
                with db.connect() as c:
                    return self.send(200, {"ok": True, "result": youform.ingest(c, app)})
            except Exception as e:  # noqa
                return self.send(400, {"error": repr(e)})
        m = re.match(r"^/api/onboarding/(\d+)/files$", u.path)
        if method == "POST" and m:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > onboarding.MAX_BYTES:
                    self.close_connection = True
                    return self.send(413, {"error": "file is larger than 25 MB"})
                data = self.rfile.read(n)
                qs = parse_qs(u.query)
                with db.connect() as c:
                    return self.send(200, onboarding.save_file(c, int(m.group(1)), (qs.get("item") or [""])[0], (qs.get("name") or ["file"])[0], data))
            except (ValueError, KeyError) as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:  # noqa
                log.exception("unhandled error in %s %s", method, self.path)
                return self.send(500, {"error": repr(e)})
        m = re.match(r"^/api/templates/(\d+)/attachments$", u.path)
        if method == "POST" and m:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > attachments.MAX_FILE:
                    self.close_connection = True
                    return self.send(413, {"error": "file is larger than 10 MB"})
                data = self.rfile.read(n)
                with db.connect() as c:
                    return self.send(200, attachments.add(c, int(m.group(1)), (parse_qs(u.query).get("name") or ["file"])[0], data))
            except (ValueError, KeyError) as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:  # noqa
                log.exception("unhandled error in %s %s", method, self.path)
                return self.send(500, {"error": repr(e)})
        m = re.match(r"^/api/templates/attachments/(\d+)$", u.path)
        if method == "GET" and m:
            with db.connect() as c:
                fp, f = attachments.path_of(c, int(m.group(1)))
            if not fp:
                return self.send(404, {"error": "not found"})
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(f['orig_name'])}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b)
            return
        m = re.match(r"^/api/onboarding/files/(\d+)$", u.path)
        if method == "GET" and m:
            with db.connect() as c:
                fp, f = onboarding.file_path(c, int(m.group(1)))
            if not fp:
                return self.send(404, {"error": "not found"})
            ext = os.path.splitext(fp)[1].lower()
            inline = (parse_qs(u.query).get("inline") or [""])[0] == "1" and ext in onboarding.INLINE_EXT
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", onboarding.INLINE_EXT.get(ext, "application/octet-stream") if inline else "application/octet-stream")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(f['orig_name'])}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
            return
        ACTOR.name = unquote(self.headers.get("X-Actor") or "")[:60]
        db.CTX.name = ACTOR.name or "someone (name not set)"       # stamped on every history entry and change this request makes
        if u.path.startswith("/api/"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                res = _guarded_api(method, unquote(u.path), parse_qs(u.query), body)
                return self.send(404, {"error": "not found"}) if res is None else self.send(200, res)
            except LookupError:
                return self.send(404, {"error": "not found"})
            except (ValueError, KeyError) as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:  # noqa
                log.exception("unhandled error in %s %s", method, self.path)
                return self.send(500, {"error": repr(e)})
        root = STATIC
        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        if rel.startswith("assets/"):
            root, rel = mail.ASSETS, rel[len("assets/"):]
        fp = os.path.realpath(os.path.join(root, rel))
        if not fp.startswith(root) or not os.path.isfile(fp):
            return self.send(404, b"not found", "text/plain")
        self.send(200, open(fp, "rb").read(), mimetypes.guess_type(fp)[0] or "application/octet-stream")

    do_GET = lambda s: s.handle_any("GET")
    do_POST = lambda s: s.handle_any("POST")
    do_PUT = lambda s: s.handle_any("PUT")
    do_PATCH = lambda s: s.handle_any("PATCH")
    do_DELETE = lambda s: s.handle_any("DELETE")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init()
    with db.connect() as _c:
        # Templates and follow-up steps are never created automatically: they only exist if you make them in the editor.
        scoring.compute_all(_c)
    with db.connect() as _c:  # partners onboarded before the country/region rules: re-word their checklist for their region
        for _r in _c.execute("SELECT partner_id FROM onboarding WHERE status<>'Onboarded'").fetchall():
            onboarding.refresh_region(_c, _r[0])
    threading.Thread(target=mail.poller, daemon=True).start()
    threading.Thread(target=backup.scheduler, daemon=True).start()
    threading.Thread(target=watch.scheduler, daemon=True).start()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Partner CRM on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
