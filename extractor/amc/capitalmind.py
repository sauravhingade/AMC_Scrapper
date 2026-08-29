"""
Capitalmind Mutual Fund factsheet extractor.

Mirrors the calling contract of amc/canara_robeco.py (segment_schemes /
extract_scheme_fields, identical return schema) but every bit of the
internal parsing is specific to how Capitalmind lays out its factsheet,
which is structurally quite different from Canara Robeco's:

  * A scheme spans several consecutive pages (Investment Objective +
    Scheme Details, one or more Portfolio pages, a Portfolio Changes
    page, sometimes a Sector Allocation page). Every one of those pages
    repeats the scheme's name as the very first line, so segmentation
    is a simple "group consecutive pages with the same first line"
    scan -- no page numbers or month text involved.

  * The portfolio table has no "Rating"/"Market Cap" sub-columns for
    equity holdings the way Canara Robeco does. Instead, holdings are
    grouped under plain-text bucket/category headers ("Equity & Equity
    related", "(a) Listed / awaiting listing on Stock Exchange(s)",
    "Money Market Instruments", "Certificate of Deposit", "Treasury
    Bill", "Reverse Repo / TREPS", "Preference Shares", "Exchange
    Traded Funds", "Mutual Fund Units", "Others", ...). These headers
    never carry their own "% to NAV" figure -- only the instruments
    beneath them (and the "Sub Total"/"Total" roll-up rows) do -- so
    they are reconstructed by walking the raw per-line rows rather than
    by an anchor/percentage-driven bucketing pass.

  * Three distinct header shapes appear across schemes:
      - "Issuer Name | % to NAV" (Flexi Cap, Multi Asset Allocation),
        printed as two independent side-by-side newspaper columns when
        the list is long -- read left column fully, then right column
        fully, which happens to reproduce the logical top-to-bottom
        order of the underlying list.
      - "Name of the Instrument / Issuer | Rating | % to NAV" (Liquid
        Fund) -- debt holdings, where the "sector" field is populated
        from the Rating column exactly like Canara Robeco's debt rows.
      - "Name of the Instrument / Issuer | % to NAV | % to NAV
        (Derivative)" (Arbitrage Fund) -- the derivative leg is not
        part of the shared output schema, so only the primary (long)
        leg's % to NAV is kept.

  * Instrument names that wrap across two or three lines put their "%
    to NAV" figure on the *first* line, with the remaining word(s)
    continuing, value-less, on the following line(s) -- the opposite
    convention from a bucket header, which is value-less on every one
    of its own lines. Both cases are handled by one generic "merge
    consecutive value-less lines into whichever value-row/bucket they
    continue" pass.

  * "Benchmark:" and "(Additional Benchmark)" are not printed in a
    predictable single line of plain text -- the surrounding
    multi-column layout means pdfplumber's linear text extraction
    regularly interleaves unrelated columns/cells into the same
    "line". Both are therefore pulled out using word coordinates
    (locate the label, then collect only the words that fall inside
    its own column) rather than a line-oriented regex.

Nothing here is wired to a specific page number, month, or scheme
name -- everything is derived from on-page text/coordinates so the
same code keeps working on next month's factsheet, including for
scheme categories not present in this particular issue.
"""

from __future__ import annotations

import re

try:  # pragma: no cover - exercised inside the real extraction framework
    from ..config import HEADING_EXCLUDE
except ImportError:  # pragma: no cover - fallback for standalone use/testing
    HEADING_EXCLUDE = set()

# --------------------------------------------------------------------------
# generic text/word helpers
# --------------------------------------------------------------------------

_PUA_RE = re.compile(r"[\u2022\u25cf\u25aa\u25e6\u2023\u2043\ue000-\uf8ff]")
_WS_RE = re.compile(r"\s+")
_TRAILING_FOOTNOTE_RE = re.compile(r"[^A-Za-z0-9\s():&,/'\-]+$")
_PCT_RE = re.compile(r"^-?\d+(?:\.\d+)?%$")
_NUMERIC_TOKEN_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?%?$")


def _clean(text):
    """Strip bullet glyphs / PUA glyphs and normalise whitespace."""
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


def _cluster_rows(words, y_tol=1.6):
    """Group words into visual text rows by their vertical position."""
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


def _join_dehyphenated(texts):
    """Join word fragments, re-joining a PDF line-break hyphen (e.g. "Eq-" +
    "uity)" -> "Equity)") instead of leaving a stray "-" behind."""
    out = []
    for t in texts:
        if out and out[-1].endswith("-") and not out[-1].endswith("--"):
            out[-1] = out[-1][:-1] + t
        else:
            out.append(t)
    return " ".join(out)


def _find_adjacent_pair(words, first, second, max_gap=12):
    """Locate two words appearing back-to-back on the same line, e.g.
    "Fund" + "Managers" -> (x0, top) of the first word."""
    for w1 in words:
        if w1["text"] != first:
            continue
        for w2 in words:
            if w2["text"] != second:
                continue
            if (
                abs(float(w2["top"]) - float(w1["top"])) <= 2
                and 0 <= float(w2["x0"]) - float(w1["x1"]) <= max_gap
            ):
                return (float(w1["x0"]), float(w1["top"]))
    return None


# --------------------------------------------------------------------------
# scheme segmentation
# --------------------------------------------------------------------------


def _is_scheme_heading(line):
    line = line.strip()
    if not line or len(line) > 90:
        return False
    normalized = _strip_trailing_footnote_symbols(line)
    upper = normalized.upper()
    if not upper.startswith("CAPITALMIND"):
        return False
    if not upper.endswith("FUND"):
        return False
    if "ASSET MANAGEMENT" in upper:
        return False
    if upper == "CAPITALMIND MUTUAL FUND":
        return False
    if any(ex in upper for ex in HEADING_EXCLUDE):
        return False
    return True


def _clean_scheme_name(line):
    name = _strip_trailing_footnote_symbols(line.strip())
    return _clean(name)


def segment_schemes(pdf):
    """Return {scheme_name: [page_index, ...]} in document order.

    Every Capitalmind scheme page repeats the scheme's name verbatim as
    the very first line -- the Investment Objective page, every
    Portfolio / Portfolio-Continued page, the Portfolio Changes page and
    (where present) the Sector Allocation page all start the same way.
    So a scheme's pages are simply the run of consecutive pages sharing
    that first line. As a safety net for a future layout that drops the
    repeated heading on a continuation page, a page is still folded into
    the current scheme if it carries a recognisable portfolio-table
    header, exactly as with the primary heading-based grouping.
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
        if _find_portfolio_headers(page):
            schemes[current].append(i)
        else:
            current = None

    return {name: schemes[name] for name in order}


# --------------------------------------------------------------------------
# "Benchmark:" / ISIN / Fund Managers metadata extraction
# --------------------------------------------------------------------------


def extract_isin(metadata_text):
    if not metadata_text:
        return ""
    m = re.search(r"\bISIN\s*[:\-]?\s*([A-Z]{2}[A-Z0-9]{9}\d)\b", metadata_text)
    return m.group(1) if m else ""


def _extract_isin_from_page(page):
    return extract_isin(page.extract_text() or "")


def _extract_benchmark(page):
    """ "Benchmark:" sits in the Scheme Details panel's middle (AUM) column,
    to the left of the Fund Managers column, and can wrap several lines
    for a composite benchmark -- pulled out by column position rather
    than by line, since the Fund Managers text often shares the same
    text rows once flattened to plain text."""
    words = _page_words(page)
    bench_word = next((w for w in words if w["text"] == "Benchmark:"), None)
    if bench_word is None:
        return None
    bx0 = float(bench_word["x0"])
    btop = float(bench_word["top"])

    fm = _find_adjacent_pair(words, "Fund", "Managers")
    right_bound = fm[0] - 5 if fm and fm[0] > bx0 else page.width

    col_words = [
        w
        for w in words
        if bx0 - 5 <= float(w["x0"]) < right_bound and float(w["top"]) >= btop - 1
    ]
    rows = _cluster_rows(col_words, y_tol=1.6)
    rows.sort(key=lambda r: r["top"])

    # Walk forward from the Benchmark row only while consecutive rows are
    # a tight, same-entry line-wrap apart; an unrelated panel (e.g. "Exit
    # Load:") sitting lower in this same page column is always a much
    # wider gap away than a wrapped benchmark name's own continuation
    # lines are from each other.
    collected = []
    last_top = None
    for r in rows:
        top = float(r["top"])
        if last_top is not None and top - last_top > 15:
            break
        collected.extend(sorted(r["words"], key=lambda w: float(w["x0"])))
        last_top = top

    text_words = [w["text"] for w in collected if w is not bench_word]
    value = _clean(_join_dehyphenated(text_words))
    return value or None


_ADDITIONAL_RE = re.compile(r"additional", re.IGNORECASE)
_ADDITIONAL_BENCHMARK_TAIL_RE = re.compile(
    r"^(.*?)\(?\s*additional\s*benchmark\s*\)?\s*$", re.IGNORECASE
)


def _extract_additional_benchmark(page):
    """The "(Additional Benchmark)" row of the Performance Disclosure
    table can wrap its label across the line above and/or below the row
    that actually carries the return figures (the label appears to be
    vertically centred against its own numbers). Reconstructed by
    finding that value-row, then folding in only the immediately
    adjacent value-less continuation line(s) -- the same
    tight-line-gap-vs-wide-block-gap distinction used for portfolio
    rows -- so a neighbouring, unrelated benchmark row is never pulled
    in by mistake."""
    words = _page_words(page)
    anchor = next((w for w in words if _ADDITIONAL_RE.search(w["text"])), None)
    if anchor is None:
        return None

    rows = _cluster_rows(words, y_tol=1.6)
    rows.sort(key=lambda r: r["top"])
    anchor_idx = next(
        (i for i, r in enumerate(rows) if any(w is anchor for w in r["words"])), None
    )
    if anchor_idx is None:
        return None

    def _has_number(row):
        return any(
            _NUMERIC_TOKEN_RE.match(w["text"]) and float(w["x0"]) > 150
            for w in row["words"]
        )

    def _numeric_x0s(row):
        return [
            float(w["x0"])
            for w in row["words"]
            if _NUMERIC_TOKEN_RE.match(w["text"]) and float(w["x0"]) > 150
        ]

    value_idx = anchor_idx
    if not _has_number(rows[anchor_idx]):
        for j in (anchor_idx + 1, anchor_idx - 1, anchor_idx + 2, anchor_idx - 2):
            if (
                0 <= j < len(rows)
                and abs(rows[j]["top"] - rows[anchor_idx]["top"]) <= 30
            ):
                if _has_number(rows[j]):
                    value_idx = j
                    break

    low = high = value_idx
    while (
        low - 1 >= 0
        and not _has_number(rows[low - 1])
        and rows[low]["top"] - rows[low - 1]["top"] <= 15
    ):
        low -= 1
    while (
        high + 1 < len(rows)
        and not _has_number(rows[high + 1])
        and rows[high + 1]["top"] - rows[high]["top"] <= 15
    ):
        high += 1

    numeric_x0s = _numeric_x0s(rows[value_idx])
    right_bound = (min(numeric_x0s) - 5) if numeric_x0s else 160.0

    label_words = [
        w
        for idx in range(low, high + 1)
        for w in sorted(rows[idx]["words"], key=lambda w: float(w["x0"]))
        if float(w["x0"]) < right_bound
    ]
    label_words.sort(key=lambda w: (float(w["top"]), float(w["x0"])))
    text = _clean(_join_dehyphenated([w["text"] for w in label_words]))
    if not text:
        return None
    m = _ADDITIONAL_BENCHMARK_TAIL_RE.match(text)
    name = _clean(m.group(1)) if m and m.group(1).strip() else text
    return name or None


_MANAGER_TITLE_RE = re.compile(
    r"\b(?:Mr|Ms|Mrs|Dr)\.\s*([A-Za-z][A-Za-z.]*(?:\s+[A-Za-z][A-Za-z.]*){0,4})"
)
_SLEEVE_RE = re.compile(r"Head of ([A-Za-z][A-Za-z\s]*?)\)")
_FUND_MANAGER_STOP_WORDS = {"Quantitative", "Exit", "Debt"}


def _parse_manager_paragraph(text):
    matches = list(_MANAGER_TITLE_RE.finditer(text))
    managers = []
    seen = set()
    for i, mm in enumerate(matches):
        name = _clean(mm.group(1))
        if not name or len(name) < 3:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        context = text[mm.end() : end]
        sm = _SLEEVE_RE.search(context)
        sleeve = _clean(sm.group(1)) if sm else None
        key = (name.lower(), sleeve)
        if key in seen:
            continue
        seen.add(key)
        managers.append({"role": "Fund Manager", "name": name, "sleeve": sleeve})
    return managers


def _extract_fund_managers(page):
    words = _page_words(page)
    fm = _find_adjacent_pair(words, "Fund", "Managers")
    if fm is None:
        return []
    fm_x0, fm_top = fm

    col_words = [
        w for w in words if float(w["x0"]) >= fm_x0 - 6 and float(w["top"]) > fm_top + 3
    ]
    rows = _cluster_rows(col_words, y_tol=1.6)
    rows.sort(key=lambda r: r["top"])

    kept_rows = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: float(w["x0"]))
        first_text = ws[0]["text"] if ws else ""
        if first_text in _FUND_MANAGER_STOP_WORDS:
            break
        kept_rows.append(ws)
    if not kept_rows:
        return []

    header_ws = kept_rows[0]
    header_texts = [w["text"] for w in header_ws]
    if (
        "Name" in header_texts
        and "Experience" in header_texts
        and "Since" in header_texts
    ):
        name_x0 = next(float(w["x0"]) for w in header_ws if w["text"] == "Name")
        exp_x0 = next(float(w["x0"]) for w in header_ws if w["text"] == "Experience")
        since_x0 = next(float(w["x0"]) for w in header_ws if w["text"] == "Since")

        managers = []
        pending_name = ""
        for ws in kept_rows[1:]:
            name_words = [w["text"] for w in ws if float(w["x0"]) < exp_x0 - 2]
            exp_words = [
                w["text"] for w in ws if exp_x0 - 2 <= float(w["x0"]) < since_x0 - 2
            ]
            since_words = [w["text"] for w in ws if float(w["x0"]) >= since_x0 - 2]
            name_text = " ".join(name_words).strip()
            if not exp_words and not since_words and pending_name:
                pending_name = (pending_name + " " + name_text).strip()
                continue
            if pending_name:
                managers.append(
                    {
                        "role": "Fund Manager",
                        "name": _clean(pending_name),
                        "sleeve": None,
                    }
                )
            pending_name = name_text
        if pending_name:
            managers.append(
                {"role": "Fund Manager", "name": _clean(pending_name), "sleeve": None}
            )
        return [m for m in managers if m["name"]]

    text = _clean(_join_dehyphenated([w["text"] for ws in kept_rows for w in ws]))
    return _parse_manager_paragraph(text)


# --------------------------------------------------------------------------
# portfolio / holdings extraction
# --------------------------------------------------------------------------


def _match_pct_to_nav(ws, idx):
    if (
        idx + 2 < len(ws)
        and ws[idx]["text"] == "%"
        and ws[idx + 1]["text"] == "to"
        and ws[idx + 2]["text"] == "NAV"
    ):
        return (ws[idx], idx + 3)
    return None


def _find_portfolio_headers(page):
    """Locate every "Issuer Name | % to NAV" and/or "Name of the
    Instrument / Issuer | [Rating] | % to NAV | [% to NAV (Derivative)]"
    header on the page, returning left-to-right x0 boundaries for each
    sub-column so holdings rows can be sliced out by coordinate."""
    words = _page_words(page)
    rows = _cluster_rows(words, y_tol=1.6)

    headers = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: float(w["x0"]))
        texts = [w["text"] for w in ws]
        n = len(ws)
        i = 0
        while i < n:
            if i + 1 < n and texts[i] == "Issuer" and texts[i + 1] == "Name":
                nav = _match_pct_to_nav(ws, i + 2)
                if nav:
                    headers.append(
                        {
                            "top": float(r["top"]),
                            "name_x0": float(ws[i]["x0"]),
                            "nav_x0": float(nav[0]["x0"]),
                            "rating_x0": None,
                            "deriv_x0": None,
                        }
                    )
                    i = nav[1]
                    continue

            seq = ["Name", "of", "the", "Instrument", "/", "Issuer"]
            if texts[i : i + len(seq)] == seq:
                name_x0 = float(ws[i]["x0"])
                j = i + len(seq)
                rating_x0 = None
                if j < n and texts[j] == "Rating":
                    rating_x0 = float(ws[j]["x0"])
                    j += 1
                nav = _match_pct_to_nav(ws, j)
                if nav:
                    nav_x0 = float(nav[0]["x0"])
                    j2 = nav[1]
                    deriv_x0 = None
                    nav2 = _match_pct_to_nav(ws, j2)
                    if nav2:
                        j3 = nav2[1]
                        tail = texts[j3] if j3 < n else ""
                        if "Derivative" in tail:
                            deriv_x0 = float(nav2[0]["x0"])
                    headers.append(
                        {
                            "top": float(r["top"]),
                            "name_x0": name_x0,
                            "nav_x0": nav_x0,
                            "rating_x0": rating_x0,
                            "deriv_x0": deriv_x0,
                        }
                    )
                    i = j
                    continue
            i += 1

    headers.sort(key=lambda h: (h["top"], h["name_x0"]))
    return headers


def _split_row_words(ws, header):
    """Classify one row's words into name / rating text and the row's own
    value token(s), by column position."""
    rating_x0 = header.get("rating_x0")
    nav_x0 = header["nav_x0"]
    deriv_x0 = header.get("deriv_x0")
    mid = (nav_x0 + deriv_x0) / 2 if deriv_x0 else None
    # Actual rating values (e.g. "CRISIL", "Sovereign") commonly start a
    # little to the left of the "Rating" header label's own x0, so the
    # name/rating boundary needs a generous margin rather than the
    # header's exact position -- otherwise the first word of the rating
    # (typically the agency name) gets misread as a trailing part of the
    # instrument's name.
    name_end = (rating_x0 - 20) if rating_x0 is not None else nav_x0

    name_words, rating_words = [], []
    value_word, deriv_word = None, None

    for w in ws:
        x0 = float(w["x0"])
        text = w["text"]
        if _PCT_RE.match(text):
            if deriv_x0 is not None:
                if x0 >= nav_x0 - 8:
                    if x0 < mid:
                        value_word = value_word or w
                    else:
                        deriv_word = deriv_word or w
                    continue
            elif x0 >= nav_x0 - 8:
                value_word = value_word or w
                continue
            # else: a %-shaped token left of the value column -- e.g. a
            # coupon rate embedded in the instrument's own name -- falls
            # through and is treated as ordinary name text below.
        if x0 < name_end - 2:
            name_words.append(w)
        elif rating_x0 is not None and x0 < nav_x0 - 2:
            rating_words.append(w)
        # else: a stray token beyond the value column(s) that wasn't
        # recognised as a value -- ignored rather than guessed at.
    return name_words, rating_words, value_word, deriv_word


def _merged_column_rows(page, header, right_edge, bottom_bound):
    """Build one row per instrument/bucket-header for a single header
    group, re-joining a name or bucket label that wraps across two or
    three physical lines. A row's own value (if any) is pinned to
    whichever physical line it printed on; adjacent value-less lines are
    folded into that same logical row only when the vertical gap is
    small enough to be a same-entry wrap rather than the start of the
    next entry."""
    words = [
        w
        for w in _page_words(page)
        if header["top"] - 3 <= float(w["top"]) < bottom_bound
        and header["name_x0"] - 15 <= float(w["x0"]) < right_edge
    ]
    words = [w for w in words if float(w["top"]) > header["top"] + 3]
    rows = _cluster_rows(words, y_tol=1.6)
    rows.sort(key=lambda r: r["top"])

    MERGE_GAP = 15.0
    merged = []
    open_kind = None
    open_text = ""
    open_rating = ""
    open_value = None
    open_top = None
    last_top = None

    def _append(base, addition):
        if not addition:
            return base
        if base.endswith("-") and not base.endswith("--"):
            return base[:-1] + addition
        return (base + " " + addition).strip() if base else addition

    def flush():
        nonlocal open_kind, open_text, open_rating, open_value, open_top
        if open_kind is not None and (open_text or open_value):
            merged.append(
                {
                    "top": open_top,
                    "company": _clean(open_text),
                    "rating": _clean(open_rating),
                    "value": open_value,
                }
            )
        open_kind = None
        open_text = ""
        open_rating = ""
        open_value = None
        open_top = None

    for r in rows:
        ws = sorted(r["words"], key=lambda w: float(w["x0"]))
        name_words, rating_words, value_word, _deriv = _split_row_words(ws, header)
        label_text = " ".join(w["text"] for w in name_words).strip()
        rating_text = " ".join(w["text"] for w in rating_words).strip()
        top = float(r["top"])
        gap = (top - last_top) if last_top is not None else None

        if value_word is not None:
            flush()
            open_kind = "value"
            open_text = label_text
            open_rating = rating_text
            open_value = value_word["text"]
            open_top = top
            last_top = top
            continue

        if not label_text and not rating_text:
            continue

        if open_kind == "value" and gap is not None and gap <= MERGE_GAP:
            open_text = _append(open_text, label_text)
            if rating_text:
                open_rating = _append(open_rating, rating_text)
            last_top = top
            continue
        if open_kind == "label" and gap is not None and gap <= MERGE_GAP:
            open_text = _append(open_text, label_text)
            last_top = top
            continue

        flush()
        open_kind = "label"
        open_text = label_text
        open_top = top
        last_top = top

    flush()
    return merged


# Bucket/category header phrases. These rows never carry their own "%
# to NAV" value (only the instruments beneath them, and the "Sub
# Total"/"Total" roll-ups, do), so they are only ever matched against
# value-less merged rows -- an actual holding can never be mistaken for
# one of these regardless of its name.
_BUCKET_PHRASES = (
    "equity & equity related",
    "equity and equity related",
    "listed / awaiting listing on stock exchange",
    "listed awaiting listing on stock exchange",
    "unlisted",
    "reits",
    "debt instruments",
    "government securities",
    "money market instruments",
    "certificate of deposit",
    "commercial paper",
    "treasury bill",
    "reverse repo",
    "treps",
    "privately placed",
    "preference shares",
    "preference share",
    "exchange traded fund",
    "mutual fund units",
    "others",
)

_GRAND_TOTAL_RE = re.compile(r"^grand\s+total\b", re.IGNORECASE)
_TOTAL_ROW_RE = re.compile(r"^(?:sub\s*)?total\b", re.IGNORECASE)
_NET_RECEIVABLES_RE = re.compile(
    r"^net\s+(?:receivables|current\s+assets)\b", re.IGNORECASE
)
_BUCKET_PREFIX_RE = re.compile(r"^\([a-zA-Z]\)\s*")


def _bucket_label(text_lower):
    for phrase in _BUCKET_PHRASES:
        if phrase in text_lower:
            return phrase
    return None


def _classify_column(merged_rows):
    """Walk one header-group's merged rows top-to-bottom, tracking the
    active bucket/category label, and emit clean holdings. Returns
    (holdings, stopped) where `stopped` signals a "Grand Total" row was
    reached -- nothing on the page after that point is portfolio data."""
    holdings = []
    current_category = ""
    stopped = False

    for row in merged_rows:
        company = row["company"]
        rating = row["rating"]
        value = row["value"]
        if not company and value is None:
            continue
        lower = company.lower().strip()

        if _GRAND_TOTAL_RE.match(lower):
            stopped = True
            break
        if _TOTAL_ROW_RE.match(lower):
            continue

        if value is None:
            bucket = _bucket_label(lower)
            if bucket is not None:
                current_category = _BUCKET_PREFIX_RE.sub("", company).strip()
            # An unrecognised value-less line (shouldn't normally occur)
            # is left alone rather than guessed at as a new category.
            continue

        if _NET_RECEIVABLES_RE.match(lower):
            sector = ""
        elif rating:
            sector = rating
        else:
            sector = current_category

        holdings.append(
            {"company": company, "sector": sector, "pct_to_net_assets": value}
        )

    return holdings, stopped


def extract_holdings(page):
    headers = _find_portfolio_headers(page)
    if not headers:
        return []
    bottom_bound = page.height - 20

    all_holdings = []
    for gi, header in enumerate(headers):
        if gi + 1 < len(headers):
            right_edge = (header["nav_x0"] + headers[gi + 1]["name_x0"]) / 2
        else:
            right_edge = page.width
        merged = _merged_column_rows(page, header, right_edge, bottom_bound)
        col_holdings, stopped = _classify_column(merged)
        all_holdings.extend(col_holdings)
        if stopped:
            break
    return all_holdings


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------


def extract_scheme_fields(pdf, page_idxs):
    """Aggregate every field the framework expects for one scheme, given
    the list of page indices segment_schemes() assigned to it."""
    benchmark = None
    additional_benchmark = None
    isin = ""
    fund_managers = []
    holdings = []
    scheme_name = None

    for pi in page_idxs:
        page = pdf.pages[pi]
        text = page.extract_text() or ""
        first_line = (text.split("\n") or [""])[0]

        if scheme_name is None and _is_scheme_heading(first_line):
            scheme_name = _clean_scheme_name(first_line)

        if benchmark is None:
            benchmark = _extract_benchmark(page)
        if additional_benchmark is None:
            found_add = _extract_additional_benchmark(page)
            if found_add:
                additional_benchmark = found_add
        if not isin:
            found_isin = _extract_isin_from_page(page)
            if found_isin:
                isin = found_isin
        if not fund_managers:
            managers = _extract_fund_managers(page)
            if managers:
                fund_managers = managers

        holdings.extend(extract_holdings(page))

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": fund_managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
