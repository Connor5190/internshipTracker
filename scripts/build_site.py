#!/usr/bin/env python3
"""Turn the enriched scan into the JSON the GitHub Pages board reads.

The board and the daily email are two views of one scan, sharing
`format_recap.buckets` so they agree on what a role is and which week it
landed in.

Neither drops anything for age -- an old listing still on a board is still
a job. The board hides roles first seen 60+ days ago until you search, but
that's the page's call: every role is in roles.json.

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

import argparse
import hashlib
import json
import sys
from datetime import datetime

import format_recap as fr
from update_ledger import TZ


def role_id(url: str) -> str:
    """Stable per-posting key. Hashed rather than raw because the URL is the
    natural identifier but contains characters Firebase forbids in keys
    (`.`, `#`, `$`, `/`, `[`, `]`)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def role_payload(company: str, m: dict) -> dict:
    return {
        "id": role_id(m["url"]),
        "company": company,
        "title": m["title"],
        "url": m["url"],
        # The email's squeeze, with a web page's wider budget.
        "location": fr.short_location(m, 80),
        "first_seen": m.get("first_seen") or "",
        # False means `first_seen` is when this scanner first noticed the
        # role, not when the employer posted it -- the page renders that
        # distinction as a `~` so an estimate never reads as fact.
        "exact": bool(m.get("date_is_posted")),
        # This role is an internship that names no cycle at all, so it's here
        # by inference rather than because it said "Summer 2027". The page
        # badges it, so the list can be complete without being misleading.
        "undated": m.get("matched_in") == "undated",
    }


def build(results: list[dict]) -> dict:
    today, week, rest, pages = fr.buckets(results)

    roles = [role_payload(c, m) for c, m in today + week + rest]
    generated_at = datetime.now(TZ).isoformat(timespec="seconds")
    roles.sort(key=lambda r: (r["first_seen"] or "0000-00-00"), reverse=True)

    failed = [r for r in results if r["error"]]
    blocked = [r["company"] for r in failed if fr.is_blocked(r)]
    broken = [
        {"company": r["company"], "error": r["error"]}
        for r in failed
        if not fr.is_blocked(r)
    ]

    return {
        "generated_at": generated_at,
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
    ap = argparse.ArgumentParser(
        description="Turn an enriched scan into the board's roles.json.")
    ap.add_argument("scan", help="scan_result_enriched.json")
    ap.add_argument("out", help="where to write roles.json")
    args = ap.parse_args()

    with open(args.scan) as f:
        results = json.load(f)
    data = build(results)
    with open(args.out, "w") as f:
        json.dump(data, f, separators=(",", ":"))
        f.write("\n")
    print(f"{len(data['roles'])} roles", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
