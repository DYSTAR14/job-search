"""Entry point: python scan.py [--window 24] [--login]

Runs every query in config.yaml against LinkedIn post/content search,
filters and scores the results with classify.py, skips anything already
reported in seen.json, and writes a timestamped CSV + Markdown summary to
output/.
"""

import argparse
import csv
import json
import logging
from datetime import datetime
from pathlib import Path

import yaml

import classify
import linkedin
import links

BASE_DIR = Path(__file__).parent
SEEN_PATH = BASE_DIR / "seen.json"
REPORTED_PATH = BASE_DIR / "reported.json"
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"


def setup_logging():
    """All logging.info() calls anywhere in this process - including from
    linkedin.py - go through this config once it's set up, so the whole
    run's progress ends up in one file as well as the console.
    """
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = LOG_DIR / f"scan_{stamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return log_path

CSV_COLUMNS = [
    "source", "company", "role", "location", "state_region", "experience",
    "email", "apply_url", "apply_type", "other_links", "how_to_apply", "age",
    "post_url", "score", "description",
]


def load_config():
    with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen():
    if SEEN_PATH.exists():
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)


def load_reported():
    if REPORTED_PATH.exists():
        with open(REPORTED_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_reported(reported):
    with open(REPORTED_PATH, "w", encoding="utf-8") as f:
        json.dump(reported, f, indent=2)


def drop_recently_reported(posts, reported, window_days):
    """seen.json stops us re-scraping the same exact post; this stops the
    same JOB OPENING resurfacing in the output day after day just because a
    different person reposted it (different content hash, same opening -
    e.g. the AmbiDer/Aditi Rathi/Bhanu Pundir case from 2026-10-02's run).
    """
    now = datetime.now()
    fresh = []
    for p in posts:
        sig = classify.dedupe_signature(p)
        last_seen = reported.get(sig)
        if last_seen:
            days_since = (now - datetime.fromisoformat(last_seen)).days
            if days_since < window_days:
                continue
        reported[sig] = now.isoformat()
        fresh.append(p)
    return fresh


def age_to_str(age):
    if age is None:
        return ""
    total_minutes = int(age.total_seconds() // 60)
    if total_minutes < 60:
        return f"{total_minutes}m"
    total_hours = total_minutes // 60
    if total_hours < 24:
        return f"{total_hours}h"
    return f"{total_hours // 24}d"


def how_to_apply(post):
    parts = []
    if post.get("email"):
        parts.append(f"email:{post['email']}")
    if post.get("apply_url"):
        parts.append(f"link:{post['apply_url']}")
    return " | ".join(parts) or "see post"


def other_links_str(post):
    """Every non-apply link in the post as 'kind: url', for the CSV."""
    return " | ".join(
        f"{l['kind']}: {l['final']}" for l in post.get("links", [])
        if l["final"] != post.get("apply_url")
    )


def md_apply_line(post):
    """Richer apply instructions for the .md (the CSV's how_to_apply stays
    plain for spreadsheet use - this is the one meant to be read by a human
    deciding how to apply)."""
    parts = []
    if post.get("email"):
        parts.append(f"📧 {post['email']}")
    if post.get("apply_url"):
        desc = links.KINDS[post["apply_kind"]][1]
        parts.append(f"🔗 {desc}: {post['apply_url']}")
    if parts:
        return "  ·  ".join(parts)
    if post.get("no_real_apply_path"):
        return ("⚠️ No email and no real application link — the only links are "
                "community/aggregator pages (see 'Other links in post' below). DM the poster or comment on the post.")
    if post.get("post_url"):
        return "No email in post — DM the poster or comment on the post link below."
    return "No email or post link captured — see raw post text below."


def md_other_links(post):
    """Non-apply links in the post, tagged by what they are."""
    icons = {"community": "💬", "aggregator": "🗑️", "linkedin_other": "in", "unresolved": "❓"}
    rows = []
    for l in post.get("links", []):
        if l["final"] == post.get("apply_url"):
            continue
        rows.append(f"{icons.get(l['kind'], '🌐')} {links.KINDS[l['kind']][1]}: {l['final']}")
    return rows


def write_outputs(posts, warnings, window_hours):
    now = datetime.now()
    # One folder per day (DD-MM-YYYY) so runs are easy to browse by date;
    # filenames only need the time since the folder already carries the date.
    day_dir = OUTPUT_DIR / now.strftime("%d-%m-%Y")
    day_dir.mkdir(parents=True, exist_ok=True)
    # 12-hour clock with AM/PM (no colon - stays filename-safe on Windows)
    # so filenames read at a glance, e.g. scan_02-29PM.csv.
    stamp = now.strftime("%I-%M%p")
    csv_path = day_dir / f"scan_{stamp}.csv"
    md_path = day_dir / f"scan_{stamp}.md"

    ranked = sorted(posts, key=lambda x: -x.get("score", 0))

    # utf-8-sig (BOM) so Excel on Windows renders emoji/₹/etc correctly
    # instead of showing mojibake - plain utf-8 is routinely misread by Excel.
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for p in ranked:
            writer.writerow({
                "source": "linkedin",
                "company": p.get("company") or "",
                "role": p.get("role", ""),
                "location": p.get("location", ""),
                "state_region": "; ".join(p.get("locations", [])),
                "experience": p.get("experience", ""),
                "email": p.get("email", ""),
                "apply_url": p.get("apply_url", ""),
                "apply_type": links.KINDS[p["apply_kind"]][1] if p.get("apply_kind") else "",
                "other_links": other_links_str(p),
                "how_to_apply": how_to_apply(p),
                "age": age_to_str(p.get("age")),
                "post_url": p.get("post_url") or "",
                "score": p.get("score", 0),
                "description": p.get("description", ""),
            })

    # Each post is listed once, under its first/primary region only - a post
    # tagged for several regions (e.g. "Pune | Hyderabad | Bangalore") used
    # to repeat its full entry under every matching section, bloating the
    # .md with duplicate full descriptions. Other matching regions are noted
    # inline on the single entry instead (see "Also relevant to" below).
    by_region = {}
    for p in ranked:
        primary = (p.get("locations") or ["Other"])[0]
        by_region.setdefault(primary, []).append(p)

    def region_sort_key(region):
        try:
            return (0, classify.STATE_ORDER.index(region))
        except ValueError:
            return (1, region)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# LinkedIn scan — {now.strftime('%Y-%m-%d %I:%M %p')}\n\n")
        f.write(f"Window: last {window_hours}h. {len(posts)} candidate posts after filtering.\n\n")
        if warnings:
            f.write("## Warnings\n")
            for w in warnings:
                f.write(f"- {w}\n")
            f.write("\n")
        # Everything needed to apply lives here - no need to open the CSV.
        # Full description (code-fenced so #hashtags/**bold** in the raw post
        # text don't get misinterpreted as markdown), email surfaced as the
        # first thing you see, no per-region cap.
        for region in sorted(by_region, key=region_sort_key):
            region_posts = by_region[region]
            f.write(f"## {region} ({len(region_posts)})\n\n")
            for i, p in enumerate(region_posts, 1):
                company = p.get("company") or "?"
                if p.get("company_inferred"):
                    company += " (from email)"
                f.write(f"### {i}. {p.get('role', '?')} — {company} (score {p.get('score', 0)})\n")
                f.write(f"- **Apply:** {md_apply_line(p)}\n")
                if p.get("post_url"):
                    f.write(f"- **Post link:** {p['post_url']}\n")
                f.write(f"- **Location:** {p.get('location') or '—'}\n")
                other_regions = (p.get("locations") or [])[1:]
                if other_regions:
                    f.write(f"- **Also relevant to:** {', '.join(other_regions)}\n")
                f.write(f"- **Experience:** {p.get('experience') or '—'}\n")
                f.write(f"- **Posted:** {age_to_str(p.get('age')) or '—'} ago\n")
                desc = (p.get("description") or "").strip()
                if desc:
                    f.write("\n```\n")
                    f.write(desc)
                    f.write("\n```\n")
                f.write("\n")

    return csv_path, md_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=24, help="Hours to look back")
    parser.add_argument("--login", action="store_true", help="One-time manual LinkedIn login")
    args = parser.parse_args()

    log_path = setup_logging()
    logging.info("Log file: %s", log_path)

    config = load_config()
    scanner = linkedin.LinkedInScanner(config)

    if args.login:
        scanner.login()
        return

    seen = load_seen()
    queries = config.get("queries", [])
    raw_posts, warnings = scanner.scan(queries, args.window, set(seen.keys()))
    processed = classify.process_posts(raw_posts, config)

    now_iso = datetime.now().isoformat()
    for p in processed:
        if p.get("urn"):
            seen[p["urn"]] = now_iso
    save_seen(seen)

    # seen.json (above) tracks exact scraped posts, to avoid re-collecting
    # them. reported.json tracks job OPENINGS already shown to the user, so
    # a fresh repost of something already seen doesn't resurface as "new".
    reported = load_reported()
    dedup_window_days = config.get("dedup_window_days", 7)
    before_count = len(processed)
    processed = drop_recently_reported(processed, reported, dedup_window_days)
    save_reported(reported)
    if before_count != len(processed):
        logging.info(
            "Suppressed %d already-reported opening(s) seen again within %d days",
            before_count - len(processed), dedup_window_days,
        )

    # Link analysis runs last, on the final survivors only, so network
    # resolution is never spent on posts that were filtered or suppressed.
    processed = links.enrich_posts(processed, config)

    csv_path, md_path = write_outputs(processed, warnings, args.window)
    logging.info("Wrote %s", csv_path)
    logging.info("Wrote %s", md_path)
    if warnings:
        logging.info("Warnings:")
        for w in warnings:
            logging.info(" - %s", w)


if __name__ == "__main__":
    main()
