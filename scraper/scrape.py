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
    """
    Build a normalised contest dict.
    'sources' is a list of {name, url} dicts — deduplicate() may append to it
    when the same contest is found on multiple sites.
    The dedup key is based on the contest name only (not source), so duplicates
    across different sites are merged into one entry.
    """
    return {
        "id":           hashlib.md5(name.encode()).hexdigest()[:10],
        "name":         name,
        "sources":      [{"name": source_name, "url": source_url}],
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
    Picru listing page structure (confirmed from live HTML):
      - Each contest block: <h3><a href="/portals/detail/NNN">Title</a></h3>
      - Pagination: /opens/index/page:N  (no standard rel=next link)
      - Deadline is NOT on the listing page — must be fetched from the detail page.
        Detail page has a two-row table: | 募集開始日 | YYYY年MM月DD日 | 募集締切日 | YYYY年MM月DD日 |
        We fetch detail pages with a short delay to be polite.
    """
    contests = []

    # Normalise to paginated form regardless of what URL was given
    base = re.sub(r"/opens.*$", "/opens/index/page:", source["url"].rstrip("/"))
    page_num = 1

    while page_num <= MAX_PAGES:
        url = f"{base}{page_num}"
        print(f"  [Picru] page {page_num}: {url}")
        soup = get_soup(url)
        if not soup:
            break

        links = soup.select("h3 > a[href*='/portals/detail/']")
        if not links:
            break  # past the last page

        for a in links:
            name = a.get_text(strip=True)
            if not name or len(name) < 3:
                continue
            detail_url = abs_url(a["href"], url)

            # Fetch the detail page to get the deadline
            deadline_text = ""
            entry_url     = detail_url
            detail_soup   = get_soup(detail_url)
            if detail_soup:
                # Table row: | 募集締切日 | YYYY年MM月DD日 |
                for td in detail_soup.select("td"):
                    if "募集締切日" in td.get_text():
                        next_td = td.find_next_sibling("td")
                        if next_td:
                            deadline_text = next_td.get_text(strip=True)
                            break
                # Real contest URL (not the picru page) is in the detail table
                for td in detail_soup.select("td"):
                    link = td.select_one("a[href^='http']")
                    if link and "picru.jp" not in link["href"]:
                        entry_url = link["href"]
                        break
            time.sleep(REQUEST_DELAY)

            contests.append(make_entry(name, source["name"], detail_url,
                                       entry_url, deadline_text))

        page_num += 1
        time.sleep(REQUEST_DELAY)

    print(f"  [Picru] {len(contests)} entries")
    return contests


def parse_photosekai(source: dict) -> list[dict]:
    """
    フォトセカイ /post/photocontestlist/ structure (confirmed from live HTML):
      - Page is organised by deadline month: <h2>2026年4月締切</h2> ... <table> ...
      - Each contest is a <tr> / single-cell <td> containing:
          "[都道府県] **[Contest name](URL)**\n応募締切：M月D日\n賞品：...\n主催：..."
      - The deadline month comes from the preceding h2 heading.
      - Contest name and URL are inside a markdown-style bold+link rendered as <strong><a>.
    """
    contests = []
    url = source["url"]
    print(f"  [フォトセカイ] {url}")
    soup = get_soup(url)
    if not soup:
        return contests

    content = soup.select_one("article, .entry-content, main, #content, .post-content")
    scope = content if content else soup

    # Track the current deadline year+month from h2 headings like "2026年4月締切"
    current_year  = date.today().year
    current_month = None

    for el in scope.find_all(["h2", "td"]):
        if el.name == "h2":
            # Extract year and month from section heading
            m = re.search(r"(\d{4})年(\d{1,2})月", el.get_text())
            if m:
                current_year  = int(m.group(1))
                current_month = int(m.group(2))
            continue

        # --- <td> element: one contest per cell ---
        text = el.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue

        # Contest name + URL: <strong><a href="...">Name</a></strong>
        link_el = el.select_one("strong > a[href], a[href]")
        if not link_el:
            continue
        name      = link_el.get_text(strip=True)
        entry_url = link_el.get("href", "")
        if not name or len(name) < 4:
            continue
        if not entry_url.startswith("http"):
            entry_url = abs_url(entry_url, url)

        # Deadline: "応募締切：4月15日" — combine with section year
        deadline_text = ""
        deadline_iso  = None
        m = re.search(r"応募締切[：:]\s*(\d{1,2})月(\d{1,2})日", text)
        if m and current_month is not None:
            month, day   = int(m.group(1)), int(m.group(2))
            deadline_iso = f"{current_year}-{month:02d}-{day:02d}"
            deadline_text = f"{current_year}年{month}月{day}日"
        else:
            # Fallback: full date anywhere in the cell
            m2 = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
            if m2:
                deadline_iso  = f"{m2.group(1)}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
                deadline_text = m2.group(0)

        prize = extract_prize(text)

        entry = make_entry(name, source["name"], url, entry_url, deadline_text, prize)
        if deadline_iso:
            entry["deadline"] = deadline_iso
        contests.append(entry)

    time.sleep(REQUEST_DELAY)
    print(f"  [フォトセカイ] {len(contests)} entries")
    return contests


def parse_japandesign(source: dict) -> list[dict]:
    """
    登竜門 confirmed HTML structure:
      <ul> of <li> > <a href="/contest-slug/"> containing:
        <h3>Contest name</h3>
        <dl> with 賞 / 主催 / 締切 rows   (main list)
        or plain "あとN日" text           (promo block)
    The generic scraper's CARD_SELECTORS don't match this structure.
    """
    contests = []
    url: str | None = source["url"]
    visited: set = set()
    page = 0

    while url and url not in visited and page < MAX_PAGES:
        print(f"  [登竜門] page {page + 1}: {url}")
        soup = get_soup(url)
        if not soup:
            break

        visited.add(url)
        page += 1

        for a in soup.select("li > a[href]"):
            h3 = a.select_one("h3")
            if not h3:
                continue
            name = h3.get_text(strip=True)
            if not name or len(name) < 5:
                continue

            href = abs_url(a["href"], url)

            # Deadline and prize are in the parent <li>
            parent_li = a.find_parent("li")
            deadline_text = ""
            prize = None
            if parent_li:
                text = parent_li.get_text(" ", strip=True)
                m = re.search(r"\d{4}年\d{1,2}月\d{1,2}日", text)
                if m:
                    deadline_text = m.group(0)
                if any(kw in text for kw in ("万円", "賞金")):
                    prize = extract_prize(text)

            contests.append(make_entry(name, source["name"], href, href,
                                       deadline_text, prize))

        url = find_next_page(soup, url)
        time.sleep(REQUEST_DELAY)

    print(f"  [登竜門] {len(contests)} entries")
    return contests


def parse_yamakei(source: dict) -> list[dict]:
    """
    山と渓谷 blocks the default User-Agent with 403.
    Retry with a Referer header; fall back gracefully with 0 results if still blocked.
    """
    contests = []
    url = source["url"]
    print(f"  [山と渓谷] {url}")

    try:
        headers = {**HEADERS, "Referer": "https://www.yamakei-online.com/"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [山と渓谷] blocked or error: {e}")
        return contests

    for item in soup.select("article, li.contest, div.contest, .entry-body"):
        name_el = item.select_one("h2, h3, h4, .title, strong")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name or len(name) < 5:
            continue

        link_el = item.select_one("a[href]")
        entry_url = abs_url(link_el["href"] if link_el else "", url)

        deadline_text = ""
        prize = None
        for el in item.select("p, span, div, time"):
            t = el.get_text(strip=True)
            if not deadline_text and any(kw in t for kw in ("締切", "〆切", "応募期間")):
                deadline_text = t[:80]
            if not prize and any(kw in t for kw in ("万円", "賞金")):
                prize = extract_prize(t)

        contests.append(make_entry(name, source["name"], url, entry_url,
                                   deadline_text, prize))

    print(f"  [山と渓谷] {len(contests)} entries")
    return contests


# Register custom parsers by source name.
# Sources not listed here fall through to scrape_generic().
CUSTOM_PARSERS: dict = {
    "Picru":       parse_picru,
    "フォトセカイ": parse_photosekai,
    "登竜門":      parse_japandesign,
    "山と渓谷":    parse_yamakei,
}


# =============================================================================
# Post-processing
# =============================================================================

def deduplicate(contests: list[dict]) -> list[dict]:
    """
    Merge entries with the same contest name (case-insensitive, whitespace-normalised).
    When the same contest appears on multiple sites, the sources lists are combined
    so the dashboard can show all source links side by side.
    The entry with the better data (non-null deadline, prize) is kept as the base.
    """
    seen: dict = {}  # normalised_name → index in result
    result: list[dict] = []

    for c in contests:
        key = re.sub(r"\s+", "", c["name"]).lower()
        if key in seen:
            existing = result[seen[key]]
            # Merge source list — avoid duplicates by name
            existing_names = {s["name"] for s in existing["sources"]}
            for s in c["sources"]:
                if s["name"] not in existing_names:
                    existing["sources"].append(s)
            # Fill in missing fields from this entry
            if not existing.get("deadline") and c.get("deadline"):
                existing["deadline"]     = c["deadline"]
                existing["deadlineText"] = c["deadlineText"]
            if not existing.get("prize") and c.get("prize"):
                existing["prize"] = c["prize"]
        else:
            seen[key] = len(result)
            result.append(c)

    return result


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