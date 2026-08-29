"""
Choice Mutual Fund factsheet extractor.

Mirrors the calling contract of amc/canara_robeco.py (segment_schemes /
extract_scheme_fields, identical return schema) but every bit of the
internal parsing is specific to how Choice Mutual Fund lays out its
factsheet, which differs from Canara Robeco's in several structural
ways:

  * One scheme per page: a title line ("Choice <Scheme Name>") followed
    by a two-column layout -- a narrower "Fund Information" panel on
    the left (Type of Scheme, Investment Objective, Date of Allotment,
    Fund Manager(s), Fund Size, NAV, Load Structure, Benchmark, Expense
    Ratio, ...) and a "Scheme Portfolio" table on the right.
  * The portfolio table has only two columns of its own: "Name of
    Instrument/Issuer" (or, for a non-equity scheme such as a Gold ETF,
    "Asset / Holding") and a single "% to AUM" / "% of Assets" figure.
    There is no separate Rating or Market Cap sub-column at all -- so,
    unlike Canara Robeco, a row's classification (industry roll-up
    header vs. an actual instrument holding vs. a terminal, unbroken-
    down allocation such as "Cash, Cash Equivalents and Others") can't
    be read off a populated/blank sub-column. Instead Choice renders
    every roll-up/total row (the "EQUITY & EQUITY RELATED" line, each
    industry sub-header such as "Banks 30.04", the terminal "Cash,
    Cash Equivalents and Others" line and "Grand Total" itself) in a
    bold weight of the same font family, while genuine instrument rows
    are regular weight -- that font-weight signal is what this module
    keys off of instead.
  * The equity-style portfolio table is laid out in two side-by-side
    columns of holdings on the page (each with its own repeated "Name
    of Instrument/Issuer | % to AUM" header), read top-to-bottom in
    the left column and then top-to-bottom in the right column -- the
    same left-then-right multi-column reading order Canara Robeco
    uses, so that piece of coordinate logic is reused.
  * A small "Industry Allocation" horizontal bar chart is printed
    beneath the portfolio table on equity scheme pages and repeats
    every sector name/percentage a second time (this time suffixed
    with a literal "%" character, e.g. "30.04%", vs. the table's own
    plain "30.04") -- this module stops reading a column's words
    before that chart (and before the Riskometer panels) so none of
    it leaks into the holdings list as duplicate rows.
  * There is no separate "Performance for all Schemes" section keyed
    by an "##" superscript the way Canara Robeco has for its
    "additional benchmark" -- each Choice scheme page shows only its
    own primary benchmark, so `additional_benchmark` is always None
    here (the key is still returned for schema compatibility).

Nothing here is wired to a specific page number, month, or scheme
name -- everything is derived from on-page text/coordinates/fonts so
the same code keeps working on next month's factsheet.
"""

from __future__ import annotations

import bisect
import re

# --------------------------------------------------------------------------
# generic text/word helpers (shape mirrors canara_robeco.py; Choice-specific
# where the underlying table layout differs)
# --------------------------------------------------------------------------

_PUA_RE = re.compile(r"[\u2022\u25cf\u25aa\u25e6\u2023\u2043\ue000-\uf8ff]")
_WS_RE = re.compile(r"\s+")
_TRAILING_FOOTNOTE_RE = re.compile(r"[^A-Za-z0-9\s():&,/'\-]+$")

# Choice's portfolio table prints its own "% to AUM" / "% of Assets"
# figures as plain decimals with no percent sign (e.g. "30.04", "97.79").
# The only place a percent *sign* shows up on these pages is the
# duplicate "Industry Allocation" bar-chart labels (e.g. "30.04%"),
# which is exactly why this module never treats a percent-suffixed
# token as a table-row anchor -- doing so would double-count every
# sector as both a table row and a chart-label row.
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _clean(text):
    """Strip bullet glyphs / PUA glyphs and normalise whitespace."""
    if not text:
        return ""
    text = _PUA_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _strip_trailing_footnote_symbols(text):
    """Drop trailing footnote markers such as '*', '^^', '$' from a heading."""
    text = text.strip()
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_FOOTNOTE_RE.sub("", text).strip()
    return text


def _page_words(page):
    return (
        page.extract_words(
            x_tolerance=3,
            y_tolerance=1.5,
            keep_blank_chars=False,
            extra_attrs=["fontname"],
        )
        or []
    )


def _is_bold(word):
    return "bold" in (word.get("fontname") or "").lower()


def _cluster_rows(words, y_tol=1.6):
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


def _norm_key(text):
    """Loose normalisation for matching scheme names across sections."""
    text = text.upper()
    text = re.sub(r"\(FORMERLY[^)]*\)", " ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return _WS_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------
# scheme segmentation
# --------------------------------------------------------------------------

# Guards against picking up the AMC's own branding ("Choice Mutual
# Fund", "Choice AMC Private Limited") as if it were a scheme name --
# real scheme names are always "Choice <... > ETF/Fund" but never
# contain "AMC" or the literal phrase "Mutual Fund".
_BRAND_LINE_RE = re.compile(r"\bAMC\b|\bMUTUAL\s+FUND\b", re.IGNORECASE)

# Deliberately NOT using the shared config.HEADING_EXCLUDE list here: it
# is written for other AMCs' section titles (things like a bare
# "INDEX" table-of-contents page) and matching it as a substring, the
# way canara_robeco.py does, false-positives against genuine Choice
# scheme names such as "Choice Nifty 50 Index Fund" -- "INDEX" is a
# substring of "Index Fund". A page whose first line starts with
# "Choice " is unambiguous on its own in this factsheet (every
# non-scheme section/divider page starts with something else --
# verified against "Gold Market Review", "Equity Market Outlook",
# "How to Read...", "Disclaimer", "CIO COMMENTARY", the table of
# contents, etc.), so no additional keyword-exclusion list is needed;
# the brand-line guard below is the only extra precision required.


def _is_scheme_heading(line):
    line = line.strip()
    if not line or len(line) > 90:
        return False
    normalized = _strip_trailing_footnote_symbols(line)
    upper = normalized.upper()
    if not upper.startswith("CHOICE "):
        return False
    if _BRAND_LINE_RE.search(upper):
        return False
    return True


def _clean_scheme_name(line):
    name = _strip_trailing_footnote_symbols(line.strip())
    return _clean(name)


_NON_SCHEME_SECTION_RE = re.compile(
    r"^(?:How to Read|Disclaimer|Glossary|Gold Market Review|"
    r"Equity Market Outlook|Debt Market Review|CIO COMMENTARY|"
    r"Performance for all Schemes|Scheme Performance|Index\b)",
    re.IGNORECASE,
)


def segment_schemes(pdf):
    """Return {scheme_name: [page_index, ...]} in document order.

    Each Choice scheme starts with a "Choice <...>" heading as the
    first line of its page, carrying both the Fund Information panel
    and the Scheme Portfolio table. A scheme can in principle spill
    onto a following page (e.g. a very long portfolio list); such a
    continuation page won't repeat the heading, but it will still have
    its own "Name of Instrument/Issuer | % to AUM" (or "Asset /
    Holding | % of Assets") table header, which is what we key off of
    rather than any month/page-specific text.
    """
    schemes = {}
    order = []
    current = None

    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        lines = text.split("\n")
        first_line = lines[0] if lines else ""

        if _is_scheme_heading(first_line):
            name = _clean_scheme_name(first_line)
            if name not in schemes:
                schemes[name] = []
                order.append(name)
            current = name
            schemes[current].append(i)
            continue

        if current is None:
            continue
        if _NON_SCHEME_SECTION_RE.match(first_line.strip()):
            current = None
            continue
        if _find_portfolio_headers(page):
            schemes[current].append(i)
        else:
            # Any other page (disclaimers, section dividers, ...) ends
            # the current scheme's run of pages.
            current = None

    return {name: schemes[name] for name in order}


# --------------------------------------------------------------------------
# portfolio-table header detection
# --------------------------------------------------------------------------


def _find_portfolio_headers(page):
    """Locate one or two "<Name column> | % <to/of> <AUM/Assets>" header
    groups on a scheme page, returning left->right x0 boundaries for the
    name column and the % column so holdings rows can be sliced out by
    coordinate. Two groups means a two-column holdings layout (the
    equity-style schemes); one group is typical of a simple asset-list
    table (e.g. a Gold ETF's "Asset / Holding" table)."""
    words = _page_words(page)
    rows = _cluster_rows(words, y_tol=2.0)

    headers = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: float(w["x0"]))
        texts = [w["text"] for w in ws]
        n = len(ws)
        i = 0
        while i < n:
            name_x0 = None
            j = None
            if (
                texts[i] == "Name"
                and i + 2 < n
                and texts[i + 1] == "of"
                and texts[i + 2].startswith("Instrument")
            ):
                name_x0 = float(ws[i]["x0"])
                j = i + 3
            elif (
                texts[i] == "Asset"
                and i + 2 < n
                and texts[i + 1] == "/"
                and texts[i + 2].startswith("Holding")
            ):
                name_x0 = float(ws[i]["x0"])
                j = i + 3
            if name_x0 is None:
                i += 1
                continue

            nav_x0 = None
            k = j
            while k < n - 2:
                if (
                    texts[k] == "%"
                    and texts[k + 1] in ("to", "of")
                    and texts[k + 2] in ("AUM", "Assets")
                ):
                    nav_x0 = float(ws[k]["x0"])
                    break
                k += 1

            if nav_x0 is not None:
                headers.append({"top": r["top"], "name_x0": name_x0, "nav_x0": nav_x0})
            i = j

    headers.sort(key=lambda h: h["name_x0"])
    return headers


# --------------------------------------------------------------------------
# left-column ("Fund Information") metadata extraction
# --------------------------------------------------------------------------


def _metadata_text(page, headers):
    """Reconstruct the left "Fund Information" column as plain text, bounded
    to the left of wherever the portfolio table starts."""
    if headers:
        boundary = min(h["name_x0"] for h in headers) - 8
    else:
        boundary = 207
    words = [w for w in _page_words(page) if float(w["x1"]) <= boundary]
    rows = _cluster_rows(words, y_tol=1.6)
    rows.sort(key=lambda r: r["top"])
    lines = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        lines.append(" ".join(w["text"] for w in ws))
    return "\n".join(lines)


_BENCHMARK_RE = re.compile(
    r"\bBenchmark\b\s*\n\s*(.+?)\n\s*(?:Expense Ratio|Scheme Statistics|"
    r"Riskometer|Note\b|ISIN\b)",
    re.IGNORECASE,
)


def extract_benchmark(metadata_text):
    if not metadata_text:
        return None
    m = _BENCHMARK_RE.search(metadata_text)
    if not m:
        return None
    value = _clean(m.group(1).replace("\n", " "))
    return value or None


_ISIN_RE = re.compile(r"\bISIN\s*[:\-]?\s*([A-Z]{2}[A-Z0-9]{9}\d)\b")


def extract_isin(metadata_text):
    if not metadata_text:
        return ""
    m = _ISIN_RE.search(metadata_text)
    return m.group(1) if m else ""


_MANAGER_BLOCK_RE = re.compile(
    r"\bFund\s+Manager\(s\)\s*\n(.+?)\n\s*Fund\s+Size\b",
    re.IGNORECASE | re.DOTALL,
)
_MANAGER_PAIR_RE = re.compile(
    r"([A-Za-z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*){0,4})\s*\n"
    r"\(Managing Since\s*([^)]+)\)",
)


def extract_fund_managers(metadata_text):
    if not metadata_text:
        return []
    m = _MANAGER_BLOCK_RE.search(metadata_text)
    if not m:
        return []
    block = m.group(1)

    managers = []
    seen = set()
    for mm in _MANAGER_PAIR_RE.finditer(block):
        name = _clean(mm.group(1))
        if not name or len(name) < 3:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        managers.append({"role": "Fund Manager", "name": name, "sleeve": None})
    return managers


# --------------------------------------------------------------------------
# portfolio / holdings extraction
# --------------------------------------------------------------------------

_STOP_ROW_RE = re.compile(r"^grand\s+total\b", re.IGNORECASE)


def _compute_bottom_bound(page, header_top, x_lo, x_hi):
    """Where *this* holdings column's real data ends, ahead of the
    duplicate "Industry Allocation" bar-chart labels and the
    Riskometer panels that sit below the table on a Choice scheme
    page.

    Choice's two side-by-side holdings columns do not end at the same
    vertical position -- the left column's list routinely runs on
    below the point where the right column's "Industry Allocation"
    chart already begins (the chart sits in the right column's own
    horizontal band, not below both columns evenly) -- so this bound
    is computed per column, restricted to that column's own x-range,
    rather than once for the whole page."""
    words = _page_words(page)
    tops = []
    for w in words:
        top = float(w["top"])
        x0 = float(w["x0"])
        if top <= header_top + 5 or not (x_lo <= x0 < x_hi):
            continue
        text = w["text"]
        if text.upper() == "RISKOMETER" or text == "Riskometer":
            tops.append(top)
        elif text == "Industry":
            for ow in words:
                if (
                    ow["text"] == "Allocation"
                    and abs(float(ow["top"]) - top) <= 2
                    and 0 < float(ow["x0"]) - float(w["x1"]) <= 10
                ):
                    tops.append(top)
                    break
    return min(tops) if tops else float(page.height)


def _rows_for_group(page, header, right_edge, bottom_bound):
    """All (company, pct, bold) rows in one column of the holdings
    table, top to bottom. A row's own name can wrap across two (or
    occasionally three) lines, and its "%" figure is typographically
    centred on that wrapped block rather than pinned to its last line
    -- so the closest anchor by |top - anchor_top|, not the next one
    at/after a word's own top, is what correctly reunites a wrapped
    name with its own value instead of bleeding into the row above or
    below (same technique as Canara Robeco's equivalent table reader)."""
    words = [
        w
        for w in _page_words(page)
        if header["top"] - 3 <= float(w["top"]) < bottom_bound - 0.5
        and header["name_x0"] - 15 <= float(w["x0"]) < right_edge
    ]
    words = [w for w in words if float(w["top"]) > header["top"] + 3]

    anchor_margin = 8
    anchors = sorted(
        (
            w
            for w in words
            if _NUM_RE.match(w["text"])
            and float(w["x0"]) >= header["nav_x0"] - anchor_margin
        ),
        key=lambda w: float(w["top"]),
    )
    if not anchors:
        return []
    anchor_tops = [float(a["top"]) for a in anchors]

    buckets = [[] for _ in anchors]
    max_lookback = 9.0
    n_anchors = len(anchors)
    for w in words:
        wtop = float(w["top"])
        idx = bisect.bisect_left(anchor_tops, wtop)
        best_idx, best_dist = None, None
        for cand in (idx - 1, idx):
            if 0 <= cand < n_anchors:
                dist = abs(anchor_tops[cand] - wtop)
                if best_dist is None or dist < best_dist:
                    best_idx, best_dist = cand, dist
        if best_idx is None or best_dist > max_lookback:
            continue
        buckets[best_idx].append(w)

    def _join(ws):
        return _clean(
            " ".join(w["text"] for w in sorted(ws, key=lambda w: (w["top"], w["x0"])))
        )

    rows = []
    for anchor, bucket in zip(anchors, buckets):
        name_words = [
            w
            for w in bucket
            if w is not anchor and float(w["x0"]) < header["nav_x0"] - anchor_margin
        ]
        rows.append(
            {
                "top": float(anchor["top"]),
                "company": _join(name_words),
                "pct": anchor["text"],
                "bold": _is_bold(anchor),
            }
        )
    rows.sort(key=lambda r: r["top"])
    return rows


def _raw_portfolio_rows(page):
    """All raw table rows on this page, left group before right group,
    each top-to-bottom -- i.e. correct reading order for a two-column
    layout."""
    headers = _find_portfolio_headers(page)
    if not headers:
        return []

    all_rows = []
    for gi, header in enumerate(headers):
        if gi + 1 < len(headers):
            right_edge = (header["nav_x0"] + headers[gi + 1]["name_x0"]) / 2
        else:
            right_edge = page.width
        x_lo = header["name_x0"] - 15
        bottom_bound = _compute_bottom_bound(page, header["top"], x_lo, right_edge)
        rows = _rows_for_group(page, header, right_edge, bottom_bound)
        for row in rows:
            if _STOP_ROW_RE.match(row["company"]):
                break
            all_rows.append(row)
    return all_rows


def _classify_rows(raw_rows):
    """Walk the raw rows top-to-bottom, tracking which equity-industry
    group we're in, and emit clean holdings.

    Every roll-up/total row on a Choice portfolio table -- the top-level
    "EQUITY & EQUITY RELATED" line, each industry sub-header ("Banks
    30.04"), and any terminal, never-broken-down allocation line such
    as "Cash, Cash Equivalents and Others" -- is printed in the same
    bold weight; genuine instrument rows are regular weight. Bold rows
    are therefore never emitted as holdings *directly* -- except when a
    bold row turns out to be a leaf with nothing itemised beneath it
    (recognised because the very next row is not a regular-weight
    instrument row), in which case its own percentage is the only
    figure that will ever represent that slice of the portfolio and it
    is emitted as a holding with no sector.
    """
    holdings = []
    current_industry = ""
    n = len(raw_rows)

    for idx, row in enumerate(raw_rows):
        company = row["company"]
        if not company:
            continue
        pct = row["pct"]

        if not row["bold"]:
            # Regular weight -- an actual instrument/asset holding.
            holdings.append(
                {
                    "company": company,
                    "sector": current_industry,
                    "pct_to_net_assets": pct,
                }
            )
            continue

        # Bold weight: either a top-level roll-up ("EQUITY & EQUITY
        # RELATED", fully upper-case), an industry sub-header that has
        # regular-weight instrument rows following it, or a terminal
        # bold leaf (e.g. "Cash, Cash Equivalents and Others") whose
        # own percentage is never itemised any further.
        if company == company.upper() and re.search(r"[A-Za-z]", company):
            # Top-level asset-class roll-up -- its own % double-counts
            # the rows beneath it, so it is never emitted, and it does
            # not become a sector label either.
            continue

        next_row = raw_rows[idx + 1] if idx + 1 < n else None
        has_regular_child = (
            bool(next_row) and next_row["company"] and not next_row["bold"]
        )
        if has_regular_child:
            current_industry = company
            continue

        holdings.append({"company": company, "sector": "", "pct_to_net_assets": pct})

    return holdings


def extract_holdings(page):
    raw_rows = _raw_portfolio_rows(page)
    return _classify_rows(raw_rows)


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------


def extract_scheme_fields(pdf, page_idxs):
    """Aggregate every field the framework expects for one scheme, given
    the list of page indices segment_schemes() assigned to it."""
    benchmark = None
    isin = ""
    fund_managers = []
    holdings = []

    for pi in page_idxs:
        page = pdf.pages[pi]
        headers = _find_portfolio_headers(page)
        metadata_text = _metadata_text(page, headers)

        if benchmark is None:
            benchmark = extract_benchmark(metadata_text)
        if not isin:
            found = extract_isin(metadata_text)
            if found:
                isin = found
        if not fund_managers:
            managers = extract_fund_managers(metadata_text)
            if managers:
                fund_managers = managers

        if headers:
            holdings.extend(extract_holdings(page))

    # Choice's factsheet shows only each scheme's own primary benchmark
    # on its own page -- there is no separate "Performance for all
    # Schemes" section (keyed by an "##" superscript, as in Canara
    # Robeco) from which a distinct additional benchmark could be
    # resolved, so this is always None. The key is still returned so
    # the output schema matches other AMC extractors exactly.
    additional_benchmark = None

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": fund_managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
