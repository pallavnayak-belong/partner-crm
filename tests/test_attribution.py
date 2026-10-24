"""Attribution tests with a fake ClickHouse client. Run: python3 -m unittest discover -s tests"""
import os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
def _clone(src, dst):
    import sqlite3 as _q
    a = _q.connect(src); b = _q.connect(dst); a.backup(b); a.close(); b.close()   # WAL-safe copy of the live database
_clone(os.path.join(HERE, "data", "partner_crm.db"), os.path.join(TMP, "a.db"))

import sqlite3 as _s  # fixtures must not depend on how many real partners have been emailed
_c = _s.connect(os.path.join(TMP, "a.db")); _c.create_function("crm_actor", 0, lambda: "test fixture"); _ids = "SELECT id FROM partners WHERE email LIKE '%@%.%' AND do_not_contact=0 ORDER BY id LIMIT 40"
_c.execute(f"DELETE FROM emails WHERE partner_id IN ({_ids})"); _c.execute(f"DELETE FROM activities WHERE type IN ('email','stage') AND partner_id IN ({_ids})"); _c.execute(f"UPDATE partners SET stage='New', last_contacted=NULL WHERE id IN ({_ids})"); _c.commit(); _c.close()
os.environ["PARTNER_CRM_GDRIVE_CFG"] = os.path.join(TMP, "gdrive.json")   # tests must never reach the real Google backup
os.environ["PARTNER_CRM_VAULT_KEY"] = os.path.join(TMP, "vault.json")
os.environ["PARTNER_CRM_YOUFORM_CFG"] = os.path.join(TMP, "youform.json")
os.environ["PARTNER_CRM_MAIL_CFG"] = os.path.join(TMP, "mail.json")   # tests must never write the real mailbox settings
os.environ["PARTNER_CRM_DB"] = os.path.join(TMP, "a.db")
import db, attribution, ch, mail  # noqa: E402

attribution.CFG_PATH = os.path.join(TMP, "attr.json")
mail.CFG_PATH = os.path.join(TMP, "mail.json")
import notify  # noqa: E402
notify.CFG_PATH = os.path.join(TMP, "slack.json")  # tests must never post to the real Slack channel


class FakeCH:
    def __init__(self, installs, milestones):
        self.installs, self.milestones, self.sqls = installs, milestones, []

    def query(self, sql):
        self.sqls.append(sql)
        if "Appsflyer Install Data" in sql:
            rows, self.installs = self.installs, []  # real CH returns each install once, in whichever code-chunk matches
            return rows
        for ev, rows in self.milestones.items():
            if f"'{ev}'" in sql:
                return rows
        return []


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(TMP, "a.db")   # each module uses its own temp copy even when run in one process
        db.init()
        c = db.connect()
        c.execute("DELETE FROM referred_customers"); c.execute("DELETE FROM customer_milestones")
        cls.p1, cls.p2 = [r[0] for r in c.execute("SELECT id FROM partners ORDER BY id LIMIT 2")]
        c.execute("UPDATE partners SET partner_code='' WHERE id=?", (cls.p1,))
        c.execute("UPDATE partners SET partner_code='Custom One' WHERE id=?", (cls.p2,))
        c.commit(); c.close()
        attribution.save_cfg({"onelink_base": "https://belong.onelink.me/AbCd/partner", "media_source": "partner"})

    def test_1_codes_and_links(self):
        c = db.connect(); p1 = c.execute("SELECT * FROM partners WHERE id=?", (self.p1,)).fetchone(); p2 = c.execute("SELECT * FROM partners WHERE id=?", (self.p2,)).fetchone(); c.close()
        self.assertEqual(attribution.code_for(p1), f"P{self.p1:04d}"); self.assertEqual(attribution.code_for(p2), "Custom One")
        self.assertEqual(attribution.link_for(p1), f"https://belong.onelink.me/AbCd/partner?pid=partner&c=P{self.p1:04d}")
        self.assertIn("c=Custom+One", attribution.link_for(p2))
        with self.assertRaises(ValueError): attribution.save_cfg({"onelink_base": "http://insecure.example"})
        with self.assertRaises(ValueError): attribution.save_cfg({"media_source": "bad'; DROP"})

    def test_2_sync_attributes_and_counts(self):
        f = FakeCH(
            installs=[{"customer_id": "CUS_a", "campaign": f"p{self.p1:04d}", "media_source": "partner", "first_seen": "2026-10-01 10:00:00"},   # case-insensitive
                      {"customer_id": "CUS_b", "campaign": f"P{self.p1:04d}", "media_source": "partner", "first_seen": "2026-10-02 10:00:00"},
                      {"customer_id": "CUS_c", "campaign": "Custom One", "media_source": "partner", "first_seen": "2026-10-03 10:00:00"},
                      {"customer_id": "CUS_x", "campaign": "unknown-campaign", "media_source": "partner", "first_seen": "2026-10-03 10:00:00"}],
            milestones={"auth.medium.registered": [{"cid": "CUS_a", "ts": "2026-10-01 10:05:00"}, {"cid": "CUS_b", "ts": "2026-10-02 10:05:00"}, {"cid": "CUS_c", "ts": "2026-10-03 10:05:00"}],
                        "verification.system.completed": [{"cid": "CUS_a", "ts": "2026-10-02 09:00:00"}],
                        "payment.lean.walletRecharge.completed": [{"cid": "CUS_a", "ts": "2026-10-04 09:00:00"}]})
        r = attribution.sync(client=f)
        self.assertEqual(r["installs"], 4)
        c = db.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM referred_customers").fetchone()[0], 3)  # unknown campaign ignored
        st = attribution.stats_for(c)
        self.assertEqual((st[self.p1]["installed"], st[self.p1]["signed_up"], st[self.p1]["kyc_done"], st[self.p1]["funded"], st[self.p1]["mf_txn"]), (2, 2, 1, 1, 0))
        self.assertEqual((st[self.p2]["installed"], st[self.p2]["signed_up"]), (1, 1))
        self.assertEqual(attribution.stats_for(c, self.p2).keys(), {self.p2})
        f.installs = [{"customer_id": "CUS_a", "campaign": f"p{self.p1:04d}", "media_source": "partner", "first_seen": "2026-10-01 10:00:00"}]
        attribution.sync(client=f)  # idempotent re-sync
        self.assertEqual(c.execute("SELECT COUNT(*) FROM referred_customers").fetchone()[0], 3)
        self.assertEqual(attribution.stats_for(c)[self.p1]["installed"], 2)
        self.assertTrue(all("media_source') = 'partner'" in s for s in f.sqls if "Appsflyer" in s))
        c.close()
        self.assertTrue(attribution.load_cfg()["last_sync"])

    def test_3_readonly_guard(self):
        cl = object.__new__(ch.Client)
        for bad in ("DROP TABLE x", "INSERT INTO t VALUES (1)", "SELECT 1; DROP TABLE x", "ALTER TABLE t DELETE WHERE 1"):
            with self.assertRaises(ch.CHError): cl.query(bad)

    def test_4_referral_link_merge_tag(self):
        c = db.connect()
        p = c.execute("SELECT * FROM partners WHERE id=?", (self.p1,)).fetchone()
        c.execute("INSERT INTO templates(name,subject,plain,html,segment,updated_at) VALUES('RL','S','Join: {{referral_link}}','<html><body><a href=\"{{referral_link}}\">go</a></body></html>','step','x')")
        tid = c.execute("SELECT MAX(id) FROM templates").fetchone()[0]
        r = mail.render(c, p, tid)
        self.assertIn(f"c=P{self.p1:04d}", r["plain"]); self.assertIn("onelink.me", r["html"])
        attribution.save_cfg({"onelink_base": ""})
        with self.assertRaises(ValueError): mail.render(c, p, tid)
        # sample partner (template editor / test send) still previews
        self.assertIn("example.onelink.me", mail.render(c, {"name": "S", "contact_person": "A B", "segment": "x", "city": "y"}, tid)["plain"])
        attribution.save_cfg({"onelink_base": "https://belong.onelink.me/AbCd/partner"})
        c.execute("DELETE FROM templates WHERE id=?", (tid,)); c.commit(); c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
