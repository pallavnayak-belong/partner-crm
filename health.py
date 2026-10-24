"""One place that answers 'are my backups healthy?' for the Backups page, the sidebar badge, alerts and the Google Sheet."""
import os
from datetime import datetime

import backup
import db


def age_hours(ts):
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def ago(ts):
    h = age_hours(ts)
    if h is None:
        return "never"
    if h < 1:
        return f"{max(1, int(h * 60))} min ago"
    if h < 48:
        return f"{int(h)}h ago"
    return f"{int(h / 24)} days ago"


def layers(c):
    import gdrive
    lc, gc = backup.load_cfg(), gdrive.load_cfg()

    def part(ok_ts, result, fresh_hours, enabled=True):
        if not enabled:
            return {"state": "off", "when": "", "detail": "Not set up"}
        age = age_hours(ok_ts)
        if (result or "").startswith("FAILED"):
            return {"state": "failed", "when": ok_ts, "detail": result}
        if age is None:
            return {"state": "stale", "when": "", "detail": "No backup yet"}
        return {"state": "ok" if age <= fresh_hours else "stale", "when": ok_ts, "detail": result}
    on = gdrive.configured(gc) and gc["enabled"]
    return {
        "local": part(lc["last_ok"], lc["last_result"], 36, lc["enabled"]),
        "drive": part(gc["last_drive_ok"], gc["last_drive_result"], 36, on),
        "sheets": part(gc["last_sheets_ok"], gc["last_sheets_result"], max(12, gc["sheet_sync_hours"] * 3), on),
    }


def overall(c, login_item=None):
    """{state: ok|warn|bad|local, label, detail} for the sidebar badge and banner."""
    import gdrive
    L = layers(c)
    names = {"local": "on this Mac", "drive": "to Google Drive", "sheets": "to the Google Sheet"}
    failed = [k for k, v in L.items() if v["state"] == "failed"]
    stale = [k for k, v in L.items() if v["state"] == "stale"]
    if failed:
        return {"state": "bad", "label": "Backup failed", "detail": "The backup " + " and ".join(names[k] for k in failed) + " failed. " + L[failed[0]]["detail"]}
    if stale:
        return {"state": "warn", "label": "Backup overdue", "detail": "The backup " + " and ".join(names[k] for k in stale) + " is out of date."}
    if L["drive"]["state"] == "off":
        return {"state": "local", "label": "Only on this Mac", "detail": "Your data has no copy outside this Mac. Connect Google on the Backups page."}
    if login_item is False:
        return {"state": "warn", "label": "Won't auto-start", "detail": "The CRM isn't set to start at login, so backups stop if the Mac restarts. Run: python3 ~/partner-crm/autostart.py install"}
    newest = max([v["when"] for v in L.values() if v["when"]] or [""])
    return {"state": "ok", "label": "Backed up " + ago(newest), "detail": "Everything is backed up, locally and on Google."}


def db_size_kb():
    try:
        return os.path.getsize(db.DB_PATH) // 1024
    except OSError:
        return 0


def sheet_rows(c):
    """(health rows, log rows) written to the Google Sheet tabs 'Backup health' and 'Backup log'."""
    import gdrive, vault
    gc, lc = gdrive.load_cfg(), backup.load_cfg()
    o = overall(c)
    summary = f"{o['label']}: {o['detail']}"
    health = [
        ["Overall", summary],
        ["Last backup on the Mac", lc["last_ok"].replace("T", " ")[:16] or "never"],
        ["Last backup to Drive", gc["last_drive_ok"].replace("T", " ")[:16] or "never"],
        ["Last Drive check (files really there)", (gc["last_verify"].replace("T", " ")[:16] + ": " + gc["last_verify_result"]) if gc["last_verify"] else "never"],
        ["Last restore test", (gc.get("last_drill", "").replace("T", " ")[:16] + ": " + gc.get("last_drill_result", "")) if gc.get("last_drill") else "never"],
        ["Last Sheet refresh", gc["last_sheets_ok"].replace("T", " ")[:16] or "never"],
        ["Database size (KB)", str(db_size_kb())],
        ["Partners in the CRM", str(c.execute("SELECT COUNT(*) FROM partners").fetchone()[0])],
        ["Backups kept on the Mac", str(len(backup.listing()))],
        ["Encrypted settings copy", "Yes" if vault.configured() else "No"],
    ]
    log = [[r["ts"].replace("T", " ")[:16], {"local": "This Mac", "drive": "Google Drive", "sheets": "Google Sheet", "drill": "Restore test", "restore": "Restore"}.get(r["kind"], r["kind"]),
            "OK" if r["ok"] else "FAILED", str(r["size_kb"] if r["size_kb"] is not None else ""), r["detail"]]
           for r in c.execute("SELECT ts,kind,ok,detail,size_kb FROM backup_runs ORDER BY id DESC LIMIT 150")]
    return health, log
