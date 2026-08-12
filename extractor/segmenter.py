"""
Splits a multi-scheme AMC factsheet PDF into per-scheme page groups.

Approach: each scheme starts with a heading line that is short, contains a
scheme keyword (FUND/ETF/SCHEME/PLAN), and is NOT a running header like
"MONTHLY FACTSHEET". We do NOT require the line to be fully uppercase --
that check silently drops mixed-case headings like "360 ONE MSCI India ETF"
(found via testing). Instead we check keyword presence + line shape.

This module is AMC-agnostic and shared: verified against both 360 ONE and
HDFC factsheets, and it held up unmodified on both despite their sidebar
layouts being completely different -- heading detection only looks at page
text, not column position, so it doesn't care about per-AMC layout at all.

Continuation pages (a scheme spanning 2+ pages, e.g. Balanced Hybrid Fund's
equity+debt tables) are attached to the current scheme as long as they
contain factsheet-body markers and no new heading appears.
"""

import re
from .config import SCHEME_KEYWORDS, HEADING_EXCLUDE

BODY_MARKERS = re.compile(
    r"Portfolio as on|Fund Manager|Benchmark Index|BENCHMARK|NAV as on|Scheme Performance",
    re.IGNORECASE,
    # IGNORECASE added after testing against HDFC: its labels are all-caps
    # ("FUND MANAGER", "BENCHMARK INDEX") and the original case-sensitive
    # pattern silently failed to match a scheme's own heading page, which
    # would have dropped that page from page_idxs entirely -- and with it,
    # the one page holding the sidebar (benchmark/manager) data. 360 ONE's
    # labels are title-case, so this only broadens matching, doesn't
    # narrow it -- safe for the AMC that was already working.
)


def _is_scheme_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    if any(ex in line.upper() for ex in HEADING_EXCLUDE):
        return False
    upper = line.upper()
    return any(re.search(rf"\b{kw}\b", upper) for kw in SCHEME_KEYWORDS)


def segment_schemes(pdf) -> dict[str, list[int]]:
    """Returns {scheme_name: [page_index, ...]} in document order."""
    scheme_pages: dict[str, list[int]] = {}
    current = None

    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        first_line = text.split("\n")[0].strip() if text else ""

        if _is_scheme_heading(first_line):
            current = first_line
            scheme_pages.setdefault(current, [])

        if current and BODY_MARKERS.search(text):
            if i not in scheme_pages[current]:
                scheme_pages[current].append(i)

    return scheme_pages
