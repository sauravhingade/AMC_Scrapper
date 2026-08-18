"""
360 ONE Mutual Fund extractor.

Layout: narrow left sidebar (fund-details labels) + wide holdings table on
the right, sidebar_width=152pt. This is the ORIGINAL, already-tested logic
-- ported here unchanged from the pre-split field_extractors.py. Nothing in
this file was touched while adding HDFC support; if 360 ONE extraction ever
needs a change, test against a real 360 ONE PDF before editing, same as any
other AMC module.
"""

import re

import pdfplumber

from ..config import HOLDINGS_CATEGORY_LABELS, HEADING_EXCLUDE, SCHEME_KEYWORDS


BODY_MARKERS = re.compile(
    r"Portfolio as on|Fund Manager|Benchmark Index|BENCHMARK|NAV as on|Scheme Performance",
    re.IGNORECASE,
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


NUMERIC_PCT = re.compile(r"^-?\d+(\.\d+)?$")


def normalize_label(s: str) -> str:
    return re.sub(r"[\s\-]+", "", s.lower())


_NORMALIZED_CATEGORY_LABELS = {normalize_label(l) for l in HOLDINGS_CATEGORY_LABELS}

SIDEBAR_WIDTH = 152


def reconstruct_lines(words, y_tolerance: float = 1.5) -> str:
    """Group words into lines by y-position, sort each line left-to-right.

    y_tolerance was originally 3.0, which merges lines whose baselines sit
    within 3pt of each other. Found via testing: a name that wraps to a new
    line (e.g. "Mehta") can land within 3pt of a small sleeve-role tag
    ("Equity"/"Debt") positioned elsewhere on the page, causing the two to
    merge into one reconstructed line with WRONG x-order -- the tag (further
    left) sorts before the wrapped name, corrupting extraction. 1.5pt keeps
    genuinely-same-line words together while separating near-miss cases like
    this one.
    """
    lines = {}
    for w in words:
        y = round(w["top"] / y_tolerance) * y_tolerance
        lines.setdefault(y, []).append(w)
    return "\n".join(
        " ".join(w["text"] for w in sorted(lines[y], key=lambda w: w["x0"]))
        for y in sorted(lines)
    )


def get_column_text(page, x0: float, x1: float) -> str:
    """Extract clean text from a vertical column slice of a page, by
    physically cropping the page to the bbox first.

    GOTCHA (found via testing against an HDFC factsheet, doesn't show up on
    360 ONE's layout): page.within_bbox() clips glyphs AT the boundary --
    if a word straddles x1 (e.g. "(TRI)" starting inside the column but
    ending just past it), the clip cuts the word mid-character ("(TRI)"
    becomes "(TRI"), silently corrupting the value. This function is kept
    as-is (360 ONE's sidebar has a clean gap at its boundary, so it never
    hits this), but any AMC whose columns wrap text right up against the
    crop line should use get_column_text_by_start() instead.
    """
    cropped = page.within_bbox((x0, 0, x1, page.height))
    words = cropped.extract_words()
    return reconstruct_lines(words)


def get_column_text_by_start(page, x0: float, x1: float, x_tolerance: float = 3) -> str:
    """Extract text from a vertical column slice, filtering by each word's
    START position (x0) instead of physically cropping the page.

    Use this instead of get_column_text() whenever a column's text wraps
    right up against the crop boundary (no clean whitespace gap) -- keeps
    whole words that merely *start* inside the column, even if they render
    a few points past x1, instead of clipping them mid-character. Safe to
    use with a generous x1 (e.g. up to where the next column's content
    actually starts) since it can only ever pull in whole words, never
    partial ones.
    """
    words = [
        w for w in page.extract_words(x_tolerance=x_tolerance) if x0 <= w["x0"] < x1
    ]
    return reconstruct_lines(words)


def get_column_tables(page, x0: float, x1: float):
    """Extract tables from a vertical column slice of a page."""
    cropped = page.within_bbox((x0, 0, x1, page.height))
    return cropped.extract_tables()


def open_pdf(path: str):
    return pdfplumber.open(path)


def is_real_holding_row(row) -> bool:
    """Most AMC tables are Company/Sector/% (3 cols); commodity ETFs (Gold,
    Silver) drop Sector since it doesn't apply -- 2 cols: Company/%."""
    if not row or len(row) < 2:
        return False
    company = row[0]
    pct = row[2] if len(row) >= 3 else row[1]
    if not company or not pct:
        return False
    # Normalize away whitespace/hyphens so "Sub Total", "SubTotal", and
    # "Sub-Total" all match the same exclusion entry -- found via testing
    # that a bare "SubTotal" (no space) slipped past an exact "sub total"
    # match.
    if normalize_label(company.replace("\n", " ")) in _NORMALIZED_CATEGORY_LABELS:
        return False
    pct_clean = str(pct).strip().replace("\n", "")
    return bool(NUMERIC_PCT.match(pct_clean))


def extract_benchmark(sidebar_text: str) -> str | None:
    """
    Handles both single-line ("Benchmark Index :BSE 500 TRI") and multi-line
    benchmark names that wrap ("...Composite Debt 50:50\nIndex" or
    "BSE 500 TRI - 25% +\nNIFTY Composite Debt Index - 45% + ...").
    Strategy: capture from the label up to the next known label on the
    sidebar (Plans Offered / Options Offered / Minimum Application), across
    newlines, then collapse whitespace.

    Layout quirk found via testing: when the benchmark value wraps across
    two lines, the label's ":" separator can sit vertically BETWEEN the two
    wrapped lines and get reconstructed as its own row in the middle of the
    captured value (e.g. "CRISIL Dynamic Bond\n:\nA-III Index"). Real
    benchmark names never have a colon surrounded by spaces on both sides
    (ratio-style colons like "50:50" are tight, no spaces) -- so any " : "
    left after whitespace collapse is this artifact, safe to strip.
    """
    m = re.search(
        r"Benchmark(?:\s+Index)?\s*:?\s*(.+?)(?=\n\s*(?:Plans Offered|Options Offered|Minimum Application|New Purchase|$))",
        sidebar_text,
        re.DOTALL,
    )
    if not m:
        return None
    raw = re.sub(r"\n\s*:\s*", " ", m.group(1))
    value = re.sub(r"\s+", " ", raw).strip()
    return value or None


def extract_isin(sidebar_text: str) -> str:
    """Returns the ISIN if present, otherwise '' (schemes like open-ended
    equity funds don't carry one; ETFs do)."""
    m = re.search(r"ISIN\s*:\s*([A-Z0-9]{6,15})", sidebar_text)
    return m.group(1).strip() if m else ""


_SLEEVE_TAGS = {"equity", "debt", "commodity", "commodities"}


def extract_fund_managers(sidebar_text: str) -> list[dict]:
    """
    Returns a list of {"role": "Fund Manager"|"Co-Fund Manager", "name": ...}
    so hybrid/multi-asset schemes with separate equity/debt/commodity
    managers aren't collapsed into one field.

    Hybrid/multi-asset schemes tag each manager with a sleeve label
    ("Equity"/"Debt"/"Commodity") positioned near the name. Layout noise can
    pull that tag into the captured name (e.g. "Mr. Viral Mehta Equity") --
    strip it off as a trailing word rather than trying to perfect the regex
    lookahead, and surface it separately as `sleeve` instead of discarding it.
    """
    managers = []
    for m in re.finditer(
        r"(Fund Manager|Co-\s*Fund Manager)\s+((?:Mr\.|Ms\.)\s*[A-Za-z]+(?:\s[A-Za-z]+){0,2}?)(?=\s+(?:Mr\.|Ms\.|Co-|\(w\.e\.f|$))",
        sidebar_text,
    ):
        role = re.sub(r"\s+", " ", m.group(1)).strip()
        raw_name = re.sub(r"\s+", " ", m.group(2)).strip()

        sleeve = None
        words = raw_name.split()
        if words and words[-1].lower() in _SLEEVE_TAGS:
            sleeve = words[-1].capitalize()
            raw_name = " ".join(words[:-1])

        entry = {"role": role, "name": raw_name, "sleeve": sleeve}
        if entry not in managers and raw_name:
            managers.append(entry)
    return managers


_COMMODITY_FALLBACK = re.compile(r"^(Gold|Silver|Goldten)\s*$", re.MULTILINE)


def extract_holdings(page, table_x0: float) -> list[dict]:
    """
    Extracts the stock/instrument portfolio table on a page.
    Filters out category-subtotal rows (e.g. "Commercial Paper", "REIT/InvIT
    Instruments") that share the same 3-column shape as real holdings but
    aren't actual securities -- found via testing on debt/liquid schemes.

    Fallback: commodity ETFs (Gold/Silver) have a table too small/simple for
    pdfplumber's line-based table detector to parse into clean columns --
    for those we fall back to a direct text-line regex on the right column.
    """

    holdings = []
    for table in get_column_tables(page, table_x0, page.width):
        if not table or not any(row and row[0] == "Company Name" for row in table):
            continue
        for row in table:
            if is_real_holding_row(row):
                has_sector = len(row) >= 3
                holdings.append(
                    {
                        "company": row[0].replace("\n", " ").strip(),
                        "sector": (row[1] or "").replace("\n", " ").strip()
                        if has_sector
                        else "",
                        "pct_to_net_assets": (row[2] if has_sector else row[1]).strip(),
                    }
                )

    if not holdings:
        text = get_column_text(page, table_x0, page.width)
        seen = set()
        for m in re.finditer(r"(Gold|Silver|Goldten)\s+(\d+\.\d+)\b", text):
            key = (m.group(1), m.group(2))
            if key not in seen:
                seen.add(key)
                holdings.append(
                    {
                        "company": m.group(1),
                        "sector": "",
                        "pct_to_net_assets": m.group(2),
                    }
                )

    return holdings


def extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
    """Runs all field extractors for one scheme's page group."""
    first_page = pdf.pages[page_idxs[0]]
    sidebar_text = get_column_text(first_page, 0, SIDEBAR_WIDTH)

    holdings = []
    for idx in page_idxs:
        page = pdf.pages[idx]
        page_holdings = extract_holdings(page, SIDEBAR_WIDTH)
        holdings.extend(page_holdings)
        if page_holdings:
            break  # holdings table found; later pages are performance tables etc.

    return {
        "benchmark": extract_benchmark(sidebar_text),
        "additional_benchmark": None,  # 360 ONE factsheets don't carry this field
        "isin": extract_isin(sidebar_text),
        "fund_managers": extract_fund_managers(sidebar_text),
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
