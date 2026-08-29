"""
DSP Mutual Fund factsheet extractor.

Mirrors the calling contract of amc/canara_robeco.py (segment_schemes /
extract_scheme_fields, identical return schema) but every bit of the
internal parsing is specific to how DSP lays out its factsheet, which
differs from Canara Robeco in several important ways:

  * One scheme per page. The Fund Information panel sits on the LEFT
    of the portfolio table on some pages and on the RIGHT on others
    (DSP alternates between the two templates), so the panel's side
    is detected per page rather than assumed.
  * The portfolio table has no "Market Cap" or always-present
    "Rating" sub-column the way Canara Robeco's does. Instead, a
    bucket/sector header row (e.g. "Banks 27.36%") is distinguished
    from an ordinary holding row (e.g. "ICICI Bank Limited 9.24%") by
    FONT WEIGHT: bucket headers and "Total" rollups are printed bold,
    individual holdings are not. Debt/money-market tables additionally
    carry a real "Rating" sub-column (populated per row, e.g. "CRISIL
    AAA", "SOV"), same idea as Canara Robeco's.
  * A single physical column on the page can contain more than one
    stacked "Name of Instrument ... % to Net Assets" table (e.g. an
    equity table followed, further down the same column, by a debt
    table with its own header and Rating column) -- there is no
    guarantee of exactly one header block per column.
  * "Additional benchmark" is not printed next to the scheme's own
    Fund Information panel; it only appears later, in the
    "Comparative Performance of all schemes" section, where each
    scheme's own benchmark is marked with a trailing "^" and the
    additional/standard benchmark with a trailing "#".

Nothing here is wired to a specific page number, month, or scheme
name -- everything is derived from on-page text/coordinates/font so
the same code keeps working on next month's factsheet.

Known limitations (documented rather than silently swallowed):
  * A handful of schemes with heavily derivative/arbitrage-style
    disclosures (e.g. an equity-savings or dynamic-asset-allocation
    fund's "Stock Futures" hedge book) don't fully reconcile to
    100% -- DSP's own layout for these is genuinely ambiguous even by
    inspection, and we err on the side of keeping every disclosed line
    rather than guessing which ones to drop.
  * `additional_benchmark` is best-effort. For plain equity schemes
    (the majority) it resolves cleanly; for schemes whose Comparative
    Performance benchmark name wraps across more than two lines, the
    lookup can miss or return a partial string, in which case it comes
    back as ``None`` rather than a wrong-but-confident value.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter

from ..config import HEADING_EXCLUDE

# --------------------------------------------------------------------------
# generic text/word helpers
# --------------------------------------------------------------------------

_PUA_RE = re.compile(r"[\u2022\u25cf\u25aa\u25e6\u2023\u2043\ue000-\uf8ff\uf0fc]")
_WS_RE = re.compile(r"\s+")
_TRAILING_FOOTNOTE_RE = re.compile(r"[^A-Za-z0-9\s():&,/'\-]+$")
_PCT_RE = re.compile(r"^(-?\d+(?:\.\d+)?%|\*)$")


def _clean(text):
    """Strip bullet glyphs / PUA glyphs and normalise whitespace."""
    if not text:
        return ""
    text = _PUA_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _strip_trailing_footnote_symbols(text):
    """Drop trailing footnote markers such as '*', '^^', '$$' from text."""
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
            extra_attrs=["fontname", "size"],
        )
        or []
    )


def _is_bold(word):
    return "bold" in word.get("fontname", "").lower()


def _cluster_rows(words, y_tol=1.6):
    """Group words sharing (approximately) the same 'top' into a single
    physical line -- used for the Fund Information panel and the
    Comparative Performance section, both of which are plain,
    non-overlapping single-column text once isolated by x-range."""
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
    """Loose normalisation for matching scheme names across sections.
    DSP appends a '(Erstwhile known as ...)' / '(The ... Fund)' style
    suffix to some scheme names on the Comparative Performance pages
    even though the scheme's own heading omits it -- stripping *any*
    parenthetical (rather than special-casing 'Erstwhile') keeps this
    general enough to survive future wording changes."""
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return _WS_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------
# scheme segmentation
# --------------------------------------------------------------------------


def _is_scheme_heading(line):
    line = line.strip()
    if not line or len(line) > 90:
        return False
    normalized = _strip_trailing_footnote_symbols(line)
    upper = normalized.upper()
    if not upper.startswith("DSP"):
        return False
    if any(ex in upper for ex in HEADING_EXCLUDE):
        return False
    return True


def _clean_scheme_name(line, next_line=None):
    name = _strip_trailing_footnote_symbols(line.strip())
    if next_line and name.count("(") > name.count(")"):
        # a long "(Erstwhile ...)" suffix occasionally wraps onto a
        # second line (e.g. "DSP Aggressive Hybrid Fund (Erstwhile DSP
        # Equity &" / "Bond Fund)") -- pull in the closing fragment so
        # the parenthetical, and therefore the scheme name, is whole.
        candidate = _strip_trailing_footnote_symbols(next_line.strip())
        if (
            candidate
            and len(candidate) <= 40
            and not candidate.upper().startswith("DSP")
        ):
            name = f"{name} {candidate}"
    return _clean(name)


def segment_schemes(pdf):
    """Return {scheme_name: [page_index, ...]} in document order.

    Each DSP scheme starts with a "DSP <NAME>" heading as the very
    first line of its page, carrying a Fund Information panel and a
    portfolio table ("Name of Instrument ... % to Net Assets"). A
    scheme can in principle spill onto a following page (a very long
    holdings list); such a continuation page won't repeat the heading
    but will still have its own portfolio table header, which is what
    we key off of rather than any month/page-specific text.
    """
    schemes = {}
    order = []
    current = None

    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        lines = text.split("\n")
        first_line = lines[0] if lines else ""

        if _is_scheme_heading(first_line):
            name = _clean_scheme_name(first_line, lines[1] if len(lines) > 1 else None)
            if name not in schemes:
                schemes[name] = []
                order.append(name)
            current = name
            schemes[current].append(i)
            continue

        if current is None:
            continue
        if _find_header_instances(page):
            schemes[current].append(i)
        else:
            # Any other page (Comparative Performance, SIP tables,
            # Snapshot pages, disclaimers, ...) ends the current
            # scheme's run of pages.
            current = None

    return {name: schemes[name] for name in order}


# --------------------------------------------------------------------------
# portfolio-table header / column-band detection
# --------------------------------------------------------------------------


def _find_header_instances(page):
    """Find every 'Name of Instrument [Rating] % to Net Assets' header
    occurring anywhere on the page. Unlike Canara Robeco (one "PORTFOLIO"
    band, headers only near its top), a single DSP page can have several
    of these stacked vertically in the same physical column -- e.g. an
    equity table followed, further down, by a debt table with its own
    header and Rating column -- so we scan the whole page rather than a
    band near a single anchor word."""
    words = _page_words(page)
    instances = []
    for i, w in enumerate(words):
        if w["text"] != "Name":
            continue
        if i + 2 >= len(words):
            continue
        if words[i + 1]["text"] != "of" or words[i + 2]["text"] != "Instrument":
            continue
        name_top = float(w["top"])
        name_x0 = float(w["x0"])

        # search a small local band around this header for the
        # '% to Net Assets' sequence and an optional 'Rating' column
        band = [
            bw
            for bw in words
            if name_top - 15 <= float(bw["top"]) <= name_top + 15
            and name_x0 - 10 <= float(bw["x0"]) <= name_x0 + 300
        ]

        pct_word = net_word = None
        for bw in band:
            if bw["text"] != "%":
                continue
            to_w = next(
                (
                    ow
                    for ow in band
                    if ow["text"] == "to"
                    and abs(ow["top"] - bw["top"]) <= 2
                    and 0 < ow["x0"] - bw["x1"] <= 8
                ),
                None,
            )
            if not to_w:
                continue
            nw = next(
                (
                    ow
                    for ow in band
                    if ow["text"] == "Net"
                    and abs(ow["top"] - to_w["top"]) <= 2
                    and 0 < ow["x0"] - to_w["x1"] <= 8
                ),
                None,
            )
            if not nw:
                continue
            pct_word, net_word = bw, nw
            break
        if pct_word is None:
            continue

        # 'Assets' may be on the same line as '% to Net' or wrapped to
        # the next line
        assets_w = None
        for ow in band:
            if ow["text"] != "Assets":
                continue
            same_line = abs(ow["top"] - net_word["top"]) <= 2
            wrapped = 2 < ow["top"] - net_word["top"] <= 10
            if (same_line or wrapped) and abs(ow["x0"] - net_word["x0"]) <= 25:
                assets_w = ow
                break

        nav_x0 = float(pct_word["x0"])
        nav_x1 = float((assets_w or net_word)["x1"])

        rating_x0 = None
        for ow in band:
            if ow["text"] != "Rating":
                continue
            if name_x0 < float(ow["x0"]) < nav_x0 and abs(ow["top"] - name_top) <= 10:
                rating_x0 = float(ow["x0"])
                break

        instances.append(
            {
                "top": name_top,
                "name_x0": name_x0,
                "rating_x0": rating_x0,
                "nav_x0": nav_x0,
                "nav_x1": nav_x1,
            }
        )
    return instances


def _assign_bands(instances, tolerance=40):
    """Cluster header instances into left-to-right column bands by
    name_x0 proximity (a band can hold more than one stacked header)."""
    if not instances:
        return []
    ordered = sorted(instances, key=lambda h: h["name_x0"])
    bands = []
    for h in ordered:
        placed = False
        for band in bands:
            if abs(band[0]["name_x0"] - h["name_x0"]) <= tolerance:
                band.append(h)
                placed = True
                break
        if not placed:
            bands.append([h])
    bands.sort(key=lambda band: min(x["name_x0"] for x in band))
    return bands


def _find_grand_total(page):
    """Locate the (top, x0) of the 'GRAND' 'TOTAL' word pair -- DSP
    prints exactly one of these per scheme page, at the true end of
    that page's holdings list, regardless of which column it lands in
    (columns are frequently uneven lengths)."""
    words = _page_words(page)
    for i, w in enumerate(words):
        if (
            w["text"] == "GRAND"
            and i + 1 < len(words)
            and words[i + 1]["text"] == "TOTAL"
        ):
            return (float(w["top"]), float(w["x0"]))
    return None


def _compute_table_regions(page, grand_total_pos=None):
    """Return a list of table regions in reading order (band left to
    right, top to bottom within each band), each carrying a right_edge
    and a bottom bound to slice words out of.

    right_edge for the last (rightmost) band in a row is derived from
    the actual width of that band's own '% to Net Assets' figures
    rather than a fixed guess -- a bare Rating+% column needs far less
    width than a bare '%' column, and a fixed margin either clips long
    values or lets in unrelated content (pie-chart legends, side
    notes, mini footnote tables) sitting just past the real column.
    That refinement happens per-region in `_rows_for_region`; here we
    only need the coarse band-to-band boundary.

    bottom bound: GRAND TOTAL caps whichever band's x-range it falls
    into (that band's content genuinely ends there); every other band
    -- and every non-last stacked header within the GRAND-TOTAL band
    -- is bounded by the next header below it, or by the page bottom.
    """
    instances = _find_header_instances(page)
    bands = _assign_bands(instances)
    if not bands:
        return []

    band_name_x0 = [min(x["name_x0"] for x in band) for band in bands]
    band_nav_x1 = [max(x["nav_x1"] for x in band) for band in bands]

    regions = []
    for bi, band in enumerate(bands):
        band.sort(key=lambda h: h["top"])
        if bi + 1 < len(bands):
            right_edge = (band_nav_x1[bi] + band_name_x0[bi + 1]) / 2
        else:
            right_edge = band_nav_x1[bi] + 30

        band_bottom = page.height - 12
        if grand_total_pos is not None:
            gt_top, gt_x0 = grand_total_pos
            left_bound = band_name_x0[bi] - 15
            if left_bound <= gt_x0 < right_edge:
                band_bottom = gt_top - 0.5

        for ii, h in enumerate(band):
            bottom = band[ii + 1]["top"] if ii + 1 < len(band) else band_bottom
            regions.append(
                {
                    "top": h["top"],
                    "name_x0": h["name_x0"],
                    "rating_x0": h["rating_x0"],
                    "nav_x0": h["nav_x0"],
                    "nav_x1": h["nav_x1"],
                    "right_edge": right_edge,
                    "bottom": bottom,
                }
            )
    return regions


# --------------------------------------------------------------------------
# Fund Information panel: metadata / benchmark / ISIN / fund managers
# --------------------------------------------------------------------------

_PANEL_LABEL_WORDS = (
    ("BENCHMARK",),
    ("INCEPTION", "DATE"),
    ("FUND", "MANAGER"),
    ("NAV", "AS", "ON"),
    ("TOTAL", "AUM"),
)


def _find_panel_x0(page, words):
    """Locate the Fund-Information panel's left-aligned x0 by matching
    one of its standard label phrases. DSP alternates between putting
    this panel to the left of the portfolio table and to the right of
    it depending on the scheme, so this has to be detected per page
    rather than assumed."""
    for phrase in _PANEL_LABEL_WORDS:
        for i, w in enumerate(words):
            if w["text"] != phrase[0]:
                continue
            ok = True
            for k in range(1, len(phrase)):
                if i + k >= len(words) or words[i + k]["text"] != phrase[k]:
                    ok = False
                    break
            if ok:
                return float(w["x0"])
    return None


def _metadata_text(page, regions):
    """Reconstruct the Fund Information panel as plain text, bounded to
    whichever side of the portfolio table it actually sits on."""
    words = _page_words(page)
    panel_x0 = _find_panel_x0(page, words)
    if panel_x0 is None:
        return ""

    band_name_x0 = [r["name_x0"] for r in regions]
    min_name_x0 = min(band_name_x0) if band_name_x0 else None

    if min_name_x0 is not None and panel_x0 < min_name_x0:
        # panel sits to the left of the portfolio table
        boundary = min_name_x0 - 8
        selected = [w for w in words if float(w["x1"]) <= boundary]
    else:
        # panel sits to the right of the table (or there's no table on
        # this page at all) -- keep only the panel's own narrow
        # left-aligned column so we don't sweep in unrelated content
        # (pie-chart legends, footnotes) sitting between the table and
        # the panel.
        left_bound = panel_x0 - 5
        selected = [w for w in words if float(w["x0"]) >= left_bound]

    rows = _cluster_rows(selected)
    rows.sort(key=lambda r: r["top"])
    lines = [
        _clean(" ".join(w["text"] for w in sorted(r["words"], key=lambda w: w["x0"])))
        for r in rows
    ]
    return "\n".join(lines)


_BENCHMARK_STOP = (
    r"FUND MANAGER|NAV AS ON|TOTAL AUM|MONTHLY AVERAGE AUM|INCEPTION DATE|"
    r"Month End Expense|Portfolio Turnover|3 Year Risk|AVERAGE MATURITY|"
    r"MODIFIED DURATION|PORTFOLIO YTM|PORTFOLIO MACAULAY|BSE\s*&\s*NSE|"
    r"Tracking Error|ASSET ALLOCATION|MINIMUM INVESTMENT|EXIT LOAD|"
    r"Regular Plan|Direct Plan"
)
_BENCHMARK_RE = re.compile(
    r"\bBENCHMARK\s*:?\s*\n?(.+?)(?=\n\s*(?:" + _BENCHMARK_STOP + r")|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def extract_benchmark(metadata_text):
    if not metadata_text:
        return None
    m = _BENCHMARK_RE.search(metadata_text)
    if not m:
        return None
    value = _clean(m.group(1).replace("\n", " "))
    value = _strip_trailing_footnote_symbols(value)
    return value or None


_ISIN_RE = re.compile(r"\bISIN\s*[:\-]?\s*([A-Z]{2}[A-Z0-9]{9}\d)\b")


def extract_isin(metadata_text):
    """DSP's Fund Information panel doesn't publish a per-scheme ISIN
    the way some AMCs do (the only ISINs on a scheme page are for
    individual debt holdings inside a footnote 'Yield to Call' table,
    which is out of scope here), so this normally returns ''."""
    if not metadata_text:
        return ""
    m = _ISIN_RE.search(metadata_text)
    return m.group(1) if m else ""


_MANAGER_BLOCK_RE = re.compile(
    r"\bFUND MANAGER\s*\n?(.+?)(?=\n\s*(?:NAV AS ON|TOTAL AUM|INCEPTION DATE)|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# DSP's manager bios are a repeating, self-contained structured pattern
# ("<Name> [(<sleeve>)] Total work experience of N years. Managing this
# Scheme since <date>."), unlike Canara Robeco's free-flowing "Mr./Ms.
# <Name> is managing the scheme since ..." prose -- so instead of
# anchoring on a title (DSP bios have none), the whole entry pattern is
# matched at once, which also naturally captures the sleeve label DSP
# already prints in parentheses right after the name (e.g. "Rohit
# Singhania (Equity Portion)") instead of having to infer it from
# nearby prose.
_MANAGER_ENTRY_RE = re.compile(
    r"([A-Z][A-Za-z.&'\-]*(?:\s+[A-Za-z.&'\-]+)*?)"
    r"(?:\s*\(([^)]+)\))?\s*"
    r"Total work experience of\s*\d+\s*years?\.?\s*"
    r"Managing (?:this|the) (?:Scheme|fund)\s+since\b",
    re.IGNORECASE,
)


def extract_fund_managers(metadata_text):
    if not metadata_text:
        return []
    m = _MANAGER_BLOCK_RE.search(metadata_text)
    if not m:
        return []
    block = _WS_RE.sub(" ", " ".join(m.group(1).split("\n")))

    managers = []
    seen = set()
    for mm in _MANAGER_ENTRY_RE.finditer(block):
        name = _strip_trailing_footnote_symbols(_clean(mm.group(1)))
        if not name or len(name) < 3:
            continue
        sleeve = _clean(mm.group(2)) if mm.group(2) else None
        key = (name.lower(), sleeve)
        if key in seen:
            continue
        seen.add(key)
        managers.append({"role": "Fund Manager", "name": name, "sleeve": sleeve})
    return managers


# --------------------------------------------------------------------------
# additional-benchmark lookup (from the separate "Comparative
# Performance of all schemes" section, matched back to each scheme by
# name)
# --------------------------------------------------------------------------

_ADDL_BENCHMARK_TOKEN_RE = re.compile(r"(\S+)\s*#(?!#)")
_GROWTH_PHRASE_RE = re.compile(r"Growth of Rs 10,?000", re.IGNORECASE)


def _extract_addl_benchmark_from_line(line):
    """DSP marks each scheme's own benchmark with a trailing '^' and the
    additional/standard benchmark with a trailing '#' in the
    Comparative Performance table header row, e.g. '...Nifty 500
    (TRI)^ Growth of Rs 10,000 Nifty 50 (TRI)# Growth of Rs 10,000'.
    The name we want is the run of words between the *last* 'Growth of
    Rs 10,000' before the '#' and the '#' itself."""
    m = _ADDL_BENCHMARK_TOKEN_RE.search(line)
    if not m:
        return None
    prefix = line[: m.start()]
    growth_matches = list(_GROWTH_PHRASE_RE.finditer(prefix))
    start = growth_matches[-1].end() if growth_matches else 0
    name = _strip_trailing_footnote_symbols(_clean(prefix[start:] + m.group(1)))
    return name or None


def _is_heading_candidate(line):
    return (
        line.startswith("DSP")
        and not line.lower().startswith("dsp mutual fund offers")
        and "+" not in line
        and "Growth of Rs" not in line
        and not line.rstrip().endswith(("-", "^", "#"))
    )


def _additional_benchmarks_on_page(page):
    """Scan one 'Comparative Performance' page (clean, non-overlapping
    single-column text once row-clustered -- unlike the portfolio
    tables, this section's columns don't visually interleave) and
    return {normalized_scheme_name: [additional_benchmark_candidate, ...]}."""
    rows = _cluster_rows(_page_words(page))
    rows.sort(key=lambda r: r["top"])
    lines = [
        _clean(" ".join(w["text"] for w in sorted(r["words"], key=lambda w: w["x0"])))
        for r in rows
    ]

    results = {}
    last_heading = None
    for i, line in enumerate(lines):
        if _is_heading_candidate(line):
            last_heading = line
            continue
        if not line.startswith("Period"):
            continue
        # the header row can wrap onto the next 1-2 lines (long
        # benchmark names) before the actual data rows start
        combined = line
        for nxt in lines[i + 1 : i + 3]:
            if re.match(r"^(1 Year|3 Year|5 Year|Since Inception|\d)", nxt):
                break
            combined += " " + nxt
        name = _extract_addl_benchmark_from_line(combined)
        if name and last_heading:
            results.setdefault(_norm_key(last_heading), []).append(name)
    return results


def _build_additional_benchmark_map(pdf):
    combined = {}
    for page in pdf.pages:
        text = page.extract_text() or ""
        if "Period" not in text or "Growth of Rs" not in text:
            continue
        if not re.search(r"\bDSP\b", text):
            continue
        for key, values in _additional_benchmarks_on_page(page).items():
            combined.setdefault(key, []).extend(values)

    resolved = {}
    for key, values in combined.items():
        resolved[key] = Counter(values).most_common(1)[0][0]
    return resolved


def _get_additional_benchmark_map(pdf):
    cache = getattr(pdf, "_dsp_addl_benchmark_cache", None)
    if cache is not None:
        return cache
    cache = _build_additional_benchmark_map(pdf)
    try:
        pdf._dsp_addl_benchmark_cache = cache
    except Exception:
        pass
    return cache


# --------------------------------------------------------------------------
# portfolio / holdings extraction
# --------------------------------------------------------------------------

# Generic, AMFI-standard asset-class / sub-bucket labels. These carry
# no '% to Net Assets' figure of their own in the ordinary case (only
# their sub-buckets/sectors do, e.g. 'Banks 27.36%'), so when one of
# these labels is the *entire* text of a row it's a pure structural
# marker, not a holding, and never becomes a sector name either. None
# of these are scheme- or company-specific -- they're the same
# boilerplate section labels used across virtually every Indian mutual
# fund's portfolio disclosure.
_ASSET_CLASS_PREFIXES = sorted(
    [
        "EQUITY & EQUITY RELATED",
        "DEBT INSTRUMENTS",
        "MONEY MARKET INSTRUMENTS",
        "GOVERNMENT SECURITIES (CENTRAL/STATE)",
        "GOVERNMENT SECURITIES",
        "BOND & NCD'S",
        "BOND & NCD",
        "LISTED / AWAITING LISTING ON THE STOCK EXCHANGES",
        "LISTED/AWAITING LISTING ON THE STOCK EXCHANGES",
        "UNLISTED",
        "ALTERNATIVE INVESTMENT FUNDS (AIF)",
        "ALTERNATIVE INVESTMENT FUND",
        "PREFERENCE SHARES",
        "PREFERENCE SHARE",
        "EXCHANGE TRADED FUNDS",
        "EXCHANGE TRADED FUND",
        "CASH & CASH EQUIVALENT",
        "SECURITISED DEBT",
        "SUBORDINATE DEBT",
        "SECURITY RECEIPTS",
        "CERTIFICATE OF DEPOSIT",
        "COMMERCIAL PAPERS",
        "TREASURY BILL",
        "OTHERS",
    ],
    key=len,
    reverse=True,
)

# A top-level asset-class marker never carries its own aggregate % in
# the primary portfolio table. Seeing one *with* a % means we've run
# into a restated/supplementary disclosure block (e.g. an underlying
# fund's own look-through allocation on a fund-of-funds page) rather
# than the scheme's own holdings.
_TOP_LEVEL_MARKERS = {
    "EQUITY & EQUITY RELATED",
    "DEBT INSTRUMENTS",
    "MONEY MARKET INSTRUMENTS",
    "GOVERNMENT SECURITIES",
    "GOVERNMENT SECURITIES (CENTRAL/STATE)",
    "OTHERS",
}

# Generic AMFI/SEBI cash & cash-equivalent line items -- standard
# terminology across virtually every Indian mutual fund's portfolio
# disclosure (not scheme- or company-specific). Unlike a real
# sector/company holding, these represent a holding in their own right
# even though DSP often styles them the same (bold, no separate
# constituent rows below them) as a bucket header.
_CASH_LEAF_LABELS = {
    "TREPS",
    "TREPS / REVERSE REPO INVESTMENTS",
    "TREPS/REVERSE REPO INVESTMENTS",
    "REVERSE REPO",
    "NET RECEIVABLES/PAYABLES",
    "NET PAYABLES/RECEIVABLES",
    "NET RECEIVABLE/PAYABLE",
    "NET CURRENT ASSETS",
    "CASH MARGIN",
    "MARGIN FIXED DEPOSIT",
    "FIXED DEPOSIT",
    "CASH & CASH EQUIVALENT",
}

_TOTAL_RE = re.compile(r"^(grand\s+)?total$", re.IGNORECASE)


def _strip_asset_class_prefixes(text):
    """Repeatedly strip known generic asset-class/sub-bucket labels off
    the front of a (possibly multi-label) merged header line, leaving
    just the real sector/company text, if any."""
    stripped = text
    changed = True
    while changed:
        changed = False
        upper = stripped.upper()
        for prefix in _ASSET_CLASS_PREFIXES:
            if upper.startswith(prefix):
                stripped = stripped[len(prefix) :].strip()
                changed = True
                break
    return stripped


def _rows_for_region(page, region):
    """Slice one table region's words into logical rows, one per
    '% to Net Assets' (or bare '*', DSP's "less than 0.01%" marker)
    figure -- reusing the same nearest-anchor/lookback technique
    Canara Robeco's extractor uses for wrapped names, since that part
    of the problem (a multi-line company name ending in a % value) is
    genuinely identical across AMCs."""
    all_words = _page_words(page)

    anchor_start = region.get("rating_x0") or region["nav_x0"]
    anchor_window_end = region["nav_x0"] + 45

    # First pass: find genuine 'NN.NN%' anchors within a tight window
    # to work out how far right this particular column's data actually
    # extends. This varies a lot (a bare 'Rating + %' column needs far
    # less width than a bare '%' column), and a fixed margin either
    # clips long values or lets in unrelated content sitting just past
    # the real column -- a pie-chart's rating-profile legend, a
    # "Yield to Call" footnote table, etc. A bare '*' is deliberately
    # excluded from this probe: it's a single narrow glyph that can
    # also appear as an unrelated footnote-reference marker elsewhere
    # on the page, which would otherwise skew the estimate.
    probe_anchors = [
        w
        for w in all_words
        if region["top"] - 3 <= float(w["top"]) < region["bottom"] - 0.5
        and anchor_start - 5 <= float(w["x0"]) <= anchor_window_end
        and re.match(r"^-?\d+(?:\.\d+)?%$", w["text"])
    ]
    if probe_anchors:
        effective_right_edge = min(
            region["right_edge"], max(float(w["x1"]) for w in probe_anchors) + 10
        )
    else:
        effective_right_edge = region["right_edge"]

    words = [
        w
        for w in all_words
        if region["top"] - 3 <= float(w["top"]) < region["bottom"] - 0.5
        and region["name_x0"] - 15 <= float(w["x0"]) < effective_right_edge
    ]
    words = [w for w in words if float(w["top"]) > region["top"] + 3]

    anchors = sorted(
        (
            w
            for w in words
            if _PCT_RE.match(w["text"])
            and anchor_start - 5 <= float(w["x0"]) <= anchor_window_end
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

    rating_x0 = region.get("rating_x0")
    first_col_end = rating_x0 or region["nav_x0"]
    margin = 12

    def _join(ws):
        return _clean(
            " ".join(w["text"] for w in sorted(ws, key=lambda w: (w["top"], w["x0"])))
        )

    rows = []
    for anchor, bucket in zip(anchors, buckets):
        name_words, rating_words = [], []
        for w in bucket:
            if w is anchor:
                continue
            x0 = float(w["x0"])
            if x0 < first_col_end - margin:
                name_words.append(w)
            elif rating_x0:
                rating_words.append(w)
            else:
                name_words.append(w)
        # drop decorative bullet/Wingdings glyphs (they clean to "")
        name_words = [w for w in name_words if _clean(w["text"])]

        # Group name words into physical lines. Leading lines with no
        # anchor of their own -- pure bucket/asset-class labels, e.g.
        # 'EQUITY & EQUITY RELATED' / 'Listed / awaiting listing on
        # the stock exchanges' stacked above a sector like 'Banks' --
        # get emitted as their own header-only pseudo rows so they
        # update the running sector name in document order instead of
        # being silently glued onto the next holding's text.
        lines = {}
        for w in name_words:
            lines.setdefault(round(float(w["top"])), []).append(w)
        ordered_lines = [lines[k] for k in sorted(lines)]

        for line in ordered_lines[:-1]:
            rows.append(
                {
                    "top": float(line[0]["top"]),
                    "company": _strip_trailing_footnote_symbols(_join(line)),
                    "rating": "",
                    "pct": "",
                    "bold": True,
                }
            )

        last_line = ordered_lines[-1] if ordered_lines else []
        bold = False
        if last_line:
            bold_count = sum(1 for w in last_line if _is_bold(w))
            bold = bold_count * 2 > len(last_line)

        company = _strip_trailing_footnote_symbols(_join(last_line))
        if not company and anchor["text"] == "*":
            # an isolated '*' with no company text attached is a
            # stray footnote-marker glyph, not a negligible-value
            # holding row -- discard it.
            continue
        stripped = _strip_asset_class_prefixes(company)
        if stripped:
            company = stripped

        rows.append(
            {
                "top": float(anchor["top"]),
                "company": company,
                "rating": _join(rating_words),
                "pct": anchor["text"],
                "bold": bold,
            }
        )
    rows.sort(key=lambda r: r["top"])
    return rows


def _classify_region_rows(rows):
    """Walk one table region's rows in document order, tracking the
    most recently seen sector/bucket header, and emit only the actual
    holdings."""
    holdings = []
    current_industry = ""
    for i, row in enumerate(rows):
        company = row["company"]
        if not company:
            continue

        if row["pct"] and company.upper() in _TOP_LEVEL_MARKERS:
            # Stop: this is a restated/supplementary disclosure block,
            # not the scheme's own holdings (see module docstring).
            break

        if _TOTAL_RE.match(company):
            if row["pct"] == "100.00%":
                # A whole-portfolio rollup (GRAND TOTAL, or a
                # secondary 100% 'TOTAL' as seen on fund-of-funds
                # pages restating an underlying fund's own
                # composition). Anything further in this region is
                # disclosure/footnote content, not real holdings.
                break
            continue

        if row["pct"] == "":
            # header-only pseudo row (a bucket/asset-class label with
            # no '%' of its own)
            stripped = _strip_asset_class_prefixes(company)
            if stripped:
                current_industry = stripped
            continue

        pct = "<0.01%" if row["pct"] == "*" else row["pct"]
        is_cash_leaf = company.upper() in _CASH_LEAF_LABELS

        if not is_cash_leaf and not _strip_asset_class_prefixes(company):
            # A pure asset-class/sub-bucket rollup with its own
            # aggregate % (e.g. 'Equity & Equity Related 75.90%')
            # rather than an individual holding. Some DSP pages render
            # these in the regular (non-bold) font by mistake, so this
            # check is text-based and doesn't depend on font weight.
            continue

        if row["bold"] and not is_cash_leaf:
            # Peek ahead: is this bold row itself a singleton bucket
            # that's really a holding (e.g. 'TREPS / Reverse Repo
            # Investments 7.33%'), detected by the next row being a
            # matching-value 'Total'?
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if (
                nxt
                and nxt["bold"]
                and _TOTAL_RE.match(nxt["company"])
                and nxt["pct"] == row["pct"]
            ):
                holdings.append(
                    {"company": company, "sector": "", "pct_to_net_assets": pct}
                )
                continue
            current_industry = company
            continue

        if is_cash_leaf:
            holdings.append(
                {"company": company, "sector": "", "pct_to_net_assets": pct}
            )
        elif row["rating"]:
            holdings.append(
                {"company": company, "sector": row["rating"], "pct_to_net_assets": pct}
            )
        else:
            holdings.append(
                {
                    "company": company,
                    "sector": current_industry,
                    "pct_to_net_assets": pct,
                }
            )
    return holdings


def extract_holdings(page):
    grand_total_pos = _find_grand_total(page)
    regions = _compute_table_regions(page, grand_total_pos)
    holdings = []
    for region in regions:
        rows = _rows_for_region(page, region)
        holdings.extend(_classify_region_rows(rows))
    return holdings


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
    scheme_name = None

    for pi in page_idxs:
        page = pdf.pages[pi]
        regions = _compute_table_regions(page, _find_grand_total(page))
        metadata_text = _metadata_text(page, regions)

        if scheme_name is None:
            text = page.extract_text() or ""
            lines = text.split("\n")
            first_line = lines[0] if lines else ""
            if _is_scheme_heading(first_line):
                scheme_name = _clean_scheme_name(
                    first_line, lines[1] if len(lines) > 1 else None
                )

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

        if regions:
            for region in regions:
                rows = _rows_for_region(page, region)
                holdings.extend(_classify_region_rows(rows))

    additional_benchmark = None
    if scheme_name:
        addl_map = _get_additional_benchmark_map(pdf)
        additional_benchmark = addl_map.get(_norm_key(scheme_name))

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": fund_managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
