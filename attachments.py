"""Per-template email attachments: stored on disk, attached to every email sent with that template."""
import os
import re
import shutil
import urllib.parse
import uuid
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.environ.get("PARTNER_CRM_ATT_DIR", os.path.join(HERE, "data", "attachments"))
MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 15 * 1024 * 1024      # Gmail's limit is 25 MB after base64 encoding (~+33%)
BLOCKED_EXT = {".exe", ".bat", ".cmd", ".com", ".scr", ".js", ".vbs", ".jar", ".msi", ".sh", ".app", ".dmg", ".html", ".htm", ".svg"}
now = lambda: datetime.now().isoformat(timespec="seconds")


def _safe_name(n):
    n = os.path.basename(urllib.parse.unquote(n or "file")).replace("\x00", "")
    n = re.sub(r"[^\w.\- ()]+", "_", n).strip(" .") or "file"
    return n[-120:]


def listing(c, tid):
    return [dict(r) for r in c.execute("SELECT id,orig_name AS name,size FROM template_attachments WHERE template_id=? ORDER BY id", (tid,))]


def files_for(c, tid):
    """[(display name, absolute path, size)] for building the outgoing message."""
    out = []
    for r in c.execute("SELECT * FROM template_attachments WHERE template_id=? ORDER BY id", (tid,)):
        fp = os.path.join(DIR, str(tid), r["stored_name"])
        if os.path.isfile(fp):
            out.append((r["orig_name"], fp, r["size"]))
    return out


def add(c, tid, name, data):
    if not c.execute("SELECT 1 FROM templates WHERE id=?", (tid,)).fetchone():
        raise ValueError("save the template first, then add attachments")
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_FILE:
        raise ValueError("file is larger than 10 MB")
    used = c.execute("SELECT COALESCE(SUM(size),0) FROM template_attachments WHERE template_id=?", (tid,)).fetchone()[0]
    if used + len(data) > MAX_TOTAL:
        raise ValueError("attachments on one template can total at most 15 MB (email providers reject larger messages)")
    name = _safe_name(name)
    ext = os.path.splitext(name)[1].lower()
    if ext in BLOCKED_EXT:
        raise ValueError("this file type isn't allowed as an attachment")
    d = os.path.join(DIR, str(tid))
    os.makedirs(d, mode=0o700, exist_ok=True)
    stored = uuid.uuid4().hex + ext
    fd = os.open(os.path.join(d, stored), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    cur = c.execute("INSERT INTO template_attachments(template_id,orig_name,stored_name,size,uploaded_at) VALUES(?,?,?,?,?)", (tid, name, stored, len(data), now()))
    return {"id": cur.lastrowid, "name": name, "size": len(data)}


def path_of(c, aid):
    r = c.execute("SELECT * FROM template_attachments WHERE id=?", (aid,)).fetchone()
    if not r:
        return None, None
    fp = os.path.realpath(os.path.join(DIR, str(r["template_id"]), r["stored_name"]))
    if not fp.startswith(os.path.realpath(DIR) + os.sep) or not os.path.isfile(fp):
        return None, None
    return fp, r


def remove(c, aid):
    r = c.execute("SELECT * FROM template_attachments WHERE id=?", (aid,)).fetchone()
    if not r:
        raise LookupError("attachment")
    fp = os.path.join(DIR, str(r["template_id"]), r["stored_name"])
    if os.path.isfile(fp):      # the row is removed even if the file has already gone missing
        os.remove(fp)
    c.execute("DELETE FROM template_attachments WHERE id=?", (aid,))
    return {"ok": True}


def remove_all(c, tid):
    c.execute("DELETE FROM template_attachments WHERE template_id=?", (tid,))
    shutil.rmtree(os.path.join(DIR, str(tid)), ignore_errors=True)


def duplicate(c, src, dst):
    for name, fp, size in files_for(c, src):
        with open(fp, "rb") as f:
            add(c, dst, name, f.read())
