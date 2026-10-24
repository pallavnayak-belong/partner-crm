"""Country / region classification: a partner is Indian when its country is India, otherwise Foreign."""
COUNTRIES = ["India", "United Arab Emirates", "Singapore", "United Kingdom", "United States", "Canada", "Australia", "Hong Kong", "Saudi Arabia", "Qatar",
             "Kuwait", "Oman", "Bahrain", "Germany", "Switzerland", "Netherlands", "France", "Ireland", "New Zealand", "Malaysia", "Japan", "South Africa", "Kenya"]
ALIASES = {"in": "India", "ind": "India", "bharat": "India", "uae": "United Arab Emirates", "u.a.e": "United Arab Emirates", "u.a.e.": "United Arab Emirates",
           "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates", "uk": "United Kingdom", "u.k.": "United Kingdom", "england": "United Kingdom", "great britain": "United Kingdom",
           "us": "United States", "usa": "United States", "u.s.": "United States", "u.s.a.": "United States", "united states of america": "United States", "america": "United States",
           "ksa": "Saudi Arabia", "sg": "Singapore", "hk": "Hong Kong", "nz": "New Zealand"}
# SQL for the same rule, so list filters and counts agree with region_of()
REGION_SQL = "CASE WHEN LOWER(TRIM(COALESCE({t}country,''))) IN ('','india') THEN 'Indian' ELSE 'Foreign' END"


def norm_country(s):
    s = " ".join(str(s or "").split())
    if not s:
        return "India"
    low = s.lower()
    if low in ALIASES:
        return ALIASES[low]
    for c in COUNTRIES:
        if c.lower() == low:
            return c
    return s.title() if s.islower() or s.isupper() else s


def region_of(country):
    return "Indian" if norm_country(country) == "India" else "Foreign"


def region_sql(table=""):
    return REGION_SQL.format(t=f"{table}." if table else "")
