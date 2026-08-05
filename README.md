# Internship Tracker

Scans ~445 companies' career sites for internship roles matching **summer** or
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

A handful of companies (Microsoft, Meta, Goldman Sachs, Bloomberg, Delta Air
Lines, Citadel, Tesla, Bain & Company, Nutanix, EPAM, Fidelity Investments,
Akamai) actively block automated access and are reported as "couldn't scan"
with a note to check manually — see `BLOCKED_COMPANIES` in
`internship_tracker.py`.

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

The subject line carries the headline number (`🔥 27 new today · 283 open`)
so a quiet day can be dismissed straight from the inbox.

The recap itself (built by `scripts/format_recap.py`) is built for skimming
on a phone. **Every role appears exactly once**, in the freshest bucket it
qualifies for:

- **🔥 Posted today** — newest first, roomy, at the top
- **🆕 Earlier this week** — the rest of the last 7 days
- **Still open** — the older backlog, as a dense one-line-per-role reference
  sorted alphabetically by company
- **Check by hand** — companies whose site has no structured job list, so
  all the scanner can say is "this page mentions your keywords". Listed as
  bare links rather than dumping the matched snippet
- **Footer** — sites that block scrapers are collapsed to a name list, since
  they fail identically every day; anything else that broke is spelled out,
  so a genuinely new failure stands out

Each role carries a colour-coded age chip (red today → grey for old). A `~`
prefix (`~3w`) means the job board doesn't publish a posting date, so the
age is measured from when this scanner first saw the role and may understate
how old it really is.

Two kinds of role are counted but not shown, with the tally noted in the
footer so nothing vanishes silently:

- **Older than 3 months** (`MAX_AGE_DAYS`) — almost always a stale listing
  still sitting on a board.
- **Listed only in India** (`INDIA_RE`) — matched on country or major city
  name. A role spanning Bangalore *and* London is still shown; only ones
  where every listed location is Indian are dropped. Word boundaries keep
  "Indiana" and "Indianapolis" from matching.

Dates use **America/New_York**, not the runner's UTC clock — otherwise a run
triggered after 8pm Eastern is stamped with tomorrow's date and roles read as
"posted today" a day early.

Two earlier layout choices were measured and dropped: re-listing roles under
several headings inflated a 300-role scan to ~400 lines, and the resulting
108 KB email exceeded Gmail's ~102 KB limit, which silently truncated the
bottom of the message behind a "[Message clipped]" link.

Two small ledger files persist state across runs and are committed back to
the repo automatically after each scan:

- `.state/seen_postings.json` — first-seen date per posting URL, used for
  "new" tracking when the source ATS doesn't expose its own posting date
- `.state/companies_seen.json` — the date each company first ever had a
  match. No longer rendered in the recap, but still recorded, since it's
  history that can't be reconstructed after the fact

You can trigger a run manually from the Actions tab or with
`gh workflow run daily-recap.yml`.

## Applied board

<https://connor5190.github.io/internshipTracker/>

The same scan, as a page you can tick things off on. Every active role gets a
checkbox. Ticking one leaves it where it is — greyed out but still under its
company, still carrying its age chip, so you don't lose your place in a list
you're working down — and adds a copy to the **Applied** section at the
bottom. Both copies have a checkbox and both write the same key, so unticking
either one clears the pair.

The board and the email are two views of one scan, so the filtering lives in
one place — `scripts/build_site.py` reuses `format_recap._buckets`, and a role
hidden from the email (older than `MAX_AGE_DAYS`, or listed only in India) is
hidden from the board too.

What differs is what gets sent. The email bakes in ages and buckets because
it's read once, the morning it arrives. `site/roles.json` ships raw
`first_seen` dates and the page recomputes ages in the browser — a tab left
open since Monday shouldn't still be calling Monday's postings "today".

`.github/workflows/update-board.yml` scans and publishes `site/` via
`actions/deploy-pages`, on its own schedule — **6:30 AM and 6:30 PM
America/New_York**. Twice a day keeps the board under ~12 hours stale: the
morning run lands just before the 7am recap, so clicking through from the
email hits fresh data, and the evening run picks up the day's later postings.
`site/roles.json` is generated, not committed.

Both workflows scan independently and both write `.state/seen_postings.json`,
so each pushes the ledger with a rebase-and-retry rather than a bare `git
push` — the 30-minute offset makes a collision unlikely (a full run takes
about 2 minutes), but a lost race would cost first-seen dates that can't be
reconstructed. The board deploy runs *after* that commit for the same reason.

Trigger it by hand from the Actions tab or with
`gh workflow run update-board.yml`.

### Applied state

There is no sign-in: anyone with the URL can tick a box, which is the point —
it should work from a phone on the train without a login. State lives in a
Firebase Realtime Database, reached over its plain REST API. No SDK, so
there's no bundle to version-pin and the only configuration is one URL, and
live updates arrive over RTDB's own `EventSource` stream, so a box ticked on
a phone lands on a laptop that already has the page open.

Roles are keyed by a hash of their posting URL (the same key the ledger uses),
so a tick survives a re-scan. Each applied role stores its own title, company
and URL alongside the tick, so a posting that later comes off the board still
shows up in your history as something you applied to, marked *no longer
listed*, rather than silently vanishing.

**One-time setup:**

1. [console.firebase.google.com](https://console.firebase.google.com) → add a
   project (Analytics not needed)
2. Build → Realtime Database → Create Database → start in **test mode**
3. Rules tab → replace with:
   ```json
   {"rules": {"applied": {".read": true, ".write": true}}}
   ```
4. Copy the URL at the top of the Data tab (`https://…firebaseio.com`) into
   `databaseURL` in `site/config.js`, and push

That URL is meant to be public — it grants access to nothing but the `applied`
list. Don't widen the rules to the database root.

Until it's filled in the board still works, but falls back to `localStorage`
— state stays in one browser, and a banner on the page says so.
