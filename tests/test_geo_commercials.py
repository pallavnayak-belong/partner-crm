"""Country/region classification + per-partner commercials."""
import os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "g.db"))
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")   # tests must never reach the real Google backup
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")   # tests must never write the real mailbox settings
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "g.db")
import db, geo, commercials, onboarding as ob, mail, server  # noqa: E402


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dbp = db.DB_PATH; db.DB_PATH = os.path.join(TMP, "g.db")
        db.init(); cls.c = db.connect()
        cls.c.execute("UPDATE partners SET country='India'"); cls.c.commit()      # temp copy: don't depend on which real partners are foreign today
        cls._dir = ob.DIR; ob.DIR = os.path.join(TMP, "onb")

    @classmethod
    def tearDownClass(cls):
        cls.c.commit(); cls.c.close(); db.DB_PATH = cls._dbp; ob.DIR = cls._dir

    def test_1_country_normalising_and_region(self):
        self.assertEqual([geo.norm_country(x) for x in ("", "india", "UAE", "uk", "U.S.A.", "singapore", "Kenya")], ["India", "India", "United Arab Emirates", "United Kingdom", "United States", "Singapore", "Kenya"])
        self.assertEqual((geo.region_of("India"), geo.region_of("in"), geo.region_of("Dubai"), geo.region_of(None)), ("Indian", "Indian", "Foreign", "Indian"))

    def test_2_add_foreign_partner_filter_and_dashboard(self):
        r = server.create_partner(self.c, {"name": "Gulf Wealth FZE", "country": "uae", "segment": "Corporate", "email": "a@gulfwealth.example"}); self.c.commit()
        self.assertEqual(self.c.execute("SELECT country FROM partners WHERE id=?", (r["id"],)).fetchone()[0], "United Arab Emirates")
        res = server.api("GET", "/api/partners", {"region": ["Foreign"]}, {})
        self.assertEqual([x["name"] for x in res["rows"]], ["Gulf Wealth FZE"]); self.assertEqual(res["rows"][0]["region"], "Foreign")
        res2 = server.api("GET", "/api/partners", {"region": ["Indian"], "limit": ["5000"]}, {})
        self.assertNotIn("Gulf Wealth FZE", [x["name"] for x in res2["rows"]])
        self.assertEqual(res["total"] + res2["total"], self.c.execute("SELECT COUNT(*) FROM partners").fetchone()[0])
        d = server.api("GET", "/api/dashboard", {}, {})
        self.assertEqual({x["k"]: x["n"] for x in d["regions"]}["Foreign"], 1); self.assertEqual(d["countries"][0]["k"], "United Arab Emirates")
        server.api("PATCH", f"/api/partners/{r['id']}", {}, {"country": "UK"})
        self.assertEqual(self.c.execute("SELECT country FROM partners WHERE id=?", (r["id"],)).fetchone()[0], "United Kingdom")
        self.__class__.fid = r["id"]

    def test_3_foreign_onboarding_checklist_and_relabel(self):
        self.c.commit(); ob.start(self.c, self.fid); self.c.commit()
        items = {i["key"]: i["label"] for i in ob.detail(self.c, self.fid)["items"]}
        self.assertIn("Tax ID", items["pan"]); self.assertIn("tax_cert", items); self.assertIn("aml", items)
        indian = self.c.execute("SELECT id FROM partners WHERE country='India' AND segment='Corporate' AND id NOT IN (SELECT partner_id FROM onboarding) LIMIT 1").fetchone()[0]
        ob.start(self.c, indian); self.c.commit()
        ii = {i["key"]: i["label"] for i in ob.detail(self.c, indian)["items"]}
        self.assertIn("PAN", ii["pan"]); self.assertNotIn("tax_cert", ii)
        server.api("PATCH", f"/api/partners/{indian}", {}, {"country": "Singapore"})  # becomes foreign mid-onboarding
        ii = {i["key"]: i["label"] for i in ob.detail(self.c, indian)["items"]}
        self.assertIn("Tax ID", ii["pan"]); self.assertIn("aml", ii)

    def test_4_template_region_pick(self):
        c = self.c
        base = c.execute("SELECT id FROM templates WHERE segment='' AND region='' ORDER BY id LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO templates(name,subject,plain,html,segment,region,updated_at) VALUES('Foreign intro','s','p','<p>h</p>','','Foreign',?)", (mail.now(),))
        c.execute("INSERT INTO templates(name,subject,plain,html,segment,region,updated_at) VALUES('Foreign corp','s','p','<p>h</p>','Corporate','Foreign',?)", (mail.now(),)); c.commit()
        fp = {"segment": "Individual", "country": "Singapore"}; fc = {"segment": "Corporate", "country": "Singapore"}; ic = {"segment": "Corporate", "country": "India"}
        self.assertEqual(mail.pick_template(c, fp)["name"], "Foreign intro")
        self.assertEqual(mail.pick_template(c, fc)["name"], "Foreign corp")
        self.assertEqual(mail.pick_template(c, ic)["id"], base)
        self.assertEqual(mail.merge("{{country}}", {**fp, "name": "X", "contact_person": "", "city": ""}), "Singapore")

    def test_5_commercials_free_text_validation_and_workflow(self):
        pid = self.fid
        with self.assertRaises(ValueError): ob.save_commercials(self.c, pid, {"effective_from": "2026-12-01", "valid_until": "2026-01-01"})
        with self.assertRaises(ValueError): ob.save_commercials(self.c, pid, {"payout_frequency": "Whenever"})
        with self.assertRaises(ValueError): ob.commercials_status(self.c, pid, "Agreed", "Pallav")        # nothing typed yet
        text = "GIFT City MFs: 0.4% trail p.a. on AUM\nGIFT Nifty futures: USD 25 per funded client\nFirst 24 months only"
        ob.save_commercials(self.c, pid, {"terms_text": text, "currency": "usd", "effective_from": "2026-11-01", "payout_frequency": "Quarterly", "payment_days": 30})
        com = commercials.get(self.c, pid); self.assertEqual((com["currency"], com["terms_text"]), ("USD", text))
        with self.assertRaises(ValueError) as e: ob.commercials_status(self.c, pid, "Agreed", "  ")
        self.assertIn("who confirmed", str(e.exception))
        ob.commercials_status(self.c, pid, "Proposed"); ob.commercials_status(self.c, pid, "Agreed", "Pallav")
        self.assertEqual(commercials.get(self.c, pid)["agreed_by"], "Pallav")
        log = [r[0] for r in self.c.execute("SELECT body FROM activities WHERE partner_id=? AND type='onboarding'", (pid,))]
        self.assertTrue(any("Commercials agreed (confirmed by Pallav)" in x and "0.4% trail p.a. on AUM" in x and "USD 25 per funded client" in x for x in log))
        r = ob.save_commercials(self.c, pid, {"terms_text": text + "\nExtra clause"})          # editing an Agreed set
        self.assertTrue(r["reset_to_draft"]); self.assertEqual(commercials.get(self.c, pid)["status"], "Draft")
        r = ob.save_commercials(self.c, pid, {"notes": "n"}); r = ob.save_commercials(self.c, pid, {"notes": "n"})
        self.assertFalse(r["changed"])

    def test_6_standard_text_copy_and_start(self):
        commercials.save(self.c, 0, {"currency": "INR", "payout_frequency": "Monthly", "terms_text": "25% revenue share on everything"}); self.c.commit()
        newp = self.c.execute("SELECT id FROM partners WHERE id NOT IN (SELECT partner_id FROM onboarding) LIMIT 1").fetchone()[0]
        ob.start(self.c, newp)
        com = commercials.get(self.c, newp)
        self.assertEqual((com["status"], com["based_on"], com["terms_text"]), ("Draft", "standard", "25% revenue share on everything"))
        ob.save_commercials(self.c, newp, {"terms_text": "35% revenue share, custom"})
        self.assertEqual(commercials.get(self.c, 0)["terms_text"], "25% revenue share on everything")  # standard text untouched
        other = self.c.execute("SELECT id FROM partners WHERE id NOT IN (SELECT partner_id FROM onboarding) LIMIT 1").fetchone()[0]
        ob.start(self.c, other); ob.commercials_copy(self.c, other, str(newp))
        self.assertEqual(commercials.get(self.c, other)["terms_text"], "35% revenue share, custom")
        self.assertEqual(commercials.get(self.c, other)["status"], "Draft"); self.assertEqual(commercials.get(self.c, other)["based_on"], f"partner:{newp}")
        never = self.c.execute("SELECT id FROM partners WHERE id NOT IN (SELECT partner_id FROM onboarding) LIMIT 1").fetchone()[0]
        with self.assertRaises(ValueError): ob.commercials_copy(self.c, other, str(never))   # that partner has no commercials to copy


if __name__ == "__main__":
    unittest.main()
