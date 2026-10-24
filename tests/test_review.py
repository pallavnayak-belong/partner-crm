"""Review before bulk sends (flags, samples, excluding people) and the large-email warning. Nothing here sends mail."""
import os, sqlite3, sys, tempfile, unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
_s = sqlite3.connect(os.path.join(HERE, "data", "partner_crm.db")); _d = sqlite3.connect(os.path.join(TMP, "rv.db")); _s.backup(_d); _s.close(); _d.close()
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "rv.db")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")
import db, mail, server, attachments  # noqa: E402

day = lambda n: (datetime.now() - timedelta(days=n)).isoformat(timespec="seconds")


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (db.DB_PATH, mail.CFG_PATH, attachments.DIR)
        db.DB_PATH = os.path.join(TMP, "rv.db"); mail.CFG_PATH = os.path.join(TMP, "mail.json"); attachments.DIR = os.path.join(TMP, "att")
        db.init(); cls.c = db.connect()
        c = cls.c
        c.execute("DELETE FROM emails"); c.execute("DELETE FROM replies_seen"); c.commit()
        mail.save_cfg({"address": "x@example.com", "password": "unused", "delay": 0})
        c.execute("DELETE FROM template_attachments"); c.execute("DELETE FROM sequence_steps"); c.execute("DELETE FROM templates")
        c.execute("INSERT INTO templates(id,name,subject,plain,html,segment,updated_at) VALUES(1,'Intro','Hello {{first_name}}','Hi {{first_name}},\n\nPlease join.','<html><body><p>Hi {{first_name}},</p><p>Please join.</p></body></html>','',?)", (day(0),))
        c.commit()
        rows = c.execute("SELECT id FROM partners WHERE do_not_contact=0 AND archived=0 ORDER BY id LIMIT 12").fetchall()
        cls.ids = [r[0] for r in rows]
        for i, pid in enumerate(cls.ids):
            c.execute("UPDATE partners SET email=?, stage='New', contact_person='Person%d', segment='Corporate', country='India' WHERE id=?" % i, (f"p{i}@example.org", pid))
        c.commit()

    @classmethod
    def tearDownClass(cls):
        cls.c.commit(); cls.c.close(); db.DB_PATH, mail.CFG_PATH, attachments.DIR = cls._saved

    def reviewed(self, ids=None, **kw):
        return mail.review(self.c, ids or self.ids, **kw)

    def flags(self, rv):
        return {r["id"]: r["flags"] for r in rv["rows"]}

    def test_01_clean_list_has_no_flags_and_samples(self):
        rv = self.reviewed()
        self.assertEqual((rv["flagged"], rv["checked"]), (0, 12))
        self.assertEqual(len(rv["samples"]), 3); s = rv["samples"][0]
        self.assertIn("Hello Person", s["subject"]); self.assertIn("unsubscribe", s["html"].lower()); self.assertNotIn("{{", s["html"])
        self.assertEqual(len({x["partner_id"] for x in rv["samples"]}), 3)

    def test_02_recent_email_active_stage_reply_and_bounce_are_flagged(self):
        a, b, c_, d = self.ids[:4]
        self.c.execute("INSERT INTO emails(partner_id,template_id,to_addr,subject,message_id,status,sent_at,step) VALUES(?,?,?,?,?,?,?,0)", (a, 1, "p0@example.org", "s", "<m1>", "sent", day(2)))
        self.c.execute("INSERT INTO emails(partner_id,template_id,to_addr,subject,message_id,status,sent_at,step) VALUES(?,?,?,?,?,?,?,0)", (d, 1, "p3@example.org", "s", "<m2>", "sent", day(40)))
        self.c.execute("UPDATE partners SET stage='In discussion' WHERE id=?", (b,))
        self.c.execute("INSERT INTO replies_seen(message_id,partner_id,kind,ts) VALUES('<r1>',?, 'reply',?)", (c_, day(5)))
        self.c.execute("INSERT INTO replies_seen(message_id,partner_id,kind,ts) VALUES('<b1>',?, 'bounce',?)", (c_, day(5)))
        self.c.commit()
        f = self.flags(self.reviewed(force=True))
        self.assertIn("emailed 2 days ago", f[a]); self.assertIn("already in conversation (In discussion)", f[b])
        self.assertIn("has replied before", f[c_]); self.assertIn("an earlier email bounced", f[c_]); self.assertNotIn(d, f)           # 40 days ago is not recent

    def test_03_duplicate_address_flags_the_later_one(self):
        x, y = self.ids[5], self.ids[6]
        self.c.execute("UPDATE partners SET email='same@example.org' WHERE id IN (?,?)", (x, y)); self.c.commit()
        rv = self.reviewed(force=True); r = {r["id"]: r for r in rv["rows"]}
        self.assertNotIn(x, r); self.assertTrue(r[y]["dup"]); self.assertIn("same address as", r[y]["flags"][0])

    def test_04_greeting_and_merge_field_problems(self):
        z = self.ids[7]
        self.c.execute("UPDATE partners SET contact_person='', segment='Corporate' WHERE id=?", (z,)); self.c.commit()
        rv = self.reviewed(force=True)
        self.assertEqual(rv["generic_greeting"], 1); self.assertNotIn(z, self.flags(rv))                  # counted, not flagged per person
        self.c.execute("UPDATE templates SET subject='Hello {{first_name}} {{oops' WHERE id=1"); self.c.commit()
        try:
            self.assertTrue(any("unfilled merge field" in f for f in self.flags(self.reviewed(force=True))[self.ids[8]]))
        finally:
            self.c.execute("UPDATE templates SET subject='Hello {{first_name}}' WHERE id=1"); self.c.commit()

    def test_05_skipped_people_are_not_listed_and_excluded_marked(self):
        q = self.ids[9]
        self.c.execute("UPDATE partners SET do_not_contact=1 WHERE id=?", (q,)); self.c.commit()
        try:
            rv = self.reviewed(force=True, excluded=[self.ids[1]])
            self.assertNotIn(q, {r["id"] for r in rv["rows"]}); self.assertEqual(rv["checked"], 11)
        finally:
            self.c.execute("UPDATE partners SET do_not_contact=0 WHERE id=?", (q,)); self.c.commit()

    def test_06_plan_api_review_and_exclusion(self):
        pl = server.api("POST", "/api/mail/plan", {}, {"partner_ids": self.ids, "review": True, "force": True})
        self.assertIn("review", pl); self.assertEqual(pl["sendable"], 12); self.assertNotIn("review", server.api("POST", "/api/mail/plan", {}, {"partner_ids": self.ids}))
        ex = [self.ids[0], self.ids[1]]
        pl2 = server.api("POST", "/api/mail/plan", {}, {"partner_ids": self.ids, "review": True, "force": True, "exclude": ex})
        self.assertEqual((pl2["sendable"], pl2["excluded"], pl2["total"]), (10, 2, 12))
        self.assertTrue(all(r["excluded"] for r in pl2["review"]["rows"] if r["id"] in ex))

    def test_07_large_email_sizes_and_time(self):
        small = mail.size_info(self.c, [1])["Intro"]; self.assertLess(small["mb"], 1)
        os.makedirs(os.path.join(attachments.DIR, "1"), exist_ok=True)
        with open(os.path.join(attachments.DIR, "1", "big.pdf"), "wb") as f: f.write(os.urandom(6_000_000))
        self.c.execute("INSERT INTO template_attachments(template_id,orig_name,stored_name,size,uploaded_at) VALUES(1,'big.pdf','big.pdf',6000000,'2026-10-20')"); self.c.commit()
        try:
            big = mail.size_info(self.c, [1])["Intro"]
            self.assertGreaterEqual(big["mb"], 7.5); self.assertEqual(big["attachments"], 1); self.assertGreater(big["seconds"], 60)
            pl = server.api("POST", "/api/mail/plan", {}, {"partner_ids": self.ids[:3], "force": True})
            self.assertGreaterEqual(pl["sizes"]["Intro"]["mb"], 7.5); self.assertGreater(pl["est_seconds"], 180)
            pv = server.api("POST", "/api/mail/preview", {}, {"partner_id": self.ids[0], "template_id": 1})
            self.assertGreaterEqual(pv["size_mb"], 7.5); self.assertGreater(pv["est_seconds"], 60)
        finally:
            self.c.execute("DELETE FROM template_attachments"); self.c.commit()


if __name__ == "__main__":
    unittest.main()
