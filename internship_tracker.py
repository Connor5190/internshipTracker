#!/usr/bin/env python3
"""Internship Tracker.

Given a list of companies, searches their career sites for internship roles
matching any of the target keywords (default: summer OR 2027) and reports what
it finds.

It works by probing the public job-board APIs of the major applicant tracking
systems (Greenhouse, Lever, Ashby, SmartRecruiters, Workable) using slugs
derived from the company name. If a careers-page URL is provided for a company
instead, that page is fetched and scanned directly.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table

TIMEOUT = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

# "Summer Analyst/Associate" is what banks call their internships.
INTERN_RE = re.compile(
    r"\bintern(ship)?s?\b|\bco[- ]?op\b|\bsummer\s+(analyst|associate)s?\b",
    re.IGNORECASE,
)

console = Console()

# Companies whose only real coverage requires an expensive, many-request
# scan (e.g. fetching hundreds of individual job pages because the company
# has no public search API). Off by default -- enabled via --heavy-scan,
# meant to be run periodically rather than on every daily scan. See
# fetch_uber_full() for why Uber needs this.
HEAVY_SCAN_ONLY_COMPANIES = ["Uber"]
HEAVY_SCAN_ENABLED = False


@dataclass
class Role:
    title: str
    url: str
    location: str = ""
    matched_in: str = "title"  # "title" or "description"
    snippet: str = ""
    posted_date: str = ""  # ISO date the ATS says this was posted, if known


@dataclass
class CompanyResult:
    company: str
    source: str = ""  # which ATS / URL the data came from
    total_intern_roles: int = 0
    matches: list[Role] = field(default_factory=list)
    error: str = ""


def slugify_candidates(name: str) -> list[str]:
    """Possible ATS slugs for a company name, most likely first."""
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    joined = base.replace(" ", "")
    hyphen = base.replace(" ", "-")
    candidates = [joined, hyphen]
    # e.g. "Jane Street Capital" -> "janestreet"
    words = base.split()
    if len(words) > 2:
        candidates.append("".join(words[:2]))
    seen: set[str] = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def get_json(session: requests.Session, url: str, **kwargs) -> dict | list | None:
    try:
        resp = session.get(url, timeout=TIMEOUT, headers=HEADERS, **kwargs)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def strip_html(text: str) -> str:
    soup = BeautifulSoup(html.unescape(text or ""), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(" ")


# ---------------------------------------------------------------------------
# ATS providers. Each returns (postings, source_label) or None if the company
# isn't on that platform. Postings are dicts: title, url, location, text, and
# (where the ATS exposes it) posted_date, an ISO date string of when the
# posting actually went live -- used to tell genuinely new roles from ones
# we simply haven't scanned before.
# ---------------------------------------------------------------------------

def _iso_date_from_timestamp(text: str | None) -> str:
    """Parse an ISO-ish datetime string (Greenhouse/Ashby/Workable style)
    down to just the date. Returns '' if unparseable."""
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _iso_date_from_epoch_ms(ms) -> str:
    """Parse a Lever-style epoch-milliseconds timestamp. Returns '' if
    unparseable."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def fetch_greenhouse(session: requests.Session, slug: str):
    data = get_json(
        session, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    )
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    postings = []
    for job in data["jobs"]:
        postings.append(
            {
                "title": job.get("title", ""),
                "url": job.get("absolute_url", ""),
                "location": (job.get("location") or {}).get("name", ""),
                "text": "",
                "posted_date": _iso_date_from_timestamp(job.get("first_published")),
                "_detail": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job.get('id')}",
            }
        )
    return postings, f"Greenhouse ({slug})"


def fetch_greenhouse_detail(session: requests.Session, posting: dict) -> str:
    data = get_json(session, posting.get("_detail", ""))
    if isinstance(data, dict):
        return strip_html(data.get("content", ""))
    return ""


def fetch_lever(session: requests.Session, slug: str):
    data = get_json(session, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return None
    postings = []
    for job in data:
        postings.append(
            {
                "title": job.get("text", ""),
                "url": job.get("hostedUrl", ""),
                "location": (job.get("categories") or {}).get("location", "") or "",
                "text": job.get("descriptionPlain", "") or "",
                "posted_date": _iso_date_from_epoch_ms(job.get("createdAt")),
            }
        )
    return postings, f"Lever ({slug})"


def fetch_ashby(session: requests.Session, slug: str):
    data = get_json(session, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    postings = []
    for job in data["jobs"]:
        postings.append(
            {
                "title": job.get("title", ""),
                "url": job.get("jobUrl", "") or job.get("applyUrl", ""),
                "location": job.get("location", "") or "",
                "text": strip_html(job.get("descriptionHtml", "")),
                "posted_date": _iso_date_from_timestamp(job.get("publishedAt")),
            }
        )
    return postings, f"Ashby ({slug})"


def fetch_smartrecruiters(session: requests.Session, slug: str):
    postings = []
    offset = 0
    while True:
        data = get_json(
            session,
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?limit=100&offset={offset}",
        )
        if not isinstance(data, dict) or "content" not in data:
            return None if offset == 0 else (postings, f"SmartRecruiters ({slug})")
        for job in data["content"]:
            postings.append(
                {
                    "title": job.get("name", ""),
                    "url": f"https://jobs.smartrecruiters.com/{slug}/{job.get('id')}",
                    "location": (job.get("location") or {}).get("city", "") or "",
                    "text": "",
                }
            )
        offset += 100
        if offset >= data.get("totalFound", 0) or offset >= 1000:
            break
    return postings, f"SmartRecruiters ({slug})"


def fetch_workable(session: requests.Session, slug: str):
    try:
        resp = session.post(
            f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
            json={"query": "", "location": [], "department": [], "worktype": []},
            timeout=TIMEOUT,
            headers=HEADERS,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict) or "results" not in data:
        return None
    postings = []
    for job in data["results"]:
        loc = job.get("location") or {}
        postings.append(
            {
                "title": job.get("title", ""),
                "url": f"https://apply.workable.com/{slug}/j/{job.get('shortcode')}/",
                "location": loc.get("city", "") or loc.get("country", "") or "",
                "text": "",
                "posted_date": _iso_date_from_timestamp(job.get("published")),
            }
        )
    return postings, f"Workable ({slug})"


ATS_FETCHERS = [
    fetch_greenhouse,
    fetch_lever,
    fetch_ashby,
    fetch_smartrecruiters,
    fetch_workable,
]


# ---------------------------------------------------------------------------
# Company-specific fetchers for big employers that don't use the ATSes above.
# ---------------------------------------------------------------------------

MAX_DETAIL_FETCHES = 30  # cap per-job description requests per company


WORKDAY_POSTED_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.IGNORECASE)


def _iso_date_from_workday_posted_on(text: str | None) -> str:
    """Workday gives relative strings like 'Posted Today', 'Posted Yesterday',
    'Posted 7 Days Ago', or 'Posted 30+ Days Ago'. Convert to an approximate
    ISO date. Returns '' if unparseable."""
    m = WORKDAY_POSTED_RE.search(text or "")
    if not m:
        return ""
    label, days = m.group(1).lower(), m.group(2)
    if label == "today":
        days_ago = 0
    elif label == "yesterday":
        days_ago = 1
    elif days:
        days_ago = int(days)
    else:
        return ""
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _workday_search(session: requests.Session, base: str, host: str, site: str,
                    applied_facets: dict, search_text: str):
    """One paginated Workday search. Returns (postings, facets) where facets is
    the raw facet-definitions list from the first page (or None on failure)."""
    postings = []
    facets = None
    offset = 0
    while offset < 100:
        try:
            resp = session.post(
                f"{base}/jobs",
                json={"appliedFacets": applied_facets, "limit": 20,
                      "offset": offset, "searchText": search_text},
                timeout=TIMEOUT, headers=HEADERS,
            )
            if resp.status_code != 200:
                break
            data = resp.json()
        except (requests.RequestException, ValueError):
            break
        if facets is None:
            facets = data.get("facets")
        jobs = data.get("jobPostings", [])
        if not jobs:
            break
        for j in jobs:
            path = j.get("externalPath", "")
            postings.append({
                "title": j.get("title", ""),
                "url": f"https://{host}/en-US/{site}{path}",
                "location": j.get("locationsText", "") or "",
                "text": "",
                "posted_date": _iso_date_from_workday_posted_on(j.get("postedOn")),
                "_detail": f"{base}{path}",
                "_detail_kind": "workday",
            })
        offset += 20
        if offset >= data.get("total", 0):
            break
    return postings, facets


def _find_intern_facet_id(facets: list | None) -> str | None:
    """Look for a 'workerSubType'-style facet whose value is literally
    'Intern'/'Internship', e.g. Capital One's Workday board tags roles this
    way independent of job title wording."""
    for facet in facets or []:
        if facet.get("facetParameter") != "workerSubType":
            continue
        for value in facet.get("values", []):
            if re.fullmatch(r"intern(ship)?s?", value.get("descriptor", ""), re.IGNORECASE):
                return value.get("id")
    return None


def fetch_workday(session: requests.Session, host: str, site: str):
    """Workday job boards, e.g. nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite.
    Runs a text search for 'intern' (relevance-sorted, catches most roles),
    plus if the tenant tags an explicit 'Intern' worker sub-type facet, a
    second pass filtered on that facet to catch roles the title wouldn't
    otherwise match."""
    tenant = host.split(".")[0]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"

    postings, facets = _workday_search(session, base, host, site, {}, "intern")
    if not postings and facets is None:
        return None  # first request itself failed; not a valid Workday board

    intern_facet_id = _find_intern_facet_id(facets)
    if intern_facet_id:
        facet_postings, _ = _workday_search(
            session, base, host, site,
            {"workerSubType": [intern_facet_id]}, "",
        )
        seen = {p["url"] for p in postings}
        for p in facet_postings:
            p["is_intern_tagged"] = True
            if p["url"] not in seen:
                postings.append(p)
                seen.add(p["url"])

    return postings, f"Workday ({tenant})"


def fetch_amazon(session: requests.Session):
    postings = []
    offset = 0
    while offset < 500:
        data = get_json(
            session,
            "https://www.amazon.jobs/en/search.json"
            f"?base_query=intern&result_limit=100&offset={offset}",
        )
        if not isinstance(data, dict) or "jobs" not in data:
            return None if offset == 0 else (postings, "amazon.jobs")
        for j in data["jobs"]:
            postings.append({
                "title": j.get("title", ""),
                "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                "location": j.get("city", "") or j.get("location", "") or "",
                "text": " ".join(
                    strip_html(j.get(k) or "")
                    for k in ("description", "basic_qualifications",
                              "preferred_qualifications")
                ),
            })
        offset += 100
        if offset >= data.get("hits", 0):
            break
    return postings, "amazon.jobs"


def fetch_google(session: requests.Session):
    """Google careers search results are server-rendered."""
    postings = []
    seen: set[str] = set()
    for page in range(1, 6):
        try:
            resp = session.get(
                "https://www.google.com/about/careers/applications/jobs/results"
                f"?q=intern&page={page}",
                timeout=TIMEOUT, headers=HEADERS,
            )
            if resp.status_code != 200:
                break
        except requests.RequestException:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("a[href*='jobs/results/']")
        new = 0
        for a in links:
            href = (a.get("href") or "").split("?")[0]
            if not href or href in seen:
                continue
            h3 = a.find("h3") or (a.find_parent() and a.find_parent().find("h3"))
            title = (h3.get_text(strip=True) if h3 else a.get_text(strip=True))
            if not title:
                continue
            seen.add(href)
            new += 1
            url = ("https://www.google.com/about/careers/applications/" + href
                   if not href.startswith("http") else href)
            postings.append({
                "title": title, "url": url, "location": "", "text": "",
                "_detail": url, "_detail_kind": "page",
            })
        if new == 0:
            break
    return (postings, "google.com/about/careers") if postings else None


def fetch_apple(session: requests.Session):
    """Apple's search page embeds results as hydration JSON."""
    postings = []
    for page in range(1, 4):
        try:
            resp = session.get(
                f"https://jobs.apple.com/en-us/search?search=intern&page={page}",
                timeout=TIMEOUT, headers=HEADERS,
            )
            if resp.status_code != 200:
                break
        except requests.RequestException:
            break
        m = re.search(
            r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((.+?)\);?\s*</script>",
            resp.text, re.DOTALL,
        )
        if not m:
            break
        try:
            data = json.loads(json.loads(m.group(1)))
        except ValueError:
            break

        def walk(obj):
            if isinstance(obj, dict):
                if "postingTitle" in obj and "positionId" in obj:
                    yield obj
                else:
                    for v in obj.values():
                        yield from walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from walk(v)

        found = list(walk(data))
        if not found:
            break
        for j in found:
            postings.append({
                "title": j.get("postingTitle", ""),
                "url": f"https://jobs.apple.com/en-us/details/{j.get('positionId')}",
                "location": (j.get("locations") or [{}])[0].get("name", "")
                if isinstance(j.get("locations"), list) else "",
                "text": strip_html(
                    " ".join(str(j.get(k) or "") for k in
                             ("jobSummary", "description", "minimumQualifications"))
                ),
            })
    return (postings, "jobs.apple.com") if postings else None


def fetch_oracle_cloud(session: requests.Session, host: str, site: str,
                       label: str, keywords: list[str]):
    """Oracle Recruiting Cloud (e.g. JPMorgan Chase)."""
    postings, seen = [], set()
    for kw in keywords:
        for offset in range(0, 300, 100):
            url = (
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                f"?onlyData=true&expand=requisitionList.secondaryLocations"
                f'&finder=findReqs;siteNumber={site},keyword="{kw}",'
                f"facetsList=LOCATIONS,limit=100,offset={offset},"
                f"sortBy=POSTING_DATES_DESC"
            )
            data = get_json(session, url)
            if not isinstance(data, dict) or not data.get("items"):
                break
            reqs = data["items"][0].get("requisitionList", [])
            if not reqs:
                break
            for j in reqs:
                rid = j.get("Id")
                if rid in seen:
                    continue
                seen.add(rid)
                postings.append({
                    "title": j.get("Title", ""),
                    "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                           f"{site}/job/{rid}",
                    "location": j.get("PrimaryLocation", "") or "",
                    "text": strip_html(j.get("ShortDescriptionStr", "") or ""),
                    "posted_date": _iso_date_from_timestamp(j.get("PostedDate")),
                })
    return (postings, label) if postings else None


def fetch_radancy(session: requests.Session, base: str, label: str):
    """Radancy-powered job sites (e.g. lockheedmartinjobs.com)."""
    postings, seen = [], set()
    for page in range(1, 6):
        try:
            resp = session.get(
                f"{base}/search-jobs/results?ActiveFacetID=0&CurrentPage={page}"
                "&RecordsPerPage=100&Distance=50&RadiusUnitType=0&Keywords=intern"
                "&ShowRadius=False&IsPagination=False&FacetType=0"
                "&SearchResultsModuleName=Search+Results"
                "&SearchFiltersModuleName=Search+Filters"
                "&SortCriteria=0&SortDirection=0&SearchType=5&ResultsType=0",
                timeout=TIMEOUT,
                headers={**HEADERS, "Accept": "application/json",
                         "X-Requested-With": "XMLHttpRequest"},
            )
            data = resp.json()
        except (requests.RequestException, ValueError):
            return None if page == 1 else (postings, label)
        soup = BeautifulSoup(data.get("results", ""), "html.parser")
        links = soup.select("a[href*='/job/']")
        new = 0
        for a in links:
            href = a.get("href") or ""
            if href in seen:
                continue
            seen.add(href)
            new += 1
            h2 = a.find("h2")
            title = h2.get_text(strip=True) if h2 else a.get_text(" ", strip=True)
            url = href if href.startswith("http") else base + href
            postings.append({
                "title": title, "url": url, "location": "", "text": "",
                "_detail": url, "_detail_kind": "page",
            })
        if new == 0:
            break
    return (postings, label) if postings else None


def fetch_atlassian(session: requests.Session):
    """Atlassian publishes all listings as JSON at a public endpoint."""
    data = get_json(session, "https://www.atlassian.com/endpoint/careers/listings")
    if not isinstance(data, list):
        return None
    postings = []
    for job in data:
        portal = job.get("portalJobPost") or {}
        postings.append({
            "title": job.get("title", ""),
            "url": portal.get("portalUrl", "")
                   or "https://www.atlassian.com/company/careers/all-jobs",
            "location": "; ".join(job.get("locations") or []),
            "text": strip_html(
                " ".join(str(job.get(k) or "") for k in
                         ("overview", "responsibilities", "qualifications"))
            ),
        })
    return (postings, "atlassian.com careers API") if postings else None


UBER_SITEMAP_URL = "https://jobs.uber.com/en/jobs/sitemap.xml"
UBER_POSTED_RE = re.compile(r"Posted on\s+([A-Za-z]+ \d{1,2},\s*\d{4})")
UBER_LOCATION_RE = re.compile(r"Location\s+(.*?)\s+Team\b")


def _fetch_uber_job_detail(session: requests.Session, url: str) -> dict | None:
    try:
        resp = session.get(url, timeout=TIMEOUT, headers=HEADERS)
        if resp.status_code != 200:
            return None
    except requests.RequestException:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    if not title:
        return None
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible = re.sub(r"\s+", " ", soup.get_text(" ")).strip()

    loc_m = UBER_LOCATION_RE.search(visible)
    posted_date = ""
    posted_m = UBER_POSTED_RE.search(visible)
    if posted_m:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                posted_date = datetime.strptime(posted_m.group(1), fmt).date().isoformat()
                break
            except ValueError:
                continue

    return {
        "title": title,
        "url": url,
        "location": loc_m.group(1) if loc_m else "",
        "text": visible,
        "posted_date": posted_date,
    }


def fetch_uber_full(session: requests.Session):
    """Uber's careers site (jobs.uber.com) is a client-rendered Next.js app
    -- the listing page just says "Loading jobs..." until JavaScript runs,
    and no public search/list API could be found. Individual job pages
    ARE server-rendered, though, and a sitemap lists every current
    posting, so this fetches the sitemap then every job page directly.
    That's a few hundred requests -- too expensive to run on every daily
    scan, hence gated behind HEAVY_SCAN_ENABLED (see --heavy-scan)."""
    try:
        resp = session.get(UBER_SITEMAP_URL, timeout=TIMEOUT, headers=HEADERS)
        if resp.status_code != 200:
            return None
    except requests.RequestException:
        return None
    urls = re.findall(r"<loc>(.*?)</loc>", resp.text)
    if not urls:
        return None

    postings = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = [pool.submit(_fetch_uber_job_detail, session, u) for u in urls]
        for future in as_completed(futures):
            job = future.result()
            if job:
                postings.append(job)
    return (postings, "Uber careers (full sitemap scan)") if postings else None


def fetch_uber(session: requests.Session):
    if not HEAVY_SCAN_ENABLED:
        return None
    return fetch_uber_full(session)


def _wd(host, site):
    return lambda s: fetch_workday(s, host, site)


# Company name (lowercased) -> fetcher(session) -> (postings, source) | None
KNOWN_COMPANIES = {
    "nvidia": _wd("nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite"),
    "salesforce": _wd("salesforce.wd12.myworkdayjobs.com", "External_Career_Site"),
    "capital one": _wd("capitalone.wd12.myworkdayjobs.com", "Capital_One"),
    "the home depot": _wd("homedepot.wd5.myworkdayjobs.com", "CareerDepot"),
    "home depot": _wd("homedepot.wd5.myworkdayjobs.com", "CareerDepot"),
    "ncr voyix": _wd("ncr.wd1.myworkdayjobs.com", "ext_us"),
    "amazon": fetch_amazon,
    "google": fetch_google,
    "apple": fetch_apple,
    "jpmorgan chase": lambda s: fetch_oracle_cloud(
        s, "jpmc.fa.oraclecloud.com", "CX_1001", "jpmorganchase.com",
        ["internship", "summer analyst", "summer", "2027"]),
    "jpmorgan": lambda s: fetch_oracle_cloud(
        s, "jpmc.fa.oraclecloud.com", "CX_1001", "jpmorganchase.com",
        ["internship", "summer analyst", "summer", "2027"]),
    "lockheed martin": lambda s: fetch_radancy(
        s, "https://www.lockheedmartinjobs.com", "lockheedmartinjobs.com"),
    "atlassian": fetch_atlassian,
    "fortinet": lambda s: fetch_oracle_cloud(
        s, "edel.fa.us2.oraclecloud.com", "CX_2001", "fortinet.com",
        ["intern", "internship"]),
    "uber": fetch_uber,
}

# Companies whose career sites block automated access.
BLOCKED_COMPANIES = {
    "microsoft": "Microsoft's careers site requires a browser — check "
                 "https://jobs.careers.microsoft.com/global/en/search?q=intern",
    "meta": "Meta's careers site blocks automated access — check "
            "https://www.metacareers.com/jobs",
    "goldman sachs": "Goldman's careers site blocks automated access — check "
                     "https://higher.gs.com/campus",
    "bloomberg": "Bloomberg's careers site blocks automated access — check "
                 "https://careers.bloomberg.com/job/search?qf=internships",
    "delta air lines": "Delta's careers site blocks automated access — check "
                       "https://delta.avature.net/careers",
    "citadel": "Citadel's careers site blocks automated access — check "
               "https://www.citadel.com/careers/students/",
    "tesla": "Tesla's careers site blocks automated access — check "
             "https://www.tesla.com/careers/search/?keyword=intern",
    "bain & company": "Bain's careers site blocks automated access — check "
                      "https://www.bain.com/careers/find-a-role/",
    "nutanix": "Nutanix's careers site blocks automated access — check "
               "https://careers.nutanix.com/en/jobs/",
    "epam": "EPAM's careers site blocks automated access — check "
            "https://www.epam.com/careers/job-listings",
}

# Recognize Workday careers URLs so we can query the job-board API instead of
# scraping the JS-rendered page, e.g.
# https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
WORKDAY_URL_RE = re.compile(
    r"https?://(?P<host>[\w-]+\.wd\d+\.myworkdayjobs\.com)"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/?#]+)"
)

# Likewise for hosted ATS board URLs: pull the slug out and hit the JSON API.
ATS_URL_ROUTES = [
    (re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([\w-]+)"),
     fetch_greenhouse),
    (re.compile(r"https?://jobs\.lever\.co/([\w-]+)"), fetch_lever),
    (re.compile(r"https?://jobs\.ashbyhq\.com/([\w-]+)"), fetch_ashby),
    (re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"),
     fetch_smartrecruiters),
]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def parse_term_keywords(term: str) -> list[str]:
    """Split a search term into OR keywords (comma- or space-separated)."""
    if "," in term:
        parts = [p.strip() for p in term.split(",")]
    else:
        parts = term.split()
    return [p for p in parts if p]


def term_patterns(term: str) -> list[re.Pattern]:
    """One pattern per keyword; a posting matches if ANY keyword hits."""
    pats = []
    for kw in parse_term_keywords(term):
        pats.append(re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
    return pats


def term_label(term: str) -> str:
    keywords = parse_term_keywords(term)
    if len(keywords) <= 1:
        return term
    return " or ".join(f'"{k}"' for k in keywords)


# Eligibility phrasing like "must graduate before Summer 2027" is not a role
# for that term, so ignore matches preceded by such wording.
ELIGIBILITY_RE = re.compile(
    r"(graduat\w*|degree)\s+(on\s+or\s+)?(before|by|prior to|after|between|no later than)\s*$",
    re.IGNORECASE,
)


def find_match(text: str, patterns: list[re.Pattern]) -> re.Match | None:
    for p in patterns:
        for m in p.finditer(text):
            preceding = text[max(0, m.start() - 60) : m.start()]
            if not ELIGIBILITY_RE.search(preceding.strip()):
                return m
    return None


def snippet_around(text: str, m: re.Match, radius: int = 70) -> str:
    start = max(0, m.start() - radius)
    return re.sub(r"\s+", " ", text[start : m.end() + radius]).strip()


def scan_company(company: str, url: str | None, term: str,
                 check_descriptions: bool) -> CompanyResult:
    session = requests.Session()
    result = CompanyResult(company=company)
    patterns = term_patterns(term)
    blocked_msg = BLOCKED_COMPANIES.get(company.lower())

    postings = None
    url_is_recognized_ats = False

    # Dedicated fetcher for companies we have special handling for.
    known = KNOWN_COMPANIES.get(company.lower())
    if known:
        fetched = known(session)
        if fetched and fetched[0]:
            postings, result.source = fetched

    # A Workday careers URL: query its job-board API rather than scraping the
    # JS-rendered page (which contains no postings).
    if postings is None and url:
        wd = WORKDAY_URL_RE.match(url)
        if wd:
            url_is_recognized_ats = True
            fetched = fetch_workday(session, wd.group("host"), wd.group("site"))
            if fetched and fetched[0]:
                postings, result.source = fetched

    # A hosted ATS board URL: same idea, use the board's JSON API.
    if postings is None and url and not url_is_recognized_ats:
        for pattern, fetcher in ATS_URL_ROUTES:
            m = pattern.match(url)
            if m:
                url_is_recognized_ats = True
                fetched = fetcher(session, m.group(1))
                if fetched and fetched[0]:
                    postings, result.source = fetched
                break

    # No dedicated fetcher and no recognized-ATS URL: try auto-detecting the
    # company's ATS by guessing its slug. We still do this even when a plain
    # (non-ATS) careers URL was given, since that URL is usually the
    # company's own marketing/careers page rather than proof they aren't
    # also on a public ATS board.
    if postings is None and not url_is_recognized_ats:
        for fetcher in ATS_FETCHERS:
            for slug in slugify_candidates(company):
                fetched = fetcher(session, slug)
                if fetched and fetched[0]:
                    postings, result.source = fetched
                    break
            if postings is not None:
                break

    # Scan the given URL as a plain page in two cases:
    #  - We still have no postings at all (whether or not the URL matched a
    #    recognized ATS pattern -- e.g. its API returned zero current
    #    openings). This is the last resort, same as before.
    #  - We DO have postings, but the URL is a separate, non-ATS page (the
    #    company's own careers site) -- scan it too and merge in anything
    #    new, since some companies also list roles only there. If the URL
    #    IS the same ATS we already queried, re-scraping it as text adds
    #    nothing, so skip it.
    custom_page_result = None
    if url and (postings is None or not url_is_recognized_ats):
        custom_page_result = scan_custom_page(session, company, url, patterns)
        if postings is None:
            if custom_page_result.error and blocked_msg:
                custom_page_result.error = blocked_msg
            return custom_page_result

    if postings is None:
        result.error = blocked_msg or (
            "no public job board found (Greenhouse/Lever/Ashby/"
            "SmartRecruiters/Workable) — add a careers URL for this company")
        return result

    intern_postings = [
        p for p in postings
        if p.get("is_intern_tagged") or INTERN_RE.search(p["title"])
    ]
    result.total_intern_roles = len(intern_postings)

    for p in intern_postings:
        posted_date = p.get("posted_date", "")
        if find_match(p["title"], patterns):
            result.matches.append(
                Role(p["title"], p["url"], p["location"], "title",
                     posted_date=posted_date)
            )
            continue
        text = p["text"]
        if not text and check_descriptions and "_detail" in p:
            text = fetch_greenhouse_detail(session, p)
        if text:
            m = find_match(text, patterns)
            if m:
                result.matches.append(
                    Role(p["title"], p["url"], p["location"], "description",
                         snippet_around(text, m), posted_date=posted_date)
                )

    if custom_page_result:
        result.total_intern_roles += custom_page_result.total_intern_roles
        had_real_matches = bool(result.matches)
        existing_urls = {m.url for m in result.matches}
        for m in custom_page_result.matches:
            # A generic "page mentions it" hit (matched_in == "page") has no
            # real title/URL of its own -- it's only useful as a last
            # resort when we have nothing else. When we already have real
            # ATS matches, it's pure noise (often just nav links or cookie
            # banners), so only add it here if we had no matches at all.
            if m.matched_in == "page" and had_real_matches:
                continue
            if m.url not in existing_urls:
                result.matches.append(m)
                existing_urls.add(m.url)

    return result


def scan_custom_page(session: requests.Session, company: str, url: str,
                     patterns: list[re.Pattern]) -> CompanyResult:
    result = CompanyResult(company=company, source=url)
    try:
        resp = session.get(url, timeout=TIMEOUT, headers=HEADERS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        result.error = f"failed to fetch {url}: {exc}"
        return result

    text = strip_html(resp.text)
    intern_hits = len(INTERN_RE.findall(text))
    result.total_intern_roles = intern_hits
    if intern_hits:
        m = find_match(text, patterns)
        if m:
            snippet = snippet_around(text, m, 80)
            result.matches.append(
                Role(f"page mentions it: “…{snippet}…”", url, "", "page")
            )
    return result


# ---------------------------------------------------------------------------
# Input / output
# ---------------------------------------------------------------------------

def load_companies(path: str) -> list[tuple[str, str | None]]:
    """Each line: 'Company Name' or 'Company Name | https://careers.url'."""
    companies = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, url = (part.strip() for part in line.split("|", 1))
                companies.append((name, url or None))
            else:
                companies.append((line, None))
    return companies


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search company career sites for internship roles."
    )
    parser.add_argument("companies_file", help="text file with one company per line")
    parser.add_argument("--term", default="summer 2027",
                        help="keywords to match, any one counts "
                             "(default: summer OR 2027)")
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel lookups (default: 8)")
    parser.add_argument("--no-descriptions", action="store_true",
                        help="only match against job titles (faster)")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="print results as JSON instead of a table")
    parser.add_argument("--heavy-scan", action="store_true",
                        help="also run expensive, many-request scans for "
                             f"companies that need them ({', '.join(HEAVY_SCAN_ONLY_COMPANIES)}) "
                             "-- meant to be run periodically, not on every scan")
    args = parser.parse_args()

    global HEAVY_SCAN_ENABLED
    HEAVY_SCAN_ENABLED = args.heavy_scan

    companies = load_companies(args.companies_file)
    if not companies:
        console.print("[red]No companies found in file.[/red]")
        return 1

    term_desc = term_label(args.term)
    results: list[CompanyResult] = []
    with console.status(f"Scanning {len(companies)} companies for {term_desc}…"):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(scan_company, name, url, args.term,
                            not args.no_descriptions): name
                for name, url in companies
            }
            for future in as_completed(futures):
                results.append(future.result())

    order = {name: i for i, (name, _) in enumerate(companies)}
    results.sort(key=lambda r: order[r.company])

    if args.json_out:
        print(json.dumps([
            {
                "company": r.company,
                "source": r.source,
                "intern_roles_found": r.total_intern_roles,
                "matches": [vars(m) for m in r.matches],
                "error": r.error,
            }
            for r in results
        ], indent=2))
        return 0

    table = Table(title=f"Internship roles matching {term_desc}", show_lines=True)
    table.add_column("Company", style="bold")
    table.add_column("Status")
    table.add_column("Matching roles", overflow="fold")

    for r in results:
        if r.error:
            status = "[yellow]couldn't scan[/yellow]"
            detail = f"[dim]{r.error}[/dim]"
        elif r.matches:
            status = f"[green]{len(r.matches)} match(es)[/green]"
            detail = "\n".join(
                f"• {m.title}"
                + (f" [dim]({m.location})[/dim]" if m.location else "")
                + f"\n  [link={m.url}]{m.url}[/link]"
                + (f"\n  [dim]“…{m.snippet}…”[/dim]" if m.snippet else "")
                for m in r.matches
            )
        elif r.total_intern_roles:
            status = "[red]no match[/red]"
            detail = (f"[dim]{r.total_intern_roles} intern role(s) live on "
                      f"{r.source}, none mention {term_desc}[/dim]")
        else:
            status = "[red]no match[/red]"
            detail = f"[dim]no intern roles found on {r.source}[/dim]"
        table.add_row(r.company, status, detail)

    console.print(table)

    hits = sum(len(r.matches) for r in results)
    console.print(
        f"\n[bold]{hits}[/bold] matching role(s) across "
        f"[bold]{sum(1 for r in results if r.matches)}[/bold] of "
        f"{len(results)} companies."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
