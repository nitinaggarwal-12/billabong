#!/usr/bin/env python3
"""
cms_ingest.py — pull the real CMS source files into a local SQLite database.

Standard library only. No pip install.

    python3 cms_ingest.py            # fetch everything
    python3 cms_ingest.py --only ptp # fetch one source
    python3 cms_ingest.py --verify   # report what's loaded

Every row is stamped with the source URL and the date it was fetched. When a
finding is challenged two years from now, that stamp is the audit trail.

CMS rotates file URLs every quarter but keeps landing pages stable, so this
discovers the newest ZIP by scraping the landing page rather than hardcoding.
"""

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import urllib.request
import zipfile
from datetime import date, datetime

DB = os.environ.get("CODEREV_DB", "cms.db")
UA = "coderev-ingest/0.1 (urology coding audit prototype)"

SOURCES = {
    "ptp": {
        "landing": "https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-procedure-procedure-ptp-edits",
        "match": r"ptp.*practitioner|practitioner.*ptp",
        "table": "ptp_edit",
        "label": "NCCI PTP edits, Practitioner Services",
    },
    "mue": {
        "landing": "https://www.cms.gov/medicare/coding-billing/national-correct-coding-initiative-ncci-edits/medicare-ncci-medically-unlikely-edits-mues",
        "match": r"practitioner.*mue|mue.*practitioner",
        "table": "mue",
        "label": "NCCI MUE, Practitioner Services",
    },
    "hcpcs": {
        "landing": "https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system/quarterly-update",
        "match": r"alpha.?numeric.*hcpcs",
        "table": "hcpcs",
        "label": "HCPCS Level II quarterly file",
    },
    "rvu": {
        "landing": "https://www.cms.gov/medicare/payment/fee-schedules/physician/pfs-relative-value-files",
        "match": r"rvu2\d[a-d]",
        "table": "rvu",
        "label": "PFS Relative Value File",
        "follow_page": True,  # landing links to a per-quarter page, not the zip
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ptp_edit (
  col1 TEXT, col2 TEXT, effective TEXT, deletion TEXT,
  modifier_indicator TEXT, rationale TEXT,
  src TEXT, fetched TEXT
);
CREATE INDEX IF NOT EXISTS ix_ptp ON ptp_edit(col1, col2);
CREATE INDEX IF NOT EXISTS ix_ptp2 ON ptp_edit(col2);

CREATE TABLE IF NOT EXISTS mue (
  code TEXT, mue_value TEXT, adjudication_indicator TEXT, rationale TEXT,
  src TEXT, fetched TEXT
);
CREATE INDEX IF NOT EXISTS ix_mue ON mue(code);

CREATE TABLE IF NOT EXISTS hcpcs (
  code TEXT, short_desc TEXT, long_desc TEXT,
  src TEXT, fetched TEXT
);
CREATE INDEX IF NOT EXISTS ix_hcpcs ON hcpcs(code);

CREATE TABLE IF NOT EXISTS rvu (
  code TEXT, modifier TEXT, description TEXT, status TEXT,
  work_rvu REAL, pe_rvu REAL, mp_rvu REAL, total_rvu REAL,
  global_days TEXT, bilateral_ind TEXT, mult_proc_ind TEXT,
  assistant_ind TEXT, pctc_ind TEXT,
  src TEXT, fetched TEXT
);
CREATE INDEX IF NOT EXISTS ix_rvu ON rvu(code);

-- Codes deleted or replaced. Hand-maintained; CMS does not publish this
-- as a single file, which is exactly why practices keep billing dead codes.
CREATE TABLE IF NOT EXISTS code_status (
  code TEXT PRIMARY KEY, status TEXT, effective_year INTEGER,
  replaced_by TEXT, note TEXT, authority TEXT
);

CREATE TABLE IF NOT EXISTS ingest_log (
  source TEXT, url TEXT, rows INTEGER, fetched TEXT, note TEXT
);
"""


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return raw if binary else raw.decode("utf-8", "replace")


def find_links(html, base, pattern):
    """Return absolute links whose href or anchor text matches pattern."""
    out = []
    for m in re.finditer(r'href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        blob = f"{href} {text}"
        if re.search(pattern, blob, re.I):
            if href.startswith("/"):
                href = "https://www.cms.gov" + href
            if href.startswith("http"):
                out.append((href, " ".join(text.split())))
    seen, uniq = set(), []
    for h, t in out:
        if h not in seen:
            seen.add(h)
            uniq.append((h, t))
    return uniq


def discover(key):
    """Find the newest downloadable ZIP for a source."""
    s = SOURCES[key]
    html = get(s["landing"])
    hits = find_links(html, s["landing"], s["match"])
    zips = [(h, t) for h, t in hits if h.lower().endswith(".zip")]

    if not zips and s.get("follow_page"):
        # RVU landing links to per-quarter pages; open the newest and find the zip
        pages = sorted(hits, key=lambda x: x[0], reverse=True)
        for href, _ in pages[:4]:
            try:
                sub = get(href)
            except Exception:
                continue
            zips = [(h, t) for h, t in find_links(sub, href, r"\.zip") if h.lower().endswith(".zip")]
            if zips:
                break

    if not zips:
        raise RuntimeError(
            f"No ZIP found on {s['landing']}.\n"
            f"CMS changed the page layout. Open it, copy the ZIP link, and pass\n"
            f"  --url <link> --only {key}"
        )
    # CMS lists newest first; prefer any link whose text names the latest quarter
    return zips[0][0]


def read_zip_tables(raw):
    """Yield (filename, list-of-rows) for every delimited file in the archive."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    for name in zf.namelist():
        low = name.lower()
        if low.endswith("/") or not re.search(r"\.(csv|txt|tsv)$", low):
            continue
        data = zf.read(name).decode("utf-8", "replace")
        delim = "\t" if "\t" in data.split("\n")[0] else ","
        rows = list(csv.reader(io.StringIO(data), delimiter=delim))
        if rows:
            yield name, rows


def header_index(rows, wanted, window=12):
    """
    CMS files carry 1-10 lines of preamble before the real header.
    Find the header row and map wanted-keys to column positions by fuzzy match.
    """
    for i, row in enumerate(rows[:window]):
        norm = [re.sub(r"[^a-z0-9]", "", c.lower()) for c in row]
        idx = {}
        for key, pats in wanted.items():
            for j, cell in enumerate(norm):
                if any(p in cell for p in pats) and cell:
                    idx[key] = j
                    break
        if len(idx) >= max(2, len(wanted) // 2):
            return i, idx
    return None, {}


def clean(v):
    v = (v or "").strip().strip('"')
    return v


def num(v):
    try:
        return float(clean(v).replace(",", ""))
    except Exception:
        return None


# ---------------------------------------------------------------- loaders

def load_ptp(cx, raw, src):
    want = {
        "col1": ["column1", "columnone", "col1"],
        "col2": ["column2", "columntwo", "col2"],
        "effective": ["effectivedate"],
        "deletion": ["deletiondate"],
        "mi": ["modifier"],
        "rationale": ["rationale"],
    }
    n, today = 0, date.today().isoformat()
    for name, rows in read_zip_tables(raw):
        h, idx = header_index(rows, want)
        if h is None or "col1" not in idx or "col2" not in idx:
            continue
        g = lambda r, k: clean(r[idx[k]]) if k in idx and idx[k] < len(r) else ""
        batch = []
        for r in rows[h + 1:]:
            if len(r) < 2 or not re.match(r"^[0-9A-Z]{5}$", clean(r[idx["col1"]]) or ""):
                continue
            batch.append((g(r, "col1"), g(r, "col2"), g(r, "effective"),
                          g(r, "deletion"), g(r, "mi"), g(r, "rationale"), src, today))
        cx.executemany("INSERT INTO ptp_edit VALUES (?,?,?,?,?,?,?,?)", batch)
        n += len(batch)
    return n


def load_mue(cx, raw, src):
    want = {
        "code": ["hcpcscptcode", "hcpcscpt", "hcpcscode", "cptcode", "hcpcs"],
        "val": ["muevalue", "muevalues", "practitionerservicesmue"],
        "mai": ["adjudicationindicator", "mai"],
        "rationale": ["rationale"],
    }
    n, today = 0, date.today().isoformat()
    for name, rows in read_zip_tables(raw):
        h, idx = header_index(rows, want)
        if h is None or "code" not in idx:
            continue
        g = lambda r, k: clean(r[idx[k]]) if k in idx and idx[k] < len(r) else ""
        batch = []
        for r in rows[h + 1:]:
            if not r or idx["code"] >= len(r):
                continue
            c = clean(r[idx["code"]])
            if not re.match(r"^[0-9A-Z]{5}$", c):
                continue
            batch.append((c, g(r, "val"), g(r, "mai"), g(r, "rationale"), src, today))
        cx.executemany("INSERT INTO mue VALUES (?,?,?,?,?,?)", batch)
        n += len(batch)
    return n


def load_hcpcs(cx, raw, src):
    want = {
        "code": ["hcpc", "code"],
        "long": ["longdescription", "longdesc"],
        "short": ["shortdescription", "shortdesc"],
    }
    n, today = 0, date.today().isoformat()
    for name, rows in read_zip_tables(raw):
        h, idx = header_index(rows, want)
        if h is None or "code" not in idx:
            continue
        g = lambda r, k: clean(r[idx[k]]) if k in idx and idx[k] < len(r) else ""
        batch, seen = [], set()
        for r in rows[h + 1:]:
            if not r or idx["code"] >= len(r):
                continue
            c = clean(r[idx["code"]])
            if not re.match(r"^[A-Z0-9]{5}$", c) or c in seen:
                continue
            seen.add(c)
            batch.append((c, g(r, "short"), g(r, "long"), src, today))
        cx.executemany("INSERT INTO hcpcs VALUES (?,?,?,?,?)", batch)
        n += len(batch)
    return n


def load_rvu(cx, raw, src):
    want = {
        "code": ["hcpcs", "hcpc"],
        "mod": ["mod"],
        "desc": ["description"],
        "status": ["statuscode", "status"],
        "work": ["workrvu"],
        "pe": ["nonfacilipe", "nonfacilitype", "fullynonfacilpe", "pervu", "perv"],
        "mp": ["mprvu", "malpractice"],
        "total": ["nonfacilitytotal", "nonfaciltotal", "total"],
        "glob": ["globdays", "globaldays", "global"],
        "bilat": ["bilatsurg", "bilateral"],
        "mult": ["multproc", "multiplproc"],
        "asst": ["asstsurg", "assistant"],
        "pctc": ["pctcind", "pctc"],
    }
    n, today = 0, date.today().isoformat()
    for name, rows in read_zip_tables(raw):
        if "pprrvu" not in name.lower():
            continue
        h, idx = header_index(rows, want, window=20)
        if h is None or "code" not in idx:
            continue
        g = lambda r, k: clean(r[idx[k]]) if k in idx and idx[k] < len(r) else ""
        gn = lambda r, k: num(r[idx[k]]) if k in idx and idx[k] < len(r) else None
        batch = []
        for r in rows[h + 1:]:
            if not r or idx["code"] >= len(r):
                continue
            c = clean(r[idx["code"]])
            if not re.match(r"^[0-9A-Z]{5}$", c):
                continue
            batch.append((c, g(r, "mod"), g(r, "desc"), g(r, "status"),
                          gn(r, "work"), gn(r, "pe"), gn(r, "mp"), gn(r, "total"),
                          g(r, "glob"), g(r, "bilat"), g(r, "mult"),
                          g(r, "asst"), g(r, "pctc"), src, today))
        cx.executemany("INSERT INTO rvu VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        n += len(batch)
    return n


LOADERS = {"ptp": load_ptp, "mue": load_mue, "hcpcs": load_hcpcs, "rvu": load_rvu}


# ---------------------------------------------------------------- deleted codes
# CMS publishes no consolidated deleted-code file. This list is hand-verified
# against the CY2026 CPT release and AUA's published summary of urology changes.
# Extend it every January — it is the highest-yield table in the database.

DELETED = [
    ("55700", "deleted", 2026, "55707-55715",
     "Prostate biopsy replaced by nine codes split by approach and guidance; "
     "imaging guidance is now bundled into the new codes and is not separately reportable.",
     "CPT 2026"),
    ("0421T", "deleted", 2026, "52597",
     "Aquablation moved from Category III to Category I code 52597.",
     "CPT 2026"),
]


def seed_status(cx):
    cx.executemany(
        "INSERT OR REPLACE INTO code_status VALUES (?,?,?,?,?,?)", DELETED)


# ---------------------------------------------------------------- main

def ingest(keys, override_url=None):
    cx = sqlite3.connect(DB)
    cx.executescript(SCHEMA)
    seed_status(cx)
    cx.commit()

    for key in keys:
        s = SOURCES[key]
        print(f"\n=== {s['label']}")
        try:
            url = override_url or discover(key)
            print(f"    url  {url}")
            raw = get(url, binary=True)
            print(f"    got  {len(raw)/1e6:.1f} MB")
            cx.execute(f"DELETE FROM {s['table']}")
            rows = LOADERS[key](cx, raw, url)
            cx.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?)",
                       (key, url, rows, datetime.now().isoformat(), "ok"))
            cx.commit()
            print(f"    load {rows:,} rows into {s['table']}")
            if rows == 0:
                print("    !! zero rows — CMS changed the column headers.")
                print("       Run with --dump to see what the file looks like.")
        except Exception as e:
            cx.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?)",
                       (key, override_url or s["landing"], 0,
                        datetime.now().isoformat(), f"error: {e}"))
            cx.commit()
            print(f"    !! {e}")

    verify(cx)
    cx.close()


def verify(cx=None):
    close = cx is None
    cx = cx or sqlite3.connect(DB)
    print("\n--- loaded")
    for t in ("ptp_edit", "mue", "hcpcs", "rvu", "code_status"):
        try:
            n = cx.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        print(f"    {t:14} {n:>10,}")

    # urology spot checks — these should return rows if the load worked
    checks = [
        ("52000 bundled into 52204",
         "SELECT modifier_indicator FROM ptp_edit WHERE col1='52204' AND col2='52000'"),
        ("52332 global/bilateral indicators",
         "SELECT global_days, bilateral_ind FROM rvu WHERE code='52332' LIMIT 1"),
        ("55700 flagged deleted",
         "SELECT replaced_by FROM code_status WHERE code='55700'"),
    ]
    print("\n--- urology spot checks")
    for label, q in checks:
        try:
            r = cx.execute(q).fetchone()
            print(f"    {'PASS' if r else 'MISS'}  {label}  {r or ''}")
        except sqlite3.OperationalError as e:
            print(f"    ERR   {label}  {e}")
    if close:
        cx.close()


def dump(key):
    raw = get(discover(key), binary=True)
    for name, rows in read_zip_tables(raw):
        print(f"\n### {name}")
        for r in rows[:8]:
            print("   ", r[:10])


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--only", choices=list(SOURCES))
    p.add_argument("--url", help="direct ZIP url, overrides discovery")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--dump", action="store_true", help="print first rows of each file")
    a = p.parse_args()

    if a.verify:
        verify()
    elif a.dump:
        dump(a.only or "ptp")
    else:
        ingest([a.only] if a.only else list(SOURCES), a.url)