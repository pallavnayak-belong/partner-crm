"""Automatic backups: a consistent SQLite snapshot (online backup API, safe while the app runs) plus a mirror of uploaded documents.

Secrets (mail.json, attribution/youform/slack config) are deliberately NOT copied.
Snapshots older than `keep_days` are deleted, so a deleted partner's data also ages out of backups.
"""
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import db

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("PARTNER_CRM_BACKUP_CFG", os.path.join(HERE, "data", "backup.json"))
DOC_DIRS = {"onboarding": None, "attachments": None}   # resolved lazily so tests/other modules can repoint them
now = lambda: datetime.now().isoformat(timespec="seconds")
_lock = threading.Lock()


def default_folder():
    return os.path.join(os.path.expanduser("~"), "partner-crm-backups")


def load_cfg():
    cfg = {"folder": default_folder(), "keep_days": 30, "enabled": True, "last_run": "", "last_result": "", "last_ok": "", "alert_email": "", "alerts_sent": {}}
    try:
        with open(CFG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(upd):
    cfg = load_cfg()
    if "folder" in upd:
        f = os.path.expanduser(str(upd["folder"]).strip())
        if not f or not os.path.isabs(f):
            raise ValueError("Choose a full folder path, e.g. /Users/you/Documents/partner-crm-backups")
        if os.path.realpath(f).startswith(os.path.realpath(HERE) + os.sep):
            raise ValueError("Pick a folder outside the app folder, so a backup survives if the app folder is lost")
        cfg["folder"] = f
    if "keep_days" in upd:
        cfg["keep_days"] = max(7, int(upd["keep_days"]))
    if "enabled" in upd:
        cfg["enabled"] = bool(upd["enabled"])
    for k in ("last_run", "last_result", "last_ok"):
        if k in upd:
            cfg[k] = str(upd[k])
    if "alert_email" in upd:
        a = str(upd["alert_email"]).strip()
        if a and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a):
            raise ValueError("That doesn't look like an email address")
        cfg["alert_email"] = a
    if "alerts_sent" in upd:
        cfg["alerts_sent"] = dict(upd["alerts_sent"])
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    fd = os.open(CFG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return cfg


def _doc_dir(name):
    import attachments
    import onboarding
    return {"onboarding": onboarding.DIR, "attachments": attachments.DIR}[name]


def _snapshot_to(path):
    """Consistent copy of the live database, then verify it opens and has data."""
    src = sqlite3.connect(db.DB_PATH, timeout=30)
    try:
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")   # a single self-contained file, no -wal/-shm beside it
        finally:
            dst.close()
    finally:
        src.close()
    chk = sqlite3.connect(path)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        n = chk.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
    finally:
        chk.close()
    for ext in ("-shm", "-wal"):   # leftovers from opening the copy while it was still flagged as WAL; none hold data once all connections are closed
        try:
            os.remove(path + ext)
        except OSError:
            pass
    os.chmod(path, 0o600)
    if ok != "ok":
        os.remove(path)
        raise RuntimeError(f"snapshot failed its integrity check: {ok}")
    return n


def _mirror_docs(folder):
    copied = 0
    for name in DOC_DIRS:
        src = _doc_dir(name)
        if not os.path.isdir(src):
            continue
        dst_root = os.path.join(folder, "files", name)
        for root, _, files in os.walk(src):
            rel = os.path.relpath(root, src)
            os.makedirs(os.path.join(dst_root, rel), mode=0o700, exist_ok=True)
            for f in files:
                s, d = os.path.join(root, f), os.path.join(dst_root, rel, f)
                if not os.path.exists(d) or os.path.getsize(d) != os.path.getsize(s):
                    shutil.copy2(s, d)
                    os.chmod(d, 0o600)
                    copied += 1
    return copied


def _prune(folder, keep_days):
    cutoff = time.time() - keep_days * 86400
    removed = 0
    d = os.path.join(folder, "snapshots")
    if os.path.isdir(d):
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            if f.endswith(".db") and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                removed += 1
    return removed


def run(label="auto"):
    """Take a snapshot + sync documents. Returns a summary dict; never raises (the result is also stored for the Settings page)."""
    cfg = load_cfg()
    with _lock:
        try:
            snaps = os.path.join(cfg["folder"], "snapshots")
            os.makedirs(snaps, mode=0o700, exist_ok=True)
            name = f"partner_crm-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{label}.db"
            n = _snapshot_to(os.path.join(snaps, name))
            copied = _mirror_docs(cfg["folder"])
            pruned = _prune(cfg["folder"], cfg["keep_days"])
            try:
                import vault
                vault.write_local(cfg["folder"])      # the encrypted settings copy rides along with every snapshot
            except Exception:  # noqa
                pass
            res = {"ok": True, "file": name, "partners": n, "documents_copied": copied, "pruned": pruned}
            save_cfg({"last_run": now(), "last_ok": now(), "last_result": f"OK: {name} ({n} partners, {copied} new documents)"})
            _log_run(True, f"{name} ({n} partners, {copied} new documents)", os.path.getsize(os.path.join(snaps, name)) // 1024)
            try:
                import alerts
                alerts.clear("local-fail")
            except Exception:  # noqa
                pass
        except Exception as e:  # noqa
            res = {"ok": False, "error": str(e)[:300]}
            save_cfg({"last_run": now(), "last_result": "FAILED: " + str(e)[:200]})
            _log_run(False, str(e)[:200])
            try:
                import alerts
                alerts.send("local-fail", "the backup on this Mac failed", "The automatic backup on this Mac could not be made:\n" + str(e)[:300] + "\n\nYour data is still in the CRM, but there is no fresh copy until this is fixed.")
            except Exception:  # noqa
                pass
    return res


def snapshot_before(label):
    """Cheap safety copy taken before a destructive action (merge / delete). Raises if it can't be made, so the action doesn't proceed unprotected."""
    cfg = load_cfg()
    snaps = os.path.join(cfg["folder"], "snapshots")
    os.makedirs(snaps, mode=0o700, exist_ok=True)
    stamp, name, i = datetime.now().strftime('%Y%m%d-%H%M%S'), None, 1
    while name is None or os.path.exists(os.path.join(snaps, name)):      # two safety copies in the same second must never overwrite each other
        name = f"partner_crm-{stamp}-before-{label}{'' if i == 1 else '-' + str(i)}.db"
        i += 1
    _snapshot_to(os.path.join(snaps, name))
    return name


def forget_documents(partner_id):
    """Remove one partner's documents from the backup mirror (used when a partner is erased)."""
    cfg = load_cfg()
    shutil.rmtree(os.path.join(cfg["folder"], "files", "onboarding", str(partner_id)), ignore_errors=True)


def latest_snapshot_path():
    d = os.path.join(load_cfg()["folder"], "snapshots")
    if not os.path.isdir(d):
        return None
    snaps = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".db")]
    return max(snaps, key=os.path.getmtime) if snaps else None


def latest_age_hours():
    p = latest_snapshot_path()
    return (time.time() - os.path.getmtime(p)) / 3600 if p else 1e9


def _log_run(ok, detail, size_kb=None):
    try:
        with db.connect() as c:
            c.execute("INSERT INTO backup_runs(ts,kind,ok,detail,size_kb) VALUES(?,?,?,?,?)", (now(), "local", 1 if ok else 0, str(detail)[:300], size_kb))
    except Exception:  # noqa
        pass


def listing():
    cfg = load_cfg()
    d = os.path.join(cfg["folder"], "snapshots")
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d), reverse=True):
            if f.endswith(".db"):
                fp = os.path.join(d, f)
                out.append({"file": f, "size": os.path.getsize(fp), "at": datetime.fromtimestamp(os.path.getmtime(fp)).isoformat(timespec="seconds")})
    return out


def due(cfg=None):
    cfg = cfg or load_cfg()
    if not cfg["enabled"]:
        return False
    if not cfg["last_ok"]:
        return True
    return datetime.now() - datetime.fromisoformat(cfg["last_ok"]) >= timedelta(hours=24)


def scheduler():
    """Checks hourly; takes a backup whenever the last good one is over 24 hours old (also right after startup)."""
    time.sleep(20)
    while True:
        try:
            if due():
                cfg = load_cfg()
                gap = (datetime.now() - datetime.fromisoformat(cfg["last_ok"])).total_seconds() / 3600 if cfg["last_ok"] else 0
                res = run("auto")
                if res.get("ok") and gap > 36:
                    import alerts
                    alerts.send("caught-up", "backup is working again", f"This Mac was off or asleep, so no backup was made for about {int(gap)} hours. The CRM has just made a fresh one ({res['partners']} partners).", throttle_hours=24)
        except Exception:  # noqa
            pass
        try:
            import gdrive
            gdrive.maybe_run()
        except Exception:  # noqa
            pass
        time.sleep(3600)
