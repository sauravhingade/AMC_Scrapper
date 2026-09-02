"""
Baroda BNP Paribas Mutual Fund extractor.
"""

import logging

logging.getLogger("pdfminer").setLevel(logging.ERROR)
import re
import statistics

# ---------------------------------------------------------------------------
# Font helpers
#
# Baroda's factsheet template embeds a single family ("BNPPSans") in three
# weights that are used with a very consistent structural meaning on every
# portfolio table on every scheme page:
#   "...+BNPPSans-Light" - body copy AND every individual holding's name /
#                           rating / value cells.
#   "...+BNPPSans"        - section headings, column headers, category /
#                           instrument-type subtotal rows (e.g. "Banks
#                           21.74%", "Certificate of Deposit 57.17%",
#                           "TOTAL EQUITY HOLDING 97.63%").
#   "...+BNPPSans-Bold"   - top-level panel headings ("SCHEME DETAILS",
#                           "PORTFOLIO"...) and hard table terminators
#                           ("GRAND TOTAL", "Total Fixed Income Holdings").
# The little "Top 10 holding" checkmark glyphs are a separate dingbat/
# symbol font entirely, never a text character, and are dropped outright.
# This font-weight signal -- not keyword text-matching -- is what lets a
# bare rollup row ("Banks 21.74%") be told apart from an individual holding
# ("HDFC Bank Limited 6.15%") even though both are, textually, just a
# "<name> <pct>" pair in the same table column.
# ---------------------------------------------------------------------------

_DECORATIVE_FONT_RE = re.compile(r"ZapfDingbats|Wingdings|Symbol", re.IGNORECASE)


def _font_suffix(fontname):
    if not fontname:
        return ""
    return fontname.split("+")[-1]


def _is_decorative_font(fontname):
    return bool(_DECORATIVE_FONT_RE.search(fontname or ""))


def _is_light_font(fontname):
    return _font_suffix(fontname).endswith("-Light")


def _is_bold_font(fontname):
    return _font_suffix(fontname).endswith("-Bold")


# ---------------------------------------------------------------------------
# Text cleanup
# ---------------------------------------------------------------------------


def _clean(text):
    if not text:
        return ""
    text = text.replace("\u00a0", " ").replace("\u00ad", "").replace("\ufeff", "")
    text = re.sub(r"[•●▪◦]", " ", text)
    text = re.sub(r"[\ue000-\uf8ff]", " ", text)
    text = re.sub(r"\(cid:\d+\)", "", text)
    return re.sub(r"\s+", " ", text).strip(" \t\r\n:-")


# Trailing footnote-marker glyphs (†, ¥, ¶, µ, §, ^, ^^, $, $$, #, ##, **,
# etc.) used throughout this factsheet to tag a scheme/manager name with a
# footnote are stripped before treating the remainder as the real name.
_TRAILING_FOOTNOTE_RE = re.compile(r"[^A-Za-z0-9\s():&,/'\-]+$")


def _strip_trailing_footnote_symbols(text):
    prev = None
    text = text.strip()
    while prev != text:
        prev = text
        text = _TRAILING_FOOTNOTE_RE.sub("", text).strip()
    return text


# ---------------------------------------------------------------------------
# Low level page/word helpers
# ---------------------------------------------------------------------------


def _page_words(page):
    try:
        words = page.extract_words(
            extra_attrs=["fontname"],
            # A tight x_tolerance matters here: on at least one scheme's
            # page (a newly-added scheme whose embedded font subset renders
            # noticeably tighter than the rest of the document) two
            # genuinely separate words -- e.g. a sector label's "Ferrous"
            # and "Metals" -- can sit as little as ~1.8pt apart with no
            # actual space glyph between them at all, which a looser
            # tolerance would wrongly fuse into one "FerrousMetals" token.
            # True intra-word kerning gaps throughout this document (even
            # for a deliberately-styled compound name like "PhysicsWallah")
            # measure close to 0pt, so 1.2 sits safely between the two.
            x_tolerance=1.2,
            y_tolerance=1.5,
            keep_blank_chars=False,
        )
    except TypeError:
        words = page.extract_words(extra_attrs=["fontname"]) or []
    out = []
    for w in words or []:
        if _is_decorative_font(w.get("fontname", "")):
            continue
        out.append(w)
    return out


def _cluster_rows(words, y_tolerance=1.6):
    """Group words sharing (approximately) the same vertical position into
    rows, tolerant of the small sub-pixel jitter pdfplumber sometimes
    reports for glyphs that are visually on the same baseline."""
    rows = []
    for w in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(w["top"])
        placed = False
        for r in reversed(rows[-4:]):
            if abs(r["top"] - top) <= y_tolerance:
                r["words"].append(w)
                placed = True
                break
        if not placed:
            rows.append({"top": top, "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: float(w["x0"]))
    return rows


def _words_to_lines(words, y_tolerance=1.6):
    rows = _cluster_rows(words, y_tolerance=y_tolerance)
    out = []
    for r in rows:
        ws = r["words"]
        out.append(
            {
                "top": r["top"],
                "x0": min(float(w["x0"]) for w in ws),
                "x1": max(float(w["x1"]) for w in ws),
                "text": _clean(" ".join(w["text"] for w in ws)),
                "words": ws,
            }
        )
    return out


def _page_text(page):
    return "\n".join(
        line["text"] for line in _words_to_lines(_page_words(page)) if line["text"]
    )


def _bucket_words_to_text(words, y_tolerance=1.8):
    """Join a bucket's words into text in correct reading order, handling a
    name that wraps across more than one physical line."""
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


# ---------------------------------------------------------------------------
# Scheme heading / page segmentation
#
# Every per-scheme page in this factsheet opens with a boxed heading in the
# page's top-left corner: "Baroda BNP Paribas" on one line, the scheme's own
# name below it, then a parenthetical scheme-type description, e.g.
#   Baroda BNP Paribas
#   Large Cap Fund
#   (An Open ended Equity Scheme predominantly investing in large cap
#   stocks)
# This heading box is reprinted, identically, at the top of every page that
# belongs to that scheme -- including a second/continuation page for a
# scheme whose portfolio table spills over (e.g. Balanced Advantage Fund) --
# which makes it a reliable, generic way to both name and segment scheme
# pages without hardcoding any individual scheme's name. The box is always
# comfortably left of x=210 on the page, well clear of the "This product is
# suitable..." suitability blurb and Riskometer widgets that start further
# right on the very same lines.
# ---------------------------------------------------------------------------

_HEADING_X_LIMIT = 212
_HEADING_TOP_LIMIT = 150
_HEADING_NAME_RE = re.compile(r"^(Baroda\s+BNP\s+Paribas\s+.+?)\s*\(", re.IGNORECASE)

# A per-scheme page always carries at least one of the panel headings that
# make up the standard scheme layout (investment objective narrative,
# scheme metadata panel, or the portfolio table itself). This is a
# defensive secondary check only -- the heading-box match above is already
# scheme-specific and reliable on its own -- guarding against ever
# attaching a stray non-scheme page (cover, index, glossary, ...) to a
# scheme should its layout ever coincidentally resemble the heading box.
_BODY_MARKERS_RE = re.compile(
    r"INVESTMENT OBJECTIVE|SCHEME DETAILS|PORTFOLIO|NAME OF INSTRUMENT|"
    r"%\s*of\s*Net\s*Assets",
    re.IGNORECASE,
)


def _extract_scheme_name(page):
    words = [
        w
        for w in _page_words(page)
        if float(w["top"]) <= _HEADING_TOP_LIMIT and float(w["x0"]) < _HEADING_X_LIMIT
    ]
    if not words:
        return None
    lines = _words_to_lines(words, y_tolerance=2.0)
    text = _clean(
        " ".join(line["text"] for line in sorted(lines, key=lambda l: l["top"]))
    )
    m = _HEADING_NAME_RE.match(text)
    if not m:
        return None
    name = _clean(m.group(1))
    return name or None


def _has_body_markers(page):
    return bool(_BODY_MARKERS_RE.search(_page_text(page)))


def segment_schemes(pdf):
    """Returns {scheme_name: [page_index, ...]} in document order.

    Unlike a factsheet where only the *first* page of a scheme carries its
    name and later pages must be attached by "current scheme so far"
    carry-over, every page belonging to a Baroda scheme -- including a
    second/continuation page -- reprints the full heading box itself (see
    _extract_scheme_name). So each page is judged independently, on its own
    heading + body markers, with no carried-over state: nothing downstream
    of the last real scheme page (SIP appendix, performance tables, fund
    manager index, distribution history, glossary, ...) can ever be
    mis-attached to whichever scheme happened to be printed last, since
    none of those pages carry this scheme's own heading box.
    """
    scheme_pages = {}
    for i, page in enumerate(pdf.pages):
        name = _extract_scheme_name(page)
        if not name:
            continue
        if not _has_body_markers(page):
            continue
        scheme_pages.setdefault(name, [])
        if i not in scheme_pages[name]:
            scheme_pages[name].append(i)
    return scheme_pages


# ---------------------------------------------------------------------------
# Scheme metadata (benchmark / ISIN / fund managers)
#
# All of these live in the narrow "SCHEME DETAILS" metadata panel that runs
# down the left ~210pt of every scheme page, as a stack of bold field
# labels ("Benchmark Index (AMFI Tier 1)", "Fund Manager", ...) each
# immediately followed by its value. Generic "next bold heading in this
# same column" boundary detection -- rather than hardcoding what the next
# label's text happens to be -- keeps this working regardless of which
# fields a particular scheme type does or doesn't carry.
# ---------------------------------------------------------------------------

_METADATA_X_LIMIT = 215


def _metadata_column_words(page):
    return [w for w in _page_words(page) if float(w["x0"]) < _METADATA_X_LIMIT]


def _metadata_bold_headings(page):
    words = [
        w for w in _metadata_column_words(page) if _is_bold_font(w.get("fontname", ""))
    ]
    return sorted(_words_to_lines(words, y_tolerance=2.0), key=lambda l: l["top"])


def _section_text(page, heading_regex):
    """Text of the metadata-panel field whose bold label matches
    ``heading_regex``, i.e. everything between that label and the next bold
    label below it in the same column."""
    headings = _metadata_bold_headings(page)
    start_top = end_top = None
    for i, h in enumerate(headings):
        if heading_regex.search(h["text"]):
            start_top = h["top"]
            end_top = headings[i + 1]["top"] if i + 1 < len(headings) else None
            break
    if start_top is None:
        return ""
    words = [
        w
        for w in _metadata_column_words(page)
        if float(w["top"]) > start_top + 2
        and (end_top is None or float(w["top"]) < end_top - 1)
    ]
    lines = _words_to_lines(words, y_tolerance=1.8)
    return "\n".join(
        line["text"] for line in sorted(lines, key=lambda l: l["top"]) if line["text"]
    )


def extract_benchmark(page):
    text = _clean(_section_text(page, re.compile(r"Benchmark\s+Index", re.IGNORECASE)))
    return text or None


def extract_isin(page):
    text = "\n".join(
        line["text"] for line in _words_to_lines(_metadata_column_words(page))
    )
    m = re.search(r"\bISIN\s*:?\s*([A-Z0-9]{6,20})\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""


_SLEEVE_RE = re.compile(
    r"\b(Equity|Debt|Fixed\s*Income|Commodity|Commodities)(?=\s*(?:Mr\.|Ms\.|Mrs\.|Dr\.))",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s+([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,4})",
)


def _normalize_sleeve(label):
    low = label.lower()
    if "debt" in low or "fixed" in low:
        return "Debt"
    if "commodit" in low:
        return "Commodity"
    return "Equity"


def extract_fund_managers(page):
    """Baroda labels a manager's sleeve/category *before* their name, once
    per row, for multi-manager hybrid schemes -- "Equity Mr. X ... Fixed
    Income Mr. Y" -- rather than Bandhan's single "<Sleeve> Portion:" label
    covering a whole block. Single-sleeve schemes simply list one or more
    "Mr./Ms. <Name>" rows with no sleeve label at all.

    The "Fund Manager" column in this table is narrow enough that a longer
    surname regularly wraps onto its own line below the rest of that row
    (date/experience included), e.g.:
        Equity Mr. Ankeet 01-Jan-25 11 years
        Pandya
    so a lone alphabetic word on its own line, with no digits and no
    "Mr./Ms." of its own, is treated as a continuation of the name on the
    row above it rather than a row of its own.
    """
    section = _section_text(page, re.compile(r"^Fund\s*Manager", re.IGNORECASE))
    if not section:
        return []

    managers = []
    current_sleeve = None
    for raw_line in section.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        sleeve_m = _SLEEVE_RE.search(line)
        if sleeve_m:
            current_sleeve = _normalize_sleeve(sleeve_m.group(1))

        name_m = _NAME_RE.search(line)
        if name_m:
            name = _clean(name_m.group(1))
            if name:
                entry = {"role": "Fund Manager", "name": name, "sleeve": current_sleeve}
                managers.append(entry)
            continue

        orphan_m = re.match(
            r"^([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,3})[^A-Za-z]*$", line
        )
        if managers and orphan_m:
            # Guard against a section heading from elsewhere on the page
            # (all-uppercase, e.g. a stray "GOLD"/"GRAND" fragment) being
            # mistaken for a continuation: only genuine, Title-Case
            # continuation words are kept, and only up to the point (if
            # any) where an all-uppercase word starts.
            continuation_words = []
            for word in orphan_m.group(1).split():
                if word == word.upper():
                    break
                continuation_words.append(word)
            if continuation_words:
                managers[-1]["name"] = _clean(
                    f"{managers[-1]['name']} {' '.join(continuation_words)}"
                )

    deduped = []
    for m in managers:
        if m not in deduped:
            deduped.append(m)
    return deduped


# ---------------------------------------------------------------------------
# Additional benchmark
#
# Unlike the Tier-1 "Benchmark Index" field, Baroda's scheme pages carry no
# labelled secondary-benchmark field at all. It is only identifiable, in
# this factsheet, from the back-of-book "Performance of Schemes" appendix,
# where every scheme's own returns table explicitly has a row literally
# labelled "Additional Benchmark <Index Name>" directly beneath its Tier-1
# benchmark row. That appendix repeats its own section title as the first
# line of every page it spans, which is what is used to scope the scan to
# just those pages generically (no page-number hardcoding).
# ---------------------------------------------------------------------------

_PERFORMANCE_SECTION_TITLE_RE = re.compile(
    r"^Performance\s+of\s+Schemes\b", re.IGNORECASE
)
_SCHEME_ROW_RE = re.compile(r"^\d+\s+(Baroda\s+BNP\s+Paribas\s+.+)$", re.IGNORECASE)
_ADDITIONAL_BENCHMARK_RE = re.compile(
    r"Additional\s+Benchmark\s+(.+?)(?=\s+-?\d+\.\d{2}\b|$)", re.IGNORECASE
)


def _normalize_key(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _build_additional_benchmark_map(pdf):
    mapping = {}
    current_name = None
    for page in pdf.pages:
        text = _page_text(page)
        first_line = text.split("\n")[0] if text else ""
        if not _PERFORMANCE_SECTION_TITLE_RE.match(first_line):
            continue

        bold_lines = _words_to_lines(
            [w for w in _page_words(page) if _is_bold_font(w.get("fontname", ""))],
            y_tolerance=2.0,
        )
        all_lines = _words_to_lines(_page_words(page), y_tolerance=1.8)

        events = []
        for line in bold_lines:
            m = _SCHEME_ROW_RE.match(line["text"])
            if m:
                raw_name = m.group(1)
                # A couple of scheme rows in this appendix carry an inline
                # parenthetical annotation right in the same bold heading
                # line, e.g. "Baroda BNP Paribas Credit Risk Fund$$ (scheme
                # has two segregated portfolios)" -- unlike every other
                # row, which is just the plain scheme name. Cutting at the
                # first "(" here too keeps this in step with how the
                # scheme's own page heading is read (see
                # _extract_scheme_name), so the two agree on the same name
                # and this scheme's additional-benchmark lookup doesn't
                # silently miss.
                raw_name = raw_name.split("(", 1)[0]
                name = _strip_trailing_footnote_symbols(_clean(raw_name))
                events.append((line["top"], "name", name))
        for line in all_lines:
            m = _ADDITIONAL_BENCHMARK_RE.search(line["text"])
            if m:
                events.append((line["top"], "bench", _clean(m.group(1))))
        events.sort(key=lambda e: e[0])

        for _, kind, value in events:
            if kind == "name":
                current_name = value
            elif kind == "bench" and current_name:
                key = _normalize_key(current_name)
                if key and key not in mapping:
                    mapping[key] = value
    return mapping


_ADDITIONAL_BENCHMARK_CACHE = {}


def _additional_benchmark_for(pdf, scheme_name):
    cache_key = id(pdf)
    if cache_key not in _ADDITIONAL_BENCHMARK_CACHE:
        _ADDITIONAL_BENCHMARK_CACHE[cache_key] = _build_additional_benchmark_map(pdf)
    mapping = _ADDITIONAL_BENCHMARK_CACHE[cache_key]
    return mapping.get(_normalize_key(scheme_name))


# ---------------------------------------------------------------------------
# Portfolio / holdings table detection
# ---------------------------------------------------------------------------

_PCT_TOKEN_RE = re.compile(r"^-?\d+(?:\.\d+)?%$")

# Terminal/aggregate rows -- subtotals and grand totals -- are already
# excluded from ever becoming a *holding* by the font-weight rule (see the
# module docstring above), but a couple of them (e.g. "TOTAL EQUITY
# HOLDING", "Total Fixed Income Holdings", "PREFSHARE HOLDING", "ETF
# TOTAL") also sit in the exact same name-column position a genuine sector
# header would, right before whatever holding happens to be listed next.
# Left unfiltered they would occasionally get read as if they were that
# holding's sector, or get carried across into the next column as a
# still-unclaimed category -- neither of which they ever legitimately are.
_TERMINAL_CATEGORY_ROW_RE = re.compile(r"\bTOTAL\b|^PREFSHARE HOLDING$", re.IGNORECASE)

# A real company/instrument name in this factsheet almost always ends in
# one of these words. Used only to arbitrate which neighbouring holding an
# orphaned, anchor-less wrapped name-line belongs to (see
# _extract_holdings_for_group) -- not to validate or reject a name outright.
_NAME_TERMINAL_RE = re.compile(
    r"(Limited|Ltd\.?|Fund|Trust|Company|Corporation|Corp\.?|Bank|Plc|Inc\.?|"
    r"Co\.?|LLC|LLP|AMC|REIT|InvIT|Bees|Cements?)\)?$",
    re.IGNORECASE,
)

# SEBI's NSE Industry Classification Structure is the standard,
# regulator-mandated sector taxonomy every AMC (Baroda included) uses on
# every monthly factsheet -- it is not scheme-specific or month-specific
# data, just the fixed reference vocabulary this template classifies
# holdings against. A short list of the longer names from it -- the ones
# actually prone to wrapping onto a second physical line in this column
# width -- is used, alongside row-position geometry, to settle the one
# genuinely ambiguous case that geometry alone cannot: a first line that
# stops with plenty of room to spare can still be a real, forced wrap if
# what it says, joined with the row below it, completes one of these
# well-known names (e.g. "Agricultural, Commercial" / "Construction
# Vehicles"), which two unrelated, back-to-back one-line labels never do.
_KNOWN_WRAPPING_SECTOR_NAMES = {
    "agricultural, commercial & construction vehicles",
    "agricultural commercial & construction vehicles",
    "agricultural food & other products",
    "non - ferrous metals",
    "diversified fmcg",
    "cement & cement products",
    "metals & minerals trading",
    "commercial services & supplies",
    "pharmaceuticals & biotechnology",
}

# The mirror-image false positive: a *complete*, single-line category name
# that simply happens to run close enough to the column's right edge to
# also satisfy the "squeezed against the boundary" geometry test above,
# even though nothing about it is actually cut off (e.g. "Financial
# Technology (Fintech)" on its own is already a whole, standard NSE
# category, not the start of some longer, wrapping one). Recognising these
# outright keeps the row after them from being wrongly absorbed as if it
# were their continuation.
_KNOWN_COMPLETE_SECTOR_NAMES = {
    "financial technology (fintech)",
    "healthcare services",
    "chemicals & petrochemicals",
    "fertilizers & agrochemicals",
    "transport infrastructure",
    "agricultural, commercial & construction vehicles",
    "agricultural commercial & construction vehicles",
    "pharmaceuticals & biotechnology",
}


def _normalize_for_lookup(text):
    return re.sub(r"\s+", " ", text.strip().lower())


def _joined_matches_known_sector(first_text, second_text):
    joined = _normalize_for_lookup(f"{first_text} {second_text}")
    return joined in _KNOWN_WRAPPING_SECTOR_NAMES


def _is_known_complete_sector(text):
    return _normalize_for_lookup(text) in _KNOWN_COMPLETE_SECTOR_NAMES


# Case-sensitive (not case-insensitive): these chart/summary headings are
# always printed in full uppercase in Baroda's template, while a couple of
# them (e.g. "Market Capitalization as per SEBI - Large Cap: ...") also
# appear as an ordinary title-case explanatory footnote elsewhere on the
# very same page. Matching the exact uppercase heading only -- never the
# lowercase-continuation prose -- keeps that footnote from being mistaken
# for the genuine section title and prematurely truncating a column.
_STOP_MARKERS_RE = re.compile(
    r"^(GRAND TOTAL|MARKET CAPITALIZATION|SECTORAL COMPOSITION|"
    r"EQUITY SECTORAL COMPOSITION|COMPOSITION BY ASSETS|CREDIT QUALITY PROFILE|"
    r"EXPOSURE TO TOP SEVEN GROUPS|TRACKING DIFFERENCE DATA|"
    r"SCHEME WISE POTENTIAL RISK CLASS|ALLOCATION ACROSS MAJOR CONGLOMERATES)"
)


def _find_portfolio_headers(page):
    """Returns a list of {top, name_x0, rating_x0, nav_x0, deriv_x0} for
    every portfolio-table column group on the page.

    Every such table -- "EQUITY HOLDINGS", "FIXED INCOME HOLDINGS",
    "InvITs Holdings", "Gold ETF", "NAME OF INSTRUMENT" (fund-of-funds) --
    ends its header row with a bold "% of Net Assets" (occasionally "% of
    Net Derivatives\\nAssets" for arbitrage/hedge overlay schemes). That
    "%" token's x-position anchors the column; the table's own name/type
    label -- whatever its text -- is simply the nearest contiguous run of
    bold words immediately to its left, which makes this fully generic
    across every table-name variant above without hardcoding any of them
    individually. An optional "Rating" bold sub-header, if present between
    the name and the pct column, marks a debt-style table.
    """
    words = _page_words(page)
    bold_words = [w for w in words if _is_bold_font(w.get("fontname", ""))]
    rows = _cluster_rows(bold_words, y_tolerance=2.2)

    # The three words of "% of Net" are not always reported at exactly the
    # same `top` by pdfplumber -- depending on the table, "Net"/"Assets"/
    # "Rating" can sit a few pt below "%"/"of" even though they render on
    # what is visually the same header line -- so only "%" immediately
    # followed by "of" (tight, same row) is required to anchor a column;
    # that pairing alone is unique enough to this table vocabulary.
    pct_hits = []
    for r in rows:
        ws = r["words"]
        for i in range(len(ws) - 1):
            if ws[i]["text"] == "%" and ws[i + 1]["text"] == "of":
                pct_hits.append((r["top"], float(ws[i]["x0"])))

    # These bold words are the fixed structural vocabulary of a portfolio
    # header row itself ("% of Net [Derivatives] Assets", "Rating") and
    # must never be mistaken for part of a table's own name/type label
    # (e.g. accidentally bridging across a ~20pt gap from one column's
    # trailing "...Net" into an adjacent column's "EQUITY..." label).
    _HEADER_MARKER_WORDS = {"%", "of", "Net", "Assets", "Rating", "Derivatives"}

    headers = []
    for top, nav_x0 in pct_hits:
        zone = [w for w in bold_words if (top - 10) <= float(w["top"]) <= (top + 20)]

        left_candidates = sorted(
            (
                w
                for w in zone
                if float(w["x0"]) < nav_x0 - 4 and w["text"] not in _HEADER_MARKER_WORDS
            ),
            key=lambda w: -float(w["x0"]),
        )
        name_x0 = None
        prev_x0 = None
        label_words = []
        for w in left_candidates:
            x0, x1 = float(w["x0"]), float(w["x1"])
            if prev_x0 is None or (prev_x0 - x1) < 22:
                name_x0 = x0 if name_x0 is None else min(name_x0, x0)
                prev_x0 = x0
                label_words.append(w)
            else:
                break
        if name_x0 is None:
            continue
        # The table's own name/type label can sit a handful of pt below the
        # "%"/"of" pair it was located from (e.g. "InvITs Holdings" on its
        # own line, with "% of Net Assets" wrapping in underneath it) --
        # its true lower edge, not just the pct pair's row, is what a
        # holdings scan must start strictly after, or the label text itself
        # gets swept in and misread as if it were a genuine category row.
        label_bottom = max(float(w["top"]) for w in label_words)

        rating_x0 = None
        deriv_x0 = None
        for w in zone:
            x0 = float(w["x0"])
            if w["text"] == "Rating" and name_x0 < x0 < nav_x0:
                rating_x0 = x0
            if w["text"] == "Derivatives" and x0 > nav_x0:
                deriv_x0 = x0

        headers.append(
            {
                "top": top,
                "name_x0": name_x0,
                "rating_x0": rating_x0,
                "nav_x0": nav_x0,
                "deriv_x0": deriv_x0,
                "label_bottom": label_bottom,
            }
        )

    unique = []
    for h in headers:
        if not any(
            abs(h["name_x0"] - u["name_x0"]) < 3 and abs(h["top"] - u["top"]) < 4
            for u in unique
        ):
            unique.append(h)
    return sorted(unique, key=lambda h: (h["top"], h["name_x0"]))


def _page_bold_stop_lines(page):
    return _words_to_lines(
        [w for w in _page_words(page) if _is_bold_font(w.get("fontname", ""))],
        y_tolerance=2.0,
    )


def _stop_bound_for_range(bold_lines, page_height, x_left, x_right):
    """The nearest "GRAND TOTAL" / summary-chart heading positioned inside
    this specific column's own horizontal span.

    A single whole-page bound doesn't work here: on a page with a short
    "EQUITY HOLDINGS" column sitting next to a much longer one (e.g. this
    scheme's second column running out of stocks quickly and dropping into
    "FIXED INCOME HOLDINGS" + "GRAND TOTAL" while the first column still has
    another two dozen holdings still to come, lower down the very same
    page), the page's overall "GRAND TOTAL" can sit at a smaller `top` than
    unrelated, perfectly valid holdings still further down a *different*
    column. Restricting the search to headings whose own x-range actually
    overlaps this column's [x_left, x_right] keeps each column bounded only
    by markers that could plausibly belong to it.
    """
    bound = page_height
    for line in bold_lines:
        if line["x1"] < x_left or line["x0"] > x_right:
            continue
        if _STOP_MARKERS_RE.match(line["text"]):
            bound = min(bound, line["top"])
    return bound


def _augment_headers(headers, page_width):
    for h in headers:
        siblings = [
            o
            for o in headers
            if o is not h
            and abs(o["top"] - h["top"]) <= 20
            and o["name_x0"] > h["name_x0"] + 5
        ]
        h["right_edge"] = min(
            [o["name_x0"] - 8 for o in siblings],
            default=min(page_width - 12, h["nav_x0"] + 26),
        )

        below = [
            o
            for o in headers
            if o is not h
            and abs(o["name_x0"] - h["name_x0"]) <= 25
            and o["top"] > h["top"] + 8
        ]
        h["column_next_top"] = min([o["top"] for o in below], default=None)
    return headers


def _extract_holdings_for_group(
    page, header, bold_lines, page_height, carry_in_sector=""
):
    words = _page_words(page)

    name_left = header["name_x0"] - 3
    right_edge = header["right_edge"]
    rating_x0 = header["rating_x0"]
    nav_x0 = header["nav_x0"]
    deriv_x0 = header["deriv_x0"]

    name_end = (rating_x0 - 4) if rating_x0 else (nav_x0 - 6)
    nav_left = nav_x0 - 14
    nav_right = (deriv_x0 - 3) if deriv_x0 else right_edge

    column_bound = _stop_bound_for_range(bold_lines, page_height, name_left, right_edge)
    stop_top = header["column_next_top"] if header["column_next_top"] else column_bound
    stop_top = min(stop_top, column_bound)
    start_top = max(header["top"], header.get("label_bottom", header["top"])) + 4

    col_words = [
        w
        for w in words
        if right_edge >= float(w["x0"]) >= name_left
        and start_top <= float(w["top"]) < stop_top
    ]
    if not col_words:
        return [], carry_in_sector

    light_words = [w for w in col_words if _is_light_font(w.get("fontname", ""))]

    anchor_pool = [w for w in light_words if nav_left <= float(w["x0"]) < nav_right]
    anchors = sorted(
        (w for w in anchor_pool if _PCT_TOKEN_RE.match(w["text"])),
        key=lambda w: float(w["top"]),
    )
    if not anchors:
        return [], carry_in_sector

    anchor_tops = [float(a["top"]) for a in anchors]
    gaps = [
        anchor_tops[i + 1] - anchor_tops[i]
        for i in range(len(anchor_tops) - 1)
        if anchor_tops[i + 1] - anchor_tops[i] > 0
    ]
    typical_gap = statistics.median(gaps) if gaps else 10.0
    max_attach = max(typical_gap * 1.6, 11.0)

    buckets = [{"name": [], "rating": []} for _ in anchors]
    anchor_ids = {id(a) for a in anchors}

    # Rating-column text (debt-style tables only) keeps the simpler
    # nearest-anchor-by-distance rule -- a rating rarely wraps, and when it
    # does it always wraps *upward* of its own value in this template, so
    # plain proximity is reliable here.
    for w in light_words:
        if id(w) in anchor_ids or rating_x0 is None:
            continue
        x0 = float(w["x0"])
        if not (name_end <= x0 < nav_left):
            continue
        top = float(w["top"])
        best_i, best_d = None, None
        for i, at in enumerate(anchor_tops):
            d = abs(top - at)
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        if best_d is not None and best_d <= max_attach:
            buckets[best_i]["rating"].append(w)

    # A holding's name can wrap across more than one physical line, and --
    # depending on exactly where the line break happens to fall -- the
    # anchor (its own "% of Net Assets" value) can end up sitting on
    # *either* the wrap's first line (e.g. "One 97 Communications" carries
    # its own "1.42%", with a lone "Limited" wrapping onto the row below,
    # unclaimed) or its last line (e.g. "Cholamandalam Investment and"
    # carries no value of its own, wrapping down onto "Finance Company Ltd
    # 1.78%"). Neither "nearest row above" nor "nearest row below" is
    # right every time, so an orphan (anchor-less) row is instead resolved
    # against whichever neighbour it's actually a fragment of: if the
    # anchor immediately above it already reads like a complete company
    # name (ends in "Limited", "Ltd", "Fund", "Trust", ...), the orphan
    # can't be finishing that name off, so it must belong to the holding
    # below instead; otherwise it's completing the name above.
    name_words = [
        w for w in light_words if id(w) not in anchor_ids and float(w["x0"]) < name_end
    ]
    name_rows = sorted(
        _cluster_rows(name_words, y_tolerance=1.8), key=lambda r: r["top"]
    )

    def row_top_of(word_list):
        return sum(float(w["top"]) for w in word_list) / len(word_list)

    row_anchor_idx = {}
    for ridx, row in enumerate(name_rows):
        for aidx, at in enumerate(anchor_tops):
            if abs(at - row["top"]) <= 3.0:
                row_anchor_idx[ridx] = aidx
                break

    last_anchor_idx = None
    pending_words = []
    for ridx, row in enumerate(name_rows):
        if ridx in row_anchor_idx:
            aidx = row_anchor_idx[ridx]
            if pending_words:
                above_text = (
                    _bucket_words_to_text(buckets[last_anchor_idx]["name"])
                    if last_anchor_idx is not None
                    else ""
                )
                prefers_above = (
                    last_anchor_idx is not None
                    and not _NAME_TERMINAL_RE.search(above_text)
                )
                target = last_anchor_idx if prefers_above else aidx
                pending_top = row_top_of(pending_words)
                # The terminal-suffix check picks *which side* an orphan
                # line belongs to, but it has no notion of distance: a
                # bond/security name ending in a maturity date rather than
                # a company suffix (e.g. "...GOI (MD 20/06/2027)") reads as
                # "not yet complete" just as readily as a genuinely
                # unfinished one does, which can point it at a neighbour
                # that is implausibly far away. Rather than silently
                # dropping the words when that happens, fall back to
                # whichever candidate is actually close enough to be
                # plausible.
                if abs(pending_top - anchor_tops[target]) > max_attach:
                    other = aidx if target == last_anchor_idx else last_anchor_idx
                    if (
                        other is not None
                        and abs(pending_top - anchor_tops[other]) <= max_attach
                    ):
                        target = other
                if (
                    target is not None
                    and abs(pending_top - anchor_tops[target]) <= max_attach
                ):
                    buckets[target]["name"].extend(pending_words)
                pending_words = []
            buckets[aidx]["name"].extend(row["words"])
            last_anchor_idx = aidx
        else:
            pending_words.extend(row["words"])
    if pending_words and last_anchor_idx is not None:
        if abs(row_top_of(pending_words) - anchor_tops[last_anchor_idx]) <= max_attach:
            buckets[last_anchor_idx]["name"].extend(pending_words)

    # Category / instrument-type rollup rows (e.g. "Banks 21.74%",
    # "Certificate of Deposit 57.17%") never have a light-font anchor of
    # their own -- their whole row, name and percentage alike, is in the
    # semi-bold "BNPPSans" weight -- so they never enter the buckets above.
    # For an equity-style table (no Rating sub-column) they are the only
    # available sector classification for the holdings listed beneath them,
    # so build a running "last seen category" timeline from exactly the
    # non-light words that fall in the name sub-range.
    sector_timeline = []
    if rating_x0 is None:
        category_words = [
            w
            for w in col_words
            if not _is_light_font(w.get("fontname", "")) and float(w["x0"]) < name_end
        ]
        raw_rows = []
        for row in _cluster_rows(category_words, y_tolerance=1.8):
            # A y-only row cluster can occasionally line up two words that
            # aren't really part of the same label at all -- e.g. a
            # "Derivatives" sub-column's own category header sharing almost
            # the exact same vertical position as this column's, purely by
            # layout coincidence. A large horizontal gap between
            # consecutive words in an otherwise-normal, tightly-kerned
            # label is the tell: real multi-word category names never have
            # one, so the row is split back apart at it rather than joined
            # into one garbled label.
            ws = sorted(row["words"], key=lambda w: float(w["x0"]))
            sub_rows = [[ws[0]]]
            for w in ws[1:]:
                if float(w["x0"]) - float(sub_rows[-1][-1]["x1"]) > 20:
                    sub_rows.append([w])
                else:
                    sub_rows[-1].append(w)
            for sub in sub_rows:
                text = _clean(" ".join(w["text"] for w in sub))
                text = re.sub(r"-?\d+(?:\.\d+)?%$", "", text).strip()
                row_x1 = max(float(w["x1"]) for w in sub)
                if text and not _TERMINAL_CATEGORY_ROW_RE.search(text):
                    raw_rows.append((row["top"], text, row_x1))
        raw_rows.sort(key=lambda t: t[0])

        # A category/sector label that is itself too long to fit on one
        # line wraps onto a second physical line right above the first
        # holding it covers (e.g. "Agricultural, Commercial & Construction"
        # / "Vehicles"), at the same line pitch as any other row -- so it
        # cannot be told apart from two genuinely distinct, back-to-back
        # category rows by row spacing alone. Two signals settle it: the
        # *first* row being squeezed hard against the column's right edge
        # (a forced wrap, its text running almost all the way to where the
        # value column starts) is the general-purpose one that keeps
        # working for any sector name Baroda's template might introduce in
        # a future month; joining the two rows' text into a name this
        # template is already known to wrap catches the rarer case where a
        # wrap happens to break well short of that edge instead.
        merged = []
        pending_continuation = False
        for top, text, row_x1 in raw_rows:
            if pending_continuation and merged:
                prev_top, prev_text = merged[-1]
                merged[-1] = (prev_top, f"{prev_text} {text}")
            else:
                if merged and _joined_matches_known_sector(merged[-1][1], text):
                    prev_top, prev_text = merged[-1]
                    merged[-1] = (prev_top, f"{prev_text} {text}")
                    pending_continuation = False
                    continue
                merged.append((top, text))
            pending_continuation = (
                name_end - row_x1
            ) < 15 and not _is_known_complete_sector(text)
        sector_timeline = merged

    def sector_for(top):
        current = carry_in_sector
        for t, label in sector_timeline:
            if t <= top + 1:
                current = label
            else:
                break
        return current

    holdings = []
    kept_anchor_tops = []
    for i, a in enumerate(anchors):
        company = _clean(_bucket_words_to_text(buckets[i]["name"]))
        if not company:
            # A stray light-font percentage elsewhere on the page (e.g. a
            # footnote like "...includes equity less than 0.75% of
            # corpus") can occasionally land inside a column's value zone
            # and be picked up as an anchor, but it will never have any
            # real name-zone text near it, so it produces no holding and
            # -- importantly -- must not count when deciding below whether
            # this column's trailing category label was ever claimed by a
            # genuine holding.
            continue
        pct = a["text"].rstrip("%")
        if rating_x0 is not None:
            sector = _clean(_bucket_words_to_text(buckets[i]["rating"]))
        else:
            sector = sector_for(float(a["top"]))
        holdings.append(
            {"company": company, "sector": sector, "pct_to_net_assets": pct}
        )
        kept_anchor_tops.append(float(a["top"]))

    carry_out = carry_in_sector
    if rating_x0 is None and sector_timeline:
        last_top, last_label = sector_timeline[-1]
        last_kept_top = kept_anchor_tops[-1] if kept_anchor_tops else -1
        carry_out = last_label if last_top > last_kept_top else ""

    return holdings, carry_out


_GLUED_RATING_RE = re.compile(
    r"^(?P<company>.+?(?:\)|Limited|Ltd\.?))"
    r"(?P<agency>Sovereign|SOV|CRISIL|ICRA|CARE|FITCH)(?P<grade>\s?[A-Z0-9+\-()]*)$"
)


def _split_glued_rating(company, sector):
    """A source-side kerning artifact -- not an extraction error -- can
    leave a bond/security's rating fused directly onto the end of its name
    with no space at all, e.g. a closing maturity-date bracket
    "...14/12/2026)" or the word "Limited" immediately followed by
    "CRISIL". Depending on exactly where that fused boundary falls, either
    the whole rating ends up glued to the company name (sector left empty)
    or just the rating agency's name does, with its grade correctly landing
    in sector on its own (e.g. company "...LimitedCRISIL", sector "AA+").
    Either way, the agency name is pulled back out of the company text and
    recombined with whatever of the rating was already captured properly."""
    m = _GLUED_RATING_RE.match(company)
    if not m:
        return company, sector
    agency_grade = _clean(f"{m.group('agency')} {m.group('grade')}")
    if sector and not sector.upper().startswith(m.group("agency").upper()):
        return m.group("company"), _clean(f"{m.group('agency')} {sector}")
    return m.group("company"), (sector or agency_grade)


def _dedupe_holdings(holdings):
    result = []
    seen = set()
    for h in holdings:
        company = _clean(h.get("company", ""))
        sector = _clean(h.get("sector", ""))
        company, sector = _split_glued_rating(company, sector)
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
    headers = _augment_headers(headers, float(page.width))
    bold_lines = _page_bold_stop_lines(page)
    page_height = float(page.height)

    # Reading order for this side-by-side column template is "down one
    # column, then down the next" rather than strict top-to-bottom across
    # the whole page, which matters for carrying a dangling trailing
    # category label (see _extract_holdings_for_group) from the foot of
    # one column to the head of the next.
    # Reading order for this side-by-side column template is "down one
    # column, then down the next" rather than strict top-to-bottom across
    # the whole page, which matters for carrying a dangling trailing
    # category label (see _extract_holdings_for_group) from the foot of
    # one column to the head of the next. Header groups stacked in the same
    # visual column (e.g. an "EQUITY HOLDINGS" table with a "FIXED INCOME
    # HOLDINGS" table continuing underneath it) share -- to within a few pt
    # of label-width jitter -- the same name_x0, so they are clustered into
    # column buckets first and then read bucket-by-bucket, top to bottom
    # within each.
    columns = []
    for h in sorted(headers, key=lambda h: h["name_x0"]):
        placed = False
        for col in columns:
            if abs(col[0]["name_x0"] - h["name_x0"]) <= 15:
                col.append(h)
                placed = True
                break
        if not placed:
            columns.append([h])
    reading_order = []
    for col in columns:
        reading_order.extend(sorted(col, key=lambda h: h["top"]))

    holdings = []
    carry_sector = ""
    for header in reading_order:
        group_holdings, carry_sector = _extract_holdings_for_group(
            page, header, bold_lines, page_height, carry_sector
        )
        holdings.extend(group_holdings)
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
    isin = ""
    managers = []
    holdings = []
    scheme_name = None

    for idx in page_idxs:
        page = pdf.pages[idx]

        if scheme_name is None:
            scheme_name = _extract_scheme_name(page)

        if benchmark is None:
            benchmark = extract_benchmark(page)
        if not isin:
            isin = extract_isin(page)
        for manager in extract_fund_managers(page):
            if manager not in managers:
                managers.append(manager)

        for holding in extract_holdings(page):
            if holding not in holdings:
                holdings.append(holding)

    additional_benchmark = None
    if scheme_name:
        additional_benchmark = _additional_benchmark_for(pdf, scheme_name)

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
