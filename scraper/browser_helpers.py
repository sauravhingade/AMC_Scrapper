"""
Generic Playwright helpers shared by every AMC handler:
  - dismiss_popups: closes cookie/legal/promo modals before scraping
  - run_interaction_steps: executes a declarative list of page actions
    (click / select / fill / wait) defined per-AMC in interaction_steps
  - select_latest_option_interaction: picks the newest month/year from
    a dropdown, used as one of the possible "select" step actions
"""

import re

# Playwright's own TimeoutError -- NOT the built-in Python TimeoutError.
# See note in select_latest_option_interaction below.
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .config import DEBUG, MONTHS


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

    Safety nets (added after HDFC's mega-menu tripped this): a loose
    selector like "[class*='close' i]" can accidentally match a real
    nav link if the site reuses "close" somewhere in a menu-toggle
    class name. Two guards catch that before it can click:
      - skip any candidate nested inside a <nav> element -- a real
        popup close button is never part of the site's navigation.
      - for the generic close_selectors tier only, skip candidates
        whose visible text is more than a couple characters -- close
        icons are empty or a bare "x"/"×"; nav links have real words
        like "Services".
    And if a click ever navigates the page away from where we started
    (rather than just closing a modal in place), that's treated as a
    misfire, not a successful dismiss: we log it and navigate back.
    """
    exact_selectors = [
        "button[aria-label='Close welcome message']",
    ]
    priority_selectors = [
        "text=/May be later/i",
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

    starting_url = page.url
    dismissed_any = False

    for attempt in range(attempts):
        clicked_this_round = False
        for selector in exact_selectors + priority_selectors + close_selectors:
            try:
                btn = page.locator(selector).first

                if btn.count() == 0 or not btn.is_visible():
                    continue

                # Guard 1: never click something nested in the nav bar.
                if btn.locator("xpath=ancestor::nav").count() > 0:
                    continue

                # Guard 2 (generic-close tier only): a real close icon
                # has little to no visible text; a nav link does.
                if selector in close_selectors:
                    try:
                        text = btn.inner_text(timeout=1000).strip()
                    except Exception:
                        text = ""
                    if len(text) > 3:
                        continue

                btn.click(timeout=2000)
                page.wait_for_timeout(600)

                # Guard 3: a click that navigated us away is a misfire,
                # not a dismissed popup -- recover instead of silently
                # continuing to scrape (or scan for more popups on) the
                # wrong page.
                if page.url != starting_url:
                    print(
                        f"WARNING: clicking '{selector}' navigated from "
                        f"{starting_url} to {page.url} -- this was "
                        f"probably a nav link, not a popup close button. "
                        f"Navigating back."
                    )
                    page.goto(starting_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1000)
                    continue

                print(f"Dismissed popup via: {selector} (round {attempt + 1})")
                clicked_this_round = True
                dismissed_any = True
                break  # re-scan from the top -- a NEW modal may have appeared
            except Exception:  # noqa: S112
                continue
        if not clicked_this_round:
            page.wait_for_timeout(wait_between_ms)

    return dismissed_any


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

    # NOTE: this used to catch the built-in `TimeoutError`, which
    # Playwright never raises (its timeout error is
    # `playwright.sync_api.TimeoutError`, a different class) -- so this
    # branch was dead code and every timeout fell through to the
    # `Exception` handler below anyway. Fixed to catch the real one.
    except PlaywrightTimeoutError:
        print("Dropdown selection timeout.")

    except Exception as e:
        print("Failed selecting latest option:", e)


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

                # Make sure element is in viewport
                locator.scroll_into_view_if_needed()

                # Allow small animations to finish
                page.wait_for_timeout(200)

                # Retry click a few times (helps on JS-heavy sites)
                clicked = False

                for attempt in range(3):
                    try:
                        locator.click(timeout=5000)
                        clicked = True
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        page.wait_for_timeout(500)

                if not clicked:
                    raise Exception("Unable to click element.")  # noqa: TRY002

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
