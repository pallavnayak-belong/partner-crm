"""Builds the tables mirrored to the Google Sheet. Only business data: never passwords, tokens, or document contents."""
from collections import OrderedDict

import attribution
import commercials
import geo
import onboarding

CELL_MAX = 3000


def cell(v):
    if v is None:
        return ""
    s = str(v)
    if s.startswith("="):          # never let partner-entered text run as a spreadsheet formula
        s = "'" + s
    return s[:CELL_MAX]


def yn(v):
    return "Yes" if v else "No"


def doc_folder(pid, name):
    """Friendly Drive folder for a partner's documents; the leading '<id> - ' lets the relay find it again for erasure."""
    clean = "".join(ch if ch not in '/\\:*?"<>|' else "_" for ch in (name or "partner")).strip()[:60]
    return f"{pid} - {clean}"


def tables(c):
    out = OrderedDict()
    names = {r[0]: r[1] for r in c.execute("SELECT id,name FROM partners")}
    tags = {}
    for pid, t in c.execute("SELECT pt.partner_id, t.name FROM partner_tags pt JOIN tags t ON t.id=pt.tag_id ORDER BY t.name"):
        tags.setdefault(pid, []).append(t)

    rows = []
    for p in c.execute("SELECT * FROM partners ORDER BY id"):
        rows.append([p["id"], p["name"], p["category"], p["segment"], geo.region_of(p["country"]), geo.norm_country(p["country"]), p["city"], p["stage"], p["owner"],
                     ", ".join(tags.get(p["id"], [])), p["contact_person"], p["designation"], p["email"], p["phone"], p["last_contacted"], p["follow_up"],
                     p["lost_reason"], yn(p["do_not_contact"]), yn(p["archived"]), p["source"], (p["created_at"] or "")[:10], p["website"], p["linkedin"], attribution.code_for(p), p["distributor_code"]])
    out["Partners"] = (["ID", "Name", "Type", "Segment", "Region", "Country", "City", "Stage", "Owner", "Tags", "Main contact", "Designation", "Email", "Phone", "Last contacted",
                        "Next follow-up", "Lost reason", "Do not contact", "Archived", "Source", "Added on", "Website", "LinkedIn", "Referral code", "Distributor code"], rows)

    out["Contacts"] = (["Partner ID", "Partner", "Name", "Role", "Designation", "Email", "Phone", "Bounced"],
                       [[k["partner_id"], names.get(k["partner_id"]), k["name"], k["role"], k["designation"], k["email"], k["phone"], yn(k["bounced"])]
                        for k in c.execute("SELECT * FROM partner_contacts ORDER BY partner_id,id")])

    # column order matters: the Dashboard tab queries A..E of Activities and A..F of Tasks
    out["Activities"] = (["Partner", "When", "Type", "By", "Note", "Partner ID"],
                         [[names.get(a["partner_id"]), (a["ts"] or "").replace("T", " ")[:16], a["type"], a["actor"], (a["body"] or "")[:500], a["partner_id"]]
                          for a in c.execute("SELECT * FROM activities WHERE type<>'draft' ORDER BY ts, id")])

    tmpl = {r["id"]: r["name"] for r in c.execute("SELECT id,name FROM templates")}
    out["Emails"] = (["Partner", "To", "Cc", "Subject", "Sent at", "Status", "Step", "Template", "Error", "Partner ID"],
                     [[names.get(e["partner_id"]), e["to_addr"], e["cc"], e["subject"], (e["sent_at"] or "").replace("T", " ")[:16], e["status"], e["step"], tmpl.get(e["template_id"]), e["error"], e["partner_id"]]
                      for e in c.execute("SELECT * FROM emails ORDER BY id")])

    stages = {r[0]: r[1] for r in c.execute("SELECT id,stage FROM partners")}
    out["Tasks"] = (["Partner", "Task", "Assignee", "Due", "Stage", "Done", "Partner ID"],
                    [[names.get(t["partner_id"]), t["title"], t["assignee"], t["due"], stages.get(t["partner_id"]), yn(t["done_at"]), t["partner_id"]]
                     for t in c.execute("SELECT * FROM tasks ORDER BY (done_at IS NOT NULL), due, id")])

    ob_rows, cm_rows = [], []
    for o in c.execute("SELECT o.*, p.name, p.country FROM onboarding o JOIN partners p ON p.id=o.partner_id ORDER BY o.partner_id"):
        pg = onboarding.progress(c, o["partner_id"])
        com = commercials.get(c, o["partner_id"])
        has_ag = c.execute("SELECT 1 FROM onboarding_files WHERE partner_id=? AND item_key='agreement'", (o["partner_id"],)).fetchone()
        ob_rows.append([o["partner_id"], o["name"], geo.norm_country(o["country"]), o["entity_type"], o["status"], f"{pg['done']}/{pg['total']}", yn(has_ag),
                        "Yes" if (o["appsflyer_link"] or "").strip() else "No", com["status"], (o["started_at"] or "")[:10], (o["onboarded_at"] or "")[:10], o["reviewer"], o["notes"]])
        cm_rows.append([o["partner_id"], o["name"], com["status"], com.get("terms_text"), com.get("currency"), com.get("effective_from"), com.get("valid_until"),
                        com.get("payout_frequency"), com.get("payment_days"), com.get("min_payout"), com.get("tax_note"), com.get("notes"), com.get("agreed_by"), (com.get("agreed_on") or "")[:10]])
    out["Onboarding"] = (["Partner ID", "Partner", "Country", "Type", "Status", "Documents verified", "Agreement uploaded", "AppsFlyer link", "Commercials", "Started", "Onboarded on", "Reviewer", "Notes"], ob_rows)
    out["Commercials"] = (["Partner ID", "Partner", "Status", "Commercials", "Currency", "Effective from", "Valid until", "Payout", "Paid within (days)", "Minimum payout", "Tax / TDS", "Other terms", "Agreed by", "Agreed on"], cm_rows)

    labels = {(r["partner_id"], r["key"]): r["label"] for r in c.execute("SELECT partner_id,key,label FROM onboarding_items")}
    out["Documents"] = (["Partner ID", "Partner", "Item", "File", "Size (KB)", "Uploaded"],
                        [[f["partner_id"], names.get(f["partner_id"]), labels.get((f["partner_id"], f["item_key"]), f["item_key"]), f["orig_name"], round(f["size"] / 1024), (f["uploaded_at"] or "")[:10]]
                         for f in c.execute("SELECT * FROM onboarding_files ORDER BY partner_id,id")])

    out["Applications"] = (["Received", "Name", "Email", "Phone", "Company", "Applying as", "Clients", "Products", "Status", "Partner"],
                           [[(a["received_at"] or "")[:16], a["name"], a["email"], a["phone"], a["company"], a["applying_as"], a["clients"], a["products"], a["status"], names.get(a["partner_id"])]
                            for a in c.execute("SELECT * FROM applications ORDER BY id")])

    seg = {"": "Everyone", "step": "Follow-ups / manual"}
    att = {}
    for a in c.execute("SELECT template_id, COUNT(*) n FROM template_attachments GROUP BY 1"):
        att[a["template_id"]] = a["n"]
    out["Templates"] = (["Name", "Subject", "For", "Region", "Attachments", "Text"],
                        [[t["name"], t["subject"], seg.get(t["segment"], t["segment"]), t["region"] or "Any", att.get(t["id"], 0), t["plain"]] for t in c.execute("SELECT * FROM templates ORDER BY id")])

    perf = []
    st = attribution.stats_for(c)
    for pid, v in st.items():
        p = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
        if p:
            perf.append([p["name"], attribution.code_for(p), v.get("installed", 0), v.get("signed_up", 0), v.get("kyc_done", 0), v.get("activated", 0), v.get("funded", 0), v.get("mf_txn", 0)])
    out["Performance"] = (["Partner", "Code", "Installed", "Signed up", "KYC done", "Activated", "Funded", "MF transaction"], perf)

    out["Team"] = (["Name"], [[m["name"]] for m in c.execute("SELECT name FROM team_members ORDER BY name")])
    return OrderedDict((tab, (h, [[cell(v) for v in r] for r in rs])) for tab, (h, rs) in out.items())
