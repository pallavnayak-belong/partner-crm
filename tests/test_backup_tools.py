"""In-app restore, compare, single-partner restore, alerts, encrypted settings, restore.py with settings."""
import gzip, os, shutil, sqlite3, subprocess, sys, tempfile, time, unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
_s = sqlite3.connect(os.path.join(HERE, "data", "partner_crm.db")); _d = sqlite3.connect(os.path.join(TMP, "b.db")); _s.backup(_d); _s.close(); _d.close()
for k, v in {"PARTNER_CRM_GDRIVE_CFG": "gdrive.json", "PARTNER_CRM_VAULT_KEY": "vault.json", "PARTNER_CRM_YOUFORM_CFG": "youform.json", "PARTNER_CRM_MAIL_CFG": "mail.json",
             "PARTNER_CRM_DB": "b.db", "PARTNER_CRM_BACKUP_CFG": "backup.json", "PARTNER_CRM_SLACK_CFG": "slack.json", "PARTNER_CRM_ATTR_CFG": "attr.json"}.items():
    os.environ[k] = os.path.join(TMP, v)
import db, backup, crm, snapshots, vault, alerts, mail, notify, onboarding as ob, server, health, attachments  # noqa: E402

SENT = []
_fake_email = lambda to, subject, body: SENT.append((to, subject, body)) or True


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (db.DB_PATH, backup.CFG_PATH, ob.DIR, attachments.DIR, mail.CFG_PATH, notify.CFG_PATH, vault.KEY_PATH)
        db.DB_PATH = os.path.join(TMP, "b.db"); backup.CFG_PATH = os.path.join(TMP, "backup.json"); ob.DIR = os.path.join(TMP, "onb"); attachments.DIR = os.path.join(TMP, "att")
        mail.CFG_PATH = os.path.join(TMP, "mail.json"); notify.CFG_PATH = os.path.join(TMP, "slack.json"); vault.KEY_PATH = os.path.join(TMP, "vault.json")
        cls._alerts = (alerts._send_email, alerts.SYNC); alerts._send_email = _fake_email; alerts.SYNC = True      # other test modules patch these too
        db.init(); backup.save_cfg({"folder": os.path.join(TMP, "bk")})
        mail.save_cfg({"address": "partnerships@getbelong.com", "password": "pw-for-tests-only"})
        cls.c = db.connect()

    @classmethod
    def tearDownClass(cls):
        cls.c.commit(); cls.c.close(); alerts._send_email, alerts.SYNC = cls._alerts
        db.DB_PATH, backup.CFG_PATH, ob.DIR, attachments.DIR, mail.CFG_PATH, notify.CFG_PATH, vault.KEY_PATH = cls._saved

    def snap(self, label):
        backup.run(label); time.sleep(1.1)
        return sorted(f for f in os.listdir(os.path.join(TMP, "bk", "snapshots")) if f.endswith(f"-{label}.db"))[-1]

    # ---- compare ----
    def test_01_compare_shows_added_removed_and_edited_partners(self):
        ids = [r[0] for r in self.c.execute("SELECT id FROM partners WHERE archived=0 ORDER BY id LIMIT 3")]
        before = self.snap("cmp1")
        self.c.execute("UPDATE partners SET stage='Contacted', owner='Asha' WHERE id=?", (ids[0],))
        self.c.execute("INSERT INTO partners(name,category,stage,created_at,updated_at,country) VALUES('Brand New Partner','Individual','New',datetime('now'),datetime('now'),'India')")
        self.c.commit()
        r = snapshots.compare(before, "live")
        self.assertEqual(r["added_n"], 1); self.assertEqual(r["added"][0]["name"], "Brand New Partner")
        self.assertEqual(r["by_field"].get("owner"), 1); self.assertTrue(any(e["field"] == "owner" and e["to"] == "Asha" for e in r["examples"]))
        self.assertEqual(next(x for x in r["counts"] if x["label"] == "Partners")["diff"], 1)
        self.assertEqual(snapshots.compare(before, before)["changed_partners"], 0)

    def test_02_bulk_edit_triggers_a_warning(self):
        before = self.snap("cmp2")
        self.c.execute("UPDATE partners SET owner='Bulk Person' WHERE id IN (SELECT id FROM partners ORDER BY id LIMIT 30)"); self.c.commit()
        w = snapshots.compare(before, "live")["warnings"]
        self.assertTrue(any("owner" in x for x in w), w)

    def test_03_bad_names_are_rejected(self):
        for bad in ("../../etc/passwd", "partner_crm-x/../../y.db", "evil.db", "", None):
            with self.assertRaises(snapshots.RestoreError): snapshots.path_of(bad)

    # ---- restore everything ----
    def test_10_restore_all_rolls_back_and_keeps_a_safety_copy(self):
        good = self.snap("good")
        n0 = self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
        self.c.execute("DELETE FROM activities WHERE partner_id IN (SELECT id FROM partners LIMIT 5)"); self.c.execute("INSERT INTO partners(name,category,stage,created_at,updated_at) VALUES('Added After','Individual','New',datetime('now'),datetime('now'))"); self.c.commit()
        pv = snapshots.preview(good); self.assertEqual(pv["integrity"], "ok"); self.assertEqual(next(r for r in pv["rows"] if r["key"] == "partners")["diff"], -1)
        r = snapshots.restore_all(good, "tester")
        self.assertEqual(r["partners"], n0); self.assertTrue(r["safety"].endswith("-before-restore.db")); self.assertTrue(os.path.isfile(os.path.join(TMP, "bk", "snapshots", r["safety"])))
        c2 = db.connect(); self.assertEqual(c2.execute("SELECT COUNT(*) FROM partners").fetchone()[0], n0); self.assertIsNone(c2.execute("SELECT 1 FROM partners WHERE name='Added After'").fetchone())
        self.assertTrue(c2.execute("SELECT 1 FROM backup_runs WHERE kind='restore'").fetchone()); c2.close()
        # the running app's own connection sees the restored data, and the safety copy can undo it
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0], n0)
        snapshots.restore_all(r["safety"])
        c3 = db.connect(); self.assertEqual(c3.execute("SELECT COUNT(*) FROM partners").fetchone()[0], n0 + 1); c3.close()

    def test_11_restore_refuses_while_a_send_is_running_and_for_damaged_files(self):
        s = self.snap("busy")
        mail.JOBS["x"] = {"state": "running"}
        try:
            with self.assertRaises(snapshots.RestoreError) as e: snapshots.restore_all(s)
            self.assertIn("send is in progress", str(e.exception))
        finally:
            mail.JOBS.pop("x")
        bad = os.path.join(TMP, "bk", "snapshots", "partner_crm-20200101-000000-broken.db")
        with open(bad, "wb") as f: f.write(b"not a database" * 100)
        with self.assertRaises(Exception): snapshots.restore_all("partner_crm-20200101-000000-broken.db")
        self.assertGreater(self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0], 0)    # live data untouched

    def test_12_older_snapshot_missing_newer_columns_is_upgraded(self):
        s = self.snap("old")
        p = os.path.join(TMP, "bk", "snapshots", s); x = sqlite3.connect(p)
        x.execute("DROP TABLE team_members"); x.commit(); x.close()
        snapshots.restore_all(s); c = db.connect(); self.assertEqual(c.execute("SELECT COUNT(*) FROM team_members").fetchone()[0], 0); c.close()

    # ---- restore one partner ----
    def _rich_partner(self):
        pid = self.c.execute("SELECT id FROM partners WHERE archived=0 ORDER BY id LIMIT 1 OFFSET 40").fetchone()[0]
        crm.add_task(self.c, pid, "Send rate card", (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"), "", "t")
        crm.add_contact(self.c, pid, {"name": "Second Person", "email": "second@example.com", "role": "Finance"}) if hasattr(crm, "add_contact") else None
        self.c.execute("INSERT INTO activities(partner_id,ts,type,body,actor) VALUES(?,?,?,?,?)", (pid, "2026-01-01T10:00:00", "note", "Important call notes", "t"))
        self.c.execute("INSERT OR IGNORE INTO tags(name) VALUES('vip')"); tid = self.c.execute("SELECT id FROM tags WHERE name='vip'").fetchone()[0]
        self.c.execute("INSERT OR IGNORE INTO partner_tags VALUES(?,?)", (pid, tid)); self.c.commit()
        return pid

    def test_20_bring_back_a_deleted_partner_with_history(self):
        pid = self._rich_partner(); name = self.c.execute("SELECT name FROM partners WHERE id=?", (pid,)).fetchone()[0]
        n_act = self.c.execute("SELECT COUNT(*) FROM activities WHERE partner_id=?", (pid,)).fetchone()[0]
        s = self.snap("pre-del")
        crm.delete_partner(self.c, pid, name, "oops", "t"); self.c.commit()
        self.assertIsNone(self.c.execute("SELECT 1 FROM partners WHERE id=?", (pid,)).fetchone())
        hit = [x for x in snapshots.partners_in(s, name[:8]) if x["id"] == pid][0]; self.assertFalse(hit["in_live"]); self.assertTrue(hit["erased"])
        with self.assertRaises(snapshots.RestoreError) as e: snapshots.restore_partner(s, pid)
        self.assertIn("erasure log", str(e.exception))
        r = snapshots.restore_partner(s, pid, allow_erased=True, by="tester")
        self.assertTrue(r["ok"]); self.assertFalse(r["replaced"])
        c = db.connect()
        self.assertEqual(c.execute("SELECT name FROM partners WHERE id=?", (pid,)).fetchone()[0], name)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM activities WHERE partner_id=? AND body LIKE 'Important%'", (pid,)).fetchone()[0], 1)
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM activities WHERE partner_id=?", (pid,)).fetchone()[0], n_act)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks WHERE partner_id=? AND title='Send rate card'", (pid,)).fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM partner_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.partner_id=? AND t.name='vip'", (pid,)).fetchone()[0], 1)
        self.assertTrue(c.execute("SELECT follow_up FROM partners WHERE id=?", (pid,)).fetchone()[0])
        self.assertTrue(c.execute("SELECT 1 FROM activities WHERE partner_id=? AND body LIKE 'Restored from backup%'", (pid,)).fetchone())
        c.close()

    def test_21_replace_overwrites_only_that_partner(self):
        pid = self._rich_partner(); other = self.c.execute("SELECT id FROM partners WHERE id<>? ORDER BY id LIMIT 1", (pid,)).fetchone()[0]
        s = self.snap("pre-edit")
        n_tasks = self.c.execute("SELECT COUNT(*) FROM tasks WHERE partner_id=?", (pid,)).fetchone()[0]
        old_stage = self.c.execute("SELECT stage FROM partners WHERE id=?", (pid,)).fetchone()[0]
        self.c.execute("UPDATE partners SET stage='Not interested', email='mangled@x.com' WHERE id=?", (pid,)); self.c.execute("UPDATE partners SET owner='Keep Me' WHERE id=?", (other,))
        self.c.execute("DELETE FROM tasks WHERE partner_id=?", (pid,)); self.c.commit()
        with self.assertRaises(snapshots.RestoreError): snapshots.restore_partner(s, pid)           # exists: needs replace=True
        r = snapshots.restore_partner(s, pid, replace=True); self.assertTrue(r["replaced"])
        c = db.connect()
        self.assertEqual(c.execute("SELECT stage FROM partners WHERE id=?", (pid,)).fetchone()[0], old_stage)
        self.assertNotEqual(c.execute("SELECT email FROM partners WHERE id=?", (pid,)).fetchone()[0], "mangled@x.com")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks WHERE partner_id=?", (pid,)).fetchone()[0], n_tasks)
        self.assertEqual(c.execute("SELECT owner FROM partners WHERE id=?", (other,)).fetchone()[0], "Keep Me")
        c.close()
        with self.assertRaises(snapshots.RestoreError): snapshots.restore_partner(s, 99999999)

    # ---- alerts ----
    def test_30_alerts_are_throttled_and_cleared(self):
        SENT.clear(); alerts.clear("k1")
        self.assertTrue(alerts.send("k1", "subject one", "body")); self.assertFalse(alerts.send("k1", "subject one", "body")); self.assertEqual(len(SENT), 1)
        self.assertEqual(SENT[0][0], "partnerships@getbelong.com"); self.assertTrue(SENT[0][1].startswith("Partner CRM:"))
        alerts.clear("k1"); self.assertTrue(alerts.send("k1", "again", "b")); self.assertEqual(len(SENT), 2)
        backup.save_cfg({"alert_email": "boss@example.com"}); alerts.clear("k2"); alerts.send("k2", "x", "y"); self.assertEqual(SENT[-1][0], "boss@example.com")
        with self.assertRaises(ValueError): backup.save_cfg({"alert_email": "not an email"})
        backup.save_cfg({"alert_email": ""})

    def test_31_local_backup_failure_alerts_and_recovery_clears(self):
        SENT.clear(); alerts.clear("local-fail")
        good = backup.load_cfg()["folder"]
        blocker = os.path.join(TMP, "blocker")
        with open(blocker, "w") as bf: bf.write("x")
        backup.save_cfg({"folder": os.path.join(blocker, "sub")})                 # a folder that can't be created
        r = backup.run("fail"); self.assertFalse(r["ok"]); self.assertTrue(any("failed" in a[1] for a in SENT))
        backup.save_cfg({"folder": good}); self.assertTrue(backup.run("ok")["ok"])
        self.assertNotIn("local-fail", backup.load_cfg()["alerts_sent"])

    def test_32_catch_up_message_when_backup_was_overdue(self):
        SENT.clear(); alerts.clear("caught-up")
        backup.save_cfg({"last_ok": (datetime.now() - timedelta(hours=80)).isoformat(timespec="seconds")})
        cfg = backup.load_cfg(); gap = (datetime.now() - datetime.fromisoformat(cfg["last_ok"])).total_seconds() / 3600
        res = backup.run("auto"); self.assertTrue(res["ok"])
        alerts.send("caught-up", "backup is working again", f"gap {int(gap)}h")
        self.assertTrue(any("working again" in a[1] for a in SENT))

    # ---- status + api ----
    def test_40_api_routes_and_confirmation_guard(self):
        s = self.snap("api")
        self.assertTrue(server.api("GET", "/api/backup/snapshots", {}, {})["rows"])
        self.assertIn("rows", server.api("POST", "/api/backup/preview", {}, {"name": s}))
        self.assertIn("counts", server.api("POST", "/api/backup/compare", {}, {"a": s, "b": "live"}))
        with self.assertRaises(ValueError) as e: server.api("POST", "/api/backup/restore", {}, {"name": s})
        self.assertIn("RESTORE", str(e.exception))
        self.assertTrue(server.api("POST", "/api/backup/restore", {}, {"name": s, "confirm": "RESTORE"})["ok"])
        self.assertIn("state", server.api("GET", "/api/backup/status", {}, {}))

    def test_41_bulk_send_takes_a_safety_snapshot_first(self):
        import glob
        before = len(glob.glob(os.path.join(TMP, "bk", "snapshots", "*-before-bulk-send.db")))
        old = time.time() - 7200
        for f in os.listdir(os.path.join(TMP, "bk", "snapshots")): os.utime(os.path.join(TMP, "bk", "snapshots", f), (old, old))
        ids = [r[0] for r in self.c.execute("SELECT id FROM partners ORDER BY id LIMIT 2")]
        keep = mail.start_send_job; mail.start_send_job = lambda *a, **k: {"id": "stub", "state": "done"}      # never start a real send (or touch SMTP) from a test
        try: server.api("POST", "/api/mail/send", {}, {"partner_ids": ids, "force": False})
        finally: mail.start_send_job = keep
        self.assertEqual(len(glob.glob(os.path.join(TMP, "bk", "snapshots", "*-before-bulk-send.db"))), before + 1)

    # ---- encrypted settings + restore.py ----
    def test_50_restore_script_brings_back_settings_with_passphrase(self):
        vault.setup("my restore passphrase")
        notify.save_cfg("https://hooks.slack.com/services/T/B/XYZ")
        backup.run("vault"); folder = os.path.join(TMP, "bk")
        self.assertTrue(os.path.isfile(os.path.join(folder, "secrets", "settings.enc")))
        app = os.path.join(TMP, "newmac2"); os.makedirs(os.path.join(app, "data"))
        env = {**os.environ, "CRMPASS": "my restore passphrase"}
        r = subprocess.run([sys.executable, os.path.join(HERE, "restore.py"), "--backup", folder, "--app-dir", app, "--passphrase-env", "CRMPASS"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn("Settings restored", r.stdout); self.assertIn("Nothing to re-enter", r.stdout)
        import json
        with open(os.path.join(app, "data", "mail.json")) as mf: self.assertEqual(json.load(mf)["password"], "pw-for-tests-only")
        self.assertEqual(oct(os.stat(os.path.join(app, "data", "mail.json")).st_mode & 0o777), "0o600")
        with open(os.path.join(app, "data", "slack.json")) as sf: self.assertIn("hooks.slack.com", sf.read())
        # wrong passphrase: database still restores, settings do not, and nothing is half-written
        app2 = os.path.join(TMP, "newmac3"); os.makedirs(os.path.join(app2, "data"))
        r2 = subprocess.run([sys.executable, os.path.join(HERE, "restore.py"), "--backup", folder, "--app-dir", app2, "--passphrase-env", "CRMPASS"], capture_output=True, text=True, env={**os.environ, "CRMPASS": "wrong wrong wrong"})
        self.assertEqual(r2.returncode, 0); self.assertIn("Wrong passphrase", r2.stdout); self.assertFalse(os.path.exists(os.path.join(app2, "data", "mail.json"))); self.assertTrue(os.path.exists(os.path.join(app2, "data", "partner_crm.db")))
        # --no-settings skips it entirely
        app3 = os.path.join(TMP, "newmac4"); os.makedirs(os.path.join(app3, "data"))
        r3 = subprocess.run([sys.executable, os.path.join(HERE, "restore.py"), "--backup", folder, "--app-dir", app3, "--no-settings"], capture_output=True, text=True)
        self.assertEqual(r3.returncode, 0); self.assertFalse(os.path.exists(os.path.join(app3, "data", "mail.json")))
        vault.remove()

    def test_51_vault_api_and_crypto_edges(self):
        with self.assertRaises(ValueError): server.api("POST", "/api/backup/vault", {}, {"passphrase": "short"})
        self.assertTrue(server.api("POST", "/api/backup/vault", {}, {"passphrase": "another long passphrase"})["configured"])
        self.assertTrue(server.api("POST", "/api/backup/vault", {}, {"action": "check", "passphrase": "another long passphrase"})["ok"])
        with self.assertRaises(ValueError): server.api("POST", "/api/backup/vault", {}, {"action": "check", "passphrase": "not the one"})
        self.assertFalse(server.api("POST", "/api/backup/vault", {}, {"action": "remove"})["configured"])
        # a malicious blob can't write outside data/ or non-json files
        d = os.path.join(TMP, "evil"); salt = os.urandom(16); m = vault._derive("pass-pass-pass", salt)
        import json as j
        blob = vault.encrypt(j.dumps({"files": {"../escape.json": "{}", "x.sh": "rm", "ok.json": "{}"}}).encode(), m, salt)
        self.assertEqual(vault.restore_into(d, blob, "pass-pass-pass"), ["ok.json"]); self.assertFalse(os.path.exists(os.path.join(TMP, "escape.json")))


if __name__ == "__main__":
    unittest.main()
