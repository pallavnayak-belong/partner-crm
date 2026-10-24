"""Mail engine tests with fake SMTP/IMAP. Run: python3 -m unittest discover -s tests"""
import os, shutil, sys, tempfile, time, unittest
from email.message import EmailMessage

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "t.db"))

import sqlite3 as _s  # fixtures must not depend on how many real partners have been emailed
_c = _s.connect(os.path.join(TMP, "t.db")); _c.create_function("crm_actor", 0, lambda: "test fixture"); _ids = "SELECT id FROM partners WHERE email LIKE '%@%.%' AND do_not_contact=0 ORDER BY id LIMIT 40"
_c.execute("DELETE FROM emails"); _c.execute("DELETE FROM replies_seen");  # temp copy only: fixtures must not depend on the real send history
_c.execute(f"DELETE FROM emails WHERE partner_id IN ({_ids})"); _c.execute(f"DELETE FROM activities WHERE type IN ('email','stage') AND partner_id IN ({_ids})"); _c.execute(f"UPDATE partners SET stage='New', last_contacted=NULL WHERE id IN ({_ids})"); _c.commit(); _c.close()
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")   # tests must never reach the real Google backup
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")   # tests must never write the real mailbox settings
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "t.db")
import db, mail, server  # noqa: E402

mail.CFG_PATH = os.path.join(TMP, "mail.json")
import notify  # noqa: E402
notify.CFG_PATH = os.path.join(TMP, "slack.json")  # tests must never post to the real Slack channel
SENT = []


class FakeSMTP:
    def __init__(self, *a, **k): pass
    def login(self, *a): pass
    def send_message(self, m): SENT.append(m)
    def quit(self): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


class FakeIMAP:
    inbox = []  # list of EmailMessage
    def __init__(self, *a, **k): pass
    def login(self, *a): pass
    def select(self, *a, **k): return "OK", [b""]
    def logout(self): pass
    def search(self, *a): return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.inbox)))]
    def fetch(self, num, spec):
        m = self.inbox[int(num) - 1]
        if "HEADER.FIELDS" in spec:
            h = "".join(f"{k}: {m[k]}\r\n" for k in ("Message-ID", "From", "References", "In-Reply-To", "Subject", "Auto-Submitted") if m[k]) + "\r\n"
            return "OK", [(b"", h.encode())]
        if "TEXT" in spec:
            return "OK", [(b"", m.get_content().encode())]
        return "OK", [(b"", m.as_bytes())]


mail.smtplib.SMTP_SSL = FakeSMTP
mail.imaplib.IMAP4_SSL = FakeIMAP


def inbound(frm, subj, body, mid, refs=None, auto=None):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = frm, subj, mid
    if refs: m["References"] = m["In-Reply-To"] = refs
    if auto: m["Auto-Submitted"] = auto
    m.set_content(body)
    return m


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(TMP, "t.db")   # each module uses its own temp copy even when run in one process
        db.init()
        c0 = db.connect(); mail.seed_templates(c0); mail.seed_sequence(c0); c0.commit(); c0.close()
        mail.save_cfg({"address": "partnerships@getbelong.com", "password": "abcd efgh ijkl mnop", "delay": 0, "daily_cap": 100})
        c = db.connect()
        mail.seed_templates(c); c.commit()
        cls.ids = [r[0] for r in c.execute("SELECT id FROM partners WHERE email LIKE '%@%.%' AND stage='New' AND do_not_contact=0 LIMIT 3")]
        cls.noemail = c.execute("SELECT id FROM partners WHERE COALESCE(email,'')='' LIMIT 1").fetchone()
        if not cls.noemail:
            c.execute("UPDATE partners SET email='' WHERE id=(SELECT MAX(id) FROM partners)"); c.commit()
            cls.noemail = c.execute("SELECT id FROM partners WHERE email='' LIMIT 1").fetchone()
        cls.noemail = cls.noemail[0]
        c.close()

    def run_job(self, ids, **kw):
        j = mail.start_send_job(ids, **kw)
        while j["state"] != "done": time.sleep(0.05)
        return j

    def test_1_send_logs_and_stage(self):
        j = self.run_job(self.ids + [self.noemail])
        self.assertEqual(j["sent"], 3)
        self.assertEqual([s["why"] for s in j["skipped"]], ["no valid email"])
        self.assertEqual(len(SENT), 3)
        c = db.connect()
        for pid in self.ids:
            p = c.execute("SELECT stage,last_contacted FROM partners WHERE id=?", (pid,)).fetchone()
            self.assertEqual(p["stage"], "Contacted"); self.assertTrue(p["last_contacted"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM emails WHERE partner_id=? AND status='sent'", (pid,)).fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM activities WHERE partner_id=? AND type='email'", (pid,)).fetchone()[0], 1)
        c.close()

    def test_2_dedupe_and_force(self):
        n = len(SENT)
        j = self.run_job(self.ids)
        self.assertEqual(j["sent"], 0); self.assertEqual(len(j["skipped"]), 3); self.assertEqual(len(SENT), n)
        self.assertEqual(self.run_job(self.ids[:1], force=True)["sent"], 1)

    def test_3_daily_cap(self):
        mail.save_cfg({"daily_cap": 1})
        c = db.connect(); pid = c.execute("SELECT id FROM partners WHERE email LIKE '%@%.%' AND stage='New' AND do_not_contact=0 LIMIT 1").fetchone()[0]; c.close()
        j = self.run_job([pid])
        self.assertEqual(j["sent"], 0); self.assertIn("cap", j["stopped_reason"].lower())
        mail.save_cfg({"daily_cap": 100})

    def test_4_replies_bounces_auto(self):
        c = db.connect()
        rows = c.execute("SELECT partner_id, message_id, to_addr FROM emails WHERE status='sent' GROUP BY partner_id").fetchall()
        c.close()
        a, b, d = rows[0], rows[1], rows[2]
        FakeIMAP.inbox = [
            inbound("Someone <x@partner.in>", "Re: Partner with Belong", "Interested, call me.\n\nOn Mon wrote:\n> quoted", "<r1@x>", refs=a["message_id"]),
            inbound(b["to_addr"], "Automatic reply: away", "Out of office", "<r2@x>", auto="auto-replied"),
            inbound("MAILER-DAEMON@google.com", "Delivery Status Notification", f"550 user unknown: {d['to_addr']}", "<r3@x>"),
            inbound("random@nobody.com", "hello", "spam", "<r4@x>"),
        ]
        res = mail.check_replies()
        self.assertEqual(res, {"replies": 1, "bounces": 1, "auto": 1, "unsubscribed": 0})
        c = db.connect()
        self.assertEqual(c.execute("SELECT stage FROM partners WHERE id=?", (a["partner_id"],)).fetchone()[0], "Replied")
        self.assertNotEqual(c.execute("SELECT stage FROM partners WHERE id=?", (b["partner_id"],)).fetchone()[0], "Replied")
        body = c.execute("SELECT body FROM activities WHERE partner_id=? AND type='reply'", (a["partner_id"],)).fetchone()[0]
        self.assertIn("Interested", body); self.assertNotIn("quoted", body)
        c.close()
        self.assertEqual(mail.check_replies(), {"replies": 0, "bounces": 0, "auto": 0, "unsubscribed": 0})  # idempotent

    def test_5_merge(self):
        self.assertEqual(mail.first_name({"contact_person": "RAJESH KUMAR", "segment": "Individual", "name": "x"}), "Rajesh")
        self.assertEqual(mail.first_name({"contact_person": "", "segment": "Corporate", "name": "ABC LLP"}), "there")
        self.assertEqual(mail.first_name({"contact_person": "", "segment": "Individual", "name": "SUNITA BANSAL"}), "Sunita")

    def test_6_footer_and_header(self):
        c = db.connect(); p = dict(c.execute("SELECT * FROM partners WHERE email<>'' LIMIT 1").fetchone())
        r = mail.render(c, p); c.close()
        self.assertIn("unsubscribe", r["plain"]); self.assertIn("unsubscribe", r["html"].lower())
        self.assertLess(r["html"].lower().rfind("unsubscribe"), r["html"].lower().rfind("</body>"))
        self.assertIn("List-Unsubscribe", mail.build_message(mail.load_cfg(), "a@b.co", r))

    def test_6b_cc(self):
        c = db.connect()
        c.execute("DELETE FROM cc_contacts"); c.execute("INSERT INTO cc_contacts(label,email) VALUES('Ops','ops@getbelong.com')"); c.commit()
        self.assertEqual(mail.resolve_cc(c, ["OPS@getbelong.com", "ops@getbelong.com"]), ["ops@getbelong.com"])
        with self.assertRaises(ValueError): mail.resolve_cc(c, ["stranger@x.com"])
        to = c.execute("SELECT email FROM partners WHERE id=?", (self.ids[0],)).fetchone()[0]
        self.assertEqual(mail.resolve_cc(c, ["ops@getbelong.com"], to=to), ["ops@getbelong.com"])
        n = len(SENT)
        self.assertEqual(self.run_job(self.ids[:1], force=True, cc=["ops@getbelong.com"])["sent"], 1)
        m = SENT[n]
        self.assertEqual(m["Cc"], "ops@getbelong.com")
        row = c.execute("SELECT cc, message_id FROM emails WHERE partner_id=? ORDER BY id DESC LIMIT 1", (self.ids[0],)).fetchone()
        self.assertEqual(row["cc"], "ops@getbelong.com")
        self.assertIn("Cc ops@getbelong.com", c.execute("SELECT body FROM activities WHERE partner_id=? AND type='email' ORDER BY id DESC", (self.ids[0],)).fetchone()[0])
        # a CC'd colleague replying-all (with unsubscribe-ish words) must not count as a partner reply or opt-out
        FakeIMAP.inbox = [inbound("Ops <ops@getbelong.com>", "Re: hello", "please unsubscribe me from this thread", "<reply-all-1@getbelong.com>", refs=row["message_id"])]
        before = c.execute("SELECT stage, do_not_contact FROM partners WHERE id=?", (self.ids[0],)).fetchone()
        self.assertEqual(mail.check_replies(), {"replies": 0, "bounces": 0, "auto": 0, "unsubscribed": 0})
        after = c.execute("SELECT stage, do_not_contact FROM partners WHERE id=?", (self.ids[0],)).fetchone()
        self.assertEqual(tuple(before), tuple(after)); self.assertEqual(after["do_not_contact"], 0)
        FakeIMAP.inbox = []
        c.execute("DELETE FROM cc_contacts"); c.commit(); c.close()

    def test_6c_login_failure_stops_job_cleanly(self):
        class BadLogin(FakeSMTP):
            def login(self, *a): raise mail.smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        keep = mail.smtplib.SMTP_SSL; mail.smtplib.SMTP_SSL = BadLogin
        try:
            n = len(SENT); c = db.connect(); before = c.execute("SELECT COUNT(*) FROM emails WHERE status='failed'").fetchone()[0]; c.close()
            j = self.run_job(self.ids[:2], force=True)
            c = db.connect(); after = c.execute("SELECT COUNT(*) FROM emails WHERE status='failed'").fetchone()[0]; c.close()
        finally:
            mail.smtplib.SMTP_SSL = keep
        self.assertEqual((j["sent"], len(SENT) - n, after - before), (0, 0, 0))
        self.assertIn("app password", j["stopped_reason"]); self.assertEqual(len(j["failed"]), 0)

    def test_6d_timeouts_scale_and_errors_are_explained(self):
        import socket
        small = mail.build_message({"address": "p@getbelong.com", "from_name": "B"}, "a@b.co", {"subject": "s", "plain": "p", "html": "<p>h</p>"})
        self.assertEqual(mail.send_timeout(small), 60)
        big = mail.build_message({"address": "p@getbelong.com", "from_name": "B"}, "a@b.co", {"subject": "s", "plain": "p", "html": "<p>h</p>", "_files": [("a.bin", __file__, 1)]})
        try:
            raise mail.smtplib.SMTPServerDisconnected("Server not connected") from socket.timeout("The write operation timed out")
        except mail.smtplib.SMTPServerDisconnected as e:
            self.assertIn("timed out", mail.explain_send_error(e, 6.6)); self.assertIn("6.6 MB", mail.explain_send_error(e, 6.6))
        self.assertEqual(mail.explain_send_error(ValueError("x")), "ValueError('x')")

    def test_6e_data_uri_images_become_inline_attachments(self):
        import base64
        from email import message_from_bytes
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKEDATA" * 5).decode()
        html = f'<p>Hi</p><img src="data:image/png;base64,{png}" alt="a"><img src=\'data:image/png;base64,{png}\'><img src="https://x.example/p.png"><img src="cid:img1">'
        msg = mail.build_message({"address": "p@getbelong.com", "from_name": "B"}, "a@b.co", {"subject": "s", "plain": "p", "html": html})
        from email import policy as _pol
        parsed = message_from_bytes(msg.as_bytes(), policy=_pol.default)
        body = next(p for p in parsed.walk() if p.get_content_type() == "text/html").get_content()
        self.assertNotIn("data:image", body); self.assertEqual(body.count("cid:inline_"), 2)
        self.assertIn("https://x.example/p.png", body); self.assertIn("cid:img1", body)
        inl = [p for p in parsed.walk() if p.get_content_type() == "image/png" and (p["Content-ID"] or "").startswith("<inline_")]
        self.assertEqual(len(inl), 1)                                   # identical pictures are attached once
        self.assertIn(inl[0]["Content-ID"].strip("<>"), body)

    def test_6f_test_send_is_a_job_that_reports_success_or_failure(self):
        j = mail.start_test_job("me@getbelong.com", None, None)
        while j["state"] != "done": time.sleep(0.05)
        self.assertEqual((j["sent"], j["failed"]), (1, []))
        class BadLogin(FakeSMTP):
            def login(self, *a): raise mail.smtplib.SMTPAuthenticationError(535, b"bad")
        keep = mail.smtplib.SMTP_SSL; mail.smtplib.SMTP_SSL = BadLogin
        try:
            j = mail.start_test_job("me@getbelong.com", None, None)
            while j["state"] != "done": time.sleep(0.05)
        finally:
            mail.smtplib.SMTP_SSL = keep
        self.assertEqual(j["sent"], 0); self.assertIn("app password", j["failed"][0]["why"])

    def test_7_dnc_blocks_send_and_unsubscribe_reply(self):
        c = db.connect()
        pid = c.execute("SELECT id FROM partners WHERE email LIKE '%@%.%' AND stage='New' AND do_not_contact=0 LIMIT 1").fetchone()[0]
        c.execute("UPDATE partners SET do_not_contact=1, dnc_reason='manual' WHERE id=?", (pid,)); c.commit()
        n = len(SENT); j = self.run_job([pid])
        self.assertEqual(j["sent"], 0); self.assertIn("do not contact", j["skipped"][0]["why"]); self.assertEqual(len(SENT), n)
        # unsubscribe reply on an already-emailed partner
        row = c.execute("SELECT partner_id, message_id FROM emails WHERE status='sent' ORDER BY id DESC LIMIT 1").fetchone()
        c.execute("UPDATE partners SET stage='Contacted', do_not_contact=0, dnc_reason='' WHERE id=?", (row["partner_id"],)); c.commit(); c.close()
        FakeIMAP.inbox = [inbound("p@x.in", "Re: Following up", "Please unsubscribe me.\n\n> If you'd prefer not to receive... unsubscribe", "<u1@x>", refs=row["message_id"])]
        res = mail.check_replies()
        self.assertEqual(res["unsubscribed"], 1)
        c = db.connect(); p = c.execute("SELECT stage,do_not_contact,dnc_reason FROM partners WHERE id=?", (row["partner_id"],)).fetchone()
        self.assertEqual((p["stage"], p["do_not_contact"], p["dnc_reason"]), ("Not interested", 1, "unsubscribed")); c.close()

    def test_8_bounce_marks_dnc_but_delay_does_not(self):
        c = db.connect(); row = c.execute("SELECT partner_id, to_addr FROM emails WHERE status='sent' AND message_id IS NOT NULL AND partner_id IN (SELECT id FROM partners WHERE do_not_contact=0) LIMIT 1").fetchone(); c.close()
        FakeIMAP.inbox = [inbound("MAILER-DAEMON@google.com", "Delivery Status Notification (Delay)", f"still trying {row['to_addr']}", "<d1@x>")]
        self.assertEqual(mail.check_replies()["bounces"], 0)
        FakeIMAP.inbox = [inbound("MAILER-DAEMON@google.com", "Delivery Status Notification (Failure)", f"550 {row['to_addr']}", "<d2@x>")]
        self.assertEqual(mail.check_replies()["bounces"], 1)
        c = db.connect()
        self.assertEqual(c.execute("SELECT dnc_reason FROM partners WHERE id=?", (row["partner_id"],)).fetchone()[0], "bounced"); c.close()

    def test_9_sequence_due_and_step_send(self):
        c = db.connect()
        pid = c.execute("SELECT id FROM partners WHERE email LIKE '%@%.%' AND stage='New' AND do_not_contact=0 LIMIT 1").fetchone()[0]; c.close()
        self.assertEqual(self.run_job([pid])["sent"], 1)
        c = db.connect()
        due = lambda: [x for st in mail.due_followups(c) for x in st["due"] if x["id"] == pid]
        self.assertEqual(due(), [])  # sent just now -> not due
        old = (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 5 * 86400)))
        c.execute("UPDATE emails SET sent_at=? WHERE partner_id=?", (old, pid)); c.commit()
        steps = mail.due_followups(c)
        self.assertEqual([x["id"] for x in steps[0]["due"] if x["id"] == pid], [pid]); self.assertEqual([x for x in steps[1]["due"] if x["id"] == pid], [])
        t1 = steps[0]["template_id"]; c.close()
        self.assertEqual(self.run_job([pid], template_id=t1, step=1)["sent"], 1)
        self.assertEqual(self.run_job([pid], template_id=t1, step=1)["sent"], 0)  # step dedupe
        c = db.connect()
        self.assertEqual([x for st in mail.due_followups(c) for x in st["due"] if x["id"] == pid], [])  # step1 just sent
        c.execute("UPDATE emails SET sent_at=? WHERE partner_id=? AND step=0", ((time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 20 * 86400))), pid))
        c.execute("UPDATE emails SET sent_at=? WHERE partner_id=? AND step=1", ((time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 8 * 86400))), pid)); c.commit()
        s2 = mail.due_followups(c)[1]["due"]; self.assertIn(pid, [x["id"] for x in s2])
        c.execute("UPDATE partners SET stage='Replied' WHERE id=?", (pid,)); c.commit()
        self.assertEqual([x for st in mail.due_followups(c) for x in st["due"] if x["id"] == pid], [])  # replied -> no more follow-ups
        c.close()

    def test_zz_analytics_runs(self):
        c = db.connect(); a = server.analytics(c); c.close()
        self.assertGreater(a["emailed"], 0); self.assertTrue(a["by_template"]); self.assertEqual(len(a["trend"]), 14)
        self.assertGreaterEqual(a["replied"], 1); self.assertGreaterEqual(a["unsubscribed"], 1)
        self.assertEqual(a["reached"][0]["n"], a["pipeline"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
