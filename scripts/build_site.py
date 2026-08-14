#!/usr/bin/env python3
"""Turn the enriched scan into the JSON the GitHub Pages board reads.

The board and the daily email are two views of one scan, sharing
`format_recap._buckets` so they agree on what a role is and which week it
landed in.

Neither drops anything. Not for age -- an old listing still on a board is
still a job, and the board sorts and colours by age well enough that stale
ones sink on their own. Not for location either: each role carries a
`non_us` flag and the board turns it into an "Only USA" switch, so the
reader can see what the filter costs and turn it off. The email shows
everything.

They differ on scope. The email is a digest of what's new, so it renders
only today and this week and links here for the rest; the board is the full
standing list, backlog included, because that's what you work down.

They also differ on what gets sent. The email bakes in ages and buckets
because it's read once, the morning it arrives. The board can be left open
for days, so it ships `first_seen` and lets the page recompute ages and
buckets from the reader's own clock -- a tab open since Monday shouldn't
still be calling Monday's postings "today".

Each role carries a stable `id` (a hash of its URL, the same key the ledger
uses) so the applied-checkbox state can be stored against something that
survives a re-scan, a re-title, or a company being renamed.

Roles new enough to still be worth applying to also get a tailored resume cut
from `master_resume.tex` -- see `tailor_resume.py` -- written to
`site/resumes/<id>.json` and fetched by the board only when you open one.
They're kept out of `roles.json` deliberately: they're ~6 KB each, and the
board loads that file on every visit to render a table that doesn't need
them. Each role carries a `resume` flag instead, so the button only appears
where there's something behind it.

Which roles get one is decided by `RESUMES_FROM` and nothing else -- a role
qualifies if it was first seen on or after that date. A stored list would
have been another piece of unreconstructible state to commit and race over,
where a date needs no bookkeeping at all: the answer is the same however many
times the scan runs, and re-deriving it can't lose anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime

import format_recap as fr
import tailor_resume as tr

# Tailored resumes start here rather than covering the whole backlog: the
# board's older roles have been sitting there for weeks and were passed over
# once already. Move it back (or pass `--resumes all`) to cover everything --
# the tailoring is local keyword scoring, so a full pass costs seconds, not
# money.
RESUMES_FROM = date(2026, 8, 14)


def role_id(url: str) -> str:
    """Stable per-posting key. Hashed rather than raw because the URL is the
    natural identifier but contains characters Firebase forbids in keys
    (`.`, `#`, `$`, `/`, `[`, `]`)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def short_location(m: dict, limit: int = 80) -> str:
    """Same "first office, count the rest" squeeze the email does, but with a
    web page's wider budget and no HTML escaping -- the page sets these as
    text, not markup."""
    loc = (m.get("location") or "").strip()
    if not loc:
        return ""
    parts = [p.strip() for p in loc.split(";") if p.strip()]
    if len(parts) > 1:
        loc = f"{parts[0]} +{len(parts) - 1} more"
    if len(loc) > limit:
        loc = loc[: limit - 1].rstrip(" ,") + "…"
    return loc


def role_payload(company: str, m: dict) -> dict:
    return {
        "id": role_id(m["url"]),
        "company": company,
        "title": m["title"],
        "url": m["url"],
        "location": short_location(m),
        "first_seen": m.get("first_seen") or "",
        # False means `first_seen` is when this scanner first noticed the
        # role, not when the employer posted it -- the page renders that
        # distinction as a `~` so an estimate never reads as fact.
        "exact": bool(m.get("date_is_posted")),
        # This role is an internship that names no cycle at all, so it's here
        # by inference rather than because it said "Summer 2027". The page
        # badges it, so the list can be complete without being misleading.
        "undated": m.get("matched_in") == "undated",
        # Every place it lists is confidently outside the US. Shipped as a
        # flag rather than acted on here, so "only USA" is a switch the
        # reader can flip and see the consequences of, not a silent drop.
        "non_us": fr._non_us_only(m),
    }


def wants_resume(m: dict, scope: str) -> bool:
    """Whether this role gets a tailored resume.

    `first_seen` is the same date the board bands and sorts by, so "roles from
    here on" means exactly what the board's own "Opened today" heading means.
    A role the employer dated before the cutoff but that only reached the
    board later doesn't qualify -- it isn't new by the board's reckoning
    either, and one rule beats two that disagree at the edges.
    """
    if scope == "off":
        return False
    if scope == "all":
        return True
    seen = m.get("first_seen") or ""
    try:
        return date.fromisoformat(seen) >= RESUMES_FROM
    except ValueError:
        return False


def resume_filename(name: str, company: str, title: str) -> str:
    """What the browser calls the file you download. Names the role, because
    a downloads folder with nine copies of `resume.tex` in it is a downloads
    folder you can't use."""
    def slug(s: str) -> str:
        return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", s)).strip("_")

    stem = "_".join(p for p in (slug(name), slug(company), slug(title)[:48]) if p)
    return f"{stem or 'resume'}.tex"


def write_resumes(master: tr.Master, roles: list[tuple[str, dict, dict]],
                  out_dir: str, generated_at: str) -> int:
    """One JSON per role, fetched by the board only when a resume is opened.

    The directory is emptied first. These are derived from the scan, so a file
    for a posting that has come off the board is not history worth keeping --
    it's a stale resume for a job you can no longer apply to, and leaving it
    behind would grow the published site without bound.
    """
    os.makedirs(out_dir, exist_ok=True)
    for stale in os.listdir(out_dir):
        if stale.endswith(".json"):
            os.remove(os.path.join(out_dir, stale))

    written = 0
    for company, m, payload in roles:
        doc = tr.tailor(master, tr.Posting(
            company=company,
            title=m.get("title", ""),
            location=m.get("location", ""),
            snippet=m.get("snippet", ""),
        ))
        doc.update({
            "id": payload["id"],
            "company": company,
            "title": m.get("title", ""),
            "url": m["url"],
            "generated_at": generated_at,
            "filename": resume_filename(doc["name"], company, m.get("title", "")),
        })
        with open(os.path.join(out_dir, f"{payload['id']}.json"), "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        written += 1
    return written


def build(results: list[dict], master: tr.Master | None = None,
          resume_dir: str = "", scope: str = "new") -> dict:
    today, week, rest, pages = fr._buckets(results)

    roles = [role_payload(c, m) for c, m in today + week + rest]
    generated_at = datetime.now(fr.TZ).isoformat(timespec="seconds")

    tailored: list[tuple[str, dict, dict]] = []
    for (company, m), payload in zip(today + week + rest, roles):
        payload["resume"] = bool(master) and wants_resume(m, scope)
        if payload["resume"]:
            tailored.append((company, m, payload))
    if master and resume_dir:
        write_resumes(master, tailored, resume_dir, generated_at)

    roles.sort(key=lambda r: (r["first_seen"] or "0000-00-00"), reverse=True)

    failed = [r for r in results if r["error"]]
    blocked = [
        r["company"]
        for r in failed
        if "blocks automated access" in r["error"] or "requires a browser" in r["error"]
    ]
    broken = [
        {"company": r["company"], "error": r["error"]}
        for r in failed
        if r["company"] not in blocked
    ]

    return {
        "generated_at": generated_at,
        "companies_scanned": len(results),
        # So the board can say why the older rows have no resume button,
        # rather than leaving the gap to be read as a bug.
        "resumes_from": RESUMES_FROM.isoformat() if scope == "new" else "",
        "companies_matched": len({c for c, _ in today + week + rest}),
        "roles": roles,
        "manual": sorted(
            ({"company": c, "url": m["url"]} for c, m in pages),
            key=lambda d: d["company"].lower(),
        ),
        "blocked": sorted(blocked),
        "errors": sorted(broken, key=lambda d: d["company"].lower()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Turn an enriched scan into the board's roles.json.")
    ap.add_argument("scan", help="scan_result_enriched.json")
    ap.add_argument("out", help="where to write roles.json")
    ap.add_argument("--master", default="master_resume.tex",
                    help="master LaTeX resume to tailor from")
    ap.add_argument("--resume-dir", default="",
                    help="write per-role tailored resumes here (e.g. site/resumes)")
    ap.add_argument("--resumes", choices=("new", "all", "off"), default="new",
                    help="which roles get one: those first seen on or after "
                         f"{RESUMES_FROM} (new), every role (all), or none (off)")
    args = ap.parse_args()

    master = None
    if args.resume_dir and args.resumes != "off":
        try:
            master = tr.load_master(args.master)
        except (OSError, ValueError) as e:
            # A resume that failed to parse is a missing button, not a missing
            # board. Say so loudly and publish the rest.
            print(f"warning: no tailored resumes — {args.master}: {e}",
                  file=sys.stderr)

    with open(args.scan) as f:
        results = json.load(f)
    data = build(results, master, args.resume_dir, args.resumes)
    with open(args.out, "w") as f:
        json.dump(data, f, separators=(",", ":"))
        f.write("\n")

    print(f"{len(data['roles'])} roles, "
          f"{sum(1 for r in data['roles'] if r['resume'])} with a tailored resume",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
