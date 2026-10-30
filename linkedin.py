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
from collections import Counter
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


last_capture_failure = None  # why the most recent capture_post_permalink() returned None


def capture_post_permalink(page, container):
    """Best-effort: open the post's '...' menu, click 'Copy link to post',
    and read the real permalink back from the clipboard. The menu item text
    is unverified against live LinkedIn (it only renders after the click, so
    it couldn't be confirmed from a static page capture) - this is written
    defensively and must never raise, since callers already have a working
    fallback link (company/author profile) from extract_post().
    """
    global last_capture_failure
    last_capture_failure = None
    try:
        menu_button = container.query_selector(MENU_BUTTON_SELECTOR)
        if not menu_button:
            last_capture_failure = "no_menu_button"
            return None
        # Live check 2026-10-30: clicking without scrolling the button into
        # view first left the menu closed ~75% of the time, and "Copy link to
        # post" needs ~0.4-0.9s to render after it opens.
        menu_button.scroll_into_view_if_needed(timeout=4000)
        page.wait_for_timeout(random.randint(600, 900))
        menu_button.click(timeout=5000)

        copy_item = None
        for _ in range(9):  # up to ~2.7s
            page.wait_for_timeout(300)
            copy_item = page.query_selector(COPY_LINK_ITEM_SELECTOR)
            if copy_item:
                break
        if not copy_item:
            last_capture_failure = "menu_not_opened" if menu_button.get_attribute("aria-expanded") != "true" else "no_copy_item"
            page.keyboard.press("Escape")
            return None

        # Clear the clipboard first so a stale copy (or something the user
        # copied earlier) can never be mistaken for this post's link.
        try:
            page.evaluate("navigator.clipboard.writeText('')")
        except Exception:
            pass
        copy_item.click(timeout=5000)

        for _ in range(8):  # up to ~2.4s for the copy to land
            page.wait_for_timeout(300)
            link = (page.evaluate("navigator.clipboard.readText()") or "").strip()
            if classify.is_permalink(link):
                return link
        last_capture_failure = "clipboard_not_permalink"
        return None
    except Exception as e:
        last_capture_failure = "error"
        logging.debug("  permalink capture failed: %s", e)
        return None
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(random.uniform(2.0, 4.5))


def _qkey(query):
    """Case/whitespace-insensitive key for matching deep_queries to queries."""
    return " ".join(str(query).lower().split())


# Absolute scroll limits, applied on top of whatever config.yaml says.
HARD_MAX_STEPS_PER_QUERY = 60
HARD_MAX_STEPS_PER_RUN = 220


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

    def _would_survive(self, post):
        """True if the classifier would keep this post. Errors count as yes."""
        try:
            return bool(classify.process_posts([dict(post)], self.config, quiet=True))
        except Exception:
            return True

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
        # Safety rails that no config value can exceed, so a typo in
        # config.yaml can never turn into an endless or account-risky scroll.
        max_steps = min(pacing.get("max_scroll_steps", 30), HARD_MAX_STEPS_PER_QUERY)
        stale_limit = pacing.get("stale_scroll_limit", 3)
        # Dynamic depth: keep scrolling while recent scrolls still produce posts
        # that survive the classifier, stop once they dry up. max_steps is only
        # the per-query ceiling; run_scroll_budget bounds the whole run.
        yield_window = pacing.get("yield_window", 3)
        min_yield = pacing.get("min_useful_per_window", 1)
        run_budget = min(pacing.get("run_scroll_budget", 110), HARD_MAX_STEPS_PER_RUN)
        reserve_steps = pacing.get("reserve_steps_per_query", 4)
        steps_used = 0
        # Queries listed under deep_queries scroll until the results run out
        # (or their own max_scroll_steps / max_posts), ignore the yield stop,
        # and are counted apart from run_scroll_budget so they don't starve
        # the other queries.
        deep_cfg = {_qkey(k): (v or {}) for k, v in (self.config.get("deep_queries") or {}).items()}
        deep_steps_used = 0
        for name in deep_cfg:
            if name not in {_qkey(q) for q in queries}:
                logging.warning('deep_queries entry "%s" matches no query in "queries" - ignored', name)

        all_posts = []
        warnings = []

        with sync_playwright() as p:
            context = self._launch(p)
            page = context.new_page()

            for i, query in enumerate(queries):
                block = None
                seen_in_query = set()
                permalink_captures_this_query = 0
                permalink_ok_this_query = 0
                capture_failures = Counter()
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
                    recent_useful = []
                    deep = _qkey(query) in deep_cfg  # listed = deep, even with no settings
                    deep_max_posts = deep_cfg[_qkey(query)].get("max_posts") if deep else None
                    if deep:
                        ceiling = min(deep_cfg[_qkey(query)].get("max_scroll_steps", max_steps), HARD_MAX_STEPS_PER_QUERY)
                        query_cap = ceiling
                        budget_limited = False
                    else:
                        # Leave each later normal query enough steps to at least start.
                        queries_left = sum(1 for q in queries[i + 1:] if _qkey(q) not in deep_cfg)
                        query_cap = min(max_steps, max(reserve_steps, run_budget - steps_used - queries_left * reserve_steps))
                        budget_limited = query_cap < max_steps
                    query_cap = max(0, min(query_cap, HARD_MAX_STEPS_PER_RUN - steps_used - deep_steps_used))
                    if query_cap == 0:
                        warnings.append(f'Query "{query}" skipped: hard scroll limit for the run ({HARD_MAX_STEPS_PER_RUN}) reached')
                        logging.warning("  hard scroll limit reached, skipping query")
                        continue

                    for step in range(query_cap):
                        if deep:
                            deep_steps_used += 1
                        else:
                            steps_used += 1
                        containers = page.query_selector_all(POST_CONTAINER_SELECTOR)
                        new_count = 0
                        batch = []
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
                                # Spend the capped clicks only on posts that will
                                # survive the filters; before this, ~60% of them
                                # went to posts that were then dropped anyway.
                                wants_capture = (
                                    self.capture_post_links and not has_email and under_cap
                                    and self._would_survive(post)
                                )
                                if wants_capture:
                                    real_link = capture_post_permalink(page, c)
                                    permalink_captures_this_query += 1
                                    if real_link:
                                        post["post_url"] = real_link
                                        permalink_ok_this_query += 1
                                    else:
                                        capture_failures[last_capture_failure] += 1
                                    if permalink_captures_this_query == self.max_permalink_captures_per_query:
                                        logging.info(
                                            "  hit permalink capture cap (%d) for this query - "
                                            "remaining no-email posts keep their profile/company link",
                                            self.max_permalink_captures_per_query,
                                        )
                                post["query"] = query
                                all_posts.append(post)
                                batch.append(post)

                        try:
                            useful = len(classify.process_posts([dict(b) for b in batch], self.config, quiet=True)) if batch else 0
                        except Exception:
                            useful = len(batch)  # can't judge -> don't stop early on a classifier error
                        # A scroll whose cards were all seen in earlier runs says
                        # nothing about how good the query is further down, so it
                        # must not count toward the "dried up" stop (a same-day
                        # re-run would otherwise stop before reaching new posts).
                        if batch:
                            recent_useful.append(useful)

                        logging.info(
                            "  scroll %d/%d: +%d cards seen, %d new (%d pass filters), %d kept so far",
                            step + 1, query_cap, new_count, len(batch), useful, len(all_posts) - collected_before,
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

                        if deep_max_posts and len(all_posts) - collected_before >= deep_max_posts:
                            logging.info("  reached max_posts (%d) for this deep query, stopping query", deep_max_posts)
                            break

                        if not deep and len(recent_useful) >= yield_window and sum(recent_useful[-yield_window:]) < min_yield:
                            logging.info(
                                "  last %d scrolls gave %d posts passing the filters (< %d), stopping query",
                                yield_window, sum(recent_useful[-yield_window:]), min_yield,
                            )
                            break

                        if step == query_cap - 1:
                            continue  # last allowed read: scrolling again would load cards nobody reads

                        # The results list scrolls inside its own container, and the
                        # wheel only reaches it while the mouse is over it. (The
                        # permalink-capture clicks used to move it there as a side
                        # effect; without them the wheel scrolled nothing.)
                        page.mouse.move(random.randint(520, 840), random.randint(380, 620))
                        page.mouse.wheel(0, 2000)
                        time.sleep(random.uniform(scroll_min, scroll_max))

                        block = detect_block(page, self.block_signals)
                        if block:
                            warnings.append(f'Stopped mid-scroll on "{query}": {block}')
                            logging.warning("  BLOCKED mid-scroll: %s", block)
                            break
                    else:
                        why = "run scroll budget" if budget_limited else "per-query ceiling"
                        warnings.append(f'Query "{query}" stopped at {query_cap} scrolls ({why}) while still productive')
                        logging.info("  hit the %d-step limit (%s) while still productive", query_cap, why)

                except Exception as e:
                    warnings.append(f'Query "{query}" failed: {e}')
                    logging.exception("  ERROR on query \"%s\"", query)

                no_results_page = False
                if len(seen_in_query) == 0 and not block:
                    try:
                        no_results_page = "no results found" in page.inner_text("body").lower()
                    except Exception:
                        pass
                if no_results_page:
                    logging.info("  LinkedIn says 'No results found' for this query (not a scraping failure)")
                elif len(seen_in_query) == 0 and not block:
                    debug_path = self._dump_debug(page, query, i)
                    if debug_path:
                        warnings.append(f'Zero cards matched for "{query}" - debug saved to {debug_path}.png/.html')
                        logging.warning("  zero cards matched selector - saved debug files to %s.png / .html", debug_path)

                if permalink_captures_this_query:
                    logging.info(
                        "  post links captured: %d of %d attempts",
                        permalink_ok_this_query, permalink_captures_this_query,
                    )
                    if capture_failures:
                        logging.info("  capture failures: %s", dict(capture_failures))
                logging.info("[%d/%d] done: %d new posts kept", i + 1, len(queries), len(all_posts) - collected_before)

                if block:
                    break

                if i < len(queries) - 1:
                    pause = random.uniform(query_min, query_max)
                    logging.info("  pausing %.0fs before next query...", pause)
                    time.sleep(pause)

            context.close()

        return all_posts, warnings
