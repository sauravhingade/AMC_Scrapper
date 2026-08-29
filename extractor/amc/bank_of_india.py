"""
Bank of India Mutual Fund extractor.

Layout notes (reverse-engineered from the monthly "Facts in Figures" factsheet):

- Each scheme's profile lives on its own page (or run of pages), headed by a
  large-font title ("Bank of India <Scheme Name>") positioned near the top of
  the page. Continuation pages (a holdings table that overflows onto another
  page) repeat the "Portfolio Holdings / % to Net / Industry/ Rating / Assets"
  column headers but carry no fresh "Bank of India ..." title.
- The holdings table is laid out in 1-4 side-by-side text columns (equity
  funds tend to use 4 narrow columns, debt funds 2 wider columns, very simple
  schemes just 1). Each column repeats the same two-line header:
  "Portfolio Holdings" / "% to Net" then "Industry/ Rating" / "Assets".
- Row values (percentages) are plain decimals with NO trailing "%" character
  inside the holdings table (unlike the surrounding bar-chart / allocation
  tables, which DO suffix a "%" onto every number - a useful discriminator).
- A holding's name can wrap onto a second (or further) line. Unusually, the
  percentage is emitted on the FIRST line of the row (immediately after the
  first fragment of the name); any rating token and/or the remainder of the
  name can spill onto the following line(s), which carry no trailing number.
- A "4" glyph (rendered via a dingbat font, extracted as literal ASCII "4")
  prefixes each of a scheme's top-10 equity holdings; it is a marker, not
  part of the company name or a value.
- Sector sub-headers (equity) and instrument-category sub-headers (debt) are
  interleaved with the rows and must be told apart from real holdings/rating
  fragments; see `_is_category_label`.
- Fund manager / benchmark / date-of-allotment / AUM metadata is not laid out
  as "Label: value" prose (unlike Abakkus) but as a label on its own line
  followed by the value on the next line(s), living in the left half of the
  page (the right half of that same band holds unrelated NAV/expense-ratio
  tables at similar y-positions, so metadata extraction is restricted to the
  left half of the page to avoid stitching two side-by-side tables together).
"""

import re

from ..config import HEADING_EXCLUDE, SCHEME_KEYWORDS  # noqa: F401  (kept for parity with other extractors)

# ---------------------------------------------------------------------------
# Generic cleanup helpers (mirrors abakkus.py's approach)
# ---------------------------------------------------------------------------


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00a0", " ")
    text = text.replace("\u00ad", "")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[•●▪◦]", " ", text)
    text = re.sub(r"[\ue000-\uf8ff]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" \t\r\n:-")


def _page_words(page, **extra):
    try:
        return (
            page.extract_words(
                x_tolerance=3,
                y_tolerance=1.5,
                keep_blank_chars=False,
                extra_attrs=list(extra.get("extra_attrs", [])) or None,
            )
            or []
        )
    except TypeError:
        return page.extract_words() or []


def _words_to_lines(words, y_tolerance=1.5):
    if not words:
        return []

    rows = []
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(word["top"])
        row = None
        for candidate in reversed(rows[-3:]):
            if abs(candidate["top"] - top) <= y_tolerance:
                row = candidate
                break
        if row is None:
            row = {"top": top, "words": []}
            rows.append(row)
        row["words"].append(word)

    result = []
    for row in rows:
        ws = sorted(row["words"], key=lambda w: float(w["x0"]))
        result.append(
            {
                "top": row["top"],
                "bottom": max(float(w.get("bottom", w["top"])) for w in ws),
                "x0": min(float(w["x0"]) for w in ws),
                "x1": max(float(w["x1"]) for w in ws),
                "text": _clean(" ".join(w["text"] for w in ws)),
                "words": ws,
            }
        )
    return result


def _page_text(page) -> str:
    return "\n".join(
        line["text"] for line in _words_to_lines(_page_words(page)) if line["text"]
    )


# ---------------------------------------------------------------------------
# Scheme title / page segmentation
# ---------------------------------------------------------------------------

_SCHEME_TITLE_RE = re.compile(r"^bank\s+of\s+india\b", re.IGNORECASE)
# Structural, factsheet-template exclusions -- generic page-type titles that
# happen to start with "Bank of India" (the AMC's own name) but are not a
# scheme profile page (e.g. the branch/ISC directory).
_NON_SCHEME_TITLE_RE = re.compile(
    r"\b(branches|investor service|isc'?s?|mutual fund$)\b", re.IGNORECASE
)

_PROFILE_MARKERS_RE = re.compile(
    r"PORTFOLIO DETAILS|INVESTMENT OBJECTIVE|WHO SHOULD INVEST|DATE OF ALLOTMENT",
    re.IGNORECASE,
)


def _page_title(page) -> str:
    words = _page_words(page, extra_attrs=["size"])
    if not words:
        return ""
    try:
        sizes = [round(float(w["size"]), 1) for w in words]
    except (KeyError, TypeError):
        return ""
    max_size = max(sizes)
    if max_size < 10:
        return ""
    candidates = [
        w
        for w in words
        if round(float(w["size"]), 1) >= max_size - 0.3 and float(w["top"]) < 200
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda w: (round(float(w["top"]), 1), float(w["x0"])))
    return _clean(" ".join(w["text"] for w in candidates))


def _is_scheme_title(title: str) -> bool:
    title = _clean(title)
    if not title or len(title) > 100:
        return False
    if any(ex in title.upper() for ex in HEADING_EXCLUDE):
        return False
    if not _SCHEME_TITLE_RE.match(title):
        return False
    if _NON_SCHEME_TITLE_RE.search(title):
        return False
    return True


def segment_schemes(pdf) -> dict[str, list[int]]:
    """Returns {scheme_name: [page_index, ...]} in document order."""
    scheme_pages: dict[str, list[int]] = {}
    current = None

    for i, page in enumerate(pdf.pages):
        title = _page_title(page)
        text = page.extract_text() or ""

        if _is_scheme_title(title) and _PROFILE_MARKERS_RE.search(text):
            current = title
            scheme_pages.setdefault(current, [])
            if i not in scheme_pages[current]:
                scheme_pages[current].append(i)
            continue

        # Not a fresh scheme title: only treat this page as a continuation of
        # the current scheme's holdings table if it actually carries a
        # "Portfolio Holdings" table header (the hallmark of a holdings page)
        # and isn't itself a differently-titled scheme page or an unrelated
        # section (Scheme Performance, IDCW History, Branches, ...).
        if (
            current is not None
            and not _is_scheme_title(title)
            and _find_portfolio_headers(page)
        ):
            if i not in scheme_pages[current]:
                scheme_pages[current].append(i)
            continue

        # Anything else (appendix/report sections) ends the current run so
        # later unrelated pages never get attributed to a stale scheme.
        current = None

    return scheme_pages


# ---------------------------------------------------------------------------
# Portfolio holdings table: header/column discovery
# ---------------------------------------------------------------------------


def _find_portfolio_headers(page):
    """Locate every "Portfolio Holdings" column-header occurrence on a page.

    Returns a list of {"x0", "top"} dicts, one per column, sorted left to
    right. The number of columns varies (1-4) depending on scheme type, so
    nothing about column count is assumed.
    """
    lines = _words_to_lines(_page_words(page))
    headers = []
    for line in lines:
        ws = line["words"]
        for i in range(len(ws) - 1):
            if re.fullmatch(
                r"Portfolio", ws[i]["text"], re.IGNORECASE
            ) and re.fullmatch(r"Holdings", ws[i + 1]["text"], re.IGNORECASE):
                headers.append({"x0": float(ws[i]["x0"]), "top": float(line["top"])})

    unique = []
    for h in headers:
        if not any(
            abs(h["x0"] - u["x0"]) < 2 and abs(h["top"] - u["top"]) < 3 for u in unique
        ):
            unique.append(h)
    return sorted(unique, key=lambda h: (h["top"], h["x0"]))


def _column_ranges(page, headers):
    page_width = float(page.width)
    # The right edge of the page carries a thin vertical sidebar spelling out
    # the scheme's category (e.g. "F L E X I C A P F U N D"), one small-font
    # capital letter per line. Left unexcluded, those letters fall inside
    # the right-most column's x-range and get grouped into whatever holdings
    # row happens to share their y-position. Trim a safety margin off the
    # right edge to keep them out.
    sidebar_margin = 40.0
    ranges = []
    ordered = sorted(headers, key=lambda h: h["x0"])
    for i, header in enumerate(ordered):
        left = header["x0"] - 3
        if i + 1 < len(ordered):
            right = ordered[i + 1]["x0"] - 5
        else:
            right = page_width - sidebar_margin
        ranges.append((left, right, header))
    return ranges


def _column_lines(page, left, right, start_top, end_top=None):
    words = []
    for w in _page_words(page):
        x0 = float(w["x0"])
        top = float(w["top"])
        if top < start_top:
            continue
        if end_top is not None and top >= end_top:
            continue
        if left <= x0 < right:
            words.append(w)
    return _words_to_lines(words)


# ---------------------------------------------------------------------------
# Row classification vocab (structural / instrument-type labels, not
# individual holdings -- mirrors abakkus.py's own _EQUITY_SECTORS /
# _DEBT_CATEGORIES precedent).
# ---------------------------------------------------------------------------

_EQUITY_SECTORS = {
    "banks",
    "pharmaceuticals & biotechnology",
    "electrical equipment",
    "auto components",
    "consumer durables",
    "aerospace & defense",
    "power",
    "finance",
    "industrial manufacturing",
    "industrial products",
    "agricultural food & other products",
    "transport infrastructure",
    "transport services",
    "minerals & mining",
    "automobiles",
    "telecom - services",
    "capital markets",
    "others",
    "textiles & apparels",
    "construction",
    "petroleum products",
    "insurance",
    "financial technology (fintech)",
    "it - software",
    "entertainment",
    "gas",
    "cement & cement products",
    "non - ferrous metals",
    "ferrous metals",
    "retailing",
    "healthcare services",
    "leisure services",
    "commercial services & supplies",
    "diversified fmcg",
    "personal products",
    "realty",
    "chemicals & petrochemicals",
    "fertilizers & agrochemicals",
    "food products",
    "beverages",
    "consumer services",
    "oil, gas & consumable fuels",
    "metals & mining",
    "construction services",
    "services",
    "information technology",
    "fast moving consumer goods",
    "capital goods",
    "financial services",
    "healthcare equipment & supplies",
    "diversified metals",
    "equity holdings",
    "debt holdings",
    "hybrid holdings",
    "bank",  # occasional singular variant of "banks" used on some pages
}

_NON_HOLDING_LABELS = {
    "total",
    "grand total",
    "equity holdings",
    "money market instruments",
    "corporate debt",
    "government bond and treasury bill",
    "cash & cash equivalent",
    "cash and cash equivalent",
    "net receivables/(payables)",
    "net receivables/payables",
    "net receivables / (payables)",
    "treps / reverse repo",
    "treps/ reverse repo",
    "treps / reverse repo investments",
    "treps/reverse repo investments",
    "mutual fund investment",
    "mutual funds/exchange traded funds",
    "futures and options",
    "cdmdf",
    "corporate debt market development fund",
    "certificate of deposit",
    "commercial paper",
    "non-convertible debentures",
    "government bond",
    "treasury bill",
    "state government bond",
    "exchange traded funds",
    "equity futures",
    "units of cdmdf",
    "rfv_n-amrt",
}

_COMPANY_SUFFIXES = {
    "LIMITED",
    "LTD",
    "LTD.",
    "CORP",
    "CORPORATION",
    "INC",
    "PLC",
    "LLP",
    "BANK",
    "TRUST",
    "AG",
    "NV",
    "N.V.",
    "CO",
    "CO.",
    "COMPANY",
}

_RATING_RE = re.compile(
    r"(?:CRISIL|ICRA|CARE|IND|FITCH)\s+[A-Za-z0-9+\-/()]+"
    r"|\(SOV\)"
    r"|\bSOV\b"
    r"|Sovereign"
    r"|\bA1\+\b"
    r"|\bAAA\b"
    r"|\bAA\+\b"
    r"|\bAA\b"
    r"|\bBBB\+\b"
    r"|\bBBB\b"
    r"|\bA1\b"
    r"|\bA\+\b",
    re.IGNORECASE,
)

# Slightly broader variant used only when anchored to the very end of an
# already-isolated name+value cell (see `_split_rating`); includes the
# generic "OTHERS" credit-classification tag used by a few instrument rows
# (e.g. Corporate Debt Market Development Fund units), which would be too
# eager to also try matching inside ordinary wrapped-name continuation text.
_RATING_RE_END = re.compile(_RATING_RE.pattern + r"|\bOTHERS\b", re.IGNORECASE)

# Cash/payables/reverse-repo lines are genuine (small but real) contributors
# to a scheme's asset allocation -- production mutual-fund apps surface them
# as a "Cash & Cash Equivalent" line in the holdings/allocation view rather
# than silently dropping them, and their value (sometimes negative, for a
# net payable position) is part of what makes a fund's holdings reconcile to
# ~100% of net assets. They're identified by a small set of distinctive
# substrings rather than an exact-phrase list because the exact wording
# varies across schemes/months ("Net Receivables/Payables",
# "Net Receivables/(Payables)", "TREPS/ Repo", "TREPS / Reverse Repo
# Investments", ...).
_CASH_LABEL_PATTERNS = (
    (re.compile(r"net\s+receivable", re.IGNORECASE), "Net Receivables / (Payables)"),
    (
        re.compile(r"treps|reverse\s+repo", re.IGNORECASE),
        "TREPS / Reverse Repo Investments",
    ),
)
_CASH_SECTOR = "CASH & CASH EQUIVALENT"


def _is_cash_label(text: str) -> bool:
    return any(pattern.search(text) for pattern, _ in _CASH_LABEL_PATTERNS)


def _normalize_cash_label(text: str) -> str:
    for pattern, canonical in _CASH_LABEL_PATTERNS:
        if pattern.search(text):
            return canonical
    return "Cash & Cash Equivalent"


# Captures an optional literal minus *and* an optional pair of wrapping
# parentheses separately, because this factsheet is inconsistent about
# which one it uses for a negative value -- e.g. Liquid Fund prints
# "(-18.61)" (both), while Credit Risk Fund prints "(4.94)" for a value that
# arithmetically must be -4.94 (parens alone, standard accounting negative
# notation, no minus glyph). Either form is normalised to a negative number.
_TRAILING_NUM_RE = re.compile(r"^(.*\S)\s+(\(?)(-?)(\d+(?:\.\d{1,3})?)\)?$")


def _signed_value(match) -> str:
    open_paren, minus, digits = match.group(2), match.group(3), match.group(4)
    negative = bool(open_paren) or bool(minus)
    return f"-{digits}" if negative else digits


_STOP_SECTION_RE = re.compile(
    r"^(?:MCAP Categorization|Mcap Category|EQUITY INDUSTRY ALLOCATION|"
    r"COMPOSITION BY ASSETS|CREDIT PROFILE|All data as on|INVESTMENT OBJECTIVE|"
    r"WHO SHOULD INVEST|BENCHMARK\^?|DATE OF ALLOTMENT|FUND MANAGER|"
    r"AVERAGE AUM|LATEST AUM|NAV \(As on|OTHER PARAMETERS|"
    r"PORTFOLIO TURNOVER RATIO|EXPENSE RATIO|LOAD STRUCTURE|"
    r"MINIMUM APPLICATION|ADDITIONAL PURCHASE|For IDCW History|Invest Now|"
    r"DEBT PARAMETER|EQUITY PARAMETER|GRAND TOTAL)\b",
    re.IGNORECASE,
)


def _is_all_caps_label(text: str) -> bool:
    # Category/sector labels are pure phrases -- bond/treasury-bill holding
    # names that happen to be built from upper-case abbreviations (e.g.
    # "7.1% GOI (MD 18/04/2029) (SOV)") must never qualify, so any digit
    # rules this out immediately.
    if re.search(r"\d", text):
        return False
    letters = re.sub(r"[^A-Za-z]", "", text)
    if not letters or not letters.isupper():
        return False
    words = text.split()
    # Single all-caps tokens are ambiguous -- they're just as likely to be a
    # derivatives underlying ("NIFTY", "BANKNIFTY") sitting under an
    # "Equity Futures" sub-heading as an actual category name, and virtually
    # every genuine single-word NSE sector name (BANKS, POWER, GAS, ...) is
    # already covered by the exact-match vocabulary above, so the
    # free-form fallback is restricted to multi-word phrases.
    if len(words) < 2:
        return False
    if len(words) > 6:
        return False
    last = words[-1].upper().rstrip(".")
    if last in _COMPANY_SUFFIXES:
        return False
    return True


def _is_category_label(name: str) -> bool:
    key = _clean(name).lower()
    if not key:
        return False
    if key in _NON_HOLDING_LABELS or key in _EQUITY_SECTORS:
        return True
    # Cash-equivalent bucket lines ("TREPS / Reverse Repo Investments",
    # "TREPS/ Repo", "Net Receivables/(Payables)", ...) show up with
    # inconsistent exact wording across schemes/months; match on the
    # distinctive substring rather than an exhaustive exact-phrase list.
    if "treps" in key or "net receivable" in key or "reverse repo" in key:
        return True
    return _is_all_caps_label(name)


def _split_rating(name_prefix: str):
    m = re.search(rf"({_RATING_RE_END.pattern})\s*$", name_prefix, re.IGNORECASE)
    if not m:
        return _clean(name_prefix), ""
    company = _clean(name_prefix[: m.start()])
    rating = _clean(m.group(1))
    return company, rating


# A small number of instrument-type category phrases can *also* be a
# holding's own name -- a scheme's investment in Corporate Debt Market
# Development Fund units is genuinely labelled "Corporate Debt Market
# Development Fund" on its holding row too, sometimes with no distinguishing
# rating/tag attached. These must never be swallowed by the wrapped-header
# lookahead in `_parse_column` below (which would otherwise discard the
# value already captured on the row's first line).
_AMBIGUOUS_CATEGORY_PHRASES = {"corporate debt market development fund"}


def _strip_top10_marker(line):
    words = line["words"]
    if words and words[0]["text"] == "4":
        text = _clean(line["text"][1:])
        return text, True
    return line["text"], False


# ---------------------------------------------------------------------------
# Row-by-row column parser
# ---------------------------------------------------------------------------


def _parse_column(lines, carry_sector, max_row_gap=17.0):
    holdings = []
    pending = None  # last holding dict, eligible for continuation appends
    mode = "equity"  # flips to "debt" once a rating is observed in-column
    prev_top = None
    i = 0

    while i < len(lines):
        line = lines[i]
        text, top10 = _strip_top10_marker(line)
        text = _clean(text)
        if not text:
            i += 1
            continue

        # The holdings table's row spacing is tight and consistent (roughly
        # one line-height apart, including multi-line wraps of a single
        # holding). Below the table, unrelated page content (metadata
        # paragraphs, allocation charts) can coincidentally fall inside this
        # same x-range column, but only after a much larger vertical jump
        # than any real row-to-row gap inside the table -- so a jump well
        # beyond the table's own row height is a reliable, self-calibrating
        # signal that we've left the table, without needing to recognise
        # every possible downstream section by name.
        top = float(line["top"])
        if prev_top is not None and (top - prev_top) > max_row_gap:
            break
        prev_top = top

        if _STOP_SECTION_RE.match(text):
            break

        num_match = _TRAILING_NUM_RE.match(text)

        if num_match:
            name_prefix = _clean(num_match.group(1))
            value = _signed_value(num_match)

            if _is_cash_label(name_prefix):
                holding = {
                    "company": _normalize_cash_label(name_prefix),
                    "sector": _CASH_SECTOR,
                    "pct_to_net_assets": value,
                    "_top10": False,
                }
                holdings.append(holding)
                # Cash lines are atomic single-value rows; any further text
                # before the next number (e.g. a wrapped "adjusting for
                # futures" qualifier) is descriptive noise, not a name to
                # keep accumulating onto, so continuation is intentionally
                # not offered here (pending stays None).
                pending = None
                i += 1
                continue

            # A sector/category label can wrap onto a second line (with no
            # trailing number) that completes the phrase, e.g.
            # "Agricultural Food &" / "other Products" or "Pharmaceuticals &"
            # / "Biotechnology". This lookahead is tried BEFORE the
            # single-line category check below: a short first-line fragment
            # like "PHARMACEUTICALS &" can independently look like a valid
            # (if truncated) all-caps category label on its own, which would
            # otherwise short-circuit before ever considering the fuller,
            # more accurate combined name.
            if i + 1 < len(lines):
                nxt_text, _ = _strip_top10_marker(lines[i + 1])
                nxt_text = _clean(nxt_text)
                if nxt_text and not _TRAILING_NUM_RE.match(nxt_text):
                    combined = _clean(f"{name_prefix} {nxt_text}")
                    if (
                        combined.lower() not in _AMBIGUOUS_CATEGORY_PHRASES
                        and _is_category_label(combined)
                    ):
                        carry_sector = combined
                        pending = None
                        i += 2
                        continue

            if _is_category_label(name_prefix):
                if name_prefix.lower() not in {"total", "grand total"}:
                    carry_sector = _clean(name_prefix)
                pending = None
                i += 1
                continue

            company, rating = _split_rating(name_prefix)
            if not company:
                pending = None
                i += 1
                continue

            if rating:
                mode = "debt"
                sector = rating
            else:
                sector = carry_sector if mode == "equity" else ""

            holding = {
                "company": company,
                "sector": sector,
                "pct_to_net_assets": value,
                "_top10": top10,
                # Whether "sector" above is a confirmed rating (found
                # inline) as opposed to a speculative carry_sector guess (or
                # nothing) -- needed so a rating discovered later, on a
                # wrapped continuation line, is still allowed to override
                # the guess rather than being blocked by it. See the
                # continuation-handling branch below.
                "_rated": bool(rating),
            }
            holdings.append(holding)
            pending = holding
            i += 1
            continue

        # No trailing number: either a (possibly multi-line) category /
        # instrument-type header, or the wrapped remainder of the previous
        # holding's name (and/or its rating). A "Total"/"Grand Total"
        # subtotal line occasionally carries a "%"-suffixed value (e.g.
        # "Total 31.98%"), which doesn't match the plain-decimal holdings
        # pattern, so it must also be recognised here rather than only in
        # the trailing-number branch above.
        if re.match(r"^(?:grand\s+)?total\b", text, re.IGNORECASE):
            pending = None
            i += 1
            continue

        if _is_category_label(text):
            pending = None
            i += 1
            continue

        if pending is None:
            i += 1
            continue

        remainder = text
        if not pending.get("_rated"):
            rating_match = re.search(_RATING_RE, remainder)
            if rating_match:
                pending["sector"] = _clean(rating_match.group(0))
                pending["_rated"] = True
                mode = "debt"
                remainder = (
                    remainder[: rating_match.start()] + remainder[rating_match.end() :]
                )
                remainder = _clean(remainder)

        remainder = _clean(remainder)
        if remainder:
            pending["company"] = _clean(f"{pending['company']} {remainder}")
        i += 1

    for h in holdings:
        h.pop("_top10", None)
    return holdings, carry_sector


def _finalize_holdings(holdings):
    """Light validation/cleanup only -- NOT a content-based dedup. Fixed
    income tables legitimately list the same issuer/rating/weight twice when
    it represents two distinct tranches or ISINs (e.g. two separate "HDFC
    Bank Limited CRISIL A1+ 3.69" lines have both been observed, genuinely,
    in a single scheme's certificate-of-deposit table), so collapsing
    identical (company, sector, pct) rows would silently drop real
    holdings/weight instead of a parsing artefact."""
    result = []
    for h in holdings:
        company = _clean(h.get("company", ""))
        sector = _clean(h.get("sector", ""))
        pct = str(h.get("pct_to_net_assets", ""))
        if not company:
            continue
        result.append({"company": company, "sector": sector, "pct_to_net_assets": pct})
    return result


def _table_row_height(page, headers, ranges=None):
    """Nominal single-row height for this page's holdings table, measured
    from the gaps between the first several body rows themselves (not the
    header's own internal line-spacing, which can differ slightly from the
    body's row pitch), so the table/non-table row-gap threshold
    self-calibrates to this page's actual layout."""
    default = 7.0
    if not headers:
        return default
    if ranges is None:
        ranges = _column_ranges(page, headers)
    if not ranges:
        return default
    left, right, header = ranges[0]
    lines = _column_lines(page, left, right, header["top"] + 20)
    tops = [line["top"] for line in lines[:8] if _clean(line["text"])]
    gaps = [b - a for a, b in zip(tops, tops[1:]) if 2 <= (b - a) <= 12]
    if not gaps:
        return default
    gaps.sort()
    return gaps[len(gaps) // 2]


def _leftmost_metadata_top(page, min_top=0.0):
    """Top position of the first metadata label (INVESTMENT OBJECTIVE, WHO
    SHOULD INVEST, BENCHMARK^, ...) found in the left portion of the page
    at or below `min_top`, if any. Free-form paragraph body text under
    these labels can wrap wide enough to bleed into a neighbouring
    holdings-table column at a vertical gap too small for the row-spacing
    heuristic alone to reliably catch, so this gives every column an
    additional, structural (not gap-based) hard floor beneath which
    nothing is treated as a holdings row. `min_top` excludes labels like
    "PORTFOLIO DETAILS" that legitimately sit *above* the table itself.
    """
    best = None
    for line in _label_lines(page):
        top = float(line["top"])
        if top < min_top:
            continue
        if _match_label(line["text"]) and (best is None or top < best):
            best = top
    return best


def extract_holdings(page):
    headers = _find_portfolio_headers(page)
    if not headers:
        return []

    ranges = _column_ranges(page, headers)
    row_height = _table_row_height(page, headers, ranges)
    # A wide margin between the multiplier used here and the one used for
    # "cash label" style near-miss transitions matters: legitimate gaps
    # between sub-sections *inside* a debt table (e.g. between one
    # instrument-type's subtotal and the next sub-category's heading) have
    # been observed up to roughly 2.7x the page's own row height, while the
    # jump from the end of a real table into unrelated below-the-table
    # content has been observed at 4x+ -- so the multiplier is set well
    # above the former and comfortably below the latter.
    max_row_gap = max(row_height * 3.5, 15.0)
    table_start_top = min(h["top"] for h in headers)
    end_top = _leftmost_metadata_top(page, min_top=table_start_top + 20)

    holdings = []
    carry_sector = ""
    for left, right, header in ranges:
        lines = _column_lines(page, left, right, header["top"] + 20, end_top)
        column_holdings, carry_sector = _parse_column(lines, carry_sector, max_row_gap)
        holdings.extend(column_holdings)

    return _finalize_holdings(holdings)


# ---------------------------------------------------------------------------
# Metadata (benchmark / additional benchmark / ISIN / fund managers)
# ---------------------------------------------------------------------------

_LABEL_PREFIXES = [
    "WHO SHOULD INVEST",
    "INVESTMENT OBJECTIVE",
    "ADDITIONAL BENCHMARK",
    "BENCHMARK",
    "DATE OF ALLOTMENT",
    "FUND MANAGER",
    "AVERAGE AUM",
    "LATEST AUM",
    "MINIMUM APPLICATION AMOUNT",
    "ADDITIONAL PURCHASE AMOUNT",
    "MCAP Categorization",
    "PORTFOLIO DETAILS",
    "EQUITY INDUSTRY ALLOCATION",
    "COMPOSITION BY ASSETS",
    "CREDIT PROFILE",
    "DEBT PARAMETER",
    "EQUITY PARAMETER",
    "For IDCW History",
    "Invest Now",
]


def _label_lines(page):
    """Left-portion-of-page lines only, to avoid stitching the label/value
    metadata column together with the unrelated NAV/expense-ratio table that
    sits at similar y-positions in the right portion of the page.

    The metadata block (BENCHMARK/FUND MANAGER/DATE OF ALLOTMENT/...) is
    consistently anchored well left of the page's horizontal midpoint, while
    the neighbouring NAV/expense-ratio/load-structure table starts just
    right of the midpoint -- so a modest safety margin inward from the exact
    midpoint keeps the two apart even though they sit only ~30pt from each
    other in this factsheet's layout.
    """
    width = float(page.width)
    cutoff = width * 0.42
    words = [w for w in _page_words(page) if float(w["x0"]) < cutoff]
    return _words_to_lines(words)


def _match_label(text):
    stripped = _clean(text).rstrip("^")
    for prefix in _LABEL_PREFIXES:
        if stripped.upper() == prefix.upper() or stripped.upper().startswith(
            prefix.upper()
        ):
            return prefix
    return None


def _extract_label_block(lines, wanted_prefix):
    positions = [(i, _match_label(line["text"])) for i, line in enumerate(lines)]
    for i, label in positions:
        if label != wanted_prefix:
            continue
        j = i + 1
        parts = []
        while j < len(lines):
            if _match_label(lines[j]["text"]):
                break
            t = _clean(lines[j]["text"])
            if t:
                parts.append(t)
            j += 1
        if parts:
            return _clean(" ".join(parts))
    return None


def extract_benchmark(page_or_lines):
    lines = (
        page_or_lines
        if isinstance(page_or_lines, list)
        else _label_lines(page_or_lines)
    )
    return _extract_label_block(lines, "BENCHMARK")


def extract_additional_benchmark(page_or_lines):
    lines = (
        page_or_lines
        if isinstance(page_or_lines, list)
        else _label_lines(page_or_lines)
    )
    return _extract_label_block(lines, "ADDITIONAL BENCHMARK")


def extract_isin(text):
    if not text:
        return ""
    m = re.search(r"\bISIN\s*:?\s*([A-Z]{2}[A-Z0-9]{7,10}\d)\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def extract_fund_managers(page_or_lines):
    lines = (
        page_or_lines
        if isinstance(page_or_lines, list)
        else _label_lines(page_or_lines)
    )
    manager_text = _extract_label_block(lines, "FUND MANAGER")
    if not manager_text:
        return []

    matches = list(
        re.finditer(
            r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s+([A-Za-z]+(?:\s+[A-Za-z.]+){0,4})",
            manager_text,
            re.IGNORECASE,
        )
    )

    managers = []
    for i, match in enumerate(matches):
        name = _clean(match.group(1))
        name = re.split(
            r"\s+(?=(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s)", name, 1, flags=re.IGNORECASE
        )[0]
        # Trim off trailing biography prose that leaked in (e.g. "(w.e.f",
        # "Around", years-of-experience blurb) -- keep only up to the first
        # parenthesis or colon, which is where the name/appointment-date
        # clause ends in this factsheet's phrasing.
        name = re.split(r"[:(]", name, 1)[0]
        name = _clean(name)
        if not name:
            continue

        # Note: unlike some AMCs' factsheets, Bank of India's "FUND MANAGER"
        # blurb is free-form biography prose ("... 20 years of experience,
        # including 16 years in mutual fund industry") rather than a
        # structured per-sleeve label, so words like "Equity"/"Fixed Income"
        # appearing incidentally in that prose are not a reliable signal of
        # which sleeve a manager runs. Sleeve is left unset here rather than
        # guessed from biography text.
        entry = {"role": "Fund Manager", "name": name, "sleeve": None}
        if entry not in managers:
            managers.append(entry)
    return managers


# ---------------------------------------------------------------------------
# Top-level per-scheme extraction
# ---------------------------------------------------------------------------


def _scheme_is_performance_page(text):
    t = _clean(text).lower()
    return "scheme performance" in t[:200]


def extract_scheme_fields(pdf, page_idxs):
    if not page_idxs:
        return {
            "benchmark": None,
            "additional_benchmark": None,
            "isin": "",
            "fund_managers": [],
            "holdings": [],
            "holdings_count": 0,
        }

    benchmark = None
    additional_benchmark = None
    isin = ""
    managers = []
    holdings = []
    seen_pages = set()

    for idx in page_idxs:
        if idx in seen_pages:
            continue
        seen_pages.add(idx)

        page = pdf.pages[idx]
        text = _page_text(page)

        if _scheme_is_performance_page(text):
            continue

        label_lines = _label_lines(page)

        if benchmark is None:
            benchmark = extract_benchmark(label_lines)
        if additional_benchmark is None:
            additional_benchmark = extract_additional_benchmark(label_lines)
        if not isin:
            isin = extract_isin(text)
        for manager in extract_fund_managers(label_lines):
            if manager not in managers:
                managers.append(manager)

        # Extend rather than content-dedup: each page index is visited at
        # most once (guarded above), so any two holdings that come out
        # looking identical are two genuinely separate table rows (e.g.
        # distinct tranches of the same issuer/rating/weight), not a
        # re-read of the same row -- see `_finalize_holdings`.
        holdings.extend(extract_holdings(page))

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
