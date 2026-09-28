# El Paso-leads

Motivated-seller lead scraper for **El Paso County, Texas** — cloned from the
Bexar / Dallas / Comal / McLennan lead-scraper system (same pipeline, dashboard
contract, scoring, and daily GitHub-Actions cron), adapted to El Paso's **open**
data sources.

## Why this county is different

El Paso County's Official Public Records app (`apps.epcountytx.gov`) — which
indexes abstracts of judgment, tax / mechanic liens, lis pendens, heirship
affidavits **and** trustee-sale foreclosure notices — is protected by
**Cloudflare Turnstile** bot-detection. A normal browser passes it invisibly,
but a headless CI runner cannot, so that portal is intentionally **not** scraped.
This scraper builds on the county's **open** distress sources instead.

## Sources

1. **El Paso County Sheriff real-estate sale notices** (open HTML, no gate)
   `https://www.epcounty.com/1192/Sheriff-Sales`
   Upcoming Sheriff's sales of real estate. Each row: Sale Date, Sale Time,
   Sale Location, Description of Property. The Description carries the
   district-court cause number, CAD tax account number(s), sometimes a street
   address, and the legal description. Cause type sets the category:
   - `####DTX####` (delinquent-tax suit)  → **TAXFC** (tax foreclosure sale)
   - `####DCV####` (civil / execution)    → **FC** (execution / judgment sale)

2. **Enrichment — El Paso Central Appraisal District** (`epcad.org`, open)
   `account# → /Search?Keywords=<acct> → /Search/Details/<id>/<year>`, which
   exposes Owners Name, Location Address (situs), Mailing Address, Legal
   Description and Exemptions (HS = homestead / owner-occupied). When a sale row
   has no account number, EPCAD is searched by the street address instead.
   Absentee = situs ≠ mailing; out-of-state = mailing state ≠ TX.

## Pipeline

`scrape (Sheriff) → parse → EPCAD enrich → hash/dedupe → NEW/CHANGED (data/state.json)
→ score → export`. Identical to the other counties:

- `dashboard/records.json` — dashboard feed (per-record `status`, `first_seen`,
  `cat`∈{foreclosure,tax_lien,judgment,probate}, `cat_code`, `score`, `flags`,
  `absentee`, `out_of_state`, owner / grantee / prop_* / mail_* / legal / doc_num).
- `data/ghl_export.csv` — GoHighLevel import.
- `data/skiptrace_export.csv` — skip-trace (name split, mailing + property address).
- `data/state.json` — dedupe / first-seen memory across runs.

## Coverage note

Sheriff / constable sales are monthly (first Tuesday), so volume is low per run
but **accumulates** via the NEW/CHANGED state across runs — a focused list of
owners about to lose property at sale. The Turnstile-gated recorder index
(judgments, liens, lis pendens, heirship) and trustee-sale foreclosure notices
are **not** included. Constable-precinct sales are a future add.

## Run

```bash
pip install -r scraper/requirements.txt
python scraper/fetch.py            # default
python scraper/fetch.py --skip-cad # sheriff rows only, no enrichment
```

Automated daily at 13:00 UTC via `.github/workflows/scrape.yml`; dashboard
published to GitHub Pages from `dashboard/`.
