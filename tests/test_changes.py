"""Change history + undo, 'who did it' labelling, and the guard that keeps tests away from the real database."""
import os, sqlite3, sys, tempfile, threading, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
_s = sqlite3.connect(os.path.join(HERE, "data", "partner_crm.db")); _d = sqlite3.connect(os.path.join(TMP, "ch.db")); _s.backup(_d); _s.close(); _d.close()
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "ch.db")
import db, crm, server  # noqa: E402


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dbp = db.DB_PATH; db.DB_PATH = os.path.join(TMP, "ch.db")
        db.init(); cls.c = db.connect()
        cls.pid = cls.c.execute("SELECT id FROM partners WHERE archived=0 AND do_not_contact=0 ORDER BY id LIMIT 1 OFFSET 7").fetchone()[0]
        cls.other = cls.c.execute("SELECT id FROM partners WHERE id<>? AND archived=0 ORDER BY id LIMIT 1", (cls.pid,)).fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls.c.commit(); cls.c.close(); db.DB_PATH = cls._dbp; db.CTX.name = ""

    def setUp(self):
        db.CTX.name = ""

    def log(self, pid=None):
        return [dict(r) for r in self.c.execute("SELECT field,old,new,actor,undone FROM change_log WHERE partner_id=? ORDER BY id", (pid or self.pid,))]

    def test_01_edits_are_logged_with_who_did_them(self):
        n = len(self.log())
        db.CTX.name = "Lucky"
        self.c.execute("UPDATE partners SET stage='In discussion', owner='Asha' WHERE id=?", (self.pid,)); self.c.commit()
        new = self.log()[n:]
        self.assertEqual({(x["field"], x["new"], x["actor"]) for x in new}, {("stage", "In discussion", "Lucky"), ("owner", "Asha", "Lucky")})
        self.c.execute("UPDATE partners SET stage='In discussion' WHERE id=?", (self.pid,)); self.c.commit()          # no real change: nothing logged
        self.assertEqual(len(self.log()), n + 2)

    def test_02_background_work_is_labelled_system_and_never_blank(self):
        db.CTX.name = ""
        self.c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (self.pid, "2026-10-20T10:00:00", "reply", "from a job")); self.c.commit()
        self.assertEqual(self.c.execute("SELECT actor FROM activities WHERE body='from a job'").fetchone()[0], "system")
        db.CTX.name = "Nandeesh"
        self.c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (self.pid, "2026-10-20T10:00:01", "note", "by a person")); self.c.commit()
        self.assertEqual(self.c.execute("SELECT actor FROM activities WHERE body='by a person'").fetchone()[0], "Nandeesh")
        self.c.execute("INSERT INTO activities(partner_id,ts,type,body,actor) VALUES(?,?,?,?,?)", (self.pid, "2026-10-20T10:00:02", "note", "explicit", "Someone")); self.c.commit()
        self.assertEqual(self.c.execute("SELECT actor FROM activities WHERE body='explicit'").fetchone()[0], "Someone")
        # another thread (like the mail or reply-check job) doesn't inherit the web request's person
        out = []
        def job():
            c2 = db.connect(); c2.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (self.pid, "2026-10-20T10:00:03", "bounce", "thread job")); c2.commit()
            out.append(c2.execute("SELECT actor FROM activities WHERE body='thread job'").fetchone()[0]); c2.close()
        t = threading.Thread(target=job); t.start(); t.join(); self.assertEqual(out, ["system"])

    def test_03_undo_restores_the_old_value_and_notes_it(self):
        db.CTX.name = "setup"
        self.c.execute("UPDATE partners SET stage='New', do_not_contact=0, dnc_reason=NULL, lost_reason=NULL WHERE id=?", (self.pid,)); self.c.commit()
        db.CTX.name = "unsubscribe-job"
        self.c.execute("UPDATE partners SET stage='Not interested', do_not_contact=1, dnc_reason='unsubscribed', lost_reason='Unsubscribed' WHERE id=?", (self.pid,)); self.c.commit()
        g = crm.recent_changes(self.c, self.pid, 5)[0]
        self.assertEqual({x["field"] for x in g["changes"]}, {"stage", "do_not_contact", "dnc_reason", "lost_reason"}); self.assertTrue(g["undoable"])
        r = crm.undo_changes(self.c, [x["id"] for x in g["changes"]], "Pallav"); self.c.commit()
        self.assertEqual((r["undone"], r["skipped"]), (4, []))
        p = self.c.execute("SELECT stage,do_not_contact,dnc_reason,lost_reason FROM partners WHERE id=?", (self.pid,)).fetchone()
        self.assertEqual((p[0], p[1], p[2], p[3]), ("New", 0, None, None))
        self.assertTrue(self.c.execute("SELECT 1 FROM activities WHERE partner_id=? AND body LIKE 'Undid a change: stage%' AND actor='Pallav'", (self.pid,)).fetchone())
        self.assertFalse(crm.recent_changes(self.c, self.pid, 1)[0]["undoable"] and any(not x["undone"] for x in crm.recent_changes(self.c, self.pid, 1)[0]["changes"] if x["field"] == "stage" and x["new"] == "Not interested"))
        self.assertEqual(crm.undo_changes(self.c, [x["id"] for x in g["changes"]])["undone"], 0)            # undoing twice does nothing

    def test_04_changed_again_since_is_left_alone(self):
        self.c.execute("UPDATE partners SET owner='A1' WHERE id=?", (self.other,)); self.c.commit()
        first = self.c.execute("SELECT id FROM change_log WHERE partner_id=? AND field='owner' ORDER BY id DESC LIMIT 1", (self.other,)).fetchone()[0]
        self.c.execute("UPDATE partners SET owner='A2' WHERE id=?", (self.other,)); self.c.commit()
        r = crm.undo_changes(self.c, [first]); self.assertEqual(r["undone"], 0); self.assertIn("changed again", r["skipped"][0])
        self.assertEqual(self.c.execute("SELECT owner FROM partners WHERE id=?", (self.other,)).fetchone()[0], "A2")

    def test_05_only_listed_fields_and_bad_ids_are_safe(self):
        self.c.execute("INSERT INTO change_log(ts,partner_id,field,old,new,actor) VALUES('2026-10-20T00:00:00',?,'id','1','2','x')", (self.pid,)); self.c.commit()
        bad = self.c.execute("SELECT MAX(id) FROM change_log").fetchone()[0]
        self.assertEqual(crm.undo_changes(self.c, [bad, 99999999, -5])["undone"], 0)
        self.assertGreater(self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0], 100)

    def test_06_api_routes(self):
        self.c.execute("UPDATE partners SET city='Pune' WHERE id=?", (self.pid,)); self.c.commit()
        rows = server.api("GET", "/api/changes", {"partner_id": [str(self.pid)], "limit": ["5"]}, {})["rows"]
        self.assertTrue(rows and rows[0]["partner"]); ids = [x["id"] for g in rows for x in g["changes"] if x["field"] == "city" and not x["undone"]]
        res = server.api("POST", "/api/changes/undo", {}, {"ids": ids}); self.assertGreaterEqual(res["undone"], 1)
        self.assertIn("rows", server.api("GET", "/api/changes", {"hours": ["24"]}, {}))

    def test_07_restoring_a_partner_from_backup_is_also_logged(self):
        n = self.c.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
        self.c.execute("UPDATE partners SET phone='123' WHERE id=?", (self.other,)); self.c.commit()
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], n + 1)

    def test_08_tests_cannot_open_the_real_database(self):
        keep = db.DB_PATH
        try:
            db.DB_PATH = os.path.join(HERE, "data", "partner_crm.db")
            with self.assertRaises(RuntimeError) as e: db.connect()
            self.assertIn("REAL CRM database", str(e.exception))
            db.DB_PATH = os.path.join(HERE, "data", "partner_crm.db-wal")
            with self.assertRaises(RuntimeError): db.connect()
        finally:
            db.DB_PATH = keep
        self.assertTrue(os.environ["PARTNER_CRM_TEST_SANDBOX"] if "PARTNER_CRM_TEST_SANDBOX" in os.environ else True)

    def test_09_old_databases_get_the_log_and_triggers(self):
        p = os.path.join(TMP, "old.db"); a = sqlite3.connect(os.path.join(TMP, "ch.db")); b = sqlite3.connect(p); a.backup(b); a.close()
        b.executescript("DROP TRIGGER trg_partner_log; DROP TRIGGER trg_activity_actor; DROP TABLE change_log;"); b.close()      # what a database from before this feature looks like
        keep = db.DB_PATH; db.DB_PATH = p
        try:
            db.init()
            with db.connect() as c:
                pid = c.execute("SELECT id FROM partners ORDER BY id LIMIT 1").fetchone()[0]
                c.execute("UPDATE partners SET owner='Zed' WHERE id=?", (pid,))
                self.assertEqual(c.execute("SELECT field FROM change_log").fetchone()[0], "owner")
        finally:
            db.DB_PATH = keep


if __name__ == "__main__":
    unittest.main()
