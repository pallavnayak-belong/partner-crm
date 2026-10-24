"""Work with backup snapshots from inside the app: preview, compare, restore everything, restore one partner.

Every restore first takes a safety snapshot of the current data, so a restore can itself be undone.
Nothing here ever reads or writes passwords: those live in data/*.json, not in the database.
"""
import os
import re
import shutil
import sqlite3
from datetime import datetime

import backup
import db
import mail

NAME_RE = re.compile(r"^partner_crm-[A-Za-z0-9._-]+\.db$")
COUNT_TABLES = [("partners", "Partners"), ("activities", "Notes and history entries"), ("emails", "Emails sent"), ("tasks", "Tasks"),
                ("partner_contacts", "Extra contacts"), ("templates", "Email templates"), ("onboarding", "Onboardings"), ("applications", "Applications")]
WATCH = ["name", "stage", "owner", "email", "phone", "country", "city", "category", "segment", "contact_person", "do_not_contact", "archived", "lost_reason", "follow_up"]
# child tables copied when a single partner is restored (id is dropped so new rows can't collide with live ones)
CHILDREN = ["activities", "emails", "tasks", "partner_contacts", "onboarding_items", "onboarding_files", "commercial_lines", "referred_customers"]
ONE_ROW = ["onboarding", "commercials"]            # keyed by partner_id


class RestoreError(ValueError):
    pass


def path_of(name):
    if not NAME_RE.match(name or ""):
        raise RestoreError("That isn't a backup file from this CRM")
    p = os.path.join(backup.load_cfg()["folder"], "snapshots", name)
    if not os.path.isfile(p):
        raise RestoreError("That backup file is no longer on this Mac")
    return p


def _open_ro(name):
    c = sqlite3.connect("file:" + path_of(name) + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _conn(which):
    return db.connect() if which == "live" else _open_ro(which)


def _counts(c):
    out = {}
    for t, _ in COUNT_TABLES:
        try:
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = 0
    return out


def label_of(name):
    m = re.match(r"partner_crm-(\d{8})-(\d{6})-(.*)\.db$", name)
    if not m:
        return name
    when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    why = m.group(3)
    nice = {"auto": "automatic", "manual": "manual"}.get(why) or ("before " + why.split("before-", 1)[1].replace("-", " ") if why.startswith("before-") else why)
    return f"{when.strftime('%d %b %Y, %H:%M')} ({nice})"


def listing():
    rows = []
    for s in backup.listing():
        rows.append({**s, "label": label_of(s["file"])})
    return rows


def preview(name):
    """What restoring this snapshot would change, in plain numbers."""
    snap = _open_ro(name)
    live = db.connect()
    try:
        a, b = _counts(snap), _counts(live)
        return {"file": name, "label": label_of(name), "rows": [{"key": t, "label": lab, "backup": a[t], "now": b[t], "diff": a[t] - b[t]} for t, lab in COUNT_TABLES],
                "integrity": snap.execute("PRAGMA integrity_check").fetchone()[0]}
    finally:
        snap.close(); live.close()


def compare(a, b="live"):
    """Differences between two snapshots (or a snapshot and the live data): a = older/reference, b = newer."""
    ca, cb = _conn(a), _conn(b)
    try:
        na, nb = _counts(ca), _counts(cb)
        pa = {r["id"]: r for r in ca.execute("SELECT * FROM partners")}
        pb = {r["id"]: r for r in cb.execute("SELECT * FROM partners")}
        cols = [w for w in WATCH if w in next(iter(pa.values()), {}).keys() and w in next(iter(pb.values()), {}).keys()] if pa and pb else WATCH
        added = [{"id": i, "name": pb[i]["name"]} for i in pb if i not in pa]
        removed = [{"id": i, "name": pa[i]["name"]} for i in pa if i not in pb]
        by_field, examples, touched = {}, [], set()
        for i in pa:
            if i not in pb:
                continue
            for f in cols:
                x, y = pa[i][f], pb[i][f]
                if (x or "") != (y or "") and not (f in ("do_not_contact", "archived") and not x and not y):
                    by_field[f] = by_field.get(f, 0) + 1
                    touched.add(i)
                    if len(examples) < 60:
                        examples.append({"id": i, "name": pb[i]["name"], "field": f, "from": "" if x is None else str(x)[:60], "to": "" if y is None else str(y)[:60]})
        warnings = []
        if len(removed) >= 5:
            warnings.append(f"{len(removed)} partners are missing compared with the earlier copy.")
        for f, n in by_field.items():
            if n >= 20 and f != "follow_up":
                warnings.append(f"{n} partners had their {f.replace('_', ' ')} changed. Check this was intended (a bulk edit?).")
        return {"from": a, "to": b, "from_label": "Now" if a == "live" else label_of(a), "to_label": "Now" if b == "live" else label_of(b),
                "counts": [{"label": lab, "before": na[t], "after": nb[t], "diff": nb[t] - na[t]} for t, lab in COUNT_TABLES],
                "added": added[:50], "added_n": len(added), "removed": removed[:50], "removed_n": len(removed), "changed_partners": len(touched),
                "by_field": by_field, "examples": examples, "warnings": warnings}
    finally:
        ca.close(); cb.close()


def _busy():
    return any(j.get("state") == "running" for j in mail.JOBS.values()) if hasattr(mail, "JOBS") else False


def restore_all(name, by=""):
    """Replace the live data with a snapshot. The current data is saved first as '<time>-before-restore'."""
    p = path_of(name)
    if _busy():
        raise RestoreError("An email send is in progress. Wait for it to finish, then restore.")
    chk = sqlite3.connect("file:" + p + "?mode=ro", uri=True)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        n = chk.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        raise RestoreError("That backup is damaged and can't be restored: " + str(ok))
    safety = backup.snapshot_before("restore")
    last = None
    for _ in range(5):
        src = sqlite3.connect("file:" + p + "?mode=ro", uri=True)
        dst = sqlite3.connect(db.DB_PATH, timeout=30)
        try:
            src.backup(dst)
            last = None
            break
        except sqlite3.OperationalError as e:
            last = e
        finally:
            src.close(); dst.close()
        import time
        time.sleep(1)
    if last:
        raise RestoreError("The database was busy and couldn't be replaced. Try again in a moment. (Nothing was changed.) " + str(last))
    db.init()                    # an older snapshot may predate newer columns/tables
    with db.connect() as c:
        c.execute("INSERT INTO backup_runs(ts,kind,ok,detail) VALUES(?,?,?,?)", (datetime.now().isoformat(timespec="seconds"), "restore", 1, f"Restored {label_of(name)} ({n} partners){(' by ' + by) if by else ''}. Previous data saved as {safety}"))
    return {"ok": True, "partners": n, "safety": safety}


def partners_in(name, q="", limit=40):
    snap = _open_ro(name)
    live = db.connect()
    try:
        live_ids = {r[0] for r in live.execute("SELECT id FROM partners")}
        erased = {r[0] for r in live.execute("SELECT partner_id FROM erasure_log") if r[0] is not None}
        like = f"%{q.strip()}%"
        rows = snap.execute("SELECT id,name,stage,email,city,country FROM partners WHERE name LIKE ? OR email LIKE ? ORDER BY name LIMIT ?", (like, like, limit)).fetchall()
        return [{"id": r["id"], "name": r["name"], "stage": r["stage"], "email": r["email"], "city": r["city"], "country": r["country"],
                 "in_live": r["id"] in live_ids, "erased": r["id"] in erased} for r in rows]
    finally:
        snap.close(); live.close()


def _common(src, dst, table):
    a = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
    b = [r[1] for r in dst.execute(f"PRAGMA table_info({table})")]
    return [x for x in a if x in b]


def restore_partner(name, pid, replace=False, allow_erased=False, by=""):
    """Bring one partner (with notes, emails, tasks, people, tags, onboarding, commercials) back from a snapshot.
    If they still exist, `replace` overwrites their current data with the snapshot's version."""
    pid = int(pid)
    snap = _open_ro(name)
    try:
        sp = snap.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
        if not sp:
            raise RestoreError("That partner isn't in this backup")
        live = db.connect()
        try:
            exists = live.execute("SELECT 1 FROM partners WHERE id=?", (pid,)).fetchone()
            if exists and not replace:
                raise RestoreError("This partner still exists. Choose 'replace with the backed-up version' if you want to overwrite their current data.")
            if not exists and live.execute("SELECT 1 FROM erasure_log WHERE partner_id=?", (pid,)).fetchone() and not allow_erased:
                raise RestoreError("This partner was permanently deleted on purpose (it is in the erasure log). Confirm that you want them back.")
            if _busy():
                raise RestoreError("An email send is in progress. Wait for it to finish.")
            safety = backup.snapshot_before(f"restore-partner-{pid}")
            counts = {}
            code = sp["partner_code"] if "partner_code" in sp.keys() else None
            if code and live.execute("SELECT 1 FROM partners WHERE partner_code=? AND id<>?", (code, pid)).fetchone():
                code = None          # that referral code now belongs to someone else
            with live:
                cols = [c for c in _common(snap, live, "partners")]
                vals = {c: (code if c == "partner_code" else sp[c]) for c in cols}
                if exists:
                    sets = ",".join(f"{c}=?" for c in cols if c != "id")
                    live.execute(f"UPDATE partners SET {sets} WHERE id=?", [vals[c] for c in cols if c != "id"] + [pid])
                    for t in CHILDREN:
                        live.execute(f"DELETE FROM {t} WHERE partner_id=?", (pid,))
                    for t in ONE_ROW:
                        live.execute(f"DELETE FROM {t} WHERE partner_id=?", (pid,))
                    live.execute("DELETE FROM partner_tags WHERE partner_id=?", (pid,))
                else:
                    live.execute(f"INSERT INTO partners({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [vals[c] for c in cols])
                for t in CHILDREN + ONE_ROW:
                    try:
                        tc = [c for c in _common(snap, live, t) if not (t in CHILDREN and c == "id")]
                        rows = snap.execute(f"SELECT {','.join(tc)} FROM {t} WHERE partner_id=?", (pid,)).fetchall()
                    except sqlite3.OperationalError:
                        continue
                    for r in rows:
                        live.execute(f"INSERT OR REPLACE INTO {t}({','.join(tc)}) VALUES({','.join('?' * len(tc))})", [r[c] for c in tc])
                    counts[t] = len(rows)
                tags = snap.execute("SELECT t.name FROM partner_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.partner_id=?", (pid,)).fetchall()
                for (tn,) in tags:
                    live.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (tn,))
                    tid = live.execute("SELECT id FROM tags WHERE name=?", (tn,)).fetchone()[0]
                    live.execute("INSERT OR IGNORE INTO partner_tags(partner_id,tag_id) VALUES(?,?)", (pid, tid))
                counts["tags"] = len(tags)
                live.execute("INSERT INTO activities(partner_id,ts,type,body,actor) VALUES(?,?,?,?,?)",
                             (pid, datetime.now().isoformat(timespec="seconds"), "note", f"Restored from backup {label_of(name)}" + (" (replaced the current version)" if exists else ""), by or "system"))
                live.execute("UPDATE partners SET follow_up=(SELECT MIN(due) FROM tasks WHERE partner_id=? AND done_at IS NULL AND due IS NOT NULL AND due<>'') WHERE id=?", (pid, pid))
                live.execute("INSERT INTO backup_runs(ts,kind,ok,detail) VALUES(?,?,?,?)", (datetime.now().isoformat(timespec="seconds"), "restore", 1, f"Restored partner {sp['name']} from {label_of(name)}{(' by ' + by) if by else ''}"))
            docs = _restore_documents(pid)
            return {"ok": True, "name": sp["name"], "id": pid, "counts": counts, "documents": docs, "safety": safety, "replaced": bool(exists)}
        finally:
            live.close()
    finally:
        snap.close()


def _restore_documents(pid):
    """Copy this partner's onboarding files back from the local mirror if they're missing on disk."""
    import onboarding
    src = os.path.join(backup.load_cfg()["folder"], "files", "onboarding", str(pid))
    dst = os.path.join(onboarding.DIR, str(pid))
    n = 0
    if os.path.isdir(src):
        os.makedirs(dst, mode=0o700, exist_ok=True)
        for f in os.listdir(src):
            if not os.path.exists(os.path.join(dst, f)):
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
                n += 1
    with db.connect() as c:
        missing = sum(1 for r in c.execute("SELECT stored_name FROM onboarding_files WHERE partner_id=?", (pid,)) if not os.path.isfile(os.path.join(dst, r[0])))
    return {"restored": n, "missing": missing}
