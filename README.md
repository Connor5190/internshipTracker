# Internship Tracker

Scans ~215 companies' career sites for internship roles matching **summer** or
**2027** (either one counts, postings mentioning **2026** are excluded), and
emails a daily recap of what's new.

## How it works

For each company, `internship_tracker.py` tries — roughly in this order —
whatever will get real, structured job data rather than scraping raw HTML:

1. **Known ATS APIs**: Greenhouse, Lever, Ashby, SmartRecruiters, and Workable,
   auto-detected by guessing the company's slug. Workday boards are detected
   directly from a `*.myworkdayjobs.com` URL and queried via their job-search
   API (including a facet pass for tenants that explicitly tag postings as
   "Intern").
2. **Content-detected platforms**: some career sites use a shared vendor
   whose page embeds real job data in a way a generic HTML scrape would miss
   — detected by page content, not URL shape, so it applies automatically to
   any company on that platform:
   - **Phenom People** — the listing page embeds already-filtered results as
     JSON in a `<script>` tag.
   - **iCIMS "Jibe"** — a same-origin `/api/jobs` endpoint the page's own JS
     calls.
3. **Company-specific fetchers** for large employers with their own systems
   (Amazon, Google, Apple, Atlassian, JPMorgan Chase-style Oracle Recruiting
   Cloud tenants, Radancy-powered sites, etc.) — see `KNOWN_COMPANIES` in
   `internship_tracker.py`.
4. **Sitemap + detail-page scanning**: for career sites that are
   JavaScript-rendered on the listing page but publish a job sitemap *and*
   server-render individual job pages, the sitemap is fetched and every job
   page pulled directly. Cheap sites (a few dozen jobs, e.g. Aflac, Norfolk
   Southern) run this on every scan; Uber's ~600-job sitemap runs the same
   way since it only adds a minute or two to the total scan time.
5. **Plain page fallback**: if none of the above finds anything and a careers
   URL is given, the page is fetched and its visible text searched directly.
   This is the weakest signal (a single "page mentions it" hit, no per-role
   detail) and is only used when nothing more structured is available.

If a company has a real ATS *and* its own careers URL in `companies.txt`, both
are used — the plain page only **supplements** the structured result (in case
some roles are listed only on the company's own site), it never replaces a
working ATS-based scan.

A handful of companies (Bain, Citadel, Meta, Tesla) actively block automated
access and are reported as "couldn't scan" with a note to check manually.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Edit `companies.txt` (one company per line), then:

```bash
python3 internship_tracker.py companies.txt
```

Options:

| Flag | Description |
|---|---|
| `--term "summer 2027"` | Keywords to match, any one counts (default: `summer` OR `2027`) |
| `--workers 8` | Number of parallel company lookups |
| `--no-descriptions` | Match only job titles (faster, fewer API calls) |
| `--json` | Output machine-readable JSON instead of a table |

Postings with **2026** anywhere in the title are always excluded, regardless
of `--term`, since a lot of programs are titled things like "2026-2027 XYZ
Intern" and would otherwise match a "summer 2027" search.

## companies.txt format

```
Stripe
Databricks
My Startup | https://mystartup.com/careers
```

Lines starting with `#` are ignored. Every company should ideally have a
careers URL — even ones that already work well via an auto-detected ATS
benefit from it as a supplementary source, and it's the only way to catch
companies with no public ATS at all.

## Daily email recap

`.github/workflows/daily-recap.yml` runs the scanner every day at 7:00 AM
America/New_York and emails the results (via iCloud SMTP) to the addresses
configured in the workflow. Requires two repo secrets:

- `ICLOUD_EMAIL` — the sending/receiving iCloud address
- `ICLOUD_APP_PASSWORD` — an [app-specific password](https://appleid.apple.com)
  for that account

The recap (built by `scripts/format_recap.py`) is organized into:

- **🎉 First roles ever seen from these companies** — a company's very first
  match since tracking began
- **🆕 New in the last 7 days** — postings whose real posting date (pulled
  from the ATS where available) or first-seen date is within the last week
- **All current matches** — everything currently matching, with `[NEW]` tags
- **Couldn't scan** — companies that failed, with the reason

Two small ledger files persist state across runs and are committed back to
the repo automatically after each scan:

- `.state/seen_postings.json` — first-seen date per posting URL, used for
  "new" tracking when the source ATS doesn't expose its own posting date
- `.state/companies_seen.json` — which companies have ever had a match, used
  for the "first roles ever seen" section

You can trigger a run manually from the Actions tab or with
`gh workflow run daily-recap.yml`.
