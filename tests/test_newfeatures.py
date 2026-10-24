"""Manual add-partner + template attachments."""
import os, shutil, sys, tempfile, time, unittest
from email import message_from_bytes

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "n.db"))
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")   # tests must never reach the real Google backup
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")   # tests must never write the real mailbox settings
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "n.db")
import db, mail, attachments, server  # noqa: E402


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dbp = db.DB_PATH; db.DB_PATH = os.path.join(TMP, "n.db")
        db.init(); cls.c = db.connect()
        cls._saved = attachments.DIR; attachments.DIR = os.path.join(TMP, "att")

    @classmethod
    def tearDownClass(cls):
        cls.c.commit(); cls.c.close(); db.DB_PATH = cls._dbp; attachments.DIR = cls._saved

    def test_1_add_partner_validation_and_duplicates(self):
        with self.assertRaises(ValueError): server.create_partner(self.c, {"name": ""})
        with self.assertRaises(ValueError): server.create_partner(self.c, {"name": "X", "email": "nope"})
        with self.assertRaises(ValueError): server.create_partner(self.c, {"name": "X", "segment": "Weird"})
        r = server.create_partner(self.c, {"name": "Acme Wealth LLP", "email": "hello@acmewealth.example", "phone": "+91 90909 11223", "segment": "Corporate", "source": "Event"})
        self.assertIn("id", r); pid = r["id"]
        p = self.c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
        self.assertEqual((p["stage"], p["source"], p["segment"]), ("New", "Event", "Corporate"))
        self.assertEqual(self.c.execute("SELECT type FROM activities WHERE partner_id=?", (pid,)).fetchone()[0], "added")
        d = server.create_partner(self.c, {"name": "acme  wealth llp.", "phone": "9090911223"})
        self.assertEqual(len(d["duplicates"]), 1); self.assertIn("same phone", d["duplicates"][0]["why"]); self.assertIn("same name", d["duplicates"][0]["why"])
        self.assertIn("id", server.create_partner(self.c, {"name": "acme wealth llp", "force": True}))

    def test_2_attachments_limits_and_send(self):
        tid = self.c.execute("SELECT id FROM templates WHERE segment='' LIMIT 1").fetchone()[0]
        with self.assertRaises(ValueError): attachments.add(self.c, 99999, "a.pdf", b"x")
        with self.assertRaises(ValueError): attachments.add(self.c, tid, "run.exe", b"x")
        with self.assertRaises(ValueError): attachments.add(self.c, tid, "big.pdf", b"x" * (attachments.MAX_FILE + 1))
        a = attachments.add(self.c, tid, "../Brochure 2026.pdf", b"%PDF-1.4 hello")
        self.assertEqual(a["name"], "Brochure 2026.pdf")
        attachments.add(self.c, tid, "terms.pdf", b"y" * (9 * 1024 * 1024))
        with self.assertRaises(ValueError) as e: attachments.add(self.c, tid, "more.pdf", b"z" * (8 * 1024 * 1024))
        self.assertIn("15 MB", str(e.exception))
        p = self.c.execute("SELECT * FROM partners WHERE email LIKE '%@%.%' LIMIT 1").fetchone()
        r = mail.render(self.c, p, tid)
        self.assertEqual([x["name"] for x in r["attachments"]], ["Brochure 2026.pdf", "terms.pdf"])
        msg = mail.build_message({"address": "p@getbelong.com", "from_name": "B"}, "to@x.com", r)
        parsed = message_from_bytes(msg.as_bytes())
        self.assertEqual(parsed.get_content_type(), "multipart/mixed")
        names = [part.get_filename() for part in parsed.walk() if part.get_filename() and part.get_content_disposition() == "attachment"]
        self.assertEqual(names, ["Brochure 2026.pdf", "terms.pdf"])
        self.assertTrue(any(part.get_content_type() == "text/html" for part in parsed.walk()))
        # no attachments -> unchanged structure
        for x in attachments.listing(self.c, tid): attachments.remove(self.c, x["id"])
        r2 = mail.render(self.c, p, tid)
        self.assertEqual(message_from_bytes(mail.build_message({"address": "p@getbelong.com", "from_name": "B"}, "to@x.com", r2).as_bytes()).get_content_type(), "multipart/related")
        self.assertEqual(os.listdir(os.path.join(attachments.DIR, str(tid))), [])

    def test_2b_remove_works_even_if_file_is_missing(self):
        tid = self.c.execute("SELECT id FROM templates WHERE segment='' LIMIT 1").fetchone()[0]
        a = attachments.add(self.c, tid, "gone.pdf", b"x")
        fp, _ = attachments.path_of(self.c, a["id"]); os.remove(fp)
        attachments.remove(self.c, a["id"])
        self.assertEqual([x for x in attachments.listing(self.c, tid) if x["name"] == "gone.pdf"], [])

    def test_3_duplicate_template_copies_attachments(self):
        tid = self.c.execute("SELECT id FROM templates WHERE segment='' LIMIT 1").fetchone()[0]
        attachments.add(self.c, tid, "one.pdf", b"1"); self.c.commit()
        r = server.mail_api(self.c, "POST", f"/api/templates/{tid}/duplicate", {})
        self.assertEqual([x["name"] for x in attachments.listing(self.c, r["id"])], ["one.pdf"])
        self.assertEqual(self.c.execute("SELECT segment FROM templates WHERE id=?", (r["id"],)).fetchone()[0], "step")
        server.mail_api(self.c, "DELETE", f"/api/templates/{r['id']}", {})
        self.assertFalse(os.path.exists(os.path.join(attachments.DIR, str(r["id"]))))


if __name__ == "__main__":
    unittest.main()
