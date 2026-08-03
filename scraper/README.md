# Factsheet Scraper — file layout

Split your single file into 5 modules, grouped by responsibility.
**No logic was changed** except two things noted below (both are real bug
fixes, not behavior changes to working paths).

```
config.py           Constants: AMC classification sets, HEADERS, timeouts,
                     month name/number lookups. No logic, just data.

utils.py             Pure helpers, no browser dependency:
                      safe_filename, extract_date, keyword_score,
                      pick_latest_links, find_pdf_links.

browser_helpers.py   Generic Playwright plumbing used by every AMC handler:
                      dismiss_popups, run_interaction_steps,
                      select_latest_option_interaction.

amc_specific.py       Per-AMC scraping flows — the "special case" handlers
                      (Kotak, Sundaram, NAVI, JIO, Tata, Taurus, ITI, and
                      the generic fallback), plus get_rendered_html().

downloader.py          Entry point. download_pdf_bytes() + download_factsheet().
                      This is what you import and call.
```

## How to use it

Same as before, just import from `downloader` instead of one giant file:

```python
from downloader import download_factsheet

download_factsheet(
    amc_name="Axis Mutual Fund",
    page_url="https://...",
    interaction_steps=[...],   # optional, same format as before
)
```

## What actually changed (both are bug fixes, not logic changes)

1. **The underlined exception you asked about, in `select_latest_option_interaction`:**
   the code was catching the built-in Python `TimeoutError`, but Playwright
   never raises that — it raises its own `playwright.sync_api.TimeoutError`,
   a completely different class. So that `except` branch was dead: every
   real timeout was silently falling through to the generic
   `except Exception` below it instead of printing the more specific
   "Dropdown selection timeout." message. Fixed by importing and catching
   the real `playwright.sync_api.TimeoutError`. Behavior is the same either
   way (both branches just print and continue) — this only fixes *which*
   message you see when it happens, so it's safe.

2. **Removed dead/duplicate imports** that had no effect on behavior:
   `from playwright.sync_api import sync_playwright` was imported twice at
   the top of the original file; `import json` and
   `from playwright_stealth import Stealth` were imported but never
   referenced anywhere in the code.

Everything else — every selector, every wait time, every regex, every
AMC-specific quirk and comment about *why* it's there — is copied over
exactly as it was.

## Verified

- All 5 files byte-compile cleanly.
- All modules import with no circular-import errors.
- Spot-checked `extract_date()` and `pick_latest_links()` against the
  original test cases (including the `factsheet_2026.pdf` edge case,
  which still correctly returns `None` — that's original regex
  behavior, not new breakage).
