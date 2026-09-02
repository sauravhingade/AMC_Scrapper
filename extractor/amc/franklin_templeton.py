"""
Franklin Templeton Mutual Fund factsheet extractor.

Mirrors the calling contract of amc/canara_robeco.py (segment_schemes /
extract_scheme_fields, identical return schema) but every bit of the
internal parsing is specific to how Franklin Templeton lays out its
factsheet, which differs substantially from Canara Robeco's:

  * Scheme "cards" are located by cross-referencing the document's own
    Table of Contents (page headed "Contents" / "FUND NAME"), which
    lists every scheme's full name, short code (e.g. "FIMF", "FILCF")
    and starting page number. This is far more robust than pattern
    matching a multi-line, inconsistently-wrapped page heading, and it
    is fully derived from the current month's document -- nothing
    about specific scheme names or page numbers is hardcoded.
  * Most scheme cards occupy one full page, but compact
    Fund-of-Funds cards can be packed two-to-a-page (stacked
    vertically). Scheme "page reference" entries returned by
    segment_schemes() are therefore (page_index, top, bottom) tuples
    bounding the exact card region rather than bare page numbers --
    an internal detail private to this module; the calling contract
    (segment_schemes()/extract_scheme_fields()) is unchanged.
  * Equity-style portfolio tables carry a "No. of shares" column and
    group holdings under a leading GICS industry-group heading (e.g.
    "Banks", "IT - Software") with no percentage of its own -- unlike
    Canara Robeco, where such headings carry their own aggregate %.
  * Debt-style tables carry a "Ratings" column instead, and use
    *trailing* "Total <bucket>" rows (e.g. "Total Corporate Debt",
    "Total Gilts") to retroactively label the instruments above them
    -- the opposite convention from the leading equity headings.
  * Percentages in the portfolio table are printed as bare decimals
    ("6.11") with no "%" suffix, unlike Canara Robeco.
  * A scheme page can host more than one physical table instance
    (e.g. an equity table followed further down the page by a debt
    table, as in Multi Asset Allocation Fund), each with its own
    column layout, rather than a single two-column spread.
  * Bucket/industry names used to recognise a leading heading are not
    hardcoded: they are read straight from each scheme's own
    "Industry Allocation - Equity Assets" chart (equity industries)
    and from every "Total <bucket>" line found anywhere in the
    document (debt/other asset-type buckets) -- both derived fresh
    from the current month's PDF.

Nothing here is wired to a specific page number, month, or scheme
name -- everything is derived from on-page text/coordinates so the
same code keeps working on next month's factsheet.
"""

from __future__ import annotations

import re
from collections import Counter

try:
    from ..config import HEADING_EXCLUDE
except Exception:  # pragma: no cover - allows standalone testing
    HEADING_EXCLUDE = ()


# --------------------------------------------------------------------------
# generic text/word helpers
# --------------------------------------------------------------------------

_PUA_RE = re.compile(r"[\u2022\u25cf\u25aa\u25e6\u2023\u2043\ue000-\uf8ff]")
_WS_RE = re.compile(r"\s+")
_TRAILING_FOOTNOTE_RE = re.compile(r"[^A-Za-z0-9\s():&,/'\-]+$")
# Franklin Templeton portfolio tables print percentages as bare decimals
# ("6.11", "-0.01", "0.00") with no "%" suffix -- unlike Canara Robeco.
_FT_NUM_RE = re.compile(r"^-?\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?$")
# Broader "this token is nothing but a number" check, used to strip
# stray "No. of shares" / "Market Value" column figures that fall
# inside the company-name x-range (those columns carry no header of
# their own worth anchoring on, unlike the % and Ratings columns).
_PURE_NUMBER_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def _looks_like_stray_column_number(text):
    """True for a stray "No. of shares"/"Market Value" figure that
    landed inside the name column's x-range on a header layout where
    that column's own boundary couldn't be pinned down precisely --
    but NOT for a short 1-2 digit number that's legitimately part of
    a security's own name (a day-of-month in a maturity date, a
    segregated-portfolio sequence number, and similar)."""
    if not _PURE_NUMBER_RE.match(text):
        return False
    digits = text.replace(",", "").replace(".", "").lstrip("-")
    return len(digits) >= 3


def _clean(text):
    if not text:
        return ""
    text = _PUA_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _strip_trailing_footnote_symbols(text):
    text = text.strip()
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_FOOTNOTE_RE.sub("", text).strip()
    return text


def _page_words(page):
    return (
        page.extract_words(x_tolerance=3, y_tolerance=1.5, keep_blank_chars=False) or []
    )


def _norm_bucket(text):
    """Uppercase / whitespace-normalised form used for whitelist matching."""
    text = _strip_trailing_footnote_symbols(text)
    text = re.sub(r"[^A-Za-z0-9&/\-\s]", " ", text)
    return _WS_RE.sub(" ", text).strip().upper()


def _cluster_rows(words, y_tol=1.6):
    """Group words into physical text rows by vertical position."""
    rows = []
    for w in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(w["top"])
        placed = False
        for r in reversed(rows[-4:]):
            if abs(r["top"] - top) <= y_tol:
                r["words"].append(w)
                placed = True
                break
        if not placed:
            rows.append({"top": top, "words": [w]})
    return rows


# --------------------------------------------------------------------------
# Table of Contents parsing -> {abbr: {"name": ..., "page": 1-indexed}}
# --------------------------------------------------------------------------

_TOC_ENTRY_RE = re.compile(
    r"(?P<name>(?:Franklin|Templeton)\b[^()\n]*?\((?P<abbr>[A-Z][A-Z0-9]{1,9})\)"
    r"(?:\s*\(Erstwhile[^)]*\))?)\s*\.{2,}\s*(?P<page>\d{1,4})\s*$",
    re.IGNORECASE,
)


def _find_toc_pages(pdf):
    toc_idxs = []
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        first_lines = text.split("\n")[:2]
        head = " ".join(first_lines).upper()
        if "CONTENTS" in head and i < 10:
            toc_idxs.append(i)
        elif toc_idxs and "FUND NAME" in text.upper() and i - toc_idxs[-1] <= 2:
            toc_idxs.append(i)
    return toc_idxs


def _build_toc_registry(pdf):
    registry = {}
    order = []
    for i in _find_toc_pages(pdf):
        text = pdf.pages[i].extract_text() or ""
        lines = text.split("\n")
        joined = []
        buf = ""
        for line in lines:
            if buf and buf.rstrip().endswith("-"):
                buf = buf.rstrip() + line.lstrip()
            else:
                buf = (buf + " " + line).strip() if buf else line
            if re.search(r"\.{2,}\s*\d{1,4}\s*$", buf):
                joined.append(buf)
                buf = ""
        if buf:
            joined.append(buf)
        for line in joined:
            m = _TOC_ENTRY_RE.search(line)
            if not m:
                continue
            abbr = m.group("abbr").upper()
            name = _clean(m.group("name"))
            name = re.sub(r"\s*\.{2,}\s*$", "", name).strip()
            page_no = int(m.group("page"))
            if abbr not in registry:
                registry[abbr] = {"name": name, "page": page_no}
                order.append(abbr)
    return registry, order


def _get_toc_registry(pdf):
    cache = getattr(pdf, "_franklin_templeton_toc_cache", None)
    if cache is not None:
        return cache
    registry, order = _build_toc_registry(pdf)
    cache = (registry, order)
    try:
        pdf._franklin_templeton_toc_cache = cache
    except Exception:
        pass
    return cache


# --------------------------------------------------------------------------
# per-page scheme "card" region detection (handles 2-schemes-per-page FoFs)
# --------------------------------------------------------------------------


def _portfolio_anchor_tops(page):
    """Top positions of every "As on <date> ... PORTFOLIO" table-start
    marker on this page, one per scheme card physically present here.

    Plain "PORTFOLIO" token matching is not enough -- the metadata
    panel's "ANNUALISED PORTFOLIO YTM" label also contains the bare
    word "PORTFOLIO". Requiring an "As"+"on" date phrase somewhere
    to its left on the same text row disambiguates the genuine
    table-start marker from that false positive.
    """
    words = _page_words(page)
    as_on_rows = set()
    for w in words:
        if w["text"] != "As":
            continue
        nxt = [
            o
            for o in words
            if o["text"] == "on"
            and abs(o["top"] - w["top"]) <= 2
            and 0 < o["x0"] - w["x1"] <= 10
        ]
        if nxt:
            as_on_rows.add(round(float(w["top"]), 1))

    tops = []
    for w in words:
        if w["text"] != "PORTFOLIO":
            continue
        top = float(w["top"])
        # "As on <date>" and "PORTFOLIO" are usually on the same line, but
        # occasionally "PORTFOLIO" wraps onto its own line just below.
        if any(-2 <= top - r <= 15 for r in as_on_rows):
            tops.append(top)
    tops.sort()
    merged = []
    for t in tops:
        if not merged or t - merged[-1] > 3:
            merged.append(t)
    return merged


def _scheme_card_regions(page, known_abbrs):
    """Return a list of (top, bottom, abbr_or_None) regions, one per
    scheme card physically present on this page, in top-to-bottom order."""
    portfolio_tops = _portfolio_anchor_tops(page)
    if not portfolio_tops:
        return []
    words = _page_words(page)
    name_word_tops = sorted(
        float(w["top"]) for w in words if w["text"] in ("Franklin", "Templeton")
    )
    abbr_hits = [
        (float(w["top"]), w["text"]) for w in words if w["text"] in known_abbrs
    ]

    # First, find each card's own abbreviation independently of the
    # heading text: it sits close above its own "PORTFOLIO" anchor.
    # (A compact Fund-of-Funds card's *holdings* can themselves be a
    # list of other "Franklin India ... Fund" scheme names, which
    # would otherwise be mistaken for a second heading if the heading
    # search window were simply "everything since the previous card".)
    prev_bottom = 0.0
    card_abbrs = []
    for ptop in portfolio_tops:
        local_abbrs = [(t, txt) for t, txt in abbr_hits if prev_bottom - 2 <= t <= ptop + 80]
        card_abbrs.append(local_abbrs[-1][1] if local_abbrs else None)
        prev_bottom = ptop

    # Then find each card's heading top in a tight window directly
    # above its own abbreviation (or, failing that, its own
    # "PORTFOLIO" anchor) rather than the whole gap since the
    # previous card.
    heading_tops = []
    prev_bottom = 0.0
    for idx, ptop in enumerate(portfolio_tops):
        abbr_top = None
        if card_abbrs[idx] is not None:
            hits = [t for t, txt in abbr_hits if txt == card_abbrs[idx] and prev_bottom - 2 <= t <= ptop + 80]
            if hits:
                abbr_top = min(hits)
        anchor = abbr_top if abbr_top is not None else ptop
        window_lo = max(prev_bottom - 2, anchor - 70)
        cands = [t for t in name_word_tops if window_lo <= t <= ptop]
        heading_top = min(cands) if cands else max(prev_bottom, anchor - 20)
        heading_tops.append(heading_top)
        prev_bottom = ptop

    regions = []
    for idx, ptop in enumerate(portfolio_tops):
        region_top = max(heading_tops[idx] - 4, 0.0)
        region_bottom = (
            heading_tops[idx + 1] if idx + 1 < len(heading_tops) else page.height
        )
        regions.append((region_top, region_bottom, card_abbrs[idx]))
    return regions


# --------------------------------------------------------------------------
# bucket / industry-heading whitelist
#
# A leading heading row in an equity-style table (e.g. "Banks", "IT -
# Software") carries no percentage of its own, which makes it
# structurally indistinguishable from the *first physical line* of a
# company name that wraps across two lines (e.g. "Cognizant Technology
# Solutions Corp.," wrapping onto "A (USA)*"). Rather than guess from
# shape/punctuation, every scheme's own "Industry Allocation - Equity
# Assets" chart lists precisely the set of industry headings used in
# its own table, and every debt/other asset-type bucket
# ("Corporate Debt", "Mutual Fund Units", "ETF", ...) is exactly the
# set of names that appear as the object of some "Total <bucket>" row
# somewhere in the document. Both sets are derived fresh from the
# current month's PDF -- nothing is hardcoded.
# --------------------------------------------------------------------------

_TOTAL_ROW_RE = re.compile(
    r"^Total\s+([A-Za-z][A-Za-z&/,\.\-\s]*?)(?=\s+[\d(\u0060]|\s*$)",
    re.IGNORECASE,
)

# These "Total <x>" labels close out an entire table/section rather than
# a sub-bucket within it; they must never be (mis)used to retroactively
# relabel the rows above them.
_AGGREGATE_STOP_NORMS = {
    "EQUITY HOLDINGS",
    "DEBT HOLDINGS",
    "HOLDINGS",
    "ASSET",
    "ASSETS",
    "INTEREST RATE SWAP",
    "PORTFOLIO TURNOVER",
    "INTEREST RATE SWAP POSITION",
}

# Small, stable, SEBI/AMFI-standard asset-type vocabulary (the same
# structural role as canara_robeco.py's own _ALL_TOP_LEVEL_BUCKETS
# constant) used only as a safety net alongside the two dynamically
# derived whitelists above -- never a scheme-specific or month-specific
# name.
_STATIC_BUCKET_NAMES = {
    "EQUITIES",
    "LISTED AWAITING LISTING ON STOCK EXCHANGE",
    "DEBT INSTRUMENTS",
    "GOVERNMENT SECURITIES",
    "MONEY MARKET INSTRUMENTS",
    "PREFERENCE SHARES",
    "PREFERENCE SHARE",
    "EXCHANGE TRADED FUND",
    "EXCHANGE TRADED FUNDS",
    "MUTUAL FUND UNITS",
    "ALTERNATIVE INVESTMENT FUND UNITS",
    "EQUITY OPTION UNITS",
    "UNLISTED",
    "UNLISTED EQUITY",
    "FOREIGN SECURITIES",
    "WARRANTS",
    "CORPORATE DEBT",
    "PSU PFI BONDS",
    "CERTIFICATE OF DEPOSIT",
    "COMMERCIAL PAPER",
    "GILTS",
}


def _build_global_bucket_whitelist(pdf):
    """Scan every page once for "Total <bucket>" rows and collect the
    distinct bucket names, skipping the aggregate/section-closing
    ones. Cached on the pdf object (mirrors canara_robeco.py's own
    additional-benchmark cache)."""
    names = set(_STATIC_BUCKET_NAMES)
    for page in pdf.pages:
        text = page.extract_text() or ""
        for line in text.split("\n"):
            m = _TOTAL_ROW_RE.match(line.strip())
            if not m:
                continue
            norm = _norm_bucket(m.group(1))
            if not norm or norm in _AGGREGATE_STOP_NORMS:
                continue
            if len(norm) > 60:
                continue
            names.add(norm)
    return names


def _build_global_industry_whitelist(pdf, known_abbrs):
    """Union the "Industry Allocation - Equity Assets" chart of every
    scheme card in the document. A GICS industry with a very small
    allocation in one scheme is occasionally folded into the scheme's
    own chart under a combined/omitted entry, but it reliably shows
    up in some other scheme's chart -- this still costs nothing to
    derive fresh from the current month's PDF, it just looks at every
    scheme instead of only the one being extracted."""
    names = set()
    for page in pdf.pages:
        for (rtop, rbot, _abbr) in _scheme_card_regions(page, known_abbrs):
            names |= _industry_whitelist_for_region(page, rtop, rbot)
    return names


def _get_global_industry_whitelist(pdf, known_abbrs):
    cache = getattr(pdf, "_franklin_templeton_industry_cache", None)
    if cache is not None:
        return cache
    cache = _build_global_industry_whitelist(pdf, known_abbrs)
    try:
        pdf._franklin_templeton_industry_cache = cache
    except Exception:
        pass
    return cache


def _get_global_bucket_whitelist(pdf):
    cache = getattr(pdf, "_franklin_templeton_bucket_cache", None)
    if cache is not None:
        return cache
    cache = _build_global_bucket_whitelist(pdf)
    try:
        pdf._franklin_templeton_bucket_cache = cache
    except Exception:
        pass
    return cache


def _industry_whitelist_for_region(page, top_bound, bottom_bound):
    """Read the "Industry Allocation - Equity Assets" chart on this
    scheme's own card (if present) and return the set of industry
    names it lists -- these are exactly the leading headings used in
    that scheme's own equity holdings table."""
    words = [
        w
        for w in _page_words(page)
        if top_bound - 1 <= float(w["top"]) < bottom_bound
    ]
    industry_tops = sorted(
        float(w["top"]) for w in words if w["text"] == "Industry"
    )
    alloc_start = None
    for t in industry_tops:
        alloc_w = [
            o
            for o in words
            if o["text"] == "Allocation" and abs(o["top"] - t) <= 2
        ]
        if alloc_w:
            alloc_start = t
            break
    if alloc_start is None:
        return set()

    label_x0 = min(
        float(o["x0"]) for o in words if o["text"] == "Industry" and abs(o["top"] - alloc_start) <= 2
    )
    section_words = [
        w
        for w in words
        if float(w["top"]) > alloc_start + 2 and float(w["x0"]) >= label_x0 - 10
    ]
    rows = _cluster_rows(section_words, y_tol=2)
    names = set()
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        # Some scheme cards print a second bar chart (e.g. an asset-type
        # breakdown) immediately to the right of the industry-allocation
        # chart, on the same text rows. Only the *first* label+"NN.NN%"
        # run on each row belongs to the industry-allocation chart;
        # anything after that first percentage is the second chart and
        # must be dropped rather than merged into the industry name.
        label_tokens = []
        for w in ws:
            if re.match(r"^-?\d+(?:\.\d+)?%$", w["text"]):
                if label_tokens:
                    label = _clean(" ".join(label_tokens))
                    norm = _norm_bucket(label)
                    if norm:
                        names.add(norm)
                label_tokens = []
                continue
            label_tokens.append(w["text"])
    return names


# --------------------------------------------------------------------------
# row extraction + bucket classification
# --------------------------------------------------------------------------

# Rows that close out a whole table/section (already excluded via
# _AGGREGATE_STOP_NORMS inside the "Total X" handling below) plus a
# couple of stray footer/legend markers that must never be read as
# holdings if they happen to fall inside a table's bounding box.
_ROW_STOP_PREFIXES = (
    "@",
    "Please",
    "Different plans",
    "Industry Allocation",
    "Composition by",
    "Outstanding Interest Rate Swap",
    "Outstanding Total Return Swap",
    "www.franklintempletonindia",
    "* Top 10 Holdings",
    "* Top Ten Holdings",
    "Top 10 Holdings",
    "Top Ten Holdings",
    "Grand Total",
)


def _compute_bottom_bound(page, x0, x1, top, region_bottom, next_group_top):
    """The lowest y at which this table's data may safely be read,
    bounded above by whichever comes first: the next stacked header
    group in the same column range, a known stop-marker line, or the
    scheme card's own bottom edge."""
    limit = region_bottom
    if next_group_top is not None and next_group_top > top:
        limit = min(limit, next_group_top)
    words = [
        w
        for w in _page_words(page)
        if float(w["top"]) > top + 15
        and float(w["top"]) < limit
        and x0 - 15 <= float(w["x0"]) < x1
    ]
    rows = _cluster_rows(words, y_tol=2)
    for r in sorted(rows, key=lambda r: r["top"]):
        ws = sorted(r["words"], key=lambda w: w["x0"])
        line = _clean(" ".join(w["text"] for w in ws))
        if any(line.lower().startswith(p.lower()) for p in _ROW_STOP_PREFIXES):
            limit = min(limit, r["top"])
            break
    return limit


_METADATA_PANEL_LABELS = {
    "BENCHMARK",
    "MINIMUM",
    "TURNOVER",
    "VOLATILITY",
    "MATURITY",
}


def _right_edge_for(header, siblings, region_right, page=None):
    same_row = [
        s
        for s in siblings
        if s is not header and abs(s["top"] - header["top"]) <= 4 and s["name_x0"] > header["name_x0"]
    ]
    if same_row:
        sib = min(same_row, key=lambda s: s["name_x0"])
        return sib["name_x0"] - 8
    cap = header["pct_x0"] + 60
    if header["deriv_x0"]:
        cap = max(cap, header["deriv_x0"] - 5)
    cap = min(cap, region_right)
    if page is not None:
        # A single, sibling-less table (e.g. a compact Fund-of-Funds
        # card) can still share the page with a secondary metadata
        # panel (BENCHMARK / MINIMUM INVESTMENT box) sitting just to
        # its right rather than far across the page; a fixed pixel
        # margin isn't always enough to clear it, so also stop short
        # of the nearest such panel label if one is closer.
        panel_hits = [
            float(w["x0"])
            for w in _page_words(page)
            if w["text"] in _METADATA_PANEL_LABELS
            and w["x0"] > header["pct_x0"]
            and abs(w["top"] - header["top"]) < 400
        ]
        if panel_hits:
            cap = min(cap, min(panel_hits) - 10)
    return cap


def _extract_data_cells(row_words, header, pct_upper):
    value_x0 = header.get("value_x0")
    if value_x0 is not None:
        # Precise boundary: the midpoint between the "Market Value" and
        # "% of assets" column labels, so a short right-aligned market
        # value figure can't be mistaken for the (also short) pct figure.
        pct_lo = (value_x0 + header["pct_x0"]) / 2
    else:
        pct_lo = header["pct_x0"] - 6
    pct_hits = [
        w
        for w in row_words
        if pct_lo <= float(w["x0"]) < pct_upper and _FT_NUM_RE.match(w["text"])
    ]
    if not pct_hits:
        return None
    # Columns are right-aligned, so a short market-value figure can
    # still fall inside a loose lower bound for the pct column; the
    # genuine "% of assets" figure is reliably the *rightmost* valid
    # number before the pct column's own right edge (the next column,
    # if any, starts after that).
    pct_w = max(pct_hits, key=lambda w: float(w["x0"]))
    pct_text = pct_w["text"].replace(",", "")
    if not pct_text.endswith("%"):
        pct_text = pct_text + "%"

    rating_x0 = header["rating_x0"]
    first_data_col = rating_x0 or header.get("shares_x0") or header.get("value_x0") or pct_lo
    name_words = [
        w
        for w in row_words
        if float(w["x0"]) < first_data_col - 3
        and not _looks_like_stray_column_number(w["text"])
        and w["text"] != "`"
    ]
    company = _clean(" ".join(w["text"] for w in sorted(name_words, key=lambda w: w["x0"])))

    rating = ""
    if rating_x0:
        rating_words = [
            w
            for w in row_words
            if rating_x0 - 8 <= float(w["x0"]) < pct_lo - 3
            and not _looks_like_stray_column_number(w["text"])
            and w["text"] != "`"
            # Ratings are always agency codes/grades in upper case
            # ("CRISIL AAA", "SOVEREIGN", "ICRA A1+"); a stray lower-
            # case word here is boilerplate overflow from a wide
            # leaf-row label (e.g. "Call,cash and other current
            # asset") wrapping past the name column, not a rating.
            and any(ch.isupper() for ch in w["text"])
        ]
        rating = _clean(" ".join(w["text"] for w in sorted(rating_words, key=lambda w: w["x0"])))

    return {"company": company, "rating": rating, "pct": pct_text}


def _looks_like_heading(text, whitelist):
    norm = _norm_bucket(text)
    if not norm:
        return False
    if norm in whitelist:
        return True
    # allow trivial punctuation drift ("IT - Software" vs "IT SOFTWARE")
    collapsed = norm.replace("-", " ").replace("/", " ")
    collapsed = _WS_RE.sub(" ", collapsed).strip()
    collapsed_whitelist = {w.replace("-", " ").replace("/", " ") for w in whitelist}
    if collapsed in collapsed_whitelist:
        return True
    # occasionally two adjacent bar-chart labels on the same PDF text
    # row get merged with no separator recoverable from position alone
    # (e.g. one bar has no visible value); a heading whose text is a
    # clean prefix of a whitelist entry still counts as a genuine match.
    return any(
        w.startswith(collapsed + " ") or w.endswith(" " + collapsed)
        for w in collapsed_whitelist
    )


def _rows_for_header(page, header, right_edge, bottom_bound):
    # Sub-column labels ("shares", "` Lakhs", "assets", ...) can wrap
    # onto a line just below the "Company Name" line itself; skip far
    # enough past the header to not catch that wrapped label text as
    # the first data row.
    words = [
        w
        for w in _page_words(page)
        if header["name_x0"] - 15 <= float(w["x0"]) < right_edge
        and header["top"] + 11 <= float(w["top"]) < bottom_bound
    ]
    return _cluster_rows(words, y_tol=2)


def _extract_table_holdings(page, headers_group, region_right, region_bottom, whitelist, bottoms):
    """Extract all holdings belonging to one logical table instance
    (a leading header plus, if side-by-side, its sibling group),
    reading left group fully top-to-bottom then right group fully
    top-to-bottom -- the same convention canara_robeco.py uses,
    matching how these factsheets wrap one continuous, alphabetically
    (or otherwise) ordered list across two print columns.

    ``bottoms`` maps id(header) -> the pre-computed, x-range-aware
    bottom bound for that specific header (see _compute_all_bottoms):
    a later header only constrains an earlier one if their column
    x-ranges actually overlap, since a new table (e.g. a debt table)
    can take over just one column while the other legitimately
    continues (e.g. Multi Asset Allocation Fund's equity table)."""
    holdings = []
    current_bucket = None
    unassigned = []

    for header in headers_group:
        right_edge = _right_edge_for(header, headers_group, region_right, page)
        bottom = bottoms.get(id(header), region_bottom)
        pct_upper = header["deriv_x0"] - 5 if header["deriv_x0"] else right_edge
        rows = _rows_for_header(page, header, right_edge, bottom)

        pending = []
        for prow in sorted(rows, key=lambda r: r["top"]):
            row_words = sorted(prow["words"], key=lambda w: w["x0"])
            data = _extract_data_cells(row_words, header, pct_upper)
            if data is None:
                text = _clean(" ".join(w["text"] for w in row_words))
                if text:
                    pending.append(text)
                continue

            company = data["company"]
            if pending:
                carry = []
                for line in pending:
                    if _looks_like_heading(line, whitelist):
                        current_bucket = line
                        carry = []
                    else:
                        carry.append(line)
                if carry:
                    company = _clean(" ".join(carry) + " " + company)
                pending = []

            company = _strip_trailing_footnote_symbols(company)
            company = re.sub(r"[\*\^#]+$", "", company).strip()
            if not company:
                continue

            total_m = _TOTAL_ROW_RE.match(company)
            if total_m:
                bucket_name = _clean(total_m.group(1))
                norm = _norm_bucket(bucket_name)
                if norm not in _AGGREGATE_STOP_NORMS:
                    for h in unassigned:
                        if not h["sector"]:
                            h["sector"] = bucket_name
                unassigned = []
                current_bucket = None
                continue

            sector = data["rating"] or current_bucket or ""
            holding = {
                "company": company,
                "sector": sector,
                "pct_to_net_assets": data["pct"],
            }
            holdings.append(holding)
            if not data["rating"]:
                unassigned.append(holding)
        pending = []

    return holdings


def _dedupe_holdings(holdings):
    seen = Counter()
    out = []
    for h in holdings:
        key = (h["company"].upper(), h["sector"].upper(), h["pct_to_net_assets"])
        if seen[key]:
            continue
        seen[key] += 1
        out.append(h)
    return out


def _ranges_overlap(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def _compute_all_bottoms(page, all_headers, edges, region_bottom):
    """For every header, find the closest *lower* header anywhere on
    the page whose column x-range actually overlaps this header's own
    x-range, and use its top (minus a small margin) as the bound;
    otherwise fall back to the scheme card's own bottom edge or an
    intervening stop-marker line. A later header only constrains an
    earlier one when their columns genuinely overlap -- a new table
    that only takes over one column (e.g. a debt table appearing
    partway down just the right-hand column) must not truncate a
    different column that legitimately continues further down the
    page (e.g. that same page's equity table's other column)."""
    bottoms = {}
    for h in all_headers:
        x0, x1 = h["name_x0"], edges[id(h)]
        lower_tops = [
            oh["top"]
            for oh in all_headers
            if oh is not h
            and oh["top"] > h["top"] + 2
            and _ranges_overlap(x0, x1, oh["name_x0"], edges[id(oh)])
        ]
        limit = min(lower_tops) - 2 if lower_tops else region_bottom
        limit = min(limit, region_bottom)
        bottoms[id(h)] = _compute_bottom_bound(page, x0, x1, h["top"], limit, None)
    return bottoms


def _extract_all_holdings(pdf, page_entries, global_whitelist):
    """page_entries: list of (page_index, region_top, region_bottom)."""
    holdings = []
    for page_idx, rtop, rbottom in page_entries:
        page = pdf.pages[page_idx]
        anchors = _find_header_anchors(page, rtop, rbottom)
        if not anchors:
            continue
        groups = _group_anchor_tables(anchors)
        local_whitelist = (
            global_whitelist | _industry_whitelist_for_region(page, rtop, rbottom)
        )
        region_right = page.width - 15
        all_headers = [h for g in groups for h in g]
        edges = {
            id(h): _right_edge_for(h, next(g for g in groups if h in g), region_right, page)
            for h in all_headers
        }
        bottoms = _compute_all_bottoms(page, all_headers, edges, rbottom)
        for group in groups:
            holdings.extend(
                _extract_table_holdings(
                    page, group, region_right, rbottom, local_whitelist, bottoms
                )
            )
    return _dedupe_holdings(holdings)


# --------------------------------------------------------------------------
# portfolio table header detection
# --------------------------------------------------------------------------


def _adjacent(words, anchor, text, max_gap=10, y_tol=2):
    for o in words:
        if o["text"] == text and abs(o["top"] - anchor["top"]) <= y_tol:
            gap = o["x0"] - anchor["x1"]
            if 0 <= gap <= max_gap:
                return o
    return None


def _find_header_anchors(page, top_bound, bottom_bound):
    """Locate every "Company Name ... % of assets" portfolio-table header
    instance within [top_bound, bottom_bound), returning one dict per
    instance with the x0 of each sub-column that's present. A scheme
    card can contain more than one such instance -- side by side (a
    two-column spread of one table) and/or stacked vertically
    (distinct tables, e.g. an equity table followed by a debt table
    further down the same page)."""
    words = [
        w
        for w in _page_words(page)
        if top_bound - 1 <= float(w["top"]) < bottom_bound
    ]
    anchors = []
    for w in words:
        if w["text"] != "Company":
            continue
        name_w = _adjacent(words, w, "Name")
        if not name_w:
            continue
        # Sub-column labels ("No. of shares", "% of assets", "Ratings", ...)
        # can appear either above or below the "Company Name" line itself,
        # and can wrap across several sub-lines, so scan a generous band
        # in both directions.
        band = [o for o in words if w["top"] - 15 <= float(o["top"]) <= w["top"] + 55]
        right_band = [o for o in band if float(o["x0"]) >= w["x0"] - 5]

        pct_x0 = None
        for pw in sorted(right_band, key=lambda o: o["x0"]):
            # Tight kerning sometimes glues the "%" glyph onto the
            # previous word ("Value%") with no separating space --
            # treat a trailing "%" the same as a standalone one,
            # approximating its column position from the token's
            # right edge.
            if pw["text"] == "%":
                pct_candidate_x0 = float(pw["x0"])
            elif len(pw["text"]) > 1 and pw["text"].endswith("%"):
                pct_candidate_x0 = float(pw["x1"]) - 8
            else:
                continue
            of_w = _adjacent(band, pw, "of", max_gap=8)
            if not of_w:
                continue
            # "assets"/"Assets"/"NAV" is either right next to "of" on the
            # same line, or wraps onto the sub-line below, column-aligned
            # under "% of" rather than horizontally adjacent to it.
            asset_w = _adjacent(band, of_w, "assets", max_gap=10) or _adjacent(
                band, of_w, "Assets", max_gap=10
            ) or _adjacent(band, of_w, "NAV", max_gap=10)
            if not asset_w:
                asset_w = next(
                    (
                        o
                        for o in band
                        if o["text"] in ("assets", "Assets", "NAV")
                        and 2 < o["top"] - of_w["top"] <= 12
                        and abs(o["x0"] - pw["x0"]) <= 20
                    ),
                    None,
                )
            if asset_w:
                pct_x0 = pct_candidate_x0
                break
        if pct_x0 is None:
            continue

        rating_x0 = None
        for rw in band:
            if rw["text"] in ("Ratings", "Rating") and rw["x0"] > w["x0"]:
                if rating_x0 is None or rw["x0"] < rating_x0:
                    rating_x0 = float(rw["x0"])

        deriv_x0 = None
        for dw in band:
            if dw["text"] == "Outstanding" and dw["x0"] > w["x0"]:
                if deriv_x0 is None or dw["x0"] < deriv_x0:
                    deriv_x0 = float(dw["x0"])

        # x0 of the "Market Value" column label, used to draw a precise
        # boundary between it and the "% of assets" column -- short
        # right-aligned figures in either column can otherwise land
        # close enough together that a fixed margin misclassifies one
        # column's number as belonging to the other.
        value_x0 = None
        for vw in band:
            if vw["text"] == "Value" and w["x0"] < vw["x0"] < pct_x0:
                if value_x0 is None or vw["x0"] > value_x0:
                    value_x0 = float(vw["x0"])

        # x0 of the "No. of shares" column label, when present -- lets
        # the name column stop precisely at the shares column instead
        # of stretching all the way to the rating/pct column (which
        # would otherwise swallow the shares-count and market-value
        # figures into the company-name text).
        shares_x0 = None
        for sw in band:
            if sw["text"] in ("shares", "Shares") and w["x0"] < sw["x0"] < pct_x0:
                if shares_x0 is None or sw["x0"] < shares_x0:
                    shares_x0 = float(sw["x0"])

        anchors.append(
            {
                "top": float(w["top"]),
                "name_x0": float(w["x0"]),
                "rating_x0": rating_x0,
                "pct_x0": pct_x0,
                "deriv_x0": deriv_x0,
                "value_x0": value_x0,
                "shares_x0": shares_x0,
            }
        )
    anchors.sort(key=lambda a: (round(a["top"], 0), a["name_x0"]))
    return anchors


def _group_anchor_tables(anchors, top_tol=4):
    """Cluster header anchors into table instances: anchors that share
    (near enough) the same top are siblings of one wrapped two-column
    table and are read left group fully, then right group fully;
    anchors at a distinctly lower top start a new, independent table
    (own column layout, own bucket-tracking state)."""
    groups = []
    for a in anchors:
        if groups and abs(groups[-1][-1]["top"] - a["top"]) <= top_tol:
            groups[-1].append(a)
        else:
            groups.append([a])
    for g in groups:
        g.sort(key=lambda a: a["name_x0"])
    groups.sort(key=lambda g: g[0]["top"])
    return groups


# --------------------------------------------------------------------------
# scheme segmentation (public entry point)
# --------------------------------------------------------------------------


def segment_schemes(pdf):
    """Return {scheme_name: [(page_index, region_top, region_bottom), ...]}
    in document order.

    Every scheme's card location is read from the document's own Table
    of Contents (name, short code, starting page) rather than pattern-
    matching each page's own multi-line, inconsistently-wrapped
    heading. The starting page is cross-checked against the card's own
    short code actually being present there (and, failing that,
    searched for on nearby pages) so a discrepancy between the ToC's
    printed page numbers and physical PDF page positions cannot
    silently point at the wrong scheme. A trailing continuation page
    (a portfolio table with no scheme heading of its own) is appended
    automatically if present, though this month's factsheet has none.
    """
    registry, order = _get_toc_registry(pdf)
    known_abbrs = set(registry)
    schemes = {}
    name_order = []

    for abbr in order:
        info = registry[abbr]
        page_idx = info["page"] - 1
        if not (0 <= page_idx < len(pdf.pages)):
            continue

        match = None
        for pi in (
            page_idx,
            page_idx - 1,
            page_idx + 1,
            page_idx - 2,
            page_idx + 2,
            page_idx - 3,
            page_idx + 3,
        ):
            if not (0 <= pi < len(pdf.pages)):
                continue
            regions = _scheme_card_regions(pdf.pages[pi], known_abbrs)
            hit = next((r for r in regions if r[2] == abbr), None)
            if hit:
                match = (pi, hit)
                break

        if match is None:
            # Could not confirm the card by its own short code anywhere
            # nearby -- fall back to the whole ToC-listed page rather
            # than dropping the scheme entirely.
            page = pdf.pages[page_idx]
            entries = [(page_idx, 0.0, page.height)]
        else:
            found_idx, (rtop, rbot, _abbr) = match
            entries = [(found_idx, rtop, rbot)]

            # Continuation-page safety net: if the very next physical
            # page carries a portfolio-style table header but starts no
            # scheme card of its own, it's a continuation of this one.
            next_idx = found_idx + 1
            while next_idx < len(pdf.pages):
                npage = pdf.pages[next_idx]
                if _scheme_card_regions(npage, known_abbrs):
                    break
                if not _find_header_anchors(npage, 0, npage.height):
                    break
                entries.append((next_idx, 0.0, npage.height))
                next_idx += 1

        name = info["name"]
        if name not in schemes:
            schemes[name] = entries
            name_order.append(name)
        else:
            schemes[name].extend(entries)

    return {name: schemes[name] for name in name_order}


# --------------------------------------------------------------------------
# left-panel ("Fund Information") metadata extraction
# --------------------------------------------------------------------------


def _metadata_text(page, top_bound, bottom_bound, boundary):
    """Reconstruct this scheme card's left "Fund Information" column
    as plain text, bounded to the left of wherever its portfolio
    table(s) start and to the card's own [top_bound, bottom_bound)
    vertical slice."""
    words = [
        w
        for w in _page_words(page)
        if top_bound - 1 <= float(w["top"]) < bottom_bound
        and float(w["x1"]) <= boundary
    ]
    rows = _cluster_rows(words, y_tol=1.6)
    rows.sort(key=lambda r: r["top"])
    lines = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        lines.append(" ".join(w["text"] for w in ws))
    return "\n".join(lines)


# Structural, SEBI-disclosure-standard section-label vocabulary used
# only to know where one metadata field's value ends and the next
# field begins -- the same stable, non-scheme-specific role as
# canara_robeco.py's own regex label boundaries.
_METADATA_LABEL_LINES = (
    "TYPE OF SCHEME",
    "SCHEME CATEGORY",
    "SCHEME CHARACTERISTICS",
    "INVESTMENT OBJECTIVE",
    "DATE OF ALLOTMENT",
    "FUND MANAGER",
    "BENCHMARK",
    "NAV AS OF",
    "FUND SIZE",
    "TURNOVER",
    "MATURITY & YIELD",
    "RESIDUAL MATURITY",
    "MODIFIED DURATION",
    "MACAULAY DURATION",
    "ANNUALISED PORTFOLIO",
    "RATIO#",
    "BASE EXPENSE",
    "EXPENSE RATIO",
    "MINIMUM INVESTMENT",
    "ADDITIONAL INVESTMENT",
    "LOAD STRUCTURE",
    "ENTRY LOAD",
    "EXIT LOAD",
)


def _capture_metadata_block(lines, label_prefixes):
    """Return the (possibly multi-line, re-joined) value following the
    first line that starts with one of label_prefixes, stopping at the
    next line that looks like any other section label."""
    start = None
    for i, line in enumerate(lines):
        upper = line.strip().upper()
        if any(upper.startswith(p) for p in label_prefixes):
            start = i
            break
    if start is None:
        return None
    # The label line itself can wrap ("FUND MANAGER(S) (FOR FRANKLIN
    # U.S." / "OPPORTUNITIES EQUITY ACTIVE FUND OF FUNDS)") -- keep
    # consuming lines as part of the label while its own parentheses
    # are still open, so that trailing qualifier text doesn't get
    # mistaken for the field's value.
    balance = lines[start].count("(") - lines[start].count(")")
    while balance > 0 and start + 1 < len(lines):
        start += 1
        balance += lines[start].count("(") - lines[start].count(")")
    value_lines = []
    for line in lines[start + 1 :]:
        upper = line.strip().upper()
        if any(upper.startswith(p) for p in _METADATA_LABEL_LINES):
            break
        if not line.strip():
            continue
        if line.strip().startswith("#") or line.strip().startswith("*"):
            break
        value_lines.append(line.strip())
    return value_lines


def extract_benchmark(metadata_text):
    if not metadata_text:
        return None
    lines = metadata_text.split("\n")
    value_lines = _capture_metadata_block(lines, ("BENCHMARK",))
    if not value_lines:
        return None
    value = _clean(" ".join(value_lines))
    return value or None


def extract_isin(metadata_text):
    # Franklin Templeton's factsheet does not print an ISIN alongside
    # a scheme's Fund Information panel.
    return ""


def _split_outside_parens(text):
    """Split on "," or " & " that are NOT inside parentheses -- a
    qualifier like "(w.e.f January 12, 2026)" has its own internal
    comma that must not be treated as a name separator."""
    pieces = []
    buf = []
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and ch == ",":
            pieces.append("".join(buf))
            buf = []
        elif depth == 0 and text[i : i + 3] == " & ":
            pieces.append("".join(buf))
            buf = []
            i += 2
        else:
            buf.append(ch)
        i += 1
    pieces.append("".join(buf))
    return pieces


def extract_fund_managers(metadata_text):
    if not metadata_text:
        return []
    lines = metadata_text.split("\n")
    value_lines = _capture_metadata_block(lines, ("FUND MANAGER",))
    if not value_lines:
        return []
    # A qualifier line ("(w.e.f ...)", "(dedicated for making
    # investments for Foreign Securities)") that wraps onto its own
    # physical line belongs to the manager named on the previous
    # line; other lines are separate manager entries in their own
    # right even with no comma/"&" between them.
    merged_lines = []
    for line in value_lines:
        prev_open = bool(merged_lines) and (
            merged_lines[-1].count("(") > merged_lines[-1].count(")")
        )
        if merged_lines and (line.lstrip().startswith("(") or prev_open):
            merged_lines[-1] = merged_lines[-1] + " " + line
        else:
            merged_lines.append(line)

    pieces = []
    for line in merged_lines:
        pieces.extend(_split_outside_parens(line))
    pieces = [re.sub(r"^\s*&\s*|\s*&\s*$", "", p).strip() for p in pieces]
    managers = []
    seen = set()
    for piece in pieces:
        piece = _clean(piece)
        if not piece:
            continue
        name = re.sub(r"\s*\(.*$", "", piece).strip()
        note_m = re.search(r"\((.*)\)\s*$", piece)
        sleeve = None
        if note_m:
            note = note_m.group(1)
            if re.search(r"\bdebt\b", note, re.IGNORECASE):
                sleeve = "Debt"
            elif re.search(r"\bequity\b", note, re.IGNORECASE):
                sleeve = "Equity"
            elif re.search(r"\bforeign\b|\boverseas\b", note, re.IGNORECASE):
                sleeve = "Overseas"
        if not name or len(name) < 3 or name.upper() in ("W E F", "WEF"):
            continue
        key = (name.lower(), sleeve)
        if key in seen:
            continue
        seen.add(key)
        managers.append({"role": "Fund Manager", "name": name, "sleeve": sleeve})
    return managers


# --------------------------------------------------------------------------
# additional-benchmark lookup (from the separate "Scheme Performance"
# section, matched back to each scheme by its short code)
# --------------------------------------------------------------------------

_SCHEME_PERF_STOP_WORDS = (
    "Compounded",
    "Since",
    "Last",
    "Current",
    "NAV",
    "Inception",
    "Fund",
    "Simple",
)


def _additional_benchmarks_on_page(page, known_abbrs):
    """Find every "<ABBR> ... AB: <value>" legend on this Scheme
    Performance page and return {abbr: value}.

    The abbreviation code, the "B:"/"AB:" labels and their values all
    sit on what is visually one printed row, but PDF text extraction
    can give them each a slightly different baseline ("top") -- up to
    a couple of points apart -- so anchoring on the abbreviation
    token with a narrow tolerance can miss "AB:" (or grab the wrong
    row) entirely. Anchoring on "AB:" itself and using a slightly
    wider, symmetric row tolerance is far more robust.

    The value can also wrap onto the next printed line (e.g.
    "CRISIL 1 Year" / "T-Bill Index"). That shared next line
    routinely carries wrapped continuations for several *different*
    fields at once -- a T1/T2 tier, this scheme's own "B:" value, and
    even a neighbouring scheme's legend sharing the row -- so picking
    up the right fragment means: (a) only look in a narrow x-band
    re-aligned under "AB:"'s own column (continuations restart at
    their field's column, not wherever the previous line happened to
    end), (b) still require word-to-word horizontal contiguity within
    that fragment, and (c) still stop at the same boilerplate/stop
    words used for the same-row capture. Anything that doesn't cleanly
    satisfy all three is left untaken -- a truncated value is a safer
    failure mode than a wrong one for production data.
    """
    words = _page_words(page)
    results = {}
    row_tol = 3.5

    for ab_w in [
        w
        for w in words
        if w["text"] == "AB:" or w["text"].startswith("AB:") or w["text"].startswith("B/AB:")
    ]:
        ab_top = float(ab_w["top"])
        ab_x0 = float(ab_w["x0"])
        # "AB:" is occasionally glued onto the following word with no
        # space ("AB:Crisil"), and a scheme whose primary and
        # additional benchmark are identical is sometimes labelled
        # "B/AB:" instead of separate "B:"/"AB:" tokens. Either way,
        # any text glued onto the label token itself is the start of
        # the value.
        inline_prefix = re.sub(r"^(?:B/)?AB:", "", ab_w["text"]).strip()

        # The abbreviation code can sit a few points above OR below
        # the "AB:" token's own baseline depending on how that
        # particular block wraps (observed both ways across
        # different schemes), so use a looser vertical window, and
        # pick whichever candidate is vertically closest, just for
        # identifying *which* scheme this legend belongs to.
        abbr_candidates = [
            w
            for w in words
            if w["text"] in known_abbrs
            and float(w["x0"]) < ab_x0 + 5
            and abs(float(w["top"]) - ab_top) <= 7.5
        ]
        if not abbr_candidates:
            continue
        abbr_w = min(abbr_candidates, key=lambda w: abs(float(w["top"]) - ab_top))
        abbr = abbr_w["text"]
        if abbr in results:
            continue

        same_row = [w for w in words if abs(float(w["top"]) - ab_top) <= row_tol]
        tail = sorted(
            (w for w in same_row if float(w["x0"]) > ab_x0), key=lambda w: w["x0"]
        )
        value_words = [inline_prefix] if inline_prefix else []
        prev_x1 = ab_w["x1"]
        for w in tail:
            if w["text"] in _SCHEME_PERF_STOP_WORDS or w["text"] in known_abbrs:
                break
            if w["text"] in ("B:", "AB:") or w["text"].startswith("AB:") or w["text"].startswith("B/AB:"):
                break
            # A large horizontal gap means this word belongs to an
            # unrelated field or a different scheme's own card sharing
            # the same visual row (this table packs several scheme
            # blocks side by side), not a continuation of this value.
            if float(w["x0"]) - prev_x1 > 25:
                break
            value_words.append(w["text"])
            prev_x1 = float(w["x1"])

        # Also check for a wrapped continuation directly below "AB:"'s
        # own column, even when the same-row capture already stopped
        # cleanly (e.g. on a table-header word like "Last" that
        # belongs to an unrelated column further along the same
        # shared row) -- the value's own continuation is a separate
        # question from what other fields happen to share that row.
        wrap_candidates = sorted(
            (
                w
                for w in words
                if row_tol < float(w["top"]) - ab_top <= 9
                and ab_x0 - 8 <= float(w["x0"]) <= ab_x0 + 30
            ),
            key=lambda w: w["x0"],
        )
        prev_x1 = ab_x0
        for w in wrap_candidates:
            if w["text"] in _SCHEME_PERF_STOP_WORDS or w["text"] in known_abbrs:
                break
            if w["text"] in ("B:", "AB:") or w["text"].startswith("AB:") or w["text"].startswith("B/AB:"):
                break
            if float(w["x0"]) - prev_x1 > 25:
                break
            value_words.append(w["text"])
            prev_x1 = float(w["x1"])

        value = _strip_trailing_footnote_symbols(_clean(" ".join(value_words)))
        if value:
            results[abbr] = value
    return results


def _build_additional_benchmark_map(pdf, known_abbrs):
    result = {}
    for page in pdf.pages:
        text = page.extract_text() or ""
        if "SCHEME PERFORMANCE" not in text.upper():
            continue
        for abbr, value in _additional_benchmarks_on_page(page, known_abbrs).items():
            result.setdefault(abbr, value)
    return result


def _get_additional_benchmark_map(pdf, known_abbrs):
    cache = getattr(pdf, "_franklin_templeton_addl_benchmark_cache", None)
    if cache is not None:
        return cache
    cache = _build_additional_benchmark_map(pdf, known_abbrs)
    try:
        pdf._franklin_templeton_addl_benchmark_cache = cache
    except Exception:
        pass
    return cache


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------


def extract_scheme_fields(pdf, page_idxs):
    """Aggregate every field the framework expects for one scheme,
    given the list of (page_index, region_top, region_bottom) entries
    segment_schemes() assigned to it."""
    registry, order = _get_toc_registry(pdf)
    known_abbrs = set(registry)
    global_whitelist = _get_global_bucket_whitelist(pdf) | _get_global_industry_whitelist(
        pdf, known_abbrs
    )

    benchmark = None
    fund_managers = []
    abbr_found = None

    for entry in page_idxs:
        page_idx, rtop, rbot = entry
        page = pdf.pages[page_idx]
        if rtop is None:
            rtop, rbot = 0.0, page.height

        if abbr_found is None:
            regions = _scheme_card_regions(page, known_abbrs)
            hit = next((r for r in regions if abs(r[0] - rtop) <= 3), None)
            if hit:
                abbr_found = hit[2]

        anchors = _find_header_anchors(page, rtop, rbot)
        boundary = min((a["name_x0"] for a in anchors), default=None)
        if boundary is None:
            boundary = page.width * 0.45
        metadata_text = _metadata_text(page, rtop, rbot, boundary - 8)

        # A compact single-column card (e.g. a Fund-of-Funds sharing a
        # page with another scheme) can additionally carry a second
        # metadata sidebar -- BENCHMARK / MINIMUM INVESTMENT -- to the
        # *right* of its portfolio table rather than only to the left
        # of it; pick that up too so BENCHMARK isn't missed just
        # because the layout put it on the other side of the table.
        if anchors:
            groups = _group_anchor_tables(anchors)
            region_right = page.width - 15
            right_zone_lo = max(
                _right_edge_for(h, g, region_right, page) for g in groups for h in g
            )
            side_words = [
                w
                for w in _page_words(page)
                if rtop - 1 <= float(w["top"]) < rbot
                and float(w["x0"]) > right_zone_lo
            ]
            side_rows = _cluster_rows(side_words, y_tol=1.6)
            side_rows.sort(key=lambda r: r["top"])
            side_lines = [
                " ".join(w["text"] for w in sorted(r["words"], key=lambda w: w["x0"]))
                for r in side_rows
            ]
            metadata_text = metadata_text + "\n" + "\n".join(side_lines)

        if benchmark is None:
            benchmark = extract_benchmark(metadata_text)
        if not fund_managers:
            fund_managers = extract_fund_managers(metadata_text)

    holdings = _extract_all_holdings(pdf, page_idxs, global_whitelist)

    additional_benchmark = None
    if abbr_found:
        addl_map = _get_additional_benchmark_map(pdf, known_abbrs)
        additional_benchmark = addl_map.get(abbr_found)

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": extract_isin(None),
        "fund_managers": fund_managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }