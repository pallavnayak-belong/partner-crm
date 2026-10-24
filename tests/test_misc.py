"""Scoring + Slack notification tests."""
import json, os, shutil, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "m.db"))

import sqlite3 as _s  # fixtures must not depend on how many real partners have been emailed
_c = _s.connect(os.path.join(TMP, "m.db")); _c.create_function("crm_actor", 0, lambda: "test fixture"); _ids = "SELECT id FROM partners WHERE email LIKE '%@%.%' AND do_not_contact=0 ORDER BY id LIMIT 40"
_c.execute(f"DELETE FROM emails WHERE partner_id IN ({_ids})"); _c.execute(f"DELETE FROM activities WHERE type IN ('email','stage') AND partner_id IN ({_ids})"); _c.execute(f"UPDATE partners SET stage='New', last_contacted=NULL WHERE id IN ({_ids})"); _c.commit(); _c.close()
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")   # tests must never reach the real Google backup
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")   # tests must never write the real mailbox settings
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "m.db")
import db, scoring, notify  # noqa: E402

notify.CFG_PATH = os.path.join(TMP, "slack.json")


class T(unittest.TestCase):
    def test_1_scoring_orders_sensibly(self):
        base = {"kind": "MFD", "top_500": 0, "rank": None, "avg_aum_cr": 0, "sif_authorized": 0, "category": "", "classification": "Individual", "email": ""}
        big = {**base, "top_500": 1, "rank": 5, "avg_aum_cr": 50000, "sif_authorized": 1, "classification": "Corporate", "email": "a@b.co"}
        mid = {**base, "rank": 1500, "avg_aum_cr": 200, "classification": "Corporate", "email": "a@b.co"}
        tiny = {**base, "rank": 3100, "avg_aum_cr": 1, "email": ""}
        self.assertGreater(scoring.score(big), scoring.score(mid)); self.assertGreater(scoring.score(mid), scoring.score(tiny))
        self.assertLessEqual(scoring.score(big), 100); self.assertGreaterEqual(scoring.score(tiny), 0)
        pm = {**base, "kind": "SEBI", "category": "SEBI - Portfolio Managers", "classification": "Corporate", "email": "x@y.in"}
        ra = {**pm, "category": "SEBI - Research Analyst"}
        self.assertGreater(scoring.score(pm), scoring.score(ra))

    def test_2_compute_all_fills_and_is_idempotent(self):
        db.init(); c = db.connect()
        c.execute("UPDATE universe SET score=NULL")
        n = scoring.compute_all(c); c.commit()
        self.assertEqual(n, c.execute("SELECT COUNT(*) FROM universe").fetchone()[0])
        self.assertEqual(scoring.compute_all(c), 0)
        top = c.execute("SELECT name, score FROM universe ORDER BY score DESC LIMIT 1").fetchone()
        self.assertGreater(top["score"], 60); c.close()

    def test_3_notify_posts_json_and_validates(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))); self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.assertFalse(notify.send("nothing configured", wait=True))           # unconfigured = silent no-op
        with self.assertRaises(ValueError): notify.save_cfg("https://evil.example.com/hook")
        with open(notify.CFG_PATH, "w") as f: json.dump({"webhook_url": f"http://127.0.0.1:{srv.server_port}/x"}, f)
        self.assertTrue(notify.send("hello", wait=True)); srv.shutdown(); srv.server_close()
        self.assertEqual(got, [{"text": "hello"}])
        with open(notify.CFG_PATH, "w") as f: json.dump({"webhook_url": "http://127.0.0.1:1/dead"}, f)
        self.assertFalse(notify.send("dead endpoint", wait=True))                 # failures never raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
