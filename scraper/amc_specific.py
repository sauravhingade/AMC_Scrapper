"""
Per-AMC scraping flows. These are the "special case" handlers for AMCs
whose sites don't expose a simple, scrapeable list of factsheet PDFs --
each one encodes the specific clicks/selects that AMC's site needs.

get_rendered_html() is the general entry point used for AMCs that just
need plain HTML rendering plus, for a handful of them, one of the
month/year picker helpers below.
"""

import os
import re
from urllib.parse import urljoin

# import requests
from playwright.sync_api import sync_playwright

from .browser_helpers import dismiss_popups, run_interaction_steps
from .config import (
    DEBUG,
    DOWNLOAD_DIR,
    HEADERS,
    MONTH_ALIASES,
    MONTHS,
    PAGE_TIMEOUT_MS,
    amc_need_month_selection_tata,
    amc_need_month_selection_with_antnative,
    amc_need_month_selection_with_selectnative,
    amc_need_slow_load,
    amc_need_year_selection,
    get_headless,
    month_pattern,
    months,
)
from .utils import download_pdf_bytes, extract_date, safe_filename, wait_for_dom_stable


# Chromium's "new" headless mode ships a real PDF viewer (old headless
# mode does not), which makes new-tab / download detection behave the
# same way it does in headed mode. Reused by every launch() call below.
#
# Takes the ALREADY-RESOLVED headless bool (per-AMC, via get_headless())
# rather than reading the global HEADLESS directly, so headful AMCs
# (see config.AMC_NEED_HEADFUL) get correct launch args too.
def _launch_args(headless: bool):
    return ["--headless=new"] if headless else []


def _capture_download_or_new_page(context, page, click_target, wait_ms=3000):
    """
    Clicks click_target and returns whichever fires first: a real
    Download object, or a new Page (e.g. from target=_blank / window.open).

    Returns (download, new_page) -- exactly one will be non-None, or
    both None if neither fired within wait_ms.

    Defined once outside any loop (rather than redefining on_download /
    on_page inline per-iteration) so the listener closures don't capture
    loop variables -- avoids Ruff B023 and the subtle bug it warns about.
    """
    result = {"download": None, "page": None}

    def on_download(d):
        if result["download"] is None:
            result["download"] = d

    def on_page(p):
        if result["page"] is None:
            result["page"] = p

    context.on("download", on_download)
    context.on("page", on_page)

    try:
        click_target.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        click_target.click(timeout=5000)
        page.wait_for_timeout(wait_ms)
    finally:
        context.remove_listener("download", on_download)
        context.remove_listener("page", on_page)

    return result["download"], result["page"]


def get_rendered_html(amc_name: str, page_url: str, interaction_steps=None) -> str:
    """
    Load the page. If a site-specific recipe (interaction_steps) is
    given, run it exactly as configured. Otherwise fall back to a
    best-effort guess: try clicking anything that looks like a
    "Factsheet" tab, since that's the most common pattern we've seen
    so far (e.g. 360 ONE).
    """
    headless = get_headless(amc_name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_launch_args(headless))
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            accept_downloads=True,
        )
        page = context.new_page()
        try:
            page.goto(page_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # let JS finish rendering after DOM is ready
        except Exception as e:
            if DEBUG:
                print(f"page.goto did not fully settle, proceeding anyway: {e}")
        if amc_name.lower() in amc_need_slow_load:
            wait_for_dom_stable(page, stable_checks=2, poll_ms=700, max_wait_ms=15000)
        else:
            page.wait_for_timeout(2500)

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


def prepare_kotak_latest_factsheet(
    page_url: str,
    interaction_steps=None,
    amc_name: str = "Kotak Mahindra Mutual Fund",
):
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

    headless = get_headless(amc_name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_launch_args(headless))
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            accept_downloads=True,
        )
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
    amc_name: str = "Sundaram Mutual Fund",
):
    """
    Returns latest available Sundaram factsheet PDF URL(s).

    Returns:
        [
            "https://.....pdf"
        ]
    """

    pdf_urls = []

    headless = get_headless(amc_name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_launch_args(headless))

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

            print(page.title())
            print(page.url)

            page.wait_for_timeout(2500)

        except Exception as e:
            if DEBUG:
                print(f"page.goto did not fully settle: {e}")

        if interaction_steps:
            run_interaction_steps(page, interaction_steps)

        # Wait until the Consolidated Factsheet tab is actually active
        try:
            page.locator("#dt_InfoDoc").wait_for(
                state="visible",
                timeout=8000,
            )
        except:  # noqa: E722
            page.wait_for_timeout(2000)
        # dismiss_popups(page)

        while True:
            # --------------------------
            # Open month picker
            # --------------------------

            page.locator("#dt_InfoDoc").click()
            page.wait_for_timeout(1000)

            month_options = page.locator(".datepicker-months span.month:not(.disabled)")

            count = month_options.count()

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

                    month_options = page.locator(
                        ".datepicker-months span.month:not(.disabled)"
                    )

                month_name = month_options.nth(idx).inner_text().strip()

                print("=" * 70)
                print(f"Trying month : {month_name}")
                print("=" * 70)

                month_options.nth(idx).click()

                page.wait_for_timeout(1000)

                # Debug selected value
                try:
                    print("Selected value :", page.locator("#dt_InfoDoc").input_value())
                except Exception:
                    pass

                # --------------------------
                # Click download and catch either a new page (headed
                # mode / new headless) or a "download" event (old
                # headless mode, or sites that force Content-Disposition)
                # --------------------------

                download, new_page = _capture_download_or_new_page(
                    context, page, page.locator("#btn_download")
                )

                if download:
                    url = download.url
                    print("Download URL :", url)

                    if url.lower().endswith(".pdf"):
                        pdf_urls.append(url)

                    found = True

                elif new_page:
                    try:
                        new_page.wait_for_load_state(timeout=5000)
                    except Exception:
                        pass

                    print("Popup URL :", new_page.url)

                    if new_page.url.lower().endswith(".pdf"):
                        pdf_urls.append(new_page.url)

                    new_page.close()

                    found = True

                if found:
                    break

                # --------------------------
                # Error toast (debug)
                # --------------------------

                try:
                    toast = page.locator("div.toast, div.alert, .toast-message")

                    if toast.count():
                        print("Toast :", toast.first.inner_text())

                except Exception:
                    pass

                print(f"No PDF for {month_name}")

            if found:
                break

            break

        browser.close()

    return pdf_urls


def fallback_get_latest_pdf_links_new(
    page_url: str,
    interaction_steps=None,
    amc_name: str | None = None,
):
    """
    Fallback for sites where factsheets don't expose PDF hrefs.

    This is the generic-fallback path (amc_with_genric_fallback in
    config.py), which includes ICICI Prudential -- so amc_name should
    be passed by the caller for the headful override to kick in there.

    Returns:
        (pdf_urls, link_contexts)

        pdf_urls      -> list[str]
        link_contexts -> {pdf_url: surrounding text}
    """

    headless = get_headless(amc_name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_launch_args(headless))

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
                    re.IGNORECASE,
                )
            )

        else:
            candidates = page.locator("a, button, span, label, p, h4, img").filter(
                has_text=re.compile(
                    rf"(factsheet.*(?:{month_regex})|(?:{month_regex}).*factsheet)",
                    re.IGNORECASE,
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

                candidates = page.locator("a, button, span, label, p, img").filter(
                    has_text=re.compile(numeric_pattern, re.IGNORECASE)
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

                # ---------------- Popup / Download (headed + headless safe) ----------------

                popup_opened = False

                for attempt in range(2):
                    try:
                        download, new_page = _capture_download_or_new_page(
                            context, page, click_target
                        )

                        if download:
                            url = download.url
                            print("Download URL:", url)

                            if url.lower().endswith(".pdf"):
                                urls.append(url)
                                link_contexts[url] = context_text

                            popup_opened = True

                        elif new_page:
                            try:
                                new_page.wait_for_load_state(timeout=5000)
                            except Exception:
                                pass

                            url = new_page.url
                            print("Popup URL:", url)

                            if url.lower().endswith(".pdf"):
                                urls.append(url)
                                link_contexts[url] = context_text

                            new_page.close()

                            popup_opened = True

                    except Exception:
                        pass

                    if popup_opened:
                        break

                    if attempt == 0:
                        page.wait_for_timeout(1000)

                if popup_opened:
                    continue

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


def fallback_direct_download(page_url: str, amc_name: str, interaction_steps=None):
    """
    ITI-specific fallback.

    ITI does NOT expose PDF URLs.
    Clicking the download icon triggers a browser download.

    Returns:
        list[str] -> downloaded file paths
    """

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    headless = get_headless(amc_name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_launch_args(headless))

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


def fallback_direct_download_bandhan(
    page_url: str,
    amc_name: str,
    interaction_steps=None,
):
    """
    Bandhan Mutual Fund.

    Assumptions:
    - Latest month is already selected by the website.
    - Simply discover the download URL from the visible row.
    - Download via requests (same style as fallback_direct_download).
    """

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    saved_files = []

    headless = get_headless(amc_name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_launch_args(headless))

        context = browser.new_context(
            accept_downloads=True,
            user_agent=HEADERS["User-Agent"],
        )

        page = context.new_page()

        page.goto(
            page_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        page.wait_for_timeout(2500)

        dismiss_popups(page)

        if interaction_steps:
            run_interaction_steps(page, interaction_steps)

        page.wait_for_timeout(1500)

        # Latest visible factsheet row
        row = page.locator("div.flex.items-center.px-2.py-3.border-b.w-full").first

        if row.count() == 0:
            raise Exception("No factsheet row found.")  # noqa: TRY002

        month_text = row.locator("div.flex-1").inner_text().strip()

        if DEBUG:
            print(f"Latest factsheet : {month_text}")

        download_btn = row.locator("button").first

        with page.expect_response(
            lambda r: "download-doc" in r.url,
            timeout=15000,
        ) as response_info:
            download_btn.click(force=True)

        response = response_info.value

        download_url = response.url

        if DEBUG:
            print(download_url)

        # Download exactly like every other AMC
        content = download_pdf_bytes(
            download_url,
            referer_url=page_url,
            amc_name=amc_name,
        )

        filename = safe_filename(f"{amc_name}_{month_text}")

        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        filepath = os.path.join(
            DOWNLOAD_DIR,
            filename,
        )

        with open(filepath, "wb") as f:
            f.write(content)

        saved_files.append(filepath)

        browser.close()

    return saved_files
