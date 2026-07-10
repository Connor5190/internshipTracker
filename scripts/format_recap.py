#!/usr/bin/env python3
"""Turn internship_tracker.py --json output into an HTML email recap."""

from __future__ import annotations

import html
import json
import sys
from datetime import date


def build_html(results: list[dict]) -> str:
    total = len(results)
    matched = [r for r in results if r["matches"]]
    failed = [r for r in results if r["error"]]
    total_roles = sum(len(r["matches"]) for r in results)

    parts = [
        f"<h2>Internship Tracker Daily Recap &mdash; {date.today().isoformat()}</h2>",
        f"<p><b>{total_roles}</b> matching role(s) across <b>{len(matched)}</b> of "
        f"<b>{total}</b> companies scanned. <b>{len(failed)}</b> couldn't be scanned.</p>",
    ]

    parts.append("<h3>New matches</h3>")
    if not matched:
        parts.append("<p>No matching roles found today.</p>")
    else:
        for r in matched:
            parts.append(f"<p><b>{html.escape(r['company'])}</b><br>")
            rows = []
            for m in r["matches"]:
                loc = f" &mdash; {html.escape(m['location'])}" if m.get("location") else ""
                rows.append(
                    f"&bull; <a href=\"{html.escape(m['url'])}\">"
                    f"{html.escape(m['title'])}</a>{loc}"
                )
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
    if len(sys.argv) != 2:
        print("usage: format_recap.py <scan_result.json>", file=sys.stderr)
        return 1
    with open(sys.argv[1]) as f:
        results = json.load(f)
    print(build_html(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
