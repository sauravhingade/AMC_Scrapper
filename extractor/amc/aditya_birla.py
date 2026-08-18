"""ABSL monthly factsheet extractor."""

import re

from ..config import HEADING_EXCLUDE, SCHEME_KEYWORDS

BODY_MARKERS = re.compile(
    r"""
    Fund\s+Manager\s*-\s*
    |Benchmark\s*:
    |Portfolio\s*Holdings
    |Sector\s*/\s*Issuer\s+Name
    |Fund\s+Snapshot
    |Investment\s+Objective
    """,
    re.IGNORECASE | re.VERBOSE,
)
_MONTH_YEAR_SUFFIX = re.compile(
    r"\s+"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+20\d{2}$",
    re.IGNORECASE,
)
_FACTSHEET_MONTH_RE = re.compile(
    r"\bMonthly\s+Factsheet\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(20\d{2})\b",
    re.IGNORECASE,
)


def _strip_month_year_suffix(text: str) -> str:
    return _MONTH_YEAR_SUFFIX.sub("", _clean(text))


def _is_scheme_overview_page(text: str) -> bool:
    """
    ABSL overview page has Fund Manager + Fund Category.
    This prevents TOC/index pages from becoming scheme headings.
    """
    return bool(
        re.search(r"\bFund\s+Manager\s*-\s*", text, re.IGNORECASE)
        and re.search(r"\bFund\s+Category\s*:", text, re.IGNORECASE)
    )


def _normalize_label(text: str) -> str:
    text = _clean(text).lower()

    text = text.replace("–", "-")
    text = text.replace("—", "-")

    # Normalize spaces around punctuation.
    text = re.sub(r"\s*&\s*", " & ", text)
    text = re.sub(r"\s*-\s*", " - ", text)
    text = re.sub(r"\s*/\s*", "/", text)

    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _scheme_names_equivalent(left: str, right: str) -> bool:
    """Compare scheme titles while tolerating harmless PDF punctuation variants."""

    def norm(value: str) -> str:
        value = _clean(value).casefold()
        value = value.replace("&", " ")
        value = re.sub(r"[^a-z0-9]+", " ", value)
        value = re.sub(r"\band\b", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    return norm(left) == norm(right)


def segment_schemes(pdf) -> dict[str, list[int]]:
    """
    Returns:
        {
            scheme_name: [page_index, ...]
        }

    Scheme boundaries are detected from the coordinate-based header
    extraction, NOT page.extract_text(), because ABSL has side-by-side
    text that pdfplumber can interleave when flattening the page.
    """

    scheme_pages: dict[str, list[int]] = {}
    current = None

    for i, page in enumerate(pdf.pages):
        meta = extract_scheme_name_and_category(page)
        heading = meta.get("scheme_name")

        # A real scheme overview/header page.
        # A scheme heading is valid only when the page also has a real
        # ABSL scheme-category tag in the visual header. This prevents TOC/index
        # rows (e.g. page 3) from becoming fake schemes.
        if heading and meta.get("scheme_category"):
            heading = _strip_month_year_suffix(heading)

            current = heading
            scheme_pages.setdefault(current, [])

            # The overview/header page contains benchmark, ISIN and fund
            # manager metadata even when it has no portfolio table.  Keep
            # that page with the scheme instead of only keeping body/table
            # pages.
            if i not in scheme_pages[current]:
                scheme_pages[current].append(i)

        # Once a scheme has started, attach relevant pages to it.
        if current:
            text = _page_text(page)
            is_body_page = bool(BODY_MARKERS.search(text))
            is_portfolio_page = bool(_find_portfolio_header_lines(page))

            if is_body_page or is_portfolio_page:
                # The heading page may itself contain a portfolio table. It was
                # already added above, so never add the same PDF page twice.
                if i not in scheme_pages[current]:
                    scheme_pages[current].append(i)

    # Metadata can appear on a scheme page whose visual title is lower than
    # the normal header band (FOF pages are a known example).  After the main
    # segmentation pass, attach such a page to the already-known scheme when
    # the page contains the exact scheme title and an actual Fund Manager
    # field.  This is deliberately exact-title based, so a monthly factsheet
    # can change page numbers/layout without cross-contaminating schemes.
    known = list(scheme_pages.items())
    for i, page in enumerate(pdf.pages):
        text = _page_text(page)
        if "Fund Manager" not in text:
            continue
        compact_text = re.sub(r"\s+", " ", text).casefold()

        # Important: a scheme title can legitimately appear inside another
        # scheme's page (most notably the Gold/Silver ETF name appears in the
        # corresponding Gold/Silver FOF page because the FOF invests in that
        # ETF).  The old substring-only check therefore attached the FOF page
        # to the underlying ETF as well, duplicating its portfolio and making
        # an otherwise valid 100% portfolio look like 200%.
        #
        # Prefer the page's actual visual scheme heading whenever one is
        # available.  Only use the exact-title fallback when the page has no
        # detectable heading at all; this preserves the original purpose of
        # this pass for metadata pages whose title sits outside the normal
        # header band.
        page_meta = extract_scheme_name_and_category(page)
        page_heading = page_meta.get("scheme_name")

        for scheme_name, pages in known:
            compact_name = re.sub(r"\s+", " ", scheme_name).casefold()
            if page_heading:
                # A detected heading is authoritative.  Allow only harmless
                # punctuation/spacing variants (e.g. "&" vs "and").  Do not
                # attach a page merely because its body text mentions another
                # scheme such as an underlying ETF referenced by a FOF.
                if not _scheme_names_equivalent(page_heading, scheme_name):
                    continue
            elif compact_name not in compact_text:
                continue

            if i not in pages:
                pages.append(i)

    return scheme_pages


_PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")

# Standard AMFI/NSE industry classification. This is the same taxonomy
# used across AMCs (it's not ABSL-specific), so it is expected to remain
# stable month to month. New entries can be appended if a future factsheet
# introduces a sector name not seen here.
_EQUITY_SECTORS = {
    "aerospace & defense",
    "agricultural food & other products",
    "agricultural, commercial & construction vehicles",
    "auto components",
    "automobiles",
    "banks",
    "beverages",
    "capital markets",
    "cement & cement products",
    "chemicals & petrochemicals",
    "commercial services & supplies",
    "construction",
    "consumable fuels",
    "consumer durables",
    "consumer services",
    "other consumer services",
    "diversified",
    "diversified fmcg",
    "diversified metals",
    "electrical equipment",
    "entertainment",
    "fertilizers & agrochemicals",
    "finance",
    "financial technology (fintech)",
    "food products",
    "gas",
    "healthcare equipment & supplies",
    "healthcare services",
    "industrial manufacturing",
    "household products",
    "preferred stock",
    "industrial products",
    "insurance",
    "it - software",
    "it -software",
    "leisure services",
    "metals & mining",
    "metals & minerals trading",
    "ferrous metals",
    "non -ferrous metals",
    "non-ferrous metals",
    "oil",
    "oil, gas & consumable fuels",
    "personal products",
    "petroleum products",
    "pharmaceuticals & biotechnology",
    "power",
    "realty",
    "retailing",
    "telecom - services",
    "telecom -services",
    "textiles & apparels",
    "transport infrastructure",
    "transport services",
    "services",
    "chemicals",
    "healthcare",
    "fast moving consumer goods",
    "capital goods",
    "financial services",
    "information technology",
    "construction services",
    "construction materials",
    "agricultural commercial & construction vehicles",
    "minerals & mining",
    "cigarettes & tobacco products",
    "it - services",
    "net cash and cash equivalent",
    "non - ferrous metals",
    "international exposure",
    "commodity & commodity related",
}

# Debt / cash / FOF category rollups. Rows matching these labels are
# subtotals, not individual holdings.
_DEBT_CATEGORIES = {
    "debt & debt related",
    "money market instruments",
    "certificate of deposit",
    "commercial paper",
    "government securities/treasury bills",
    "government securities",
    "government bond",
    "treasury bills",
    "state government bond",
    "corporate bond & ncds",
    "fixed rates bonds -corporate",
    "fixed rates bonds - corporate",
    "zero coupon bonds",
    "cash & cash equivalents",
    "cash & current assets",
    "net cash and cash equivalent",
    "net receivables / (payables)",
    "net receivables/(payables)",
    "treps / reverse repo",
    "alternative investment funds (aif)",
    "corporate debt market development fund",
    "mutual funds units",
    "investment funds/mutual funds",
    "exchange traded fund",
    "futures",
    "grand total",
    "cash management bills",
    "interest rate swaps",
    "securitised debt",
    "floating rates notes - corporate",
    "reits",
    "invits",
}


def _metadata_lines(page):
    """Return physical lines from the left metadata panel.

    ABSL overview pages put benchmark / manager metadata in the left panel,
    while performance tables and other text can sit on the right at the same
    vertical positions. Using only the left panel prevents flattened PDF text
    from joining unrelated columns.
    """
    words = _page_words(page)
    left_limit = float(page.width) * 0.65
    left_words = [w for w in words if float(w["x0"]) < left_limit]
    return _words_to_lines(left_words, y_tolerance=0.25)


def _benchmark_from_line(lines, require_colon=False):
    for i, line in enumerate(lines):
        text = _clean(line["text"])
        pattern = (
            r"\bBenchmark\s*:\s*(.+)$"
            if require_colon
            else r"\bBenchmark\s*[:\-]\s*(.+)$"
        )
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue

        value = _clean(m.group(1))
        value = re.split(
            r"\b(?:Load\s+Structure|Fund\s+Manager|Investment\s+Objective|"
            r"SIP\s*Performance|Additional\s+Benchmark)\b",
            value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" -:")

        # Composite benchmarks wrap across physical lines in FOF/arbitrage
        # pages. Follow only nearby lines that start in the same left metadata
        # column; this avoids accidentally taking a right-side "Market Value"
        # line that happens to be vertically adjacent.
        anchor_x = float(line["x0"])
        cursor_y = float(line["top"])
        for candidate in lines[i + 1 : i + 7]:
            nxt = _clean(candidate["text"])
            if not nxt:
                continue
            if float(candidate["x0"]) > anchor_x + 25:
                continue
            if float(candidate["top"]) - cursor_y > 45:
                break
            if re.match(
                r"^(?:Fund\s+Manager|Load\s+Structure|Investment\s+Objective|"
                r"SIP\s*Performance|Additional\s+Benchmark)\b",
                nxt,
                re.IGNORECASE,
            ):
                break

            starts_formula = nxt.startswith("+")
            needs_formula = value.endswith(("+", "Short", "and", "Gold", "Silver"))
            if not (needs_formula or starts_formula):
                break

            value = f"{value} {nxt}".strip()
            cursor_y = float(candidate["top"])

        return _clean(value) or None

    return None


def extract_benchmark(text: str) -> str | None:
    """Extract the scheme benchmark from flattened page text.

    Public contract intentionally matches the existing ABSL extractor:
    input is page text and the return value is a string or None.
    """
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        m = re.search(r"\bBenchmark\s*:\s*(.+)", raw, re.IGNORECASE)
        if not m:
            continue

        value = m.group(1).strip()
        value = re.split(
            r"(?i)(?:SIP\s*Performance|Load\s*Structure|Fund\s*Manager|"
            r"Investment\s*Objective|Additional\s*Benchmark|"
            r"Subsequentinstalments|Subsequent\s+instalments|PastPerformance|"
            r"Past\s+Performance|Taxesarenot|Taxes\s+are\s+not|"
            r"#SchemeBenchmark|SchemeBenchmark|Particulars|flowby|XIRRmethod|WhereBenchmarkreturns|Total\s*Amount\s*Invested|Entry\s*Load|Exit\s*Load)",
            value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

        for j in range(i + 1, min(i + 5, len(lines))):
            nxt = re.split(
                r"(?i)(?:SIP\s*Performance|Load\s*Structure|Fund\s*Manager|"
                r"Investment\s*Objective|Additional\s*Benchmark|"
                r"Subsequentinstalments|Subsequent\s+instalments|PastPerformance|"
                r"Past\s+Performance|Taxesarenot|Taxes\s+are\s+not|"
                r"#SchemeBenchmark|SchemeBenchmark|Particulars|flowby|XIRRmethod|WhereBenchmarkreturns|Total\s*Amount\s*Invested|Entry\s*Load|Exit\s*Load)",
                lines[j],
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()
            if not nxt:
                continue
            if not (
                nxt.startswith("+")
                or value.endswith(("Short", "+", "and", "Gold", "Silver"))
            ):
                break
            value = f"{value} {nxt}"

        return _clean(value) or None

    return None


def extract_additional_benchmark(text: str) -> str | None:
    m = re.search(
        r"\bAdditional\s+Benchmark\s*-\s*(.+?)(?=\s+(?:NA\b|-?\d+(?:\.\d+)?%))",
        text,
        re.IGNORECASE,
    )
    return _clean(m.group(1)) if m else None


def extract_isin(text: str) -> str:
    # Preserve the existing public contract.  The extractor only consumes an
    # explicitly labelled ISIN; holding-level ISINs without that label are not
    # treated as the scheme ISIN.
    m = re.search(r"\bISIN\s*:?\s*([A-Z0-9]{6,20})\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""


_MANAGER_STOP_RE = re.compile(
    r"(?i)(?:PastPerformance|Past\s+Performance|Returns|Managing|Experience|"
    r"Total|Particulars|SIPPerformance|SIP\s+Performance|"
    r"Subsequentinstalments|Subsequent\s+instalments|InvestmentObjective|"
    r"Investment\s+Objective|PortfolioHoldings|Portfolio\s+Holdings|"
    r"Benchmark|FundCategory|Fund\s+Category|LoadStructure|Load\s+Structure|"
    r"Reportbycalling|Report\s+by\s+calling|Centers|Center|Scheme|Market|Value)"
)

_MANAGER_LINE_RE = re.compile(
    r"(?i:Fund\s+Manager\s*[-:]\s*(?:Mr\.|Ms\.|Mrs\.|Dr\.)?)\s*(.+?)"
    r"(?=\s+(?:Managing|Experience|Date\s+of\s+Allotment|Benchmark|SIP|Load|AUM|$)|$)",
    re.IGNORECASE,
)


def _normalise_manager_name(raw: str) -> str | None:
    """Clean one ABSL manager field without accepting neighbouring PDF prose."""
    raw = _clean(raw)
    if not raw:
        return None

    # Some PDF text layers glue the disclaimer to the surname. Cut at the
    # first known prose token even when there is no whitespace.
    raw = _MANAGER_STOP_RE.split(raw, maxsplit=1)[0].strip(" ,;:-")
    parts = raw.split()

    # A normal ABSL manager name is 2-3 words.  The first two words are the
    # safest fallback when the third token is actually a glued page marker.
    clean_parts = []
    for part in parts:
        if _MANAGER_STOP_RE.search(part):
            break
        clean_parts.append(part)
    parts = clean_parts[:3]

    if len(parts) < 2 or any(len(part) > 35 for part in parts):
        return None

    name = _clean(" ".join(parts))
    # Reject obvious non-name prose while allowing genuine multi-word names.
    if not re.fullmatch(r"[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,2}", name):
        return None
    return name


def _manager_entries(names) -> list:
    managers = []
    for raw in names:
        name = _normalise_manager_name(raw)
        if not name:
            continue
        entry = {"role": "Fund Manager", "name": name, "sleeve": None}
        if entry not in managers:
            managers.append(entry)
    return managers


def extract_fund_managers(text: str) -> list:
    """Extract Fund Manager fields from flattened ABSL page text."""
    pattern = re.compile(
        r"(?i:Fund\s+Manager\s*[-:]\s*(?:Mr\.|Ms\.|Mrs\.|Dr\.)?)\s*"
        r"(.+?)(?=\s+(?:Managing|Experience|Date\s+of\s+Allotment|Benchmark|SIP|Load|AUM|$)|$)",
        re.IGNORECASE,
    )
    return _manager_entries(m.group(1) for m in pattern.finditer(text))


def _extract_fund_managers_from_page(page) -> list:
    """Prefer the left metadata panel so side-by-side PDF text cannot hide a manager."""
    names = []
    for line in _metadata_lines(page):
        text = _clean(line["text"])
        m = _MANAGER_LINE_RE.search(text)
        if m:
            names.append(m.group(1))
    if names:
        return _manager_entries(names)
    return extract_fund_managers(_page_text(page))


def extract_factsheet_month(pdf) -> str | None:
    """Extract the document month, e.g. 'July 2026'."""
    for page in pdf.pages[:5]:
        text = _page_text(page)
        m = _FACTSHEET_MONTH_RE.search(text)
        if m:
            month = m.group(1).capitalize()
            year = m.group(2)
            return f"{month} {year}"
    return None


# ---------------------------------------------------------------------------
# Scheme name / category metadata
# ---------------------------------------------------------------------------

_RATING_TOKEN_RE = re.compile(
    r"(?:"
    r"AAA(?:\([A-Z0-9]+\))?"
    r"|AA\+?"
    r"|A\+?"
    r"|A1\+?"
    r"|SOV(?:EREIGN)?"
    r"|Unrated"
    r")",
    re.IGNORECASE,
)

_RATING_RE = re.compile(
    r"(?:"
    r"(?:CRISIL|ICRA|CARE|IND)\s+"
    r"(?:AAA(?:\([A-Z0-9]+\))?|AA\+?|A1\+?|SOV(?:EREIGN)?|Unrated)"
    r"|AAA(?:\([A-Z0-9]+\))?"
    r"|AA\+?"
    r"|A1\+?"
    r"|SOV(?:EREIGN)?"
    r"|Unrated"
    r")",
    re.IGNORECASE,
)

_RATING_AGENCY_RE = re.compile(r"\b(?:CRISIL|ICRA|CARE|IND)\b", re.IGNORECASE)


def _is_rating_fragment(text: str) -> bool:
    """True for PDF lines that contain only a rating/agency fragment.

    ABSL sometimes positions the rating column a few points above/below the
    issuer row. Those fragments must not be merged into the next security
    (especially a category subtotal such as ``State Government bond``).
    """
    value = _clean(text)
    if not value:
        return False
    if _RATING_AGENCY_RE.fullmatch(value):
        return True
    return bool(_RATING_TOKEN_RE.fullmatch(value))


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00a0", " ")
    text = text.replace("\u00ad", "")
    text = text.replace("\ufeff", "")
    # Top-Ten-Holding markers show up either as a normal Unicode bullet or
    # as a Private-Use-Area glyph from an icon font, depending on how the
    # PDF was generated for a given month -- strip both defensively.
    text = re.sub(r"[•●▪◦]", " ", text)
    text = re.sub(r"[\ue000-\uf8ff]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" \t\r\n:-")


def _page_words(page):
    try:
        return (
            page.extract_words(
                x_tolerance=3,
                y_tolerance=1.5,
                keep_blank_chars=False,
            )
            or []
        )
    except TypeError:
        return page.extract_words() or []


def _words_to_lines(words, y_tolerance=1.5):
    """Group words into physical PDF lines while preserving coordinates."""
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
# Scheme name / category metadata
# ---------------------------------------------------------------------------

_CATEGORY_TAGS = {
    "equity funds": "Equity",
    "hybrid funds": "Hybrid",
    "debt funds": "Debt",
    "passive equity index": "Index",
    "exchange traded": "ETF",
    "passive fund of funds": "FOF",
    "debt index": "Debt Index",
    "solution oriented": "Solution Oriented",
}

# Used by extract_scheme_name_and_category to detect a second scheme's
# name glued onto the first (see the guard where these are used).
_ADITYA_MARKER = re.compile(r"aditya birla sun life", re.IGNORECASE)
_TRAILING_CATEGORY_TAG = re.compile(
    r"\s+(?:" + "|".join(tag.replace(" ", r"\s+") for tag in _CATEGORY_TAGS) + r")\s*$",
    re.IGNORECASE,
)


def extract_scheme_name_and_category(page) -> dict:
    """
    Extract ABSL scheme name/category from the visual header area.

    ABSL places:
        category tag -> upper/right
        scheme name  -> upper/left
        month/year   -> below the scheme name

    We use coordinates rather than flattened page text because the PDF
    contains side-by-side text which can become interleaved in
    page.extract_text().
    """

    lines = _words_to_lines(_page_words(page))

    # Only inspect the visual header area.
    top_lines = [line for line in lines if line["top"] < 90]

    category = None

    # ---------------------------------------------------------
    # 1. Find category using right-side position
    # ---------------------------------------------------------
    for line in top_lines:
        text = _clean(line["text"])
        low = text.lower()

        for tag, label in _CATEGORY_TAGS.items():
            if low == tag or low.startswith(tag) or tag in low:  # noqa: SIM102
                # Category lives on the right side. Some PDF lines begin
                # slightly left because the title/category text overlaps the
                # visual boundary, so use x1 as a second signal.
                if line["x0"] > page.width * 0.45 or line["x1"] > page.width * 0.72:
                    category = label
                    break

        if category:
            break

    # ---------------------------------------------------------
    # 2. Find scheme-name lines
    # ---------------------------------------------------------
    # ABSL repeats a running-header version of the scheme name around
    # y ~= 35 on some continuation/overview pages.  The actual visual
    # title is lower, around y ~= 45-50.  The running header can also
    # differ slightly from the real title, e.g.:
    #   "... Banking PSU Debt Fund"
    # vs the actual
    #   "... Banking & PSU Debt Fund"
    #
    # Taking the first Aditya line therefore creates false duplicate
    # schemes.  Only use the visual title band for scheme-name detection.
    name_lines = [
        line
        for line in top_lines
        if float(line["top"]) >= 40 and float(line["x0"]) >= 0
    ]

    name_parts = []

    for line in name_lines:
        text = _clean(line["text"])

        if not text:
            continue

        # Month/year is never part of the name.
        if re.fullmatch(
            r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
            r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
            r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2}",
            text,
            re.IGNORECASE,
        ):
            continue

        # Don't take category text.
        low = text.lower()

        if any(low == tag or low.startswith(tag) for tag in _CATEGORY_TAGS):
            continue

        # Don't take buttons / page furniture.
        if re.fullmatch(
            r"INVEST\s+NOW",
            text,
            re.IGNORECASE,
        ):
            continue

        # Genuine scheme name starts with ABSL.
        if text.lower().startswith("aditya birla sun life"):
            # A category tag can be glued to the title on some pages
            # (e.g. "... Liquid Fund Debt Funds").  Category is metadata,
            # never part of the scheme name.
            text = _TRAILING_CATEGORY_TAG.sub("", text).strip()

            # `_words_to_lines` groups purely by y-position, so two
            # side-by-side columns sitting at the same vertical spot (a
            # continuation-page running header repeating the scheme name,
            # or a two-scheme Dividend History table) can get glued into
            # one merged line -- e.g. "Aditya Birla Sun Life Overnight
            # Fund Debt Funds Aditya Birla Sun Life Overnight Fund" or
            # "...Nifty 50 Index Fund Aditya Birla Sun Life CRISIL IBX
            # Gilt Apr 2029 Index Fund". A genuine scheme name only ever
            # states the AMC name once, so if it shows up again later in
            # this same line (or on a following top_line, once we
            # already have a name in progress), that's a second column's
            # text, not a continuation of this one -- stop there instead
            # of absorbing it.
            if name_parts:
                break
            second = _ADITYA_MARKER.search(text, len("aditya birla sun life"))
            if second:
                text = _TRAILING_CATEGORY_TAG.sub("", text[: second.start()]).strip()
                if text:
                    name_parts.append(text)
                break
            name_parts.append(text)
            continue

        # A wrapped second line belongs to the name only if we
        # already found the first line.
        if name_parts:
            # Stop at obvious non-name content.
            if re.search(
                r"\b(?:Investment Objective|Portfolio Holdings|"
                r"NAV as on|Tracking Error|SIP Performance|"
                r"Fund Manager|Benchmark)\b",
                text,
                re.IGNORECASE,
            ):
                break

            # Don't absorb descriptive sentences.
            if re.match(
                r"^(?:An|A|The|This|For|Invest|Scheme|Returns|"
                r"Long-term|Income|Reasonable)\b",
                text,
                re.IGNORECASE,
            ):
                break

            # A short continuation line such as:
            #
            # Aditya Birla Sun Life Nifty Midcap 150
            # Index Fund
            #
            # is allowed.
            if len(text) <= 60:
                name_parts.append(text)
            else:
                break

    name = _clean(" ".join(name_parts))

    if not name:
        return {
            "scheme_name": None,
            "scheme_category": category,
        }

    # Remove accidental month/year suffix.
    name = _strip_month_year_suffix(name)

    return {
        "scheme_name": name,
        "scheme_category": category,
    }


# ---------------------------------------------------------------------------
# Portfolio header / column detection
# ---------------------------------------------------------------------------


def _find_portfolio_header_lines(page):
    """
    Return every 'Sector/Issuer Name' header occurrence on the page, with
    coordinates. There can be one such header per side-by-side column, and
    -- for Hybrid/Arbitrage funds where the table format switches mid-page
    (equity block, then a debt block, then another equity block) --
    multiple header pairs stacked vertically down the page.

    This works directly off individual words rather than merged physical
    lines: two side-by-side "Sector/Issuer Name" headers sit at the same
    y-coordinate, so a naive line-grouping (group-by-top) would merge them
    into one garbled "Sector/Issuer Name Sector/Issuer Name" string and
    hide both from a simple text match.
    """
    words = _page_words(page)
    headers = []
    for w in words:
        if not re.fullmatch(r"Sector\s*/\s*Issuer", w["text"], re.IGNORECASE):
            continue
        # Look for the matching "Name" word immediately to its right.
        name_word = next(
            (
                w2
                for w2 in words
                if w2["text"].lower() == "name"
                and abs(float(w2["top"]) - float(w["top"])) < 2
                and 0 < float(w2["x0"]) - float(w["x1"]) < 20
            ),
            None,
        )
        if name_word is not None:
            headers.append({"top": float(w["top"]), "x0": float(w["x0"])})
    # Round the sort key so two headers that are visually on the same row
    # (e.g. top=162.72 vs 162.74 -- sub-pixel PDF rendering jitter) don't
    # get flipped out of left-to-right reading order by float noise.
    return sorted(headers, key=lambda h: (round(h["top"]), h["x0"]))


def _classify_columns(page, header, right_bound):
    """
    Inspect the label band above a 'Sector/Issuer Name' header, bounded to
    that header's own column width (its x0 up to right_bound), to work out
    which value columns follow it (Rating / Total AUM / Derivatives / Net
    AUM / generic "% to Net Assets"). Bounding by column width matters --
    without it, a Rating label belonging to the *other* side-by-side
    column would leak into this one since both sit in the same y-band.
    """
    words = _page_words(page)
    # The column sub-labels are split across the line above the header
    # ("% of") and the line below it ("Total AUM" / "Derivatives" /
    # "Net AUM"), so the label band must span both sides of the header row.
    band = [
        w
        for w in words
        if header["top"] - 16 <= float(w["top"]) <= header["top"] + 16
        and header["x0"] + 15 <= float(w["x0"]) < right_bound
    ]
    # Label tokens can be split across two physical lines (e.g. "Net" above
    # "Assets") in an order that isn't reliably reconstructable by joining
    # text, so classify off individual token membership rather than
    # substring-matching a concatenated string.
    tokens = {w["text"].strip(".,").lower() for w in band}

    columns = []
    if "rating" in tokens:
        columns.append("rating")
    if "total" in tokens and "aum" in tokens:
        columns.append("pct_total_aum")
    if "derivatives" in tokens:
        columns.append("pct_derivatives")
    if "net" in tokens and ("aum" in tokens or "assets" in tokens):
        columns.append("pct_net_assets")
    if not columns:
        # Fall back to a single generic percentage column.
        columns.append("pct_net_assets")
    return columns


def _find_column_ranges(page):
    """Build independent table windows for every portfolio header.

    The important rule is that a right boundary is determined only by a
    header on the same visual row. A header from a different stacked block
    must never narrow the current column. The data window starts just below
    the header label band so the first holding row is not clipped.
    """
    headers = _find_portfolio_header_lines(page)
    if not headers:
        return []

    page_width = float(page.width)
    page_height = float(page.height)
    ranges = []

    # ABSL can place a continuation header lower down the page while the
    # other side's header remains near the top. Therefore the horizontal
    # column boundary must come from the next distinct header x-position
    # anywhere on the page, not only from a header on the same y-row.
    header_xs = sorted({round(float(hdr["x0"]), 2) for hdr in headers})

    for h in headers:
        hx = round(float(h["x0"]), 2)
        greater_xs = [x for x in header_xs if x > hx + 1]
        right = min(greater_xs, default=page_width) - 5

        # Only a lower header in the same horizontal column closes this
        # block. This preserves a left-column continuation even when the
        # right column has already switched to another table.
        below_same_column = [
            o
            for o in headers
            if o["top"] > h["top"] + 5 and abs(o["x0"] - h["x0"]) < 30
        ]
        bottom = min((o["top"] for o in below_same_column), default=page_height)

        # Start only a few points below the header. ABSL has at least one
        # valid first data row inside the old +12 offset in some ETF pages.
        columns = _classify_columns(page, h, right)
        ranges.append(
            {
                "left": h["x0"] - 3,
                "right": right,
                "top": h["top"] + 4,
                "bottom": bottom,
                "columns": columns,
                "header_top": h["top"],
                "header_x0": h["x0"],
            }
        )

    return ranges


def _column_lines(page, rng):
    words = []
    for w in _page_words(page):
        x0 = float(w["x0"])
        top = float(w["top"])
        if not (rng["top"] <= top < rng["bottom"]):
            continue
        if rng["left"] <= x0 < rng["right"]:
            words.append(w)
    return _words_to_lines(words)


# ---------------------------------------------------------------------------
# Row classification
# ---------------------------------------------------------------------------
_NORMALIZED_EQUITY_SECTORS = {_normalize_label(x) for x in _EQUITY_SECTORS}


def _is_equity_sector(text: str) -> bool:
    value = _normalize_label(text)
    value_no_pct = _normalize_label(_PERCENT_RE.sub("", value))
    return (
        value in _NORMALIZED_EQUITY_SECTORS
        or value_no_pct in _NORMALIZED_EQUITY_SECTORS
    )


_NORMALIZED_DEBT_CATEGORY = {_normalize_label(x) for x in _DEBT_CATEGORIES}


def _is_debt_category(text: str) -> bool:
    value = _normalize_label(text)
    value_no_pct = _normalize_label(_PERCENT_RE.sub("", value))
    return (
        value in _NORMALIZED_DEBT_CATEGORY or value_no_pct in _NORMALIZED_DEBT_CATEGORY
    )


_CASH_ROWS = {
    "net cash and cash equivalent",
    "cash & cash equivalents",
    "cash & current assets",
    "net receivables / (payables)",
    "net receivables/(payables)",
}


def _is_cash_row(text: str) -> bool:
    value = _normalize_label(text)
    value_no_pct = _normalize_label(_PERCENT_RE.sub("", value))
    return value in _CASH_ROWS or value_no_pct in _CASH_ROWS


_INVESTMENT_CATEGORY_ROWS = {
    "corporate debt market development fund",
}


def _is_investment_category_row(text: str) -> bool:
    value = _normalize_label(text)
    value_no_pct = _normalize_label(_PERCENT_RE.sub("", value))
    return (
        value in _INVESTMENT_CATEGORY_ROWS or value_no_pct in _INVESTMENT_CATEGORY_ROWS
    )


_COUNTRY_SUBTOTALS = {
    "united states of america",
    "france",
    "japan",
    "taiwan",
    "china",
    "canada",
    "united kingdom",
    "germany",
    "switzerland",
    "australia",
    "netherlands",
    "singapore",
    "south korea",
    "ireland",
    "denmark",
    "spain",
    "italy",
    "sweden",
    "finland",
    "belgium",
    "hong kong",
    "indonesia",
    "india",
    "luxembourg",
    "bermuda",
}


def _looks_like_country(text: str) -> bool:
    return _normalize_label(text) in _COUNTRY_SUBTOTALS


def _is_group_title(text: str) -> bool:
    """Rows with no trailing percentage at all -- pure section titles like
    'Equity & Equity Related' or 'Mutual Funds Units'."""
    return bool(
        re.fullmatch(
            r"(?:Equity\s*&\s*Equity\s+Related|Debt\s*&\s*Debt\s+Related|"
            r"Mutual\s+Funds\s+Units|Money\s+Market\s+Instruments)",
            _clean(text),
            re.IGNORECASE,
        )
    )


def _is_stop_line(text: str) -> bool:
    t = _clean(text)
    return bool(
        re.match(
            r"^(?:Grand Total|Sector\s*/\s*Issuer|Miscellaneous|Top Ten Holdings|"
            r"Disclaimer|Note[:\s]|This product|Scheme Name|Tracking Differences|"
            r"Product Label|ProductLabel)\b",
            t,
            re.IGNORECASE,
        )
    )


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------

_TRAILING_PCT = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")


def _leading_sector_prefix(text: str) -> str | None:
    value = _normalize_label(text)
    # Longest-first so multi-word sectors win over short prefixes.
    for sector in sorted(_EQUITY_SECTORS, key=len, reverse=True):
        normalized = _normalize_label(sector)
        if value.startswith(normalized + " "):
            return sector
    return None


def _is_name_continuation(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:Ltd\.?|Limited|PLC|Inc\.?|Corp\.?|Corporation|Company|Co\.?|"
            r"LLC|LP|N\.?V\.?|S\.?A\.?|Pte\.?|AG|SE)",
            _clean(text),
            re.IGNORECASE,
        )
    )


def _strip_embedded_header_prefix(name: str) -> str:
    value = _clean(name)
    # Some pages merge the small column-label line with the first sector row.
    # Remove only known header phrases at the beginning; never alter normal
    # issuer names.
    value = re.sub(
        r"^(?:Assets\s+)?(?:Total\s+)?(?:AUM\s+)?"
        r"(?:Derivatives\s+AUM\s+)?(?:Net\s+AUM|Net\s+Assets)\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"^(?:AUM\s+Derivatives\s+AUM)\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    # If the merged text still contains the generic equity group label, drop
    # that label only when a concrete sector follows it.
    value = re.sub(
        r"^Equity\s*&\s*Equity\s+Related\s+(?=.+)",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return _clean(value)


def _is_table_header_noise(text: str) -> bool:
    t = _normalize_label(text)
    if "sector/issuer" in t or "issuer name" in t:
        return True
    if "derivatives" in t and "aum" in t and not _PERCENT_RE.search(t):
        return True
    header_phrases = (
        "total aum",
        "derivatives",
        "net aum",
        "net assets",
        "rating",
    )
    # These words can occur in genuine issuers, so require at least two header
    # cues before treating the line as column-label noise.
    score = sum(phrase in t for phrase in header_phrases)
    return score >= 2 or t == "assets"


def _split_trailing_tokens(text: str, columns):
    """
    Given a physical line's cleaned text and the ordered list of expected
    column kinds for this table, split off the trailing rating/percentage
    tokens from the leading issuer/sector name.

    Equity-style rows can have between 1 and 3 trailing percentages
    (a plain long-only holding just shows Net AUM; a row with a matching
    short future shows all three), so token count is auto-detected off the
    tail of the line -- it is never assumed to equal len(columns).
    """
    pct_matches = list(_TRAILING_PCT.finditer(text))
    if not pct_matches:
        return None

    # Only trust a trailing run of percentages that reaches the end of the
    # line (ignoring the literal '%' char and trailing whitespace).
    tail_start = None
    end = len(text)
    for m in reversed(pct_matches):
        chunk = text[m.end() : end].strip()
        if chunk and chunk != "%":
            break
        tail_start = m.start()
        end = m.start()
    if tail_start is None:
        return None

    pct_values = [m.group(1) for m in pct_matches if m.start() >= tail_start]
    name_and_rating = text[:tail_start].strip()

    rating = None
    if "rating" in columns:
        rmatch = None
        for m in _RATING_RE.finditer(name_and_rating):
            rmatch = m
        if rmatch:
            rating = _clean(rmatch.group(0))
            name_and_rating = (
                name_and_rating[: rmatch.start()] + name_and_rating[rmatch.end() :]
            )

    name = _clean(name_and_rating)
    # Some ABSL rows place a standalone rating token at the start of the
    # issuer text (e.g. ``AAA(SO) India Universal Trust``). If the row already
    # supplied a rating, strip that token from the issuer name.
    if rating:
        name = _clean(
            re.sub(
                r"^(?:AAA(?:\([A-Z0-9]+\))?|AA\+?|A1\+?|SOV(?:EREIGN)?|Unrated)\s+",
                "",
                name,
                count=1,
                flags=re.IGNORECASE,
            )
        )
    return name, rating, pct_values


def _assign_pct_values(pct_values, columns):
    """Map a trailing run of percentage tokens onto named fields based on
    how many value-columns this table has and how many numbers the row
    actually carries (equity rows omit zero-valued Derivatives/Total AUM
    cells rather than printing "0.00 %")."""
    result = {"pct_total_aum": None, "pct_derivatives": None, "pct_net_assets": None}
    value_cols = [c for c in columns if c != "rating"]

    if len(pct_values) == len(value_cols):
        for col, val in zip(value_cols, pct_values):
            result[col] = val
    elif len(pct_values) == 1:
        # Only one number printed -- it's always the net/final figure.
        result["pct_net_assets"] = pct_values[0]
    elif len(pct_values) == 3 and set(value_cols) >= {
        "pct_total_aum",
        "pct_derivatives",
        "pct_net_assets",
    }:
        result["pct_total_aum"] = pct_values[0]
        result["pct_derivatives"] = pct_values[1]
        result["pct_net_assets"] = pct_values[2]
    else:
        # Unexpected count -- keep the last value as net assets and leave
        # the rest unassigned rather than mis-map them.
        result["pct_net_assets"] = pct_values[-1]
    return result


def _parse_table_lines(lines, columns, initial_sector=""):
    """Parse one physical table block and preserve sector/country hierarchy."""
    holdings = []
    current_sector = initial_sector
    pending_rating_agency = None
    i = 0

    while i < len(lines):
        text = _clean(lines[i]["text"])
        if not text:
            i += 1
            continue
        if _is_stop_line(text):
            break
        if _is_group_title(text):
            i += 1
            continue
        if _is_table_header_noise(text):
            i += 1
            continue

        # Ratings can be emitted as standalone PDF lines because the rating
        # column is vertically offset from the issuer column. Never merge such
        # a line into the next security/category row.
        if not _PERCENT_RE.search(text) and _is_rating_fragment(text):
            if _RATING_AGENCY_RE.fullmatch(text):
                pending_rating_agency = _clean(text)
            elif holdings:
                fragment = _clean(text)
                prefix = pending_rating_agency
                holdings[-1]["rating"] = (
                    f"{prefix} {fragment}".strip() if prefix else fragment
                )
                pending_rating_agency = None
            i += 1
            continue

        # PDF line wrapping can leave a legal suffix such as "Ltd." on the
        # line immediately after the percentage. Attach it to the previous
        # issuer instead of treating it as a new holding/category.
        if not _PERCENT_RE.search(text) and holdings and _is_name_continuation(text):
            holdings[-1]["issuer"] = _clean(f"{holdings[-1]['issuer']} {text}")
            i += 1
            continue

        split = _split_trailing_tokens(text, columns)
        if split is None:
            # Merge only short continuation lines. Do not merge a known
            # category heading with the following security row.
            normalized = _normalize_label(text)
            if (
                normalized in _NORMALIZED_EQUITY_SECTORS
                or normalized in _NORMALIZED_DEBT_CATEGORY
                or normalized in {_normalize_label(x) for x in _COUNTRY_SUBTOTALS}
            ):
                current_sector = text
                i += 1
                continue

            combined = text
            merged = False
            for j in range(i + 1, min(i + 4, len(lines))):
                nxt = _clean(lines[j]["text"])
                if not nxt or _is_stop_line(nxt):
                    break
                # Never merge across a recognised boundary line. Leftover
                # column-label noise (e.g. a lone "AUM" fragment split off
                # from "Total AUM / Derivatives / Net AUM") would otherwise
                # keep hunting forward for a percentage and swallow the
                # actual group title / category heading that follows it
                # (e.g. "Equity & Equity Related" then "REITS 1.58 %"),
                # corrupting the merged text into a bogus issuer name and
                # silently destroying the real category header in the
                # process. Stop here instead; the unmerged noise line is
                # simply dropped below, and the boundary line is then
                # handled correctly on its own next iteration.
                nxt_normalized = _normalize_label(nxt)
                if (
                    _is_group_title(nxt)
                    or _is_table_header_noise(nxt)
                    or nxt_normalized in _NORMALIZED_EQUITY_SECTORS
                    or nxt_normalized in _NORMALIZED_DEBT_CATEGORY
                    or nxt_normalized
                    in {_normalize_label(x) for x in _COUNTRY_SUBTOTALS}
                ):
                    break
                combined = f"{combined} {nxt}"
                split = _split_trailing_tokens(combined, columns)
                if split is not None:
                    i = j
                    merged = True
                    break
            if not merged:
                i += 1
                continue

        name, rating, pct_values = split
        if not name:
            i += 1
            continue

        # A standalone agency line may precede the issuer, with the actual
        # rating token appearing on the following physical line. Keep the
        # agency out of the company name and let the next rating fragment
        # complete the internal rating value.
        if pending_rating_agency and rating is None:
            trailing_agency = re.search(
                r"\b(?:CRISIL|ICRA|CARE|IND)\s*$",
                name,
                re.IGNORECASE,
            )
            if trailing_agency:
                name = _clean(name[: trailing_agency.start()])
        name = _strip_embedded_header_prefix(name)
        if not name:
            i += 1
            continue

        if rating is not None:
            pending_rating_agency = None

        if not current_sector:
            prefix_sector = _leading_sector_prefix(name)
            if prefix_sector:
                current_sector = prefix_sector

        # Cash / net-receivable rows are real portfolio lines. Keep them so
        # cash-only ETFs have a valid 100% holding instead of an empty table.
        if rating is None and (_is_cash_row(name) or _is_investment_category_row(name)):
            values = _assign_pct_values(pct_values, columns)
            holdings.append(
                {
                    "issuer": name,
                    "sector": name,
                    "rating": rating,
                    **values,
                }
            )
            i += 1
            continue

        # Category/subtotal rows are state changes, not holdings. This includes
        # International Exposure and Commodity & Commodity Related.
        if rating is None and (
            _is_equity_sector(name)
            or _is_debt_category(name)
            or _looks_like_country(name)
        ):
            # Country subtotal rows define the next level of the
            # International Exposure hierarchy. They are not holdings.
            if _looks_like_country(name):
                current_sector = name
            else:
                current_sector = name
            i += 1
            continue

        values = _assign_pct_values(pct_values, columns)
        holdings.append(
            {
                "issuer": name,
                "sector": current_sector,
                "rating": rating,
                **values,
            }
        )
        i += 1

    return holdings, current_sector


def _dedupe_holdings(holdings):
    """Only dedupe the known Silver/Gold ETF duplicate label."""
    result = []
    seen = set()
    for h in holdings:
        sector = _normalize_label(h.get("sector", ""))
        issuer = _normalize_label(h.get("issuer", ""))
        if sector == "commodity & commodity related" and issuer in {"silver", "gold"}:
            key = (sector, issuer, h.get("pct_net_assets"))
            if key in seen:
                continue
            seen.add(key)
        result.append(h)
    return result


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def extract_holdings(page, carry_state=None):
    """Extract all portfolio holdings from one page.

    ``carry_state`` maps a column schema (tuple of column names, e.g.
    ``("rating",)`` vs ``("pct_total_aum", "pct_derivatives",
    "pct_net_assets")``) to the last sector seen for that schema on a
    previous page. A page can carry two unrelated tables side by side (a
    debt block with a Rating column next to an equity block without one),
    so a single scalar "last sector" is not enough -- each schema needs its
    own carried sector, otherwise a first-on-page block can inherit the
    wrong table's sector. This also covers the plain single-table case,
    where it degenerates to carrying one sector forward as before.

    If a block's own rows do start with a real category header, that
    header immediately overrides this seed inside ``_parse_table_lines``,
    so passing a stale seed is harmless.

    Returns a ``(holdings, trailing_carry_state)`` tuple so the caller can
    chain ``carry_state`` into the next page belonging to the same scheme.
    """
    carry_state = dict(carry_state) if carry_state else {}
    ranges = _find_column_ranges(page)
    if not ranges:
        return [], carry_state

    holdings_by_block = {}
    column_state = []
    previous_state = None
    orphan_candidates = []

    for block_index, rng in enumerate(ranges):
        seed_sector = ""
        schema_key = tuple(rng["columns"])

        # Continue a table in the same physical column when the schema is the
        # same. This handles a sector subtotal at the bottom of one block and
        # its holdings continuing below a repeated header.
        best_state = None
        best_distance = None
        for state in column_state:
            if state["columns"] != rng["columns"]:
                continue
            distance = abs(state["x0"] - rng["left"])
            if distance <= 30 and (best_distance is None or distance < best_distance):
                best_state = state
                best_distance = distance
        if best_state is not None:
            seed_sector = best_state["sector"]

        # If two side-by-side blocks share the same header row and schema, the
        # right block can be a continuation of the left block's sector/country.
        if (
            not seed_sector
            and previous_state is not None
            and previous_state["columns"] == rng["columns"]
            and abs(previous_state["top"] - rng["header_top"]) < 3
        ):
            seed_sector = previous_state["sector"]

        # No block on this page yet shares this schema. Fall back to the
        # sector carried over from the previous PDF page of this same
        # scheme, keyed by schema so a debt table's carry-over never bleeds
        # into an unrelated equity table that happens to start the page.
        if not seed_sector and schema_key in carry_state:
            seed_sector = carry_state[schema_key]

        lines = _column_lines(page, rng)
        block_holdings, last_sector = _parse_table_lines(
            lines, rng["columns"], seed_sector
        )
        holdings_by_block[block_index] = block_holdings

        state = {
            "x0": rng["left"],
            "columns": rng["columns"],
            "sector": last_sector,
            "top": rng["header_top"],
        }
        previous_state = state

        updated = False
        for old_state in column_state:
            if (
                old_state["columns"] == rng["columns"]
                and abs(old_state["x0"] - rng["left"]) <= 30
            ):
                old_state.update(state)
                updated = True
                break
        if not updated:
            column_state.append(state)

        # A block whose very first holding still has no sector, with no
        # seed available from anywhere yet, is a candidate for the "snake"
        # case below: its real category heading may only ever be printed
        # once, at the bottom of a DIFFERENT same-schema column elsewhere
        # on this same page (ABSL wraps one long equity/debt table across
        # two side-by-side columns purely to balance print height; the
        # wrapped column's first row can be the tail end of the previous
        # column's last category, with no heading of its own).
        if not seed_sector and block_holdings and not block_holdings[0]["sector"]:
            orphan_candidates.append(block_index)

    # Second pass: resolve orphan candidates now that every OTHER block's
    # trailing sector on this page is known -- including ones that appear
    # physically further down the page than the orphan itself, which the
    # single top-to-bottom pass above could not yet see. Only ever borrow
    # from a same-schema block in a DIFFERENT physical column (x0 further
    # away than the normal same-column tolerance); if more than one such
    # column exists, the one positioned furthest right is the one whose
    # category list ends immediately before this orphan continues it, since
    # ABSL fills a wrapped table's left column fully before continuing at
    # the top of the right one.
    for block_index in orphan_candidates:
        rng = ranges[block_index]
        candidates = [
            s
            for s in column_state
            if s["columns"] == rng["columns"] and abs(s["x0"] - rng["left"]) > 30
        ]
        if not candidates:
            continue
        seed_sector = max(candidates, key=lambda s: s["top"])["sector"]
        if not seed_sector:
            continue

        lines = _column_lines(page, rng)
        block_holdings, last_sector = _parse_table_lines(
            lines, rng["columns"], seed_sector
        )
        holdings_by_block[block_index] = block_holdings
        for s in column_state:
            if s["columns"] == rng["columns"] and abs(s["x0"] - rng["left"]) <= 30:
                s["sector"] = last_sector
                break

    holdings = []
    for block_index in range(len(ranges)):
        holdings.extend(holdings_by_block.get(block_index, []))

    # A page can hold more than one physical column sharing the same table
    # schema (a wide equity/debt table wrapped left-then-right for print
    # height, exactly as above). Whichever of those columns sits furthest
    # right is the one that continues onto the NEXT page in reading order,
    # so THAT column's trailing sector -- not simply whichever block
    # happened to be reached last while scanning top-to-bottom -- is what
    # must be carried forward. Using top-to-bottom order here previously
    # let an earlier (left-hand) column's sector silently overwrite a later
    # (right-hand) column's, mislabelling the next page's opening holdings
    # with the wrong sector instead of merely leaving one row blank.
    trailing_by_schema = {}
    for state in column_state:
        schema_key = tuple(state["columns"])
        current = trailing_by_schema.get(schema_key)
        if current is None or state["x0"] > current["x0"]:
            trailing_by_schema[schema_key] = state
    for schema_key, state in trailing_by_schema.items():
        carry_state[schema_key] = state["sector"]

    return _dedupe_holdings(holdings), carry_state


_BENCHMARK_ARTIFACT_RE = re.compile(
    r"(?:Market\s+Value\s+of\s+Amount\s+Invested|Scheme\s+Returns|"
    r"Investment\s+Performance|SIP\s*Performance|\(CAGR\)|#|##)",
    re.IGNORECASE,
)


def _collapse_repeated_runs(text: str) -> str:
    """Collapse an immediately-repeated run of words.

    ABSL's flattened text can interleave two visually-overlapping columns
    (e.g. the investment-objective narrative and the benchmark formula both
    mentioning the same composite benchmark), which duplicates a whole
    phrase back-to-back. Only runs of 3+ words are collapsed, to avoid
    touching legitimate short repeats.
    """
    words = text.split()
    n = len(words)
    out: list[str] = []
    i = 0
    while i < n:
        matched = False
        max_w = (n - i) // 2
        for w in range(max_w, 2, -1):
            if words[i : i + w] == words[i + w : i + 2 * w]:
                out.extend(words[i : i + w])
                i += 2 * w
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    return " ".join(out)


def _clean_extracted_benchmark(
    value: str | None, additional: str | None = None
) -> str | None:
    """Remove right-column/performance-table text accidentally joined to Benchmark."""
    if not value:
        return None
    value = _clean(value)
    value = _collapse_repeated_runs(value)

    # The flattened PDF can concatenate the Additional Benchmark directly to
    # the primary benchmark (notably ELSS). Keep the primary benchmark only.
    if additional:
        add = _clean(additional)
        if add:
            value = re.split(re.escape(add), value, maxsplit=1, flags=re.IGNORECASE)[
                0
            ].strip(" -:,")

    # Never let performance-table labels/numbers become part of the benchmark.
    value = _BENCHMARK_ARTIFACT_RE.split(value, maxsplit=1)[0].strip(" -:,")

    # A stray trailing numeric performance sequence is another common text
    # layer artifact after a benchmark. Benchmarks themselves can contain
    # percentages (e.g. composite FOF benchmarks), so only strip a run that
    # starts after a clear table boundary handled above.
    return _clean(value) or None


def _is_aum_subtotal_artifact(company: str) -> bool:
    """Recognise the PDF's AUM label accidentally glued to a category subtotal."""
    value = _normalize_label(company)
    return bool(re.fullmatch(r"a(u)?m\s+equity\s+&\s+equity\s+related\s+reits", value))


def _normalise_extracted_holdings(holdings: list[dict]) -> list[dict]:
    result = []
    category_labels = _NORMALIZED_EQUITY_SECTORS | _NORMALIZED_DEBT_CATEGORY
    country_labels = {_normalize_label(x) for x in _COUNTRY_SUBTOTALS}

    for holding in holdings:
        company = _clean(holding.get("company", ""))
        if not company:
            continue

        normalized_company = _normalize_label(company)
        if (
            normalized_company in category_labels
            or normalized_company in country_labels
        ):
            continue
        if _is_aum_subtotal_artifact(company):
            continue
        if normalized_company == "preferred stock":
            continue

        holding["company"] = company
        result.append(holding)

    return result


def _append_missing_cash_rows(
    pdf, page_idxs: list[int], holdings: list[dict]
) -> list[dict]:
    """Recover Net Cash rows when coordinate clipping excludes the final table row."""
    cash_patterns = (
        r"Net\s+Cash\s+and\s+Cash\s+Equivalent",
        r"Cash\s*&\s*Current\s+Assets",
        r"Cash\s*&\s*Cash\s+Equivalents",
    )
    existing_cash = any(_is_cash_row(h.get("company", "")) for h in holdings)
    if existing_cash:
        return holdings

    found = None
    for idx in page_idxs:
        text = _page_text(pdf.pages[idx])
        for pat in cash_patterns:
            m = re.search(pat + r"\s+(-?\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
            if m:
                found = m.group(1)
                break
        if found is not None:
            break

    if found is not None:
        holdings.append(
            {
                "company": "Net Cash and Cash Equivalent",
                "sector": "Net Cash and Cash Equivalent",
                "pct_to_net_assets": found,
            }
        )
    return holdings


def extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
    """
    Same public output contract as the existing extractor.

    Returns exactly:
        benchmark, additional_benchmark, isin, fund_managers, holdings,
        holdings_count
    """
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
    carry_state = {}

    for idx in page_idxs:
        page = pdf.pages[idx]
        text = _page_text(page)

        if benchmark is None:
            benchmark = extract_benchmark(text)
        if additional_benchmark is None:
            additional_benchmark = extract_additional_benchmark(text)
        if benchmark is not None:
            benchmark = _clean_extracted_benchmark(benchmark, additional_benchmark)
        if not isin:
            isin = extract_isin(text)

        for manager in _extract_fund_managers_from_page(page):
            if manager not in managers:
                managers.append(manager)

        page_holdings, carry_state = extract_holdings(page, carry_state)
        for h in page_holdings:
            pct = h["pct_net_assets"] or h["pct_total_aum"] or "0"
            holding = {
                "company": h["issuer"],
                "sector": h["sector"] or (h["rating"] or ""),
                "pct_to_net_assets": pct,
            }
            # Do not deduplicate exact issuer/value rows here. ABSL can
            # legitimately print multiple positions with the same issuer,
            # rating and percentage (notably separate derivative contracts).
            # Deduplicating them changes the portfolio arithmetic and can make
            # a scheme look incorrectly flagged for review. Known source-label
            # artifacts are handled inside _dedupe_holdings().
            holdings.append(holding)

    holdings = _normalise_extracted_holdings(holdings)
    holdings = _append_missing_cash_rows(pdf, page_idxs, holdings)

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
