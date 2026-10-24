#!/usr/bin/env python3
"""One-time import: scraper CSVs (+classification) -> universe, Sheet export -> partners.

Usage: python3 import_data.py <partner_tracker.csv> [--reset]
"""
import csv
import json
import os
import re
import sys
from datetime import datetime

import db

SCRAPE = os.path.expanduser("~/Documents/claude-misc/distributor-scrape/output")


def norm_key(raw):
    raw = (raw or "").strip().upper()
    m = re.match(r"ARN-0*(\d+)$", raw)
    return f"ARN-{m.group(1)}" if m else raw


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_with_class(csv_name, tsv_name):
    with open(os.path.join(SCRAPE, csv_name), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with open(os.path.join(SCRAPE, tsv_name), newline="", encoding="utf-8") as f:
        cls = [r[0] if r else "" for r in csv.reader(f, delimiter="\t")][1:]
    assert len(rows) == len(cls), (csv_name, len(rows), len(cls))
    return rows, cls


def load_universe(c):
    n = 0
    rows, cls = read_with_class("curated_mfd_distributors.csv", "mfd_classification.tsv")
    for r, k in zip(rows, cls):
        social = " | ".join(f"{lab}: {r[col]}" for lab, col in [
            ("Facebook", "fb_pagelink"), ("Instagram", "instagram_pagelink"), ("YouTube", "youtube_channellink"),
            ("X", "x_accountlink"), ("Telegram", "telegram_link"), ("WhatsApp", "whatsapp_link")] if r.get(col))
        c.execute(
            """INSERT OR IGNORE INTO universe(kind,key,name,category,classification,email,phone,city,state,address,
               website,linkedin,other_social,rank,top_500,avg_aum_cr,gross_commission_cr,sif_authorized,validity)
               VALUES('MFD',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (norm_key(r["arn_label"]), r["name"], "Indian Mutual Fund Distributors", k, r["email"], r["telephone"],
             r["city"].strip().title(), r["state"], r["address"], r["website_url"], r["linkedin_url"], social,
             int(r["rank"]) if r["rank"] else None, 1 if r["top_500"] == "True" else 0,
             num(r["avg_aum_cr"]), num(r["gross_commission_cr"]), 1 if r["sif_authorized"] == "True" else 0,
             r["arn_valid_till"][:10]))
        n += 1
    rows, cls = read_with_class("sebi_intermediaries.csv", "sebi_classification.tsv")
    for r, k in zip(rows, cls):
        addr = r["address"]
        c.execute(
            """INSERT OR IGNORE INTO universe(kind,key,name,category,classification,email,phone,contact_person,
               city,address,validity) VALUES('SEBI',?,?,?,?,?,?,?,?,?,?)""",
            (norm_key(r["registration_no"]), r["name"], f"SEBI - {r['category']}", k, r["email"], r["telephone"],
             r["contact_person"], city_from_address(addr), addr, r["validity"]))
        n += 1
    return n


def city_from_address(addr):
    parts = [p.strip() for p in (addr or "").split(",")]
    return parts[-3].title() if len(parts) >= 3 and not parts[-3].isdigit() else ""


def load_tracker(c, path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in list(csv.reader(f))[2:] if r and r[0]]
    hdr = ["name", "category", "website", "country", "city", "description", "source", "segment", "priority",
           "coverage", "nri_base", "contact_person", "designation", "email", "phone", "linkedin", "other_social",
           "owner", "status", "last_contacted", "follow_up", "remarks", "send", "email_log"]
    recs = [dict(zip(hdr, r + [""] * (len(hdr) - len(r)))) for r in rows]
    best = {}
    dups = 0
    for r in recs:
        m = re.search(r"\(([^)]+)\)\s*$", r["source"])
        key = norm_key(m.group(1)) if m else None
        r["_key"] = key or f"nokey:{r['name']}"
        score = (r["status"] == "Contacted", sum(bool(v) for v in r.values()))
        if r["_key"] in best:
            dups += 1
            if score <= best[r["_key"]][0]:
                continue
        best[r["_key"]] = (score, r)
    now = datetime.now().isoformat(timespec="seconds")
    added = unmatched = 0
    for key, (_, r) in best.items():
        u = c.execute("SELECT id FROM universe WHERE key=?", (key,)).fetchone()
        if not u:
            unmatched += 1
        stage = r["status"] if r["status"] in db.STAGES else "New"
        remarks = "" if r["remarks"].startswith("Added from") else r["remarks"]
        cur = c.execute(
            """INSERT INTO partners(universe_id,name,category,segment,country,city,website,description,source,
               contact_person,designation,email,phone,linkedin,other_social,priority,owner,stage,remarks,
               created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (u["id"] if u else None, r["name"], r["category"], r["segment"], r["country"], r["city"], r["website"],
             r["description"], r["source"], r["contact_person"], r["designation"], r["email"], r["phone"],
             r["linkedin"], r["other_social"], r["priority"], r["owner"], stage, remarks, now, now))
        pid = cur.lastrowid
        c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)",
                  (pid, now, "added", "Imported from Google Sheet Partner Tracker"))
        if r["email_log"]:
            c.execute("INSERT INTO activities(partner_id,ts,type,body) VALUES(?,?,?,?)",
                      (pid, now, "draft", r["email_log"]))
        added += 1
    return added, dups, unmatched


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if "--reset" in sys.argv and os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init()
    with db.connect() as c:
        if c.execute("SELECT COUNT(*) FROM universe").fetchone()[0]:
            sys.exit("DB already populated; use --reset to rebuild")
        n = load_universe(c)
        a, d, u = load_tracker(c, sys.argv[1])
    print(f"universe rows read: {n}; partners imported: {a}; duplicates collapsed: {d}; unmatched to universe: {u}")
