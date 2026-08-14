#!/usr/bin/env python3
"""Cut a one-page, role-specific resume out of the master LaTeX resume.

`master_resume.tex` is deliberately too full -- it holds every project, every
internship and every skill, which is more than fits on a page and more than
any one posting cares about. This module parses that file into blocks, scores
each block against a posting, and emits the subset that fits, both as LaTeX
(the same macros, so it compiles unchanged) and as structured content the
board renders in the browser.

**It never writes a word.** Every bullet, date and skill in the output is
copied verbatim from the master. Tailoring here means *selecting and
ordering*, nothing else -- a resume you send to an employer is the last place
to let a generator improvise, and there is no model in the loop to check.

Scoring is keyword-based and runs in-process, so a scan can tailor a few
hundred roles in a couple of seconds with no API key, no network and no
per-role cost. Two signals decide a block's fate:

- **Domain coverage.** Both the posting and the block are turned into a
  weighted vector over `DOMAINS` (ml, infra, quant, ...), and the block is
  scored on how much of what the posting asks for it actually evidences --
  see `_relevance` for why that is not the same as cosine. Terms in the
  posting's *title* count triple: a title is three words long and every one
  of them is deliberate, where a company name is mostly noise.
- **Shared technology.** The master's own skills line doubles as the
  vocabulary of concrete tools. A posting that says "Terraform" and a bullet
  that says "Terraform" is a stronger signal than any amount of thematic
  overlap, so it's scored separately and weighted above it.

Both sides derive from the master's own text, so editing `master_resume.tex`
re-tunes the scoring for free -- there is no per-entry tag table to keep in
sync with the resume it describes.

What order things come out in is not a scoring decision:

- **Experience stays reverse-chronological.** Relevance decides what gets
  *cut*, never what goes first. A resume whose jobs are out of date order
  reads as one with something to hide.
- **Projects reorder freely.** They carry no dates and nobody expects an
  order, so the most relevant one leads.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

# --------------------------------------------------------------- page budget
#
# The geometry the master's preamble asks for: letterpaper, 0.45in side
# margins, 0.4in top and bottom. Everything below is measured in points
# against that, so a change to \usepackage[...]{geometry} only needs the three
# numbers here updated.
#
# A line count is not a typesetter, so the widths were measured rather than
# guessed: the board's preview is a real 8.5x11in sheet in the same font at
# the same size, and Chrome was asked where it actually wraps each of the
# master's bullets. Guessing put every resume ~15% short of the page, which
# costs a whole entry. `\usepackage{times}` and the preview's Times New Roman
# are metrically compatible, so one measurement covers both.
PAGE_PT = (11.0 - 0.4 - 0.4) * 72        # usable text height
BODY_PT = 12.0                           # baseline skip at 10pt
BODY_CPL = 130                           # chars per line, 10pt Times over 7.6in
BULLET_CPL = BODY_CPL - 4                # bullets lose 1.35em to the label
SMALL_PT = 11.25                         # \small line, used by the skills run
SMALL_CPL = 142
SECTION_PT = 24.5                        # \section: spacing + title + rule
GAP_PT = 3.0                             # \gap
LIST_PT = 1.0                            # tight list topsep

# Fill the page rather than leaving a tailored resume looking thin: anything
# under this fraction of the budget pulls dropped blocks back in, best first.
BACKFILL_AT = 0.88

# A block scoring below this share of the best in its section is off-topic
# enough to cut before the page is even full -- that pass is what makes this a
# tailored resume rather than a squeezed one. Backfill can still restore it.
OFF_TOPIC_AT = 0.38

# How many entries a section will not go below, however irrelevant they score.
# A resume with one job on it reads as a resume with one job on it.
SECTION_FLOOR = {"EXPERIENCE": 3, "PROJECT": 1}

# Sections that are never touched: who you are and where you study is not a
# thing to tailor, and the skills line is filtered rather than dropped.
PINNED = ("EDUCATION", "SKILL")

# A skills line is scanned, not read, so it can be long -- but a short one
# reads as a short list of skills. Below the minimum the filter tops back up
# from the master in its own order rather than leaving a thin line.
MAX_SKILLS = 42
MIN_SKILLS = 26


# ------------------------------------------------------------------ vocabulary
#
# One bag of terms per domain, matched on both sides -- the posting and the
# resume block -- so the two are always compared in the same units. Terms
# overlap between domains on purpose ("sql" is data *and* backend); a posting
# that is genuinely both simply weights both.
DOMAINS: dict[str, tuple[str, ...]] = {
    "ml": (
        "machine learning", "ml", "deep learning", "neural network", "nlp",
        "llm", "large language model", "genai", "generative ai",
        "artificial intelligence", "ai", "pytorch", "tensorflow",
        "scikit-learn", "sklearn", "xgboost", "classifier", "classification",
        "regression", "random forest", "logistic regression", "model",
        "modeling", "modelling", "training", "inference", "feature engineering",
        "recommendation", "recommender", "ranking", "applied scientist",
        "research scientist", "data scientist", "data science", "statistics",
        "statistical", "predictive", "prediction", "roc-auc", "log-loss",
        "transformer", "embedding", "fine-tuning", "chatbot", "accuracy",
    ),
    "data": (
        "data engineer", "data engineering", "etl", "elt", "pipeline",
        "data pipeline", "warehouse", "data warehouse", "bigquery",
        "snowflake", "spark", "hadoop", "kafka", "airflow", "dbt", "analytics",
        "analyst", "business intelligence", "tableau", "sql", "postgres",
        "postgresql", "mysql", "database", "query", "queryable", "dataset",
        "reporting", "dashboard", "pandas", "numpy", "jupyter", "data quality",
        "at scale", "big data", "ingestion", "schema",
    ),
    "infra": (
        "infrastructure", "devops", "sre", "site reliability",
        "platform engineering", "cloud", "aws", "gcp", "azure", "kubernetes",
        "docker", "container", "containerize", "terraform", "iac", "ansible",
        "ci/cd", "cicd", "continuous integration", "continuous delivery",
        "jenkins", "github actions", "build", "build system", "deployment",
        "deploy", "provision", "observability", "monitoring", "logging",
        "logs", "linux", "scalability", "reliability", "automation",
        "serverless", "lambda", "ec2", "s3", "rds", "orchestration", "fly.io",
        "render", "hosting",
    ),
    "backend": (
        "backend", "back-end", "back end", "server-side", "api", "apis",
        "rest", "restful", "graphql", "microservice", "microservices",
        "distributed systems", "service", "services", "grpc",
        "software engineer", "software engineering", "software development",
        "java", "python", "c++", "go", "golang", "rust", "node.js", "node",
        "express", "fastapi", "flask", "django", "concurrency", "latency",
        "throughput", "caching", "redis", "queue", "websockets", "http",
        "scraper", "scraping",
    ),
    "web": (
        "frontend", "front-end", "front end", "full stack", "fullstack", "ui",
        "ux", "react", "next.js", "angular", "vue", "typescript",
        "javascript", "html", "css", "tailwind", "web application", "web app",
        "browser", "accessibility", "responsive", "design system", "website",
        "single-page",
    ),
    "mobile": (
        "ios", "android", "mobile", "mobile app", "swift", "swiftui",
        "kotlin", "objective-c", "react native", "flutter", "app store",
        "bluetooth", "corebluetooth", "on-device", "wearable",
    ),
    "quant": (
        "quant", "quantitative", "trading", "trader", "markets", "equities",
        "derivatives", "portfolio", "hedge fund", "investment", "investing",
        "finance", "financial", "risk", "pricing", "alpha", "backtest",
        "securities", "asset management", "capital markets", "banking",
        "fintech", "actuarial", "valuation", "insider trading", "sec",
        "form 4", "credit", "underwriting",
    ),
    "vision": (
        "computer vision", "image", "imaging", "video", "opencv", "yolo",
        "yolov8", "ultralytics", "object detection", "segmentation",
        "graphics", "rendering", "augmented reality", "virtual reality",
        "ar/vr", "spatial", "camera", "perception", "media", "photo",
        "frame", "overlay", "pillow", "pil",
    ),
    "robotics": (
        "robot", "robotics", "embedded", "firmware", "hardware",
        "microcontroller", "sensor", "control system", "controls", "autonomy",
        "autonomous", "path planning", "motion planning", "signal processing",
        "rf", "lora", "telemetry", "real-time", "mechatronics", "electrical",
        "mechanical", "transceiver", "gateway",
    ),
    "security": (
        "security", "cybersecurity", "appsec", "infosec", "cryptography",
        "encryption", "vulnerability", "threat", "penetration testing",
        "authentication", "authorization", "compliance", "fraud", "identity",
        "privacy", "captcha",
    ),
    "ops": (
        "operations", "area manager", "supply chain", "logistics",
        "process improvement", "lean", "six sigma", "project management",
        "program management", "product manager", "product management",
        "business analyst", "consulting", "consultant", "business",
        "strategy", "marketing", "sales",
        "human resources", "recruiting", "customer", "stakeholder",
        "cross-functional", "mentor", "mentorship", "leadership",
    ),
}

# What the modal calls each domain when it explains why this resume looks the
# way it does. A tailoring you can't see the reasoning behind is one you can't
# correct.
DOMAIN_LABEL = {
    "ml": "machine learning", "data": "data", "infra": "cloud & CI/CD",
    "backend": "backend", "web": "frontend", "mobile": "mobile",
    "quant": "quant & finance", "vision": "computer vision",
    "robotics": "embedded & robotics", "security": "security",
    "ops": "operations & product",
}

# Languages stay on the skills line whoever is reading. A posting that never
# says "Python" still expects to see which languages you write.
CORE_SKILLS = {
    "python", "java", "c++", "c", "c#", "sql", "typescript", "javascript",
    "swift", "go", "rust", "kotlin", "scala", "r", "matlab", "git",
}


def _boundary(term: str) -> str:
    return rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"


@lru_cache(maxsize=8192)
def _word_re(term: str) -> re.Pattern[str]:
    """Whole-word matcher for one term. Cached because scoring a few hundred
    roles against the same vocabulary otherwise recompiles the same hundred
    patterns a few hundred times -- which was most of the run time."""
    return re.compile(_boundary(term))


DOMAIN_RE = {
    d: re.compile("|".join(f"({_boundary(t)})" for t in terms))
    for d, terms in DOMAINS.items()
}


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
    anything else rather than guessing. Cached because the same forty bullets
    are converted once per tailored role.
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
    """Plain text, for scoring and for measuring how wide a line runs."""
    txt = re.sub(r"<[^>]+>", "", tex_html(src))
    return html.unescape(txt)


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

    def text(self) -> str:
        bits = [self.org, *[f"{a} {b}" for a, b in self.subs], *self.paras,
                *self.bullets]
        return tex_text(" \u00b7 ".join(b for b in bits if b))

    def end_year(self) -> int | None:
        """The year this ended, for the recency prior. `Present` counts as now."""
        for _, right in self.subs:
            plain = tex_text(right)
            if re.search(r"present|current", plain, re.I):
                return date.today().year
            years = re.findall(r"\b(19|20)(\d{2})\b", plain)
            if years:
                return int(years[-1][0] + years[-1][1])
        return None


@dataclass
class Section:
    name: str
    blocks: list[Block] = field(default_factory=list)
    skills: list[str] | None = None                 # set on the skills section
    skills_open: str = ""                           # verbatim tex around the run
    skills_close: str = ""

    @property
    def floor(self) -> int:
        for key, n in SECTION_FLOOR.items():
            if key in self.name.upper():
                return n
        return 0

    @property
    def pinned(self) -> bool:
        return any(k in self.name.upper() for k in PINNED)


@dataclass
class Master:
    preamble: str
    header_tex: str
    name: str
    contact_html: str
    sections: list[Section]

    @property
    def all_skills(self) -> list[str]:
        for s in self.sections:
            if s.skills is not None:
                return s.skills
        return []


# ------------------------------------------------------------------ parsing
_SECTION = re.compile(r"\\section\s*(?=\{)")


def parse_master(src: str) -> Master:
    """Split `master_resume.tex` into blocks, keeping every argument in its
    original LaTeX so the tailored copy can be re-emitted losslessly."""
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
        if sec.pinned and "SKILL" in sec.name.upper() and _parse_skills(sec, content):
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
    lines = [ln for ln in content.split("\n")]
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
    """Name and contact line out of the `center` block, for the HTML preview.
    The LaTeX copy re-emits `header_tex` verbatim, so this only has to be good
    enough to look right on screen."""
    m = re.search(r"\\begin\{center\}(.*?)\\end\{center\}", header_tex, re.S)
    inner = m.group(1) if m else header_tex
    parts = re.split(r"\\\\(?:\[[^\]]*\])?", inner, maxsplit=1)
    name = tex_text(parts[0]).strip()
    contact = tex_html(parts[1]).strip() if len(parts) > 1 else ""
    contact = re.sub(r"(?:<br>|\s)+", " ", contact).strip()
    return name, contact


# ------------------------------------------------------------------ scoring
@lru_cache(maxsize=8192)
def _profile(text: str, weight: float = 1.0) -> dict[str, float]:
    """Distinct-term hits per domain. Distinct, not total, so one bullet
    saying "model" three times doesn't outweigh one that names three
    different tools."""
    low = text.lower()
    out: dict[str, float] = {}
    for dom, rx in DOMAIN_RE.items():
        hits = {m.group(0) for m in rx.finditer(low)}
        if hits:
            out[dom] = len(hits) * weight
    return out


def _merge(*profiles: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in profiles:
        for k, v in p.items():
            out[k] = out.get(k, 0.0) + v
    return out


def _relevance(posting: dict[str, float], block: dict[str, float],
               cap: float = 3.0) -> float:
    """How much of what the posting asks for this block actually evidences,
    in [0, 1].

    Deliberately *not* cosine. Cosine compares composition, so a two-line
    entry whose only technical word happens to be "software engineer" scores
    as well against a backend posting as one that spends nine terms on cloud
    infrastructure -- the thin entry's single hit is 100% of a very short
    vector. That is exactly backwards for deciding what earns space on a page.

    So each domain the posting names is scored on the block's *absolute*
    evidence, saturating at `cap` distinct terms, and the result is weighted
    by how much the posting cares about that domain. Evidence the posting
    didn't ask for is ignored rather than counted against the block.
    """
    total = sum(posting.values())
    if not total or not block:
        return 0.0
    return sum(w * min(1.0, block.get(d, 0.0) / cap)
               for d, w in posting.items()) / total


@dataclass
class Posting:
    company: str = ""
    title: str = ""
    location: str = ""
    snippet: str = ""

    def profile(self) -> dict[str, float]:
        # The title carries the weight: it's three deliberate words, where a
        # company name is mostly brand noise and a snippet is whatever text
        # happened to sit near the matched keyword.
        return _merge(_profile(self.title, 3.0),
                      _profile(self.company, 1.0),
                      _profile(self.snippet, 1.0))

    def text(self) -> str:
        return " ".join([self.title, self.company, self.snippet]).lower()


@lru_cache(maxsize=8)
def _skill_tokens(skills: tuple[str, ...]) -> dict[str, re.Pattern[str]]:
    """Concrete tools, taken from the master's own skills line so the list
    maintains itself. `AWS (EC2, S3, Lambda)` contributes all four."""
    toks: dict[str, re.Pattern[str]] = {}
    for skill in skills:
        plain = tex_text(skill)
        base = re.sub(r"\(.*?\)", " ", plain)
        inner = re.findall(r"\((.*?)\)", plain)
        for piece in [base, *inner]:
            for part in re.split(r"[,/]", piece):
                part = part.strip().lower()
                if len(part) >= 2 and not part.isdigit() and part not in toks:
                    toks[part] = _word_re(part)
    return toks


def score_blocks(master: Master, posting: Posting) -> dict[int, float]:
    """A relevance score per block. Absolute values mean nothing; only the
    ordering and the ratios within a section are ever used."""
    pprof = posting.profile()
    ptext = posting.text()
    tokens = _skill_tokens(tuple(master.all_skills))
    posting_tokens = {t: rx for t, rx in tokens.items() if rx.search(ptext)}
    this_year = date.today().year

    scores: dict[int, float] = {}
    for sec in master.sections:
        for blk in sec.blocks:
            btext = blk.text().lower()
            fit = _relevance(pprof, _profile(btext))
            shared = sum(1 for rx in posting_tokens.values() if rx.search(btext))
            overlap = min(1.0, 0.34 * shared)
            year = blk.end_year()
            if year is None:
                recency = 0.5                     # projects: undated by design
            else:
                recency = max(0.0, min(1.0, (year - (this_year - 4)) / 4.0))
            scores[blk.order] = 2.0 * fit + 1.4 * overlap + 0.9 * recency
    return scores


# ---------------------------------------------------------------- selection
@dataclass
class Plan:
    """What survived, and enough about what didn't to explain the page."""
    kept: dict[int, list[int]]                    # block order -> bullet indexes
    dropped: list[int]
    skills: list[str]
    emphasis: list[str]
    cost_pt: float
    project_order: list[int]


def _bullet_cost(bullet: str) -> float:
    return BODY_PT * max(1, math.ceil(len(tex_text(bullet)) / BULLET_CPL))


def _block_cost(blk: Block, bullets: list[int]) -> float:
    pt = 0.0
    if blk.org:
        pt += BODY_PT
    pt += BODY_PT * len(blk.subs)
    for para in blk.paras:
        pt += BODY_PT * max(1, math.ceil(len(tex_text(para)) / BODY_CPL))
    if bullets:
        pt += LIST_PT + sum(_bullet_cost(blk.bullets[i]) for i in bullets)
    return pt


def _skills_cost(skills: list[str]) -> float:
    if not skills:
        return 0.0
    width = len(", ".join(tex_text(s) for s in skills))
    return SECTION_PT + SMALL_PT * max(1, math.ceil(width / SMALL_CPL))


def _header_cost(master: Master) -> float:
    plain = re.sub(r"<[^>]+>", "", master.contact_html)
    return 21.0 + 3.0 + BODY_PT * max(1, math.ceil(len(plain) / BODY_CPL)) - 4.0


def _tailor_skills(master: Master, posting: Posting, kept_text: str) -> list[str]:
    """Filter the skills run, keeping the master's grouping intact.

    Order is preserved rather than sorted by relevance: the master's list is
    already grouped (languages, then ML, then cloud, ...) and a resorted run
    reads as a keyword dump. Reordering would also put the page's most
    scannable line out of step with every other copy of this resume.
    """
    skills = master.all_skills
    if not skills:
        return []
    pprof = posting.profile()
    hot = {d for d, v in pprof.items() if v >= max(pprof.values()) * 0.45} if pprof else set()
    ptext = posting.text()
    kept_low = kept_text.lower()

    picked: set[int] = set()
    for n, skill in enumerate(skills):
        plain = tex_text(skill).lower()
        base = re.sub(r"\(.*?\)", "", plain).strip()
        if not base:
            continue
        if (base in CORE_SKILLS                           # always shown
                or _word_re(base).search(ptext)           # the posting names it
                or _word_re(base).search(kept_low)        # a surviving bullet uses it
                or hot & set(_profile(plain))):           # in an emphasised domain
            picked.add(n)
    # A narrow posting can filter this down to a dozen entries, which reads as
    # a dozen skills rather than as a focused list. Top back up from the
    # master's own order -- these are all real skills, just ones this posting
    # gave no reason to lead with.
    for n in range(len(skills)):
        if len(picked) >= MIN_SKILLS:
            break
        picked.add(n)
    return [skills[n] for n in sorted(picked)][:MAX_SKILLS]


def plan(master: Master, posting: Posting) -> Plan:
    scores = score_blocks(master, posting)
    by_order = {b.order: b for s in master.sections for b in s.blocks}
    section_of = {b.order: s for s in master.sections for b in s.blocks}

    kept: dict[int, list[int]] = {
        o: list(range(len(b.bullets))) for o, b in by_order.items()
    }
    dropped: list[int] = []

    # The newest entry in a dated section stays whatever it scores. A resume
    # that skips your current internship because the posting is about robots
    # doesn't read as focused, it reads as a gap.
    anchors: set[int] = set()
    for sec in master.sections:
        if sec.pinned or not sec.floor:
            continue
        dated = [(b.end_year(), -b.order) for b in sec.blocks if b.end_year()]
        if dated:
            anchors.add(-max(dated)[1])

    def live(sec: Section) -> list[int]:
        return [b.order for b in sec.blocks if b.order in kept]

    def can_drop(order: int) -> bool:
        sec = section_of[order]
        return (order not in anchors and not sec.pinned
                and len(live(sec)) > sec.floor)

    # Bullets carry their own relevance so a long entry can be shortened
    # rather than cut. Scored against the posting alone: a bullet's job here
    # is to earn its two lines on *this* page.
    pprof = posting.profile()
    bullet_score = {
        (o, i): _relevance(pprof, _profile(tex_text(b.bullets[i]).lower()), cap=2.0)
        for o, b in by_order.items() for i in range(len(b.bullets))
    }

    # ---- pass 1: cut what's off-topic, before the page is even full
    for sec in master.sections:
        if sec.pinned or not sec.blocks:
            continue
        best = max((scores[b.order] for b in sec.blocks), default=0.0)
        if best <= 0:
            continue
        # Ties break towards the bottom of the section. The master's own order
        # is a statement of what matters most, so when the posting can't tell
        # two entries apart, the one its author put last is the one to lose.
        for blk in sorted(sec.blocks, key=lambda b: (scores[b.order], -b.order)):
            if scores[blk.order] < OFF_TOPIC_AT * best and can_drop(blk.order):
                del kept[blk.order]
                dropped.append(blk.order)

    # The skills line is filtered against what survived pass 1, so a tool only
    # named by a dropped bullet doesn't linger on the page with nothing behind
    # it. Later passes shorten the list but never re-pick it: re-deriving it
    # from a set that the budget is still shrinking would not converge.
    skills = _tailor_skills(
        master, posting, " ".join(by_order[o].text() for o in kept))

    def total() -> float:
        pt = _header_cost(master)
        for sec in master.sections:
            if sec.skills is not None:
                continue
            orders = live(sec)
            if not orders:
                continue
            pt += SECTION_PT + GAP_PT * max(0, len(orders) - 1)
            pt += sum(_block_cost(by_order[o], kept[o]) for o in orders)
        return pt + _skills_cost(skills)

    # ---- pass 2: make it fit
    #
    # Bullets go before whole entries: a reader would rather see five jobs at
    # two bullets each than three at three. Only once nothing is trimmable
    # does an entry come off the page.
    guard = 0
    while total() > PAGE_PT and guard < 400:
        guard += 1
        # Never below two bullets (or however few the master wrote): a
        # one-line job reads as a job that didn't go anywhere, and at that
        # point the page is better off without the entry at all.
        trimmable = [
            (scores[o] * (1 + bullet_score[(o, i)]), -o, -i)
            for o, idxs in kept.items()
            for i in idxs
            if len(idxs) > min(2, len(by_order[o].bullets))
        ]
        if trimmable:
            _, o, i = min(trimmable)
            o, i = -o, -i
            kept[o] = [x for x in kept[o] if x != i]
            continue
        droppable = [(scores[o], -o) for o in kept if can_drop(o)]
        if droppable:
            o = -min(droppable)[1]
            del kept[o]
            dropped.append(o)
            continue
        if len(skills) > MIN_SKILLS:
            skills = skills[: max(MIN_SKILLS, int(len(skills) * 0.85))]
            continue
        break                                     # nothing left that may go

    # ---- pass 3: fill the page back up
    #
    # Pass 1 is happy to cut an entry the posting has no use for, which on a
    # narrow posting can leave a thin page. A thin resume reads as a thin
    # candidate, so anything that fits comes back, best score first.
    for order in sorted(dropped, key=lambda o: (-scores[o], o)):
        if total() >= BACKFILL_AT * PAGE_PT:
            break
        blk = by_order[order]
        kept[order] = list(range(len(blk.bullets)))
        if total() > PAGE_PT:
            del kept[order]
        else:
            dropped.remove(order)

    # Projects carry no dates, so nobody expects an order -- lead with the one
    # this posting actually asked about. Experience is left alone: relevance
    # decides what gets cut there, never what comes first.
    project_order = []
    for sec in master.sections:
        if "PROJECT" in sec.name.upper():
            project_order = sorted(live(sec), key=lambda o: (-scores[o], o))

    hot = sorted(pprof.items(), key=lambda kv: (-kv[1], kv[0]))
    top = hot[0][1] if hot else 0.0
    emphasis = [DOMAIN_LABEL.get(d, d) for d, v in hot[:2] if top and v >= 0.5 * top]

    return Plan(kept=kept, dropped=sorted(dropped), skills=skills,
                emphasis=emphasis, cost_pt=total(), project_order=project_order)


# ----------------------------------------------------------------- emitting
def _ordered(sec: Section, p: Plan) -> list[Block]:
    live = [b for b in sec.blocks if b.order in p.kept]
    if p.project_order and "PROJECT" in sec.name.upper():
        rank = {o: i for i, o in enumerate(p.project_order)}
        live.sort(key=lambda b: rank.get(b.order, 1 << 20))
    return live


def render_tex(master: Master, p: Plan) -> str:
    """The tailored resume as LaTeX, using the master's own preamble and
    macros -- so it compiles with no edits, and a change to the master's
    styling shows up here without touching this file."""
    out = [master.preamble.rstrip("\n"), r"\begin{document}", "",
           master.header_tex, ""]
    for sec in master.sections:
        if sec.skills is not None:
            if not p.skills:
                continue
            out.append(f"\\section{{{sec.name}}}")
            if sec.skills_open:
                out.append(sec.skills_open)
            out.append(", ".join(p.skills))
            if sec.skills_close:
                out.append(sec.skills_close)
            out.append("")
            continue
        blocks = _ordered(sec, p)
        if not blocks:
            continue
        out.append(f"\\section{{{sec.name}}}")
        for n, blk in enumerate(blocks):
            if n:
                out.append(r"\gap")
            if blk.kind == "project" and blk.org:
                out.append(f"\\entrytitle{{{blk.org}}}")
            elif blk.org:
                out.append(f"\\headerrow{{{blk.org}}}{{{blk.place}}}")
            for left, right in blk.subs:
                out.append(f"\\subrow{{{left}}}{{{right}}}")
            for para in blk.paras:
                out.append(para)
            idxs = p.kept.get(blk.order, [])
            if idxs:
                out.append(r"\begin{tight}")
                for i in idxs:
                    out.append(f"  \\item {blk.bullets[i]}")
                out.append(r"\end{tight}")
        out.append("")
    out.append(r"\end{document}")
    return "\n".join(out) + "\n"


def render_content(master: Master, p: Plan) -> list[dict]:
    """The same selection as HTML-ready fragments, so the board renders the
    resume without shipping a LaTeX engine to the browser."""
    sections = []
    for sec in master.sections:
        if sec.skills is not None:
            if p.skills:
                sections.append({"name": sec.name,
                                 "skills": [tex_html(s) for s in p.skills]})
            continue
        entries = []
        for blk in _ordered(sec, p):
            entries.append({
                "org": tex_html(blk.org),
                "place": tex_html(blk.place),
                "subs": [[tex_html(a), tex_html(b)] for a, b in blk.subs],
                "paras": [tex_html(x) for x in blk.paras],
                "bullets": [tex_html(blk.bullets[i]) for i in p.kept[blk.order]],
            })
        if entries:
            sections.append({"name": sec.name, "entries": entries})
    return sections


def tailor(master: Master, posting: Posting) -> dict:
    """Everything the board needs for one role, in one payload."""
    p = plan(master, posting)
    optional = [b.order for s in master.sections if not s.pinned for b in s.blocks]
    return {
        "name": master.name,
        "contact": master.contact_html,
        "sections": render_content(master, p),
        "tex": render_tex(master, p),
        "emphasis": p.emphasis,
        "kept_entries": sum(1 for o in optional if o in p.kept),
        "total_entries": len(optional),
        "kept_skills": len(p.skills),
        "total_skills": len(master.all_skills),
        # Roughly how much of the page the estimate says this fills. The
        # preview draws the real 11in line too, so a bad estimate is visible
        # rather than silent.
        "fill": round(p.cost_pt / PAGE_PT, 3),
    }


def load_master(path: str) -> Master:
    with open(path, encoding="utf-8") as f:
        return parse_master(f.read())


# --------------------------------------------------------------------- cli
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Tailor the master resume to one posting.")
    ap.add_argument("master", help="path to master_resume.tex")
    ap.add_argument("--title", default="", help="the posting's job title")
    ap.add_argument("--company", default="", help="the hiring company")
    ap.add_argument("--location", default="")
    ap.add_argument("--snippet", default="")
    ap.add_argument("--format", choices=("tex", "json", "report"), default="tex")
    args = ap.parse_args()

    master = load_master(args.master)
    posting = Posting(company=args.company, title=args.title,
                      location=args.location, snippet=args.snippet)

    if args.format == "json":
        json.dump(tailor(master, posting), sys.stdout, indent=2)
        print()
        return 0

    p = plan(master, posting)
    if args.format == "tex":
        sys.stdout.write(render_tex(master, p))
        return 0

    scores = score_blocks(master, posting)
    by_order = {b.order: b for s in master.sections for b in s.blocks}
    print(f"{args.company} — {args.title}")
    print(f"emphasis: {', '.join(p.emphasis) or '—'}   "
          f"fill: {p.cost_pt / PAGE_PT:.0%}   skills: {len(p.skills)}"
          f"/{len(master.all_skills)}")
    for sec in master.sections:
        if sec.skills is not None:
            continue
        print(f"\n  {sec.name}")
        for blk in _ordered(sec, p) + [by_order[o] for o in p.dropped
                                       if by_order[o].section == sec.name]:
            on = blk.order in p.kept
            mark = "keep" if on else "DROP"
            n = len(p.kept.get(blk.order, []))
            print(f"    {mark}  {scores[blk.order]:5.2f}  "
                  f"{tex_text(blk.org)[:44]:<44} {n}/{len(blk.bullets)} bullets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
