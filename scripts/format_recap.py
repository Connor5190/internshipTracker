#!/usr/bin/env python3
"""Turn internship_tracker.py --json output into an HTML email recap.

Built for skimming on a phone first thing in the morning:

* Every role appears exactly **once**, in the freshest bucket it qualifies
  for. The old layout re-listed today's roles under "this week" and again
  under "all matches", which inflated a 300-role scan to ~400 lines and
  pushed the email past Gmail's ~102 KB clipping threshold.
* Roles are sorted newest-first, and carry a colour-coded age chip so the
  urgent ones are findable without reading a word.
* Density increases as importance drops: today's roles get room to breathe,
  the long tail is a compact reference list.

Styling lives in one <style> block rather than inline on every element --
with a few hundred roles, inline styles alone blow the size budget. If a
client strips the block the email degrades to plain, correctly ordered,
still-complete HTML.
"""

from __future__ import annotations

import html
import json
import sys
from datetime import date

# Age chips are styled by CSS class rather than inline: with a few hundred
# roles an inline style on every row costs ~15 KB on its own.
# (max age in days, css class)
AGE_CHIPS = [(0, "c0"), (2, "c1"), (6, "c2"), (13, "c3"), (10**9, "c4")]

STYLE = """
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<style>
:root{color-scheme:light dark;supported-color-schemes:light dark}
body{margin:0;padding:0;background:#f6f7f9}
.wrap{max-width:640px;margin:0 auto;padding:20px 16px 32px;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
color:#1a1f26;background:#ffffff}
.hd{font-size:20px;font-weight:700;letter-spacing:-.01em;margin:0}
.dt{font-size:13px;color:#7b838d;margin:2px 0 16px}
.stat{font-size:26px;font-weight:700;line-height:1.1}
.hot{color:#b42318}
.statl{font-size:11px;color:#7b838d;text-transform:uppercase;letter-spacing:.06em}
.sec{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
color:#5b6673;margin:26px 0 10px;padding-bottom:6px;border-bottom:2px solid #e8eaed}
.co{font-size:13px;font-weight:700;color:#1a1f26;margin:14px 0 5px}
.cod{font-size:12px;font-weight:700;color:#5b6673;margin:11px 0 3px}
a.t{font-size:16px;line-height:1.35;color:#1552d8;text-decoration:none;font-weight:500}
a.d{font-size:14px;line-height:1.4;color:#2d6ae0;text-decoration:none}
i{font-style:normal;font-size:12px;color:#7b838d}
.m{display:block;margin:2px 0 9px}
.r{margin:0 0 5px}
.c0,.c1,.c2,.c3,.c4{display:inline-block;padding:1px 6px;border-radius:9px;
font-size:11px;font-weight:700;font-style:normal}
.c0{background:#fee2e2;color:#b42318}
.c1{background:#ffedd5;color:#c4320a}
.c2{background:#fef3c7;color:#a15c07}
.c3{background:#eef2f6;color:#5b6673}
.c4{background:#f4f5f7;color:#8a929c}
.none{font-size:14px;color:#7b838d;margin:6px 0}
.ft{margin-top:28px;padding-top:14px;border-top:1px solid #e8eaed;
font-size:11px;line-height:1.6;color:#6b7480}
.err{font-size:11px;line-height:1.55;color:#6b7480;margin:3px 0}
@media (prefers-color-scheme:dark){
body{background:#15181c}
.wrap{background:#1c2026;color:#e6e9ed}
.hd,.co,.stat{color:#f0f2f5}
.hot{color:#ff7a6b}
.sec{color:#9aa3ad;border-bottom-color:#2c323a}
.cod{color:#9aa3ad}
.ft{border-top-color:#2c323a}
.none{color:#9aa3ad}
a.t{color:#7aa8ff}
a.d{color:#6d9bf5}
.c0{background:#4a1d1d;color:#ff9b90}
.c1{background:#4a2c17;color:#ffb384}
.c2{background:#43350f;color:#ecc253}
.c3{background:#2b3138;color:#a3acb7}
.c4{background:#24292f;color:#8a929c}
i,.ft,.err,.dt,.statl{color:#858e99}
}
</style>
"""


def _age_days(m: dict) -> int | None:
    raw = m.get("first_seen")
    if not raw:
        return None
    try:
        days = (date.today() - date.fromisoformat(raw)).days
    except ValueError:
        return None
    return days if days >= 0 else None


def _age_key(m: dict) -> int:
    days = _age_days(m)
    return 10**9 if days is None else days


def _chip(m: dict) -> str:
    """Colour-coded age chip. '~' marks a first-seen date rather than a real
    posting date, so an estimate never reads as fact."""
    days = _age_days(m)
    if days is None:
        return ""
    if days == 0:
        text = "today"
    elif days == 1:
        text = "1d"
    elif days < 14:
        text = f"{days}d"
    elif days < 60:
        text = f"{days // 7}w"
    else:
        text = f"{days // 30}mo"
    if not m.get("date_is_posted"):
        text = "~" + text
    for limit, css in AGE_CHIPS:
        if days <= limit:
            break
    return f'<b class="{css}">{text}</b>'


def _group(pairs: list[tuple[str, dict]], by_age: bool) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for company, m in pairs:
        groups.setdefault(company, []).append(m)
    for ms in groups.values():
        ms.sort(key=_age_key)
    items = list(groups.items())
    if by_age:
        items.sort(key=lambda kv: (min(_age_key(m) for m in kv[1]), kv[0].lower()))
    else:
        items.sort(key=lambda kv: kv[0].lower())
    return items


def _location(m: dict, limit: int) -> str:
    """Some boards return every office as one string (Anduril's runs to 212
    characters), which swamps the role title. Keep the first, count the rest."""
    loc = (m.get("location") or "").strip()
    if not loc:
        return ""
    parts = [p.strip() for p in loc.split(";") if p.strip()]
    if len(parts) > 1:
        loc = f"{parts[0]} +{len(parts) - 1} more"
    if len(loc) > limit:
        loc = loc[: limit - 1].rstrip(" ,") + "…"
    return html.escape(loc)


def _render(
    pairs: list[tuple[str, dict]],
    by_age: bool,
    dense: bool,
    drop_exact_today: bool = False,
) -> list[str]:
    out: list[str] = []
    for company, ms in _group(pairs, by_age):
        out.append(
            f'<div class="{"cod" if dense else "co"}">{html.escape(company)}</div>'
        )
        for m in ms:
            title = html.escape(m["title"])
            url = html.escape(m["url"])
            # Under a "Posted today" heading a `today` chip says nothing --
            # but `~today` still earns its place, since it flags a date we
            # inferred rather than one the employer published.
            exact_today = _age_days(m) == 0 and m.get("date_is_posted")
            chip = "" if (drop_exact_today and exact_today) else _chip(m)
            if dense:
                # Chip leads the row so it can never orphan onto a line of
                # its own when a long title wraps, and so ages form a
                # scannable left-hand column.
                loc = _location(m, 42)
                lead = f"{chip} " if chip else ""
                tail = f" <i>{loc}</i>" if loc else ""
                out.append(
                    f'<div class="r">{lead}<a class="d" href="{url}">{title}</a>{tail}</div>'
                )
            else:
                bits = " · ".join(x for x in (chip, _location(m, 64)) if x)
                out.append(f'<div><a class="t" href="{url}">{title}</a></div>')
                if bits:
                    out.append(f'<i class="m">{bits}</i>')
    return out


def _buckets(results: list[dict]):
    """Split matches into today / this week / older, plus page-level hits.

    A `matched_in == "page"` hit isn't a role -- it means the careers site
    exposes no structured listing and its raw text merely mentioned the
    search terms. Its "title" is a 190-character snippet dump, so it's kept
    out of the role sections entirely and listed separately as a nudge to go
    look manually.
    """
    today, week, rest, pages = [], [], [], []
    for r in results:
        for m in r["matches"]:
            if m.get("matched_in") == "page":
                pages.append((r["company"], m))
            elif m.get("is_today"):
                today.append((r["company"], m))
            elif m.get("is_new"):
                week.append((r["company"], m))
            else:
                rest.append((r["company"], m))
    return today, week, rest, pages


def build_subject(results: list[dict]) -> str:
    """Inbox-line triage: the count you care about, before opening anything."""
    today, week, rest, _ = _buckets(results)
    total = len(today) + len(week) + len(rest)
    if today:
        return f"\U0001F525 {len(today)} new today · {total} open — Internship Tracker"
    if week:
        return f"{len(week)} new this week · {total} open — Internship Tracker"
    return f"Nothing new · {total} open — Internship Tracker"


def build_html(results: list[dict]) -> str:
    failed = [r for r in results if r["error"]]
    today, week, rest, pages = _buckets(results)
    total_roles = len(today) + len(week) + len(rest)
    role_companies = {c for c, _ in today + week + rest}

    p = [STYLE, '<div class="wrap">']
    p.append('<div class="hd">Internship Tracker</div>')
    p.append(
        f'<div class="dt">{date.today().strftime("%A, %B %-d, %Y")} &middot; '
        f'{len(role_companies)} of {len(results)} companies have matches</div>'
    )

    # The highlight colour goes on a class, not inline: an inline style wins
    # over the dark-mode media query, which would leave this number in dark
    # red on a dark background.
    cells = [
        (len(today), "new today", bool(today)),
        (len(week), "earlier this wk", False),
        (total_roles, "open total", False),
    ]
    p.append('<table cellpadding="0" cellspacing="0" role="presentation"><tr>')
    for value, label, hot in cells:
        cls = "stat hot" if hot else "stat"
        p.append(
            f'<td style="padding-right:26px">'
            f'<div class="{cls}">{value}</div>'
            f'<div class="statl">{label}</div></td>'
        )
    p.append("</tr></table>")

    p.append('<div class="sec">\U0001F525 Posted today</div>')
    if today:
        p += _render(today, by_age=True, dense=False, drop_exact_today=True)
    else:
        p.append('<div class="none">Nothing new today.</div>')

    if week:
        p.append('<div class="sec">\U0001F195 Earlier this week</div>')
        p += _render(week, by_age=True, dense=False)

    if rest:
        p.append(f'<div class="sec">Still open &middot; {len(rest)}</div>')
        p += _render(rest, by_age=False, dense=True)

    if not total_roles:
        p.append('<div class="none">No matching roles found today.</div>')

    if pages:
        p.append(f'<div class="sec">Check by hand &middot; {len(pages)}</div>')
        p.append(
            '<i class="m">No structured job list on these sites &mdash; their page '
            "text matched, so the roles (if any) need eyeballing.</i>"
        )
        links = " &nbsp;·&nbsp; ".join(
            f'<a class="d" href="{html.escape(m["url"])}">{html.escape(c)}</a>'
            for c, m in sorted(pages, key=lambda cm: cm[0].lower())
        )
        p.append(f'<div class="r">{links}</div>')

    p.append('<div class="ft">')
    p.append(
        "Ages are the employer's posting date where the job board exposes one. "
        "A <b>~</b> means that board doesn't, so the date shown is when this "
        "scanner first saw the role &mdash; it may be older."
    )
    if failed:
        # Sites that deliberately block scrapers fail identically every day.
        # Collapsing them to a name list keeps a genuinely new breakage --
        # a dead URL, a moved job board -- from hiding in the noise.
        known = [r for r in failed if "blocks automated access" in r["error"]
                 or "requires a browser" in r["error"]]
        unexpected = [r for r in failed if r not in known]
        if unexpected:
            p.append(f"<br><br><b>{len(unexpected)} couldn't be scanned</b>")
            for r in unexpected:
                reason = r["error"]
                if len(reason) > 110:
                    reason = reason[:108].rstrip() + "…"
                p.append(
                    f'<div class="err">{html.escape(r["company"])} &mdash; '
                    f'{html.escape(reason)}</div>'
                )
        if known:
            names = ", ".join(html.escape(r["company"]) for r in known)
            p.append(
                f'<br><br><b>{len(known)} block scrapers</b> (expected, check by '
                f'hand): {names}'
            )
    p.append("</div></div>")
    return "\n".join(p)


def main() -> int:
    args = sys.argv[1:]
    subject_only = "--subject" in args
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        print("usage: format_recap.py <scan_result.json> [--subject]", file=sys.stderr)
        return 1
    with open(paths[0]) as f:
        results = json.load(f)
    print(build_subject(results) if subject_only else build_html(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
