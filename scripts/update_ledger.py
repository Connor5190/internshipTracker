#!/usr/bin/env python3
"""Track when each posting was first seen, and annotate today's scan results
with that history so the recap can call out what's new.

Reads scan_result.json (internship_tracker.py --json output) and a ledger
file mapping posting URL -> {first_seen, company, title}. Postings not
already in the ledger are recorded as first seen today. The ledger is
pruned of postings that are both no longer live and older than
RETENTION_DAYS, so it doesn't grow forever. Writes the updated ledger back
to disk and an enriched copy of the scan results (each match gets
`first_seen` and `is_new`) to the given output path.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

NEW_WINDOW_DAYS = 7
RETENTION_DAYS = 45


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: update_ledger.py <scan_result.json> <ledger.json> <enriched_out.json>",
            file=sys.stderr,
        )
        return 1
    scan_path, ledger_path, out_path = sys.argv[1:4]

    with open(scan_path) as f:
        results = json.load(f)

    try:
        with open(ledger_path) as f:
            ledger = json.load(f)
    except FileNotFoundError:
        ledger = {}

    today = date.today()
    seen_today: set[str] = set()

    for company in results:
        for m in company["matches"]:
            url = m["url"]
            seen_today.add(url)
            entry = ledger.get(url)
            if entry is None:
                entry = {
                    "first_seen": today.isoformat(),
                    "company": company["company"],
                    "title": m["title"],
                }
                ledger[url] = entry
            m["first_seen"] = entry["first_seen"]
            m["is_new"] = (today - date.fromisoformat(entry["first_seen"])).days < NEW_WINDOW_DAYS

    cutoff = today - timedelta(days=RETENTION_DAYS)
    ledger = {
        url: entry
        for url, entry in ledger.items()
        if url in seen_today or date.fromisoformat(entry["first_seen"]) >= cutoff
    }

    with open(ledger_path, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
        f.write("\n")

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
