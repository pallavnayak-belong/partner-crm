"""CRM core: tasks, contacts, tags, views, archive, lost reason, merge, delete, export, backups, request guard."""
import http.client, json, os, shutil, sqlite3, sys, tempfile, threading, time, unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "c.db"))
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")   # tests must never reach the real Google backup
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")   # tests must never write the real mailbox settings
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "c.db")
os.environ["PARTNER_CRM_BACKUP_CFG"] = os.path.join(TMP, "backup.json")
import db, crm, backup, onboarding as ob, attachments, mail, server  # noqa: E402

day = lambda n: (date.today() + timedelta(days=n)).isoformat()


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (db.DB_PATH, backup.CFG_PATH, ob.DIR, attachments.DIR, mail.CFG_PATH)
        mail.CFG_PATH = os.path.join(TMP, "mail.json")
        db.DB_PATH = os.path.join(TMP, "c.db"); backup.CFG_PATH = os.path.join(TMP, "backup.json")
        ob.DIR = os.path.join(TMP, "onb"); attachments.DIR = os.path.join(TMP, "att")
        db.init(); cls.c = db.connect()
        cls.c.execute("DELETE FROM team_members"); cls.c.execute("DELETE FROM saved_views"); cls.c.commit()   # temp copy: don't depend on the team the real CRM has now
        backup.save_cfg({"folder": os.path.join(TMP, "bk")})
        ids = [r[0] for r in cls.c.execute("SELECT id FROM partners WHERE archived=0 ORDER BY id LIMIT 6")]
        cls.a, cls.b, cls.d, cls.e, cls.f, cls.g = ids

    @classmethod
    def tearDownClass(cls):
        cls.c.commit(); cls.c.close(); db.DB_PATH, backup.CFG_PATH, ob.DIR, attachments.DIR, mail.CFG_PATH = cls._saved

    def fu(self, pid): return self.c.execute("SELECT follow_up FROM partners WHERE id=?", (pid,)).fetchone()[0]

    def test_01_tasks_keep_follow_up_in_sync(self):
        a = self.a
        t1 = crm.add_task(self.c, a, "Send brochure", day(5))["id"]; t2 = crm.add_task(self.c, a, "Call about terms", day(2))["id"]
        self.assertEqual(self.fu(a), day(2))
        crm.update_task(self.c, t2, {"done": True}); self.assertEqual(self.fu(a), day(5))
        self.assertTrue(any("Task done: Call about terms" in r[0] for r in self.c.execute("SELECT body FROM activities WHERE partner_id=?", (a,))))
        crm.update_task(self.c, t1, {"due": day(9), "title": "Send brochure + terms"}); self.assertEqual(self.fu(a), day(9))
        crm.delete_task(self.c, t1); self.assertEqual(self.fu(a), "")
        with self.assertRaises(ValueError): crm.add_task(self.c, a, " ", day(1))
        with self.assertRaises(ValueError): crm.add_task(self.c, a, "x", "tomorrow")
        crm.add_task(self.c, a, "keep", day(1)); crm.close_open_tasks(self.c, a); self.assertEqual(self.fu(a), "")
        self.assertNotIn(a, [t["partner_id"] for t in crm.open_tasks(self.c)])

    def test_02_extra_contacts_recipients_and_primary_swap(self):
        b = self.b
        before = self.c.execute("SELECT contact_person,email FROM partners WHERE id=?", (b,)).fetchone()
        with self.assertRaises(ValueError): crm.add_contact(self.c, b, {"email": "bad"})
        with self.assertRaises(ValueError): crm.add_contact(self.c, b, {})
        cid = crm.add_contact(self.c, b, {"name": "Priya Ops", "email": "priya@ops.example", "role": "Operations"})["id"]
        rec = crm.recipients(self.c, b); self.assertEqual([r["primary"] for r in rec][:2], [True, False]) if before["email"] else None
        crm.make_primary(self.c, cid)
        p = self.c.execute("SELECT contact_person,email FROM partners WHERE id=?", (b,)).fetchone()
        self.assertEqual((p["contact_person"], p["email"]), ("Priya Ops", "priya@ops.example"))
        k = self.c.execute("SELECT name,email FROM partner_contacts WHERE id=?", (cid,)).fetchone()
        self.assertEqual((k["name"] or "", k["email"] or ""), (before["contact_person"] or "", before["email"] or ""))
        crm.delete_contact(self.c, cid)

    def test_03_send_to_specific_contact_and_contact_bounce(self):
        b = self.b
        self.c.execute("UPDATE partners SET do_not_contact=0, stage='New', email='main@firm.example' WHERE id=?", (b,))
        crm.add_contact(self.c, b, {"name": "Raj", "email": "raj@firm.example"}); self.c.commit()
        sent = []
        class S:
            def __init__(s, *a, **k): pass
            def login(s, *a): pass
            def send_message(s, m): sent.append(m)
            def quit(s): pass
        old = mail.smtplib.SMTP_SSL; mail.smtplib.SMTP_SSL = S
        try:
            mail.save_cfg({"address": "partnerships@getbelong.com", "password": "x", "delay": 0, "daily_cap": 100})
            j = mail.start_send_job([b], to="raj@firm.example", force=True)
            while j["state"] != "done": time.sleep(0.05)
        finally:
            mail.smtplib.SMTP_SSL = old
        self.assertEqual(j["sent"], 1); self.assertEqual(sent[0]["To"], "raj@firm.example")
        row = self.c.execute("SELECT to_addr FROM emails WHERE partner_id=? ORDER BY id DESC LIMIT 1", (b,)).fetchone()
        self.assertEqual(row[0], "raj@firm.example")
        with self.assertRaises(ValueError): server.api("POST", "/api/mail/send", {}, {"partner_ids": [b], "to": "stranger@x.com"})
        with self.assertRaises(ValueError): server.api("POST", "/api/mail/send", {}, {"partner_ids": [b, self.d], "to": "raj@firm.example"})

    def test_04_tags_filter_and_bulk(self):
        crm.add_tag(self.c, self.d, "NRI-focused"); crm.add_tag(self.c, self.d, "nri-focused"); crm.bulk_tag(self.c, [self.e, self.f], "Event lead"); self.c.commit()
        tags = {t["name"]: t["n"] for t in crm.tags_list(self.c)}
        self.assertEqual((tags["NRI-focused"], tags["Event lead"]), (1, 2))
        tid = next(t["id"] for t in crm.tags_list(self.c) if t["name"] == "Event lead")
        r = server.api("GET", "/api/partners", {"tag": [str(tid)]}, {})
        self.assertEqual({x["id"] for x in r["rows"]}, {self.e, self.f}); self.assertIn("Event lead", r["rows"][0]["tags"])
        crm.bulk_tag(self.c, [self.e], "Event lead", "remove"); self.assertEqual({t["name"]: t["n"] for t in crm.tags_list(self.c)}["Event lead"], 1)
        crm.delete_tag(self.c, tid); self.assertNotIn("Event lead", [t["name"] for t in crm.tags_list(self.c)])

    def test_05_saved_views_and_team(self):
        v = crm.save_view(self.c, "Foreign leads", "pipeline", {"region": "Foreign", "evil": "x", "tag": "3"})["id"]
        self.assertEqual(crm.save_view(self.c, "foreign LEADS", "pipeline", {"region": "Indian"})["updated"], True)
        vs = crm.views_list(self.c, "pipeline"); self.assertEqual(vs[0]["filters"], {"region": "Indian"})
        crm.delete_view(self.c, v); self.assertEqual(crm.views_list(self.c, "pipeline"), [])
        crm.team_add(self.c, "Ayush"); 
        with self.assertRaises(ValueError): crm.team_add(self.c, "ayush")
        self.assertEqual([m["name"] for m in crm.team_list(self.c)], ["Ayush"])

    def test_06_archive_hides_everywhere_and_blocks_sends(self):
        g = self.g; before = server.api("GET", "/api/dashboard", {}, {})["in_pipeline"]
        crm.add_task(self.c, g, "t", day(0)); crm.set_archived(self.c, g, True, "duplicate firm"); self.c.commit()
        self.assertNotIn(g, [x["id"] for x in server.api("GET", "/api/partners", {"limit": ["5000"]}, {})["rows"]])
        self.assertIn(g, [x["id"] for x in server.api("GET", "/api/partners", {"archived": ["1"]}, {})["rows"]])
        self.assertEqual(server.api("GET", "/api/dashboard", {}, {})["in_pipeline"], before - 1)
        self.assertNotIn(g, [t["partner_id"] for t in crm.open_tasks(self.c)])
        j = mail.start_send_job([g]); 
        while j["state"] != "done": time.sleep(0.05)
        self.assertEqual(j["skipped"][0]["why"], "archived") if self.c.execute("SELECT email FROM partners WHERE id=?", (g,)).fetchone()[0] else None
        crm.set_archived(self.c, g, False); self.c.commit()
        self.assertIn(g, [x["id"] for x in server.api("GET", "/api/partners", {"limit": ["5000"]}, {})["rows"]])

    def test_07_lost_reason_flow(self):
        e = self.e; self.c.commit()
        with self.assertRaises(ValueError): server.api("PATCH", f"/api/partners/{e}", {}, {"stage": "Not interested", "lost_reason": "Because"})
        server.api("PATCH", f"/api/partners/{e}", {}, {"stage": "Not interested", "lost_reason": "Using another platform", "lost_note": "Has Kuvera tie-up"})
        p = self.c.execute("SELECT stage,lost_reason FROM partners WHERE id=?", (e,)).fetchone(); self.assertEqual(tuple(p), ("Not interested", "Using another platform"))
        self.assertIn("Using another platform — Has Kuvera tie-up", self.c.execute("SELECT body FROM activities WHERE partner_id=? AND type='stage' ORDER BY id DESC", (e,)).fetchone()[0])
        server.api("PATCH", f"/api/partners/{e}", {}, {"stage": "Contacted"})
        self.assertIsNone(self.c.execute("SELECT lost_reason FROM partners WHERE id=?", (e,)).fetchone()[0])

    def test_08_activity_edit_rules(self):
        d = self.d
        server.api("POST", f"/api/partners/{d}/notes", {}, {"body": "typo hete"}); self.c.commit()
        aid = self.c.execute("SELECT id FROM activities WHERE partner_id=? AND type='note' ORDER BY id DESC", (d,)).fetchone()[0]
        crm.edit_activity(self.c, aid, "typo here"); r = self.c.execute("SELECT body,edited FROM activities WHERE id=?", (aid,)).fetchone(); self.assertEqual(tuple(r), ("typo here", 1))
        with self.assertRaises(ValueError): crm.edit_activity(self.c, aid, " ")
        sys_id = self.c.execute("SELECT id FROM activities WHERE partner_id=? AND type IN ('added','stage','email') AND body NOT LIKE 'typo%' ORDER BY id LIMIT 1", (d,)).fetchone()
        if sys_id:
            with self.assertRaises(ValueError): crm.delete_activity(self.c, sys_id[0])
        crm.delete_activity(self.c, aid); self.assertIsNone(self.c.execute("SELECT 1 FROM activities WHERE id=?", (aid,)).fetchone())

    def test_09_merge_moves_everything(self):
        c = self.c
        keep = crm._partner(c, self.e)["id"]; drop = self.f
        c.execute("UPDATE partners SET email='keep@one.example', phone='', do_not_contact=0 WHERE id=?", (keep,))
        c.execute("UPDATE partners SET email='drop@two.example', phone='9000000001', stage='Replied', do_not_contact=0 WHERE id=?", (drop,))
        crm.add_tag(c, drop, "dup-tag"); crm.add_task(c, drop, "moved task", day(3)); crm.add_contact(c, drop, {"name": "Extra", "email": "x@two.example"})
        c.execute("INSERT INTO emails(partner_id,to_addr,subject,status,sent_at,step) VALUES(?,?,?,?,?,0)", (drop, "drop@two.example", "s", "sent", "2026-10-01T10:00:00"))
        n_act = c.execute("SELECT COUNT(*) FROM activities WHERE partner_id IN (?,?)", (keep, drop)).fetchone()[0]
        n_em = c.execute("SELECT COUNT(*) FROM emails WHERE partner_id IN (?,?)", (keep, drop)).fetchone()[0]; c.commit()
        crm.merge(c, keep, drop); c.commit()
        self.assertIsNone(c.execute("SELECT 1 FROM partners WHERE id=?", (drop,)).fetchone())
        k = c.execute("SELECT stage,phone FROM partners WHERE id=?", (keep,)).fetchone(); self.assertEqual((k["stage"], k["phone"]), ("Replied", "9000000001"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM emails WHERE partner_id=?", (keep,)).fetchone()[0], n_em)
        emails = {r[0] for r in c.execute("SELECT email FROM partner_contacts WHERE partner_id=?", (keep,))}; self.assertEqual(emails, {"x@two.example", "drop@two.example"})
        self.assertEqual(self.fu(keep), day(3)); self.assertEqual([t["name"] for t in crm.partner_tags(c, keep)] if False else [t["name"] for t in crm.partner_tags(c, keep)], ["dup-tag"])
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM activities WHERE partner_id=?", (keep,)).fetchone()[0], n_act)
        self.assertTrue(any("-before-merge-" in x["file"] for x in backup.listing()))
        with self.assertRaises(ValueError): crm.merge(c, keep, keep)
        ob.start(c, self.g); c.commit()
        with self.assertRaises(ValueError) as e: crm.merge(c, keep, self.g)
        self.assertIn("onboarding", str(e.exception))

    def test_10_delete_removes_data_and_files_and_needs_typed_name(self):
        c = self.c; pid = self.g
        ob.save_file(c, pid, "pan", "pan.pdf", b"%PDF"); c.commit()
        folder = os.path.join(ob.DIR, str(pid)); self.assertTrue(os.listdir(folder))
        name = c.execute("SELECT name FROM partners WHERE id=?", (pid,)).fetchone()[0]
        with self.assertRaises(ValueError): crm.delete_partner(c, pid, "wrong name")
        backup.run("t"); mirror = os.path.join(TMP, "bk", "files", "onboarding", str(pid)); self.assertTrue(os.path.isdir(mirror))
        crm.delete_partner(c, pid, name.upper(), "data request", "Pallav"); c.commit()
        self.assertFalse(os.path.exists(folder)); self.assertFalse(os.path.exists(mirror))
        for tbl in ("partners", "activities", "onboarding", "onboarding_items", "onboarding_files"):
            col = "id" if tbl == "partners" else "partner_id"
            self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {col}=?", (pid,)).fetchone()[0], 0, tbl)
        log = c.execute("SELECT partner_id,by,reason FROM erasure_log").fetchone(); self.assertEqual(tuple(log), (pid, "Pallav", "data request"))
        self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_11_export_contains_the_partner_data(self):
        crm.add_contact(self.c, self.d, {"name": "Z", "email": "z@z.example"}); crm.add_tag(self.c, self.d, "t1")
        x = crm.export_partner(self.c, self.d)
        self.assertEqual(x["partner"]["id"], self.d); self.assertEqual(x["additional_contacts"][0]["email"], "z@z.example"); self.assertIn("t1", x["tags"]); json.dumps(x)

    def test_12_backup_snapshot_mirror_prune_schedule_and_validation(self):
        c = self.c; c.commit()
        with self.assertRaises(ValueError): backup.save_cfg({"folder": "relative/path"})
        with self.assertRaises(ValueError): backup.save_cfg({"folder": os.path.join(HERE, "data", "bk")})
        os.makedirs(ob.DIR, exist_ok=True)
        with open(os.path.join(ob.DIR, "x.txt"), "w") as f: f.write("doc")
        r = backup.run("manual"); self.assertTrue(r["ok"], r)
        snap = os.path.join(TMP, "bk", "snapshots", r["file"]); chk = sqlite3.connect(snap)
        self.assertEqual(chk.execute("SELECT COUNT(*) FROM partners").fetchone()[0], c.execute("SELECT COUNT(*) FROM partners").fetchone()[0]); chk.close()
        self.assertEqual(oct(os.stat(snap).st_mode & 0o777), "0o600")
        self.assertEqual([f for f in os.listdir(os.path.dirname(snap)) if f.endswith(("-wal", "-shm"))], [])
        self.assertTrue(os.path.exists(os.path.join(TMP, "bk", "files", "onboarding", "x.txt")))
        self.assertFalse(os.path.exists(os.path.join(TMP, "bk", "mail.json")))
        old = os.path.join(TMP, "bk", "snapshots", "partner_crm-20200101-000000-auto.db"); shutil.copy(snap, old)
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        self.assertGreaterEqual(backup.run("manual")["pruned"], 1); self.assertFalse(os.path.exists(old))
        self.assertFalse(backup.due()); backup.save_cfg({"last_ok": (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 90000)))}); self.assertTrue(backup.due())
        backup.save_cfg({"enabled": False}); self.assertFalse(backup.due()); backup.save_cfg({"enabled": True})
        backup.save_cfg({"folder": "/nonexistent-root-dir/x/y"}); bad = backup.run("manual"); self.assertFalse(bad["ok"]); self.assertIn("FAILED", backup.load_cfg()["last_result"])
        backup.save_cfg({"folder": os.path.join(TMP, "bk")})

    def test_13_request_guard_blocks_cross_site_and_rebinding(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H); threading.Thread(target=srv.serve_forever, daemon=True).start(); port = srv.server_port
        def call(method, path, body=None, headers=None):
            h = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            h.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {}); r = h.getresponse(); out = (r.status, r.read()); h.close(); return out
        try:
            ok = {"X-CRM": "1", "Content-Type": "application/json", "Host": f"127.0.0.1:{port}"}
            self.assertEqual(call("GET", "/api/team", headers={"Host": f"127.0.0.1:{port}"})[0], 200)
            self.assertEqual(call("POST", "/api/team", {"name": "Guard Test"}, ok)[0], 200)
            self.assertEqual(call("POST", "/api/team", {"name": "NoHeader"}, {"Content-Type": "text/plain", "Host": f"127.0.0.1:{port}"})[0], 403)       # classic cross-site simple request
            self.assertEqual(call("POST", "/api/team", {"name": "EvilOrigin"}, {**ok, "Origin": "https://evil.example"})[0], 403)
            self.assertEqual(call("POST", "/api/team", {"name": "Rebound"}, {**ok, "Host": "attacker.example:8765"})[0], 403)
            self.assertEqual(call("GET", "/api/team", headers={"Host": "attacker.example"})[0], 403)
            self.assertEqual(call("POST", "/api/team", {"name": "LocalOrigin"}, {**ok, "Origin": f"http://127.0.0.1:{port}"})[0], 200)
            code, raw = call("POST", "/api/tasks/0", {"done": True}, ok); self.assertIn(code, (404, 400))
            self.assertEqual({m["name"] for m in crm.team_list(db.connect())} & {"NoHeader", "EvilOrigin", "Rebound"}, set())
            # actor is stamped on activities created by a request
            pid = self.d
            call("POST", f"/api/partners/{pid}/notes", {"body": "stamped"}, {**ok, "X-Actor": "Ayush"})
            self.assertEqual(db.connect().execute("SELECT actor FROM activities WHERE partner_id=? AND body='stamped'", (pid,)).fetchone()[0], "Ayush")
        finally:
            srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
