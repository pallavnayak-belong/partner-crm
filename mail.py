"""Email engine: templates, SMTP send (Gmail app password), IMAP reply/bounce detection."""
import base64
import email
import hashlib
import email.utils
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from email import encoders, policy
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import attachments
import db
import geo

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("PARTNER_CRM_MAIL_CFG", os.path.join(HERE, "data", "mail.json"))
ASSETS = os.path.join(HERE, "assets")
SRC_TEMPLATE = os.path.expanduser("~/Documents/claude-misc/distributor-scrape/partner-outreach/EmailTemplate.html")
DEFAULTS = {"address": "", "password": "", "from_name": "Belong Partnerships", "daily_cap": 100, "delay": 3,
            "smtp_host": "smtp.gmail.com", "imap_host": "imap.gmail.com"}
IMG_RE = re.compile(r'cid:(img\d+)')
now = lambda: datetime.now().isoformat(timespec="seconds")

DEFAULT_PLAIN = """Hi {{first_name}},

Belong would like to invite you to partner with us in making GIFT City investments accessible to your network.

Belong is the only full-stack, IFSCA-regulated investment platform offering a unified suite of GIFT City and global investment products for NRIs, OCIs and resident Indians — India-focussed and global mutual funds, AIF strategies, and GIFT Nifty Futures.

As a Belong Affiliate Partner you get access to this product suite, a seamless investor onboarding experience for your clients, and revenue-sharing-based commercials. We are opening the Belong Affiliate Partner Programme to a limited set of partners.

Partner with us: https://app.youform.com/forms/geok4bom

Regards,
Team Belong"""


# ---------------- config ----------------
def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(upd):
    cfg = load_cfg()
    for k in ("address", "from_name", "smtp_host", "imap_host"):
        if k in upd:
            cfg[k] = str(upd[k]).strip()
    for k in ("daily_cap", "delay"):
        if k in upd:
            cfg[k] = max(0, int(upd[k]))
    if upd.get("password"):
        cfg["password"] = str(upd["password"]).replace(" ", "")
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    fd = os.open(CFG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return cfg


def public_cfg(c):
    cfg = load_cfg()
    sent_today = c.execute("SELECT COUNT(*) FROM emails WHERE status='sent' AND sent_at>=?", (date.today().isoformat(),)).fetchone()[0]
    return {"configured": bool(cfg["address"] and cfg["password"]), "address": cfg["address"], "from_name": cfg["from_name"],
            "daily_cap": cfg["daily_cap"], "delay": cfg["delay"], "sent_today": sent_today, "has_password": bool(cfg["password"])}


def test_connection():
    cfg = load_cfg()
    out = {}
    if not valid_email(cfg["address"]):
        msg = "Email address is empty or invalid \u2014 type it into the Email field and save"
        return {"smtp": msg, "imap": msg}
    if not cfg["password"]:
        msg = "No app password saved"
        return {"smtp": msg, "imap": msg}
    try:
        with smtplib.SMTP_SSL(cfg["smtp_host"], 465, timeout=20) as s:
            s.login(cfg["address"], cfg["password"])
        out["smtp"] = "ok"
    except Exception as e:  # noqa
        out["smtp"] = repr(e)
    try:
        m = imaplib.IMAP4_SSL(cfg["imap_host"], timeout=20)
        m.login(cfg["address"], cfg["password"])
        m.logout()
        out["imap"] = "ok"
    except Exception as e:  # noqa
        out["imap"] = repr(e)
    return out


# ---------------- templates ----------------
def seed_templates(c):
    if c.execute("SELECT COUNT(*) FROM templates").fetchone()[0]:
        return
    try:
        html = open(SRC_TEMPLATE, encoding="utf-8").read().replace("<?= firstName ?>", "{{first_name}}")
    except OSError:
        html = "<p>Hi {{first_name}},</p>" + "".join(f"<p>{escape(p)}</p>" for p in DEFAULT_PLAIN.split("\n\n")[1:])
    c.execute("INSERT INTO templates(name,subject,plain,html,segment,updated_at) VALUES(?,?,?,?,?,?)",
              ("Affiliate Partner Programme (default)", "Partner with Belong. Grow together.", DEFAULT_PLAIN, html, "", now()))


def first_name(p):
    cp = (p["contact_person"] or "").strip()
    if cp:
        return cp.split()[0].title() if cp.isupper() else cp.split()[0]
    if p["segment"] == "Individual":
        tok = (p["name"] or "").split()
        if tok and re.fullmatch(r"[A-Za-z]{2,}", tok[0]):
            return tok[0].title()
    return "there"


def referral_link(p):
    import attribution
    try:
        p["id"]
    except (KeyError, IndexError):
        return "https://example.onelink.me/partner?pid=partner&c=P0000"  # sample preview
    return attribution.link_for(p)


def merge(text, p, html=False):
    vals = {"first_name": first_name(p), "name": p["name"] or "", "city": p["city"] or "", "country": (geo.norm_country(p["country"]) if "country" in p.keys() else ""), "referral_link": referral_link(p) if "referral_link" in text else "",
            "distributor_code": (p["distributor_code"] if "distributor_code" in p.keys() else "ID1026001") or ""}
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: (escape(vals.get(m.group(1), "")) if html else vals.get(m.group(1), "")), text)


def pick_template(c, p, template_id=None):
    """Most specific template wins: matches this partner's segment and region (Indian / Foreign) beat segment-only, region-only, then the default."""
    if template_id:
        return c.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
    seg = (p["segment"] or "~") if "segment" in p.keys() else "~"
    reg = geo.region_of(p["country"] if "country" in p.keys() else "India")
    return c.execute("""SELECT * FROM templates WHERE segment<>'step' AND segment IN (?, '') AND region IN (?, '')
                        ORDER BY (segment<>'')*2 + (region<>'') DESC, id LIMIT 1""", (seg, reg)).fetchone()


UNSUB_PLAIN = "\n\n--\nIf you'd prefer not to receive further emails from us, just reply with \"unsubscribe\" and we won't contact you again."
UNSUB_HTML = ('<div style="max-width:600px;margin:14px auto 0;padding:0 16px;font:11px/1.5 Arial,sans-serif;color:#8a8a8a;text-align:center">'
              'If you\'d prefer not to receive further emails from us, just reply with \u201cunsubscribe\u201d and we won\'t contact you again.</div>')
UNSUB_RE = re.compile(r"\b(unsubscribe|remove me|take me off|stop (emailing|sending|mailing|contacting)|do not (contact|email)|don'?t (contact|email))\b", re.I)


def with_footer(plain, html):
    plain = plain.rstrip() + UNSUB_PLAIN
    i = html.lower().rfind("</body>")
    return plain, (html[:i] + UNSUB_HTML + html[i:]) if i != -1 else html + UNSUB_HTML


def render(c, p, template_id=None):
    t = pick_template(c, p, template_id)
    if not t:
        raise ValueError("no template")
    if "{{referral_link}}" in (t["plain"] + t["html"]).replace(" ", "") and not referral_link(p):
        raise ValueError("this template uses {{referral_link}} but this partner has no link yet: set the OneLink base in Settings \u2192 Partner attribution, and give the partner a code or link")
    if "{{distributor_code}}" in (t["plain"] + t["html"] + t["subject"]).replace(" ", "") and "distributor_code" in p.keys() and not (p["distributor_code"] or "").strip():
        raise ValueError("this template uses {{distributor_code}} but this partner has none yet (it is assigned when onboarding is completed)")
    plain, html = with_footer(merge(t["plain"], p), merge(t["html"], p, html=True))
    return {"template_id": t["id"], "template_name": t["name"], "subject": merge(t["subject"], p), "plain": plain, "html": html,
            "attachments": attachments.listing(c, t["id"]), "_files": attachments.files_for(c, t["id"])}


DATA_IMG_RE = re.compile(r"""(src\s*=\s*)(["'])data:image/(png|jpe?g|gif);base64,([A-Za-z0-9+/=\s]+)\2""", re.I)


def inline_data_images(html):
    """Gmail, Outlook and most mail apps refuse <img src="data:...">, so pictures pasted into a template show as broken boxes.
    Turn each into an inline attachment referenced by cid:, which every client displays."""
    parts = {}

    def swap(mo):
        raw = base64.b64decode("".join(mo.group(4).split()))
        key = hashlib.md5(raw).hexdigest()[:16]
        parts.setdefault(key, (raw, mo.group(3).lower().replace("jpg", "jpeg")))
        return f'{mo.group(1)}{mo.group(2)}cid:inline_{key}{mo.group(2)}'
    return DATA_IMG_RE.sub(swap, html), parts


def upload_seconds(file_bytes):
    """How long one email realistically takes to upload (6.6 MB took ~52 s on a normal connection)."""
    return int(8 + file_bytes * 1.34 / 100_000)


def build_message(cfg, to, r, cc=None):
    html_out, inline = inline_data_images(r["html"])
    r = {**r, "html": html_out}
    body = MIMEMultipart("related")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(r["plain"], "plain", "utf-8"))
    alt.attach(MIMEText(r["html"], "html", "utf-8"))
    body.attach(alt)
    for cid in sorted(set(IMG_RE.findall(r["html"]))):
        for ext in ("png", "jpg"):
            fp = os.path.join(ASSETS, f"{cid}.{ext}")
            if os.path.exists(fp):
                with open(fp, "rb") as fh:
                    img = MIMEImage(fh.read(), _subtype="jpeg" if ext == "jpg" else "png")
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=os.path.basename(fp))
                body.attach(img)
                break
    for key, (raw, sub) in inline.items():
        img = MIMEImage(raw, _subtype=sub)
        img.add_header("Content-ID", f"<inline_{key}>")
        img.add_header("Content-Disposition", "inline", filename=f"image_{key[:6]}.{sub}")
        body.attach(img)
    files = r.get("_files") or []
    if files:
        msg = MIMEMultipart("mixed")
        msg.attach(body)
        for name, fp, _ in files:
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            maintype, _, subtype = ctype.partition("/")
            with open(fp, "rb") as fh:
                part = MIMEBase(maintype, subtype)
                part.set_payload(fh.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=name)
            msg.attach(part)
    else:
        msg = body
    domain = cfg["address"].split("@")[-1] or "localhost"
    msg["Message-ID"] = email.utils.make_msgid(idstring=uuid.uuid4().hex[:12], domain=domain)
    msg["From"] = email.utils.formataddr((cfg["from_name"], cfg["address"]))
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = r["subject"]
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["List-Unsubscribe"] = f"<mailto:{cfg['address']}?subject=unsubscribe>"
    return msg


def resolve_cc(c, requested, to=""):
    """Only addresses saved as CC contacts can be copied; anything else is rejected. Never CCs the recipient."""
    saved = {r["email"].lower(): r["email"] for r in c.execute("SELECT email FROM cc_contacts")}
    out = []
    for a in requested or []:
        k = str(a).strip().lower()
        if k not in saved:
            raise ValueError(f"{a} isn't a saved CC contact")
        if k != (to or "").strip().lower() and saved[k] not in out:
            out.append(saved[k])
    return out


def cc_contacts(c):
    return [dict(r) for r in c.execute("SELECT id,label,email,auto FROM cc_contacts ORDER BY LOWER(label)")]


def send_timeout(msg):
    """Seconds allowed for the upload: scales with message size (a 6.6 MB email took ~50 s on a normal connection; a flat 30 s made it fail)."""
    return max(60, int(len(msg.as_bytes()) / 40000))


def explain_send_error(e, size_mb=0):
    import socket
    root = e.__cause__ or e.__context__ or e
    if isinstance(root, socket.timeout) or "timed out" in repr(root):
        return f"Upload to Gmail timed out. This email is about {size_mb:.1f} MB with attachments, which is slow on your connection. Try fewer or smaller attachments, or link to the files instead."
    return repr(e)[:300]


def valid_email(a):
    return bool(re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}", (a or "").strip()))


def send_test(c, to, partner_id=None, template_id=None):
    cfg = load_cfg()
    if not (cfg["address"] and cfg["password"]):
        raise ValueError("mail not configured")
    p = c.execute("SELECT * FROM partners WHERE id=?", (partner_id,)).fetchone() if partner_id else \
        {"name": "Sample Partner", "contact_person": "Alex Sample", "segment": "Corporate", "city": "Mumbai", "country": "India"}
    r = render(c, p, template_id)
    r["subject"] = "[TEST] " + r["subject"]
    msg = build_message(cfg, to, r)
    with smtplib.SMTP_SSL(cfg["smtp_host"], 465, timeout=30) as s:
        try:
            s.login(cfg["address"], cfg["password"])
        except (smtplib.SMTPException, OSError):
            raise ValueError("Couldn't log in to the mailbox. Check the app password in Settings → Mailbox, then use Test connection.")
        if getattr(s, "sock", None):
            s.sock.settimeout(send_timeout(msg))
        try:
            s.send_message(msg)
        except smtplib.SMTPException as e:
            raise ValueError(explain_send_error(e, sum(f[2] for f in r.get("_files", [])) * 1.34 / 1e6))
    return {"sent_to": to}


# ---------------- bulk send job ----------------
JOBS = {}


def _job_label(template_id):
    if not template_id:
        return "Auto (by segment)"
    try:
        with db.connect() as c:
            r = c.execute("SELECT name FROM templates WHERE id=?", (template_id,)).fetchone()
        return r["name"] if r else f"template {template_id}"
    except Exception:  # noqa
        return ""


def _log_job_start(jid, kind, total, template_id, label=None):
    try:
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO send_jobs(id,started,kind,by,template,total,state) VALUES(?,?,?,?,?,?,'running')",
                      (jid, now(), kind, db.current_actor(), label if label is not None else _job_label(template_id), total))
    except Exception:  # noqa
        pass


def _log_job_end(job):
    """Persist the outcome so there is a permanent record of every send, including what was skipped and why."""
    try:
        det = {"sent": job.get("sent_list", [])[:500], "skipped": job["skipped"][:500], "failed": job["failed"][:500]}
        with db.connect() as c:
            c.execute("UPDATE send_jobs SET finished=?, sent=?, skipped=?, failed=?, state=?, stopped_reason=?, details=? WHERE id=?",
                      (now(), job["sent"], len(job["skipped"]), len(job["failed"]), "stopped" if job.get("stopped_reason") else "done", job.get("stopped_reason") or "", json.dumps(det), job["id"]))
    except Exception:  # noqa
        pass


def already_sent(c, pid, template_id, step):
    if step:
        return bool(c.execute("SELECT 1 FROM emails WHERE partner_id=? AND step=? AND status='sent'", (pid, step)).fetchone())
    return bool(c.execute("SELECT 1 FROM emails WHERE partner_id=? AND template_id=? AND status='sent'", (pid, template_id)).fetchone())


def start_send_job(partner_ids, template_id=None, force=False, step=0, cc=None, to=None):
    cfg = load_cfg()
    if not (cfg["address"] and cfg["password"]):
        raise ValueError("Mail not configured. Add credentials in Settings first.")
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "total": len(partner_ids), "done": 0, "sent": 0, "skipped": [], "failed": [], "sent_list": [], "state": "running", "stopped_reason": ""}
    _log_job_start(jid, "followup" if step else ("single" if len(partner_ids) == 1 else "bulk"), len(partner_ids), template_id)
    threading.Thread(target=_run_job, args=(jid, list(partner_ids), template_id, force, step, list(cc or []), to), daemon=True).start()
    return JOBS[jid]


def start_test_job(to, partner_id=None, template_id=None):
    jid = uuid.uuid4().hex[:10]
    job = JOBS[jid] = {"id": jid, "total": 1, "done": 0, "sent": 0, "skipped": [], "failed": [], "sent_list": [], "state": "running", "stopped_reason": "", "to": to}
    _log_job_start(jid, "test", 1, template_id, label=_job_label(template_id) + f" → {to}")

    def work():
        c = db.connect()
        try:
            send_test(c, to, partner_id, template_id)
            job["sent"] = 1
        except Exception as e:  # noqa
            job["failed"].append({"id": 0, "name": "Test email", "why": (str(e) if isinstance(e, ValueError) else explain_send_error(e))[:300]})
        finally:
            c.close()
            job["done"] = 1
            _log_job_end(job)
            job["state"] = "done"
    threading.Thread(target=work, daemon=True).start()
    return job


def _run_job(jid, ids, template_id, force, step=0, cc=(), to_override=None):
    job = JOBS[jid]
    cfg = load_cfg()
    c = db.connect()
    smtp = None
    try:
        for pid in ids:
            p = c.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
            job["done"] += 1
            if not p:
                continue
            if p["do_not_contact"]:
                job["skipped"].append({"id": pid, "name": p["name"], "why": f"do not contact ({p['dnc_reason'] or 'manual'})"})
                continue
            if p["archived"]:
                job["skipped"].append({"id": pid, "name": p["name"], "why": "archived"})
                continue
            if not valid_email(to_override or p["email"]):
                job["skipped"].append({"id": pid, "name": p["name"], "why": "no valid email"})
                continue
            sent_today = c.execute("SELECT COUNT(*) FROM emails WHERE status='sent' AND sent_at>=?", (date.today().isoformat(),)).fetchone()[0]
            if cfg["daily_cap"] and sent_today >= cfg["daily_cap"]:
                job["stopped_reason"] = f"Daily cap of {cfg['daily_cap']} reached"
                job["skipped"] += [{"id": i, "name": "", "why": "daily cap"} for i in ids[job["done"] - 1:]]
                break
            try:
                r = render(c, p, template_id)
            except ValueError as e:
                job["failed"].append({"id": pid, "name": p["name"], "why": str(e)})
                continue
            if not force and already_sent(c, pid, r["template_id"], step):
                job["skipped"].append({"id": pid, "name": p["name"], "why": "already sent" if step else "already sent this template"})
                continue
            to = (to_override or p["email"]).strip()
            try:
                if smtp is None:
                    try:
                        smtp = smtplib.SMTP_SSL(cfg["smtp_host"], 465, timeout=30)
                        smtp.login(cfg["address"], cfg["password"])
                    except (smtplib.SMTPException, OSError) as e:
                        smtp = None
                        # Gmail answers a wrong or revoked app password by dropping the connection, so say so instead of logging a per-partner failure
                        job["stopped_reason"] = "Couldn't log in to the mailbox. Check the app password in Settings → Mailbox, then use Test connection. Nothing was sent."
                        job["skipped"] += [{"id": i, "name": "", "why": "mailbox login failed"} for i in ids[job["done"] - 1:]]
                        break
                cc_now = [a for a in cc if a.lower() != to.lower()]
                msg = build_message(cfg, to, r, cc_now)
                if getattr(smtp, "sock", None):
                    smtp.sock.settimeout(send_timeout(msg))
                smtp.send_message(msg)
                ts = now()
                c.execute("INSERT INTO emails(partner_id,template_id,to_addr,subject,message_id,status,sent_at,step,cc) VALUES(?,?,?,?,?,'sent',?,?,?)",
                          (pid, r["template_id"], to, r["subject"], msg["Message-ID"], ts, step, ", ".join(cc_now)))
                c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)",
                          (pid, ts, "email", f"Sent “{r['subject']}” to {to} ({r['template_name']})" + (f", Cc {', '.join(cc_now)}" if cc_now else "")))
                c.execute("UPDATE partners SET last_contacted=?, updated_at=?, stage=CASE WHEN stage='New' THEN 'Contacted' ELSE stage END WHERE id=?",
                          (date.today().isoformat(), ts, pid))
                if p["stage"] == "New":
                    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, ts, "stage", "New → Contacted"))
                c.commit()
                job["sent"] += 1
                job.setdefault("sent_list", []).append({"id": pid, "name": p["name"], "to": to})
            except Exception as e:  # noqa
                why = explain_send_error(e, sum(f[2] for f in r.get("_files", [])) * 1.34 / 1e6)
                c.execute("INSERT INTO emails(partner_id,template_id,to_addr,subject,status,error,sent_at) VALUES(?,?,?,?,'failed',?,?)",
                          (pid, r["template_id"], to, r["subject"], why, now()))
                c.commit()
                job["failed"].append({"id": pid, "name": p["name"], "why": why[:200]})
                try:
                    smtp.close()
                except Exception:  # noqa
                    pass
                smtp = None
                if isinstance(e, smtplib.SMTPAuthenticationError):
                    job["stopped_reason"] = "Login rejected — check app password in Settings"
                    break
            time.sleep(cfg["delay"])
    except Exception as e:  # noqa
        job["stopped_reason"] = "Stopped by an unexpected error: " + str(e)[:200]
    finally:
        try:
            smtp and smtp.quit()
        except Exception:  # noqa
            pass
        c.close()
        _log_job_end(job)          # recorded before the job reads as finished, so anything that waits for "done" can rely on the log
        job["state"] = "done"


# ---------------- replies / bounces ----------------
def _snippet(msg):
    part = msg.get_body(preferencelist=("plain",))
    text = part.get_content() if part else ""
    out = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:?$|^-{2,}\s*Original", line.strip()):
            break
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()[:600]


def _alert(c, pid, label, extra=""):
    try:
        import notify
        p = c.execute("SELECT name FROM partners WHERE id=?", (pid,)).fetchone()
        notify.send(f"{label} \u2014 {p['name'] if p else 'partner'}" + (f"\n> {extra}" if extra else "") + "\nPartner CRM: http://127.0.0.1:8765/#/partner/" + str(pid))
    except Exception:  # noqa
        pass


def check_replies():
    cfg = load_cfg()
    if not (cfg["address"] and cfg["password"]):
        raise ValueError("Mail not configured")
    c = db.connect()
    try:
        sent = c.execute("SELECT partner_id, message_id, to_addr, sent_at FROM emails WHERE status='sent' AND message_id IS NOT NULL").fetchall()
        if not sent:
            return {"replies": 0, "bounces": 0, "auto": 0, "unsubscribed": 0}
        by_msgid = {r["message_id"]: r["partner_id"] for r in sent}
        # people we CC are colleagues, not partners: a reply-all from them must not count as a partner reply
        internal = {r[0].lower() for r in c.execute("SELECT email FROM cc_contacts")}
        for (cc_,) in c.execute("SELECT cc FROM emails WHERE cc IS NOT NULL AND cc<>''"):
            internal |= {a.strip().lower() for a in cc_.split(",") if a.strip()}
        by_addr = {}
        for r in sent:
            by_addr[r["to_addr"].lower()] = r["partner_id"]
        for p in c.execute("SELECT id, email FROM partners WHERE email<>''"):
            if p["id"] in set(by_addr.values()):
                by_addr[p["email"].strip().lower()] = p["id"]
        since = (datetime.fromisoformat(min(r["sent_at"] for r in sent)) - timedelta(days=1)).strftime("%d-%b-%Y")
        m = imaplib.IMAP4_SSL(cfg["imap_host"], timeout=30)
        m.login(cfg["address"], cfg["password"])
        m.select("INBOX", readonly=True)
        _, data = m.search(None, "SINCE", since)
        res = {"replies": 0, "bounces": 0, "auto": 0, "unsubscribed": 0}
        for num in data[0].split():
            _, d = m.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM REFERENCES IN-REPLY-TO SUBJECT AUTO-SUBMITTED)])")
            h = email.message_from_bytes(d[0][1], policy=policy.default)
            mid = (h["Message-ID"] or "").strip()
            if not mid or c.execute("SELECT 1 FROM replies_seen WHERE message_id=?", (mid,)).fetchone():
                continue
            frm = email.utils.parseaddr(str(h["From"] or ""))[1].lower()
            if frm == cfg["address"].lower() or frm in internal:
                continue
            refs = f"{h['References'] or ''} {h['In-Reply-To'] or ''}"
            pid = next((by_msgid[x] for x in re.findall(r"<[^>]+>", refs) if x in by_msgid), None)
            kind = None
            bounce_addr = None
            if frm.startswith(("mailer-daemon", "postmaster")):
                _, d2 = m.fetch(num, "(BODY.PEEK[TEXT]<0.20000>)")
                body = d2[0][1].decode("utf-8", "ignore").lower() if d2 and d2[0] else ""
                bounce_addr = next((a for a in by_addr if a in body), None)
                pid = pid or (by_addr[bounce_addr] if bounce_addr else None)
                kind = "delay" if re.search(r"delay|warning", str(h["Subject"] or ""), re.I) else "bounce"
            else:
                pid = pid or by_addr.get(frm)
                subj = str(h["Subject"] or "")
                auto = (h["Auto-Submitted"] or "no").lower() != "no" or re.match(r"(?i)(automatic reply|auto.?reply|out of office)", subj)
                kind = "auto" if auto else "reply"
            if not pid:
                continue
            ts = now()
            if kind == "delay":
                continue  # temporary delivery delay notice: not a failure, leave unseen so a later final notice is still handled
            extra = c.execute("SELECT id FROM partner_contacts WHERE partner_id=? AND LOWER(TRIM(email))=?", (pid, bounce_addr or "")).fetchone() if kind == "bounce" else None
            if kind == "bounce" and extra and (bounce_addr or "") != (c.execute("SELECT LOWER(TRIM(email)) FROM partners WHERE id=?", (pid,)).fetchone()[0] or ""):
                c.execute("UPDATE partner_contacts SET bounced=1 WHERE id=?", (extra["id"],))
                c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, ts, "bounce", f"Email to {bounce_addr} bounced (additional contact). The partner's main address is unaffected."))
                res["bounces"] += 1
            elif kind == "bounce":
                c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, ts, "bounce", "Email bounced — address marked do-not-contact"))
                # several partners can share one address, so suppress every partner that uses it
                c.execute("UPDATE partners SET do_not_contact=1, dnc_reason=COALESCE(NULLIF(dnc_reason,''),'bounced') WHERE id=? OR (?<>'' AND LOWER(TRIM(email))=?)",
                          (pid, bounce_addr or "", bounce_addr or ""))
                res["bounces"] += 1
            else:
                _, d3 = m.fetch(num, "(BODY.PEEK[])")
                full = email.message_from_bytes(d3[0][1], policy=policy.default)
                snip = _snippet(full)
                if kind == "auto":
                    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, ts, "reply", f"(auto-reply) {snip[:200]}"))
                    res["auto"] += 1
                elif UNSUB_RE.search(f"{full['Subject']}\n{snip}"):
                    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, ts, "reply", f"{full['Subject']}\n\n{snip}"))
                    cur = c.execute("SELECT stage FROM partners WHERE id=?", (pid,)).fetchone()["stage"]
                    c.execute("UPDATE partners SET do_not_contact=1, dnc_reason='unsubscribed', stage='Not interested', lost_reason='Unsubscribed' WHERE id=?", (pid,))
                    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, ts, "stage", f"{cur} \u2192 Not interested (unsubscribed)"))
                    res["unsubscribed"] = res.get("unsubscribed", 0) + 1
                    _alert(c, pid, "\U0001F6D1 Opt-out")
                else:
                    c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, ts, "reply", f"{full['Subject']}\n\n{snip}"))
                    cur = c.execute("SELECT stage FROM partners WHERE id=?", (pid,)).fetchone()["stage"]
                    if cur in ("New", "Contacted"):
                        c.execute("UPDATE partners SET stage='Replied' WHERE id=?", (pid,))
                        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)", (pid, ts, "stage", f"{cur} → Replied"))
                    res["replies"] += 1
                    _alert(c, pid, "\U0001F4AC Reply", snip[:160])
            c.execute("UPDATE partners SET updated_at=? WHERE id=?", (ts, pid))
            c.execute("INSERT INTO replies_seen(message_id,partner_id,kind,ts) VALUES(?,?,?,?)", (mid, pid, kind, ts))
        c.commit()
        m.logout()
        return res
    finally:
        c.close()


def poller():
    """Background reply check every 10 minutes when mail is configured."""
    tick = 0
    while True:
        time.sleep(600)
        tick += 1
        if tick % 6 == 0:  # hourly
            try:
                import attribution
                if attribution.load_cfg()["onelink_base"]:
                    attribution.start_sync()
            except Exception:  # noqa
                pass
        try:
            if load_cfg()["password"]:
                check_replies()
        except Exception:  # noqa
            pass
        try:
            import youform
            if youform.load_cfg()["relay_url"]:
                youform.pull_relay()
        except Exception:  # noqa
            pass


# ---------------- follow-up sequence ----------------
FU1_SUBJECT = "Following up: Partner with Belong"
FU1_PLAIN = """Hi {{first_name}},

Just following up on my earlier note about the Belong Affiliate Partner Programme.

Belong is an IFSCA-regulated investment platform that gives NRI and resident clients access to GIFT City products \u2014 India-focussed and global mutual funds, AIF strategies and GIFT Nifty Futures \u2014 with revenue-sharing commercials for partners.

If it's relevant to your practice, details and the application are here: https://app.youform.com/forms/geok4bom. Happy to answer any questions by reply.

Regards,
Team Belong"""
FU2_SUBJECT = "Last note: Belong Affiliate Partner Programme"
FU2_PLAIN = """Hi {{first_name}},

One last note from me. We're opening the Belong Affiliate Partner Programme to a limited set of partners, and I didn't want you to miss it.

Details and application: https://app.youform.com/forms/geok4bom

If it isn't relevant to you, no problem at all \u2014 just let me know and I won't follow up again.

Regards,
Team Belong"""


def _plain_to_html(plain):
    paras = "".join(f'<p style="margin:0 0 12px 0">{escape(p).replace(chr(10), "<br>")}</p>' for p in plain.split("\n\n"))
    paras = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', paras)
    paras = paras.replace("{{referral_link}}", '<a href="{{referral_link}}">{{referral_link}}</a>')
    return ('<html><body style="margin:0;padding:24px 16px;background:#ffffff"><div style="max-width:600px;margin:0 auto;'
            'font:14px/1.6 Arial,sans-serif;color:#222">' + paras + '</div></body></html>')


def seed_sequence(c):
    if c.execute("SELECT COUNT(*) FROM sequence_steps").fetchone()[0]:
        return
    for no, delay, name, subj, plain in ((1, 4, "Follow-up 1", FU1_SUBJECT, FU1_PLAIN), (2, 7, "Follow-up 2 (final)", FU2_SUBJECT, FU2_PLAIN)):
        cur = c.execute("INSERT INTO templates(name,subject,plain,html,segment,updated_at) VALUES(?,?,?,?,?,?)",
                        (name + " \u2014 DRAFT, review before use", subj, plain, _plain_to_html(plain), "step", now()))
        c.execute("INSERT INTO sequence_steps(step_no,delay_days,template_id) VALUES(?,?,?)", (no, delay, cur.lastrowid))


def due_followups(c):
    """Partners due for each sequence step: still 'Contacted', not DNC, last send was step-1, enough days passed."""
    out = []
    today = date.today()
    for st in c.execute("SELECT s.*, t.name AS template_name FROM sequence_steps s JOIN templates t ON t.id=s.template_id ORDER BY step_no"):
        rows = c.execute(
            """SELECT p.id, p.name, p.email, p.segment, p.contact_person, e.sent_at FROM partners p
               JOIN emails e ON e.id=(SELECT id FROM emails WHERE partner_id=p.id AND status='sent' ORDER BY sent_at DESC, id DESC LIMIT 1)
               WHERE p.stage='Contacted' AND p.do_not_contact=0 AND COALESCE(p.archived,0)=0 AND e.step=? AND COALESCE(p.email,'')<>''""", (st["step_no"] - 1,)).fetchall()
        due = []
        for r in rows:
            days = (today - date.fromisoformat(r["sent_at"][:10])).days
            if days >= st["delay_days"] and valid_email(r["email"]):
                due.append({"id": r["id"], "name": r["name"], "email": r["email"], "segment": r["segment"], "days_since": days})
        due.sort(key=lambda x: -x["days_since"])
        out.append({"step_no": st["step_no"], "delay_days": st["delay_days"], "template_id": st["template_id"],
                    "template_name": st["template_name"], "due": due})
    return out


WELCOME_PLAIN = """Hi {{first_name}},

Thank you for applying to the Belong Affiliate Partner Programme. Here is your personal link to share with your clients:

{{referral_link}}

Clients who install the Belong app and sign up through this link are tracked to you. If you'd like any material to share with them, or have questions, just reply to this email.

Regards,
Team Belong"""


def seed_welcome(c):
    name = "Welcome \u2014 your referral link \u2014 DRAFT, review before use"
    if c.execute("SELECT 1 FROM templates WHERE name=?", (name,)).fetchone():
        return
    c.execute("INSERT INTO templates(name,subject,plain,html,segment,updated_at) VALUES(?,?,?,?,?,?)",
              (name, "Welcome to the Belong Affiliate Partner Programme", WELCOME_PLAIN, _plain_to_html(WELCOME_PLAIN), "step", now()))


# ---------------- review before sending ----------------
RECENT_DAYS = 14
ACTIVE_STAGES = ("Replied", "Applied", "In discussion", "Onboarded")
LARGE_MB, VERY_LARGE_MB = 3.0, 10.0


def email_bytes(t, files_bytes):
    """Approximate size of one finished email: its HTML (pasted pictures included) plus attachments, which grow ~34% when encoded."""
    return len(t["html"] or "") + int(files_bytes * 1.34)


def size_info(c, template_ids):
    """Per template: how big each email is, so the send dialog can warn before a slow or rejected send."""
    out = {}
    for tid in template_ids:
        t = c.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        if not t:
            continue
        files = attachments.files_for(c, tid)
        fb = sum(f[2] for f in files)
        b = email_bytes(t, fb)
        out[t["name"]] = {"mb": round(b / 1048576, 1), "attachments": len(files), "attachment_mb": round(fb / 1048576, 1), "seconds": upload_seconds(fb)}
    return out


def review(c, ids, template_id=None, step=0, force=False, excluded=(), samples=3):
    """Everything worth a second look before a bulk send: who would get it, what they'd see, and who needs a pause.
    Returns {rows: flagged partners, samples: rendered emails, counts}. People who'd be skipped anyway (do-not-contact, no address, already sent) are not listed."""
    rows, seen, picked = [], {}, []
    n_ok = generic = 0
    for pid in ids:
        p = c.execute("SELECT * FROM partners WHERE id=?", (int(pid),)).fetchone()
        if not p or p["do_not_contact"] or not valid_email(p["email"]):
            continue
        t = pick_template(c, p, template_id)
        if not t or (not force and already_sent(c, p["id"], t["id"], step)):
            continue
        n_ok += 1
        flags, dup = [], False
        addr = p["email"].strip().lower()
        if addr in seen:
            flags.append(f"same address as {seen[addr]}"); dup = True
        else:
            seen[addr] = p["name"]
        last = c.execute("SELECT MAX(sent_at) FROM emails WHERE partner_id=? AND status='sent'", (p["id"],)).fetchone()[0]
        if last:
            days = (datetime.now() - datetime.fromisoformat(last)).days
            if days < RECENT_DAYS:
                flags.append("emailed today" if days == 0 else f"emailed {days} day{'s' if days != 1 else ''} ago")
        if p["stage"] in ACTIVE_STAGES:
            flags.append(f"already in conversation ({p['stage']})")
        seen_kinds = {r[0] for r in c.execute("SELECT kind FROM replies_seen WHERE partner_id=?", (p["id"],))}
        if "reply" in seen_kinds and p["stage"] not in ACTIVE_STAGES:
            flags.append("has replied before")
        if "bounce" in seen_kinds:
            flags.append("an earlier email bounced")
        try:
            r = render(c, p, t["id"])
            if re.search(r"\{\{|\}\}", r["subject"] + r["plain"]):
                flags.append("has an unfilled merge field")
            elif "{{first_name}}" in (t["plain"] + t["html"] + t["subject"]).replace(" ", "") and first_name(p) == "there":
                generic += 1          # common for companies, so counted once rather than flagging each row
            if len(picked) < samples and t["id"] not in {x["template_id"] for x in picked}:
                picked.append({"partner_id": p["id"], "name": p["name"], "to": p["email"], "template_id": t["id"], "template": t["name"], "subject": r["subject"], "html": r["html"], "plain": r["plain"]})
        except ValueError as e:
            flags.append("can't be filled in: " + str(e)[:80])
        if flags:
            rows.append({"id": p["id"], "name": p["name"], "email": p["email"], "stage": p["stage"], "flags": flags, "dup": dup, "excluded": p["id"] in set(excluded)})
    if len(picked) < samples and n_ok:
        # fewer templates than samples: top up with different partners on the same template so names/greetings are visible
        have = {x["partner_id"] for x in picked}
        for pid in ids:
            if len(picked) >= samples:
                break
            p = c.execute("SELECT * FROM partners WHERE id=?", (int(pid),)).fetchone()
            if not p or p["id"] in have or p["do_not_contact"] or not valid_email(p["email"]):
                continue
            t = pick_template(c, p, template_id)
            if not t or (not force and already_sent(c, p["id"], t["id"], step)):
                continue
            try:
                r = render(c, p, t["id"])
            except ValueError:
                continue
            picked.append({"partner_id": p["id"], "name": p["name"], "to": p["email"], "template_id": t["id"], "template": t["name"], "subject": r["subject"], "html": r["html"], "plain": r["plain"]})
            have.add(p["id"])
    return {"rows": rows[:300], "flagged": len(rows), "checked": n_ok, "samples": picked, "generic_greeting": generic}
