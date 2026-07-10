# Internship Tracker

Given a list of companies, checks their public job boards for internship roles
that mention **summer** or **2027** (either one counts).

## How it works

For each company it probes the public job APIs of the major applicant tracking
systems — Greenhouse, Lever, Ashby, SmartRecruiters, and Workable — using slugs
derived from the company name. Internship postings are matched if they mention
any of the target keywords in the title or (where available) the job description.
By default that means **summer** or **2027** — both are not required.

If a company hosts its own careers page, add the URL after a pipe in
`companies.txt` and that page will be fetched and scanned directly instead.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Edit `companies.txt` (one company per line), then:

```bash
python internship_tracker.py companies.txt
```

Options:

| Flag | Description |
|---|---|
| `--term "summer 2027"` | Keywords to match, any one counts (default: `summer` OR `2027`) |
| `--workers 8` | Number of parallel company lookups |
| `--no-descriptions` | Match only job titles (faster, fewer API calls) |
| `--json` | Output machine-readable JSON instead of a table |

## companies.txt format

```
Stripe
Databricks
My Startup | https://mystartup.com/careers
```

Lines starting with `#` are ignored. Companies that don't use one of the
supported job boards will be reported as "couldn't scan" — give those a
careers URL so the page can be scanned directly.
