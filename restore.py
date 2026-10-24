#!/usr/bin/env python3
"""Restore the CRM from a downloaded copy of the Google Drive backup folder (or from a local backup folder).

    python3 restore.py --backup ~/Downloads/crm-backup            # newest snapshot + all documents
    python3 restore.py --backup ~/Downloads/crm-backup --snapshot partner_crm-20261018-020000-auto.db.gz
    python3 restore.py --backup ~/partner-crm-backups             # works on the local backup folder too

It never deletes anything: the current database (if any) is first moved to data/partner_crm.db.before-restore-<time>.
Passwords are not part of backups; re-enter them in Settings afterwards.
"""
import argparse
import getpass
import gzip
import json
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def newest_snapshot(folder):
    d = os.path.join(folder, "snapshots")
    if not os.path.isdir(d):
        sys.exit(f"No 'snapshots' folder inside {folder}. Point --backup at the folder you downloaded from Google Drive.")
    snaps = [f for f in os.listdir(d) if f.endswith((".db", ".db.gz"))]
    if not snaps:
        sys.exit("There are no database snapshots in that folder.")
    return os.path.join(d, max(snaps, key=lambda f: os.path.getmtime(os.path.join(d, f))))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", required=True, help="the backup folder (downloaded from Drive, or ~/partner-crm-backups)")
    ap.add_argument("--snapshot", help="a specific snapshot file name inside <backup>/snapshots (default: newest)")
    ap.add_argument("--app-dir", default=HERE, help="where the app lives (default: this folder)")
    ap.add_argument("--no-docs", action="store_true", help="restore the database only")
    ap.add_argument("--no-settings", action="store_true", help="don't restore the encrypted settings (Gmail app password etc.)")
    ap.add_argument("--passphrase-env", help="name of an environment variable holding the settings passphrase (default: ask)")
    a = ap.parse_args()
    folder, data = os.path.expanduser(a.backup), os.path.join(os.path.expanduser(a.app_dir), "data")
    snap = os.path.join(folder, "snapshots", a.snapshot) if a.snapshot else newest_snapshot(folder)
    if not os.path.isfile(snap):
        sys.exit(f"Snapshot not found: {snap}")
    os.makedirs(data, exist_ok=True)
    target = os.path.join(data, "partner_crm.db")
    if os.path.exists(target):
        keep = f"{target}.before-restore-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(target, keep)
        print("Existing database kept at", keep)
    for ext in ("-wal", "-shm"):
        if os.path.exists(target + ext):
            os.remove(target + ext)
    tmp = target + ".incoming"
    with (gzip.open(snap, "rb") if snap.endswith(".gz") else open(snap, "rb")) as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    chk = sqlite3.connect(tmp)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        n = chk.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        os.remove(tmp)
        sys.exit(f"That snapshot is damaged ({ok}). Try an older one with --snapshot.")
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    print(f"Database restored from {os.path.basename(snap)}: {n} partners.")

    restored = missing = 0
    if not a.no_docs:
        idx_path = os.path.join(folder, "documents", "_restore_index.json")
        if os.path.exists(idx_path):
            for item in json.load(open(idx_path)):
                src = os.path.join(folder, item["remote_path"])
                if not os.path.isfile(src):
                    missing += 1
                    continue
                if item["kind"] == "onboarding":
                    dst_dir = os.path.join(data, "onboarding", str(item["partner_id"]))
                else:
                    dst_dir = os.path.join(data, "attachments", str(item["template_id"]))
                os.makedirs(dst_dir, mode=0o700, exist_ok=True)
                shutil.copy2(src, os.path.join(dst_dir, item["stored_name"]))
                restored += 1
            print(f"Documents restored: {restored}" + (f" ({missing} listed but not found in the folder)" if missing else ""))
        else:
            # a local backup folder keeps documents as an exact mirror
            for kind in ("onboarding", "attachments"):
                src_root = os.path.join(folder, "files", kind)
                if os.path.isdir(src_root):
                    shutil.copytree(src_root, os.path.join(data, kind), dirs_exist_ok=True)
                    restored += 1
            print("Documents restored from the local mirror." if restored else "No documents found to restore.")
    enc = os.path.join(folder, "secrets", "settings.enc")
    settings_done = False
    if not a.no_settings and os.path.isfile(enc):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import vault
        blob = open(enc, "rb").read()
        for attempt in range(3):
            pw = os.environ.get(a.passphrase_env, "") if a.passphrase_env else getpass.getpass("Passphrase for the encrypted settings (Enter to skip): ")
            if not pw:
                break
            try:
                names = vault.restore_into(data, blob, pw)
                print("Settings restored:", ", ".join(names))
                settings_done = True
                break
            except ValueError as e:
                print(" ", e)
                if a.passphrase_env:
                    break
    print("\nNext: start the app with   python3 server.py   and open http://127.0.0.1:8765")
    if settings_done:
        print("Your Gmail app password, Slack webhook and Google backup connection came back with the settings. Nothing to re-enter.")
    else:
        print("Then re-enter passwords in Settings (Gmail app password, Slack webhook, AppsFlyer link base, Youform relay, Google backup URL).")


if __name__ == "__main__":
    main()
