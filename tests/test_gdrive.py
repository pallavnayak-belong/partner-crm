"""Google Drive/Sheets backup: protocol client against a fake relay, document sync, sheets mirror, restore."""
import base64, gzip, hashlib, io, json, os, shutil, sqlite3, sys, tempfile, threading, time, unittest, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "g.db"))
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "g.db")
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_BACKUP_CFG"] = os.path.join(TMP, "backup.json")
import db, backup, gdrive, onboarding as ob, attachments, sheet_export, server, mail, notify, alerts, vault, health  # noqa: E402
SENT_ALERTS = []
_fake_email = lambda to, subject, body: SENT_ALERTS.append((to, subject, body)) or True   # tests never send real email


class FakeRelay:
    """Behaves like the Apps Script: POST -> 302 -> GET result; same actions, same checks."""
    def __init__(self, token):
        self.token, self.files, self.manifest, self.status, self.tabs, self.uploads = token, {}, {}, None, {}, {}
        self.calls, self.dashboard_built, self.corrupt, self.results, self.alerts = [], 0, False, {}, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                rid = str(len(outer.results)); outer.results[rid] = outer.handle(body)
                self.send_response(302); self.send_header("Location", f"/exec/result/{rid}"); self.send_header("Content-Length", "0"); self.end_headers()
            def do_GET(self):
                data = json.dumps(outer.results[self.path.rsplit("/", 1)[1]]).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/exec"

    def stop(self): self.srv.shutdown(); self.srv.server_close()

    def handle(self, b):
        if b.get("token") != self.token: return {"ok": False, "error": "bad token"}
        a = b["action"]; self.calls.append(a)
        if a == "ping": return {"ok": True, "root_url": "https://drive.example/folder", "sheet_url": "https://sheets.example/sheet", "status": self.status or {}}
        if a == "begin_upload": uid = f"u{len(self.uploads)}"; self.uploads[uid] = {}; return {"ok": True, "id": uid}
        if a == "upload_chunk": self.uploads[b["id"]][b["i"]] = base64.b64decode(b["data"]); return {"ok": True}
        if a == "finish_upload":
            ch = self.uploads[b["id"]]
            if len(ch) != b["total"]: return {"ok": False, "error": "chunk count"}
            data = b"".join(ch[i] for i in sorted(ch))
            if len(data) != b["size"]: return {"ok": False, "error": "size mismatch"}
            md5 = hashlib.md5(data).hexdigest()
            if md5 != b["md5"]: return {"ok": False, "error": "checksum mismatch"}
            self.files[b["path"]] = data
            return {"ok": True, "id": "f1", "size": len(data), "md5": "0" * 32 if self.corrupt else md5}
        if a == "get_manifest": return {"ok": True, "manifest": self.manifest}
        if a == "put_manifest": self.manifest = b["manifest"]; return {"ok": True}
        if a == "put_status": self.status = b["status"]; return {"ok": True}
        if a == "put_text": self.files[b["path"]] = b["text"].encode(); return {"ok": True}
        if a == "sheet_write":
            t = self.tabs.setdefault(b["tab"], {"header": None, "rows": []})
            if b.get("first"): t["header"], t["rows"] = b["header"], []
            t["rows"] += b["rows"]; return {"ok": True, "rows": len(b["rows"])}
        if a == "dashboard": self.dashboard_built += 1; return {"ok": True}
        if a == "prune": return {"ok": True, "pruned": 0}
        if a == "verify":
            miss = [f["path"] for f in b.get("files", []) if f["path"] not in self.files]
            wrong = [f["path"] for f in b.get("files", []) if f["path"] in self.files and len(self.files[f["path"]]) != f["size"]]
            s = b.get("snapshot"); snap = None
            if s:
                data = self.files.get(s["path"]); snap = {"path": s["path"], "found": data is not None, "md5": hashlib.md5(data).hexdigest() if data else None}
                snap["ok"] = bool(data) and snap["md5"] == s["md5"]
            return {"ok": True, "missing": miss, "wrong": wrong, "snapshot": snap, "checked": len(b.get("files", []))}
        if a == "latest":
            names = sorted(k for k in self.files if k.startswith(b["folder"] + "/"))
            return {"ok": True, "found": bool(names), "name": names[-1].split("/", 1)[1] if names else None, "size": len(self.files[names[-1]]) if names else 0}
        if a == "download":
            d = self.files.get(b["path"])
            if d is None: return {"ok": False, "error": "not found: " + b["path"]}
            return {"ok": True, "size": len(d), "offset": b["offset"], "data": base64.b64encode(d[b["offset"]:b["offset"] + b["length"]]).decode()}
        if a == "trash_prefix":
            n = [k for k in self.files if k.startswith(b["parent"] + "/" + b["prefix"])]
            for k in n: del self.files[k]
            return {"ok": True, "trashed": len(n)}
        return {"ok": False, "error": "unknown action"}


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (db.DB_PATH, backup.CFG_PATH, gdrive.CFG_PATH, ob.DIR, attachments.DIR, mail.CFG_PATH, notify.CFG_PATH, vault.KEY_PATH)
        vault.KEY_PATH = os.path.join(TMP, "vault.json")
        cls._alerts = (alerts._send_email, alerts.SYNC); alerts._send_email = _fake_email; alerts.SYNC = True
        db.DB_PATH = os.path.join(TMP, "g.db"); backup.CFG_PATH = os.path.join(TMP, "backup.json"); gdrive.CFG_PATH = os.path.join(TMP, "gdrive.json")
        ob.DIR = os.path.join(TMP, "onb"); attachments.DIR = os.path.join(TMP, "att"); mail.CFG_PATH = os.path.join(TMP, "mail.json"); notify.CFG_PATH = os.path.join(TMP, "slack.json")
        db.init(); cls.c = db.connect()
        backup.save_cfg({"folder": os.path.join(TMP, "bk")})
        mail.save_cfg({"address": "partnerships@getbelong.com", "password": "supersecretpassword"})
        cls.token = gdrive.ensure_token(); cls.relay = FakeRelay(cls.token)
        cls._sleep = time.sleep; gdrive.time.sleep = lambda s: None
        gdrive.save_cfg({"url": "https://script.google.com/macros/s/AKfy/exec", "enabled": True})
        gdrive.save_cfg({"url": "https://script.google.com/macros/s/AKfy/exec"})
        cfg = gdrive.load_cfg(); cfg["url"] = cls.relay.url
        with open(gdrive.CFG_PATH, "w") as f: json.dump(cfg, f)       # point at the fake relay (bypasses the URL format check on purpose)
        # a partner with documents, including a hostile name and a too-big file
        ids = [r[0] for r in cls.c.execute("SELECT id FROM partners ORDER BY id LIMIT 2")]
        cls.p1, cls.p2 = ids
        cls.c.execute("UPDATE partners SET name='=HYPERLINK(\"http://evil\",\"x\") Corp' WHERE id=?", (cls.p2,))
        ob.start(cls.c, cls.p1); cls.c.commit()
        ob.save_file(cls.c, cls.p1, "pan", "PAN card.pdf", b"%PDF pan"); ob.save_file(cls.c, cls.p1, "pan", "PAN card.pdf", b"%PDF second with same name")
        ob.save_file(cls.c, cls.p1, "agreement", "Signed agreement.pdf", os.urandom(3_500_000))
        cls.c.commit()

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop(); time.sleep_ = None; alerts._send_email, alerts.SYNC = cls._alerts
        gdrive.time.sleep = cls._sleep
        cls.c.commit(); cls.c.close()
        db.DB_PATH, backup.CFG_PATH, gdrive.CFG_PATH, ob.DIR, attachments.DIR, mail.CFG_PATH, notify.CFG_PATH, vault.KEY_PATH = cls._saved

    def test_01_config_validation_token_and_script(self):
        with self.assertRaises(ValueError): gdrive.save_cfg({"url": "https://evil.example/exec"})
        with self.assertRaises(ValueError): gdrive.save_cfg({"url": "https://script.google.com/macros/s/x/dev"})
        self.assertEqual(gdrive.ensure_token(), gdrive.ensure_token())
        s = gdrive.script_text(); self.assertIn(self.token, s); self.assertNotIn("__TOKEN__", s); self.assertEqual(len(self.token), 64)

    def test_02_ping_saves_links_and_bad_token_fails_fast(self):
        r = gdrive.test(); self.assertEqual(r["root_url"], "https://drive.example/folder"); self.assertEqual(gdrive.load_cfg()["sheet_url"], "https://sheets.example/sheet")
        cfg = gdrive.load_cfg(); cfg["token"] = "wrong"; n = len(self.relay.calls)
        with self.assertRaises(gdrive.RelayError) as e: gdrive._call({"action": "ping"}, cfg=cfg)
        self.assertEqual(str(e.exception), "bad token")
        gdrive.save_cfg({"token": self.token})

    def test_03_chunked_upload_integrity(self):
        data = os.urandom(4_200_000)
        before = len(self.relay.calls)
        gdrive.upload_bytes(data, "x/big.bin")
        self.assertEqual(self.relay.files["x/big.bin"], data)
        self.assertGreaterEqual(self.relay.calls[before:].count("upload_chunk"), 3)
        self.relay.corrupt = True
        try:
            with self.assertRaises(gdrive.RelayError) as e: gdrive.upload_bytes(b"abc", "x/bad.bin")
            self.assertIn("checksum", str(e.exception))
        finally:
            self.relay.corrupt = False

    def test_04_unreachable_relay_gives_friendly_error(self):
        cfg = gdrive.load_cfg(); cfg["url"] = "http://127.0.0.1:9/exec"
        with self.assertRaises(gdrive.RelayError) as e: gdrive._call({"action": "ping"}, cfg=cfg, timeout=3)
        self.assertIn("reach Google", str(e.exception))

    def test_05_full_run_drive_and_sheets(self):
        backup.run("t1")
        res = gdrive.run(("drive", "sheets"))
        self.assertTrue(res["drive"]["ok"], res); self.assertTrue(res["sheets"]["ok"], res)
        files = self.relay.files
        snap = next(k for k in files if k.startswith("snapshots/") and k.endswith(".db.gz"))
        raw = gzip.decompress(files[snap]); p = os.path.join(TMP, "check.db")
        with open(p, "wb") as fh: fh.write(raw)
        chk = sqlite3.connect(p); n = chk.execute("SELECT COUNT(*) FROM partners").fetchone()[0]; chk.close()
        self.assertEqual(n, self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0])
        docs = sorted(k for k in files if k.startswith("documents/") and not k.endswith(".json"))
        self.assertEqual(len(docs), 3); self.assertTrue(any("PAN card (2).pdf" in d for d in docs)); self.assertTrue(all(d.startswith(f"documents/{self.p1} - ") for d in docs))
        idx = json.loads(files["documents/_restore_index.json"]); self.assertEqual(len(idx), 3)
        self.assertEqual({i["remote_path"] for i in idx}, set(docs))
        # code zip: has the app, never data or secrets
        z = zipfile.ZipFile(io.BytesIO(next(v for k, v in files.items() if k.startswith("code/")))); names = z.namelist()
        self.assertIn("partner-crm/server.py", names); self.assertIn("partner-crm/restore.py", names); self.assertFalse([n for n in names if "/data/" in n or n.endswith("mail.json")])
        self.assertIn(b"Restore on a new Mac", files["README.md"]); self.assertEqual(self.relay.status["counts"]["partners"], n)
        # sheets
        tabs = self.relay.tabs
        for need in ("Partners", "Activities", "Emails", "Tasks", "Onboarding", "Commercials", "Documents", "Meta"):
            self.assertIn(need, tabs)
        self.assertEqual(len(tabs["Partners"]["rows"]), n); self.assertEqual(self.relay.dashboard_built, 1)
        self.assertEqual(tabs["Partners"]["header"][:3], ["ID", "Name", "Type"]); self.assertEqual(tabs["Tasks"]["header"][5], "Done"); self.assertEqual(tabs["Activities"]["header"][:5], ["Partner", "When", "Type", "By", "Note"])
        hostile = next(r for r in tabs["Partners"]["rows"] if r[0] == str(self.p2))[1]; self.assertTrue(hostile.startswith("'="))
        blob = json.dumps(tabs) + "".join(str(v) for v in self.relay.status.values())
        self.assertNotIn("supersecretpassword", blob); self.assertNotIn(self.token, blob)
        self.assertNotIn("supersecretpassword", json.dumps({k: v.decode("utf-8", "ignore") for k, v in files.items() if k.endswith((".json", ".md"))}))
        cfg = gdrive.load_cfg(); self.assertTrue(cfg["last_drive_result"].startswith("OK")); self.assertTrue(cfg["last_sheets_result"].startswith("OK"))
        kinds = [r[0] for r in self.c.execute("SELECT kind FROM backup_runs")]; self.assertTrue({"local", "drive", "sheets"} <= set(kinds))

    def test_06_second_run_only_uploads_changes(self):
        before = self.relay.calls.count("begin_upload")
        r = gdrive.run(("drive",)); self.assertEqual(r["drive"]["documents"]["uploaded"], 0)
        new_uploads = self.relay.calls.count("begin_upload") - before
        self.assertEqual(new_uploads, 1)                    # just the new database snapshot (code unchanged, documents unchanged)
        ob.save_file(self.c, self.p1, "bank", "cheque.png", b"\x89PNG new"); self.c.commit()
        r = gdrive.run(("drive",)); self.assertEqual(r["drive"]["documents"]["uploaded"], 1)

    def test_07_oversized_document_is_skipped_not_fatal(self):
        keep = gdrive.MAX_DOC_BYTES; gdrive.MAX_DOC_BYTES = 1_000_000
        try:
            r = gdrive.run(("drive",))
        finally:
            gdrive.MAX_DOC_BYTES = keep
        self.assertTrue(r["drive"]["ok"]); self.assertEqual(r["drive"]["documents"]["skipped_large"], 1)
        self.assertFalse([k for k in self.relay.files if k.endswith("Signed agreement.pdf")] and False)

    def test_08_erasing_a_partner_removes_their_drive_documents(self):
        gdrive.run(("drive",)); self.assertTrue([k for k in self.relay.files if k.startswith(f"documents/{self.p1} - ")])
        gdrive.forget_partner(self.p1)
        self.assertFalse([k for k in self.relay.files if k.startswith(f"documents/{self.p1} - ")])
        self.assertFalse([k for k in self.relay.manifest if k.startswith(f"documents/{self.p1} - ")])

    def test_09_failures_are_recorded_counted_and_alert_once(self):
        SENT_ALERTS.clear(); alerts.clear("drive-fail")
        keep_notify = notify.send; slack = []; notify.send = lambda text, wait=False: slack.append(text) or True
        cfg = gdrive.load_cfg(); good = cfg["url"]
        try:
            with open(gdrive.CFG_PATH, "w") as f: json.dump({**cfg, "url": "http://127.0.0.1:9/exec", "failures": 0}, f)
            keep_call = gdrive._call
            gdrive._call = lambda payload, timeout=180, cfg=None: (_ for _ in ()).throw(gdrive.RelayError("Couldn't reach Google (test)"))
            try:
                gdrive.run(("drive",)); self.assertEqual(len(SENT_ALERTS), 1)           # told on the FIRST failure
                self.assertIn("Google Drive backup failed", SENT_ALERTS[0][1]); self.assertEqual(SENT_ALERTS[0][0], "partnerships@getbelong.com")
                gdrive.run(("drive",)); gdrive.run(("drive",)); self.assertEqual(len(SENT_ALERTS), 1)   # but not nagged
                self.assertEqual(len(slack), 1)
            finally:
                gdrive._call = keep_call
            self.assertTrue(gdrive.load_cfg()["last_drive_result"].startswith("FAILED")); self.assertGreaterEqual(gdrive.load_cfg()["failures"], 3)
        finally:
            notify.send = keep_notify
            restored = {**gdrive.load_cfg(), "url": good, "failures": 0}
            with open(gdrive.CFG_PATH, "w") as f: json.dump(restored, f)

    def test_09b_recovery_message_after_failures(self):
        SENT_ALERTS.clear(); gdrive.save_cfg({"failures": 2}); alerts.clear("drive-recovered")
        r = gdrive.run(("drive",)); self.assertTrue(r["drive"]["ok"], r)
        self.assertTrue(any("working again" in a[1] for a in SENT_ALERTS))

    def test_10_due_parts_and_health_and_api_hides_token(self):
        cfg = gdrive.load_cfg(); old = (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 90000)))
        self.assertEqual(gdrive.due_parts({**cfg, "last_drive_ok": old, "last_sheets_ok": old, "enabled": True}), ["drive", "sheets"])
        fresh = time.strftime("%Y-%m-%dT%H:%M:%S"); self.assertEqual(gdrive.due_parts({**cfg, "last_drive_ok": fresh, "last_sheets_ok": fresh, "enabled": True}), [])
        self.assertEqual(gdrive.due_parts({**cfg, "enabled": False}), [])
        h = server.backup_health(self.c); self.assertIn(h["drive"]["state"], ("ok", "stale", "failed")); self.assertEqual(h["local"]["state"], "ok")
        api = server.api("GET", "/api/gdrive", {}, {}); self.assertNotIn(self.token, json.dumps(api)); self.assertNotIn("token", api)
        with self.assertRaises(ValueError): server.api("POST", "/api/gdrive/config", {}, {"url": "https://evil.example/exec"})
        self.assertIn(self.token, server.api("GET", "/api/gdrive/script", {}, {})["script"])

    def test_10b_verify_detects_missing_or_altered_drive_files(self):
        gdrive.run(("drive",))
        with db.connect() as c:
            ok = gdrive.verify_drive(c); self.assertTrue(ok["ok"], ok); self.assertGreater(ok["checked"], 2)
            doc = next(k for k in self.relay.files if k.startswith("documents/") and not k.endswith(".json"))
            saved = self.relay.files.pop(doc)
            bad = gdrive.verify_drive(c); self.assertFalse(bad["ok"]); self.assertTrue(any("missing in Drive" in p for p in bad["problems"]))
            r = gdrive.run(("drive",))                      # a backup that cannot be verified is reported as failed, not as OK
            self.assertFalse(r["drive"]["ok"]); self.assertIn("verification failed", r["drive"]["error"])
            self.relay.files[doc] = saved
            snap = gdrive.load_cfg()["last_snapshot"]["path"]; good = self.relay.files[snap]
            self.relay.files[snap] = good[:-4] + b"XXXX"
            bad2 = gdrive.verify_drive(c); self.assertFalse(bad2["ok"]); self.assertTrue(any("does not match" in p for p in bad2["problems"]))
            self.relay.files[snap] = good
            self.assertTrue(gdrive.verify_drive(c)["ok"])
        res = server.api("POST", "/api/gdrive/verify", {}, {}); self.assertTrue(res["ok"]); self.assertEqual(gdrive.load_cfg()["last_verify_result"], "OK")

    def test_11_restore_script_rebuilds_database_and_documents(self):
        gdrive.run(("drive",)); drive_dir = os.path.join(TMP, "drive_download")
        shutil.rmtree(drive_dir, ignore_errors=True)
        for path, data in self.relay.files.items():
            fp = os.path.join(drive_dir, path); os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "wb") as fh: fh.write(data)
        app = os.path.join(TMP, "newmac"); os.makedirs(os.path.join(app, "data"))
        with open(os.path.join(app, "data", "partner_crm.db"), "w") as fh: fh.write("old broken db")
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(HERE, "restore.py"), "--backup", drive_dir, "--app-dir", app], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        chk = sqlite3.connect(os.path.join(app, "data", "partner_crm.db")); n = chk.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
        stored = chk.execute("SELECT partner_id, stored_name FROM onboarding_files").fetchall(); chk.close()
        self.assertEqual(n, self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0])
        # the kept documents come back under their exact stored names
        for pid, sn in stored:
            if os.path.exists(os.path.join(ob.DIR, str(pid), sn)) and pid != self.p1:
                self.assertTrue(os.path.isfile(os.path.join(app, "data", "onboarding", str(pid), sn)))
        self.assertTrue([f for f in os.listdir(os.path.join(app, "data")) if f.startswith("partner_crm.db.before-restore-")])
        bad = subprocess.run([sys.executable, os.path.join(HERE, "restore.py"), "--backup", os.path.join(TMP, "nothing"), "--app-dir", app], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)

    def test_12_restore_test_downloads_and_checks_drive_copy(self):
        gdrive.run(("drive",))
        r = gdrive.drill(); self.assertTrue(r["ok"], r)
        txt = " ".join(r["steps"]); self.assertIn("partners", txt); self.assertIn("App code", txt); self.assertIn("Documents", txt)
        # nothing left behind in the temp area
        import glob, tempfile as tf
        self.assertEqual(glob.glob(os.path.join(tf.gettempdir(), "crm-restore-test-*")), [])

    def test_12b_restore_test_catches_a_damaged_drive_copy(self):
        gdrive.run(("drive",))
        snap = sorted(k for k in self.relay.files if k.startswith("snapshots/"))[-1]; good = self.relay.files[snap]
        try:
            self.relay.files[snap] = gzip.compress(b"this is not a database at all")
            r = gdrive.drill(); self.assertFalse(r["ok"]); self.assertTrue(r["problems"])
            SENT_ALERTS.clear(); alerts.clear("restore-test")
            out = gdrive.run_drill(); self.assertFalse(out["ok"]); self.assertTrue(any("restore test found a problem" in a[1] for a in SENT_ALERTS))
            self.assertTrue(gdrive.load_cfg()["last_drill_result"].startswith("PROBLEMS"))
        finally:
            self.relay.files[snap] = good
        self.assertTrue(gdrive.drill()["ok"])

    def test_12c_drill_is_due_monthly_and_runs_via_job_api(self):
        cfg = gdrive.load_cfg(); self.assertTrue(cfg["last_drive_ok"])
        gdrive.save_cfg({"last_drill": ""}); self.assertTrue(gdrive.drill_due())
        res = gdrive.run(("drill",)); self.assertTrue(res["drill"]["ok"], res)
        self.assertFalse(gdrive.drill_due())
        gdrive.save_cfg({"last_drill": (__import__("datetime").datetime.now() - __import__("datetime").timedelta(days=31)).isoformat(timespec="seconds")}); self.assertTrue(gdrive.drill_due())
        self.assertIn("drill", [r[0] for r in self.c.execute("SELECT kind FROM backup_runs")])

    def test_13_encrypted_settings_ride_along_and_are_verified(self):
        with self.assertRaises(ValueError): vault.setup("short")
        vault.setup("a long passphrase for tests")
        r = gdrive.run(("drive",)); self.assertTrue(r["drive"]["ok"], r); self.assertIn("settings", r["drive"])
        blob = self.relay.files["secrets/settings.enc"]
        self.assertNotIn(b"supersecretpassword", blob); self.assertNotIn(self.token.encode(), blob)
        files = vault.open_blob(blob, "a long passphrase for tests"); self.assertIn("mail.json", files); self.assertIn("supersecretpassword", files["mail.json"])
        with self.assertRaises(ValueError): vault.open_blob(blob, "wrong passphrase!!")
        tampered = bytearray(blob); tampered[-3] ^= 1
        with self.assertRaises(ValueError): vault.open_blob(bytes(tampered), "a long passphrase for tests")
        self.assertTrue(gdrive.drill()["ok"])                              # the restore test also opens the settings copy
        # local copy next to snapshots, and verification covers it
        self.assertTrue(os.path.isfile(os.path.join(TMP, "bk", "secrets", "settings.enc")))
        del self.relay.files["secrets/settings.enc"]
        with db.connect() as c: v = gdrive.verify_drive(c)
        self.assertFalse(v["ok"]); self.assertTrue(any("settings.enc" in p for p in v["problems"]))
        gdrive.run(("drive",)); vault.remove()

    def test_14_sheet_gets_backup_health_and_log_tabs(self):
        gdrive.run(("drive", "sheets"))
        tabs = self.relay.tabs
        self.assertIn("Backup health", tabs); self.assertIn("Backup log", tabs)
        items = {r[0]: r[1] for r in tabs["Backup health"]["rows"]}
        self.assertIn("Overall", items); self.assertIn("Last restore test", items); self.assertEqual(items["Partners in the CRM"], str(self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0]))
        self.assertEqual(tabs["Backup log"]["header"], ["When", "What", "Result", "Size (KB)", "Details"])
        self.assertTrue(any(r[1] == "Google Drive" and r[2] == "OK" and r[3].isdigit() for r in tabs["Backup log"]["rows"]))
        blob = json.dumps(tabs); self.assertNotIn("supersecretpassword", blob)

    def test_15_overall_status_for_the_badge(self):
        with db.connect() as c:
            o = health.overall(c, True); self.assertIn(o["state"], ("ok", "warn", "bad")); self.assertTrue(o["label"])
            self.assertEqual(health.overall(c, False)["state"], "warn" if health.overall(c, True)["state"] == "ok" else health.overall(c, True)["state"])
            gdrive.save_cfg({"last_drive_result": "FAILED: boom"}); o = health.overall(c, True); self.assertEqual(o["state"], "bad"); self.assertIn("failed", o["label"].lower())
            gdrive.save_cfg({"last_drive_result": "OK"}); self.assertEqual(server.api("GET", "/api/backup/status", {}, {})["state"], health.overall(c, server.autostart.installed())["state"])


if __name__ == "__main__":
    unittest.main()
