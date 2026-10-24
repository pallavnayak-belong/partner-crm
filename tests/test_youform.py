"""Youform integration tests. Run: python3 -m unittest discover -s tests"""
import hashlib, hmac, json, os, shutil, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "y.db"))

import sqlite3 as _s  # fixtures must not depend on how many real partners have been emailed
_c = _s.connect(os.path.join(TMP, "y.db")); _c.create_function("crm_actor", 0, lambda: "test fixture"); _ids = "SELECT id FROM partners WHERE email LIKE '%@%.%' AND do_not_contact=0 ORDER BY id LIMIT 40"
_c.execute(f"DELETE FROM emails WHERE partner_id IN ({_ids})"); _c.execute(f"DELETE FROM activities WHERE type IN ('email','stage') AND partner_id IN ({_ids})"); _c.execute(f"UPDATE partners SET stage='New', last_contacted=NULL WHERE id IN ({_ids})"); _c.commit(); _c.close()
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")   # tests must never reach the real Google backup
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")   # tests must never write the real mailbox settings
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "y.db")
import db, youform  # noqa: E402

youform.CFG_PATH = os.path.join(TMP, "youform.json")
import notify  # noqa: E402
notify.CFG_PATH = os.path.join(TMP, "slack.json")  # tests must never post to the real Slack channel


def payload(sid, name, email, phone="9876543210", company="", form="geok4bom"):
    f = lambda q, a: {"id": q[:4], "question": q, "type": "input", "answer": a}
    return {"event_id": "e" + sid, "event_type": "submission", "form_id": form, "submission_id": sid, "completed_at": "2026-10-14T05:30:00.123456Z",
            "fields": [f("Name", name), f("Work Email", email), f("Phone Number", phone), f("Company", company),
                       f("You are applying as", "Financial advisor, distributor or wealth manager"),
                       f("What kind of clients are you planning to refer?", "Non Resident Indian (NRI)"),
                       f("What products are you looking for?", "GIFT City Mutual Funds"), f("Website, LinkedIn or social media profile", "https://linkedin.com/in/x")]}


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(TMP, "y.db")   # each module uses its own temp copy even when run in one process
        db.init()
        c = db.connect()
        # known partners for matching
        c.execute("DELETE FROM applications")
        cls.p1 = c.execute("SELECT id,name,email FROM partners WHERE email LIKE '%@%.%' AND stage='New' AND do_not_contact=0 LIMIT 1").fetchone()
        cls.p2 = c.execute("SELECT id,name,email FROM partners WHERE id<>? AND email LIKE '%@%.%' AND stage='Contacted' LIMIT 1", (cls.p1["id"],)).fetchone()
        c.execute("UPDATE partners SET contact_person='', phone='' WHERE id IN (?,?)", (cls.p1["id"], cls.p2["id"]))
        c.commit(); c.close()

    def test_1_parse_webhook(self):
        a = youform.from_webhook(payload("s1", "Asha Rao", "asha@firm.in", company="Rao Wealth LLP"))
        self.assertEqual((a["name"], a["email"], a["company"], a["external_id"]), ("Asha Rao", "asha@firm.in", "Rao Wealth LLP", "s1"))
        self.assertIn("NRI", a["clients"]); self.assertIn("GIFT City Mutual", a["products"]); self.assertTrue(a["profile"].startswith("https"))
        self.assertIsNone(youform.from_webhook({"hello": 1}))

    def test_2_parse_csv(self):
        csv_text = "Submission ID,Name,Work Email,Phone Number,Company,You are applying as,Submitted at\nabc1,Ravi K,ravi@x.com,9000000000,RK Advisors,Content creator,2026-10-13 10:00:00\n"
        a = youform.from_csv(csv_text)
        self.assertEqual(len(a), 1); self.assertEqual((a[0]["name"], a[0]["company"], a[0]["external_id"]), ("Ravi K", "RK Advisors", "abc1"))

    def test_3_email_match_autolinks_and_applies(self):
        c = db.connect(); before = c.execute("SELECT stage, follow_up FROM partners WHERE id=?", (self.p1["id"],)).fetchone()
        a = youform.from_webhook(payload("s2", "New Person", self.p1["email"].upper(), phone="9111111111"))
        self.assertEqual(youform.ingest(c, a), "linked"); c.commit()
        p = c.execute("SELECT * FROM partners WHERE id=?", (self.p1["id"],)).fetchone()
        self.assertEqual(p["stage"], "Applied"); self.assertEqual(p["contact_person"], "New Person"); self.assertEqual(p["phone"], "9111111111"); self.assertTrue(p["follow_up"])
        acts = [r["type"] for r in c.execute("SELECT type FROM activities WHERE partner_id=?", (self.p1["id"],))]
        self.assertIn("application", acts); self.assertIn("stage", acts)
        self.assertEqual(youform.ingest(c, a), "duplicate")  # idempotent
        c.close()

    def test_4_company_name_match(self):
        c = db.connect()
        name = self.p2["name"]
        a = youform.from_webhook(payload("s3", "Someone Else", "x@gmail.com", phone="9222222222", company=name.upper().replace(" LLP", " L.L.P.") if "LLP" in name.upper() else name + " Pvt Ltd"))
        res = youform.ingest(c, a)
        self.assertEqual(res, "linked")
        row = c.execute("SELECT * FROM applications WHERE external_id='s3'").fetchone()
        self.assertTrue(json.loads(row["candidates"]))
        self.assertEqual(json.loads(row["candidates"])[0][0], self.p2["id"]); c.close()

    def test_5_no_candidates_creates_inbound_partner(self):
        c = db.connect(); n = c.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
        a = youform.from_webhook(payload("s4", "Zed Quillfeather", "zed@zzzunique-domain.io", phone="9333333333"))
        self.assertEqual(youform.ingest(c, a), "new_partner"); c.commit()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM partners").fetchone()[0], n + 1)
        p = c.execute("SELECT * FROM partners WHERE email='zed@zzzunique-domain.io'").fetchone()
        self.assertEqual((p["stage"], p["source"], p["segment"]), ("Applied", "Youform inbound", "Individual")); c.close()

    def test_6_free_email_domain_not_a_match(self):
        c = db.connect()
        a = {"name": "Nobody Atall", "email": "someone@gmail.com", "phone": "", "company": ""}
        self.assertEqual(youform.find_candidates(c, a), []); c.close()

    def test_7_manual_link_and_dnc_note(self):
        c = db.connect()
        pid = c.execute("SELECT id FROM partners WHERE stage IN ('New','Contacted') AND id NOT IN (?,?) LIMIT 1", (self.p1["id"], self.p2["id"])).fetchone()[0]
        c.execute("UPDATE partners SET do_not_contact=1, dnc_reason='unsubscribed' WHERE id=?", (pid,))
        c.execute("INSERT INTO applications(external_id,received_at,name,email,status,created_at) VALUES('m1','2026-10-14T10:00:00','M One','m@m.io','review','x')"); aid = c.execute("SELECT id FROM applications WHERE external_id='m1'").fetchone()[0]
        youform.apply_link(c, aid, pid, "manual", "manual"); c.commit()
        self.assertEqual(c.execute("SELECT do_not_contact FROM partners WHERE id=?", (pid,)).fetchone()[0], 1)  # flag stays
        self.assertTrue(c.execute("SELECT 1 FROM activities WHERE partner_id=? AND body LIKE 'Applied despite%'", (pid,)).fetchone()); c.close()

    def test_8_signature(self):
        body = b'{"a":1}'; sig = hmac.new(b"sek", body, hashlib.sha256).hexdigest()
        self.assertTrue(youform.verify_signature(body, sig, "sek")); self.assertFalse(youform.verify_signature(body, "bad", "sek"))

    def test_9_relay_pull_acks_after_store(self):
        state = {"acked": None, "rows": [{"id": 5, "payload": payload("r1", "Relay Person", "relay@zzrelay-unique.io")},
                                         {"id": 6, "payload": payload("r2", "Wrong Form", "w@w-unique.io", form="other")}]}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                if q.get("token") != ["tok"]: out = {"ok": False, "error": "bad token"}
                elif q["action"] == ["pull"]: out = {"ok": True, "rows": state["rows"]}
                else: state["acked"] = q["ids"][0]; out = {"ok": True}
                b = json.dumps(out).encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        youform.save_cfg({"relay_url": f"http://127.0.0.1:{srv.server_port}/exec", "relay_token": "tok"})
        res = youform.pull_relay(); srv.shutdown(); srv.server_close()
        self.assertEqual(res["pulled"], 2); self.assertEqual(res["new_partner"], 1); self.assertEqual(state["acked"], "5,6")
        c = db.connect(); self.assertEqual(c.execute("SELECT COUNT(*) FROM applications WHERE external_id='r2'").fetchone()[0], 0); c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
