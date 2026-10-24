"""Onboarding unit: checklist, files, AppsFlyer link check, onboard gate."""
import os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "o.db"))

import sqlite3 as _s  # fixtures must not depend on how many real partners have been emailed
_c = _s.connect(os.path.join(TMP, "o.db")); _c.create_function("crm_actor", 0, lambda: "test fixture"); _ids = "SELECT id FROM partners WHERE email LIKE '%@%.%' AND do_not_contact=0 ORDER BY id LIMIT 40"
_c.execute(f"DELETE FROM emails WHERE partner_id IN ({_ids})"); _c.execute(f"DELETE FROM activities WHERE type IN ('email','stage') AND partner_id IN ({_ids})"); _c.execute(f"UPDATE partners SET stage='New', last_contacted=NULL WHERE id IN ({_ids})"); _c.commit(); _c.close()
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")   # tests must never reach the real Google backup
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")   # tests must never write the real mailbox settings
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "o.db")
os.environ["PARTNER_CRM_ONB_DIR"] = os.path.join(TMP, "onb")
os.environ["PARTNER_CRM_ATTR_CFG"] = os.path.join(TMP, "attr.json")
import db, onboarding as ob, attribution, server  # noqa: E402


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dbp = db.DB_PATH; db.DB_PATH = os.path.join(TMP, "o.db")
        db.init()
        cls.c = db.connect()
        cls._saved = (attribution.CFG_PATH, ob.DIR)
        attribution.CFG_PATH = os.path.join(TMP, "attr.json"); ob.DIR = os.path.join(TMP, "onb")
        attribution.save_cfg({"onelink_base": "https://belong.onelink.me/AbCd/partner", "media_source": "partner"})
        cls.pid = cls.c.execute("SELECT id FROM partners WHERE segment='Corporate' AND stage='New' LIMIT 1").fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls.c.commit(); cls.c.close(); db.DB_PATH = cls._dbp
        attribution.CFG_PATH, ob.DIR = cls._saved

    def test_1_start_seeds_by_segment_and_moves_stage(self):
        ob.start(self.c, self.pid)
        d = ob.detail(self.c, self.pid)
        keys = [i["key"] for i in d["items"]]
        self.assertIn("incorporation", keys); self.assertEqual(keys[-2:], ["agreement", "other"])
        self.assertEqual(self.c.execute("SELECT stage FROM partners WHERE id=?", (self.pid,)).fetchone()[0], "In discussion")
        self.assertTrue(ob.start(self.c, self.pid)["existing"])
        self.assertEqual(len(ob.detail(self.c, self.pid)["items"]), len(keys))

    def test_2_cannot_onboard_until_everything_done(self):
        with self.assertRaises(ValueError) as e: ob.complete(self.c, self.pid)
        self.assertIn("agreement", str(e.exception))
        with self.assertRaises(ValueError) as e: ob.patch_item(self.c, self.pid, "pan", {"status": "verified"})
        self.assertIn("upload", str(e.exception))
        self.c.commit()
        with self.assertRaises(ValueError): server.api("PATCH", f"/api/partners/{self.pid}", {}, {"stage": "Onboarded"})

    def test_3_files_safe_and_status_follows(self):
        r = ob.save_file(self.c, self.pid, "pan", "../../evil/PAN card.pdf", b"%PDF-1")
        d = ob.detail(self.c, self.pid); it = next(i for i in d["items"] if i["key"] == "pan")
        self.assertEqual(it["status"], "received"); self.assertEqual(it["files"][0]["orig_name"], "PAN card.pdf")
        fp, f = ob.file_path(self.c, r["id"]); self.assertTrue(fp.startswith(os.path.realpath(ob.DIR)))
        self.assertEqual(oct(os.stat(fp).st_mode & 0o777), "0o600")
        with self.assertRaises(ValueError): ob.save_file(self.c, self.pid, "pan", "x.html", b"<script>")
        with self.assertRaises(ValueError): ob.save_file(self.c, self.pid, "nope", "a.pdf", b"x")
        with self.assertRaises(ValueError): ob.save_file(self.c, self.pid, "pan", "a.pdf", b"x" * (ob.MAX_BYTES + 1))
        ob.delete_file(self.c, r["id"])
        self.assertFalse(os.path.exists(fp))
        self.assertEqual(next(i for i in ob.detail(self.c, self.pid)["items"] if i["key"] == "pan")["status"], "pending")

    def test_4_link_check_and_code_adoption(self):
        p = self.c.execute("SELECT * FROM partners WHERE id=?", (self.pid,)).fetchone()
        good = "https://belong.onelink.me/AbCd/partner?pid=partner&c=ACME_01"
        self.assertTrue(ob.check_link(good, p)["trackable"])
        bad = ob.check_link("https://belong.onelink.me/AbCd/partner?pid=other&c=ACME_01", p)
        self.assertFalse(bad["trackable"]); self.assertIn("media source", bad["issues"][0])
        self.assertFalse(ob.check_link("https://belong.onelink.me/AbCd/x", p)["trackable"])
        with self.assertRaises(ValueError): ob.patch(self.c, self.pid, {"appsflyer_link": "http://x.y"})
        ob.patch(self.c, self.pid, {"appsflyer_link": good})
        self.assertEqual(self.c.execute("SELECT partner_code FROM partners WHERE id=?", (self.pid,)).fetchone()[0], "ACME_01")
        other = self.c.execute("SELECT id FROM partners WHERE id<>? LIMIT 1", (self.pid,)).fetchone()[0]
        ob.start(self.c, other)
        with self.assertRaises(ValueError): ob.patch(self.c, other, {"appsflyer_link": good})

    def test_5_full_flow_complete_lock_reopen(self):
        pid = self.pid
        for it in ob.detail(self.c, pid)["items"]:
            if it["required"]:
                ob.save_file(self.c, pid, it["key"], it["key"] + ".pdf", b"doc")
                ob.patch_item(self.c, pid, it["key"], {"status": "verified"})
        self.assertEqual(ob.blockers(self.c, pid), ["Commercials not agreed with the partner"])
        ob.save_commercials(self.c, pid, {"effective_from": "2026-11-01", "payout_frequency": "Monthly", "terms_text": "30% revenue share on all products"})
        ob.commercials_status(self.c, pid, "Agreed", "Pallav")
        self.assertEqual(ob.blockers(self.c, pid), [])
        ob.complete(self.c, pid)
        self.assertEqual(self.c.execute("SELECT stage FROM partners WHERE id=?", (pid,)).fetchone()[0], "Onboarded")
        with self.assertRaises(ValueError): ob.save_file(self.c, pid, "other", "a.pdf", b"x")
        with self.assertRaises(ValueError): ob.patch(self.c, pid, {"status": "On hold"})                 # still locked
        ob.patch(self.c, pid, {"appsflyer_link": "https://a.b/?pid=partner&c=Z"})                     # but the link can be added or changed after onboarding
        self.assertEqual(self.c.execute("SELECT appsflyer_link FROM onboarding WHERE partner_id=?", (pid,)).fetchone()[0], "https://a.b/?pid=partner&c=Z")
        ob.reopen(self.c, pid, "re-check PAN")
        self.assertEqual(self.c.execute("SELECT stage FROM partners WHERE id=?", (pid,)).fetchone()[0], "In discussion")
        ob.patch_item(self.c, pid, "pan", {"status": "rejected", "note": "blurry"})

    def test_5b_no_link_is_not_a_blocker(self):
        other = self.c.execute("SELECT id FROM partners WHERE id<>? ORDER BY id LIMIT 1", (self.pid,)).fetchone()[0]
        if not self.c.execute("SELECT 1 FROM onboarding WHERE partner_id=?", (other,)).fetchone():
            ob.start(self.c, other); self.c.commit()
        self.assertFalse((self.c.execute("SELECT appsflyer_link FROM onboarding WHERE partner_id=?", (other,)).fetchone()[0] or "").strip())
        self.assertFalse([b for b in ob.blockers(self.c, other) if "AppsFlyer" in b])

    def test_6_custom_items_and_agreement_rules(self):
        k = ob.add_item(self.c, self.pid, {"label": "GST certificate", "required": False})["key"]
        ob.patch_item(self.c, self.pid, k, {"required": True})
        ob.delete_item(self.c, self.pid, k)
        with self.assertRaises(ValueError): ob.delete_item(self.c, self.pid, "pan")
        with self.assertRaises(ValueError): ob.patch_item(self.c, self.pid, "agreement", {"status": "na"})

    def test_7_list_and_candidates(self):
        r = ob.list_all(self.c); self.assertGreaterEqual(len(r["rows"]), 2)
        self.assertNotIn(self.pid, [x["id"] for x in ob.candidates(self.c)])


if __name__ == "__main__":
    unittest.main()


class DistributorCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dbp = db.DB_PATH; db.DB_PATH = os.path.join(TMP, "o.db"); db.init()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._dbp

    def test_codes(self):
        onboarding = ob
        c = db.connect()
        try:
            c.execute("UPDATE partners SET distributor_code=NULL")
            ids = [r[0] for r in c.execute("SELECT id FROM partners LIMIT 4")]
            c.execute("UPDATE partners SET country='Malaysia' WHERE id=?", (ids[0],))
            c.execute("UPDATE partners SET country='India', category='Community / influencer' WHERE id=?", (ids[1],))
            c.execute("UPDATE partners SET country='India', category='Indian Mutual Fund Distributors' WHERE id IN (?,?)", (ids[2], ids[3]))
            codes = [onboarding.assign_distributor_code(c, i, "2026-10-22T10:00:00") for i in ids]
            self.assertEqual(codes, ["FD1026001", "II1026001", "ID1026001", "ID1026002"])
            self.assertEqual(onboarding.assign_distributor_code(c, ids[0], "2026-11-01T10:00:00"), "FD1026001")   # never changes
            c.execute("UPDATE partners SET distributor_code=NULL WHERE id=?", (ids[0],))
            self.assertEqual(onboarding.assign_distributor_code(c, ids[0], "2026-11-01T10:00:00"), "FD1126001")
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):    # the database itself refuses a duplicate
                c.execute("UPDATE partners SET distributor_code=? WHERE id=?", (codes[2], ids[3]))
        finally:
            c.rollback()
            c.close()
