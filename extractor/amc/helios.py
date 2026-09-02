"""
Helios Mutual Fund factsheet extractor.

Mirrors the calling contract of amc/canara_robeco.py (segment_schemes /
extract_scheme_fields, identical return schema) but every bit of the
internal parsing is specific to how Helios Mutual Fund lays out its
factsheet:

  * A scheme's data spans (usually) two pages: a "page 1" carrying a
    "Fund Features" panel on the left and a "Portfolio" table
    (Issuer Name | Industry/Rating | % of AUM) on the right, followed
    by a "page 2" of pure charts (Asset Category Details, Top 10
    Stocks, Industry Allocation, "We have our Skin in the Game").
    Both pages repeat the same large-font scheme banner at the very
    top, so the natural "one dict entry per scheme name" grouping
    used by Canara Robeco applies here too.
  * Very large portfolios (e.g. Small Cap Fund, ~90 names) don't fit
    on page 1: the last few rows spill onto the top of the following
    (chart) page, complete with a repeated "Issuer Name / Industry/
    Rating / % of AUM" header -- but Helios's PDF export also leaves
    a stray duplicate of those trailing rows behind, overlapping the
    next page's banner. A final de-duplication pass on (company, pct)
    absorbs this without any per-scheme special-casing.
  * Unlike Canara Robeco, every holdings row already carries its own
    Industry (for equities) or Rating/description (for debt & money
    market instruments) directly in the "Industry/Rating" cell -- there
    is no separate "industry header" row to track state across. This
    makes row classification much simpler: every row is a holding
    except the roll-up "Equity Total" / "Grand Total" rows.
  * Hybrid schemes (Balanced Advantage, Arbitrage) print two more
    columns after "% of AUM" (Asset Description, Derivatives (Hedging)
    % of AUM, Net (Unhedged) Equity %). Only the primary "% of AUM"
    figure is used for pct_to_net_assets, so the table's right edge is
    clipped to just before "Asset Description" on those pages.
  * "Additional benchmark" is not printed next to the scheme's own
    Fund Features panel; Helios instead states it explicitly as running
    prose in the "Scheme Performance" section footnote ("...Benchmark:
    <X> Additional Benchmark: <Y> Inception Date: ..."), keyed by the
    scheme name heading that precedes each block.

Nothing here is wired to a specific page number, month, or scheme
name -- everything is derived from on-page text/coordinates so the
same code keeps working on next month's factsheet.
"""

from __future__ import annotations

import bisect
import re

from ..config import HEADING_EXCLUDE, SCHEME_KEYWORDS

# --------------------------------------------------------------------------
# generic text/word helpers
# --------------------------------------------------------------------------

_PUA_RE = re.compile(r"[\u2022\u25cf\u25aa\u25e6\u2023\u2043\ue000-\uf8ff]")
_WS_RE = re.compile(r"\s+")
_TRAILING_FOOTNOTE_RE = re.compile(r"[^A-Za-z0-9\s():&,/'\-]+$")
_NUM_RE = re.compile(r"^-?\d+\.\d{1,2}$")


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
            x_tolerance=3, y_tolerance=1.5, keep_blank_chars=False, extra_attrs=["size"]
        )
        or []
    )


def _norm_key(text):
    """Loose normalisation for matching scheme names across sections."""
    text = text.upper()
    text = re.sub(r"\(FORMERLY[^)]*\)", " ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return _WS_RE.sub(" ", text).strip()


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


# --------------------------------------------------------------------------
# scheme segmentation
# --------------------------------------------------------------------------

# Real scheme banners are rendered noticeably larger than anything else on
# the page (~15.9pt in this edition vs. ~13pt for the biggest section
# titles and ~9.4pt for the small "<Scheme Name>" sub-headings repeated
# inside the Performance/SIP tables). Rather than pin the check to this
# edition's exact point size (which a future template refresh could shift
# up or down), the banner is instead identified *relative to* the largest
# font size actually used anywhere on that page, with a small absolute
# floor to rule out matching on a page that has no large text at all.
_HEADING_SIZE_RATIO = 0.85
_HEADING_MIN_ABS_SIZE = 11.0

_HEADING_EXCLUDE_EXTRA = (
    "MUTUAL FUND",
    "CAPITAL ASSET MANAGEMENT",
    "FACTSHEET",
    "PERFORMANCE",
    "DISTRIBUTION",
    "RISKOMETER",
)

_NON_SCHEME_SECTION_RE = re.compile(
    r"^(?:Scheme Performance|SIP Performance|Income Distribution|How to Read|"
    r"Riskometer|Potential Risk Class|Index|Disclaimer|Helios Capital Asset)",
    re.IGNORECASE,
)


def _scheme_heading_name(page):
    """If the very top line of `page` is a large-font 'Helios <Scheme> Fund'
    banner, return the cleaned scheme name; otherwise None.

    Restricting to the topmost band of words (rather than just the first
    line of `extract_text()`) is what keeps this from firing on the small
    '<Scheme Name>' sub-headings that Helios reprints inside the later
    Scheme Performance / SIP Performance tables -- those sit well under
    the size floor computed below.
    """
    words = _page_words(page)
    if not words:
        return None
    min_top = min(float(w["top"]) for w in words)
    band = [w for w in words if float(w["top"]) <= min_top + 3]
    if not band:
        return None

    page_max_size = max((float(w.get("size") or 0) for w in words), default=0)
    if page_max_size < _HEADING_MIN_ABS_SIZE:
        return None
    size_floor = max(_HEADING_MIN_ABS_SIZE, page_max_size * _HEADING_SIZE_RATIO)
    if not all(float(w.get("size") or 0) >= size_floor for w in band):
        return None

    line = _clean(" ".join(w["text"] for w in sorted(band, key=lambda w: w["x0"])))
    normalized = _strip_trailing_footnote_symbols(line)
    if not normalized or len(normalized) > 90:
        return None
    upper = normalized.upper()
    if not upper.startswith("HELIOS"):
        return None
    # Tolerate trailing decoration after the scheme name itself -- e.g. a
    # footnote marker the symbol-stripper above didn't catch, or (in a
    # future edition) something like "(Formerly ...)" -- by keeping only
    # the "Helios ... Fund" span and dropping anything after it.
    fm = re.match(r"^(HELIOS\b.*?\bFUND)\b", upper)
    if not fm:
        return None
    normalized = normalized[: fm.end()].strip()
    upper = fm.group(1)
    if any(ex in upper for ex in HEADING_EXCLUDE):
        return None
    if any(ex in upper for ex in _HEADING_EXCLUDE_EXTRA):
        return None
    return normalized


def segment_schemes(pdf):
    """Return {scheme_name: [page_index, ...]} in document order.

    Each Helios scheme's first page carries a large-font "Helios <Scheme>
    Fund" banner (repeated, at the same size, on its chart page too, so
    both pages naturally collapse into the same dict entry). A handful of
    very long portfolios spill their final rows onto the top of the chart
    page without repeating that big banner -- such a continuation page is
    instead recognised by the "Issuer Name / Industry/Rating / % of AUM"
    table header it still carries.
    """
    schemes = {}
    order = []
    current = None

    for i, page in enumerate(pdf.pages):
        heading = _scheme_heading_name(page)
        if heading:
            if heading not in schemes:
                schemes[heading] = []
                order.append(heading)
            current = heading
            schemes[current].append(i)
            continue

        if current is None:
            continue

        text = page.extract_text() or ""
        first_line = (text.split("\n") or [""])[0].strip()
        if _NON_SCHEME_SECTION_RE.match(first_line):
            current = None
            continue

        if _find_portfolio_header(page):
            schemes[current].append(i)
        else:
            current = None

    return {name: schemes[name] for name in order}


# --------------------------------------------------------------------------
# portfolio table header detection
# --------------------------------------------------------------------------


def _find_portfolio_header(page):
    """Locate the "Issuer Name | Industry/Rating | % of AUM [| Asset
    Description | ...]" header on a scheme page and return the x0
    boundaries needed to slice holdings rows out by coordinate, or None
    if this page doesn't carry a portfolio table at all."""
    words = _page_words(page)
    issuer_positions = [
        (float(w["top"]), float(w["x0"])) for w in words if w["text"] == "Issuer"
    ]
    if not issuer_positions:
        return None
    header_top, name_x0 = min(issuer_positions, key=lambda p: p[0])

    band = [w for w in words if header_top - 10 <= float(w["top"]) <= header_top + 15]

    industry_candidates = [
        float(w["x0"]) for w in band if w["text"] == "Industry/Rating"
    ]
    if not industry_candidates:
        return None
    industry_x0 = min(industry_candidates)

    # The "% of AUM" data column lines up with the '%' glyph itself, not
    # with "AUM" -- on hybrid schemes the phrase wraps as "% of" / "AUM"
    # across two lines with "AUM" re-indented to roughly the same x0 as
    # '%', while on simple schemes it's one line "% of AUM" and the data
    # sits under the '%'. So anchor on '%' immediately followed by 'of',
    # then confirm an 'AUM' nearby (same row or the next line down) to
    # rule out an unrelated '%' elsewhere in the header band (e.g. the
    # hybrid "Derivatives (Hedging) %" / "Net (Unhedged) Equity %"
    # columns, whose own '%' glyphs are never immediately followed by
    # 'of' on the same row).
    pct_candidates = []
    for w in band:
        if w["text"] != "%":
            continue
        wx0, wtop, wx1 = float(w["x0"]), float(w["top"]), float(w["x1"])
        if wx0 <= industry_x0:
            continue
        of_word = None
        for ow in band:
            if ow["text"] != "of":
                continue
            otop, ox0 = float(ow["top"]), float(ow["x0"])
            if abs(otop - wtop) <= 2 and 0 <= ox0 - wx1 <= 15:
                of_word = ow
                break
        if of_word is None:
            continue
        oftop, ofx0, ofx1 = (
            float(of_word["top"]),
            float(of_word["x0"]),
            float(of_word["x1"]),
        )
        aum_confirmed = False
        for aw in band:
            if aw["text"] != "AUM":
                continue
            atop, ax0 = float(aw["top"]), float(aw["x0"])
            same_row = abs(atop - oftop) <= 2 and 0 <= ax0 - ofx1 <= 15
            stacked = 2 < abs(atop - wtop) <= 12 and abs(ax0 - wx0) <= 20
            if same_row or stacked:
                aum_confirmed = True
                break
        if aum_confirmed:
            pct_candidates.append(wx0)
    if not pct_candidates:
        return None
    pct_x0 = min(pct_candidates)

    # Hybrid schemes (Balanced Advantage, Arbitrage) print further columns
    # -- Asset Description, Derivatives (Hedging) % of AUM, Net (Unhedged)
    # Equity % -- after "% of AUM". Their data is plain decimals too, so
    # without a right boundary they'd be mistaken for more "% of AUM"
    # holdings. "Asset" stacked directly above "Description" (two-line
    # header, like "% of" / "AUM") marks where that boundary starts.
    right_edge = None
    asset_words = [w for w in band if w["text"] == "Asset"]
    desc_words = [w for w in band if w["text"] == "Description"]
    for aw in asset_words:
        ax0, atop = float(aw["x0"]), float(aw["top"])
        if ax0 <= pct_x0:
            continue
        for dw in desc_words:
            dx0, dtop = float(dw["x0"]), float(dw["top"])
            if abs(dtop - atop) <= 12 and abs(dx0 - ax0) <= 15:
                right_edge = min(ax0, right_edge) if right_edge else ax0
                break

    return {
        "top": header_top,
        "name_x0": name_x0,
        "industry_x0": industry_x0,
        "pct_x0": pct_x0,
        "right_edge": right_edge,
    }


def _metadata_text(page, header):
    """Reconstruct the left "Fund Features" column as plain text, bounded
    to the left of wherever the portfolio table starts."""
    if header:
        boundary = header["name_x0"] - 8
    else:
        boundary = 240
    words = [w for w in _page_words(page) if float(w["x1"]) <= boundary]
    rows = _cluster_rows(words)
    rows.sort(key=lambda r: r["top"])
    lines = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        lines.append(" ".join(w["text"] for w in ws))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# left-column ("Fund Features") field extraction
# --------------------------------------------------------------------------

_BENCHMARK_RE = re.compile(
    r"\bBenchmark\s*:\s*(.+?)(?=\n\s*(?:Plans and Options|Inception Date|"
    r"Minimum Investment|Additional Investment|Fund Manager|Entry Load|"
    r"Exit Load)\s*:|\Z)",
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
    # Helios's factsheet doesn't print a per-scheme ISIN anywhere in the
    # Fund Features panel; kept for interface/schema parity with other
    # AMC extractors and in case a future edition adds one.
    if not metadata_text:
        return ""
    m = _ISIN_RE.search(metadata_text)
    return m.group(1) if m else ""


_MANAGER_TITLE_RE = re.compile(
    r"\b(?:Mr|Ms|Mrs|Dr)\.\s*([A-Za-z][A-Za-z.]*(?:\s+[A-Za-z][A-Za-z.]*){0,4})"
)
_MANAGER_BLOCK_RE = re.compile(
    r"\bFund\s+Manager\s*:\s*(.+?)(?=\n\s*(?:Entry Load|Exit Load|Face Value)\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_SLEEVE_PATTERNS = (
    (re.compile(r"for\s+fixed\s+income", re.IGNORECASE), "Fixed Income"),
    (re.compile(r"for\s+equit", re.IGNORECASE), "Equity"),
)


def extract_fund_managers(metadata_text):
    if not metadata_text:
        return []
    m = _MANAGER_BLOCK_RE.search(metadata_text)
    if not m:
        return []
    block = m.group(1)

    matches = list(_MANAGER_TITLE_RE.finditer(block))
    managers = []
    seen = set()
    for i, mm in enumerate(matches):
        name = _clean(mm.group(1))
        name = re.sub(r"\s+is$", "", name, flags=re.IGNORECASE).strip()
        if not name or len(name) < 3:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        context = block[mm.end() : end]
        sleeve = None
        for pattern, label in _SLEEVE_PATTERNS:
            if pattern.search(context):
                sleeve = label
                break
        key = (name.lower(), sleeve)
        if key in seen:
            continue
        seen.add(key)
        managers.append({"role": "Fund Manager", "name": name, "sleeve": sleeve})
    return managers


# --------------------------------------------------------------------------
# additional-benchmark lookup (from the "Scheme Performance" section's
# footnote prose, matched back to each scheme by its heading)
# --------------------------------------------------------------------------

_PERF_SCHEME_HEADING_RE = re.compile(
    r"^(Helios [A-Za-z0-9&(),.'\- ]+? Fund)\s*$", re.MULTILINE
)
_ADDL_BENCHMARK_RE = re.compile(
    r"Additional\s+Benchmark\s*:\s*(.+?)\s*Inception\s+Date\s*:",
    re.IGNORECASE | re.DOTALL,
)


def _build_additional_benchmark_map(pdf):
    combined = {}
    for page in pdf.pages:
        text = page.extract_text() or ""
        if "Additional Benchmark" not in text:
            continue

        headings = list(_PERF_SCHEME_HEADING_RE.finditer(text))
        if not headings:
            continue

        for i, hm in enumerate(headings):
            start = hm.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            segment = text[start:end]
            am = _ADDL_BENCHMARK_RE.search(segment)
            if not am:
                continue
            value = _clean(am.group(1).replace("\n", " "))
            if not value:
                continue
            key = _norm_key(hm.group(1))
            combined.setdefault(key, value)
    return combined


def _get_additional_benchmark_map(pdf):
    cache = getattr(pdf, "_helios_addl_benchmark_cache", None)
    if cache is not None:
        return cache
    cache = _build_additional_benchmark_map(pdf)
    try:
        pdf._helios_addl_benchmark_cache = cache
    except Exception:
        pass
    return cache


# --------------------------------------------------------------------------
# portfolio / holdings extraction
# --------------------------------------------------------------------------

# Pure roll-up / summary rows -- these carry their own "% of AUM" figure
# but aren't individual holdings. Unlike Canara Robeco, Helios has no
# separate industry-header rows to skip: every other row in the table
# (including asset-class leaves like "Triparty Repo", "Treasury Bills",
# "Government Securities", "Cash, Cash Equivalents And Others") is a
# genuine, itemised holding with its own Industry/Rating value.
_STOP_ROW_RE = re.compile(r"^grand\s+total\b", re.IGNORECASE)

_SUMMARY_ROW_WORDS = {"equity", "debt", "net", "grand", "unhedged", "hedged", "total"}


def _is_summary_row(company):
    """True for pure roll-up rows such as 'Equity Total', 'Grand Total',
    or the hybrid-scheme 'Equity / Net Equity Total' -- i.e. every word in
    the label is drawn from a small closed vocabulary of asset-class /
    roll-up terms and the row ends in the word 'Total'. Genuine issuer
    names never match this (they carry a company/instrument suffix)."""
    tokens = [t for t in re.split(r"[\s/]+", company) if t]
    if not tokens or tokens[-1].lower() != "total":
        return False
    return all(t.lower() in _SUMMARY_ROW_WORDS for t in tokens)


def _rows_for_page(page, header):
    words = [
        w
        for w in _page_words(page)
        if float(w["top"]) > header["top"] + 3
        and header["name_x0"] - 15
        <= float(w["x0"])
        < (header["right_edge"] or page.width)
    ]
    if not words:
        return []

    pct_x0 = header["pct_x0"]
    right_bound = header["right_edge"] if header["right_edge"] else page.width
    anchors = sorted(
        (
            w
            for w in words
            if _NUM_RE.match(w["text"]) and pct_x0 - 6 <= float(w["x0"]) < right_bound
        ),
        key=lambda w: float(w["top"]),
    )
    if not anchors:
        return []
    anchor_tops = [float(a["top"]) for a in anchors]

    # A holding's own name occasionally wraps across two lines, and its
    # "% of AUM" figure is centred on that wrapped block rather than
    # pinned to its own last line -- so the closest anchor by
    # |top - anchor_top|, not the next one at/after a word's own top,
    # correctly reunites a wrapped name with its own value.
    buckets = [[] for _ in anchors]
    max_lookback = 9.0
    n_anchors = len(anchors)
    for w in words:
        if w in anchors:
            continue
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

    margin = 8
    name_end = header["industry_x0"] - margin
    industry_end = pct_x0 - margin

    rows = []
    for anchor, bucket in zip(anchors, buckets):
        name_words, industry_words = [], []
        for w in bucket:
            x0 = float(w["x0"])
            if x0 < name_end:
                name_words.append(w)
            elif x0 < industry_end:
                industry_words.append(w)
            # else: falls in/after the pct column itself -- discard
            # (this only happens for stray glyphs, since the anchor
            # word itself is excluded above).

        def _join(ws):
            return _clean(
                " ".join(
                    w["text"] for w in sorted(ws, key=lambda w: (w["top"], w["x0"]))
                )
            )

        rows.append(
            {
                "top": float(anchor["top"]),
                "company": _join(name_words),
                "industry": _join(industry_words),
                "pct": anchor["text"],
            }
        )
    rows.sort(key=lambda r: r["top"])
    return rows


def extract_holdings(page, header):
    holdings = []
    for row in _rows_for_page(page, header):
        company = row["company"]
        if not company:
            continue
        if _STOP_ROW_RE.match(company):
            break
        if _is_summary_row(company):
            continue
        holdings.append(
            {
                "company": company,
                "sector": row["industry"],
                "pct_to_net_assets": row["pct"],
            }
        )
    return holdings


def _dedupe_holdings(holdings):
    """Drop exact (company, pct) repeats.

    The only source of these in Helios's factsheet is a PDF-export
    artifact: on schemes whose portfolio spills onto the following
    (chart) page, the trailing rows are sometimes rendered twice -- once
    correctly at the bottom of page 1, and again as a stray duplicate
    overlapping the banner of page 2. Two distinct real holdings sharing
    both an identical name and an identical percentage to two decimal
    places essentially never happens, so this is a safe, generic filter
    rather than a per-scheme workaround.
    """
    seen = set()
    deduped = []
    for h in holdings:
        key = (h["company"].strip().lower(), h["pct_to_net_assets"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)
    return deduped


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
        header = _find_portfolio_header(page)
        metadata_text = _metadata_text(page, header)

        if scheme_name is None:
            heading = _scheme_heading_name(page)
            if heading:
                scheme_name = heading

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

        if header:
            holdings.extend(extract_holdings(page, header))

    holdings = _dedupe_holdings(holdings)

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
