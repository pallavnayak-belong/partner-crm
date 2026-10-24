"""Missing-email worklist, paged Pipeline list, the send log, and the unusual-activity watcher."""
import os, smtplib, sqlite3, sys, tempfile, time, unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
_s = sqlite3.connect(os.path.join(HERE, "data", "partner_crm.db")); _d = sqlite3.connect(os.path.join(TMP, "ops.db")); _s.backup(_d); _s.close(); _d.close()
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "ops.db")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")
os.environ["PARTNER_CRM_WATCH_STATE"] = os.path.join(TMP, "watch.json")
import db, crm, mail, server, watch, alerts, backup  # noqa: E402

SENT_MAIL, ALERTS = [], []


class FakeSMTP:
    def __init__(self, *a, **k): pass
    def login(self, *a): pass
    def send_message(self, m): SENT_MAIL.append(m)
    def quit(self): pass
    def close(self): pass


ago_m = lambda m: (datetime.now() - timedelta(minutes=m)).isoformat(timespec="seconds")


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (db.DB_PATH, mail.CFG_PATH, watch.STATE_PATH, backup.CFG_PATH, alerts._send_email, alerts.SYNC, smtplib.SMTP_SSL)
        db.DB_PATH = os.path.join(TMP, "ops.db"); mail.CFG_PATH = os.path.join(TMP, "mail.json"); watch.STATE_PATH = os.path.join(TMP, "watch.json"); backup.CFG_PATH = os.path.join(TMP, "backup.json")
        alerts._send_email = lambda to, subject, body: ALERTS.append((subject, body)) or True; alerts.SYNC = True
        smtplib.SMTP_SSL = FakeSMTP
        db.init(); cls.c = db.connect()
        mail.save_cfg({"address": "x@example.com", "password": "unused", "delay": 0, "daily_cap": 500})
        cls.c.execute("DELETE FROM emails"); cls.c.execute("DELETE FROM replies_seen"); cls.c.execute("DELETE FROM change_log"); cls.c.commit()

    @classmethod
    def tearDownClass(cls):
        cls.c.commit(); cls.c.close()
        db.DB_PATH, mail.CFG_PATH, watch.STATE_PATH, backup.CFG_PATH, alerts._send_email, alerts.SYNC, smtplib.SMTP_SSL = cls._saved

    # ---------- reachability ----------
    def test_01_summary_rows_and_saving(self):
        ids = [r[0] for r in self.c.execute("SELECT id FROM partners WHERE archived=0 AND do_not_contact=0 ORDER BY id LIMIT 4")]
        self.c.execute("UPDATE partners SET email='' WHERE id=?", (ids[0],)); self.c.execute("UPDATE partners SET email='not-an-email' WHERE id=?", (ids[1],)); self.c.execute("UPDATE partners SET email='ok@example.org' WHERE id=?", (ids[2],)); self.c.commit()
        s = crm.reach_summary(self.c); self.assertEqual(s["total"], s["reachable"] + s["missing"] + s["invalid"]); self.assertGreaterEqual(s["missing"], 1); self.assertGreaterEqual(s["invalid"], 1)
        rows = {r["id"]: r for r in crm.reach_rows(self.c, "needs", limit=5000)["rows"]}
        self.assertEqual(rows[ids[0]]["kind"], "missing"); self.assertEqual(rows[ids[1]]["kind"], "invalid"); self.assertNotIn(ids[2], rows)
        self.assertNotIn(ids[1], {r["id"] for r in crm.reach_rows(self.c, "missing", limit=5000)["rows"]}); self.assertIn(ids[1], {r["id"] for r in crm.reach_rows(self.c, "invalid", limit=5000)["rows"]})
        r = crm.set_emails(self.c, [{"id": ids[0], "email": " <New@Example.org> "}, {"id": ids[1], "email": "still bad"}, {"id": 99999999, "email": "a@b.co"}, {"id": "x", "email": "a@b.co"}, {"id": ids[3], "email": ""}], "Pallav"); self.c.commit()
        self.assertEqual(r["saved"], 1); self.assertEqual(len(r["errors"]), 3)
        self.assertEqual(self.c.execute("SELECT email FROM partners WHERE id=?", (ids[0],)).fetchone()[0], "New@Example.org")
        self.assertTrue(self.c.execute("SELECT 1 FROM activities WHERE partner_id=? AND body LIKE 'Email address added: New@Example.org' AND actor='Pallav'", (ids[0],)).fetchone())
        self.assertTrue(self.c.execute("SELECT 1 FROM change_log WHERE partner_id=? AND field='email'", (ids[0],)).fetchone())      # undoable
        w = crm.set_emails(self.c, [{"id": ids[1], "email": "OK@example.org"}]); self.assertEqual(w["saved"], 1); self.assertTrue(w["warnings"])   # same address on another partner: saved but flagged
        self.c.commit()

    def test_02_blocked_partners_are_left_out_and_routes_work(self):
        pid = self.c.execute("SELECT id FROM partners WHERE archived=0 ORDER BY id LIMIT 1 OFFSET 20").fetchone()[0]
        self.c.execute("UPDATE partners SET email='', do_not_contact=1 WHERE id=?", (pid,)); self.c.commit()
        self.assertNotIn(pid, {r["id"] for r in crm.reach_rows(self.c, "needs", limit=5000)["rows"]})
        res = server.api("GET", "/api/reach", {"show": ["needs"], "limit": ["5"]}, {}); self.assertIn("reachable", res); self.assertLessEqual(len(res["rows"]), 5)
        self.assertIn("reach", server.api("GET", "/api/dashboard", {}, {}))
        out = server.api("POST", "/api/reach/emails", {}, {"rows": []}); self.assertEqual(out["saved"], 0)

    # ---------- paged list ----------
    def test_03_partner_list_filters_pages_and_all_ids(self):
        allr = server.api("GET", "/api/partners", {"limit": ["5000"], "ids": ["1"]}, {})
        self.assertEqual(len(allr["ids"]), allr["total"])
        page = server.api("GET", "/api/partners", {"limit": ["50"], "ids": ["1"]}, {})
        self.assertEqual(len(page["rows"]), 50); self.assertEqual(page["ids"], allr["ids"]); self.assertEqual(page["total"], allr["total"])
        miss = server.api("GET", "/api/partners", {"email": ["missing"], "limit": ["5000"]}, {}); has = server.api("GET", "/api/partners", {"email": ["has"], "limit": ["5000"]}, {})
        self.assertEqual(miss["total"] + has["total"], allr["total"]); self.assertTrue(all(not (r["email"] or "").strip() or "@" not in r["email"] for r in miss["rows"]))
        self.assertTrue(all("@" in r["email"] for r in has["rows"]))
        self.assertNotIn("ids", server.api("GET", "/api/partners", {"limit": ["5"]}, {}))

    # ---------- send log ----------
    def run_job(self, ids, **kw):
        j = mail.start_send_job(ids, **kw)
        while j["state"] != "done": time.sleep(0.05)
        return j

    def test_04_every_send_is_logged_with_details(self):
        self.c.execute("DELETE FROM send_jobs"); self.c.commit()
        ids = [r[0] for r in self.c.execute("SELECT id FROM partners WHERE archived=0 AND do_not_contact=0 AND stage='New' AND email LIKE '%@%.%' ORDER BY id LIMIT 3")]
        dnc = self.c.execute("SELECT id FROM partners WHERE do_not_contact=1 LIMIT 1").fetchone()[0]
        db.CTX.name = "Lucky"
        try:
            j = self.run_job(ids + [dnc])
        finally:
            db.CTX.name = ""
        log = server.api("GET", "/api/sends", {}, {})["rows"][0]
        self.assertEqual((log["kind"], log["by"], log["state"], log["total"], log["sent"], log["skipped"], log["failed"]), ("bulk", "Lucky", "done", 4, j["sent"], 1, 0))
        det = server.api("GET", f"/api/sends/{log['id']}", {}, {})["details"]
        self.assertEqual(len(det["sent"]), j["sent"]); self.assertIn("do not contact", det["skipped"][0]["why"]); self.assertTrue(det["sent"][0]["to"])
        j2 = self.run_job(ids[:1], force=True); self.assertEqual(server.api("GET", "/api/sends", {}, {})["rows"][0]["kind"], "single")
        j3 = self.run_job(ids[:1], force=True, step=1); self.assertEqual(server.api("GET", "/api/sends", {}, {})["rows"][0]["kind"], "followup")
        with self.assertRaises(LookupError): server.api("GET", "/api/sends/nope", {}, {})

    def test_05_failures_and_stops_are_logged(self):
        ids = [r[0] for r in self.c.execute("SELECT id FROM partners WHERE archived=0 AND do_not_contact=0 AND email LIKE '%@%.%' ORDER BY id LIMIT 2")]
        class Boom(FakeSMTP):
            def send_message(self, m): raise smtplib.SMTPRecipientsRefused({})
        smtplib.SMTP_SSL = Boom
        try:
            self.run_job(ids, force=True)
        finally:
            smtplib.SMTP_SSL = FakeSMTP
        row = server.api("GET", "/api/sends", {}, {})["rows"][0]; self.assertEqual(row["failed"], 2)
        det = server.api("GET", f"/api/sends/{row['id']}", {}, {})["details"]; self.assertEqual(len(det["failed"]), 2); self.assertTrue(det["failed"][0]["why"])
        mail.save_cfg({"daily_cap": 1})
        try:
            self.run_job(ids, force=True)
        finally:
            mail.save_cfg({"daily_cap": 500})
        row = server.api("GET", "/api/sends", {}, {})["rows"][0]; self.assertEqual(row["state"], "stopped"); self.assertIn("cap", row["stopped_reason"].lower())

    def test_06_test_emails_are_logged_too(self):
        j = mail.start_test_job("me@example.com", None, None)
        while j["state"] != "done": time.sleep(0.05)
        row = server.api("GET", "/api/sends", {}, {})["rows"][0]; self.assertEqual(row["kind"], "test"); self.assertIn("me@example.com", row["template"])

    # ---------- watcher ----------
    def fresh(self):
        self.c.execute("DELETE FROM change_log"); self.c.execute("DELETE FROM activities WHERE ts>=?", (ago_m(60 * 30),)); self.c.execute("DELETE FROM replies_seen"); self.c.execute("DELETE FROM emails WHERE status='failed'"); self.c.commit()
        ALERTS.clear()
        for k in ("bulk-change", "unsub-spike", "bounce-spike", "send-failures", "partners-dropped", "test-data"): alerts.clear(k)
        watch._save({"partners": self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0]})

    def test_10_quiet_database_raises_nothing(self):
        self.fresh(); self.assertEqual(watch.check(self.c), []); self.assertEqual(ALERTS, [])

    def test_11_many_edits_at_once_alert_but_normal_send_moves_do_not(self):
        self.fresh()
        ids = [r[0] for r in self.c.execute("SELECT id FROM partners ORDER BY id LIMIT 12")]
        self.c.executemany("INSERT INTO change_log(ts,partner_id,field,old,new,actor) VALUES(?,?,?,?,?,?)", [(ago_m(2), i, "stage", "New", "Contacted", "system") for i in ids]); self.c.commit()
        self.assertEqual(watch.check(self.c), [])                                           # a bulk send moving New -> Contacted is normal
        self.c.executemany("INSERT INTO change_log(ts,partner_id,field,old,new,actor) VALUES(?,?,?,?,?,?)", [(ago_m(3), i, "stage", "Contacted", "Not interested", "Lucky") for i in ids]); self.c.commit()
        f = watch.check(self.c); self.assertEqual([x[0] for x in f], ["bulk-change"]); self.assertIn("Lucky", f[0][2]); self.assertEqual(len(ALERTS), 1)
        watch.check(self.c); self.assertEqual(len(ALERTS), 1)                              # throttled, not repeated every five minutes

    def test_12_unsubscribe_bounce_failure_spikes(self):
        self.fresh()
        ids = [r[0] for r in self.c.execute("SELECT id FROM partners ORDER BY id LIMIT 8")]
        self.c.executemany("INSERT INTO change_log(ts,partner_id,field,old,new,actor) VALUES(?,?,?,?,?,?)", [(ago_m(30), i, "do_not_contact", "0", "1", "system") for i in ids[:6]])
        self.c.executemany("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", [(i, ago_m(20), "bounce", "Email bounced") for i in ids[:6]])
        self.c.executemany("INSERT INTO emails(partner_id,template_id,to_addr,subject,status,error,sent_at) VALUES(?,?,?,?,?,?,?)", [(ids[0], 1, "a@b.co", "s", "failed", "x", ago_m(10)) for _ in range(6)]); self.c.commit()
        kinds = {x[0] for x in watch.check(self.c)}; self.assertEqual(kinds, {"unsub-spike", "bounce-spike", "send-failures"})

    def test_13_partners_dropping_and_test_data(self):
        self.fresh()
        n = self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0]; watch._save({"partners": n + 5})
        self.assertEqual([x[0] for x in watch.check(self.c)], ["partners-dropped"])
        self.assertEqual(watch.check(self.c, notify=False), [])                           # state moved on: only reported once
        self.c.execute("INSERT INTO replies_seen(message_id,partner_id,kind,ts) VALUES('<u1@x>',1,'reply',?)", (ago_m(1),)); self.c.commit()
        self.assertEqual([x[0] for x in watch.check(self.c, notify=False)], ["test-data"])


if __name__ == "__main__":
    unittest.main()
