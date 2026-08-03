"""
Pure helpers: no browser dependency. Filename/date parsing, PDF link
extraction from raw HTML, and scoring/ranking of candidate links to
pick out the latest factsheet(s).
"""

import posixpath
import re
from datetime import datetime
from ssl import SSLError
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from practise import PAGE_TIMEOUT_MS

from .config import (
    DEBUG,
    DOWNLOAD_TIMEOUT_S,
    HEADERS,
    HEADLESS,
    MONTH_NAMES_PATTERN,
    MONTHS,
)


def safe_filename(amc_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", amc_name)


def find_pdf_links(page_url: str, html: str):
    """
    Returns (links, contexts):
      links    -- plain list of PDF URLs, same shape as before
      contexts -- dict of {url: nearby text}, since some sites put the
                  'factsheet' categorization in a sibling element (e.g.
                  a file-size description span) rather than in the link
                  text or URL itself -- HSBC's newer files are named
                  'the-asset-june-2026.pdf' with no 'factsheet' in the
                  URL at all; the word only appears in a sibling span.
    """
    soup = BeautifulSoup(html, "html.parser")
    links = []
    contexts = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ".pdf" in href.lower():
            full_url = urljoin(page_url, href)
            links.append(full_url)
            parent = a.find_parent()
            context = parent.get_text(" ", strip=True) if parent else ""
            contexts[full_url] = context[:300]
    return links, contexts


def _basename_for_date_search(value: str) -> str:
    """
    Restrict date-pattern matching to the filename portion of a URL.
    Also supports sites (e.g. NJ Mutual Fund) that store the filename
    in the ?file= query parameter.
    """
    if "://" in value or value.startswith("/"):
        parsed = urlparse(value)

        # Special case: filename stored in ?file=
        params = parse_qs(parsed.query)
        if "file" in params:
            filename = params["file"][0]
            if filename.lower().endswith(".pdf"):
                return unquote(filename)

        # Normal case: filename is in the URL path
        return unquote(posixpath.basename(parsed.path) or value)

    return value


def extract_date(value: str):
    """
    Extract (year, month) from a filename or surrounding text.

    Supported examples:

        July-2026
        July_2026
        July 2026
        July-31-2026
        June 30, 2026

        Jun26
        May25
        March2025

        07-2026
        07_2026

        2026-07
        2026_07

        20260723115930

        Factsheet as on 30-06-2026

        2026

    Returns:
        (year, month)
        or None
    """

    text = _basename_for_date_search(value).lower()

    current_year = datetime.now().year  # noqa: DTZ005

    def valid(year):
        return 2000 <= year <= current_year + 1

    # --------------------------------------------------
    # Month name + optional day + year
    #
    # July-2026
    # July_2026
    # July 2026
    # July-31-2026
    # June 30, 2026
    # March2025
    # --------------------------------------------------

    m = re.search(
        rf"({MONTH_NAMES_PATTERN})[-_ ]?(?:\d{{1,2}}[-_ ,/]*)?(\d{{4}})",
        text,
    )

    if m:
        year = int(m.group(2))

        if valid(year):
            return (year, MONTHS[m.group(1)])

    # --------------------------------------------------
    # Month + 2-digit year
    #
    # Jun26
    # May25
    # --------------------------------------------------

    m = re.search(
        rf"({MONTH_NAMES_PATTERN})[-_ ]?(\d{{2}})(?!\d)",
        text,
    )

    if m:
        year = 2000 + int(m.group(2))

        if valid(year):
            return (year, MONTHS[m.group(1)])

    # --------------------------------------------------
    # Numeric month-year
    #
    # 07-2026
    # 07_2026
    # --------------------------------------------------

    m = re.search(
        r"(?<!\d)(0[1-9]|1[0-2])[-_/](20\d{2})(?!\d)",
        text,
    )

    if m:
        year = int(m.group(2))

        if valid(year):
            return (year, int(m.group(1)))

    # --------------------------------------------------
    # Year-month
    #
    # 2026-07
    # 2026_07
    # --------------------------------------------------

    m = re.search(
        r"(20\d{2})[-_/](0[1-9]|1[0-2])(?!\d)",
        text,
    )

    if m:
        year = int(m.group(1))

        if valid(year):
            return (year, int(m.group(2)))

    # --------------------------------------------------
    # DD-MM-YYYY
    #
    # Factsheet as on 30-06-2026
    # 31/05/2026
    # --------------------------------------------------

    m = re.search(
        r"\b\d{1,2}[-/](0[1-9]|1[0-2])[-/](20\d{2})\b",
        text,
    )

    if m:
        year = int(m.group(2))
        month = int(m.group(1))

        if valid(year):
            return (year, month)

    # --------------------------------------------------
    # Compact timestamp
    #
    # 20260723115930
    # --------------------------------------------------

    m = re.search(
        r"(20\d{2})(0[1-9]|1[0-2])\d{2}(?:\d{6})?",
        text,
    )

    if m:
        year = int(m.group(1))
        month = int(m.group(2))

        if valid(year):
            return (year, month)

    # --------------------------------------------------
    # Year only
    #
    # Factsheet_2026.pdf
    # --------------------------------------------------

    m = re.search(r"\b(20\d{2})\b", text)

    if m:
        year = int(m.group(1))

        if valid(year):
            return (year, 1)

    return None


def keyword_score(link: str) -> int:
    lower = link.lower()
    score = 0

    if any(
        k in lower
        for k in [
            "factsheet",
            "fact-sheet",
            "fact_sheet",
            "facts",
            "active_fund",
            "passive_fund",
            "active fund",
            "passive fund",
            "taurus_times",
            "taurus times",
            "fact",
        ]
    ):
        score += 3

    if "regular" in lower:
        score += 1

    return score


def pick_latest_links(pdf_links, link_contexts=None):
    """
    Return ALL relevant factsheet PDFs belonging to the latest month.

    Example

    July 2026 Equity
    July 2026 Debt
    July 2026 Hybrid

    -> returns all three.
    """
    link_contexts = link_contexts or {}
    pdf_links = list(dict.fromkeys(pdf_links))

    all_scored = []

    for link in pdf_links:
        context_text = link_contexts.get(link, "")
        date = extract_date(link)
        if date is None and context_text:
            date = extract_date(context_text)
        score = keyword_score(link + " " + context_text)
        all_scored.append((link, date, score))

    if DEBUG:
        print("\nAll PDF links:")
        for link, date, score in all_scored:
            print(f"{date=}  {score=}  {link}")

    relevant = [(link, date, score) for link, date, score in all_scored if score > 0]

    if not relevant:
        return []

    dated = [x for x in relevant if x[1] is not None]

    if dated:
        latest_date = max(x[1] for x in dated)

        latest_links = [x for x in relevant if x[1] == latest_date]
    else:
        latest_links = relevant

    latest_links.sort(key=lambda x: x[2], reverse=True)

    if DEBUG:
        print(f"\nLatest month = {latest_links[0][1]}")
        print("Selected PDFs:")
        for link, _, score in latest_links:
            print(f"score={score} -> {link}")

    return [x[0] for x in latest_links]


def download_pdf_bytes(
    pdf_url: str, referer_url: str, timeout=DOWNLOAD_TIMEOUT_S
) -> bytes:
    """
    Some sites (e.g. Edelweiss) 403 direct/hotlinked file requests
    unless the request carries a Referer header matching their own
    site, or real session cookies. Try the cheap path first (plain
    requests + Referer, same SSL-retry behavior as before); only fall
    back to a real Playwright browser session -- which loads the
    referring page first to pick up real cookies -- if that's still
    blocked. Existing sites that already worked with plain requests
    are completely unaffected; this only adds a fallback for the new
    403 case, it doesn't change anything about the working path.
    """
    headers_with_referer = {**HEADERS, "Referer": referer_url}

    try:
        resp = requests.get(pdf_url, headers=headers_with_referer, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except SSLError:
        print("SSL verification failed. Retrying without certificate verification...")
        resp = requests.get(
            pdf_url, headers=headers_with_referer, timeout=timeout, verify=False
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"Plain download failed ({e}), retrying via real browser session...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        try:
            page.goto(
                referer_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded"
            )
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Navigate the PAGE itself to the PDF, not context.request --
        # a real navigation sends the same Sec-Fetch-* / Referer chain
        # a genuine click would, which some WAFs specifically check
        # and an API-style fetch (even from within a browser context)
        # does not replicate.
        try:
            response = page.goto(pdf_url, timeout=PAGE_TIMEOUT_MS)
            status = response.status if response else None
            content = response.body() if response and status == 200 else None
        except Exception as e:
            status = None
            content = None
            print(f"Direct navigation to PDF also failed: {e}")

        browser.close()

    if content is None:
        raise Exception(  # noqa: TRY002
            f"Still blocked (status {status}) even via browser session: {pdf_url}"
        )

    return content
