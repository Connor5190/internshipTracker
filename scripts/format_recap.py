#!/usr/bin/env python3
"""Turn internship_tracker.py --json output into an HTML email recap."""

from __future__ import annotations

import html
import json
import sys
from datetime import date


def _age_label(m: dict) -> str:
    """How old this posting is, worded honestly about what we actually know.

    `first_seen` is the employer's real posting date when the ATS exposed
    one, and otherwise just the first date our own scans saw the posting --
    so say "posted" only in the former case.
    """
    raw = m.get("first_seen")
    if not raw:
        return ""
    try:
        days = (date.today() - date.fromisoformat(raw)).days
    except ValueError:
        return ""
    if days < 0:
        return ""
    if days == 0:
        when = "today"
    elif days == 1:
        when = "yesterday"
    else:
        when = f"{days} days ago"
    verb = "posted" if m.get("date_is_posted") else "first seen"
    return f' <span style="color:#888">({verb} {when})</span>'


def _role_line(m: dict) -> str:
    loc = f" &mdash; {html.escape(m['location'])}" if m.get("location") else ""
    return (
        f"&bull; <a href=\"{html.escape(m['url'])}\">{html.escape(m['title'])}</a>"
        f"{loc}{_age_label(m)}"
    )


def build_html(results: list[dict]) -> str:
    total = len(results)
    matched = [r for r in results if r["matches"]]
    failed = [r for r in results if r["error"]]
    total_roles = sum(len(r["matches"]) for r in results)
    new_count = sum(1 for r in results for m in r["matches"] if m.get("is_new"))
    today_count = sum(1 for r in results for m in r["matches"] if m.get("is_today"))

    parts = [
        f"<h2>Internship Tracker Daily Recap &mdash; {date.today().isoformat()}</h2>",
        f"<p><b>{total_roles}</b> matching role(s) across <b>{len(matched)}</b> of "
        f"<b>{total}</b> companies scanned &mdash; <b>{today_count}</b> posted today, "
        f"<b>{new_count}</b> new in the last 7 days. <b>{len(failed)}</b> couldn't be "
        f"scanned.</p>",
    ]

    parts.append("<h1>\U0001F525 Roles posted today</h1>")
    today_by_company = [
        (r["company"], [m for m in r["matches"] if m.get("is_today")]) for r in matched
    ]
    today_by_company = [(c, ms) for c, ms in today_by_company if ms]
    if not today_by_company:
        parts.append("<p>Nothing posted today.</p>")
    else:
        for company, ms in today_by_company:
            parts.append(f"<p><b>{html.escape(company)}</b><br>")
            parts.append("<br>".join(_role_line(m) for m in ms))
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
    if len(sys.argv) != 2:
        print("usage: format_recap.py <scan_result.json>", file=sys.stderr)
        return 1
    with open(sys.argv[1]) as f:
        results = json.load(f)
    print(build_html(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
