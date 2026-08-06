# Internship Tracker

Scans ~505 companies' career sites for internship roles matching **summer** or
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
   - **Radancy** — a `/search-jobs/results` endpoint on the site's own
     origin. Worth trying even when the careers URL is a marketing landing
     page (e.g. `jobs.boeing.com/internships`), since the real board lives on
     the same host.
3. **Company-specific fetchers** for large employers with their own systems
   (Amazon, Google, Apple, Atlassian, JPMorgan Chase-style Oracle Recruiting
   Cloud tenants, etc.) — see `KNOWN_COMPANIES` in `internship_tracker.py`.
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

The recap (built by `scripts/format_recap.py`) is **a digest of what's new**,
not the full list. It carries only the last 7 days:

- **🔥 Posted today** — newest first, roomy, at the top
- **🆕 Earlier this week** — the rest of the last 7 days
- **Check by hand** — companies whose site has no structured job list, so
  all the scanner can say is "this page mentions your keywords". Listed as
  bare links rather than dumping the matched snippet
- **Footer** — the backlog count with a link to the board; then sites that
  block scrapers, collapsed to a name list since they fail identically every
  day; anything else that broke is spelled out, so a genuinely new failure
  stands out

The older backlog is counted, not listed. It barely changes day to day, so
re-sending it every morning only trains you to skim past the part that *is*
new — and the board holds it in a form you can sort, search and tick off.
Dropping it took a 300-role recap from ~35 KB to ~10 KB, comfortably clear of
Gmail's ~102 KB clipping threshold even on a heavy day. The subject line and
the stat row still count every open role, so the total never silently shrinks.

Each role carries a colour-coded age chip (red today → grey for old). A `~`
prefix (`~3w`) means the job board doesn't publish a posting date, so the
age is measured from when this scanner first saw the role and may understate
how old it really is.

**The email drops nothing.** Not for age, not for location — every role the
scan finds in the last 7 days is in it. Location is judged, but only so the
board can offer it as a switch; see below.

## Judging location

`_non_us_only` decides whether every place a role lists is confidently
outside the US. On a recent scan that was 113 of 239 roles. Nothing acts on
it automatically: `build_site.py` attaches it to each role as a `non_us`
flag, and the board turns it into an **Only USA** toggle. A filter nobody
can see is a filter nobody can correct.

A place reads as foreign only when it names a non-US country or a major
foreign city (`NON_US_RE`) **and** carries no US signal — a US state name, a
two-letter state code beside a comma or ZIP (`US_ABBR_RE`), or a known US
city (`US_CITY_RE`). Anything else — an unrecognised place, `5 Locations`,
`Flexible - Any SpaceX Site` — reads as US.

The asymmetry is deliberate. Hiding a US role is worse than showing a
foreign one, so ambiguity always resolves towards keeping: `London, KY` and
`Cambridge, MA` stay, and a role spanning Bangalore *and* Seattle stays,
because only roles where **every** listed place is confidently foreign are
flagged.

The residual risk is a bare foreign city that also exists in the US —
`London`, `Dublin`, `Bristol`, `Melbourne` all read as foreign when they
arrive with no country or state. On the scan this was built against, every
one was genuinely foreign (Jump Trading's London and Bristol desks, Amazon's
Dublin hub, an Amazon role literally titled *Amazon International*). If a US
one ever gets caught, delete that city from `NON_US_RE` — the qualified form
(`Dublin, OH`) is already safe.

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

The same scan, as a page you can tick things off on. Roles are one ranked
table — `#1`, `#2`, … — sorted newest-first with a rule between each age
band, or alphabetically by company. Search filters across company, role and
location; `/` jumps to the box.

Company names are coloured by how fresh the posting is, so recency reads off
the page without parsing a date:

| Band | Colour | |
|---|---|---|
| Opened today | rust | `--age0` |
| Earlier this week (1–6 days) | ochre | `--age1` |
| This month (7–29 days) | full-strength ink | `--age2` |
| Older, or undated | grey, recedes | `--age3` |

The name and the status dot both take their colour from a single class on the
row, so the two can't drift apart. Every value clears 4.5:1 against its
background in both light and dark — the company name is the largest text in
the row, and a "subtle" tint there is the one that gets missed.

Every role gets a checkbox. Ticking one leaves it where it is — greyed out
but still in its place in the table, so you don't lose your spot in a list
you're working down — and adds a copy to the **Applied** section at the
bottom. Both copies have a checkbox and both write the same key, so unticking
either one clears the pair. **Hide applied** drops the ticked ones out of the
main table without touching the Applied list.

A second checkbox, **Ignore**, is for roles you've looked at and don't want.
Ignoring one removes it from the table and discounts it from every "still to
do" number, so the tiles describe work you actually intend to do. They come
back with **Show ignored**, which carries the count so you always know how
many are tucked away — struck through and dimmed, with the checkbox ready to
un-ignore. Applied and ignored are independent, so a role you applied to and
later thought better of can be both; the Applied copy at the bottom has no
Ignore box, since it's a record of a decision already made.

The layout follows [internship-radar-2027.yuxhuang.com](https://internship-radar-2027.yuxhuang.com/):
a briefing masthead with the week's count set large, stat tiles, then a dense
ranked table. Georgia carries the numbers and company names, Arial everything
functional, on warm paper (`#f4f0e7`) rather than white — both faces are
web-safe, so there's no webfont for the page to wait on. Dark mode is a warm
inversion of the same palette.

The board and the email are two views of one scan, sharing
`format_recap._buckets` so they agree on what a role is and which week it
landed in.

**Neither drops anything.** Not for age — an old listing still sitting on a
board is still a job you can apply to, and newest-first sorting plus the
grey oldest band let stale ones sink without hiding them. Not for location
either: **Only USA** is a checkbox, on by default, labelled with what it's
costing you (`Only USA (113 elsewhere)`) so the trade-off is visible before
you decide. It's a per-device view preference, so it lives in
`localStorage`, not in the shared Firebase state.

They differ on scope. The email is a digest of the last 7 days; the board is
the full standing list, backlog included, because that's what you work down.

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

### Run it from the board

Two buttons in the masthead — **Re-scan now** (runs `update-board.yml`, then
reloads the table in place when it finishes) and **Email me the recap** (runs
`daily-recap.yml`). Both show progress and report the outcome; a failed run
says so rather than silently doing nothing.

Starting a workflow needs an authenticated GitHub API call, which a static
public page cannot make safely. A token in the page would be readable by
anyone — and wouldn't survive anyway, since GitHub scans public repositories
for its own tokens and revokes them. So `worker/trigger.js` does it instead:
a Cloudflare Worker holding the token in a secret, which the page calls.

It is still an unauthenticated endpoint, matching the rest of the board, so
two limits keep it dull for anyone who finds it:

- **Only those two workflows** can be started. Anything else is rejected, so
  it can't become a general "run arbitrary CI" button.
- **Cooldowns** — 10 minutes for the recap (its side effect lands in an
  inbox), 5 for a re-scan. GitHub's own run history is what enforces them,
  so there's no KV namespace to create and a run started from the Actions
  tab counts too.

**Setup, once:**

1. A [fine-grained PAT](https://github.com/settings/personal-access-tokens)
   scoped to **this repository only**, with **Actions: Read and write**.
   Nothing else.
2. ```bash
   cd worker
   npx wrangler secret put GITHUB_TOKEN   # paste the PAT
   npx wrangler deploy
   ```
3. Put the deployed URL in `triggerURL` in `site/config.js`, and push.

Left blank, the buttons don't render and the rest of the board is unaffected.
`ALLOWED_ORIGIN` in `wrangler.toml` restricts which page may call the Worker
from a browser — leave it pointed at the board.

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
   {"rules": {
     "applied": {".read": true, ".write": true},
     "ignored": {".read": true, ".write": true}
   }}
   ```
4. Copy the URL at the top of the Data tab (`https://…firebaseio.com`) into
   `databaseURL` in `site/config.js`, and push

That URL is meant to be public — it grants access to nothing but those two
lists. Don't widen the rules to the database root: Firebase's "test mode"
default is root-open, which lets anyone with the URL write anywhere in the
database rather than just to these two keys.

Until it's filled in the board still works, but falls back to `localStorage`
— state stays in one browser, and a banner on the page says so.
