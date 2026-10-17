"""Apply-link extraction, safe resolution and classification for post bodies.

Many posts have no email and apply through a link instead - usually an opaque
lnkd.in shortener. This module pulls those links out of the post text,
resolves shorteners to their real destination, and classifies where they go
(application form, company ATS, LinkedIn job, job board, community/spam...)
so the report can say *what kind of apply path this is* instead of making you
open every link.

Safety: only known URL-shortener hosts are ever contacted (lnkd.in, bit.ly,
...), logged-out and without redirect-following. The final destination site
(which could be anything a poster typed) is never connected to - it's read
out of the shortener's own redirect/interstitial response.
"""

import html
import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

CACHE_PATH = Path(__file__).parent / "link_cache.json"

SHORTENER_HOSTS = {
    "lnkd.in", "bit.ly", "buff.ly", "tinyurl.com", "shorturl.at", "smrtr.io",
    "t.co", "ow.ly", "rb.gy", "cutt.ly", "is.gd",
}

URL_RE = re.compile(
    r"https?://[^\s<>\"'\]]+"
    r"|\bwww\.[^\s<>\"'\]]+"
    r"|\b(?:lnkd\.in|bit\.ly|buff\.ly|forms\.gle|tinyurl\.com|shorturl\.at|smrtr\.io)/[^\s<>\"'\]]+",
    re.IGNORECASE,
)
TRAILING_URL_JUNK = ").,;:!?*>'\"​"
# LinkedIn autolinks tech names that look like domains ("React.js",
# "ASP.NET"). Only these exact names are rejected - real hosts such as
# careers.taleo.net must still pass.
NOT_A_DOMAIN_RE = re.compile(
    r"^(?:[\w-]+\.(?:js|py|ts)|asp\.net|vb\.net|ado\.net|node\.js|dot\.net)$", re.IGNORECASE
)

APPLY_LABEL_RE = re.compile(
    r"\b(apply|application|register|registration|submit|jd|job description|job link)\b",
    re.IGNORECASE,
)
COMMUNITY_LABEL_RE = re.compile(
    r"\b(follow|join|channel|whatsapp|telegram|subscribe|newsletter|stay updated|"
    r"community|group|daily jobs|job alerts?|more\b.{0,25}\bjobs)\b",
    re.IGNORECASE,
)

FORM_HOSTS = (
    "forms.gle", "docs.google.com/forms", "forms.office.com", "typeform.com",
    "jotform.com", "tally.so", "airtable.com", "forms.zohopublic", "zfrmz.",
    "surveymonkey.com", "forms.microsoft.com",
)
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "ashbyhq.com", "icims.com", "taleo.net",
    "successfactors", "bamboohr.com", "recruitee.com", "jobvite.com",
    "keka.com", "zohorecruit.com", "freshteam.com", "darwinbox.in",
    "darwinbox.com", "pinpointhq.com", "workable.com", "breezy.hr",
    "oraclecloud.com", "peoplestrong.com",
)
JOBBOARD_HOSTS = (
    "naukri.com", "indeed.com", "foundit.in", "monsterindia.com",
    "instahyre.com", "cutshort.io", "wellfound.com", "hirist.tech",
    "hirist.com", "shine.com", "timesjobs.com", "apna.co", "glassdoor.",
    "abekus.ai",
)
COMMUNITY_HOSTS = (
    "t.me", "telegram.me", "chat.whatsapp.com", "whatsapp.com", "wa.me",
    "discord.gg", "discord.com", "instagram.com", "youtube.com", "youtu.be",
    "facebook.com", "twitter.com", "x.com", "linktr.ee",
)
# Lead-gen / job-aggregator sites that repost listings and funnel to
# sign-ups - seen repeatedly in this tool's scraped data. Not a direct apply.
AGGREGATOR_HOSTS = (
    "jobsrmine.com", "prepintro.com", "hireeing.com", "reffido.com",
)
CAREERS_HINT_RE = re.compile(r"(career|jobs?|apply|opening|vacanc|join-?us|recruit|hiring)", re.IGNORECASE)

# kind -> (base priority as an actual application path, human description)
KINDS = {
    "linkedin_job": (100, "LinkedIn job listing"),
    "ats": (90, "Company application system (ATS)"),
    "form": (85, "Application form"),
    "careers": (70, "Company careers/apply page"),
    "jobboard": (60, "Job-board listing"),
    "other": (40, "Other website"),
    "unresolved": (30, "Unresolved short link"),
    "linkedin_other": (0, "Another LinkedIn page (not a direct apply)"),
    "community": (0, "Community/social channel (not an application)"),
    "aggregator": (0, "Job-aggregator/lead-gen site (not a direct apply)"),
}
DIRECT_KINDS = {"linkedin_job", "ats", "form", "careers", "jobboard"}
NO_APPLY_KINDS = {"linkedin_other", "community", "aggregator"}
# Links that actively signal "this post isn't a real application path".
# linkedin_other (e.g. the poster's own company page) is excluded: it's
# merely not an apply link, not evidence the post is a dead end.
DEAD_END_KINDS = {"community", "aggregator"}

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_A_TAG_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


def _host(url):
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _label_for(text, start):
    """Words immediately before the URL on its line (e.g. "Apply Here:"),
    falling back to a short preceding line when the URL sits alone."""
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    label = re.sub(r"[^A-Za-z0-9 ]+", " ", prefix).strip()
    if not label and line_start > 0:
        prev_end = line_start - 1
        prev_start = text.rfind("\n", 0, prev_end) + 1
        prev_raw = text[prev_start:prev_end]
        prev = re.sub(r"[^A-Za-z0-9 ]+", " ", prev_raw).strip()
        if 0 < len(prev) <= 80 and not URL_RE.search(prev_raw):
            label = prev
    return re.sub(r"\s+", " ", label)[-60:]


def extract_links(text):
    """All distinct URLs in a post body with the label text next to each."""
    found, seen = [], set()
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(TRAILING_URL_JUNK)
        if not url.lower().startswith("http"):
            url = "https://" + url
        host = _host(url)
        if "." not in host or NOT_A_DOMAIN_RE.search(host):
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append({"url": url, "label": _label_for(text, m.start())})
    return found


def normalize_url(url):
    """Drop tracking params/fragment so the same destination compares equal."""
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "trk", "trkinfo", "usp", "lipi", "igshid"}]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", urlencode(q), ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _fetch_once(url):
    """One hop of a shortener: Location header for 3xx, or (for lnkd.in) the
    external-link interstitial's destination. None if not determinable."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    try:
        resp = _opener.open(req, timeout=8)
        body = resp.read(120_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") if 300 <= e.code < 400 else None
        return loc
    for tag in _A_TAG_RE.findall(body):
        if "external_url_click" in tag:
            href = _HREF_RE.search(tag)
            if href:
                return html.unescape(href.group(1))
    return None


def resolve_short_url(url, max_hops=3):
    """Follow shortener hops (never contacting non-shortener hosts). Returns
    the destination URL, or None if it couldn't be determined."""
    current = url
    for _ in range(max_hops):
        if _host(current) not in SHORTENER_HOSTS:
            return current
        nxt = _fetch_once(current)
        if not nxt:
            return None
        current = nxt
    return current if _host(current) not in SHORTENER_HOSTS else None


def classify_link(url, label="", resolved=True):
    host = _host(url)
    full = (host + urlparse(url).path).lower()
    if not resolved:
        kind = "unresolved"
    elif any(h in full for h in FORM_HOSTS):
        kind = "form"
    elif "linkedin.com" in host:
        path = urlparse(url).path.lower()
        if path.startswith("/jobs/view") or "currentjobid=" in url.lower():
            kind = "linkedin_job"
        elif path.startswith("/groups"):
            kind = "community"
        else:
            kind = "linkedin_other"
    elif any(h in host for h in AGGREGATOR_HOSTS):
        kind = "aggregator"
    elif any(host == h or host.endswith("." + h) for h in COMMUNITY_HOSTS):
        kind = "community"
    elif any(h in host for h in ATS_HOSTS):
        kind = "ats"
    elif any(h in host for h in JOBBOARD_HOSTS):
        kind = "jobboard"
    elif CAREERS_HINT_RE.search(full):
        kind = "careers"
    else:
        kind = "other"
    # A "Follow"/"Join our channel" label overrides an ambiguous destination,
    # but never overrides a destination that is clearly an application path.
    if kind in {"other", "unresolved", "careers"} and COMMUNITY_LABEL_RE.search(label or "") \
            and not APPLY_LABEL_RE.search(label or ""):
        kind = "community"
    return kind


def _load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


MAX_CONSECUTIVE_FAILURES = 5


def analyze_post(post, cache, budget, failures):
    """Fill in link analysis fields on one post. `budget` is a one-item list
    holding the remaining number of network resolutions for this run."""
    out = []
    for link in extract_links(post.get("description") or post.get("text") or ""):
        url, label = link["url"], link["label"]
        final, resolved = url, True
        if _host(url) in SHORTENER_HOSTS:
            entry = cache.get(url)
            if entry and entry.get("final"):
                final = entry["final"]
            elif budget[0] > 0 and failures[0] < MAX_CONSECUTIVE_FAILURES:
                budget[0] -= 1
                time.sleep(random.uniform(0.6, 1.5))
                try:
                    dest = resolve_short_url(url)
                except Exception as e:  # network errors must never break a scan
                    logging.info("link resolve failed for %s: %s", url, e)
                    dest = None
                if dest:
                    cache[url] = {"final": dest, "at": datetime.now().isoformat()}
                    final = dest
                    failures[0] = 0
                else:
                    resolved = False
                    failures[0] += 1
                    if failures[0] == MAX_CONSECUTIVE_FAILURES:
                        logging.info("links: %d consecutive resolution failures - stopping lookups this run", failures[0])
            else:
                resolved = False
        kind = classify_link(final, label, resolved)
        out.append({
            "url": url, "final": normalize_url(final) if resolved else url,
            "label": label, "kind": kind,
            "apply_label": bool(APPLY_LABEL_RE.search(label)),
        })

    best, best_rank = None, 0
    for l in out:
        if l["kind"] in NO_APPLY_KINDS:
            continue
        rank = KINDS[l["kind"]][0] + (10 if l["apply_label"] else 0)
        if rank > best_rank:
            best, best_rank = l, rank
    post["links"] = out
    post["apply_url"] = best["final"] if best else ""
    post["apply_kind"] = best["kind"] if best else ""
    post["apply_short_url"] = best["url"] if best and best["final"] != best["url"] else ""
    return post


def enrich_posts(posts, config):
    """Analyze links on every surviving post, then adjust score for apply
    path quality and merge posts that share the same destination URL."""
    cache = _load_cache()
    budget = [int(config.get("max_link_resolutions_per_run", 60))]
    resolve = config.get("resolve_links", True)
    if not resolve:
        budget[0] = 0
    start_budget = budget[0]
    failures = [0]

    for p in posts:
        analyze_post(p, cache, budget, failures)
        links = p["links"]
        if not p.get("email"):
            if p["apply_kind"] in DIRECT_KINDS:
                p["score"] = p.get("score", 0) + 1
            elif links and all(l["kind"] in DEAD_END_KINDS for l in links):
                # Only community/aggregator links and no email: nothing here
                # can actually be applied through.
                p["score"] = max(0, p.get("score", 0) - 3)
                p["no_real_apply_path"] = True
    _save_cache(cache)

    kinds = {}
    for p in posts:
        for l in p["links"]:
            kinds[l["kind"]] = kinds.get(l["kind"], 0) + 1
    logging.info(
        "links: %d posts, %d with links, %d network resolutions used (%d allowed). Link kinds: %s",
        len(posts), sum(1 for p in posts if p["links"]), start_budget - budget[0], start_budget, kinds,
    )
    return merge_by_apply_url(posts)


def _role_key(post):
    return re.sub(r"[^a-z0-9]+", "", (post.get("role") or "").lower())[:25]


def merge_by_apply_url(posts):
    """Posts with the same apply link AND the same role are one opening
    reposted by different people - keep one (highest score). Role must match
    too: a company's generic careers page / one shared form is routinely
    linked from posts about completely different roles."""
    best = {}
    out = []
    for p in sorted(posts, key=lambda x: -x.get("score", 0)):
        if p.get("apply_kind") in DIRECT_KINDS and p.get("apply_url"):
            key = (p["apply_url"], _role_key(p))
            keep = best.get(key)
            if keep is not None:
                keep["duplicate_count"] = keep.get("duplicate_count", 1) + p.get("duplicate_count", 1)
                for loc in p.get("locations", []):
                    if loc not in keep.get("locations", []):
                        keep.setdefault("locations", []).append(loc)
                if not keep.get("email") and p.get("email"):
                    keep["email"] = p["email"]
                continue
            best[key] = p
        out.append(p)
    return out
