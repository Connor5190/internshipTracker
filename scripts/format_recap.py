#!/usr/bin/env python3
"""Turn internship_tracker.py --json output into an HTML email recap."""

from __future__ import annotations

import html
import json
import sys
from datetime import date

# Keep in sync with HEAVY_SCAN_ONLY_COMPANIES in internship_tracker.py --
# companies whose real coverage requires the expensive --heavy-scan pass,
# which only runs periodically (currently: Mondays), not on every scan.
HEAVY_SCAN_ONLY_COMPANIES = ["Uber"]


def _role_line(m: dict) -> str:
    loc = f" &mdash; {html.escape(m['location'])}" if m.get("location") else ""
    return (
        f"&bull; <a href=\"{html.escape(m['url'])}\">{html.escape(m['title'])}</a>{loc}"
    )


def build_html(results: list[dict], heavy_scan: bool = False) -> str:
    total = len(results)
    matched = [r for r in results if r["matches"]]
    failed = [r for r in results if r["error"]]
    total_roles = sum(len(r["matches"]) for r in results)
    new_count = sum(1 for r in results for m in r["matches"] if m.get("is_new"))
    first_match_companies = [r for r in matched if r.get("first_match")]

    parts = [
        f"<h2>Internship Tracker Daily Recap &mdash; {date.today().isoformat()}</h2>",
    ]

    if heavy_scan:
        parts.append(
            "<p><i>✅ Today's run included the periodic deep scan for "
            f"{', '.join(HEAVY_SCAN_ONLY_COMPANIES)}.</i></p>"
        )
    elif HEAVY_SCAN_ONLY_COMPANIES:
        parts.append(
            "<p><i>ℹ️ Today's run skipped the expensive periodic deep "
            f"scan, so {', '.join(HEAVY_SCAN_ONLY_COMPANIES)} may be missing roles "
            "that don't show up in a normal scan (runs periodically instead).</i></p>"
        )

    parts.append(
        f"<p><b>{total_roles}</b> matching role(s) across <b>{len(matched)}</b> of "
        f"<b>{total}</b> companies scanned &mdash; <b>{new_count}</b> new in the last "
        f"7 days. <b>{len(failed)}</b> couldn't be scanned.</p>"
    )

    if first_match_companies:
        parts.append("<h3>\U0001F389 First roles ever seen from these companies</h3>")
        for r in first_match_companies:
            parts.append(f"<p><b>{html.escape(r['company'])}</b><br>")
            parts.append("<br>".join(_role_line(m) for m in r["matches"]))
            parts.append("</p>")

    parts.append("<h1>\U0001F195 New in the last 7 days</h1>")
    new_by_company = [
        (r["company"], [m for m in r["matches"] if m.get("is_new")]) for r in matched
    ]
    new_by_company = [(c, ms) for c, ms in new_by_company if ms]
    if not new_by_company:
        parts.append("<p>No new roles since the last recap.</p>")
    else:
        for company, ms in new_by_company:
            parts.append(f"<p><b>{html.escape(company)}</b><br>")
            parts.append("<br>".join(_role_line(m) for m in ms))
            parts.append("</p>")

    parts.append("<h1>/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////</h1>")
    parts.append("<h1>All current matches</h1>")
    if not matched:
        parts.append("<p>No matching roles found today.</p>")
    else:
        for r in matched:
            parts.append(f"<p><b>{html.escape(r['company'])}</b><br>")
            rows = []
            for m in r["matches"]:
                badge = " <b>[NEW]</b>" if m.get("is_new") else ""
                rows.append(_role_line(m) + badge)
            parts.append("<br>".join(rows))
            parts.append("</p>")

    parts.append("<h3>Couldn't scan</h3>")
    if not failed:
        parts.append("<p>None &mdash; every company scanned cleanly.</p>")
    else:
        for r in failed:
            parts.append(
                f"<p><b>{html.escape(r['company'])}</b>: {html.escape(r['error'])}</p>"
            )

    return "\n".join(parts)


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("usage: format_recap.py <scan_result.json> [heavy_scan:true|false]",
              file=sys.stderr)
        return 1
    with open(sys.argv[1]) as f:
        results = json.load(f)
    heavy_scan = len(sys.argv) == 3 and sys.argv[2].lower() == "true"
    print(build_html(results, heavy_scan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
