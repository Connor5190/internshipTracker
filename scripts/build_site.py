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
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime

import format_recap as fr


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


def build(results: list[dict]) -> dict:
    today, week, rest, pages = fr._buckets(results)

    roles = [role_payload(c, m) for c, m in today + week + rest]
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
        "generated_at": datetime.now(fr.TZ).isoformat(timespec="seconds"),
        "companies_scanned": len(results),
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
    if len(sys.argv) != 3:
        print(
            "usage: build_site.py <scan_result_enriched.json> <out roles.json>",
            file=sys.stderr,
        )
        return 1
    with open(sys.argv[1]) as f:
        results = json.load(f)
    with open(sys.argv[2], "w") as f:
        json.dump(build(results), f, separators=(",", ":"))
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
