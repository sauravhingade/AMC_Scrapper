import os
import posixpath
import re
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from requests.exceptions import SSLError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

amc_with_genric_fallback = {
    "360 one mutual fund",
    "axis mutual fund",
    "choice mutual fund",
    "mahindra manulife mutual fund",
    "pgim india mutual fund",
    "trust mutual fund",
    "the wealth company mutual fund",
}
amc_with_download_fallback = {
    "iti mutual fund",
}
amc_need_month_selection_with_antnative = {"jio blackrock mutual fund"}
amc_need_month_selection_with_selectnative = {"navi mutual fund"}
amc_need_month_selection_with_date_picker = {"sundaram mutual fund"}
amc_need_date_selection = {"kotak mahindra mutual fund"}
amc_need_month_selection_tata = {"tata mutual fund"}
amc_need_year_selection = {
    "taurus mutual fund",
}


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DOWNLOAD_DIR = "downloads"
PAGE_TIMEOUT_MS = 30000
DOWNLOAD_TIMEOUT_S = 20
DEBUG = True  # set False to silence link-scoring logs

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

months = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

MONTH_ALIASES = {
    "jan": ["jan", "january"],
    "feb": ["feb", "february"],
    "mar": ["mar", "march"],
    "apr": ["apr", "april"],
    "may": ["may"],
    "jun": ["jun", "june"],
    "jul": ["jul", "july"],
    "aug": ["aug", "august"],
    "sep": ["sep", "sept", "september"],
    "oct": ["oct", "october"],
    "nov": ["nov", "november"],
    "dec": ["dec", "december"],
}

month_pattern = re.compile(
    r"\b("
    r"jan(?:uary)?|"
    r"feb(?:ruary)?|"
    r"mar(?:ch)?|"
    r"apr(?:il)?|"
    r"may|"
    r"jun(?:e)?|"
    r"jul(?:y)?|"
    r"aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|"
    r"nov(?:ember)?|"
    r"dec(?:ember)?"
    r")\b",
    re.I,
)
MONTH_NAMES_PATTERN = "|".join(MONTHS.keys())


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

    current_year = datetime.now().year

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


def run_interaction_steps(page, steps):
    """
    Generic interaction runner.

    Expensive operations are opt-in via step flags:
      - disable_chat : temporarily disables chat/widget interception
      - verify_click : prints class after click for debugging
      - settle_ms    : custom wait after click
      - wait_for     : wait for a selector to appear
      - wait_for_pdf : wait until a PDF link appears
    """

    for step in steps:
        action = step["action"]

        try:
            if action == "click":
                locator = page.locator(step["selector"]).nth(step.get("nth", 0))

                if locator.count() == 0:
                    if DEBUG:
                        print(f"Skipping click, selector not found: {step['selector']}")
                    continue

                try:
                    locator.wait_for(state="visible", timeout=3000)
                except Exception:
                    if DEBUG:
                        print(f"Element never became visible: {step['selector']}")
                    continue

                # Optional: disable chat/widget overlays
                if step.get("disable_chat"):
                    try:
                        page.locator(
                            "[class*='chat' i],[class*='bot' i],[class*='widget' i]"
                        ).evaluate_all(
                            "(els) => els.forEach(el => el.style.pointerEvents='none')"
                        )
                    except Exception:
                        pass

                locator.click(timeout=5000)

                # Optional: verify click
                if DEBUG and step.get("verify_click"):
                    try:
                        cls = locator.get_attribute("class") or ""
                        parent_cls = (
                            locator.locator("xpath=..").get_attribute("class") or ""
                        )

                        print(f"Clicked: {step['selector']}")
                        print(f"   element class : {cls}")
                        print(f"   parent class  : {parent_cls}")

                    except Exception:
                        print(f"Clicked: {step['selector']}")

                page.wait_for_timeout(step.get("settle_ms", 300))

                if step.get("wait_for"):
                    page.locator(step["wait_for"]).wait_for(
                        state="visible",
                        timeout=8000,
                    )

                if step.get("wait_for_pdf"):
                    page.locator("a[href$='.pdf']").first.wait_for(
                        state="attached",
                        timeout=8000,
                    )

            elif action == "select":
                locator = page.locator(step["selector"]).first

                locator.wait_for(state="visible", timeout=3000)

                locator.select_option(step["value"])

                page.wait_for_timeout(step.get("settle_ms", 300))

            elif action == "fill":
                locator = page.locator(step["selector"]).first

                locator.wait_for(state="visible", timeout=3000)

                locator.fill(step["value"])

                page.wait_for_timeout(step.get("settle_ms", 200))

            elif action == "wait":
                page.wait_for_timeout(step.get("ms", 1000))

            elif action == "select_latest_option":
                select_latest_option_interaction(page, step)

        except Exception as e:
            if DEBUG:
                print(f"Interaction failed: {step}")
                print(e)


def get_rendered_html(amc_name: str, page_url: str, interaction_steps=None) -> str:
    """
    Load the page. If a site-specific recipe (interaction_steps) is
    given, run it exactly as configured. Otherwise fall back to a
    best-effort guess: try clicking anything that looks like a
    "Factsheet" tab, since that's the most common pattern we've seen
    so far (e.g. 360 ONE).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        try:
            page.goto(page_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # let JS finish rendering after DOM is ready
        except Exception as e:
            if DEBUG:
                print(f"page.goto did not fully settle, proceeding anyway: {e}")
        dismiss_popups(page)
        if interaction_steps:
            if DEBUG:
                print(
                    f"Running {len(interaction_steps)} configured interaction step(s)"
                )
            run_interaction_steps(page, interaction_steps)

        if amc_name.lower() in amc_need_month_selection_with_antnative:
            print(f"{amc_name} need latest month selection")
            select_latest_available_month_JIO(page)
        elif amc_name.lower() in amc_need_month_selection_with_selectnative:
            print(f"{amc_name} need latest month selection for simple native page")
            select_latest_available_month_NAVI(page)
        elif amc_name.lower() in amc_need_month_selection_tata:
            print(f"{amc_name} need latest month selection for simple native page")
            select_latest_available_month_Tata(page)
        elif amc_name.lower() in amc_need_year_selection:
            print(f"{amc_name} need latest year selection")
            select_latest_available_year_taurus(page)

        html = page.content()
        browser.close()
        return html


def fallback_get_latest_pdf_links_new(page_url: str, interaction_steps=None):
    """
    Fallback for sites where factsheets don't expose PDF hrefs.

    Returns:
        (pdf_urls, link_contexts)

        pdf_urls      -> list[str]
        link_contexts -> {pdf_url: surrounding text}
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context = browser.new_context(user_agent=HEADERS["User-Agent"])

        page = context.new_page()

        try:
            page.goto(
                page_url,
                timeout=PAGE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            dismiss_popups(page)
        except Exception:
            pass

        page.wait_for_timeout(2000)

        if interaction_steps:
            print("Running interaction steps...")
            run_interaction_steps(page, interaction_steps)

        page.wait_for_timeout(2000)
        dismiss_popups(page)

        print("\nDetecting latest month...")

        latest_month = None
        latest_date = None

        factsheet_section = page.locator("#pdf-factsheets")

        # -------------------------------------------------
        # Pass 1 : Find the latest complete date if present
        # e.g.
        # Factsheet as on 30-06-2026
        # Factsheet June 2026
        # Factsheet Jun26
        # -------------------------------------------------

        all_text = page.locator("body *")

        for i in range(all_text.count()):
            try:
                text = all_text.nth(i).inner_text().strip()

                if not text or len(text) > 120:
                    continue

                if "factsheet" not in text.lower():
                    continue

                d = extract_date(text)

                if d and (latest_date is None or d > latest_date):
                    latest_date = d

            except Exception:
                pass

        if latest_date:
            month_no = latest_date[1]

            for short, num in MONTHS.items():
                if len(short) == 3 and num == month_no:
                    latest_month = short
                    break

        # -------------------------------------------------
        # Pass 2 : Existing logic (kept exactly as before)
        # -------------------------------------------------

        if latest_month is None:
            # -------------------------------------------------
            # Trust Mutual Fund
            # -------------------------------------------------

            if factsheet_section.count() > 0:
                buttons = factsheet_section.locator("button, a")

                for i in range(buttons.count()):
                    try:
                        text = buttons.nth(i).inner_text().strip()

                        m = month_pattern.search(text)

                        if not m:
                            continue

                        raw = m.group(1).lower()

                        for short, aliases in MONTH_ALIASES.items():
                            if raw in aliases:
                                latest_month = short
                                break

                        if latest_month:
                            break

                    except Exception:
                        pass

            # -------------------------------------------------
            # Generic
            # -------------------------------------------------

            else:
                for i in range(all_text.count()):
                    try:
                        text = all_text.nth(i).inner_text().strip()

                        if not text or len(text) > 60:
                            continue

                        if "factsheet" not in text.lower():
                            continue

                        m = month_pattern.search(text)

                        if not m:
                            continue

                        raw = m.group(1).lower()

                        for short, aliases in MONTH_ALIASES.items():
                            if raw in aliases:
                                latest_month = short
                                break

                        if latest_month:
                            break

                    except Exception:
                        pass

        if latest_month is None:
            browser.close()
            return [], {}

        print(f"Latest month detected: {latest_month}")
        print(f"Latest date detected: {latest_date}")

        aliases = MONTH_ALIASES[latest_month]
        month_regex = "|".join(aliases)

        print("\nSearching clickable factsheet entries...")

        # -------------------------------------------------
        # Candidate selection
        # -------------------------------------------------
        if factsheet_section.count() > 0:
            candidates = factsheet_section.locator("button, a").filter(
                has_text=re.compile(
                    rf"\b(?:{month_regex})\b",
                    re.I,
                )
            )

        else:
            candidates = page.locator("a, button, span, label, p, img").filter(
                has_text=re.compile(
                    rf"(factsheet.*(?:{month_regex})|(?:{month_regex}).*factsheet)",
                    re.I,
                )
            )

            if candidates.count() == 0 and latest_date:
                year, month = latest_date

                numeric_pattern = (
                    rf"factsheet[\s\S]*("
                    rf"{month:02d}[/-]{year}"
                    rf"|"
                    rf"[0-3]?\d[/-]{month:02d}[/-]{year}"
                    rf")"
                )

                # text_pattern = (
                #     rf"({numeric_pattern})"
                #     rf"|"
                #     rf"(factsheet.*(?:{month_regex})|(?:{month_regex}).*factsheet)"
                # )

                candidates = page.locator("a, button, span, label, p, img").filter(
                    has_text=re.compile(numeric_pattern, re.I)
                )

        count = candidates.count()

        print(f"Found {count} clickable candidates")

        urls = []
        link_contexts = {}

        for i in range(count):
            locator = candidates.nth(i)

            try:
                context_text = locator.inner_text().strip()

                print(f"\nCandidate {i + 1}: {context_text}")

                locator.scroll_into_view_if_needed()

                click_target = locator

                try:
                    for level in range(1, 3):
                        container = locator.locator(f"xpath=ancestor::div[{level}]")

                        download = container.locator(
                            """
                            img[src*='download'],
                            img[alt*=download i],
                            button,
                            a[download],
                            a[href$='.pdf'],
                            [aria-label*=download i],
                            [title*=download i]
                            """
                        ).first

                        if download.count():
                            click_target = download
                            break

                except Exception:
                    pass

                # ---------------- Popup ----------------

                try:
                    with context.expect_page(timeout=5000) as popup:
                        click_target.click()

                    pdf_page = popup.value

                    pdf_page.wait_for_load_state()

                    url = pdf_page.url

                    print("Popup URL:", url)

                    if url.lower().endswith(".pdf"):
                        urls.append(url)
                        link_contexts[url] = context_text

                    pdf_page.close()

                    continue

                except Exception:
                    pass

                # ---------------- Same tab ----------------

                old = page.url

                click_target.click()

                page.wait_for_timeout(2000)

                if page.url != old and page.url.lower().endswith(".pdf"):
                    print("Same tab:", page.url)

                    urls.append(page.url)
                    link_contexts[page.url] = context_text

            except Exception as e:
                print("Skipped:", e)

        browser.close()

        urls = list(dict.fromkeys(urls))

        print("\nCollected PDF URLs")

        for u in urls:
            print(u)
            print("Context:", link_contexts.get(u, ""))

        return urls, link_contexts


def dismiss_popups(page, attempts=6, wait_between_ms=1200):
    """
    Handles two separate issues, in priority order, across multiple
    polling rounds since popups can appear in random order with delays:

      1. Site-specific EXACT close buttons, matched precisely by their
         real aria-label -- e.g. Mahindra Manulife's welcome modal has
         an invisible overlapping "Register or login" button sitting on
         top of part of it, which a loose generic selector can hit by
         accident instead of the real close button, triggering a login
         flow rather than dismissing the popup.
      2. Legal disclaimer gates requiring a SPECIFIC choice (e.g. the
         US/Canada residency confirmation) -- must click the correct
         wording, never a generic close, since the wrong choice or an
         X redirects to a different page entirely (confirmed manually).
      3. Generic promotional modals, closed via a best-effort list of
         close-button patterns, as a last resort.
    """
    exact_selectors = [
        "button[aria-label='Close welcome message']",
    ]
    priority_selectors = [
        "text=/I AM NOT A US PERSON.*CANADA/i",
        # Tata disclaimer
        "button:has-text('Continue')",
        # Other common disclaimer buttons
        "button:has-text('I Agree')",
        "button:has-text('Agree')",
        "button:has-text('Accept')",
    ]
    close_selectors = [
        "button[aria-label*='close' i]",
        "[class*='close' i]",
        "#portal-root button",
        "[class*='modal'] button",
        "svg[class*='close' i]",
        "text=/^(india|continue|accept|ok|got it)$/i",
    ]

    dismissed_any = False
    for attempt in range(attempts):
        clicked_this_round = False
        for selector in exact_selectors + priority_selectors + close_selectors:
            try:
                btn = page.locator(selector).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=2000)
                    page.wait_for_timeout(600)
                    print(f"Dismissed popup via: {selector} (round {attempt + 1})")
                    clicked_this_round = True
                    dismissed_any = True
                    break  # re-scan from the top -- a NEW modal may have appeared
            except Exception:
                continue
        if not clicked_this_round:
            page.wait_for_timeout(wait_between_ms)

    return dismissed_any


def fallback_direct_download(page_url: str, amc_name: str, interaction_steps=None):
    """
    ITI-specific fallback.

    ITI does NOT expose PDF URLs.
    Clicking the download icon triggers a browser download.

    Returns:
        list[str] -> downloaded file paths
    """

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            accept_downloads=True,
        )

        page = context.new_page()

        try:
            page.goto(
                page_url,
                timeout=PAGE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            dismiss_popups(page)

        except Exception:
            pass

        page.wait_for_timeout(3000)

        if interaction_steps:
            run_interaction_steps(page, interaction_steps)

        page.wait_for_timeout(2000)

        # dismiss_overlays(page)

        print("\nScanning factsheet rows...")

        rows = page.locator("div.row.file-list-section")

        latest_date = None
        latest_rows = []

        seen = set()

        for i in range(rows.count()):
            row = rows.nth(i)

            try:
                text = row.inner_text().strip()

                if "factsheet" not in text.lower():
                    continue

                if text in seen:
                    continue

                seen.add(text)

                date = extract_date(text)

                if date is None:
                    continue

                print(text, "->", date)

                if latest_date is None or date > latest_date:
                    latest_date = date
                    latest_rows = [row]

                elif date == latest_date:
                    latest_rows.append(row)

            except Exception:
                pass

        if latest_date is None:
            browser.close()
            return []

        print(f"\nLatest factsheet found: {latest_date}")

        saved_files = []

        for idx, row in enumerate(latest_rows, start=1):
            print(f"Downloading latest factsheet #{idx}")

            download_btn = row.locator(
                "a.file-download-link, img[src*='download']"
            ).first

            try:
                with page.expect_download(timeout=10000) as download_info:
                    download_btn.click(force=True)

                download = download_info.value

                filename = f"{safe_filename(amc_name)}_{download.suggested_filename}"

                filepath = os.path.join(DOWNLOAD_DIR, filename)

                download.save_as(filepath)

                print("Saved:", filepath)

                saved_files.append(filepath)

            except Exception as e:
                print("Download failed:", e)

        browser.close()

        return saved_files


def select_latest_option_interaction(page, step):
    """
    Opens a dropdown and selects the latest option.

    Supports:
        value_type = month
        value_type = year
    """

    try:
        dropdown_selector = step["dropdown_selector"]
        option_selector = step["option_selector"]
        value_type = step.get("value_type", "month")

        # Open dropdown
        page.locator(dropdown_selector).click()
        page.wait_for_timeout(500)

        options = page.locator(option_selector)

        if options.count() == 0:
            print("Dropdown has no options.")
            return

        best_idx = None
        best_value = -1

        for i in range(options.count()):
            text = options.nth(i).inner_text().strip().lower()

            if value_type == "month":
                value = MONTHS.get(text)

            elif value_type == "year":
                try:
                    value = int(re.search(r"\d{4}", text).group())
                except Exception:
                    value = None

            else:
                value = None

            if value is None:
                continue

            if value > best_value:
                best_value = value
                best_idx = i

        if best_idx is None:
            print("No valid option found.")
            return

        selected = options.nth(best_idx).inner_text().strip()

        print(f"Selecting latest {value_type}: {selected}")

        options.nth(best_idx).click()

        page.wait_for_timeout(1500)

    except TimeoutError:
        print("Dropdown selection timeout.")

    except Exception as e:
        print("Failed selecting latest option:", e)


def select_latest_available_month_NAVI(page):
    """
    NAVI helper.

    Starts from the currently selected month and keeps moving one month
    backwards until a PDF appears.
    """

    month_dropdown = page.locator("select.month")

    current = month_dropdown.input_value().strip()
    current_index = months.index(current)

    for idx in range(current_index, -1, -1):
        month = months[idx]
        print(f"\nChecking {month}...")

        # Select month
        month_dropdown.select_option(label=month)

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)

        # PDF appeared?
        pdfs = page.locator("a[href$='.pdf']")

        if pdfs.count() > 0:
            print(f"Found PDF for {month}")
            return month

        print(f"No PDF for {month}")

    return None


def select_latest_available_month_JIO(page):
    """
    Jio BlackRock helper.

    Starts from the currently selected month and keeps moving one month
    backwards until a PDF link appears on the page.
    """

    month_box = page.locator("div.ant-select").nth(1)

    current = month_box.locator(".ant-select-selection-item").inner_text().strip()

    current_index = months.index(current)

    for idx in range(current_index, -1, -1):
        month = months[idx]
        print(f"\nChecking {month}...")

        # Wait for page after previous selection
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)

        # Any PDF available?
        if page.locator("a[href$='.pdf']").count() > 0:
            print(f"Found PDF for {month}")
            return month

        # Last month reached
        if idx == 0:
            break

        print(f"No PDF for {month}, selecting previous month...")

        # Open dropdown
        month_box.click()

        page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)").wait_for(
            state="visible", timeout=5000
        )

        # Move one month up
        page.keyboard.press("ArrowUp")
        page.keyboard.press("Enter")

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)

    return None


def prepare_kotak_latest_factsheet(page_url: str, interaction_steps=None):
    """
    Kotak Mutual Fund

    Returns:
        (pdf_links, link_contexts)

    Example:
        (
            ["https://....pdf"],
            {
                "https://....pdf": "Factsheet July 2026"
            }
        )
    """

    MONTH_ORDER = [
        "December",
        "November",
        "October",
        "September",
        "August",
        "July",
        "June",
        "May",
        "April",
        "March",
        "February",
        "January",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()

        try:
            page.goto(page_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            print(page.title())
            print(page.url)
            page.wait_for_timeout(2500)
        except Exception as e:
            if DEBUG:
                print(f"page.goto did not fully settle, proceeding anyway: {e}")

        if interaction_steps:
            run_interaction_steps(page, interaction_steps)

        page.wait_for_timeout(2000)
        dismiss_popups(page)

        factsheet = (
            page.locator("div.row.justify-content-between")
            .filter(has=page.locator("div.subHeaderTitle", has_text="Factsheet"))
            .first
        )

        if factsheet.count() == 0:
            browser.close()
            return [], {}

        year_select = factsheet.locator("select").first

        years = []

        options = year_select.locator("option")

        for i in range(options.count()):
            txt = options.nth(i).inner_text().strip()

            if txt.isdigit():
                years.append(int(txt))

        if not years:
            browser.close()
            return [], {}

        latest_year = str(max(years))

        print(f"Selecting year : {latest_year}")

        year_select.select_option(label=latest_year)

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)

        available = {}

        buttons = factsheet.locator("button.btn-months")

        for i in range(buttons.count()):
            btn = buttons.nth(i)

            try:
                month = btn.inner_text().strip()

                if month:
                    available[month] = btn

            except Exception:
                pass

        print("Available months :", list(available.keys()))

        for month in MONTH_ORDER:
            if month not in available:
                continue

            print(f"Selecting month : {month}")

            btn = available[month]

            old_url = page.url

            btn.scroll_into_view_if_needed()
            btn.click(force=True)

            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            context_text = f"Factsheet {month} {latest_year}"

            # Case 1: Same tab PDF
            if page.url != old_url and page.url.lower().endswith(".pdf"):
                pdf = page.url

                print("PDF :", pdf)

                browser.close()

                return [pdf], {pdf: context_text}

            # Case 2: PDF link rendered
            pdf_link = page.locator("a[href$='.pdf']").first

            if pdf_link.count():
                href = pdf_link.get_attribute("href")

                if href:
                    pdf = urljoin(page.url, href)

                    print("PDF :", pdf)

                    browser.close()

                    return [pdf], {pdf: context_text}

            break

        browser.close()

        return [], {}


def get_sundaram_latest_pdf_links(
    page_url: str,
    interaction_steps=None,
):
    """
    Returns latest available Sundaram factsheet PDF URL(s).

    Returns:
        [
            "https://.....pdf"
        ]
    """

    pdf_urls = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context = browser.new_context(user_agent=HEADERS["User-Agent"])

        page = context.new_page()

        try:
            page.goto(
                page_url,
                timeout=PAGE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )

            print(page.title())
            print(page.url)

            page.wait_for_timeout(2500)

        except Exception as e:
            if DEBUG:
                print(f"page.goto did not fully settle: {e}")

        if interaction_steps:
            run_interaction_steps(page, interaction_steps)

        page.wait_for_timeout(2000)
        # dismiss_popups(page)

        while True:
            # --------------------------
            # Open month picker
            # --------------------------

            page.locator("#dt_InfoDoc").click()
            page.wait_for_timeout(1000)

            months = page.locator(".datepicker-months span.month:not(.disabled)")

            count = months.count()

            print(f"\nEnabled months : {count}")

            if count == 0:
                break

            found = False

            # Latest -> Oldest
            for idx in range(count - 1, -1, -1):
                # Calendar closes after each click
                if idx != count - 1:
                    page.locator("#dt_InfoDoc").click()
                    page.wait_for_timeout(700)

                    months = page.locator(
                        ".datepicker-months span.month:not(.disabled)"
                    )

                month_name = months.nth(idx).inner_text().strip()

                print("=" * 70)
                print(f"Trying month : {month_name}")
                print("=" * 70)

                months.nth(idx).click()

                page.wait_for_timeout(1000)

                # Debug selected value
                try:
                    print("Selected value :", page.locator("#dt_InfoDoc").input_value())
                except:
                    pass

                before = len(page.context.pages)

                page.locator("#btn_download").click()

                page.wait_for_timeout(3000)

                after = len(page.context.pages)

                print(f"Pages before : {before}")
                print(f"Pages after  : {after}")

                # --------------------------
                # PDF opened
                # --------------------------

                if after > before:
                    popup = page.context.pages[-1]

                    popup.wait_for_load_state()

                    print("Popup URL :", popup.url)

                    if popup.url.lower().endswith(".pdf"):
                        pdf_urls.append(popup.url)

                    popup.close()

                    found = True
                    break

                # --------------------------
                # Error toast (debug)
                # --------------------------

                try:
                    toast = page.locator("div.toast, div.alert, .toast-message")

                    if toast.count():
                        print("Toast :", toast.first.inner_text())

                except:
                    pass

                print(f"No PDF for {month_name}")

            if found:
                break

            break

        browser.close()

    return pdf_urls


def select_latest_available_month_Tata(page):
    """
    Tata Mutual Fund helper.

    Starts from the currently selected month and keeps moving
    backwards until a PDF link appears after clicking Submit.

    Returns:
        Month name if a PDF is found, otherwise None.
    """

    month_dropdown = page.locator("div[aria-labelledby='select-month-label']")
    submit_btn = page.locator("button:has-text('Submit')")

    # Current selected month
    current_month = month_dropdown.inner_text().strip()

    print(f"Current month: {current_month}")

    current_index = months.index(current_month)

    for idx in range(current_index, -1, -1):
        month = months[idx]

        print(f"\nChecking {month}")

        # Select previous month (skip for first/current month)
        if idx != current_index:
            month_dropdown.click()

            page.wait_for_selector(
                "#select-month-listbox",
                state="visible",
                timeout=5000,
            )

            page.locator(f"#select-month-listbox button:has-text('{month}')").click()

            page.wait_for_timeout(500)

        # Submit selection
        submit_btn.click()

        # React updates the results without page navigation
        page.wait_for_timeout(500)

        # Check for rendered PDF links
        factsheet = page.locator("a[aria-label*='Factsheet']")

        if factsheet.count():
            href = factsheet.first.get_attribute("href")
            print(href)
            return month

        print(f"No PDF for {month}")

    return None


def select_latest_available_year_taurus(page):
    """
    Select the latest year that actually displays factsheet PDFs.
    """

    year_dropdown = page.locator("select[id^='edit-field-factsheet-item-target-id']")

    years = sorted(
        [
            int(opt.inner_text().strip())
            for opt in year_dropdown.locator("option").all()
            if opt.inner_text().strip().isdigit()
        ],
        reverse=True,
    )

    print("Available years:", years)

    for year in years:
        print(f"\nChecking {year}")

        year_dropdown.select_option(label=str(year))

        page.wait_for_timeout(1200)

        # Factsheet entries only
        factsheet_links = page.locator(
            "div.download-table a[href*='Taurus_Times_'][href$='.pdf']"
        )

        if factsheet_links.count() > 0:
            print(f"Found {factsheet_links.count()} factsheet(s) for {year}")
            return str(year)

        print("No factsheets")

    return None


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
        browser = p.chromium.launch(headless=False)
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
        raise Exception(
            f"Still blocked (status {status}) even via browser session: {pdf_url}"
        )

    return content


def download_factsheet(amc_name: str, page_url: str, interaction_steps=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print(f"amc name : {amc_name}")

    # -----------------------------------------
    # Determine latest PDF links
    # -----------------------------------------
    if amc_name.lower() in amc_with_download_fallback:
        if DEBUG:
            print(f"Using generic direct download fallback for {amc_name}")

        return fallback_direct_download(
            page_url,
            amc_name,
            interaction_steps,
        )
    elif amc_name.lower() in amc_need_date_selection:
        if DEBUG:
            print(f"Using prepare factsheet by date selection{amc_name}")
        # cannot automate kotak as its protected by radware bot detection
        pdf_links, link_contexts = prepare_kotak_latest_factsheet(
            page_url, interaction_steps
        )
        latest_links = pick_latest_links(pdf_links, link_contexts)

    elif amc_name.lower() in amc_with_genric_fallback:
        if DEBUG:
            print(f"Using generic fallback for {amc_name}")

        pdf_links, link_contexts = fallback_get_latest_pdf_links_new(
            page_url,
            interaction_steps,
        )

        latest_links = pick_latest_links(
            pdf_links,
            link_contexts,
        )

    elif amc_name.lower() in amc_need_month_selection_with_date_picker:
        if DEBUG:
            print(f"Using date selection for {amc_name}")
            # cannot automate kotak as its protected by radware bot detection
        pdf_links = get_sundaram_latest_pdf_links(page_url, interaction_steps)
        latest_links = pick_latest_links(pdf_links)

    else:
        if DEBUG:
            print(f"Using rendered HTML extraction for {amc_name}")

        html = get_rendered_html(
            amc_name,
            page_url,
            interaction_steps=interaction_steps,
        )

        pdf_links, link_contexts = find_pdf_links(
            page_url,
            html,
        )

        latest_links = pick_latest_links(
            pdf_links,
            link_contexts,
        )

    if not latest_links:
        raise ValueError("No relevant factsheet PDFs found.")

    # -----------------------------------------
    # Download PDFs
    # -----------------------------------------
    saved_files = []

    for pdf_url in latest_links:
        try:
            content = download_pdf_bytes(pdf_url, referer_url=page_url)
        except Exception as e:
            print(f"Failed to download {pdf_url}: {e}")
            continue

        filename = unquote(
            parse_qs(urlparse(pdf_url).query).get(
                "file", [os.path.basename(urlparse(pdf_url).path)]
            )[0]
        )

        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        filename = f"{safe_filename(amc_name)}_{filename}"

        filepath = os.path.join(
            DOWNLOAD_DIR,
            filename,
        )

        with open(filepath, "wb") as f:
            f.write(content)

        saved_files.append(filepath)

    return saved_files
