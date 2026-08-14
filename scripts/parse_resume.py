#!/usr/bin/env python3
"""Parse `master_resume.tex` into the JSON the board and the Worker read.

`master_resume.tex` is deliberately too full -- every project, internship and
skill, about 1.35 pages of a 1-page document. Cutting it down for a specific
posting is Claude's job, in `worker/trigger.js`, on demand. This module does
the part that shouldn't involve a model at all: turning LaTeX into structured
data, once per scan, so nothing downstream has to parse LaTeX.

**Everything published here is plain text, not LaTeX.** The model is shown
plain text and answers in plain text; the board escapes its way back to LaTeX
when you download the `.tex`. That means a model can't emit broken LaTeX,
can't unbalance a brace, and can't inject a macro -- the worst it can do is
write a sentence. Three things stay raw because they're re-emitted verbatim
and never shown to the model: the preamble, the header block, and the wrapper
around the skills line.

Each entry carries a stable `id` slugged from its section and organisation,
which is how the Worker checks the model's answer against this file: an entry
the master doesn't have is a fabricated job, and it's rejected before it ever
reaches the page.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache


# --------------------------------------------------------------- LaTeX bits
def _braced(src: str, i: int) -> tuple[str, int]:
    """Read the `{...}` group at `src[i]`, returning its body and the index
    just past the closing brace. Brace-aware rather than regex-based because
    an argument can legitimately contain braces (`{\\LARGE\\bfseries ...}`)."""
    if i >= len(src) or src[i] != "{":
        raise ValueError("expected '{'")
    depth, start, n = 0, i + 1, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:i], i + 1
        i += 1
    raise ValueError("unbalanced braces")


def _macro(line: str, name: str, count: int) -> list[str] | None:
    """`\\headerrow{a}{b}` -> `["a", "b"]`, or None if this line isn't one."""
    m = re.match(rf"\s*\\{name}\s*(?=\{{)", line)
    if not m:
        return None
    i, args = m.end(), []
    try:
        for _ in range(count):
            arg, i = _braced(line, i)
            args.append(arg)
    except ValueError:
        return None
    return args


_UNWRAP = {"textbf": "b", "bfseries": None, "textit": "i", "emph": "i",
           "underline": "u", "texttt": "code", "textrm": None, "mbox": None}
_SYMBOL = {"&": "&amp;", "%": "%", "$": "$", "#": "#", "_": "_",
           "{": "{", "}": "}"}


@lru_cache(maxsize=4096)
def tex_html(src: str) -> str:
    """A LaTeX inline fragment as HTML.

    Deliberately small: it handles the macros the master actually uses
    (`\\href`, `\\textbf`, `\\textit`, escaped symbols, en dashes) and drops
    anything else rather than guessing.
    """
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            m = re.match(r"\\([a-zA-Z]+)\*?", src[i:])
            if not m:                                    # \& \% \$ \# \_ \\ ...
                nxt = src[i + 1] if i + 1 < n else ""
                if nxt == "\\":
                    j = i + 2
                    if j < n and src[j] == "[":          # \\[3pt]
                        j = src.find("]", j) + 1 or j
                    out.append("<br>")
                    i = j
                    continue
                out.append(_SYMBOL.get(nxt, ""))
                i += 2
                continue
            cmd, j = m.group(1), i + m.end()
            if cmd == "href":
                try:
                    url, j = _braced(src, j)
                    txt, j = _braced(src, j)
                except ValueError:
                    i = j
                    continue
                out.append('<a href="%s" target="_blank" rel="noopener">%s</a>'
                           % (html.escape(tex_text(url), quote=True), tex_html(txt)))
                i = j
                continue
            if cmd in _UNWRAP:
                tag = _UNWRAP[cmd]
                if j < n and src[j] == "{":
                    body, j = _braced(src, j)
                    inner = tex_html(body)
                    out.append(f"<{tag}>{inner}</{tag}>" if tag else inner)
                i = j
                continue
            i = j                                        # unknown switch: drop
            continue
        if c in "{}":
            i += 1
            continue
        if c == "$":                                     # $\bullet$ and friends
            i += 1
            continue
        if c == "~":
            out.append("&nbsp;")
            i += 1
            continue
        if src.startswith("---", i):
            out.append("&mdash;")
            i += 3
            continue
        if src.startswith("--", i):
            out.append("&ndash;")
            i += 2
            continue
        out.append(html.escape(c))
        i += 1
    return "".join(out)


@lru_cache(maxsize=4096)
def tex_text(src: str) -> str:
    """Plain text -- what the model is shown, and what it answers in."""
    return html.unescape(re.sub(r"<[^>]+>", "", tex_html(src)))


def _split_commas(text: str) -> list[str]:
    """Split a skills run on top-level commas only -- `AWS (EC2, S3, Lambda)`
    is one skill, not three."""
    parts, depth, buf = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


# ------------------------------------------------------------------- model
@dataclass
class Block:
    """One entry: a job, a project, or the loose text under a heading."""
    section: str
    order: int
    org: str = ""                                   # \headerrow{1} / \entrytitle
    place: str = ""                                 # \headerrow{2}
    kind: str = "entry"                             # "entry" | "project" | "text"
    subs: list[tuple[str, str]] = field(default_factory=list)
    paras: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{slug(self.section)}:{slug(tex_text(self.org)) or self.order}"


@dataclass
class Section:
    name: str
    blocks: list[Block] = field(default_factory=list)
    skills: list[str] | None = None                 # set on the skills section
    skills_open: str = ""                           # verbatim tex around the run
    skills_close: str = ""


@dataclass
class Master:
    preamble: str
    header_tex: str
    name: str
    contact_html: str
    sections: list[Section]


# ------------------------------------------------------------------ parsing
_SECTION = re.compile(r"\\section\s*(?=\{)")


def parse_master(src: str) -> Master:
    head, _, rest = src.partition(r"\begin{document}")
    body = rest.partition(r"\end{document}")[0]

    cuts = [(m.start(), m.end()) for m in _SECTION.finditer(body)]
    header_tex = body[: cuts[0][0]].strip("\n") if cuts else body.strip("\n")

    sections: list[Section] = []
    order = 0
    for idx, (start, after) in enumerate(cuts):
        title, content_at = _braced(body, after)
        end = cuts[idx + 1][0] if idx + 1 < len(cuts) else len(body)
        sec = Section(name=tex_text(title).strip())
        content = body[content_at:end]
        if "SKILL" in sec.name.upper() and _parse_skills(sec, content):
            sections.append(sec)
            continue
        for chunk in re.split(r"^\s*\\gap\s*$", content, flags=re.M):
            for blk in _parse_blocks(sec.name, chunk, order):
                sec.blocks.append(blk)
                order += 1
        sections.append(sec)

    name, contact = _parse_header(header_tex)
    return Master(preamble=head, header_tex=header_tex, name=name,
                  contact_html=contact, sections=sections)


def _parse_skills(sec: Section, content: str) -> bool:
    """Pull the comma-separated run out of `{\\small \\justifying ... \\par}`,
    keeping the wrapper verbatim. Returns False if this doesn't look like a
    skills run, in which case the caller parses it as ordinary blocks."""
    lines = content.split("\n")
    body_idx = [i for i, ln in enumerate(lines) if ln.count(",") >= 2]
    if not body_idx:
        return False
    lo, hi = body_idx[0], body_idx[-1]
    sec.skills_open = "\n".join(lines[:lo]).strip("\n")
    sec.skills_close = "\n".join(lines[hi + 1:]).strip("\n")
    sec.skills = _split_commas(" ".join(lines[lo:hi + 1]))
    return bool(sec.skills)


def _parse_blocks(section: str, chunk: str, order: int) -> list[Block]:
    """One `\\gap`-delimited chunk. Usually a single entry, but Education runs
    a heading, two subrows and a loose Affiliations line together."""
    blocks: list[Block] = []
    cur: Block | None = None
    lines = chunk.split("\n")
    i = 0

    def fresh(kind: str, org: str = "", place: str = "") -> Block:
        b = Block(section=section, order=order + len(blocks), kind=kind,
                  org=org, place=place)
        blocks.append(b)
        return b

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i += 1
        if not stripped:
            continue
        args = _macro(line, "headerrow", 2)
        if args:
            cur = fresh("entry", args[0], args[1])
            continue
        args = _macro(line, "entrytitle", 1)
        if args:
            cur = fresh("project", args[0])
            continue
        args = _macro(line, "subrow", 2)
        if args:
            if cur is None:
                cur = fresh("entry")
            cur.subs.append((args[0], args[1]))
            continue
        if re.match(r"\s*\\begin\{tight\}", line):
            if cur is None:
                cur = fresh("text")
            item: list[str] = []
            while i < len(lines) and not re.match(r"\s*\\end\{tight\}", lines[i]):
                ln = lines[i]
                i += 1
                m = re.match(r"\s*\\item\s*(.*)", ln)
                if m:
                    if item:
                        cur.bullets.append(" ".join(item).strip())
                    item = [m.group(1)]
                elif item and ln.strip():
                    item.append(ln.strip())
            if item:
                cur.bullets.append(" ".join(item).strip())
            i += 1                                       # past \end{tight}
            continue
        if stripped.startswith("\\"):                    # \vspace and friends
            continue
        if cur is None:
            cur = fresh("text")
        cur.paras.append(stripped)
    return blocks


def _parse_header(header_tex: str) -> tuple[str, str]:
    """Name and contact line out of the `center` block, for the preview. The
    LaTeX copy re-emits `header_tex` verbatim, so this only has to be good
    enough to look right on screen."""
    m = re.search(r"\\begin\{center\}(.*?)\\end\{center\}", header_tex, re.S)
    inner = m.group(1) if m else header_tex
    parts = re.split(r"\\\\(?:\[[^\]]*\])?", inner, maxsplit=1)
    name = tex_text(parts[0]).strip()
    contact = tex_html(parts[1]).strip() if len(parts) > 1 else ""
    return name, re.sub(r"(?:<br>|\s)+", " ", contact).strip()


# ----------------------------------------------------------------- publish
def to_json(master: Master) -> dict:
    """What lands in `site/master.json`. Read by the board (to render and to
    rebuild the `.tex`) and by the Worker (to build the prompt and to check
    the model's answer against it)."""
    sections = []
    for sec in master.sections:
        if sec.skills is not None:
            sections.append({
                "name": sec.name,
                "kind": "skills",
                "skills": [tex_text(s) for s in sec.skills],
                "open": sec.skills_open,
                "close": sec.skills_close,
            })
            continue
        sections.append({
            "name": sec.name,
            "kind": "entries",
            "entries": [
                {
                    "id": blk.id,
                    "kind": blk.kind,
                    "org": tex_text(blk.org),
                    "place": tex_text(blk.place),
                    "subs": [[tex_text(a), tex_text(b)] for a, b in blk.subs],
                    "paras": [tex_text(p) for p in blk.paras],
                    "bullets": [tex_text(b) for b in blk.bullets],
                }
                for blk in sec.blocks
            ],
        })
    return {
        # Raw LaTeX, re-emitted verbatim and never shown to the model.
        "preamble": master.preamble.rstrip("\n"),
        "header_tex": master.header_tex,
        "name": master.name,
        "contact_html": master.contact_html,
        "sections": sections,
    }


def load_master(path: str) -> Master:
    with open(path, encoding="utf-8") as f:
        return parse_master(f.read())


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Parse master_resume.tex into JSON for the board.")
    ap.add_argument("master", help="path to master_resume.tex")
    ap.add_argument("out", nargs="?", help="where to write the JSON (default: stdout)")
    args = ap.parse_args()

    data = to_json(load_master(args.master))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(data, f, separators=(",", ":"))
            f.write("\n")
    else:
        json.dump(data, sys.stdout, indent=2)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
