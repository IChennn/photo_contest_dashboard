#!/usr/bin/env python3
"""
Photo contest scraper for Japan.

Source definitions live in sources.py — that is the only file you should
need to edit. This file handles fetching, pagination, parsing, and output.

Output: docs/data/contests.json
"""

import json
import re
import time
import hashlib
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

from sources import SOURCES

OUTPUT_PATH = Path(__file__).parent.parent / "docs" / "data" / "contests.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# Seconds to wait between requests (be polite to servers)
REQUEST_DELAY = 1.5

# Maximum pages to follow when paginating (safety cap)
MAX_PAGES = 10


# =============================================================================
# Utilities
# =============================================================================

def base_url(url: str) -> str:
    """Return scheme + host from any URL, e.g. 'https://picru.jp'."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def get_soup(url: str, timeout: int = 15) -> BeautifulSoup | None:
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [ERROR] {url}: {e}")
        return None


def make_id(name: str, source_name: str) -> str:
    """Stable 10-char hash used as a deduplication key."""
    return hashlib.md5(f"{source_name}:{name}".encode()).hexdigest()[:10]


def abs_url(href: str, page_url: str) -> str:
    """Resolve any href (relative or absolute) against the current page URL."""
    if not href:
        return page_url
    return urljoin(page_url, href)


def parse_deadline(text: str) -> str | None:
    """
    Extract a deadline from arbitrary Japanese text.
    Returns YYYY-MM-DD, or None if nothing found.

    Recognised patterns:
      2026年5月31日   — explicit western year
      5/31 or 05/31  — month/day only, year inferred
    """
    if not text:
        return None
    text = text.replace("\u3000", " ").strip()

    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    m = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        today = date.today()
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year if month >= today.month else today.year + 1
        return f"{year}-{month:02d}-{day:02d}"

    return None


def extract_prize(text: str) -> str | None:
    """Pull a prize amount like '50万円' out of text."""
    m = re.search(r"[\d,]+万?円", text)
    return m.group(0) if m else None


def find_next_page(soup: BeautifulSoup, current_url: str) -> str | None:
    """
    Look for a 'next page' link in common pagination patterns.
    Returns the absolute URL of the next page, or None.
    """
    # Common Japanese pagination text and rel=next
    candidates = soup.select(
        "a[rel='next'], "
        "a.next, "
        "a.pagination-next, "
        "li.next > a, "
        ".pager-next a, "
        ".wp-pagenavi a.nextpostslink"
    )
    # Also match links containing 次 (next) or ›/»
    for a in soup.find_all("a", href=True):
        txt = a.get_text(strip=True)
        if txt in ("次へ", "次のページ", "›", "»", ">", "NEXT") or a.get("rel") == ["next"]:
            candidates.append(a)

    if candidates:
        href = candidates[0].get("href", "")
        if href:
            return abs_url(href, current_url)
    return None


def make_entry(name, source_name, source_url, entry_url,
               deadline_text="", prize=None) -> dict:
    """Build a normalised contest dict."""
    return {
        "id":           make_id(name, source_name),
        "name":         name,
        "source":       source_name,
        "sourceUrl":    source_url,
        "entryUrl":     entry_url,
        "deadline":     parse_deadline(deadline_text),
        "deadlineText": deadline_text or "要確認",
        "prize":        prize,
        "notes":        None,
    }


# =============================================================================
# Generic scraper
# Iterates pages automatically; tries common CSS selectors for cards.
# =============================================================================

CARD_SELECTORS = [
    "article",
    "li.contest",
    "div.contest-list-item",
    "div.comp-item",
    "li.competition",
    "div.contest-item",
]

NAME_SELECTORS = ["h2", "h3", "h4", ".title", ".name", "strong"]


def scrape_generic(source: dict) -> list[dict]:
    """
    Entry point for each source. Routes to a custom parser if one is
    registered in CUSTOM_PARSERS, otherwise uses the generic approach.
    """
    custom = CUSTOM_PARSERS.get(source["name"])
    if custom:
        return custom(source)

    contests = []
    url: str | None = source["url"]
    visited = set()
    page = 0

    while url and url not in visited and page < MAX_PAGES:
        print(f"  [{source['name']}] page {page + 1}: {url}")
        soup = get_soup(url)
        if not soup:
            break

        visited.add(url)
        page += 1

        # Find cards using the first selector that returns results
        items = []
        for sel in CARD_SELECTORS:
            items = soup.select(sel)
            if items:
                break

        for item in items:
            name_el = None
            for sel in NAME_SELECTORS:
                name_el = item.select_one(sel)
                if name_el:
                    break
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if not name or len(name) < 5:
                continue

            link_el = item.select_one("a[href]")
            entry_url = abs_url(link_el["href"] if link_el else "", url)

            deadline_text = ""
            prize = None
            for el in item.select("p, span, div, time, li"):
                t = el.get_text(strip=True)
                if not deadline_text and any(kw in t for kw in ("締切", "〆切", "期限", "応募期間")):
                    deadline_text = t[:80]
                if not prize and any(kw in t for kw in ("万円", "賞金")):
                    prize = extract_prize(t)

            contests.append(make_entry(name, source["name"], url, entry_url,
                                       deadline_text, prize))

        url = find_next_page(soup, url)
        time.sleep(REQUEST_DELAY)

    print(f"  [{source['name']}] {len(contests)} entries")
    return contests


# =============================================================================
# Custom parsers
# Add a function here when a site's layout doesn't match the generic scraper.
# Register it by name in CUSTOM_PARSERS at the bottom of this section.
# =============================================================================

def parse_picru(source: dict) -> list[dict]:
    """
    Picru's listing page links to contest detail pages via
    <a href="/opens/view/NNN"> rather than using article cards.
    Also handles Picru's page:N URL pattern for pagination.
    """
    contests = []
    url: str | None = source["url"]
    # Normalise entry URL: picru.jp/opens/ → picru.jp/opens/index/page:1
    if url.rstrip("/").endswith("/opens"):
        url = url.rstrip("/") + "/index/page:1"
    elif url.rstrip("/").endswith("/opens/"):
        url = url.rstrip("/") + "index/page:1"

    visited = set()
    page = 0

    while url and url not in visited and page < MAX_PAGES:
        print(f"  [Picru] page {page + 1}: {url}")
        soup = get_soup(url)
        if not soup:
            break

        visited.add(url)
        page += 1

        # Try structured cards first, then fall back to bare detail links
        items = soup.select("div.contest-list-item, article.contest-item, li.contest")
        if not items:
            items = soup.select("a[href*='/opens/view/']")

        for item in items:
            try:
                link_el = item if item.name == "a" else item.select_one("a")
                if not link_el:
                    continue
                href = abs_url(link_el.get("href", ""), url)

                name_el = item.select_one("h2, h3, .title, .name, strong")
                name = (name_el.get_text(strip=True) if name_el
                        else link_el.get_text(strip=True))
                if not name or len(name) < 3:
                    continue

                deadline_text = ""
                for el in item.select("span, p, div, time"):
                    t = el.get_text(strip=True)
                    if any(kw in t for kw in ("締切", "〆切", "期限", "月")):
                        deadline_text = t
                        break

                contests.append(make_entry(name, source["name"], href, href,
                                           deadline_text))
            except Exception as e:
                print(f"    [Picru] item error: {e}")

        # Picru paginates as /opens/index/page:N — build next URL manually
        # if the generic next-page detection doesn't find anything
        next_url = find_next_page(soup, url)
        if not next_url:
            m = re.search(r"/page:(\d+)", url)
            if m:
                next_page_num = int(m.group(1)) + 1
                next_url = re.sub(r"/page:\d+", f"/page:{next_page_num}", url)
                # Stop if the next page returns no items (checked next iteration)
            else:
                next_url = None

        url = next_url
        time.sleep(REQUEST_DELAY)

    print(f"  [Picru] {len(contests)} entries")
    return contests


def parse_photosekai(source: dict) -> list[dict]:
    """
    フォトセカイ structures each contest as an <h3> heading followed by
    sibling elements containing the URL, deadline, and prize.
    The page also has multiple sub-sections worth scraping.
    """
    contests = []

    # Scrape the main URL plus related sub-pages on the same site
    extra_paths = ["/post/photocontestprize/", "/post/prefecture/"]
    parsed = urlparse(source["url"])
    urls_to_scrape = [source["url"]] + [
        f"{parsed.scheme}://{parsed.netloc}{p}" for p in extra_paths
    ]

    for url in urls_to_scrape:
        print(f"  [フォトセカイ] {url}")
        soup = get_soup(url)
        if not soup:
            time.sleep(REQUEST_DELAY)
            continue

        for h3 in soup.select("h3"):
            name = h3.get_text(strip=True)
            if not name or len(name) < 5:
                continue

            entry_url = None
            deadline_text = ""
            prize = None

            # Walk up to 6 siblings; stop at the next h3
            sibling = h3.find_next_sibling()
            for _ in range(6):
                if sibling is None or sibling.name == "h3":
                    break
                text = sibling.get_text(" ", strip=True)

                if not entry_url:
                    link = sibling.select_one("a[href]")
                    if link:
                        entry_url = link["href"]

                if not deadline_text:
                    m = re.search(r"\d{4}年\d{1,2}月\d{1,2}日", text)
                    if m:
                        deadline_text = m.group(0)

                if not prize and any(kw in text for kw in ("賞金", "万円")):
                    prize = extract_prize(text)

                sibling = sibling.find_next_sibling()

            if not entry_url:
                continue

            contests.append(make_entry(name, source["name"], url, entry_url,
                                       deadline_text, prize))

        time.sleep(REQUEST_DELAY)

    print(f"  [フォトセカイ] {len(contests)} entries")
    return contests


# Register custom parsers by source name.
# Sources not listed here fall through to scrape_generic().
CUSTOM_PARSERS: dict = {
    "Picru":       parse_picru,
    "フォトセカイ": parse_photosekai,
}


# =============================================================================
# Post-processing
# =============================================================================

def deduplicate(contests: list[dict]) -> list[dict]:
    """Remove duplicates by ID; first occurrence wins."""
    seen: dict = {}
    for c in contests:
        if c["id"] not in seen:
            seen[c["id"]] = c
    return list(seen.values())


def filter_active(contests: list[dict]) -> list[dict]:
    """Drop entries whose deadline has already passed."""
    today = date.today()
    result = []
    for c in contests:
        if c.get("deadline"):
            try:
                if date.fromisoformat(c["deadline"]) < today:
                    continue
            except ValueError:
                pass
        result.append(c)
    return result


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    print("=== Photo contest scraper started ===")
    all_contests: list[dict] = []

    for source in SOURCES:
        print(f"\n--- {source['name']} ---")
        try:
            results = scrape_generic(source)
            all_contests.extend(results)
        except Exception as e:
            print(f"  [ERROR] {source['name']}: {e}")

    all_contests = deduplicate(all_contests)
    all_contests = filter_active(all_contests)
    all_contests.sort(key=lambda c: c.get("deadline") or "9999-12-31")

    output = {
        "updatedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count":     len(all_contests),
        "contests":  all_contests,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n=== Done: {len(all_contests)} contests → {OUTPUT_PATH} ===")


if __name__ == "__main__":
    main()
