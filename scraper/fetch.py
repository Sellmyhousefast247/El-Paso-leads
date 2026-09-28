#!/usr/bin/env python3
"""
El Paso County (Texas) Motivated Seller Lead Scraper
====================================================
El Paso County's Official Public Records app (apps.epcountytx.gov) -- which
indexes judgments, tax liens, lis pendens, heirship affidavits AND trustee-
sale foreclosure notices -- sits behind Cloudflare Turnstile bot-detection,
so it cannot be scraped from an automated (headless CI) runner. This scraper
therefore builds on the county's OPEN distress sources:

Sources
  1. El Paso County Sheriff's real-estate sale notices (open HTML, no gate):
       https://www.epcounty.com/1192/Sheriff-Sales
     A table of upcoming Sheriff's sales of real estate (judgment sales).
     Each row: Sale Date, Sale Time, Sale Location, Description of Property.
     The Description carries the district-court cause number, the CAD tax
     account number(s), sometimes a street address, and the legal
     description. Cause type drives the category:
       ####DTX#### (delinquent-tax suit)  -> TAXFC  (tax foreclosure sale)
       ####DCV#### (civil / execution)    -> FC     (execution / judgment sale)
     These are genuine distress -- owners about to lose property at sale.
  2. Enrichment: El Paso Central Appraisal District (epcad.org, open, no gate).
     account# -> GET /Search?Keywords=<acct> -> /Search/Details/<id>/<year>,
     which exposes Owners Name, Location Address (situs), Mailing Address,
     Legal Description and Exemptions (HS = homestead / owner-occupied).
     When the sale row has no account number, EPCAD is searched by the
     street address instead.

NOTE (coverage): the higher-volume recorder index (abstracts of judgment,
tax/mechanic liens, lis pendens, heirship) and the trustee-sale foreclosure
notices are Turnstile-gated on apps.epcountytx.gov and are intentionally NOT
scraped here. Sheriff/constable sales are monthly (first Tuesday), so volume
is low per run but accumulates via the NEW/CHANGED state across runs.
Constable-precinct sales are a future add.

Run:
    python scraper/fetch.py                 # default
    python scraper/fetch.py --skip-cad      # sheriff rows only, no enrichment
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COUNTY = "El Paso"
STATE = "TX"

SHERIFF_URL = "https://www.epcounty.com/1192/Sheriff-Sales"

# Enrichment: City of El Paso GIS parcels (public ArcGIS FeatureServer). Same
# schema family as the Waco CAD layer used for McLennan. Keyed by prop_id (the
# CAD account number that the Sheriff notices carry). Note: EPCAD's own site
# (epcad.org) is a Cloudflare-gated HTML app that blocks datacenter/CI IPs, so
# this JSON ArcGIS API is used instead -- it returns owner, situs, mailing and
# appraised value in one query.
PARCEL_API_URL = ("https://gis.elpasotexas.gov/dev/rest/services/"
                  "Hosted/parcels2021/FeatureServer/0/query")
PARCEL_FIELDS = ("file_as_na,situs_num,situs_stre,situs_dir,situs_unit,"
                 "situs_city,situs_zip,addr_line2,addr_city,addr_state,"
                 "addr_zip,prop_id,geo_id,prop_val_y")

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
REQUEST_TIMEOUT = 45
CAD_MAX_LOOKUPS = 400

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("elpaso_scraper")

GHL_FIELDS = [
    "doc_num","doc_type","cat","cat_label","filed","owner","grantee",
    "amount","prop_address","prop_city","prop_state","prop_zip",
    "mail_address","mail_city","mail_state","mail_zip","legal","clerk_url","score","flags",
    "first_seen","status",
]
GHL_HEADERS = {f: f.replace("_", " ").title() for f in GHL_FIELDS}
GHL_HEADERS["first_seen"] = "Date Entered System"
GHL_HEADERS["status"] = "Status"

@dataclass
class LeadRecord:
    doc_num: str = ""
    doc_type: str = ""
    cat: str = ""
    cat_label: str = ""
    filed: str = ""
    owner: str = ""
    grantee: str = ""
    amount: float = 0.0
    legal: str = ""
    prop_address: str = ""
    prop_city: str = ""
    prop_state: str = STATE
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = STATE
    mail_zip: str = ""
    clerk_url: str = ""
    flags: list = field(default_factory=list)
    score: int = 0
    status: str = ""
    first_seen: str = ""
    rid: str = ""
    content_hash: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm_ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip())


def normalize_date(raw: str) -> str:
    raw = _norm_ws(raw)
    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw.title() if "," in raw else raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


ENTITY_RE = re.compile(
    r"\b(LLC|L\.?L\.?C|INC|CORP|COMPANY|CO|BANK|N\.?A|TRUST|LP|L\.?P|LLP|"
    r"ASSOCIATION|ASSN|FUND|FUNDING|CREDIT UNION|CU|SYSTEM|COUNTY|CITY OF|"
    r"STATE OF|UNITED STATES|IRS|DEPARTMENT|ISD|UNIVERSITY|COLLEGE|"
    r"HOSPITAL|MEDICAL|SERVICES|CAPITAL|MORTGAGE|FINANCIAL|HOLDINGS|"
    r"ESTATES?|PARTNERS|GROUP|ENTERPRISES|INVESTMENTS?)\b", re.I)


def _looks_like_entity(name: str) -> bool:
    return bool(ENTITY_RE.search(name or ""))


# Street address parser (fallback when the row has no "STREET ADDRESS:" label)
STREET_RE = re.compile(
    r"\b(\d{2,6}\s+[A-Z0-9][A-Za-z0-9 .'-]{2,40}?"
    r"(?:ROAD|RD|STREET|ST|DRIVE|DR|LANE|LN|COURT|CT|CIRCLE|CIR|TRAIL|TRL|"
    r"AVENUE|AVE|BOULEVARD|BLVD|WAY|PASS|PATH|LOOP|RUN|COVE|CV|BEND|BND|PLACE|"
    r"PL|POINT|PT|PARK|PKWY|PARKWAY|TERRACE|TER|RIDGE|HILL|HILLS|VISTA|VIEW))"
    r"\b", re.I)

# El Paso County incorporated / CDP place names, longest first so multi-word
# names win. Used to split "STREET CITY, TX ZIP" (CAD) and "street, City, TX
# ZIP" (sheriff) and "STREET CITY TX ZIP" (CAD mailing) uniformly.
EP_CITIES = [
    "HOMESTEAD MEADOWS SOUTH", "HOMESTEAD MEADOWS NORTH", "HORIZON CITY",
    "SAN ELIZARIO", "EL PASO", "CLINT", "SOCORRO", "FABENS", "TORNILLO",
    "ANTHONY", "CANUTILLO", "VINTON", "WESTWAY", "SPARKS", "AGUA DULCE",
    "PRADO VERDE", "MORNINGSIDE HEIGHTS",
]
_CITY_ALT = "|".join(re.escape(c) for c in EP_CITIES)
FULL_ADDR_RE = re.compile(
    r"^(.*?),?\s+(" + _CITY_ALT + r")\s*,?\s*(?:TX|TEXAS)\.?\s+(\d{5})(?:-\d{4})?",
    re.I)
TRAIL_ST_ZIP_RE = re.compile(r"^(.*?),?\s*(?:TX|TEXAS)\.?\s+(\d{5})(?:-\d{4})?", re.I)


def _split_addr_line(line: str) -> tuple:
    """Parse any El Paso address shape -> (street, city, zip)."""
    line = _norm_ws(line)
    m = FULL_ADDR_RE.search(line)
    if m:
        return _norm_ws(m.group(1)).title(), _norm_ws(m.group(2)).title(), m.group(3)
    # no known city token: split trailing ", TX zip" and treat the rest as street
    m = TRAIL_ST_ZIP_RE.search(line)
    if m:
        street = _norm_ws(m.group(1)).rstrip(",")
        return street.title(), "", m.group(2)
    return line.title(), "", ""

# ---------------------------------------------------------------------------
# El Paso County Sheriff real-estate sale notices (open HTML table)
# ---------------------------------------------------------------------------
CAUSE_RE = re.compile(r"\b(\d{4}[A-Z]{2,4}\d{3,6})")
ACCT_RE = re.compile(
    r"(?:ACCT\.?\s*NO\.?|TAX ACCOUNT NUMBER\(?S?\)?|ACCOUNT NUMBER\(?S?\)?|\bPID)\s*:?\s*(\d{4,12})",
    re.I)
STREET_LABEL_RE = re.compile(r"STREET ADDRESS\s*:?\s*(.+?)\s*(?:;|TAX ACCOUNT|LEGAL DESCRIPTION|$)", re.I)
LEGAL_LABEL_RE = re.compile(r"LEGAL DESCRIPTION\s*:?\s*(.+)$", re.I)


def _parse_sheriff_desc(desc: str) -> dict:
    """Pull cause number, account number(s), street address and legal out of a
    Sheriff-sale 'Description of Property' cell."""
    d = _norm_ws(desc)
    cause_m = CAUSE_RE.search(d)
    cause = cause_m.group(1) if cause_m else ""
    accts = []
    for m in ACCT_RE.finditer(d):
        a = m.group(1)
        if a not in accts:
            accts.append(a)
    # street address: prefer the explicit label, else a city-anchored match,
    # else a general street token.
    street = city = zp = ""
    lbl = STREET_LABEL_RE.search(d)
    if lbl:
        street, city, zp = _split_addr_line(lbl.group(1))
    else:
        fa = FULL_ADDR_RE.search(d)
        if fa:
            city = _norm_ws(fa.group(2)).title()
            zp = fa.group(3)
            prefix = _norm_ws(fa.group(1))
            sm = STREET_RE.search(prefix)
            if sm:
                street = _norm_ws(prefix[sm.start():]).title()
            else:
                street = " ".join(prefix.split()[-4:]).title()
        else:
            sm = STREET_RE.search(d)
            if sm:
                street = _norm_ws(sm.group(1)).title()
    legal = ""
    lm = LEGAL_LABEL_RE.search(d)
    if lm:
        legal = _norm_ws(lm.group(1))
    else:
        # legal is whatever follows the first account number / first semicolon
        tail = d
        if accts:
            tail = d.split(accts[0], 1)[-1]
        legal = _norm_ws(tail.lstrip("; ").lstrip(";"))[:300]
    return {"cause": cause, "accts": accts, "street": street,
            "city": city, "zip": zp, "legal": legal}


def _sheriff_tables(soup) -> list:
    out = []
    for t in soup.find_all("table"):
        head = t.find("tr")
        if not head:
            continue
        htxt = " ".join(c.get_text(" ").lower() for c in head.find_all(["th", "td"]))
        if "description of property" in htxt:
            out.append(t)
    return out


def fetch_sheriff_records(session) -> list:
    """Scrape the open Sheriff real-estate sale-notice table(s). Never raises."""
    records = []
    try:
        r = session.get(SHERIFF_URL, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(r.text, "lxml")
        tables = _sheriff_tables(soup)
        if not tables:
            log.warning("Sheriff sales: no sale table found")
            return records
        seen = set()
        for t in tables:
            for tr in t.find_all("tr")[1:]:
                cells = [_norm_ws(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
                if len(cells) < 4 or not cells[0]:
                    continue
                sale_date, sale_time, sale_loc, desc = cells[0], cells[1], cells[2], cells[3]
                if not desc or len(desc) < 8:
                    continue
                p = _parse_sheriff_desc(desc)
                saledate = normalize_date(sale_date)
                is_tax = bool(re.search(r"\d{4}DTX", p["cause"]))
                cat = "TAXFC" if is_tax else "FC"
                cat_label = (f"Tax Foreclosure Sale {saledate}" if is_tax
                             else f"Sheriff Sale {saledate}")
                doc_num = (p["cause"] or (f"EPSHF-{p['accts'][0]}" if p["accts"]
                           else "EPSHF-" + hashlib.sha1(desc.encode()).hexdigest()[:10]))
                if doc_num in seen:
                    continue
                seen.add(doc_num)
                rec = LeadRecord(
                    doc_num=doc_num,
                    doc_type=("Tax Foreclosure Sale" if is_tax else "Sheriff Execution Sale"),
                    cat=cat, cat_label=cat_label,
                    filed=datetime.now().strftime("%Y-%m-%d"),
                    prop_address=p["street"], prop_city=p["city"], prop_zip=p["zip"],
                    legal=(f"Sale date: {saledate}. " if saledate else "")
                          + (f"Cause {p['cause']}. " if p["cause"] else "")
                          + p["legal"],
                    clerk_url=SHERIFF_URL,
                )
                # stash account candidates for CAD enrichment via a flag we strip later
                rec._accts = p["accts"]  # type: ignore[attr-defined]
                records.append(rec)
        log.info("Sheriff sales: %d sale rows (%d tax, %d execution)",
                 len(records),
                 sum(1 for r in records if r.cat == "TAXFC"),
                 sum(1 for r in records if r.cat == "FC"))
    except Exception as exc:
        log.warning("Sheriff sales source failed (skipping): %s", exc)
    return records

# ---------------------------------------------------------------------------
# El Paso parcel enrichment (City of El Paso GIS, public ArcGIS FeatureServer)
# ---------------------------------------------------------------------------
def _arc_val(x) -> str:
    s = _norm_ws(x)
    return "" if s.upper() in ("NULL", "NONE") else s


def _sql_lit(s: str) -> str:
    return s.upper().replace("'", "''")


def _arcgis_query(session, where: str, count: int = 5) -> list:
    params = {
        "where": where, "outFields": PARCEL_FIELDS,
        "returnGeometry": "false", "f": "json", "resultRecordCount": count,
    }
    try:
        r = session.get(PARCEL_API_URL, params=params, timeout=REQUEST_TIMEOUT)
        return r.json().get("features", []) or []
    except Exception as exc:
        log.debug("ArcGIS query error: %s", exc)
        return []


def _situs_from_arc(att: dict) -> tuple:
    # situs_stre is the street name; situs_dir carries the suffix/type (CIR,
    # ST, DR ...). situs_zip is often blank on this layer.
    num = _arc_val(att.get("situs_num"))
    stre = _arc_val(att.get("situs_stre"))
    suf = _arc_val(att.get("situs_dir"))
    unit = _arc_val(att.get("situs_unit"))
    street = _norm_ws(" ".join(x for x in (num, stre, suf, unit) if x))
    city = _arc_val(att.get("situs_city")).title()
    zp = _arc_val(att.get("situs_zip"))[:5]
    return street.title(), city, zp


def _mailing_from_arc(att: dict) -> tuple:
    return (_arc_val(att.get("addr_line2")).title(),
            _arc_val(att.get("addr_city")).title(),
            _arc_val(att.get("addr_state")) or STATE,
            _arc_val(att.get("addr_zip"))[:5])


def _addr_key(addr: str) -> tuple:
    """(house number, core street) for reverse situs lookup; situs_stre
    excludes the suffix, so drop a trailing suffix word and a leading dir."""
    m = re.match(r"\s*(\d+)\s+(.*)", addr or "")
    if not m:
        return "", ""
    rest = _norm_ws(m.group(2))
    rest = re.sub(r"\s+(#|APT|UNIT|STE|SUITE|BLDG|LOT)\b.*$", "", rest, flags=re.I)
    rest = re.sub(r"\s+(RD|ROAD|ST|STREET|DR|DRIVE|LN|LANE|CT|COURT|CIR|CIRCLE|"
                  r"TRL|TRAIL|AVE|AVENUE|BLVD|WAY|PASS|PATH|LOOP|RUN|CV|COVE|"
                  r"BND|BEND|PKWY|PARKWAY|TER|TERRACE|PL|PLACE|PT|POINT|XING|"
                  r"CROSSING|PARK)\.?$", "", rest, flags=re.I).strip()
    rest = re.sub(r"^(N|S|E|W|NE|NW|SE|SW)\s+", "", rest, flags=re.I).strip()
    return m.group(1), rest


def _apply_parcel(rec, att: dict) -> None:
    owner = _arc_val(att.get("file_as_na"))
    if owner and not rec.owner:
        rec.owner = owner
    ps, pc, pz = _situs_from_arc(att)
    if ps and not rec.prop_address:
        rec.prop_address = ps
    if pc and not rec.prop_city:
        rec.prop_city = pc
    if pz and not rec.prop_zip:
        rec.prop_zip = pz
    ms, mc, mst, mz = _mailing_from_arc(att)
    if ms and not rec.mail_address:
        rec.mail_address, rec.mail_city, rec.mail_state, rec.mail_zip = ms, mc, mst, mz
    if not rec.amount:
        try:
            rec.amount = float(att.get("prop_val_y") or 0)
        except (TypeError, ValueError):
            pass


def enrich_cad(records: list) -> None:
    """Enrich Sheriff records against the City of El Paso parcels layer: first
    by CAD account number (prop_id), then by the situs street address."""
    session = requests.Session()
    session.headers["User-Agent"] = _UA
    n_hit = 0
    todo = [r for r in records if not r.owner or not r.mail_address]
    log.info("ArcGIS parcel enrichment for %d records...", len(todo))
    for rec in todo[:CAD_MAX_LOOKUPS]:
        att = None
        # 1) by account number (prop_id) -- the reliable key
        for acct in (getattr(rec, "_accts", []) or []):
            if not acct.isdigit() or len(acct) > 8:
                continue
            feats = _arcgis_query(session, f"prop_id = {int(acct)}", count=2)
            if len(feats) == 1:
                att = feats[0].get("attributes", {})
                break
            time.sleep(0.1)
        # 2) reverse by situs address
        if att is None and rec.prop_address:
            num, core = _addr_key(rec.prop_address)
            if num and core:
                feats = _arcgis_query(
                    session, f"situs_num = '{_sql_lit(num)}' AND "
                             f"UPPER(situs_stre) LIKE '{_sql_lit(core)}%'", count=2)
                if len(feats) == 1:
                    att = feats[0].get("attributes", {})
        if att:
            n_hit += 1
            _apply_parcel(rec, att)
        time.sleep(0.05)
    log.info("ArcGIS parcel enrichment: %d records enriched", n_hit)

# ---------------------------------------------------------------------------
# Hash / dedupe + NEW-CHANGED detection
# ---------------------------------------------------------------------------
def _repo_base() -> Path:
    return Path(__file__).parent.parent


def _record_rid(r) -> str:
    basis = r.doc_num or f"{r.owner}|{r.filed}|{r.doc_type}|{r.prop_address}"
    return hashlib.sha1(f"elpaso|{basis}".encode()).hexdigest()[:16]


def _record_chash(r) -> str:
    fields = "|".join(str(x or "") for x in (
        r.doc_num, r.doc_type, r.filed, r.owner, r.grantee, r.legal,
        r.amount, r.prop_address, r.mail_address))
    return hashlib.sha1(fields.encode()).hexdigest()[:16]


def detect_changes(records: list) -> None:
    state_path = _repo_base() / "data" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    today = datetime.now().strftime("%Y-%m-%d")
    n_new = n_chg = n_exist = 0
    for r in records:
        r.rid = _record_rid(r)
        r.content_hash = _record_chash(r)
        prev = state.get(r.rid)
        if prev is None:
            r.status, r.first_seen = "NEW", today
            n_new += 1
        elif prev.get("content_hash") != r.content_hash:
            r.status = "CHANGED"
            r.first_seen = prev.get("first_seen", today)
            n_chg += 1
        else:
            r.status = "EXISTING"
            r.first_seen = prev.get("first_seen", today)
            n_exist += 1
        state[r.rid] = {"content_hash": r.content_hash,
                        "first_seen": r.first_seen, "last_seen": today}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    log.info("NEW/CHANGED: NEW=%d CHANGED=%d EXISTING=%d (state=%d ids)",
             n_new, n_chg, n_exist, len(state))

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_records(records: list, start: datetime) -> None:
    for r in records:
        s, flags = 30, []
        if r.cat == "LP": s += 10; flags.append("LIS_PENDENS")
        if r.cat == "FC": s += 15; flags.append("FORECLOSURE")
        if r.cat == "TAXFC": s += 18; flags.append("TAX_FORECLOSURE")
        if r.cat == "TAXDEED": s += 10; flags.append("TAX_DEED")
        if r.cat in ("LP","FC","TAXFC"): s += 5
        if r.cat == "JUD": s += 8; flags.append("JUDGMENT")
        if r.cat == "LIEN": s += 7; flags.append("LIEN")
        if r.cat == "PRO": s += 12; flags.append("PROBATE")
        if r.amount > 100000: s += 15; flags.append("HIGH_AMOUNT")
        elif r.amount > 50000: s += 10; flags.append("MID_AMOUNT")
        if r.filed:
            try:
                if datetime.strptime(r.filed, "%Y-%m-%d") >= start:
                    s += 5; flags.append("NEW_THIS_WEEK")
            except ValueError:
                pass
        if r.prop_address:
            s += 5; flags.append("HAS_ADDRESS")
        r.score = min(s, 100)
        r.flags = flags

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
DASH_CAT = {
    "LP": "foreclosure", "FC": "foreclosure", "TAXFC": "foreclosure",
    "TAXDEED": "tax_lien", "LIEN": "tax_lien",
    "JUD": "judgment", "PRO": "probate",
}
FLAG_NICE = {
    "LIS_PENDENS": "Lis pendens", "FORECLOSURE": "Pre-foreclosure",
    "TAX_FORECLOSURE": "Tax foreclosure", "TAX_DEED": "Tax deed",
    "JUDGMENT": "Judgment lien", "LIEN": "Tax lien",
    "PROBATE": "Probate / estate", "HIGH_AMOUNT": "Amount > $100k",
    "MID_AMOUNT": "Amount > $50k", "NEW_THIS_WEEK": "New this week",
    "HAS_ADDRESS": "Has address",
}


def write_outputs(records: list, start: datetime, end: datetime) -> None:
    base = _repo_base()
    for d in [base / "dashboard", base / "data"]:
        d.mkdir(parents=True, exist_ok=True)
    week_ago = (end - timedelta(days=7)).strftime("%Y-%m-%d")
    recs_out = []
    for r in records:
        d = asdict(r)
        d["cat_code"] = r.cat
        d["cat"] = DASH_CAT.get(r.cat, "foreclosure")
        d["flags"] = [FLAG_NICE.get(f, f) for f in (r.flags or [])]
        d["absentee"] = bool(
            r.prop_address and r.mail_address
            and r.prop_address.upper() != r.mail_address.upper())
        d["out_of_state"] = bool(r.mail_state and r.mail_state.upper() != STATE)
        recs_out.append(d)
    payload = {
        "fetched_at": datetime.utcnow().isoformat(),
        "county": COUNTY,
        "source": f"{COUNTY} County, {STATE} -- Sheriff Sale Notices + City of El Paso GIS parcels",
        "date_range": {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
        "total": len(records),
        "new_7d": sum(1 for r in records if (r.first_seen or "") >= week_ago),
        "with_address": sum(1 for r in records if r.prop_address),
        "by_cat": {c: sum(1 for r in records if r.cat == c) for c in ("FC","TAXFC","TAXDEED","LP","JUD","LIEN","PRO")},
        "records": recs_out,
    }
    for path in [base / "dashboard" / "records.json", base / "data" / "records.json"]:
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.info("JSON written: %s (%d records)", path, len(records))
    csv_path = base / "data" / "ghl_export.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(GHL_HEADERS.values()))
        writer.writeheader()
        for r in records:
            d = asdict(r)
            writer.writerow({GHL_HEADERS[k]: ("|".join(d[k]) if k=="flags" else d[k]) for k in GHL_FIELDS})
    log.info("GHL CSV written: %s (%d records)", csv_path, len(records))
    skip_path = base / "data" / "skiptrace_export.csv"
    skip_cols = ["First Name", "Last Name", "Mailing Address", "Mailing City",
                 "Mailing State", "Mailing Zip", "Property Address",
                 "Property City", "Property State", "Property Zip"]
    with open(skip_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=skip_cols)
        writer.writeheader()
        for r in records:
            owner = (r.owner or "").strip()
            if "," in owner:
                p = owner.split(",", 1)
                first, last = p[1].strip().title(), p[0].strip().title()
            elif _looks_like_entity(owner):
                first, last = "", owner.title()
            else:
                p = owner.split()
                if p and owner == owner.upper() and len(p) > 1:
                    # CAD "LAST FIRST M" style
                    first, last = p[1].title(), p[0].title()
                else:
                    first = p[0].title() if p else ""
                    last = p[-1].title() if len(p) > 1 else ""
            writer.writerow({
                "First Name": first, "Last Name": last,
                "Mailing Address": r.mail_address, "Mailing City": r.mail_city,
                "Mailing State": r.mail_state, "Mailing Zip": r.mail_zip,
                "Property Address": r.prop_address, "Property City": r.prop_city,
                "Property State": r.prop_state, "Property Zip": r.prop_zip,
            })
    log.info("Skip trace CSV written: %s (%d records)", skip_path, len(records))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="El Paso County lead scraper")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--skip-cad", action="store_true")
    args = parser.parse_args()
    end = datetime.now()
    start = end - timedelta(days=max(args.days, LOOKBACK_DAYS))
    log.info("=" * 60)
    log.info("El Paso County Motivated Seller Lead Scraper")
    log.info("Open sources: Sheriff sale notices + El Paso CAD enrichment")
    log.info("=" * 60)

    session = requests.Session()
    session.headers["User-Agent"] = _UA
    records = fetch_sheriff_records(session)

    # dedupe on doc_num
    seen, unique = set(), []
    for r in records:
        key = r.doc_num or f"{r.owner}|{r.filed}|{r.doc_type}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    records = unique

    if not args.skip_cad:
        enrich_cad(records)
    detect_changes(records)
    score_records(records, start)
    records.sort(key=lambda r: (r.status != "NEW", -r.score))
    if not records:
        log.warning("No records found. Writing empty output files.")
    else:
        log.info("Total after dedup + enrichment: %d", len(records))
    write_outputs(records, start, end)
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("  Total records  : %d", len(records))
    log.info("  With address   : %d", sum(1 for r in records if r.prop_address))
    log.info("  Score >= 70    : %d", sum(1 for r in records if r.score >= 70))
    log.info("  Score >= 50    : %d", sum(1 for r in records if r.score >= 50))
    for c in ("TAXFC","FC"):
        log.info("  cat %-6s     : %d", c, sum(1 for r in records if r.cat == c))


if __name__ == "__main__":
    main()
