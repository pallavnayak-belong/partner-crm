"""Partner fit score (0-100) for the Discover list. Simple and explainable on purpose."""
import math


def score(u):
    s = 0.0
    if u["kind"] == "MFD":
        s += 25 if u["top_500"] else 0                                           # top-500 distributor by commission
        if u["rank"]:
            s += 25 * max(0.0, 1 - (u["rank"] - 1) / 3158)                        # rank percentile
        s += min(20.0, 5 * math.log10((u["avg_aum_cr"] or 0) + 1))                # AUM, saturates at ~10,000 Cr
        s += 10 if u["sif_authorized"] else 0                                     # SIF-authorised = sophisticated book
    else:
        cat = (u["category"] or "").lower()
        s += 35 if "portfolio" in cat else 30 if "adviser" in cat or "advisor" in cat else 15   # advisers/PMs advise NRIs; RAs less so
    s += {"Corporate": 10, "Bank": 5}.get(u["classification"] or "", 0)           # firms scale better than individuals
    s += 10 if (u["email"] or "").strip() else 0                                  # reachable
    return round(min(s, 100), 1)


def compute_all(c):
    n = 0
    for u in c.execute("SELECT * FROM universe WHERE score IS NULL").fetchall():
        c.execute("UPDATE universe SET score=? WHERE id=?", (score(u), u["id"]))
        n += 1
    return n
