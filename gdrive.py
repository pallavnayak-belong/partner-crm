"""Google Drive + Sheets backup through the user's own Apps Script relay (gdrive_relay.gs).

Uploads: database snapshot (gzip), uploaded documents (changed files only), a zip of the app code, a restore guide.
Sheets : mirrors CRM tables into a Google Sheet and rebuilds its Dashboard tab.
Passwords and API tokens are never uploaded.
"""
import base64
import gzip
import hashlib
import io
import json
import os
import random
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import attachments
import backup
import db
import alerts
import health
import notify
import onboarding
import vault
import sheet_export

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("PARTNER_CRM_GDRIVE_CFG", os.path.join(HERE, "data", "gdrive.json"))
CHUNK = 1_500_000            # raw bytes per request (base64 makes it ~2 MB)
MAX_DOC_BYTES = 15 * 1024 * 1024   # the relay assembles files in memory; larger documents stay local-only
ROW_CHUNK = 700
now = lambda: datetime.now().isoformat(timespec="seconds")
utcnow = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
_lock = threading.Lock()
JOB = {"state": "idle", "started": "", "parts": [], "result": None, "error": ""}


class RelayError(Exception):
    pass


# ---------------- config ----------------
def load_cfg():
    cfg = {"enabled": False, "url": "", "token": "", "include_documents": True, "include_code": True, "sheet_sync_hours": 6, "keep_days": 30,
           "root_url": "", "sheet_url": "", "last_drive_ok": "", "last_drive_result": "", "last_sheets_ok": "", "last_sheets_result": "",
           "code_hash": "", "code_uploaded": "", "failures": 0, "last_alert": "", "last_snapshot": None, "last_verify": "", "last_verify_result": "",
           "last_drill": "", "last_drill_result": "", "vault_size": 0}
    try:
        with open(CFG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(upd):
    cfg = load_cfg()
    if "url" in upd:
        u = str(upd["url"]).strip()
        if u and not (u.startswith("https://script.google.com/") and "/exec" in u):
            raise ValueError("Paste the Web app URL from the Apps Script deployment. It starts with https://script.google.com/ and ends in /exec")
        cfg["url"] = u
    for k in ("enabled", "include_documents", "include_code"):
        if k in upd:
            cfg[k] = bool(upd[k])
    if "sheet_sync_hours" in upd:
        cfg["sheet_sync_hours"] = max(1, int(upd["sheet_sync_hours"]))
    if "keep_days" in upd:
        cfg["keep_days"] = max(7, int(upd["keep_days"]))
    for k in ("token", "root_url", "sheet_url", "last_drive_ok", "last_drive_result", "last_sheets_ok", "last_sheets_result", "code_hash", "code_uploaded", "failures", "last_alert", "last_snapshot", "last_verify", "last_verify_result", "last_drill", "last_drill_result", "vault_size"):
        if k in upd:
            cfg[k] = upd[k]
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    fd = os.open(CFG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return cfg


def ensure_token():
    cfg = load_cfg()
    if not cfg["token"]:
        cfg = save_cfg({"token": secrets.token_hex(32)})
    return cfg["token"]


def script_text():
    with open(os.path.join(HERE, "gdrive_relay.gs"), encoding="utf-8") as f:
        return f.read().replace("__TOKEN__", ensure_token())


def configured(cfg=None):
    cfg = cfg or load_cfg()
    return bool(cfg["url"] and cfg["token"])


# ---------------- transport ----------------
def _call(payload, timeout=180, cfg=None):
    cfg = cfg or load_cfg()
    if not configured(cfg):
        raise RelayError("Google backup isn't set up yet")
    data = json.dumps({**payload, "token": cfg["token"]}).encode()
    last = None
    for attempt in range(3):
        try:
            # Apps Script answers a POST with a redirect to the result; urllib follows it (as a GET), which is what the platform expects
            req = urllib.request.Request(cfg["url"], data=data, headers={"Content-Type": "text/plain;charset=utf-8"})
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            try:
                res = json.loads(raw)
            except ValueError:
                raise RelayError("That URL didn't answer like the backup script. Check the deployment is set to 'Execute as: Me' and 'Who has access: Anyone'.")
            if not res.get("ok"):
                raise RelayError(res.get("error") or "relay error")
            return res
        except RelayError as e:
            if str(e) in ("bad token", "unknown action"):
                raise
            last = e
        except (urllib.error.URLError, OSError) as e:
            last = RelayError(f"Couldn't reach Google ({e})")
        time.sleep(2 * (attempt + 1))
    raise last


def test():
    res = _call({"action": "ping"})
    save_cfg({"root_url": res.get("root_url", ""), "sheet_url": res.get("sheet_url", "")})
    return {"root_url": res.get("root_url"), "sheet_url": res.get("sheet_url"), "status": res.get("status")}


def upload_bytes(data, remote_path, mime="application/octet-stream"):
    md5 = hashlib.md5(data).hexdigest()
    uid = _call({"action": "begin_upload"})["id"]
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
    import base64
    for i, ch in enumerate(chunks):
        _call({"action": "upload_chunk", "id": uid, "i": i, "data": base64.b64encode(ch).decode()})
    res = _call({"action": "finish_upload", "id": uid, "path": remote_path, "total": len(chunks), "size": len(data), "md5": md5, "mime": mime})
    if res.get("md5") != md5:
        raise RelayError(f"checksum mismatch uploading {remote_path}")
    return res


def download_bytes(path, timeout=240):
    """Fetch a Drive file through the relay in slices."""
    out, size = b"", None
    while size is None or len(out) < size:
        r = _call({"action": "download", "path": path, "offset": len(out), "length": CHUNK}, timeout=timeout)
        size = r["size"]
        piece = base64.b64decode(r["data"])
        if not piece and len(out) < size:
            raise RelayError(f"download of {path} stalled")
        out += piece
    return out


def upload_settings():
    """The encrypted settings copy (only if you've set a passphrase)."""
    blob = vault.build_blob()
    if blob is None:
        return None
    upload_bytes(blob, "secrets/settings.enc", "application/octet-stream")
    try:
        vault.write_local(backup.load_cfg()["folder"])
    except OSError:
        pass
    save_cfg({"vault_size": len(blob)})
    return {"kb": max(1, len(blob) // 1024)}


def put_text(path, text, mime="text/plain"):
    _call({"action": "put_text", "path": path, "text": text, "mime": mime})


# ---------------- what gets uploaded ----------------
def upload_snapshot():
    snap = backup.latest_snapshot_path()
    if not snap:
        raise RelayError("No local backup to upload yet")
    with open(snap, "rb") as f:
        gz = gzip.compress(f.read(), 6)
    name = os.path.basename(snap) + ".gz"
    upload_bytes(gz, f"snapshots/{name}", "application/gzip")
    save_cfg({"last_snapshot": {"path": f"snapshots/{name}", "md5": hashlib.md5(gz).hexdigest()}})
    return {"file": name, "kb": round(len(gz) / 1024)}


def _code_files():
    skip_dirs = {"data", "__pycache__", ".git", "backups"}
    out = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith((".pyc", ".DS_Store")) or f.startswith("."):
                continue
            fp = os.path.join(root, f)
            out.append((os.path.relpath(fp, HERE), fp))
    return sorted(out)


def code_signature():
    h = hashlib.md5()
    for rel, fp in _code_files():
        h.update(rel.encode()); h.update(str(os.path.getsize(fp)).encode()); h.update(str(int(os.path.getmtime(fp))).encode())
    return h.hexdigest()


def upload_code(force=False):
    cfg = load_cfg()
    sig = code_signature()
    last = cfg["code_uploaded"] and datetime.fromisoformat(cfg["code_uploaded"])
    if not force and sig == cfg["code_hash"] and last and datetime.now() - last < timedelta(days=7):
        return {"skipped": "unchanged"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, fp in _code_files():
            z.write(fp, "partner-crm/" + rel)
    data = buf.getvalue()
    upload_bytes(data, f"code/partner-crm-code-{datetime.now().strftime('%Y%m%d')}.zip", "application/zip")
    save_cfg({"code_hash": sig, "code_uploaded": now()})
    return {"kb": round(len(data) / 1024)}


def _unique_path(used, path):
    if path not in used:
        used.add(path)
        return path
    stem, dot, ext = path.rpartition(".")
    i = 2
    while True:
        cand = f"{stem} ({i}).{ext}" if dot else f"{path} ({i})"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


def wanted_documents(c):
    """[(local path, drive path)] for every document that exists locally, plus the restore index entries for them."""
    used, index, wanted = set(), [], []
    names = {r[0]: r[1] for r in c.execute("SELECT id,name FROM partners")}
    labels = {(r["partner_id"], r["key"]): r["label"].split(" (")[0] for r in c.execute("SELECT partner_id,key,label FROM onboarding_items")}
    for f in c.execute("SELECT * FROM onboarding_files ORDER BY partner_id,id"):
        fp = os.path.join(onboarding.DIR, str(f["partner_id"]), f["stored_name"])
        rp = _unique_path(used, f"documents/{sheet_export.doc_folder(f['partner_id'], names.get(f['partner_id']))}/{labels.get((f['partner_id'], f['item_key']), f['item_key'])}/{f['orig_name']}")
        wanted.append((fp, rp))
        index.append({"kind": "onboarding", "partner_id": f["partner_id"], "stored_name": f["stored_name"], "orig_name": f["orig_name"], "remote_path": rp})
    for a in c.execute("SELECT * FROM template_attachments ORDER BY template_id,id"):
        fp = os.path.join(attachments.DIR, str(a["template_id"]), a["stored_name"])
        rp = _unique_path(used, f"template-attachments/{a['template_id']}/{a['orig_name']}")
        wanted.append((fp, rp))
        index.append({"kind": "template", "template_id": a["template_id"], "stored_name": a["stored_name"], "orig_name": a["orig_name"], "remote_path": rp})
    return wanted, index


def sync_documents(c):
    """Upload new or changed documents; returns counts. The restore index maps every Drive file back to its place in the app."""
    manifest = _call({"action": "get_manifest"}).get("manifest", {})
    wanted, index = wanted_documents(c)
    up = skipped_large = missing = 0
    new_manifest = {}
    for fp, rp in wanted:
        if not os.path.isfile(fp):
            missing += 1
            continue
        size = os.path.getsize(fp)
        if size > MAX_DOC_BYTES:
            skipped_large += 1
            continue
        with open(fp, "rb") as fh:
            data = fh.read()
        md5 = hashlib.md5(data).hexdigest()
        new_manifest[rp] = {"md5": md5, "size": size}
        if manifest.get(rp, {}).get("md5") != md5:
            upload_bytes(data, rp)
            up += 1
    index = [i for i in index if i["remote_path"] in new_manifest]
    put_text("documents/_restore_index.json", json.dumps(index, indent=1), "application/json")
    _call({"action": "put_manifest", "manifest": new_manifest})
    return {"uploaded": up, "total": len(new_manifest), "skipped_large": skipped_large, "missing_locally": missing}


def verify_drive(c, include_documents=True):
    """Ask Google whether the files we think are in Drive really are, and whether the database copy is byte-for-byte what we uploaded.
    Also checks nothing local is missing from Drive. Returns {ok, checked, problems[]}."""
    cfg = load_cfg()
    problems = []
    manifest = _call({"action": "get_manifest"}).get("manifest", {}) if include_documents else {}
    snap = cfg.get("last_snapshot")
    files = [{"path": p, "size": m["size"]} for p, m in manifest.items()]
    if vault.configured() and cfg.get("vault_size"):
        files.append({"path": "secrets/settings.enc", "size": cfg["vault_size"]})
    res = _call({"action": "verify", "files": files, "snapshot": snap}, timeout=240)
    problems += [f"missing in Drive: {p}" for p in res.get("missing", [])]
    problems += [f"wrong size in Drive: {p}" for p in res.get("wrong", [])]
    s = res.get("snapshot")
    if snap and (not s or not s.get("found")):
        problems.append("the latest database copy is not in Drive")
    elif s and not s.get("ok"):
        problems.append("the database copy in Drive does not match what was uploaded")
    if include_documents:
        wanted, _ = wanted_documents(c)
        for fp, rp in wanted:
            if os.path.isfile(fp) and os.path.getsize(fp) <= MAX_DOC_BYTES and rp not in manifest:
                problems.append(f"not uploaded yet: {rp}")
    return {"ok": not problems, "checked": res.get("checked", 0) + (1 if snap else 0), "problems": problems[:20]}


def drill():
    """Restore test: download the newest Drive copies into a temp folder, open them, and check they are complete and match live data.
    Never touches the live database or documents. Returns {ok, steps[], problems[]}."""
    steps, problems = [], []
    tmp = tempfile.mkdtemp(prefix="crm-restore-test-")
    try:
        status = (_call({"action": "ping"}).get("status") or {})
        lat = _call({"action": "latest", "folder": "snapshots"})
        if not lat.get("found"):
            raise RelayError("There is no database copy in Drive")
        path = "snapshots/" + lat["name"]
        gz = download_bytes(path)
        rec = load_cfg().get("last_snapshot") or {}
        if rec.get("path") == path and rec.get("md5") != hashlib.md5(gz).hexdigest():
            problems.append("the database copy from Drive does not match its upload checksum")
        dbp = os.path.join(tmp, "restored.db")
        with open(dbp, "wb") as f:
            f.write(gzip.decompress(gz))
        chk = sqlite3.connect(dbp)
        try:
            integ = chk.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {t: chk.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("partners", "activities", "emails", "tasks", "templates", "onboarding")}
        except sqlite3.DatabaseError as e:
            return {"ok": False, "steps": steps, "problems": problems + [f"the database copy in Drive can't be opened ({e})"]}
        finally:
            chk.close()
        if integ != "ok":
            problems.append("the restored database failed its integrity check: " + str(integ))
        with db.connect() as c:
            live_n = c.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
            wanted, _ = wanted_documents(c)
        if counts["partners"] == 0 or abs(counts["partners"] - live_n) > max(5, int(live_n * 0.03)):
            problems.append(f"the restored copy has {counts['partners']} partners but the CRM has {live_n}")
        steps.append(f"Database from {lat['name']}: {counts['partners']} partners, {counts['activities']} history entries, {counts['emails']} emails, integrity {integ}")

        manifest = _call({"action": "get_manifest"}).get("manifest", {})
        sample = [p for p, m in manifest.items() if m["size"] <= 3 * 1024 * 1024]
        random.shuffle(sample)
        bad = 0
        for p in sample[:3]:
            if hashlib.md5(download_bytes(p)).hexdigest() != manifest[p]["md5"]:
                bad += 1
                problems.append(f"document differs from what was uploaded: {p}")
        steps.append(f"Documents: {min(3, len(sample))} of {len(manifest)} downloaded and compared, {bad} problem(s)")

        code = _call({"action": "latest", "folder": "code"})
        if code.get("found"):
            zp = os.path.join(tmp, "code.zip")
            with open(zp, "wb") as f:
                f.write(download_bytes("code/" + code["name"]))
            with zipfile.ZipFile(zp) as z:
                names = set(z.namelist())
                if z.testzip() is not None or not {"partner-crm/server.py", "partner-crm/restore.py"} <= names:
                    problems.append("the app code in Drive is incomplete")
            steps.append(f"App code {code['name']}: opens, {len(names)} files")
        else:
            problems.append("no copy of the app code in Drive")

        if vault.configured():
            try:
                names = vault.check_stored(download_bytes("secrets/settings.enc"))
                steps.append("Encrypted settings: opens with your passphrase key (" + ", ".join(names) + ")")
            except Exception as e:  # noqa
                problems.append("encrypted settings copy: " + str(e)[:120])
        return {"ok": not problems, "steps": steps, "problems": problems}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_drill(auto=False):
    try:
        r = drill()
    except Exception as e:  # noqa
        r = {"ok": False, "steps": [], "problems": [str(e)[:200]]}
    msg = ("OK: " if r["ok"] else "PROBLEMS: ") + ("; ".join(r["steps"]) if r["ok"] else "; ".join(r["problems"][:3]))
    save_cfg({"last_drill": now(), "last_drill_result": msg[:400]})
    _log("drill", r["ok"], msg)
    if not r["ok"] or auto:
        alerts.send("restore-test", ("monthly restore test passed" if r["ok"] else "the restore test found a problem"),
                    ("The CRM downloaded your Google Drive backup into a scratch folder and rebuilt it. Result:\n\n" + "\n".join(r["steps"] + [("PROBLEM: " + p) for p in r["problems"]])), throttle_hours=0.01)
    return r


def forget_partner(pid):
    """Best effort: when a partner is permanently deleted, remove their documents from Drive too."""
    try:
        if configured():
            _call({"action": "trash_prefix", "parent": "documents", "prefix": f"{int(pid)} - "}, timeout=60)
            m = _call({"action": "get_manifest"}).get("manifest", {})
            pre = "documents/" + f"{int(pid)} - "
            _call({"action": "put_manifest", "manifest": {k: v for k, v in m.items() if not k.startswith(pre)}})
    except Exception:  # noqa
        pass


def push_sheets(c):
    tabs = sheet_export.tables(c)
    total = 0
    for tab, (header, rows) in tabs.items():
        first = True
        for i in range(0, max(len(rows), 1), ROW_CHUNK):
            chunk = rows[i:i + ROW_CHUNK]
            _call({"action": "sheet_write", "tab": tab, "header": header, "rows": chunk, "first": first}, timeout=240)
            first = False
        total += len(rows)
    h_rows, log_rows = health.sheet_rows(c)
    _call({"action": "sheet_write", "tab": "Backup health", "header": ["Item", "Value"], "rows": [[sheet_export.cell(v) for v in r] for r in h_rows], "first": True})
    _call({"action": "sheet_write", "tab": "Backup log", "header": ["When", "What", "Result", "Size (KB)", "Details"], "rows": [[sheet_export.cell(v) for v in r] for r in log_rows], "first": True})
    _call({"action": "sheet_write", "tab": "Meta", "header": ["Last sync", datetime.now().strftime("%d %b %Y %H:%M")], "rows": [], "first": True})
    _call({"action": "dashboard"}, timeout=240)
    return {"tabs": len(tabs), "rows": total}


RESTORE_README = """# Belong Partner CRM: backup folder

Written by the CRM every time a backup runs. If your Mac fails, everything needed to run the CRM again is here.

## What is in this folder
- snapshots/   database copies (partner_crm-*.db.gz), newest last. Older than {keep} days are removed automatically.
- documents/   uploaded onboarding documents, one folder per partner ("<id> - <name>"), plus _restore_index.json
- code/        the CRM application (partner-crm-code-*.zip). It needs only Python 3, nothing else to install.
- manifest.json / status.json   used by the backup itself
- The Google Sheet next to this folder mirrors your partners, pipeline, onboarding, commercials, emails and tasks, with a Dashboard tab.
  It is read-only insurance: you can keep working from it while you set up a new machine.

## Restore on a new Mac
1. Install Python 3 (macOS already has it: type  python3 --version  in Terminal).
2. In Google Drive, right-click this folder > Download. Unzip it, e.g. to  ~/Downloads/crm-backup
3. Unzip code/partner-crm-code-<latest>.zip into your home folder. You get  ~/partner-crm
4. Run:  python3 ~/partner-crm/restore.py --backup ~/Downloads/crm-backup
   It restores the newest database and all documents into ~/partner-crm/data
5. Start the app:  cd ~/partner-crm && python3 server.py   then open http://127.0.0.1:8765
6. Passwords are NOT in the backup (on purpose). Re-enter the Gmail app password in Settings > Mailbox, and the other keys you use
   (Slack webhook, AppsFlyer link base, Youform relay, this Google backup URL).

Last backup written: {when}
"""


def write_status(c):
    cfg = load_cfg()
    counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("partners", "activities", "emails", "onboarding", "tasks")}
    _call({"action": "put_status", "status": {"last_backup_utc": utcnow(), "last_backup_local": now(), "counts": counts, "app_version": code_signature()[:8]}})
    put_text("README.md", RESTORE_README.format(keep=cfg["keep_days"], when=datetime.now().strftime("%d %b %Y %H:%M")), "text/markdown")


# ---------------- orchestration ----------------
def _log(kind, ok, detail, size_kb=None):
    try:
        with db.connect() as c:
            c.execute("INSERT INTO backup_runs(ts,kind,ok,detail,size_kb) VALUES(?,?,?,?,?)", (now(), kind, 1 if ok else 0, str(detail)[:300], size_kb))
            c.execute("DELETE FROM backup_runs WHERE id NOT IN (SELECT id FROM backup_runs ORDER BY id DESC LIMIT 300)")
    except Exception:  # noqa
        pass


def run(parts=("drive", "sheets")):
    """parts: 'drive' (snapshot, documents, code, README) and/or 'sheets'. Each part succeeds or fails on its own."""
    cfg = load_cfg()
    res = {}
    if not configured(cfg):
        return {"error": "Google backup isn't set up yet"}
    with _lock:
        if "drive" in parts:
            try:
                r = {"snapshot": upload_snapshot()}
                with db.connect() as c:
                    if cfg["include_documents"]:
                        r["documents"] = sync_documents(c)
                    if cfg["include_code"]:
                        r["code"] = upload_code()
                    s_ = upload_settings()
                    if s_:
                        r["settings"] = s_
                    write_status(c)
                    v = verify_drive(c, cfg["include_documents"])
                if not v["ok"]:
                    raise RelayError("Uploaded, but verification failed: " + "; ".join(v["problems"][:3]))
                r["verified"] = v["checked"]
                _call({"action": "prune", "keep_days": cfg["keep_days"], "folders": ["snapshots", "code"]})
                d = r.get("documents") or {}
                msg = f"OK: {r['snapshot']['file']} ({r['snapshot']['kb']} KB), verified in Drive" + (", settings included (encrypted)" if r.get("settings") else "") + (f", {d.get('uploaded', 0)} document(s) uploaded" if d else "") + (f", {d['skipped_large']} too large to upload" if d.get("skipped_large") else "")
                had_failures = cfg["failures"] > 0
                save_cfg({"last_drive_ok": now(), "last_drive_result": msg, "failures": 0})
                _log("drive", True, msg, r["snapshot"]["kb"])
                alerts.clear("drive-fail")
                if had_failures:
                    alerts.send("drive-recovered", "Google Drive backup is working again", "The backup to Google Drive succeeded after earlier failures:\n" + msg, throttle_hours=1)
                res["drive"] = {"ok": True, **r}
            except Exception as e:  # noqa
                msg = "FAILED: " + str(e)[:220]
                save_cfg({"last_drive_result": msg, "failures": load_cfg()["failures"] + 1})
                _log("drive", False, msg)
                res["drive"] = {"ok": False, "error": str(e)}
        if "drill" in parts:
            res["drill"] = run_drill()
        if "sheets" in parts:
            try:
                with db.connect() as c:
                    r = push_sheets(c)
                msg = f"OK: {r['rows']} rows across {r['tabs']} tabs"
                save_cfg({"last_sheets_ok": now(), "last_sheets_result": msg})
                _log("sheets", True, msg)
                res["sheets"] = {"ok": True, **r}
            except Exception as e:  # noqa
                msg = "FAILED: " + str(e)[:220]
                save_cfg({"last_sheets_result": msg})
                _log("sheets", False, msg)
                res["sheets"] = {"ok": False, "error": str(e)}
    _maybe_alert(res)
    return res


def _maybe_alert(res):
    cfg = load_cfg()
    d, sh = res.get("drive"), res.get("sheets")
    if isinstance(d, dict) and d.get("ok") is False:
        alerts.send("drive-fail", "the Google Drive backup failed", f"The backup to Google Drive failed ({cfg['failures']} time(s) in a row):\n{str(d.get('error'))[:300]}\n\nYour local backup on the Mac is unaffected.")
    if isinstance(sh, dict) and sh.get("ok") is False:
        alerts.send("sheets-fail", "the Google Sheet refresh failed", f"The Google Sheet mirror could not be refreshed:\n{str(sh.get('error'))[:300]}", throttle_hours=24)
    elif isinstance(sh, dict) and sh.get("ok"):
        alerts.clear("sheets-fail")


def date_str():
    return datetime.now().strftime("%Y-%m-%d")


def due_parts(cfg=None):
    cfg = cfg or load_cfg()
    if not (cfg["enabled"] and configured(cfg)):
        return []
    parts = []
    if not cfg["last_drive_ok"] or datetime.now() - datetime.fromisoformat(cfg["last_drive_ok"]) >= timedelta(hours=24):
        parts.append("drive")
    if not cfg["last_sheets_ok"] or datetime.now() - datetime.fromisoformat(cfg["last_sheets_ok"]) >= timedelta(hours=cfg["sheet_sync_hours"]):
        parts.append("sheets")
    return parts


def drill_due(cfg=None):
    cfg = cfg or load_cfg()
    if not (cfg["enabled"] and configured(cfg) and cfg["last_drive_ok"]):
        return False
    return not cfg["last_drill"] or datetime.now() - datetime.fromisoformat(cfg["last_drill"]) >= timedelta(days=30)


def maybe_run():
    parts = due_parts()
    if parts:
        if "drive" in parts and backup.latest_age_hours() > 20:
            backup.run("auto")          # make sure the snapshot being uploaded is fresh
        run(tuple(parts))
    if drill_due():
        run_drill(auto=True)


def start_job(parts=("drive", "sheets")):
    if JOB["state"] == "running":
        return JOB
    JOB.update(state="running", started=now(), parts=list(parts), result=None, error="")

    def work():
        try:
            if "drive" in parts:
                backup.run("manual")
            JOB["result"] = run(tuple(parts))
            JOB["state"] = "done"
        except Exception as e:  # noqa
            JOB["error"] = str(e)[:300]
            JOB["state"] = "error"
    threading.Thread(target=work, daemon=True).start()
    return JOB
