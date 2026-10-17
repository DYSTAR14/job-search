"""Filtering, extraction and scoring rules for scraped LinkedIn posts.

Posts come in as one raw text blob per card (author line, headline, relative
timestamp and post body all mixed together, exactly as LinkedIn renders it).
Two extraction tiers are used, in order:

1. Labeled lines ("Role: Java Developer", "Exp: 4-7 yrs") - common in
   agency/bulk-recruiter posts that are templated and copy-pasted across
   many listings. High confidence.
2. Freeform regex anchored on "hiring" - fallback for narrative-style posts.
   Lower confidence, but better than dropping the post entirely.
"""

import html
import logging
import re
import unicodedata
from collections import Counter

# "s?" throughout: recruiters commonly use plural labels ("Locations:",
# "Roles:") especially when listing multiple options on one line.
LABEL_LINE_PATTERNS = {
    "role": r"(?:roles?|positions?|titles?|designations?)\s*[:\-]\s*(.+)",
    "experience": r"(?:exp(?:erience)?s?(?:\s+required)?)\s*[:\-]\s*(.+)",
    "location": r"(?:locations?|loc)\s*[:\-]\s*(.+)",
    "notice_period": r"notice\s*periods?\s*[:\-]\s*(.+)",
}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Real TLDs that merely START with com/org/net - must not be trimmed below.
_LONG_TLDS_STARTING_COM = {"community", "company", "computer", "commbank", "compare"}


def clean_email(raw):
    """EMAIL_RE over-captures in running text: a sentence-ending dot
    ("hr@firm.com.") or the next word glued on with no space
    ("career@firm.comFeel"). Both produce a wrong address that would be
    copied straight into an application."""
    email = raw.rstrip(".,;:!?)-_")
    local, _, domain = email.rpartition("@")
    labels = domain.split(".")
    last = labels[-1]
    low = last.lower()
    if low not in {"com", "org", "net"} and low not in _LONG_TLDS_STARTING_COM:
        for tld in ("com", "org", "net"):
            if low.startswith(tld) and len(last) > len(tld):
                labels[-1] = last[: len(tld)]
                break
    return f"{local}@{'.'.join(labels)}"


GENERIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in",
    "outlook.com", "hotmail.com", "live.com", "msn.com", "rediffmail.com",
    "icloud.com", "proton.me", "protonmail.com", "aol.com", "zoho.com",
    "ymail.com", "mail.com",
}
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def company_from_email(email):
    """Best-effort company name from a work-email domain
    (hr@tekskills.in -> "Tekskills"). None for free-mail providers."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].lower()
    if domain in GENERIC_MAIL_DOMAINS:
        return None
    labels = domain.split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL:
        name = labels[-3]
    elif len(labels) >= 2:
        name = labels[-2]
    else:
        return None
    return name.replace("-", " ").title() or None

# "Position:"/"Role:" sometimes means employment type, not job title
# (confirmed: a real post used "Position: Full-Time" while the actual title
# was written as markdown emphasis elsewhere, not a labeled line at all).
NON_ROLE_LABEL_VALUES = {
    "full-time", "full time", "part-time", "part time", "contract",
    "permanent", "remote", "onsite", "on-site", "hybrid", "immediate",
    "urgent", "wfo", "wfh",
}

# Anchors on the first "hiring" and takes the rest of that line, rather than
# a fixed allowed-character class - headlines commonly use "|" as a visual
# separator ("We're Hiring | Senior Backend Java Developers"), which a
# restrictive charset would refuse to capture, causing the match to fall
# through to a LATER, unrelated "hiring" mention (e.g. a #Hiring hashtag at
# the bottom of the post) instead of the real headline.
FALLBACK_ROLE_ANCHOR_RE = re.compile(r"\bhiring\b(?:\s+for)?", re.IGNORECASE)
# Some posts have no real line breaks at all (emoji used as the only visual
# separator), so a "rest of line" capture can run straight into the next
# labeled field. Stop early if another label shows up before the newline.
NEXT_LABEL_RE = re.compile(r"\b(?:roles?|locations?|exp(?:erience)?s?|ctc|notice\s*periods?)\s*[:\-]", re.IGNORECASE)
# A leading "." is junk ("...Java Dev") EXCEPT in ".NET" - stripping it turned
# ".Net Developer" into "Net Developer" and defeated the .net exclusion.
LEADING_ROLE_JUNK_RE = re.compile(r"^(?:[\s|:\-\u2013\u2014!*#]|\.(?!net\b))+", re.IGNORECASE)
TRAILING_ROLE_JUNK_RE = re.compile(r"[\s|:\-–—!.,*]+$")

# Separators include en/em dashes ("8–12 Years") - recruiters use them as
# often as a plain hyphen, and without them those ranges were never parsed
# at all (ceiling filter and in-band score bonus silently skipped).
EXP_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|–|—|to|\+)\s*(\d{1,2})?\s*\+?\s*(?:yrs?|years?)", re.IGNORECASE)

COMPANY_SUFFIX_RE = re.compile(
    r"\b(pvt\.?\s*ltd\.?|private\s+limited|llp|inc\.?|llc|limited|ltd\.?|corp\.?|corporation)\b",
    re.IGNORECASE,
)


LEADING_SYMBOLS_RE = re.compile(r"^[^A-Za-z]+")


def extract_labeled_fields(text):
    fields = {}
    for line in text.splitlines():
        # Bulk-recruiter posts almost always prefix labeled lines with an
        # emoji/bullet ("📍 Location: Bangalore", "🔹 Exp: 4-7 yrs") - strip
        # it so the label regexes (anchored at line start) still match.
        line = LEADING_SYMBOLS_RE.sub("", line.strip())
        if not line:
            continue
        for key, pattern in LABEL_LINE_PATTERNS.items():
            if key in fields:
                continue
            m = re.match(pattern, line, re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                if key == "location":
                    # Narrative posts often run a location straight into the
                    # next sentence on the same line ("Remote, India
                    # preferred. 5+ years experience") - cut at the first
                    # sentence boundary so we don't swallow unrelated facts.
                    value = re.split(r"(?<=\S)\.\s", value)[0]
                fields[key] = clean_field_value(value.strip().rstrip(".,"))
    email_match = EMAIL_RE.search(text)
    if email_match:
        fields["email"] = clean_email(email_match.group(0))
    return fields


def extract_fallback_role(text):
    m = FALLBACK_ROLE_ANCHOR_RE.search(text)
    if not m:
        return None
    line_end = text.find("\n", m.end())
    tail = text[m.end() : line_end if line_end != -1 else len(text)]
    next_label = NEXT_LABEL_RE.search(tail)
    if next_label:
        tail = tail[: next_label.start()]
    tail = LEADING_ROLE_JUNK_RE.sub("", tail)
    tail = TRAILING_ROLE_JUNK_RE.sub("", tail)
    tail = tail.strip()
    if _is_hashtag_dump(tail):
        # Walk-in-drive / interview-announcement posts sometimes only use
        # "hiring" inside a hashtag cluster ("#hiring #bangalore
        # #datascientists ..."), not as a real headline - in that case
        # there's no usable role text to extract, so drop it rather than
        # show a hashtag string as the "role".
        return None
    return tail[:80] or None


MISSING_AMP_RE = re.compile(r"(?<=\s)amp;(?=\s)")


def normalize_entities(text):
    """Decode HTML entities (twice, for double-encoded "&amp;amp;") and
    repair "amp;" whose "&" was already lost upstream
    ("Java 17 amp; Java 21" -> "Java 17 & Java 21")."""
    for _ in range(2):
        text = html.unescape(text)
    return MISSING_AMP_RE.sub("&", text)


def clean_role(role):
    """Strip decorations from an extracted role title: leading '#', trailing
    emoji/symbols (alert sirens, rockets), non-breaking spaces."""
    role = role.replace(" ", " ")
    role = LEADING_ROLE_JUNK_RE.sub("", role)
    role = re.sub(r"[^\w)\]+#]+$", "", role)
    return re.sub(r"\s+", " ", role).strip()


def _is_hashtag_dump(text):
    tokens = text.split()
    if not tokens:
        return False
    hash_tokens = sum(1 for t in tokens if t.startswith("#"))
    return hash_tokens >= max(2, len(tokens) // 2)


def parse_experience_years(text_or_none):
    if not text_or_none:
        return None, None
    m = EXP_RANGE_RE.search(text_or_none)
    if not m:
        return None, None
    lo = int(m.group(1))
    # hi is None for open-ended "5+ years" - there is no stated maximum, so
    # callers must not treat it as an upper bound.
    hi = int(m.group(2)) if m.group(2) else None
    return lo, hi


# Matches the action-bar button labels LinkedIn always appends at the very
# end of a post's rendered text ("Like\nComment\nRepost\nSend") - UI chrome,
# not part of the actual post content.
TRAILING_ACTION_BAR_RE = re.compile(
    r"\n+Like\s*\n+Comment\s*\n+Repost\s*\n+Send\s*$", re.IGNORECASE
)
MORE_TOGGLE_RE = re.compile(r"\s*…\s*more\s*", re.IGNORECASE)
BLANK_LINES_RE = re.compile(r"\n{3,}")


ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u2800]")
# The reaction/comment count LinkedIn renders after the post body ("#TCS 4",
# "...@firm.com 36", or a bare "2" on its own last line). All 42 posts in one
# audited scan ended with one of these, plus zero-width spacer lines.
TRAILING_COUNT_RE = re.compile(
    r"(?:(?<=[^\s:\-\u2013\u2014])[ \t]+|\n+[ \t]*)\d{1,5}(?:[ \t]*(?:comments?|reposts?|likes?))*\s*$",
    re.IGNORECASE,
)


def clean_description(text):
    """Strip LinkedIn's UI chrome out of a post's raw body text so it reads
    as an actual job description rather than a DOM dump.
    """
    cleaned = ZERO_WIDTH_RE.sub("", text)
    cleaned = TRAILING_ACTION_BAR_RE.sub("", cleaned.rstrip())
    cleaned = MORE_TOGGLE_RE.sub(" ", cleaned)
    # Reactions and comments render as separate trailing numbers ("4\n1"),
    # so strip repeatedly until nothing more comes off.
    for _ in range(4):
        stripped = TRAILING_COUNT_RE.sub("", cleaned.rstrip())
        if stripped == cleaned.rstrip():
            break
        cleaned = stripped
    cleaned = BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def clean_field_value(value):
    """Tidy a labeled field value: drop markdown bold markers, emoji and
    stray brackets glued onto the ends ("** 6-12 Years", "Bangalore💼",
    "[Muzaffarpur/Jaipur]", "Noida or New Delhi ?")."""
    value = ZERO_WIDTH_RE.sub("", value).replace("*", "")
    value = re.sub(r"^[^\w(\[]+|[^\w)\]+#.]+$", "", value.strip())
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return re.sub(r"\s+", " ", value).strip()


PHONE_RE = re.compile(r"(?<![\d])(?:\+?91[\s-]?)?([6-9]\d{4})[\s-]?(\d{5})(?!\d)")
CTC_LABEL_RE = re.compile(
    r"^(?:ctc|salary|compensation|package|budget|pay(?:\s*range)?|rate)(?:\s*range)?\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
CTC_INLINE_RE = re.compile(
    r"(?:\u20b9|rs\.?|inr)\s?[\d.,]+\s?(?:-|\u2013|to)?\s?[\d.,]*\s?(?:lpa|lakhs?|k|per month|/month)"
    r"|\b\d{1,3}(?:\.\d)?\s?(?:-|\u2013|to)\s?\d{1,3}(?:\.\d)?\s?lpa\b",
    re.IGNORECASE,
)
APPLY_INSTRUCTION_RE = re.compile(
    r"(?:send|share|drop|forward|email|e-mail|mail)\b.*\b(?:resume|cv|profile)"
    r"|\b(?:dm|comment|whatsapp|connect with me)\b"
    r"|interested candidates?"
    r"|apply\s+(?:now|here|via|at|through|on|using)",
    re.IGNORECASE,
)
WORK_MODE_TERMS = [
    ("Remote", r"\bremote\b|\bwfh\b|work from home"),
    ("Hybrid", r"\bhybrid\b"),
    ("Onsite", r"\bon-?site\b|\bwfo\b|work from office"),
]
JOB_TYPE_TERMS = [
    ("Contract-to-hire", r"contract[\s-]*to[\s-]*hire|\bc2h\b"),
    ("Contract", r"\bcontract\b|\bc2c\b"),
    ("Full-time", r"\bfull[\s-]*time\b|\bpermanent\b|\bfte\b"),
    ("Freelance", r"\bfreelanc"),
    ("Part-time", r"\bpart[\s-]*time\b"),
]


def extract_phones(text):
    """Indian mobile numbers in the post (apply-by-call/WhatsApp), deduped."""
    found = []
    for m in PHONE_RE.finditer(text):
        number = m.group(1) + m.group(2)
        if number not in found:
            found.append(number)
    return found[:3]


def extract_apply_instruction(text):
    """The post's own sentence on how to apply (e.g. "Share your resume at
    ..." / "DM me your CV"), so the report can show it without reading the
    whole description."""
    for line in text.splitlines():
        line = LEADING_SYMBOLS_RE.sub("", ZERO_WIDTH_RE.sub("", line)).strip()
        if 12 <= len(line) <= 400 and APPLY_INSTRUCTION_RE.search(line) and not line.startswith("#"):
            return line[:220]
    return ""


def extract_details(text, fields):
    """Decision-making basics that live in the post body: pay, notice
    period, work mode and employment type."""
    ctc = ""
    for line in text.splitlines():
        m = CTC_LABEL_RE.match(LEADING_SYMBOLS_RE.sub("", line.strip()))
        if m:
            ctc = clean_field_value(m.group(1))[:120]
            break
    if not ctc:
        m = CTC_INLINE_RE.search(text)
        ctc = m.group(0).strip() if m else ""
    lower = text.lower()
    return {
        "ctc": ctc,
        "notice_period": fields.get("notice_period", ""),
        "work_mode": " / ".join(name for name, pat in WORK_MODE_TERMS if re.search(pat, lower)),
        "job_type": " / ".join(name for name, pat in JOB_TYPE_TERMS if re.search(pat, lower)),
    }


def normalize_company(name):
    if not name:
        return ""
    n = name.lower()
    n = COMPANY_SUFFIX_RE.sub("", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n.strip()


# Canonical city -> text patterns that indicate it, checked against the
# Indian states/UTs -> city/text patterns that indicate them, checked
# against the full post (location field often has multiple cities jammed
# into one freeform string like "Pune | Hyderabad | Bhubaneswar | Bangalore",
# so this returns a list, not a single value - a post can belong to several
# regions). Delhi NCR is kept as its own pseudo-region rather than split
# across Delhi/Haryana/UP, since that's how job posts actually phrase it;
# Haryana/UP keep their own entries for cities clearly outside the NCR belt.
STATE_PATTERNS = {
    "Karnataka": ["bangalore", "bengaluru", "banglore", "bengalore", "bangaluru", "blr", "mysore", "mysuru", "mangalore", "mangaluru", "hubli", "belgaum", "whitefield", "electronic city", "marathahalli", "koramangala"],
    "Telangana": ["hyderabad", "hyd", "secunderabad", "warangal", "gachibowli", "hitech city"],
    "Maharashtra": ["mumbai", "pune", "nagpur", "nashik", "thane", "navi mumbai", "aurangabad", "andheri", "bkc"],
    "Tamil Nadu": ["chennai", "coimbatore", "madurai", "trichy", "tiruchirapalli", "salem"],
    "Delhi NCR": ["delhi", "new delhi", "ncr", "gurgaon", "gurugram", "noida", "faridabad", "ghaziabad"],
    "West Bengal": ["kolkata", "howrah", "durgapur", "siliguri"],
    "Gujarat": ["ahmedabad", "surat", "vadodara", "gandhinagar", "rajkot"],
    "Rajasthan": ["jaipur", "jodhpur", "udaipur", "kota"],
    "Kerala": ["kochi", "cochin", "thiruvananthapuram", "trivandrum", "kozhikode", "calicut", "ernakulam"],
    "Madhya Pradesh": ["indore", "bhopal", "gwalior", "jabalpur"],
    "Punjab": ["ludhiana", "amritsar", "mohali", "jalandhar"],
    "Chandigarh": ["chandigarh"],
    "Odisha": ["bhubaneswar", "cuttack", "rourkela"],
    "Uttar Pradesh": ["lucknow", "kanpur", "varanasi", "agra", "prayagraj"],
    "Haryana": ["panchkula", "ambala", "hisar", "karnal"],
    "Bihar": ["patna", "gaya"],
    "Jharkhand": ["ranchi", "jamshedpur", "dhanbad"],
    "Chhattisgarh": ["raipur", "bhilai"],
    "Assam": ["guwahati", "dibrugarh"],
    "Uttarakhand": ["dehradun", "haridwar"],
    "Himachal Pradesh": ["shimla", "manali"],
    "Goa": ["goa", "panaji", "margao"],
    "Jammu and Kashmir": ["srinagar", "jammu"],
    "Andhra Pradesh": ["visakhapatnam", "vizag", "vijayawada", "guntur", "tirupati"],
    "Puducherry": ["puducherry", "pondicherry"],
    "Tripura": ["agartala"],
    "Manipur": ["imphal"],
    "Meghalaya": ["shillong"],
    "Nagaland": ["kohima"],
    "Mizoram": ["aizawl"],
    "Sikkim": ["gangtok"],
    "Arunachal Pradesh": ["itanagar"],
    "Ladakh": ["leh"],
    "Andaman and Nicobar Islands": ["port blair"],
}

# Countries outside India - a post mentioning one of these is dropped UNLESS
# it's explicitly WFH/remote-from-India (see is_foreign_post_wfh below).
FOREIGN_COUNTRY_SIGNALS = [
    "usa only", "us citizens", "united states", "canada", "canadian",
    "pakistan", "islamabad", "karachi", "lahore", "bangladesh", "dhaka",
    "nepal", "kathmandu", "sri lanka", "colombo", "singapore", "dubai",
    "uae", "saudi", "qatar", "kuwait", "philippines", "manila",
    "united kingdom", "london", "germany", "berlin", "australia", "sydney",
    "melbourne",
]

# Common fee/deposit scam phrasing in recruitment posts - a post asking the
# candidate to pay anything is dropped outright regardless of how good the
# role/skills match looks.
SCAM_KEYWORDS = [
    "charges applicable", "registration fee", "registration charges",
    "processing fee", "security deposit", "refundable deposit",
    "refundable fee", "pay to apply", "token amount", "advance payment",
    "small fee", "nominal fee", "one-time fee",
]

# Deliberately NOT including bare "remote" here - that word alone is what
# originally let Canada/Pakistan/Singapore posts slip through ("remote
# within Canada" also says "remote"). A foreign post only counts as WFH if
# it has an explicit WFH phrase, or says "remote" together with "india".
WFH_SIGNALS = ["wfh", "work from home", "work from anywhere"]


def is_foreign_post_wfh(text_lower):
    if any(s in text_lower for s in WFH_SIGNALS):
        return True
    return "remote" in text_lower and "india" in text_lower


def build_loose_terms_regex(terms):
    """Like build_terms_regex but for terms that start/end with symbols
    (".net", "c#") where \b cannot anchor - uses non-word lookarounds."""
    if not terms:
        return None
    pattern = r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)"
    return re.compile(pattern, re.IGNORECASE)


def build_terms_regex(terms):
    """Word-boundary regex for a list of phrases, instead of plain substring
    matching - needed for short/ambiguous entries like "uk", which as a bare
    substring would false-positive on common Indian names (Mukesh,
    Mukherjee, ...) that are routine in this tool's actual content.
    """
    if not terms:
        return None
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


# Fixed display order for the .md summary's per-region sections; anything
# not listed here falls back to alphabetical. Catch-alls always go last.
STATE_ORDER = [
    "Karnataka", "Maharashtra", "Telangana", "Tamil Nadu", "Delhi NCR",
    "WFH (Overseas Client)", "Pan-India", "Remote", "Other",
]


# Word-boundary regex per state, built once - plain substring matching
# previously let short city patterns match inside unrelated words (e.g.
# Ladakh's "leh" inside "rippleHIRE.com", "goa" in "goal", "patna" in
# "visakhaPATNAm", "kota" inside "Kotak Mahindra Bank").
STATE_REGEXES = {state: build_terms_regex(patterns) for state, patterns in STATE_PATTERNS.items()}

# Hashtag fallback ("#bangalorejobs", "#punehiring"): used ONLY when no
# real location text matched, so a post with "Location: Pune" doesn't also
# pick up every city in its SEO hashtag cloud, while a hashtag-only post
# still gets located.
STATE_HASHTAG_REGEXES = {
    state: re.compile(r"#(?:" + "|".join(re.escape(p.replace(" ", "")) for p in patterns) + r")\w*", re.IGNORECASE)
    for state, patterns in STATE_PATTERNS.items()
}


def strip_diacritics(text):
    """"Hyderābād" -> "Hyderabad" so accented spellings still match."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def extract_locations(location, text_lower, is_wfh_overseas):
    if is_wfh_overseas:
        return ["WFH (Overseas Client)"]
    haystack = strip_diacritics(f"{location} {text_lower}")
    found = [state for state, rx in STATE_REGEXES.items() if rx.search(haystack)]
    if not found:
        found = [state for state, rx in STATE_HASHTAG_REGEXES.items() if rx.search(haystack)]
    if found:
        return found
    if "pan india" in haystack or "pan-india" in haystack:
        return ["Pan-India"]
    if "remote" in haystack or "india" in haystack:
        return ["Remote"]
    return ["Other"]


def dedupe_signature(post):
    """A cross-run identity for 'the same job opening', as opposed to 'the
    same scraped post' (which is what the urn/content-hash tracks). Used to
    stop the same opening from resurfacing day after day just because a
    different person reposted it with slightly different wording - see
    scan.py's reported.json handling.
    """
    email = (post.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    return f"role:{normalize_company(post.get('company') or '')}:{(post.get('role') or '').strip().lower()[:40]}"


def score_post(text_lower, fields, config):
    score = 0
    for kw in config.get("include_keywords", []):
        if kw.lower() in text_lower:
            score += 1
    score += len(fields)
    if fields.get("email"):
        score += 1
    exp_cfg = config.get("experience", {})
    lo, _hi = parse_experience_years(fields.get("experience") or text_lower)
    min_y, max_y = exp_cfg.get("min_years"), exp_cfg.get("max_years")
    if lo is not None and min_y is not None and max_y is not None and min_y <= lo <= max_y:
        score += 2
    return score


def merge_duplicates(posts):
    """Merge posts that are the same job reposted by different people.

    Prefers the contact email as the merge key when present - confirmed
    against real data to be a stronger same-job signal than company name:
    only the official company-page repost tends to tag a /company/ link,
    employee reposts of the same opening usually don't, and headline
    wording often varies slightly between reposts ("Senior-Level" vs
    "Senior Backend Java Developers" for the literal same AmbiDer opening).
    Falls back to (company, role) when no email was extracted.
    """
    groups = {}
    order = []
    for p in posts:
        key = dedupe_signature(p)
        if key not in groups:
            groups[key] = dict(p)
            groups[key]["_urls"] = [p.get("post_url")]
            order.append(key)
        else:
            groups[key]["_urls"].append(p.get("post_url"))
            if not groups[key].get("company") and p.get("company"):
                groups[key]["company"] = p["company"]
            for loc in p.get("locations", []):
                if loc not in groups[key].get("locations", []):
                    groups[key].setdefault("locations", []).append(loc)

    merged = []
    for key in order:
        p = groups[key]
        urls = p.pop("_urls")
        if len(urls) > 1:
            p["duplicate_count"] = len(urls)
        merged.append(p)
    return merged


def process_posts(raw_posts, config):
    """Filter, extract structured fields from, and score a list of raw
    scraped posts (each a dict with at least 'text', 'company', 'post_url',
    'urn', 'age'). Returns the surviving posts, deduped by company+role.
    """
    exclude_keywords = config.get("exclude_keywords", [])
    exclude_role_prefixes = config.get("exclude_role_prefixes", [])
    exp_cfg = config.get("experience", {})
    foreign_signals = config.get("foreign_country_signals", FOREIGN_COUNTRY_SIGNALS)
    foreign_re = build_terms_regex(foreign_signals)
    weak_foreign_re = build_terms_regex(config.get("foreign_weak_signals", []))
    scam_signals = config.get("scam_keywords", SCAM_KEYWORDS)
    scam_re = build_terms_regex(scam_signals)
    role_excl_re = build_loose_terms_regex(config.get("exclude_role_keywords", []))
    role_keep_re = build_loose_terms_regex(config.get("role_keep_keywords", []))

    drop_reasons = Counter()
    results = []
    for post in raw_posts:
        # Posts routinely carry literal "&amp;" etc. (238 of 1,287 stored
        # rows did) - decode before any extraction so roles/descriptions read
        # correctly.
        text = normalize_entities(post.get("text", "") or "")
        text_lower = text.lower()

        if any(kw.lower() in text_lower for kw in exclude_keywords):
            drop_reasons["exclude_keyword"] += 1
            continue

        if scam_re and scam_re.search(text_lower):
            drop_reasons["scam_pattern"] += 1
            continue

        fields = extract_labeled_fields(text)
        labeled_role = fields.get("role")
        if labeled_role and labeled_role.strip().lower() in NON_ROLE_LABEL_VALUES:
            # "Position: Full-Time" means employment type, not job title -
            # some posts use "Position"/"Role" for that instead of the
            # title, which lives elsewhere (often markdown-style emphasis,
            # not a labeled line). Fall through to the freeform extractor.
            labeled_role = None
        role = clean_role((labeled_role or extract_fallback_role(text) or "").strip())
        if not role:
            drop_reasons["no_role_found"] += 1
            continue

        # Role-title stack filter: exclude_role_prefixes only sees the START
        # of the title, so "Sr. Backend .NET Developer" slipped through.
        # A role naming an off-stack tech is dropped unless it ALSO names
        # something on-stack ("Java + Angular" stays).
        if role_excl_re and role_excl_re.search(role) and not (role_keep_re and role_keep_re.search(role)):
            drop_reasons["stack_mismatch"] += 1
            continue

        if any(role.lower().startswith(p.lower()) for p in exclude_role_prefixes):
            drop_reasons["excluded_role_prefix"] += 1
            continue

        exp_lo, exp_hi = parse_experience_years(fields.get("experience") or text)
        max_years = exp_cfg.get("max_years")
        min_years = exp_cfg.get("min_years")
        if exp_lo is not None and max_years is not None and exp_lo > max_years:
            drop_reasons["experience_too_high"] += 1
            continue
        # Floor check uses the band's upper end, not its lower end: a "2-5
        # yrs" post still overlaps an acceptable band and should survive,
        # but a "0-2 yrs" (fresher) post should not, even though its lower
        # bound (0) is below max_years and wouldn't trip the ceiling check.
        # Conservative: a post that lists several roles/bands ("Junior 0-2
        # yrs ... Senior 5-8 yrs") is only dropped if EVERY stated band is
        # below the floor, so a mixed junior+senior post survives.
        if min_years is not None and exp_hi is not None:
            bands = [(int(a), int(b) if b else None)
                     for a, b in EXP_RANGE_RE.findall(fields.get("experience") or text)]
            if bands and all(hi is not None and hi < min_years for _lo, hi in bands):
                drop_reasons["experience_too_low"] += 1
                continue

        location = fields.get("location", "")
        loc_haystack = f"{location} {text_lower}"
        is_foreign = bool(foreign_re and foreign_re.search(loc_haystack))
        if not is_foreign and weak_foreign_re and weak_foreign_re.search(loc_haystack):
            # US city/state names double as company names on real India
            # posts (Texas Instruments, Boston Consulting Group) - only
            # foreign if there's no Indian city/state to anchor the post.
            plain = strip_diacritics(loc_haystack)
            is_foreign = not any(rx.search(plain) for rx in STATE_REGEXES.values())
        is_wfh_overseas = is_foreign and is_foreign_post_wfh(loc_haystack)
        if is_foreign and not is_wfh_overseas:
            drop_reasons["foreign_not_wfh"] += 1
            continue

        locations = extract_locations(location, text_lower, is_wfh_overseas)
        if locations == ["Other"]:
            drop_reasons["no_location_signal"] += 1
            continue

        enriched = dict(post)
        enriched["role"] = role
        company = post.get("company") or fields.get("company")
        if not company:
            company = company_from_email(fields.get("email"))
            if company:
                enriched["company_inferred"] = True
        enriched["company"] = company
        enriched["location"] = location
        enriched["locations"] = locations
        experience = fields.get("experience", "")
        if not experience:
            # Narrative posts ("4+ years of hands-on experience...", a
            # "(3+ Years)" in the title) have no "Exp:" line - take the
            # stated years from the text so the report isn't blank.
            exp_m = EXP_RANGE_RE.search(text)
            if exp_m:
                experience = clean_field_value(exp_m.group(0))
        enriched["experience"] = experience
        enriched["email"] = fields.get("email", "")
        enriched["phones"] = extract_phones(text)
        enriched["apply_instruction"] = extract_apply_instruction(text)
        enriched.update(extract_details(text, fields))
        enriched["description"] = clean_description(text)
        enriched["score"] = score_post(text_lower, fields, config)
        results.append(enriched)

    merged = merge_duplicates(results)
    if raw_posts:
        logging.info(
            "classify: %d raw -> %d kept (%d merged into existing) -> %d after dedup. Dropped: %s",
            len(raw_posts), len(results), len(results) - len(merged), len(merged),
            dict(drop_reasons),
        )
    return merged
