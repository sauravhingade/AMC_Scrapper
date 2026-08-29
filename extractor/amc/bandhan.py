"""
Bandhan Mutual Fund extractor.
"""

import re
import statistics

from ..config import HEADING_EXCLUDE, SCHEME_KEYWORDS

# Bandhan's per-scheme pages always carry one of these markers (the FUND
# FEATURES panel heading, the Fund Manager field, the Benchmark field, or
# the portfolio table's "% of NAV" column header). Multi-page schemes (a
# portfolio table that spills onto a second page) repeat "% of NAV" on the
# continuation page even though "FUND FEATURES"/"Fund Manager:" only appear
# once, on the first page. Field labels are colon-qualified (not just the
# bare words) because "Benchmark" and "Fund Manager" alone also appear
# throughout the back-of-book performance/SIP appendix tables and the
# glossary, well past the actual per-scheme pages -- an unqualified match
# would keep gluing those appendix pages onto whichever scheme happened to
# be last, and "% of NAV" would falsely fire even earlier in that glossary
# copy for the same reason.
BODY_MARKERS = re.compile(
    r"FUND FEATURES|Fund\s+Manager[\^\s]*:|Benchmark\s*:|%\s*of\s*NAV",
    re.IGNORECASE,
)

# Trailing footnote-marker glyphs (£, ¥, §, ¢, ß, ^, @, $, *, † and similar,
# used throughout this factsheet to flag scheme-name footnotes) are stripped
# before testing whether a line looks like a scheme heading. This matters
# because some of them -- notably "ß" -- are Unicode *letters*, so a
# raw "\bFUND\b" boundary check silently fails to match inside "Fundßßß"
# and would otherwise misdetect it as plain body text rather than a new
# scheme heading, causing two distinct schemes to merge under one heading.
_TRAILING_FOOTNOTE_RE = re.compile(r"[^A-Za-z0-9\s():&,/'\-]+$")

# A generic structural fallback for the handful of scheme-type suffixes
# ("FOF", "ETF") that a shared, AMC-agnostic SCHEME_KEYWORDS list may not
# happen to include. This never hardcodes an individual scheme's name --
# only the two-word "Bandhan <...> <generic scheme-type word>" shape common
# to every heading in this factsheet.
_FALLBACK_HEADING_RE = re.compile(
    r"^Bandhan\b.{0,70}\b(?:Fund|FOF|ETF)\b", re.IGNORECASE
)


def _strip_trailing_footnote_symbols(line: str) -> str:
    return _TRAILING_FOOTNOTE_RE.sub("", line).strip()


def _is_scheme_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    normalized = _strip_trailing_footnote_symbols(line)
    if any(ex in normalized.upper() for ex in HEADING_EXCLUDE):
        return False
    upper = normalized.upper()
    if any(re.search(rf"\b{kw}\b", upper) for kw in SCHEME_KEYWORDS):
        return True
    return bool(_FALLBACK_HEADING_RE.match(normalized))


# "Click here to Know more" is a boilerplate call-to-action link appended to
# most scheme headings in this factsheet; it is not part of the scheme's
# actual name and must not leak into scheme_name in the output.
_CTA_SUFFIX_RE = re.compile(r"\s*Click here to Know more\s*$", re.IGNORECASE)


def _clean_scheme_name(line: str) -> str:
    """Strip the boilerplate "Click here to Know more" call-to-action and
    any trailing footnote-marker glyphs (see _strip_trailing_footnote_symbols)
    from a raw heading line, leaving just the scheme's actual name -- e.g.
    "Bandhan US specific Equity Active FOF¢¢ Click here to Know more"
    becomes "Bandhan US specific Equity Active FOF"."""
    name = _CTA_SUFFIX_RE.sub("", line)
    name = _strip_trailing_footnote_symbols(name)
    return name.strip()


def segment_schemes(pdf) -> dict[str, list[int]]:
    """Returns {scheme_name: [page_index, ...]} in document order.

    Keys are the *cleaned* scheme name (CTA text and footnote glyphs
    stripped) so downstream consumers never see raw factsheet boilerplate.
    """
    scheme_pages: dict[str, list[int]] = {}
    current = None

    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        first_line = text.split("\n")[0].strip() if text else ""

        if _is_scheme_heading(first_line):
            current = _clean_scheme_name(first_line)
            scheme_pages.setdefault(current, [])

        if current and BODY_MARKERS.search(text):
            if i not in scheme_pages[current]:
                scheme_pages[current].append(i)

    return scheme_pages


# ---------------------------------------------------------------------------
# Text cleanup
# ---------------------------------------------------------------------------

# This factsheet's embedded font is missing ToUnicode entries for a handful
# of glyphs, so pdfplumber falls back to literal "(cid:N)" placeholders for
# them. The affected letters were identified by cross-referencing repeated,
# recognisable words across many scheme pages (e.g. "(cid:46)arnataka" next
# to other "... SDL" state names -> (cid:46) is a capital K; "Ra(cid:77)asthan"
# next to "Gu(cid:77)arat" -> (cid:77) is a lowercase j; "NIFT(cid:60)" next to
# plain "NIFTY" elsewhere -> (cid:60) is a capital Y). These are generic
# font-level substitutions, not scheme-specific text.
CID_FIXUPS = {
    "(cid:10)": "'",
    "(cid:34)": '"',
    "(cid:35)": "@",
    "(cid:46)": "K",
    "(cid:60)": "Y",
    "(cid:77)": "j",
    "(cid:110)": "(",
    "(cid:111)": ")",
    "(cid:431)": "ffi",
}


def _clean(text: str) -> str:
    if not text:
        return ""
    for cid, ch in CID_FIXUPS.items():
        if cid in text:
            text = text.replace(cid, ch)
    text = text.replace("\u00a0", " ")
    text = text.replace("\u00ad", "")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[•●▪◦]", " ", text)
    text = re.sub(r"[\ue000-\uf8ff]", " ", text)
    # Any remaining, unmapped (cid:N) placeholder carries no real text value.
    text = re.sub(r"\(cid:\d+\)", "", text)
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
# Scheme metadata (benchmark / additional benchmark / ISIN / fund managers)
# ---------------------------------------------------------------------------


def extract_benchmark(text):
    if not text:
        return None
    m = re.search(
        r"\bBenchmark\s*:\s*(.+?)(?=\n\s*SIP\b|\n\s*Minimum Investment|"
        r"\n\s*Option Available|\n\s*Exit Load|\n\s*Investment Objective|"
        r"\n\s*NAV\b|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return _clean(m.group(1)) if m else None


def extract_additional_benchmark(page):
    """Unlike Abakkus, Bandhan's FUND FEATURES panel has no labelled
    "Additional Benchmark:" field at all. The secondary/additional
    benchmark is only identifiable from the Performance Table further
    down the page, where the row for it is marked with a trailing "##"
    straight after the index name, immediately followed by its return
    figures -- e.g. "Nifty 50 TRI## -5.42% 8.80% ...". The footnote
    legend line ("#Benchmark Returns. ##Additional Benchmark Returns.")
    also contains "##", so a numeric lookahead is required to tell the
    two apart.

    The riskometer widget is laid out at the same page height as this
    row, so a plain top-to-bottom, left-to-right text reconstruction can
    interleave stray riskometer label fragments ("Low", "High", ...)
    into the same line as the benchmark name. Reading actual word
    positions and only walking backward through *horizontally contiguous*
    words (small x-gaps) from the "##" token keeps those unrelated,
    far-off riskometer fragments out.

    Because of this, extract_additional_benchmark works from the ``page``
    object directly (see extract_scheme_fields) rather than from the
    FUND-FEATURES-only metadata_text used for the other metadata fields.
    """
    words = _page_words(page)
    rows = []
    for w in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(w["top"])
        placed = False
        for r in reversed(rows[-3:]):
            if abs(r["top"] - top) <= 1.5:
                r["words"].append(w)
                placed = True
                break
        if not placed:
            rows.append({"top": top, "words": [w]})

    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        for i, w in enumerate(ws):
            text = w["text"]
            if not text.endswith("##") or "Returns" in text:
                continue
            nxt = ws[i + 1]["text"] if i + 1 < len(ws) else ""
            if not re.match(r"^-?\d", nxt):
                continue
            name_words = [text[:-2]]
            prev_x0 = w["x0"]
            for j in range(i - 1, -1, -1):
                pw = ws[j]
                if prev_x0 - pw["x1"] > 20:
                    break
                name_words.insert(0, pw["text"])
                prev_x0 = pw["x0"]
            name = _clean(" ".join(name_words))
            if name:
                return name
    return None


_FACTSHEET_DATE_RE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\s+([A-Za-z]+)\s+(\d{4})\b")


def extract_factsheet_month(pdf):
    """The "as on" reporting date printed near the top of every scheme
    page (e.g. "30th June 2026"), normalized to a "Month YYYY" string
    (e.g. "June 2026"). Deliberately scoped to just the first few lines
    of each page rather than the whole page text: the same day-month-year
    shape also appears deep in portfolio tables as bond maturity dates and
    scheme inception dates, which are unrelated to the factsheet's own
    reporting date and must not be matched instead.
    """
    for page in pdf.pages:
        text = page.extract_text() or ""
        for line in text.split("\n")[:5]:
            m = _FACTSHEET_DATE_RE.search(line)
            if m:
                return f"{m.group(1)} {m.group(2)}"
    return None


def extract_isin(text):
    if not text:
        return ""
    m = re.search(r"\bISIN\s*:?\s*([A-Z0-9]{6,20})\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""


_SLEEVE_LABEL_RE = re.compile(
    r"\b(Equity|Debt|Fixed\s+Income|Commodity|Commodities)\s+Portion\s*:",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,4})",
    re.IGNORECASE,
)


def _normalize_sleeve(label: str):
    low = label.lower()
    if "debt" in low or "fixed" in low:
        return "Debt"
    if "commodit" in low:
        return "Commodity"
    return "Equity"


def extract_fund_managers(text):
    """Bandhan labels a manager's sleeve *before* their name for hybrid /
    multi-asset schemes -- "Fund Manager^^:Equity Portion: Mr. X ... Debt
    Portion: Mr. Y" -- the reverse of a "name, then sleeve" convention.
    Single-sleeve schemes list one or more managers with no sleeve label
    at all. Both shapes are handled by splitting the captured text on any
    "<Sleeve> Portion:" labels first, then pulling every "Mr./Ms./Mrs./Dr.
    <Name>" out of each resulting segment.
    """
    if not text:
        return []

    m = re.search(
        r"\bFund\s+Manager\b[^:]*:\s*(.+?)(?=\n\s*Other Parameter\b|"
        r"\n\s*Base Expense Ratio\b|\n\s*Benchmark\s*:|\n\s*Category\s*:|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return []
    manager_text = m.group(1)

    parts = _SLEEVE_LABEL_RE.split(manager_text)
    segments = []
    if len(parts) == 1:
        segments.append((None, manager_text))
    else:
        if parts[0].strip():
            segments.append((None, parts[0]))
        for i in range(1, len(parts), 2):
            label = parts[i]
            seg_text = parts[i + 1] if i + 1 < len(parts) else ""
            segments.append((_normalize_sleeve(label), seg_text))

    managers = []
    for sleeve, seg_text in segments:
        for match in _NAME_RE.finditer(seg_text):
            name = _clean(match.group(1))
            name = re.sub(
                r"\s+(?:Equity|Fixed Income|Debt)$", "", name, flags=re.IGNORECASE
            )
            name = _clean(name)
            if not name:
                continue
            entry = {"role": "Fund Manager", "name": name, "sleeve": sleeve}
            if entry not in managers:
                managers.append(entry)
    return managers


# ---------------------------------------------------------------------------
# Portfolio / holdings table detection
# ---------------------------------------------------------------------------

# Bandhan's portfolio tables all share one column layout: an instrument-name
# column, an optional sector/rating column, and a "% of NAV" column -- but
# the header text varies by scheme type:
#   "Company/Instrument  Industry/Rating  % of NAV"   (equity funds)
#   "Name  Rating  % of NAV"                          (debt funds)
#   "Name  Industries  % of NAV"                      (arbitrage funds)
#   "Name  % of NAV"                                  (FOF / ETF-of-FOF, no
#                                                       sector column at all)
# A page can carry the header twice side by side (two-column layout used by
# arbitrage/hybrid/debt funds to fit more rows per page).
_NAME_LABELS = {"Company/Instrument", "Name"}
_SECTOR_LABELS = {"Industry/Rating", "Industries", "Rating"}


def _find_portfolio_headers(page):
    """Returns a list of {top, name_x0, sector_x0, nav_x0} for every
    portfolio-table column group on the page, left to right, top to
    bottom."""
    words = _page_words(page)
    rows = []
    for w in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(w["top"])
        placed = False
        for r in reversed(rows[-3:]):
            if abs(r["top"] - top) <= 1.5:
                r["words"].append(w)
                placed = True
                break
        if not placed:
            rows.append({"top": top, "words": [w]})

    headers = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        nav_positions = []
        for i in range(len(ws) - 2):
            if (
                ws[i]["text"] == "%"
                and ws[i + 1]["text"] == "of"
                and ws[i + 2]["text"] == "NAV"
            ):
                nav_positions.append(ws[i]["x0"])
        if not nav_positions:
            continue
        name_positions = [w for w in ws if w["text"] in _NAME_LABELS]
        for nw in name_positions:
            candidates = [p for p in nav_positions if p > nw["x0"]]
            if not candidates:
                continue
            nav_x0 = min(candidates)
            sector_x0 = None
            for w in ws:
                if w["text"] in _SECTOR_LABELS and nw["x0"] < w["x0"] < nav_x0:
                    sector_x0 = w["x0"]
                    break
            headers.append(
                {
                    "top": r["top"],
                    "name_x0": nw["x0"],
                    "sector_x0": sector_x0,
                    "nav_x0": nav_x0,
                }
            )

    unique = []
    for h in headers:
        if not any(
            abs(h["name_x0"] - x["name_x0"]) < 2 and abs(h["top"] - x["top"]) < 3
            for x in unique
        ):
            unique.append(h)
    return sorted(unique, key=lambda h: (h["top"], h["name_x0"]))


# Bare instrument/category labels that appear as their own row in a debt (or
# mixed equity+debt) portfolio table, sometimes with their own aggregate
# percentage (e.g. "Certificate of Deposit  65.79%") and sometimes with none
# at all (just the label, with the aggregate given later as "... Total").
# These describe the *category*, never an individual holding, so they must
# never be counted as one. This is standard Indian mutual-fund portfolio
# vocabulary (AMFI/SEBI instrument categories), not scheme-specific text.
_CATEGORY_LABELS = {
    "equity",
    "equity futures",
    "debt",
    "certificate of deposit",
    "commercial paper",
    "treasury bill",
    "treasury bills",
    "government bond",
    "government bonds",
    "government securities",
    "government securities/treasury bills",
    "state government bond",
    "state government bonds",
    "state government securities",
    "corporate bond",
    "corporate bonds",
    "corporate bond & ncds",
    "non-convertible debentures",
    "non convertible debentures",
    "mutual fund units",
    "domestic mutual fund units",
    "international mutual fund units",
    "tri party repo",
    "treps",
    "treps / reverse repo",
    "treps/reverse repo",
    "reverse repo",
    "repo",
    "cblo",
    "corporate debt market development fund",
    "zero coupon bond",
    "exchange traded funds",
    "invit",
    "invits",
    "reit",
    "reits",
    "alternative investment fund",
    "fixed deposit",
    "fixed deposits",
    "preference shares",
    "warrants",
    "securitised debt",
    "pass through certificates",
}

# Unlike every other entry in _CATEGORY_LABELS -- which are pure rollups of
# instruments that are *also* itemized individually elsewhere in the same
# table, so including the rollup too would double-count -- these labels
# represent real allocation that has no itemized breakdown anywhere in the
# factsheet:
#   - "Others Equity" is the aggregate of index/portfolio constituents below
#     the individually-named "Top Holdings" cutoff (common for broad index
#     funds tracking hundreds of names) -- it is genuine, otherwise
#     completely invisible equity allocation, not a subtotal of rows above it.
#   - "Net Cash and Cash Equivalent" / "Cash & Cash Equivalent" / "Net
#     Receivables/(Payables)" are the fund's actual cash or short-term
#     borrowing position -- a real (sometimes negative) part of the
#     portfolio, not a security, but excluding it makes the reported
#     holdings never reconcile to Grand Total, which matters for consumers
#     of this data (e.g. an app rendering an allocation/holdings chart that
#     is expected to sum to ~100%).
# These are therefore *included* as holdings, under a clean canonical name,
# rather than skipped.
_TERMINAL_ALLOCATION_LABELS = {
    "others equity": "Others Equity",
    "cash & cash equivalent": "Cash & Cash Equivalent",
    "cash and cash equivalent": "Cash & Cash Equivalent",
    "net cash and cash equivalent": "Net Cash and Cash Equivalent",
    "net cash & cash equivalent": "Net Cash and Cash Equivalent",
    "net receivables / (payables)": "Net Receivables/(Payables)",
    "net receivables/(payables)": "Net Receivables/(Payables)",
}

# Markers for the definitive end of a portfolio table / page section. Unlike
# category labels above (which are skipped but scanning continues, since
# more holdings usually follow further down), these mark a hard stop.
_STOP_PREFIXES = (
    "grand total",
    "sector allocation",
    "market cap",
    "industry allocation",
    "rating allocation",
    "rating profile",
    "asset allocation",
    "maturity profile",
    "asset quality",
    "potential risk class",
    "performance table",
    "this product is suitable",
    "riskometer",
    "sip performance",
    "product suitability",
    "fund manager details",
)

# Markers for the page footer / next section, used only as a defensive
# secondary bound on how far down the page a column may be scanned (the
# primary bound is the hard "grand total" stop below). Deliberately excludes
# routine FUND FEATURES fields such as "Investment Objective:" or "NAV (`)"
# which appear at very different heights scheme to scheme and would
# otherwise truncate short portfolios prematurely.
#
# Matched with search(), not match(): a marker heading and unrelated
# metadata prose in the facing FUND FEATURES column can legitimately land
# within the same tight-line y-tolerance band purely by coincidence (e.g.
# "...as the date of the" ending at the same height as "SECTOR ALLOCATION"
# starts), which -- since words are joined in left-to-right x order --
# pushes the marker text away from the front of the reconstructed line and
# a start-anchored match would silently miss it.
#
# Case-sensitive (not IGNORECASE): these particular section titles are
# always rendered in full uppercase in Bandhan's template, whereas some of
# them can also appear as ordinary lowercase/mixed-case prose elsewhere on
# the same page -- e.g. a Balanced Advantage / Dynamic Asset Allocation
# scheme's own category description literally contains the words "asset
# allocation". Case-sensitive matching lets the genuine ALL-CAPS section
# heading match while leaving that prose alone. "This product is suitable"
# and "Riskometer" are mixed-case in the template but distinctive enough
# not to collide with any routine field text, so they stay case-insensitive.
_BOTTOM_MARKERS_CASED = re.compile(
    r"(SECTOR ALLOCATION|MARKET CAP|INDUSTRY ALLOCATION|RATING ALLOCATION|"
    r"RATING PROFILE|ASSET ALLOCATION|MATURITY PROFILE|ASSET QUALITY|"
    r"POTENTIAL RISK CLASS|PERFORMANCE TABLE)"
)
_BOTTOM_MARKERS_CI = re.compile(r"(This product is suitable|Riskometer)", re.IGNORECASE)

_PCT_TOKEN_RE = re.compile(r"^-?\d+(?:\.\d+)?%$")


def _is_category_label(name: str) -> bool:
    n = name.lower().strip()
    n = re.sub(r"\s+total$", "", n)
    return n in _CATEGORY_LABELS


def _is_stop(name: str) -> bool:
    n = name.lower().strip()
    return any(n.startswith(p) for p in _STOP_PREFIXES)


def _strip_category_prefix(company: str) -> str:
    """A bare category header with no percentage of its own (e.g. "State
    Government Bond") has no anchor to attach to and so gets swept into the
    *following* holding's name by nearest-anchor attachment (see
    _extract_holdings_for_group). Strip a leading category-label token so
    "State Government Bond 7.17% ... SDL" becomes "7.17% ... SDL".
    """
    low = company.lower()
    for cat in sorted(_CATEGORY_LABELS, key=len, reverse=True):
        if low == cat:
            return company
        if low.startswith(cat + " "):
            return company[len(cat) :].strip()
    return company


def _compute_bottom_bound(page):
    words = _page_words(page)
    rows = []
    for w in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(w["top"])
        placed = False
        for r in reversed(rows[-3:]):
            if abs(r["top"] - top) <= 1.5:
                r["words"].append(w)
                placed = True
                break
        if not placed:
            rows.append({"top": top, "words": [w]})
    bound = float(page.height)
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        line = _clean(" ".join(w["text"] for w in ws))
        if _BOTTOM_MARKERS_CASED.search(line) or _BOTTOM_MARKERS_CI.search(line):
            bound = min(bound, r["top"])
    return bound


def _extract_holdings_for_group(page, header, next_header_x0, page_width, bottom_bound):
    """Extract holdings for one "<Name> [<Sector/Rating>] % of NAV" column
    group.

    Row splitting is anchored on the "% of NAV" figure rather than on
    visual line breaks: every word is attached to its *nearest* pct-token
    by vertical distance. This handles both a name that wraps across two
    lines before its rating/pct appear (common for long issuer names in the
    narrower debt-table columns) and a sector/rating label that itself
    wraps onto a second line -- both of which break a naive "group by tight
    line" approach, since the pieces of one logical row can legitimately
    span multiple, unevenly spaced visual lines.
    """
    words = _page_words(page)
    name_left = header["name_x0"] - 3
    name_right = header["sector_x0"] - 5 if header["sector_x0"] else None
    right_edge = (next_header_x0 - 5) if next_header_x0 else page_width
    start_top = header["top"] + 4

    col_words = [
        w
        for w in words
        if right_edge >= w["x0"] >= name_left and start_top <= w["top"] < bottom_bound
    ]

    # Bond/SDL/G-Sec instrument names frequently embed their own coupon
    # rate as a leading "7.3%"-style token (e.g. "7.3% - 2053 G-Sec"). That
    # token sits in the NAME sub-range and must never be mistaken for the
    # row's actual "% of NAV" allocation figure, which always sits in the
    # VALUE sub-range (after the rating/sector). So when a sector/rating
    # sub-column exists, anchors are only searched for there.
    if name_right is not None:
        anchor_pool = [w for w in col_words if w["x0"] >= name_right]
    else:
        anchor_pool = col_words
    anchors = sorted(
        [w for w in anchor_pool if _PCT_TOKEN_RE.match(w["text"])],
        key=lambda w: w["top"],
    )
    if not anchors:
        return []

    anchor_tops = [a["top"] for a in anchors]
    buckets = [{"name_words": [], "sector_words": []} for _ in anchors]

    # A continuation word should never be closer to some other row's anchor
    # than to its own; cap attachment distance at a generous multiple of
    # the typical anchor-to-anchor gap so that stray, far-away text (e.g. a
    # mis-scoped bottom bound letting footer prose leak in) can never be
    # swept into a holding.
    gaps = [
        anchor_tops[i + 1] - anchor_tops[i]
        for i in range(len(anchor_tops) - 1)
        if anchor_tops[i + 1] - anchor_tops[i] > 0
    ]
    typical_gap = statistics.median(gaps) if gaps else 12.0
    max_attach = max(typical_gap * 1.5, 12.0)

    anchor_ids = {id(a) for a in anchors}
    for w in col_words:
        if id(w) in anchor_ids:
            continue
        best_i, best_d = None, None
        for i, at in enumerate(anchor_tops):
            d = abs(w["top"] - at)
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        if best_d is None or best_d > max_attach:
            continue
        if name_right is not None and w["x0"] < name_right:
            buckets[best_i]["name_words"].append(w)
        elif name_right is not None:
            buckets[best_i]["sector_words"].append(w)
        else:
            buckets[best_i]["name_words"].append(w)

    holdings = []
    for i, a in enumerate(anchors):
        company = _clean(_bucket_words_to_text(buckets[i]["name_words"]))
        sector = _clean(_bucket_words_to_text(buckets[i]["sector_words"]))
        pct = a["text"].rstrip("%")

        if not company:
            continue
        company = _normalize_cdmdf(company)
        if _is_stop(company):
            break
        # "<Category> Total <pct>%" subtotal rows -- skip, but keep scanning
        # since more holdings in a different category typically follow.
        # A bare category header with no % of its own (e.g. "Mutual Fund
        # Units") has no anchor to attach to, so nearest-anchor attachment
        # can sweep it onto a *different*, adjacent "<Category> Total" row
        # instead of the real holding that follows it, producing something
        # like "Government Bond Total Mutual Fund Units". Checking whether
        # the text up to the first "Total" is itself a category label --
        # not just whether the row ends with "Total" -- catches this
        # regardless of what noise trails after it.
        total_match = re.match(r"^(.+?)\s+Total\b", company, re.IGNORECASE)
        if total_match:
            label_key = total_match.group(1).lower().strip()
            if label_key in _TERMINAL_ALLOCATION_LABELS:
                holdings.append(
                    {
                        "company": _TERMINAL_ALLOCATION_LABELS[label_key],
                        "sector": "",
                        "pct_to_net_assets": pct,
                    }
                )
                continue
            if _is_category_label(total_match.group(1)):
                continue
        if re.search(r"\btotal$", company, re.IGNORECASE):
            continue
        company = _strip_category_prefix(company)
        if not company:
            continue

        # "Corporate Debt Market Development Fund" is a SEBI-mandated
        # regulatory holding that every debt-oriented scheme carries a
        # small amount of, and its category label is textually identical
        # to the (single) instrument it holds -- unlike every other
        # category in _CATEGORY_LABELS, this one's name can legitimately
        # BE a real holding's name. The bare category-header row (which
        # shows its own aggregate %, same convention as e.g. "Certificate
        # of Deposit 65.79%") is distinguished from the actual holding row
        # by a footnote-marker glyph glued directly onto the label with no
        # separating space (e.g. "...Fund£") -- the holding row itself
        # carries no such glyph, so stripping a trailing glyph and checking
        # whether anything was actually removed tells the two apart.
        bare = _strip_trailing_footnote_symbols(company)
        if bare.lower() == "corporate debt market development fund":
            if bare != company:
                continue  # footnote-marked -> bare category header, skip
            company = bare
        else:
            label_key = company.lower().strip()
            if label_key in _TERMINAL_ALLOCATION_LABELS:
                company = _TERMINAL_ALLOCATION_LABELS[label_key]
            elif _is_category_label(company):
                continue

        holdings.append(
            {"company": company, "sector": sector, "pct_to_net_assets": pct}
        )
    return holdings


def _bucket_words_to_text(words, y_tolerance=1.8):
    """Join a bucket's words into text in correct reading order.

    Word "top" coordinates carry tiny floating-point jitter (e.g. two
    words on the same visual line can come back as 292.4749 and 292.4686
    instead of an identical value), so sorting directly by (top, x0) as a
    tuple lets that sub-pixel jitter outrank x0 and can scramble same-line
    word order. Clustering into sub-lines first (tolerant of that jitter),
    ordering the sub-lines themselves by average top, and only then sorting
    each sub-line's own words by x0 keeps both a single visual line's words
    in left-to-right order and, for a name that wraps across two lines
    (e.g. "National Bank For Financing" / "Infrastructure And
    Development"), keeps the earlier physical line before the later one.
    """
    if not words:
        return ""
    rows = []
    for w in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(w["top"])
        placed = False
        for r in reversed(rows[-3:]):
            if abs(r["avg_top"] - top) <= y_tolerance:
                r["words"].append(w)
                r["avg_top"] = sum(float(x["top"]) for x in r["words"]) / len(
                    r["words"]
                )
                placed = True
                break
        if not placed:
            rows.append({"avg_top": top, "words": [w]})
    rows.sort(key=lambda r: r["avg_top"])
    parts = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: float(w["x0"]))
        parts.append(" ".join(w["text"] for w in ws))
    return " ".join(parts)


# "Corporate Debt Market Development Fund" is a SEBI-mandated regulatory
# holding that every debt-oriented scheme carries a small amount of. Unlike
# every other entry in _CATEGORY_LABELS, its category label is textually
# identical to the single instrument it holds, which shows up in the source
# PDF in two different ways depending on the scheme: sometimes the bare
# category-header row carries its own inline aggregate % (then the
# following, separately-anchored holding row repeats the same name), and
# sometimes the bare category-header row carries no % of its own at all, so
# it has no anchor to attach to and gets swept by nearest-anchor attachment
# straight into the holding row's own bucket -- producing the name doubled
# back to back. Both shapes are handled below.
_CDMDF_NAME = "Corporate Debt Market Development Fund"
_CDMDF_DUP_RE = re.compile(
    re.escape(_CDMDF_NAME) + r"\W*" + re.escape(_CDMDF_NAME), re.IGNORECASE
)


def _normalize_cdmdf(company: str) -> str:
    if _CDMDF_DUP_RE.search(company):
        return _CDMDF_NAME
    return company


def _dedupe_holdings(holdings):
    result = []
    seen = set()
    for h in holdings:
        company = _clean(h.get("company", ""))
        sector = _clean(h.get("sector", ""))
        pct = str(h.get("pct_to_net_assets", ""))
        if not company:
            continue
        key = (company.lower(), sector.lower(), pct)
        if key in seen:
            continue
        seen.add(key)
        result.append({"company": company, "sector": sector, "pct_to_net_assets": pct})
    return result


def extract_holdings(page):
    headers = _find_portfolio_headers(page)
    if not headers:
        return []
    bottom_bound = _compute_bottom_bound(page)
    page_width = float(page.width)

    holdings = []
    for i, header in enumerate(headers):
        next_x0 = headers[i + 1]["name_x0"] if i + 1 < len(headers) else None
        holdings.extend(
            _extract_holdings_for_group(page, header, next_x0, page_width, bottom_bound)
        )
    return _dedupe_holdings(holdings)


# ---------------------------------------------------------------------------
# Per-scheme aggregation
# ---------------------------------------------------------------------------


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

    for idx in page_idxs:
        page = pdf.pages[idx]
        full_text = _page_text(page)

        headers = _find_portfolio_headers(page)
        metadata_text = full_text
        if headers:
            boundary = min(h["name_x0"] for h in headers)
            metadata_words = [
                w
                for w in _page_words(page)
                if float(w["x1"]) <= boundary + 2 and float(w["top"]) >= 0
            ]
            metadata_text = "\n".join(
                line["text"] for line in _words_to_lines(metadata_words) if line["text"]
            )

        if benchmark is None:
            benchmark = extract_benchmark(metadata_text)
        if not isin:
            isin = extract_isin(metadata_text)
        for manager in extract_fund_managers(metadata_text):
            if manager not in managers:
                managers.append(manager)

        page_holdings = extract_holdings(page)
        for holding in page_holdings:
            if holding not in holdings:
                holdings.append(holding)

        if additional_benchmark is None:
            # The "##" marker that identifies the additional benchmark lives
            # in the Performance Table, which sits outside the FUND
            # FEATURES panel -- read it from the page directly rather than
            # metadata_text (see extract_additional_benchmark docstring).
            additional_benchmark = extract_additional_benchmark(page)

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
