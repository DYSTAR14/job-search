"""Read-only LinkedIn post/content search scraper.

Design notes (see conversation for the full reasoning):
- Navigates directly to a filtered search URL (keywords + datePosted=past-24h
  + sortBy=date_posted) instead of click-driving the filter UI, which is far
  more stable across LinkedIn's frequent UI A/B tests.
- LinkedIn's search results page (as of the live markup captured 2026-10-02)
  is rendered by a "Server-Driven UI" system with fully hashed/meaningless
  class names (e.g. "b4lmvb") and NO data-urn attribute anywhere - the
  original plan to key off [data-urn^="urn:li:activity"] does not match
  current markup at all. The stable anchors that do exist:
    1. each post card: [role="listitem"][componentkey^="update-card-focus"]
    2. a company mention link inside it: a[href*="/company/"]
    3. the author's profile link inside it: a[href*="/in/"]
  No post permalink/URN is exposed in the markup without clicking into a
  "..." menu ("Copy link to post"). We only do that click for posts with no
  extractable email - those are the ones where the real post link is the
  only actionable next step, so it's worth the extra interaction; posts with
  an email already have one (see capture_post_permalink(), gated by
  config's capture_post_links). Otherwise post_url is a best-effort link to
  the company page (preferred) or author profile, not the exact post. The
  dedup key is a content hash (link + text prefix) rather than a real URN -
  see extract_post().
  Everything else (role, experience, location, email) is parsed out of the
  card's plain innerText() blob by classify.py, not via dedicated selectors.
- Checks for login walls, checkpoints, CAPTCHAs and the commercial-use-limit
  banner after every navigation and every scroll step, and stops the whole
  run immediately if any is seen - this tool never tries to work around them.
- Only ever reads. It never applies, messages, comments, follows or connects.
"""

import hashlib
import logging
import random
import re
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

import classify

POST_CONTAINER_SELECTOR = '[role="listitem"][componentkey^="update-card-focus"]'
COMPANY_LINK_SELECTOR = 'a[href*="/company/"]'
AUTHOR_LINK_SELECTOR = 'a[href*="/in/"]'

DEFAULT_BLOCK_SIGNALS = [
    "unusual activity",
    "we've restricted",
    "checkpoint/challenge",
    "commercial use limit",
    "verify you're not a robot",
    "security verification",
]


def build_search_url(query, window_hours):
    date_posted = '"past-24h"' if window_hours <= 24 else '"past-week"'
    kw = quote(f'"{query}"')
    date_posted_enc = quote(date_posted)
    sort_enc = quote('"date_posted"')
    return (
        "https://www.linkedin.com/search/results/content/"
        f"?keywords={kw}&datePosted={date_posted_enc}&sortBy={sort_enc}&origin=FACETED_SEARCH"
    )


def wait_for_stable_cards(page, selector, stable_checks=2, poll_interval=0.6, max_wait=12.0):
    """LinkedIn's search results render progressively - elements can appear
    and then be entirely replaced as more data streams in (confirmed: 3
    cards present 0.1s after load, 0 present moments later). Waiting for the
    selector to merely exist isn't enough; poll until the count holds steady
    across consecutive checks, or give up after max_wait and return whatever
    was last seen.
    """
    start = time.time()
    last_count = -1
    stable_streak = 0
    while time.time() - start < max_wait:
        count = len(page.query_selector_all(selector))
        if count == last_count and count > 0:
            stable_streak += 1
            if stable_streak >= stable_checks:
                return count
        else:
            stable_streak = 0
        last_count = count
        time.sleep(poll_interval)
    return max(last_count, 0)


def detect_block(page, block_signals):
    url = page.url
    if "/checkpoint/" in url or "/authwall" in url:
        return "checkpoint_or_authwall"
    try:
        body_text = page.inner_text("body").lower()
    except Exception:
        return None
    for signal in block_signals:
        if signal.lower() in body_text:
            return signal
    return None


# Marks the end of the author header block (name, connection degree,
# headline, timestamp) and the start of the actual post content. Checked in
# this order because "Follow" is a substring of "Following" - matching
# "Following" first avoids cutting mid-word. 1st-degree connections show
# "Message" instead of a follow control.
BODY_START_RE = re.compile(r"\b(Following|Follow|Message)\b")


def parse_relative_age(header):
    """header is the text before the post body (see BODY_START_RE) - the
    timestamp always lives there, right after the author's name/headline.
    Recruiter headlines are often keyword-stuffed and 200+ chars long, which
    is why this needs the real header boundary rather than a fixed prefix.
    """
    if re.search(r"\bjust now\b", header, re.IGNORECASE):
        return timedelta(0)
    m = re.search(r"\b(\d{1,2})\s*(mo|[mhdw])\b", header)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    if unit == "mo":
        return timedelta(days=value * 30)
    unit_map = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    return timedelta(**{unit_map[unit]: value})


def extract_post(container):
    full_text = container.inner_text()

    # Split off the author header (name/headline/timestamp/Follow button) so
    # that role/field extraction only ever sees the actual post content, not
    # the author's own bio - which can itself contain words like "hiring"
    # (e.g. a recruiter's headline "...Leadership Hiring Across CXO Roles")
    # and would otherwise get mistaken for the job post's own headline.
    m = BODY_START_RE.search(full_text)
    if m:
        header, body = full_text[: m.start()], full_text[m.end() :]
    else:
        header, body = full_text[:400], full_text

    company = None
    company_href = None
    try:
        link = container.query_selector(COMPANY_LINK_SELECTOR)
        if link:
            company = link.inner_text().strip() or None
            company_href = link.get_attribute("href")
    except Exception:
        pass

    author_href = None
    try:
        link = container.query_selector(AUTHOR_LINK_SELECTOR)
        if link:
            author_href = link.get_attribute("href")
    except Exception:
        pass

    # No post permalink/URN is exposed in the markup (see module docstring).
    # post_url defaults to the company page or author profile, not the post
    # itself - the caller may overwrite this with a real permalink captured
    # via capture_post_permalink() below, when that's enabled and succeeds.
    # The dedup key is a content hash of this link plus a text prefix, since
    # it's the closest thing to a stable identity we have.
    post_url = company_href or author_href
    dedupe_basis = f"{company_href or author_href or ''}|{body[:200]}"
    urn = hashlib.sha1(dedupe_basis.encode("utf-8")).hexdigest()[:16]

    return {
        "urn": urn,
        "text": body,
        "company": company,
        "post_url": post_url,
        "age": parse_relative_age(header),
    }


MENU_BUTTON_SELECTOR = 'button[aria-label^="Open control menu"]'
COPY_LINK_ITEM_SELECTOR = "text=/copy link to post/i"


def capture_post_permalink(page, container):
    """Best-effort: open the post's '...' menu, click 'Copy link to post',
    and read the real permalink back from the clipboard. The menu item text
    is unverified against live LinkedIn (it only renders after the click, so
    it couldn't be confirmed from a static page capture) - this is written
    defensively and must never raise, since callers already have a working
    fallback link (company/author profile) from extract_post().
    """
    try:
        menu_button = container.query_selector(MENU_BUTTON_SELECTOR)
        if not menu_button:
            return None
        menu_button.click()
        page.wait_for_timeout(random.randint(400, 800))

        copy_item = page.query_selector(COPY_LINK_ITEM_SELECTOR)
        if not copy_item:
            page.keyboard.press("Escape")
            return None
        copy_item.click()
        page.wait_for_timeout(random.randint(300, 600))

        link = page.evaluate("navigator.clipboard.readText()")
        if link and link.strip().startswith("http"):
            return link.strip()
        return None
    except Exception as e:
        logging.debug("  permalink capture failed: %s", e)
        return None
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(random.uniform(2.0, 4.5))


class LinkedInScanner:
    def __init__(self, config):
        self.config = config
        browser_cfg = config.get("browser", {})
        self.profile_dir = browser_cfg.get("profile_dir", "profile/linkedin")
        self.channel = browser_cfg.get("channel", "chrome")
        self.headless = browser_cfg.get("headless", False)
        self.block_signals = config.get("block_signals", DEFAULT_BLOCK_SIGNALS)
        self.capture_post_links = config.get("capture_post_links", True)
        self.max_permalink_captures_per_query = config.get("max_permalink_captures_per_query", 15)

    def _launch(self, playwright):
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            self.profile_dir,
            channel=self.channel,
            headless=self.headless,
            viewport={"width": 1366, "height": 900},
        )
        if self.capture_post_links:
            try:
                context.grant_permissions(["clipboard-read", "clipboard-write"])
            except Exception:
                logging.warning("Could not grant clipboard permissions - real post links will be skipped")
                self.capture_post_links = False
        Stealth().apply_stealth_sync(context)
        return context

    def login(self):
        with sync_playwright() as p:
            context = self._launch(p)
            page = context.new_page()
            page.goto("https://www.linkedin.com/login")
            logging.info("Log in manually in the opened browser window (complete any 2FA).")
            input("Press Enter here once you can see your LinkedIn feed... ")

            # The page may still be mid-redirect from whatever LinkedIn just
            # did (password reset prompt, security check, etc). Let it
            # settle before touching it, and only force a goto if we're not
            # already somewhere feed-like.
            try:
                page.wait_for_load_state("load", timeout=10000)
            except Exception:
                pass

            if "/feed" not in page.url and "/in/" not in page.url:
                try:
                    page.goto("https://www.linkedin.com/feed/", timeout=15000)
                except Exception as e:
                    logging.info("Note: navigation to feed was interrupted (%s). Checking current page instead.", e)
                try:
                    page.wait_for_load_state("load", timeout=10000)
                except Exception:
                    pass

            block = detect_block(page, self.block_signals)
            if block:
                logging.warning("Still seeing a login/checkpoint page (%s). Try again.", block)
            elif "/feed" not in page.url and "/in/" not in page.url:
                logging.warning("Unexpected page after login (%s). Check the browser window and re-run if needed.", page.url)
            else:
                logging.info("Login looks good. Session saved to %s", self.profile_dir)
            context.close()

    def _dump_debug(self, page, query, index):
        """Zero posts found is far more likely to mean the container
        selector doesn't match LinkedIn's current markup than that a query
        genuinely has no results. Save a screenshot + the raw HTML so that
        can be diagnosed without needing to drive the browser manually.
        """
        try:
            debug_dir = Path("debug")
            debug_dir.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")[:40]
            base = debug_dir / f"{index:02d}_{slug}_{stamp}"
            page.screenshot(path=str(base) + ".png", full_page=True)
            Path(str(base) + ".html").write_text(page.content(), encoding="utf-8")
            return base
        except Exception:
            return None

    def scan(self, queries, window_hours, seen_urns):
        """Runs every query in order, returns (raw_posts, warnings).
        raw_posts excludes anything already in seen_urns. warnings records
        any query that was skipped or any block signal that stopped the run.
        """
        pacing = self.config.get("pacing", {})
        scroll_min = pacing.get("scroll_delay_min", 4)
        scroll_max = pacing.get("scroll_delay_max", 9)
        query_min = pacing.get("query_delay_min", 20)
        query_max = pacing.get("query_delay_max", 40)
        max_steps = pacing.get("max_scroll_steps", 30)
        stale_limit = pacing.get("stale_scroll_limit", 3)

        all_posts = []
        warnings = []

        with sync_playwright() as p:
            context = self._launch(p)
            page = context.new_page()

            for i, query in enumerate(queries):
                block = None
                seen_in_query = set()
                permalink_captures_this_query = 0
                collected_before = len(all_posts)
                logging.info('[%d/%d] "%s" -> loading search...', i + 1, len(queries), query)
                try:
                    url = build_search_url(query, window_hours)
                    page.goto(url, wait_until="domcontentloaded")
                    wait_started = time.time()
                    stable_count = wait_for_stable_cards(page, POST_CONTAINER_SELECTOR)
                    logging.info(
                        "  card count stabilized at %d after %.1fs",
                        stable_count, time.time() - wait_started,
                    )

                    block = detect_block(page, self.block_signals)
                    if block:
                        warnings.append(f'Stopped before query "{query}": {block}')
                        logging.warning("  BLOCKED: %s", block)
                        break

                    stale_steps = 0

                    for step in range(max_steps):
                        containers = page.query_selector_all(POST_CONTAINER_SELECTOR)
                        new_count = 0
                        hit_window_edge = False

                        for c in containers:
                            post = extract_post(c)
                            urn = post["urn"]
                            if urn in seen_in_query:
                                continue
                            seen_in_query.add(urn)
                            new_count += 1

                            if post["age"] is not None and post["age"] > timedelta(hours=window_hours):
                                hit_window_edge = True
                                break

                            if urn not in seen_urns:
                                has_email = bool(classify.EMAIL_RE.search(post["text"]))
                                under_cap = permalink_captures_this_query < self.max_permalink_captures_per_query
                                if self.capture_post_links and not has_email and under_cap:
                                    real_link = capture_post_permalink(page, c)
                                    permalink_captures_this_query += 1
                                    if real_link:
                                        post["post_url"] = real_link
                                    if permalink_captures_this_query == self.max_permalink_captures_per_query:
                                        logging.info(
                                            "  hit permalink capture cap (%d) for this query - "
                                            "remaining no-email posts keep their profile/company link",
                                            self.max_permalink_captures_per_query,
                                        )
                                post["query"] = query
                                all_posts.append(post)

                        logging.info(
                            "  scroll %d/%d: +%d cards seen, %d kept so far",
                            step + 1, max_steps, new_count, len(all_posts) - collected_before,
                        )

                        if hit_window_edge:
                            logging.info("  reached the window edge (oldest visible post is too old), stopping query")
                            break

                        if new_count == 0:
                            stale_steps += 1
                            if stale_steps >= stale_limit:
                                logging.info("  no new cards for %d scrolls, stopping query", stale_limit)
                                break
                        else:
                            stale_steps = 0

                        page.mouse.wheel(0, 2000)
                        time.sleep(random.uniform(scroll_min, scroll_max))

                        block = detect_block(page, self.block_signals)
                        if block:
                            warnings.append(f'Stopped mid-scroll on "{query}": {block}')
                            logging.warning("  BLOCKED mid-scroll: %s", block)
                            break
                    else:
                        warnings.append(f'Query "{query}" hit the {max_steps}-step scroll cap')
                        logging.info("  hit the %d-step scroll cap", max_steps)

                except Exception as e:
                    warnings.append(f'Query "{query}" failed: {e}')
                    logging.exception("  ERROR on query \"%s\"", query)

                if len(seen_in_query) == 0 and not block:
                    debug_path = self._dump_debug(page, query, i)
                    if debug_path:
                        warnings.append(f'Zero cards matched for "{query}" - debug saved to {debug_path}.png/.html')
                        logging.warning("  zero cards matched selector - saved debug files to %s.png / .html", debug_path)

                logging.info("[%d/%d] done: %d new posts kept", i + 1, len(queries), len(all_posts) - collected_before)

                if block:
                    break

                if i < len(queries) - 1:
                    pause = random.uniform(query_min, query_max)
                    logging.info("  pausing %.0fs before next query...", pause)
                    time.sleep(pause)

            context.close()

        return all_posts, warnings
