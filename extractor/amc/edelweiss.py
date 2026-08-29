"""
Edelweiss Mutual Fund factsheet extractor.

Mirrors the calling contract of amc/canara_robeco.py (segment_schemes /
extract_scheme_fields, identical return schema) but every bit of the
internal parsing is specific to how Edelweiss lays out its factsheet,
which is structurally quite different from Canara Robeco's:

  * A scheme's heading (e.g. "Edelweiss Large Cap Fund") repeats, in a
    large bold font, on *every* page that belongs to it -- not just the
    first -- so schemes are segmented by grouping consecutive pages that
    share the same (font-size-detected) heading, rather than by looking
    for the heading only on page 1.
  * Page 1 of a scheme carries an "About the Scheme" panel on the left
    (Inception Date / Benchmark / NAV / Expense Ratio / Fund Size / a
    small Fund Managers table / Minimum Investment / Exit Load /
    Quantitative Indicators) and one or more holdings tables on the
    right.
  * Holdings tables are headed "Company Name | Allocation" (equity
    "Top N Holdings", no Rating/sector column at all) or "Security Name
    | Rating | Allocation" (debt "All Holdings", grouped under
    instrument-type sub-headers such as "Certificate of Deposit", "NCD",
    "Sovereign" that carry no allocation figure of their own). Hybrid
    schemes can carry *both* an equity table and a debt table on the
    same page, and either table can be split into two side-by-side
    column groups.
  * "Additional Benchmark" is not off in a separate cross-referenced
    section (unlike Canara Robeco) -- it is printed right on the
    scheme's own performance page, in a "(Regular Plan) (<benchmark>)
    (<additional benchmark>)" line.
  * Edelweiss's factsheet does not publish a per-scheme ISIN anywhere,
    so extract_isin() always returns "" here (the hook is kept for
    interface parity and forward compatibility).

Nothing here is wired to a specific page number, month, or scheme name
-- everything is derived from on-page text/coordinates/font-size so the
same code keeps working on next month's factsheet, and it deliberately
does not reuse Canara Robeco's PORTFOLIO-band / Market-Cap-column /
industry-bucket-vocabulary logic, since Edelweiss's factsheet has none
of those things.
"""

from __future__ import annotations

import re

# Note: Canara Robeco's HEADING_EXCLUDE (from ..config) is deliberately
# *not* reused here. It is a substring-exclusion list tuned around that
# AMC's own table-of-contents/section wording (it disqualifies any
# heading containing "INDEX", for instance) and Edelweiss genuinely
# markets several schemes with "Index" in their name (e.g. "Edelweiss
# Nifty 50 Index Fund") -- applying that list here would silently drop
# real schemes. _NON_SCHEME_SECTION_RE below plays the same role,
# written narrowly enough for Edelweiss's own non-scheme section titles.

# --------------------------------------------------------------------------
# generic text/word helpers
# --------------------------------------------------------------------------

_PUA_RE = re.compile(r"[\u2022\u25cf\u25aa\u25e6\u2023\u2043\ue000-\uf8ff]")
_WS_RE = re.compile(r"\s+")
_TRAILING_FOOTNOTE_RE = re.compile(r"[^A-Za-z0-9\s():&,/'\-]+$")
_PCT_RE = re.compile(r"^-?\d+(?:\.\d+)?%$")


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


def _safe_crop_words(page, x0, top, x1, bottom, extra_attrs=None):
    """extract_words() over a cropped region of the page.

    Cropping *before* extracting words (rather than extracting from the
    whole page and filtering the word list afterwards) matters here: a
    handful of holdings rows in this factsheet are laid out with
    per-glyph positioning that makes pdfplumber's whole-page word
    grouping fall apart into single-character "words" (e.g. "Sidbi"
    coming back as ['S','i','d','b','i']). Restricting the character
    set to the target cell first, via within_bbox(), reliably avoids
    that -- the same characters extracted from a tight crop come back
    correctly grouped into real words.
    """
    x0 = max(0.0, min(x0, page.width))
    x1 = max(0.0, min(x1, page.width))
    top = max(0.0, min(top, page.height))
    bottom = max(0.0, min(bottom, page.height))
    if x1 <= x0 or bottom <= top:
        return []
    try:
        crop = page.within_bbox((x0, top, x1, bottom))
    except ValueError:
        return []
    kwargs = dict(x_tolerance=3, y_tolerance=1.5, keep_blank_chars=False)
    if extra_attrs:
        kwargs["extra_attrs"] = extra_attrs
    return crop.extract_words(**kwargs) or []


def _norm_key(text):
    """Loose normalisation for matching scheme names / headings."""
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return _WS_RE.sub(" ", text).strip()


def _cluster_rows(words, y_tol=1.6):
    """Group words into physical text lines by tight y-proximity.

    Unlike an anchor/lookback scheme (attaching stray words to the
    nearest numeric anchor within N points), this keeps genuinely
    distinct physical lines separate -- which is what lets a bare
    instrument-type sub-header line (e.g. "Certificate of Deposit",
    with no allocation figure on its own line) come back as its own
    row instead of bleeding into the holding row below it.
    """
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
# scheme heading detection (font-size based -- the heading repeats on
# every page of a scheme, in a bold font clearly larger than any body
# text on the page)
# --------------------------------------------------------------------------

_NON_SCHEME_SECTION_RE = re.compile(
    r"^(?:I\s*N\s*D\s*E\s*X|Index|Expert Speaks|SIP Performance|"
    r"Fund Performance|IDCW History|Performance Disclosure|"
    r"Quantitative Indicators|Quantative Indicators|"
    r"Investing Made Simple|How to Read|Investor Service|"
    r"Schemes managed by|Riskometer|Disclaimers?|Statutory|"
    r"Product Labelling)",
    re.IGNORECASE,
)


def _page_heading(page):
    """Return the largest-font text block at the top of the page (the
    scheme name), reconstructed across however many lines it wraps to."""
    words = _safe_crop_words(
        page, 0, 0, page.width, min(140, page.height), extra_attrs=["size"]
    )
    if not words:
        return ""
    max_size = max(round(float(w.get("size", 0)), 1) for w in words)
    if max_size <= 0:
        return ""
    heading_words = [
        w for w in words if abs(round(float(w.get("size", 0)), 1) - max_size) <= 0.6
    ]
    heading_words.sort(key=lambda w: (float(w["top"]), float(w["x0"])))
    return _clean(" ".join(w["text"] for w in heading_words))


def _is_scheme_heading(heading):
    heading = _strip_trailing_footnote_symbols(heading.strip())
    if not heading or len(heading) > 100:
        return False
    upper = heading.upper()
    if not (upper.startswith("EDELWEISS") or upper.startswith("BHARAT")):
        return False
    if _NON_SCHEME_SECTION_RE.match(heading):
        return False
    return True


def _clean_scheme_name(heading):
    return _clean(_strip_trailing_footnote_symbols(heading.strip()))


def segment_schemes(pdf):
    """Return {scheme_name: [page_index, ...]} in document order.

    Every page belonging to an Edelweiss scheme repeats that scheme's
    name as the largest text block at the top of the page, so schemes
    are found by grouping consecutive pages whose (font-size-detected)
    heading normalises to the same value -- this naturally handles
    schemes that run 2, 3, or more pages without hard-coding a page
    count anywhere.
    """
    schemes = {}
    order = []
    current_name = None
    current_key = None

    for i, page in enumerate(pdf.pages):
        heading = _page_heading(page)
        if _is_scheme_heading(heading):
            name = _clean_scheme_name(heading)
            key = _norm_key(name)
            if key == current_key:
                schemes[current_name].append(i)
                continue
            if name not in schemes:
                schemes[name] = []
                order.append(name)
            current_name = name
            current_key = key
            schemes[current_name].append(i)
            continue

        # Not a scheme heading. It might still be a genuine continuation
        # page of the current scheme (a long holdings table can run past
        # the point where the heading band is still on-page in some
        # layouts) -- keep it only if it visibly carries a holdings
        # table of the same kind this extractor knows how to read.
        if current_name is not None and _find_portfolio_headers(page):
            schemes[current_name].append(i)
        else:
            current_name = None
            current_key = None

    return {name: schemes[name] for name in order}


# --------------------------------------------------------------------------
# "About the Scheme" left-panel metadata
# --------------------------------------------------------------------------


def _find_portfolio_headers(page):
    """Locate every "Company/Security Name | [Rating] | Allocation"
    holdings-table header on the page, returning one dict per column
    group with the x0 of each sub-column. A page can carry more than
    one such group -- side by side (two-column layout) and/or stacked
    (a hybrid scheme's separate equity and debt holdings tables)."""
    words = _page_words(page)

    name_anchors = []
    for w in words:
        if w["text"] != "Name":
            continue
        for ow in words:
            if ow is w or ow["text"] not in ("Company", "Security", "Issuer", "Scheme"):
                continue
            if (
                abs(float(ow["top"]) - float(w["top"])) <= 2
                and 0 <= float(w["x0"]) - float(ow["x1"]) <= 10
            ):
                name_anchors.append({"top": float(w["top"]), "x0": float(ow["x0"])})
                break

    alloc_anchors = [
        (float(w["top"]), float(w["x0"])) for w in words if w["text"] == "Allocation"
    ]
    rating_anchors = [
        (float(w["top"]), float(w["x0"])) for w in words if w["text"] == "Rating"
    ]

    headers = []
    for na in name_anchors:
        cands = [
            a for a in alloc_anchors if abs(a[0] - na["top"]) <= 2.5 and a[1] > na["x0"]
        ]
        if not cands:
            continue
        alloc_top, alloc_x0 = min(cands, key=lambda a: a[1])
        rating_x0 = None
        for rt, rx in rating_anchors:
            if abs(rt - na["top"]) <= 2.5 and na["x0"] < rx < alloc_x0:
                rating_x0 = rx
                break
        headers.append(
            {
                "top": na["top"],
                "name_x0": na["x0"],
                "rating_x0": rating_x0,
                "alloc_x0": alloc_x0,
            }
        )

    dedup = {}
    for h in headers:
        dedup[(round(h["top"]), round(h["name_x0"]))] = h
    headers = list(dedup.values())
    headers.sort(key=lambda h: (h["top"], h["name_x0"]))
    return headers


def _metadata_text(page, headers):
    """Reconstruct the "About the Scheme" left panel as plain text,
    bounded to the left of wherever the holdings table(s) start."""
    if headers:
        boundary = min(h["name_x0"] for h in headers) - 8
    else:
        boundary = 205
    words = [w for w in _page_words(page) if float(w["x1"]) <= boundary]
    rows = _cluster_rows(words, y_tol=1.6)
    rows.sort(key=lambda r: r["top"])
    lines = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        lines.append(_clean(" ".join(w["text"] for w in ws)))
    return "\n".join(lines)


_BENCHMARK_RE = re.compile(
    r"\bBenchmark\b\s*(.+?)(?=\n\s*(?:NAV\b|Direct Plan|Regular Plan|Expense|"
    r"Fund Size|Fund Manager|Minimum Investment|Exit Load)|\Z)",
    re.IGNORECASE | re.DOTALL,
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
    """Edelweiss's monthly factsheet does not print a per-scheme ISIN
    anywhere (unlike some other AMCs' factsheets); this always comes
    back "". The regex is kept so that if a future factsheet layout
    starts publishing one, it starts getting picked up for free."""
    if not metadata_text:
        return ""
    m = _ISIN_RE.search(metadata_text)
    return m.group(1) if m else ""


_ADDL_BENCH_RE = re.compile(
    r"\((?:Regular|Direct)\s+Plan\)\s*\(([^)]+)\)\s*\(([^)]+)\)"
)


def extract_additional_benchmark(page_text):
    """The performance page prints "(Regular Plan) (<benchmark>)
    (<additional benchmark>)" directly beneath a "Scheme Benchmark
    Additional Benchmark" column heading -- no separate cross-reference
    section to hunt through, unlike Canara Robeco."""
    if not page_text or "Additional Benchmark" not in page_text:
        return None
    m = _ADDL_BENCH_RE.search(page_text)
    if not m:
        return None
    value = _clean(m.group(2))
    return value or None


# --------------------------------------------------------------------------
# fund managers (a small "Fund Managers | Experience | Managing Since"
# table inside the left panel, not a prose sentence)
# --------------------------------------------------------------------------

_FUND_MGR_SECTION_RE = re.compile(r"^Fund\s+Managers?$", re.IGNORECASE)
_FUND_MGR_COLHEAD_RE = re.compile(
    r"^Fund\s+Managers?\s+Experience\s+Managing\s+Since$", re.IGNORECASE
)
_MGR_STOP_RE = re.compile(
    r"^(?:Minimum Investment|Exit Load|Quantitative Indicators|"
    r"Quantative Indicators|Asset Allocation|Ratio|Sharpe|Portfolio)",
    re.IGNORECASE,
)
_MGR_TITLE_RE = re.compile(r"^(Mr|Ms|Mrs|Dr)\.\s*(.*)$", re.IGNORECASE)
_MGR_TAIL_RE = re.compile(r"^(?:(.*?)\s+)?(\d+)\s*[Yy]ears?(?:\s+([\d\-A-Za-z]+))?$")


def extract_fund_managers(metadata_text):
    if not metadata_text:
        return []
    lines = metadata_text.split("\n")

    start = None
    for i, line in enumerate(lines):
        if _FUND_MGR_SECTION_RE.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return []

    managers = []
    seen = set()
    pending_role = None
    pending_name = None
    i = start
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if _MGR_STOP_RE.match(line):
            break
        if _FUND_MGR_COLHEAD_RE.match(line):
            continue

        title_m = _MGR_TITLE_RE.match(line)
        if title_m:
            rest = title_m.group(2).strip()
            tail_m = _MGR_TAIL_RE.match(rest)
            if tail_m:
                name = _clean(tail_m.group(1))
            else:
                # Name wraps across further lines -- and in this
                # factsheet a surname can land *after* the "<n> years
                # <date>" line too (e.g. "Ms. Manasi" / "12 years
                # 01-Apr-26" / "Jalgaonkar" as three consecutive
                # physical lines for one manager), so keep collecting
                # name-fragment lines for one extra step past the
                # experience/date line rather than stopping right on it.
                name_parts = [rest] if rest else []
                found_tail = False
                lookahead = 0
                while lookahead < 3 and i < n:
                    nxt = lines[i].strip()
                    if not nxt:
                        i += 1
                        continue
                    if _MGR_TITLE_RE.match(nxt) or _MGR_STOP_RE.match(nxt):
                        break
                    tail_m = _MGR_TAIL_RE.match(nxt)
                    if tail_m:
                        if tail_m.group(1):
                            name_parts.append(tail_m.group(1))
                        i += 1
                        lookahead += 1
                        found_tail = True
                        continue
                    name_parts.append(nxt)
                    i += 1
                    lookahead += 1
                    if found_tail:
                        # One trailing name-fragment line past the
                        # experience/date line is expected; a second
                        # one is more likely the start of unrelated
                        # text, so stop here.
                        break
                name = _clean(" ".join(name_parts))
            # Strip a trailing footnote-reference mark some manager
            # names carry in this factsheet (e.g. "Mehul Dalmia*",
            # referencing a note elsewhere on the page) -- it is a
            # footnote marker, not part of the person's name.
            name = _strip_trailing_footnote_symbols(name)
            name = _clean(name)
            if not name or len(name) < 3:
                pending_role = None
                continue
            key = (name.lower(), pending_role)
            if key not in seen:
                seen.add(key)
                managers.append(
                    {"role": "Fund Manager", "name": name, "sleeve": pending_role}
                )
            pending_role = None
            continue

        # Not a "Mr./Ms./..." line and not a recognised stop line: this
        # is a role/sleeve label ahead of the next manager entry (e.g.
        # "Overseas Fund" + "Manager:" wrapped across two lines).
        role_text = re.sub(r"[:\s]+$", "", line).strip()
        role_text = re.sub(r"\bFund\s+Manager\b", "", role_text, flags=re.IGNORECASE)
        role_text = _clean(role_text)
        if role_text:
            pending_role = _clean(
                ((pending_role + " ") if pending_role else "") + role_text
            )

    return managers


# --------------------------------------------------------------------------
# holdings tables
# --------------------------------------------------------------------------

_STOP_MARKERS_RE = re.compile(
    r"Top\s*\d*\s*Sector|Top\s*\d*\s*Contributors|Top\s*\d*\s*(?:Equity|Debt)\s*Holdings|"
    r"Market Capitalization|"
    r"Portfolio Changes|New Entries|^Exits$|Rating\s*Profile|"
    r"Maturity\s*Profile|Instrument Allocation|Country Allocation|"
    r"Portfolio Weight|Portfolio Metrics|Key Portfolio [Cc]hanges|"
    r"Trailing Return|RollingReturn|SIPPerformance|SWPPerformance|"
    r"PortfolioYTM|Notes\s*:|DebtQuants|"
    r"Riskometer|Product Labell?ing",
    re.IGNORECASE,
)

# Checked against the page's full (unfiltered-by-column) text: the page
# footer runs the full width of the page and is always a safe bottom
# bound no matter which column a holdings table lives in.
_FOOTER_RE = re.compile(r"EDELWEISS MUTUAL FUND", re.IGNORECASE)

_TOTAL_ROW_RE = re.compile(r"\btotal\b", re.IGNORECASE)
# A residual "everything else below the named list" catch-all row, not
# an individual security -- e.g. "Other Equity Holdings 27.88%".
_OTHER_HOLDINGS_RE = re.compile(r"^Other\b.*Holdings$", re.IGNORECASE)
# A handful of equity "Top N Holdings" tables (e.g. sector funds that
# mix in ADRs/GDRs) carry an extra "Domestic"/"International" tag word
# after the security name, in place of a Rating column. It is metadata
# about the listing, not part of the company name, and can end up
# anywhere in the reassembled name once a wrapped entry is stitched
# back together (see _merge_wrap_fragments), not just at the end.
_LISTING_TAG_RE = re.compile(r"(?<!\S)(?:Domestic|International)(?!\S)", re.IGNORECASE)


def _page_word_rows_cache(page):
    """Physical text rows for the whole page (word list per row, not yet
    joined into text) so different callers can filter by column without
    re-clustering the page's words every time."""
    cached = getattr(page, "_edelweiss_word_rows_cache", None)
    if cached is not None:
        return cached
    words = _page_words(page)
    rows = _cluster_rows(words, y_tol=1.8)
    rows.sort(key=lambda r: r["top"])
    try:
        page._edelweiss_word_rows_cache = rows
    except Exception:
        pass
    return rows


def _right_edge(header, headers, page_width):
    same_row = [
        h
        for h in headers
        if h is not header
        and abs(h["top"] - header["top"]) <= 2.5
        and h["name_x0"] > header["name_x0"]
    ]
    if same_row:
        nxt = min(same_row, key=lambda h: h["name_x0"])
        return (header["alloc_x0"] + nxt["name_x0"]) / 2.0
    return page_width


def _bottom_bound(page, header, headers):
    """How far down a holdings-table column runs before something else
    starts. Two different "something else"s have to be told apart:

      * Pure left-panel metadata (e.g. "Market Capitalization (% of
        total)" at x0~30) that happens to share a physical text row
        with an unrelated word in a holdings column purely by vertical
        coincidence -- never a real bound for a holdings column.
      * A trailing single-column summary block that follows the
        holdings table (e.g. a fund-of-funds page's "Portfolio Weight
        (%)" sector breakdown or "Country Allocation" list) which is
        heading-anchored at the *left* holdings column's x0 but whose
        entries visually spill across into where the *right* holdings
        column used to be, once that column's own rows have run out --
        a real bound for *both* column groups on the page.

    So stop-marker text only counts when it falls at or to the right of
    the left-most holdings column on the page (excluding the metadata
    panel) -- not narrowly scoped to this one header's own column.
    The page footer, which runs the full page width, is always a safe
    bound regardless of column.
    """
    rows = _page_word_rows_cache(page)
    shared_col_x0 = min(h["name_x0"] for h in headers) - 30
    candidates = []
    for r in rows:
        if r["top"] <= header["top"] + 5:
            continue
        full_text = " ".join(
            w["text"] for w in sorted(r["words"], key=lambda w: w["x0"])
        )
        if _FOOTER_RE.search(full_text):
            candidates.append(r["top"])
            continue
        col_words = [w for w in r["words"] if float(w["x0"]) >= shared_col_x0]
        if not col_words:
            continue
        col_text = " ".join(w["text"] for w in sorted(col_words, key=lambda w: w["x0"]))
        if _STOP_MARKERS_RE.search(col_text):
            candidates.append(r["top"])
    for h in headers:
        if h is header:
            continue
        if (
            h["top"] > header["top"] + 10
            and abs(h["name_x0"] - header["name_x0"]) < 260
        ):
            candidates.append(h["top"])
    if candidates:
        return min(candidates) - 2
    return page.height - 15


def _rows_for_header(page, header, right_edge, bottom_bound):
    top0 = header["top"] + 3
    if bottom_bound <= top0:
        return []
    # Crop a little wider than right_edge: right_edge is the midpoint
    # used to split two side-by-side column groups, but an allocation
    # percentage sitting right at that midpoint (e.g. "...AAA 3.93%")
    # would otherwise have its trailing "%" sliced off by a bbox crop
    # drawn exactly on the midpoint. Words are still only accepted into
    # this group's rows by their x0 (below), so the extra margin cannot
    # pull in the *next* group's own holdings -- those start well to
    # the right of the midpoint.
    crop_x1 = min(right_edge + 16, page.width)
    words = _safe_crop_words(page, header["name_x0"] - 15, top0, crop_x1, bottom_bound)
    if not words:
        return []
    words = [w for w in words if float(w["x0"]) < right_edge]

    first_col_end = header["rating_x0"] or header["alloc_x0"]
    margin = 10
    rows = _cluster_rows(words, y_tol=2.4)
    rows.sort(key=lambda r: r["top"])

    out = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: float(w["x0"]))
        name_words, rating_words, alloc_words = [], [], []
        for w in ws:
            x0 = float(w["x0"])
            if x0 < first_col_end - margin:
                name_words.append(w["text"])
            elif header["rating_x0"] and x0 < header["alloc_x0"] - margin:
                rating_words.append(w["text"])
            else:
                alloc_words.append(w["text"])
        pct = next((t for t in alloc_words if _PCT_RE.match(t)), None)
        out.append(
            {
                "top": r["top"],
                "name": _clean(" ".join(name_words)),
                "rating": _clean(" ".join(rating_words)),
                "pct": pct,
            }
        )
    return out


def _merge_wrap_fragments(rows):
    """Reassemble a security name that spilled across more than one
    physical line back onto its own allocation row.

    This only gets called for tables with no Rating column, where a
    line with no allocation figure can only be part of a wrapped name
    (see _classify_holdings) -- never a legitimate section header. Each
    such fragment line is folded into whichever allocation-bearing
    ("anchor") row it sits physically closest to, and multiple
    fragments belonging to the same anchor are re-joined in top-to-
    bottom order. That handles both a fragment appearing *before* its
    anchor (the common case: the first line of a long name, with the
    allocation trailing on the next line) and, in this factsheet, a
    trailing suffix word squeezed onto its own line *after* the
    allocation figure (e.g. "Oracle Financial Services Software" /
    "Domestic 1.51%" / "Ltd" as three consecutive physical lines for
    one holding).
    """
    anchors = [i for i, r in enumerate(rows) if r["pct"]]
    if not anchors:
        return []
    fragments = {i: [] for i in anchors}
    for i, r in enumerate(rows):
        if r["pct"] or not r["name"]:
            continue
        name = r["name"]
        # A genuine wrapped-name fragment is a couple of words at most
        # (the tail/head of one company name); a stray footnote or
        # legend line squeezed in near the bottom of the table (e.g.
        # "Int - International / Non-Domestic Entity; Dom - Domestic
        # Entity.") is not, and must never get stitched onto a holding.
        if len(name.split()) > 6 or ";" in name or _STOP_MARKERS_RE.search(name):
            continue
        best = min(anchors, key=lambda a: abs(rows[a]["top"] - r["top"]))
        fragments[best].append((r["top"], name))

    merged = []
    for a in anchors:
        parts = list(fragments[a])
        if rows[a]["name"]:
            parts.append((rows[a]["top"], rows[a]["name"]))
        parts.sort(key=lambda t: t[0])
        name = _clean(" ".join(p[1] for p in parts))
        merged.append(
            {
                "top": rows[a]["top"],
                "name": name,
                "rating": rows[a]["rating"],
                "pct": rows[a]["pct"],
            }
        )
    return merged


def _classify_holdings(rows, has_rating):
    """Turn physical table rows into holdings.

    Tables with a Rating column (debt "All Holdings") can legitimately
    contain bare instrument-type sub-headers with no allocation figure
    of their own (e.g. "Certificate of Deposit", "Sovereign") -- those
    become `context`, tagging the sector of the holdings beneath them.

    Tables with no Rating column (equity "Top N Holdings") never carry
    that kind of sub-header; every no-allocation line is a wrapped-name
    fragment, so rows are pre-merged with _merge_wrap_fragments()
    before classification.
    """
    if not has_rating:
        rows = _merge_wrap_fragments(rows)

    holdings = []
    context = ""
    for row in rows:
        name = row["name"]
        pct = row["pct"]
        rating = row["rating"]

        if not pct:
            if has_rating and name and 1 <= len(name.split()) <= 8:
                context = name
            continue

        name = _clean(_LISTING_TAG_RE.sub(" ", name))
        if not name:
            continue
        if _TOTAL_ROW_RE.search(name) or _OTHER_HOLDINGS_RE.match(name):
            # roll-up / sub-total row, e.g. "NCD Total 69.40%",
            # "Certificate of Deposit Total 15.96%", "Total 100.00%",
            # "Other Equity Holdings 27.88%"
            continue

        sector = rating if rating else (context if has_rating else "")
        holdings.append({"company": name, "sector": sector, "pct_to_net_assets": pct})
    return holdings


def extract_holdings(page):
    headers = _find_portfolio_headers(page)
    if not headers:
        return []
    holdings = []
    for header in headers:
        right_edge = _right_edge(header, headers, page.width)
        bottom = _bottom_bound(page, header, headers)
        rows = _rows_for_header(page, header, right_edge, bottom)
        holdings.extend(_classify_holdings(rows, header["rating_x0"] is not None))
    return holdings


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

        if additional_benchmark is None:
            page_text = page.extract_text() or ""
            additional_benchmark = extract_additional_benchmark(page_text)

        if headers:
            holdings.extend(extract_holdings(page))

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": fund_managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
