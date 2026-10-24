"""Partner attribution: referral links (AppsFlyer OneLink) + per-partner client performance from ClickHouse."""
import json
import os
import re
import urllib.parse
from datetime import datetime

import db

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("PARTNER_CRM_ATTR_CFG", os.path.join(HERE, "data", "attribution.json"))
now = lambda: datetime.now().isoformat(timespec="seconds")

# funnel order matters; each milestone = (label, source table, [event names])
MILESTONES = [
    ("signed_up", "Signed up", "kafka", ["auth.medium.registered"]),
    ("kyc_done", "KYC completed", "kafka", ["verification.system.completed"]),
    ("activated", "Account activated", "kafka", ["belongAdminBff.verification.activation.completed", "belongCustomerCareBff.verification.activation.completed"]),
    ("funded", "Funded wallet", "kafka", ["payment.lean.walletRecharge.completed", "payment.glomopay.walletRecharge.completed"]),
    ("mf_txn", "MF transaction", "wkafka", ["mf.transaction.updated"]),
]
STEPS = [("installed", "Installed")] + [(k, label) for k, label, _, _ in MILESTONES]
SAFE = re.compile(r"^[A-Za-z0-9 ._\-]{1,64}$")


# ---------------- config ----------------
def load_cfg():
    cfg = {"onelink_base": "", "media_source": "partner", "last_sync": "", "last_sync_result": ""}
    try:
        with open(CFG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(upd):
    cfg = load_cfg()
    for k in ("onelink_base", "media_source", "last_sync", "last_sync_result"):
        if k in upd:
            cfg[k] = str(upd[k]).strip()
    if cfg["onelink_base"] and not re.match(r"^https://[^\s?#]+$", cfg["onelink_base"]):
        raise ValueError("OneLink base must be an https:// URL without query parameters, e.g. https://belong.onelink.me/AbCd/partner")
    if cfg["media_source"] and not SAFE.match(cfg["media_source"]):
        raise ValueError("media source (or leave empty to count any source): letters, numbers, space . _ - only")
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    fd = os.open(CFG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return cfg


# ---------------- codes + links ----------------
def code_for(p):
    c = (p["partner_code"] or "").strip() if "partner_code" in p.keys() else ""
    return c or f"P{p['id']:04d}"


def is_path_style(base):
    """Branded short links (https://app.getbelong.com/<template>/<Code>) carry the partner code as the last path segment, not ?c=."""
    return urllib.parse.urlparse(base or "").netloc.lower() == "app.getbelong.com"


def link_for(p, cfg=None):
    cfg = cfg or load_cfg()
    if not cfg["onelink_base"]:
        return ""
    if is_path_style(cfg["onelink_base"]):
        if not (p["partner_code"] or "").strip():
            return ""        # no real page exists for a default code like P0123
        return f"{cfg['onelink_base'].rstrip('/')}/{urllib.parse.quote(code_for(p))}"
    q = urllib.parse.urlencode({"pid": cfg["media_source"], "c": code_for(p)})
    return f"{cfg['onelink_base']}?{q}"


# ---------------- sync from ClickHouse ----------------
def _in(values):
    return ",".join("'" + v.replace("'", "") + "'" for v in values)


def sync(client=None):
    import ch
    cfg = load_cfg()
    c = db.connect()
    try:
        partners = c.execute("SELECT id, partner_code FROM partners").fetchall()
        codes = {code_for(p).lower(): p["id"] for p in partners if SAFE.match(code_for(p))}
        ms = cfg["media_source"]
        client = client or ch.Client()
        ms_filter = f"AND JSONExtractString(event_data,'media_source') = '{ms}'" if ms else ""
        installs = []
        code_list = list(codes)
        for i in range(0, len(code_list), 300):
            chunk = code_list[i:i + 300]
            installs += client.query(f"""
                SELECT customer_id,
                       argMin(JSONExtractString(event_data,'campaign'), event_time) AS campaign,
                       argMin(JSONExtractString(event_data,'media_source'), event_time) AS media_source,
                       toString(min(event_time)) AS first_seen
                FROM clickstream_events
                WHERE event_name='Appsflyer Install Data' AND customer_id LIKE 'CUS_%'
                  AND JSONExtractString(event_data,'af_status')='Non-organic' {ms_filter}
                  AND lowerUTF8(JSONExtractString(event_data,'campaign')) IN ({_in(chunk)})
                GROUP BY customer_id LIMIT 100000""")
        ts = now()
        for r in installs:
            pid = codes.get((r["campaign"] or "").lower())
            if pid:
                c.execute("""INSERT INTO referred_customers(customer_id,partner_id,media_source,campaign,installed_at,synced_at) VALUES(?,?,?,?,?,?)
                             ON CONFLICT(customer_id) DO UPDATE SET partner_id=excluded.partner_id, media_source=excluded.media_source, campaign=excluded.campaign, synced_at=excluded.synced_at""",
                          (r["customer_id"], pid, r["media_source"], r["campaign"], r["first_seen"], ts))
        custs = [r[0] for r in c.execute("SELECT customer_id FROM referred_customers")]
        found = 0
        for i in range(0, len(custs), 400):
            chunk = custs[i:i + 400]
            for key, label, table, events in MILESTONES:
                tbl = "kafka_event_details" if table == "kafka" else "wkafka_event_details"
                rows = client.query(f"""SELECT customerId AS cid, toString(min(eventTime)) AS ts FROM {tbl}
                                        WHERE customerId IN ({_in(chunk)}) AND eventName IN ({_in(events)}) GROUP BY customerId LIMIT 100000""")
                for r in rows:
                    c.execute("INSERT OR REPLACE INTO customer_milestones(customer_id,milestone,ts) VALUES(?,?,?)", (r["cid"], key, r["ts"]))
                    found += 1
        c.commit()
        result = f"{len(installs)} attributed installs across {len({r['campaign'].lower() for r in installs})} partner links; {found} milestones"
    finally:
        c.close()
    save_cfg({"last_sync": ts, "last_sync_result": result})
    return {"installs": len(installs), "customers": len(custs), "milestones": found, "summary": result}


# ---------------- stats ----------------
def stats_for(c, partner_id=None):
    """Per-partner funnel counts: installed -> milestones. Returns {partner_id: {step: n}}."""
    out = {}
    q = "SELECT partner_id, COUNT(*) FROM referred_customers" + (" WHERE partner_id=?" if partner_id else "") + " GROUP BY partner_id"
    for pid, n in c.execute(q, (partner_id,) if partner_id else ()):
        out[pid] = {"installed": n, **{k: 0 for k, *_ in MILESTONES}}
    q = """SELECT r.partner_id, m.milestone, COUNT(*) FROM customer_milestones m JOIN referred_customers r ON r.customer_id=m.customer_id""" + \
        (" WHERE r.partner_id=?" if partner_id else "") + " GROUP BY r.partner_id, m.milestone"
    for pid, ms, n in c.execute(q, (partner_id,) if partner_id else ()):
        if pid in out:
            out[pid][ms] = n
    return out


# ---------------- background sync job ----------------
import threading
JOB = {"state": "idle", "error": "", "result": None}


def start_sync():
    if JOB["state"] == "running":
        return JOB
    JOB.update(state="running", error="", result=None)

    def run():
        try:
            JOB["result"] = sync()
            JOB["state"] = "done"
        except Exception as e:  # noqa
            JOB["error"] = str(e)[:300]
            JOB["state"] = "error"
    threading.Thread(target=run, daemon=True).start()
    return JOB
