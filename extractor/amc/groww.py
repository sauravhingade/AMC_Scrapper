"""
Groww Mutual Fund monthly-factsheet extractor.

Framework contract (kept identical to canara_robeco.py so the pipeline can
call this module exactly the same way):

    segment_schemes(pdf)                 -> {scheme_name: [page_idx, ...]}
    extract_holdings(page)               -> [{"company", "sector", "pct_to_net_assets"}, ...]
    extract_isin(text)                   -> str
    extract_scheme_fields(pdf, page_idxs)-> {
        "benchmark": str | None,
        "additional_benchmark": str | None,
        "isin": str,
        "fund_managers": [{"role", "name", "sleeve"}, ...],
        "holdings": [...],
        "holdings_count": int,
    }

Groww's factsheet is structurally very different from Canara Robeco's:

* One scheme per page (equity / hybrid / debt / index / ETF / FoF), with a
  single "FUND SNAPSHOT" panel on the left and a single PORTFOLIO table on
  the right -- there is no Canara-style two-group / continuation-page
  layout in the current template, but the code below does not assume that;
  it is written to keep working if a future month's PDF wraps a long
  portfolio onto a follow-on page.
* The portfolio table has only ONE classification column ("Industry/Rating"
  or "Rating Class" or plain "Rating"), which holds the equity sector for
  equity holdings *or* the credit rating for debt holdings -- Groww does
  not print a separate market-cap column the way Canara Robeco does, so
  none of that logic is reused here.
* Every scheme's benchmark, additional benchmark, and fund manager(s) are
  printed on the scheme's own page, so (unlike Canara Robeco) there is no
  need to search neighbouring pages for this metadata.
* The Grand Total line at the bottom of the portfolio table is printed
  without a trailing "%", which makes it invisible to the normal
  percentage-anchored row grouping and risks being merged into the last
  real holding above it; this is handled explicitly (see
  ``_find_grand_total_top``).

What IS reused from canara_robeco.py, because it is genuinely generic and
proven:
* the "nearest-percentage-anchor" strategy for reconstructing a table row
  whose company name/rating wrap across multiple PDF text lines while its
  own "% of NAV" figure sits vertically centred on the wrapped block
  (see ``_rows_for_group``);
* the overall shape of the public interface and the returned dict schema.
"""

from __future__ import annotations

import bisect
import re

import pdfplumber.utils as _pp_utils

try:
    from ..config import HEADING_EXCLUDE
except ImportError:  # pragma: no cover - keeps this module importable stand-alone
    HEADING_EXCLUDE = ()


# --------------------------------------------------------------------------
# Generic text / geometry helpers
# --------------------------------------------------------------------------

_PUA_RE = re.compile(r"[\uE000-\uF8FF\u2022\u25CF\u25AA\u2023\u2043]")
_WS_RE = re.compile(r"\s+")
_PCT_RE = re.compile(r"^-?\d+(?:\.\d+)?%$")


def _clean(text: str) -> str:
    """Strip bullet glyphs / private-use-area artefacts and collapse whitespace."""
    if not text:
        return ""
    text = _PUA_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    return text.strip(" -\u2013\u2014\t")


def _dedupe_chars(chars):
    """Drop exact-duplicate character glyphs.

    A small number of pages in the source PDF draw certain bold heading
    runs (the scheme title, "PORTFOLIO", "FUND SNAPSHOT", "DATE OF
    ALLOTMENT", ...) twice, as two separate character objects stacked at
    the identical (text, x0, top) position -- most likely a faux-bold
    rendering artefact from whatever tool produced that page. Left as-is,
    pdfplumber's word extraction reads each glyph twice ("PORTFOLIO"
    becomes "PPOORRTTFFOOLLIIOO"), which silently breaks every
    text-equality check in this module (heading detection, the PORTFOLIO
    table header, the FUND MANAGER / BENCHMARK labels, the Grand Total
    row, ...). Body text (company names, "% of NAV" figures, etc.) is not
    affected. Deduplicating by exact (text, rounded x0, rounded top) is
    safe because two genuinely distinct characters never legitimately
    share the same glyph at the same position.
    """
    seen = set()
    out = []
    for c in chars:
        key = (c["text"], round(float(c["x0"]), 1), round(float(c["top"]), 1))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _page_words(page):
    chars = _dedupe_chars(page.chars)
    # A tight x_tolerance matters here: a handful of adjacent-column word
    # pairs in the source PDF (e.g. a company's "...Limited" immediately
    # followed by its rating's "CRISIL...") are kerned only ~1.7pt apart,
    # which a default tolerance of 3 merges into one bogus glued word
    # ("LimitedCRISIL") that then straddles the company/rating column
    # boundary. Genuine intra-word character gaps in this document run
    # ~0pt, so 1.3 safely separates real word boundaries (consistently
    # >=1.5pt across the document) without fragmenting normal words.
    words = _pp_utils.extract_words(
        chars, x_tolerance=1.3, y_tolerance=1.5, keep_blank_chars=False
    )
    # Quadrant/gauge chart labels ("Fund Style", "Interest Rate
    # Sensitivity", ...) are rendered as rotated text. pdfplumber marks
    # some of them non-upright outright (in which case they surface here
    # reversed, e.g. "elytS" for "Style"); dropping non-upright words
    # keeps this rotated chart furniture out of every downstream text
    # match, since no genuine body/heading text in this document is
    # ever rotated.
    return [w for w in words if w.get("upright", True)]


def _page_text(page) -> str:
    """Like page.extract_text(), but immune to the duplicated-glyph pages
    (see ``_dedupe_chars``): heading detection, the additional-benchmark
    footnote scan, and the ISIN scan all read whole-page text and would
    otherwise silently fail to match on an affected page."""
    chars = _dedupe_chars(page.chars)
    return _pp_utils.extract_text(chars) or ""


def _cluster_rows(words, y_tol: float = 1.6):
    """Group words into visual rows by proximity in `top`, preserving reading order."""
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


def _row_text(row_words) -> str:
    # Sort by (top, x0) rather than x0 alone: a company/rating value that
    # wraps across two or three PDF text lines must read top line first,
    # left-to-right, then the next line -- sorting by x0 alone can
    # interleave words from different wrapped lines whenever their x0
    # ranges overlap (e.g. a two-word bucket header sitting just above a
    # holding whose own name also starts at the same left margin).
    return _clean(
        " ".join(
            w["text"]
            for w in sorted(row_words, key=lambda w: (float(w["top"]), float(w["x0"])))
        )
    )


def _smart_join(words, gap_threshold: float = 1.0) -> str:
    """Join words that all share (roughly) one visual row, inserting a
    space only where the horizontal gap between consecutive glyph runs
    indicates a real word boundary.

    A handful of the FUND MANAGER designation lines are emitted by the
    source PDF as one text-run per *character* (an embedded-italic-font
    quirk), which defeats a plain " ".join over pdfplumber's own word
    segmentation -- every "word" collapses to a single letter. Comparing
    the x-gap between successive runs recovers correct spacing in both the
    normal case (inter-word gaps of ~1.5pt+) and the pathological one
    (intra-word gaps of ~0pt, inter-word gaps of ~1.5pt+).

    This assumes `words` are all (approximately) one line -- callers with
    a multi-line block must join per-row with this and only then join the
    rows together (see ``_smart_join_rows``), since sorting a multi-row
    word list by x0 alone would interleave unrelated rows.
    """
    ws = sorted(words, key=lambda w: float(w["x0"]))
    out = []
    prev_x1 = None
    for w in ws:
        x0 = float(w["x0"])
        if prev_x1 is not None and x0 - prev_x1 > gap_threshold:
            out.append(" ")
        out.append(w["text"])
        prev_x1 = float(w["x1"])
    return _clean("".join(out))


def _smart_join_rows(rows, gap_threshold: float = 1.0) -> str:
    """Join a multi-row word block (as produced by ``_cluster_rows``) into
    one string: each row's own words are gap-joined left-to-right (see
    ``_smart_join``), and the resulting row strings are then joined in
    top-to-bottom order with a single space."""
    ordered = sorted(rows, key=lambda r: r["top"])
    return _clean(
        " ".join(_smart_join(r["words"], gap_threshold) for r in ordered if r["words"])
    )


# --------------------------------------------------------------------------
# Scheme segmentation
# --------------------------------------------------------------------------

_SCHEME_NAME_KEYWORDS = ("FUND", "ETF", "FOF")

_NON_SCHEME_SECTION_RE = re.compile(
    r"^(?:"
    r"snapshot\s+of|"
    r"index$|"
    r"how\s+to\s+read|"
    r"cio\s+desk|"
    r"market\s+outlook|"
    r"groww\s+performance\s+disclosure|"
    r"groww\s+sip\s+performance|"
    r"scheme\s*&\s*benchmark|"
    r"potential\s+risk\s+class|"
    r"idcw\s+history|"
    r"minimum\s+investment|"
    r"groww\s+asset\s+management"
    r")",
    re.IGNORECASE,
)

_TRAILING_FOOTNOTE_RE = re.compile(r"[\^#*\d]+$")


def _strip_trailing_footnote_symbols(line: str) -> str:
    return _TRAILING_FOOTNOTE_RE.sub("", line).strip()


def _is_scheme_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 100:
        return False
    normalized = _strip_trailing_footnote_symbols(line)
    upper = normalized.upper()
    if not upper.startswith("GROWW"):
        return False
    if any(ex in upper for ex in HEADING_EXCLUDE):
        return False
    if not any(kw in upper for kw in _SCHEME_NAME_KEYWORDS):
        return False
    return True


def _clean_scheme_name(line: str) -> str:
    return _clean(_strip_trailing_footnote_symbols(line))


def segment_schemes(pdf) -> dict:
    """Split the factsheet into {scheme_name: [page_idx, ...]}.

    A page starts a new scheme when its first line looks like a Groww
    scheme title *and* the page actually carries a PORTFOLIO table --
    the second check is what keeps AMC letterhead / disclaimer pages such
    as "Groww Asset Management Limited" or "Groww Smallcap 250 ETF"
    (an index-licensing disclaimer, not the scheme's factsheet page) from
    being mistaken for a new scheme.

    A page with no heading continues the current scheme if it still has a
    PORTFOLIO table (a long portfolio spilling onto a follow-on page);
    otherwise the current scheme run ends.
    """
    schemes: dict[str, list[int]] = {}
    order: list[str] = []
    current = None

    for i, page in enumerate(pdf.pages):
        text = _page_text(page)
        lines = text.split("\n")
        first_line = lines[0].strip() if lines else ""

        if _is_scheme_heading(first_line):
            if _find_portfolio_headers(page):
                name = _clean_scheme_name(first_line)
                if name not in schemes:
                    schemes[name] = []
                    order.append(name)
                current = name
                schemes[current].append(i)
                continue
            # Looked like a heading but has no portfolio table -> not a
            # real scheme page (e.g. a disclaimer page reusing the fund
            # name). Fall through to the continuation/ending logic below.

        if current is None:
            continue

        if _NON_SCHEME_SECTION_RE.match(first_line):
            current = None
            continue

        if _find_portfolio_headers(page):
            schemes[current].append(i)
        else:
            current = None

    return {name: schemes[name] for name in order}


# --------------------------------------------------------------------------
# Portfolio table: header + row detection
# --------------------------------------------------------------------------


def _header_phrase_left_edge(band, ntop, name_word_x0, right_edge):
    """Find the true left edge of the "Instrument Type/Issuer Name" (or
    "Company Name") header phrase.

    The FUND SNAPSHOT panel occasionally has a left-column label (e.g.
    "DATE OF ALLOTMENT") sitting at the exact same `top` as the PORTFOLIO
    table's header row purely by coincidence of that page's layout.
    Naively taking the minimum x0 of *every* word sharing that top would
    then drag the detected name-column boundary far to the left,
    swallowing the whole FUND SNAPSHOT panel into the "company name"
    column on every data row. Instead, walk left from the "Name" word
    only through words that are contiguous with it (a small horizontal
    gap, consistent with being part of the same header phrase); an
    unrelated label elsewhere on the row is always separated by a much
    larger gap and is left out.
    """
    row_words = sorted(
        (
            w
            for w in band
            if abs(float(w["top"]) - ntop) <= 2 and float(w["x0"]) < right_edge
        ),
        key=lambda w: float(w["x0"]),
    )
    if not row_words:
        return name_word_x0
    anchor_idx = min(
        range(len(row_words)),
        key=lambda i: abs(float(row_words[i]["x0"]) - name_word_x0),
    )
    left_edge = float(row_words[anchor_idx]["x0"])
    prev_x0 = left_edge
    for i in range(anchor_idx - 1, -1, -1):
        w = row_words[i]
        gap = prev_x0 - float(w["x1"])
        if gap > 25:
            break
        left_edge = float(w["x0"])
        prev_x0 = left_edge
    return left_edge


def _find_portfolio_headers(page) -> list:
    """Locate the PORTFOLIO table's column x-positions on this page.

    Returns a list of header dicts (usually just one, on a Groww page)
    sorted left-to-right, each with:
        top       - the header row's y position
        name_x0   - left edge of the "Instrument/Company Name" column
        sector_x0 - x0 of the "Industry/Rating" (or "Rating"/"Rating
                    Class") column, or None if not found
        nav_x0    - x0 of the "% of NAV" figure column
    """
    words = _page_words(page)
    port_tops = sorted(
        {round(float(w["top"]), 1) for w in words if w["text"] == "PORTFOLIO"}
    )
    if not port_tops:
        return []
    band_top = port_tops[0]
    band = [w for w in words if band_top <= float(w["top"]) <= band_top + 60]

    name_word_tops = [
        (float(w["top"]), float(w["x0"])) for w in band if w["text"] == "Name"
    ]

    nav_positions = []
    for w in band:
        if w["text"] != "NAV":
            continue
        for ow in band:
            if ow["text"] != "of":
                continue
            if (
                abs(ow["top"] - w["top"]) <= 2
                and 0 < float(w["x0"]) - float(ow["x1"]) <= 8
            ):
                nav_positions.append((float(w["top"]), float(w["x0"])))
                break

    rating_positions = [
        (float(w["top"]), float(w["x0"])) for w in band if "Rating" in w["text"]
    ]

    headers = []
    for ntop, name_word_x0 in name_word_tops:
        cands = [
            p for p in nav_positions if p[1] > name_word_x0 and abs(p[0] - ntop) <= 3
        ]
        if not cands:
            continue
        nav_x0 = min(cands, key=lambda p: p[1])[1]

        sector_x0 = None
        for rt, rx in rating_positions:
            if name_word_x0 < rx < nav_x0 and abs(rt - ntop) <= 3:
                sector_x0 = rx
                break

        right_edge_for_name = sector_x0 or nav_x0
        name_x0 = _header_phrase_left_edge(
            band, ntop, name_word_x0, right_edge_for_name
        )

        headers.append(
            {"top": ntop, "name_x0": name_x0, "sector_x0": sector_x0, "nav_x0": nav_x0}
        )

    headers.sort(key=lambda h: h["name_x0"])
    return headers


def _find_grand_total_top(page, name_x0: float):
    """Return the top of the "Grand Total" row, if present.

    Groww prints the Grand Total value without a "%" sign ("100.00"
    instead of "100.00%"), so it never becomes its own percentage anchor
    in ``_rows_for_group`` -- left unhandled, the words "Grand" and
    "Total" would instead be swept into whichever real holding row above
    it happens to be within the anchor lookback distance, corrupting that
    holding's company name. Finding this row's own top up front lets the
    row-grouping window stop just above it.
    """
    words = _page_words(page)
    grands = [
        w for w in words if w["text"] == "Grand" and float(w["x0"]) >= name_x0 - 15
    ]
    for g in grands:
        gtop, gx1 = float(g["top"]), float(g["x1"])
        for w in words:
            if w["text"] != "Total":
                continue
            if abs(float(w["top"]) - gtop) <= 2 and 0 <= float(w["x0"]) - gx1 <= 30:
                return gtop
    return None


_BARE_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")

_CATEGORY_HEADER_PHRASES = {
    "equity equity related holdings",
    "equity shares",
    "instrument",
    "govt securities sdl",
    "government securities",
    "government bonds",
    "government bond",
    "corporate bonds ncd",
    "corporate debt",
    "certificate of deposits",
    "commercial papers",
    "treasury bills",
    "tri party repo treps",
    "triparty repo reverse repo instrument",
    "triparty repo reverse repo",
    "mutual fund units",
    "alternative investement funds",
    "alternative investment funds",
    "cash cash equivalents",
    "money market instruments",
    "reverse repo",
    "futures",
    "options",
    "derivatives",
}


def _normalize_phrase(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9\s]", " ", text)
    text = _WS_RE.sub(" ", text).strip().lower()
    return text


def _rows_for_group(page, header, right_edge, bottom_bound):
    """Reconstruct portfolio rows for one column-group using nearest-
    percentage-anchor bucketing (reused from the proven Canara Robeco
    approach): every word is assigned to the row whose "% of NAV" figure
    sits closest to it vertically, which is what correctly re-associates
    a company name that wraps across two or three PDF text lines with its
    own (vertically centred) percentage and rating/sector text, even when
    those also wrap independently.
    """
    candidate_words = [
        w
        for w in _page_words(page)
        if header["top"] + 3 < float(w["top"]) < bottom_bound
        and header["name_x0"] - 15 <= float(w["x0"]) < right_edge
    ]

    # Drop pure SEBI/AMFI category-classification headings (e.g. "Tri
    # Party Repo (TREPs)", "Mutual Fund Units", "Certificate of
    # Deposits") before anchor bucketing. These are a small, stable,
    # regulator-defined vocabulary -- not scheme-specific content -- and
    # they never carry their own "% of NAV" figure. Left in the word
    # pool, such a line is routinely swept into whichever single holding
    # happens to be its bucket's only member and sits within the anchor
    # lookback distance below it, corrupting that holding's name (this is
    # not a rare edge case: most schemes hold exactly one TREPs line item
    # "The Clearing Corporation of India Ltd.", so without this filter
    # that holding's name is corrupted on nearly every scheme). A bucket
    # heading that *does* print its own aggregate percentage on the same
    # line (e.g. a derivatives "Futures 11.02%" line) is left untouched,
    # since it is then structurally real row data rather than a bare
    # heading.
    all_words = []
    for line in _cluster_rows(candidate_words, y_tol=2.0):
        has_pct = any(_PCT_RE.match(w["text"]) for w in line["words"])
        if (
            not has_pct
            and _normalize_phrase(_row_text(line["words"])) in _CATEGORY_HEADER_PHRASES
        ):
            continue
        all_words.extend(line["words"])

    anchor_start = header.get("sector_x0") or header["name_x0"]
    nav_x0 = header["nav_x0"]

    def _is_anchor(w):
        x0 = float(w["x0"])
        if _PCT_RE.match(w["text"]):
            return x0 >= anchor_start - 5
        # A handful of schemes print the "% of NAV" column without a "%"
        # suffix at all (e.g. a fund-of-funds' underlying-scheme
        # weights). Accept a bare number as an anchor only when it sits
        # tightly against the NAV column's own x-position, since a bare
        # number anywhere else in this word pool is far more likely to
        # be part of a company name (a bond's maturity year, etc.).
        if _BARE_NUM_RE.match(w["text"]):
            return abs(x0 - nav_x0) <= 20
        return False

    anchors = sorted(
        (w for w in all_words if _is_anchor(w)), key=lambda w: float(w["top"])
    )
    if not anchors:
        return []
    anchor_tops = [float(a["top"]) for a in anchors]
    anchor_ids = {id(a) for a in anchors}

    max_lookback = 9.0
    prev_anchor_bias = 1.5  # see docstring below
    n_anchors = len(anchors)
    buckets = [[] for _ in anchors]
    for w in all_words:
        wtop = float(w["top"])
        idx = bisect.bisect_left(anchor_tops, wtop)
        best_idx, best_dist = None, None
        for cand in (idx - 1, idx):
            if 0 <= cand < n_anchors:
                dist = abs(anchor_tops[cand] - wtop)
                # A word whose own row wraps across several lines can sit
                # almost exactly midway between its own anchor (the "% of
                # NAV" line further up) and the very next holding's
                # anchor just below it -- long issuer names like
                # "National Bank for Agriculture and Rural Development"
                # or "Small Industries Development Bank of India"
                # routinely produce a trailing continuation word only a
                # point or two closer to the *next* row than to their
                # own. Nudging the preceding anchor's effective distance
                # down resolves these near-ties in favour of the row the
                # word visually trails, without disturbing any case where
                # the two candidate distances aren't already close.
                if cand == idx - 1:
                    dist -= prev_anchor_bias
                if best_dist is None or dist < best_dist:
                    best_idx, best_dist = cand, dist
        if best_idx is None or best_dist > max_lookback:
            continue
        buckets[best_idx].append(w)

    sector_x0 = header.get("sector_x0")
    first_col_end = sector_x0 or header["nav_x0"]
    margin = 12

    rows = []
    for anchor, bucket in zip(anchors, buckets):
        name_words, sector_words = [], []
        for w in bucket:
            if id(w) in anchor_ids:
                continue
            x0 = float(w["x0"])
            if x0 < first_col_end - margin:
                name_words.append(w)
            else:
                sector_words.append(w)

        pct_text = anchor["text"]
        if not pct_text.endswith("%"):
            pct_text = f"{pct_text}%"

        rows.append(
            {
                "top": float(anchor["top"]),
                "company": _row_text(name_words),
                "sector": _row_text(sector_words),
                "pct": pct_text,
            }
        )

    rows.sort(key=lambda r: r["top"])
    return rows


def _raw_portfolio_rows(page) -> list:
    headers = _find_portfolio_headers(page)
    if not headers:
        return []

    grand_top = _find_grand_total_top(page, headers[0]["name_x0"])
    bottom_bound = (grand_top - 1) if grand_top is not None else page.height

    rows = []
    for gi, header in enumerate(headers):
        if gi + 1 < len(headers):
            right_edge = (header["nav_x0"] + headers[gi + 1]["name_x0"]) / 2.0
        else:
            right_edge = page.width
        rows.extend(_rows_for_group(page, header, right_edge, bottom_bound))

    rows.sort(key=lambda r: r["top"])
    return rows


_TOTAL_ROW_RE = re.compile(r"^total$", re.IGNORECASE)
_GRAND_TOTAL_ROW_RE = re.compile(r"^grand\s+total\b", re.IGNORECASE)
_LEADING_ASTERISK_RE = re.compile(r"^\*+\s*")


def _classify_rows(raw_rows: list) -> list:
    """Turn raw (company, sector, pct) rows into clean holding dicts.

    Groww's own bucket-subtotal convention is simply a row whose company
    text is exactly "Total" (equity/debt/TREPs sub-totals) or "Grand
    Total" -- both are dropped. Every other named row with its own
    percentage (including roll-up lines the source itself prints as a
    distinct row, such as "Others" or the "*TREPS/Reverse Repo/Net
    current assets" residual line) is kept as-is, since it represents a
    genuine slice of the portfolio and inventing a different treatment
    for it would be a scheme-specific workaround rather than a general
    rule. A leading footnote "*" marker is stripped for cleanliness.
    """
    holdings = []
    for row in raw_rows:
        company = _LEADING_ASTERISK_RE.sub("", row["company"]).strip()
        if not company:
            continue
        if _TOTAL_ROW_RE.match(company) or _GRAND_TOTAL_ROW_RE.match(company):
            continue
        holdings.append(
            {
                "company": company,
                "sector": row["sector"],
                "pct_to_net_assets": row["pct"],
            }
        )
    return holdings


def extract_holdings(page) -> list:
    return _classify_rows(_raw_portfolio_rows(page))


# --------------------------------------------------------------------------
# Benchmark / additional benchmark
# --------------------------------------------------------------------------

_BENCHMARK_STOP_RE = re.compile(
    r"\b(NOTE|STATISTICAL|PORTFOLIO|NAV\s+OF|FUND\s+MANAGER|MATURITY\s+AND\s+YIELD|"
    r"CREDIT\s+QUALITY|COMPOSITION\s+OF\s+ASSET|RATING\s+PROFILE|BASE\s+EXPENSE|"
    r"ONE\s+YEAR\s+ROLLING|TRACKING\s+ERROR|DATA\s+AS\s+ON)\b",
    re.IGNORECASE,
)


def _extract_benchmark(page):
    """Extract the BENCHMARK panel value.

    The FUND SNAPSHOT panel packs the benchmark text and the (unrelated)
    "Direct/Regular Plan expense ratio" figures into two side-by-side
    sub-columns that share the same visual lines, so a plain top-to-bottom
    text reconstruction would interleave them. Restricting collected
    words to those left of the "Direct"/"Regular Plan" sub-column (found
    dynamically per page, since its x-position shifts slightly with AUM
    column widths) isolates the benchmark's own sub-column; a
    stop-keyword check then trims off the unrelated footnote text that
    sits just below the benchmark value at a similar x-position.
    """
    words = _page_words(page)
    labels = [w for w in words if w["text"] == "BENCHMARK"]
    if not labels:
        return None
    label = min(labels, key=lambda w: float(w["top"]))
    label_top, label_x0 = float(label["top"]), float(label["x0"])

    # The "Direct"/"Regular Plan" *expense-ratio* row that shares the
    # benchmark's lines always sits immediately below the BENCHMARK
    # label (within ~15-20pt on every page seen). A later, unrelated
    # "% Direct % Regular" column header (part of a "One Year Rolling"
    # tracking-error table further down the same panel on ETF/FoF pages)
    # must not be allowed to win the minimum-x0 comparison below, so the
    # search for it is kept to a narrow window rather than the wider
    # window used to collect the benchmark's own text.
    near_window = [
        w for w in words if label_top + 2 < float(w["top"]) <= label_top + 40
    ]
    right_candidates = [
        float(w["x0"])
        for w in near_window
        if w["text"] in ("Direct", "Regular") and float(w["x0"]) > label_x0
    ]
    right_bound = (min(right_candidates) - 5) if right_candidates else (label_x0 + 160)

    window = [w for w in words if label_top + 2 < float(w["top"]) <= label_top + 90]
    value_words = [w for w in window if float(w["x1"]) <= right_bound]
    rows = _cluster_rows(value_words)
    rows.sort(key=lambda r: r["top"])

    lines = []
    for r in rows:
        text = _row_text(r["words"])
        if not text:
            continue
        if _BENCHMARK_STOP_RE.search(text):
            break
        lines.append(text)
        if len(lines) >= 6:
            break

    value = _clean(" ".join(lines))
    return value or None


_ADDITIONAL_BENCHMARK_RE = re.compile(r"\*\*\s*([^.]+)")


def _extract_additional_benchmark(page):
    """Extract the "**<index>" additional-benchmark footnote next to the
    PERFORMANCE table, when the scheme discloses one. Not every scheme
    has an additional benchmark (e.g. newly-launched schemes with no
    performance table yet, or schemes whose Additional Benchmark column
    is shown as "-"), so returning None here is expected and normal.
    """
    text = _page_text(page)
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("*") or stripped.startswith("**"):
            continue
        if "**" not in stripped:
            continue
        m = _ADDITIONAL_BENCHMARK_RE.search(stripped)
        if not m:
            continue
        value = _clean(m.group(1))
        if value:
            return value
    return None


# --------------------------------------------------------------------------
# ISIN (kept for interface/future-proofing; Groww's factsheet does not
# currently print an ISIN anywhere in the document)
# --------------------------------------------------------------------------

_ISIN_RE = re.compile(r"\bISIN\s*[:\-]?\s*([A-Z]{2}[A-Z0-9]{9}\d)\b")


def extract_isin(text: str) -> str:
    if not text:
        return ""
    m = _ISIN_RE.search(text)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# Fund manager(s)
# --------------------------------------------------------------------------

_TITLE_RE = re.compile(r"^(Mr\.|Ms\.|Mrs\.|Dr\.|Smt\.)$")
_LEADING_TITLE_RE = re.compile(r"^(Mr\.|Ms\.|Mrs\.|Dr\.|Smt\.)\s*")

_SLEEVE_PATTERNS = (
    (re.compile(r"debt|fixed\s+income", re.IGNORECASE), "Debt"),
    (re.compile(r"equit", re.IGNORECASE), "Equity"),
    (re.compile(r"overseas", re.IGNORECASE), "Overseas"),
)

_MANAGER_STOP_RE = re.compile(
    r"\b(SECTORAL\s+ALLOCATION|CREDIT\s+QUALITY|COMPOSITION\s+OF\s+ASSET|"
    r"RATING\s+PROFILE|FUND\s+STYLE|PERFORMANCE|MATURITY\s+AND\s+YIELD)\b",
    re.IGNORECASE,
)


def _find_fund_manager_label_top(words):
    for w in words:
        if w["text"] != "FUND":
            continue
        for ow in words:
            if ow["text"] != "MANAGER":
                continue
            if (
                abs(float(ow["top"]) - float(w["top"])) <= 2
                and 0 <= float(ow["x0"]) - float(w["x1"]) <= 10
            ):
                return float(w["top"])
    return None


def _extract_fund_managers(page) -> list:
    """Extract every fund manager listed on this scheme's page.

    Groww lays managers out either stacked vertically (one column, common
    on debt-fund pages with only 1-2 managers) or side-by-side
    horizontally (multiple columns, common on equity pages with 3+
    managers), and the columns can sit as little as ~80pt apart -- too
    close for a fixed per-column x-window to safely hold a full
    "(Managing Fund Since <date>)" / "Total experience - over NN years"
    line without either clipping it or bleeding into the next column.
    Instead, every "Mr./Ms./Mrs./Dr." title token in the block is treated
    as a column reference point, and each physical *line* of the block is
    independently split into word-clusters wherever the horizontal gap
    between consecutive words is large enough to indicate a real column
    boundary (mirroring the same nearest-anchor idea used for portfolio
    rows, applied on the x-axis instead of the y-axis for this block).
    Each cluster is then routed to whichever column reference point it
    sits closest to, and -- for a column with more than one manager
    stacked vertically -- to whichever of that column's own title anchors
    is the nearest one at or above the cluster's own line. This adapts to
    each line's actual content width and correctly handles vertical
    stacking, horizontal columns, or a grid of both.
    """
    words = _page_words(page)
    fm_top = _find_fund_manager_label_top(words)
    if fm_top is None:
        return []

    # Bound the block to the FUND SNAPSHOT panel's own width so the
    # (parallel, same-top-range) PORTFOLIO table on the right of the page
    # is never mistaken for manager text.
    headers = _find_portfolio_headers(page)
    panel_right_edge = (headers[0]["name_x0"] - 10) if headers else 358.0

    block = [
        w
        for w in words
        if fm_top + 2 < float(w["top"]) <= fm_top + 250
        and float(w["x0"]) < panel_right_edge
    ]

    titles = [w for w in block if _TITLE_RE.match(w["text"])]
    if not titles:
        return []

    column_x0s: list = []
    for t in sorted(titles, key=lambda w: float(w["x0"])):
        tx0 = float(t["x0"])
        if not column_x0s or tx0 - column_x0s[-1] > 30:
            column_x0s.append(tx0)

    def _nearest_column(x0):
        return min(column_x0s, key=lambda cx: abs(cx - x0))

    col_anchor_tops: dict = {cx: [] for cx in column_x0s}
    for t in titles:
        col_anchor_tops[_nearest_column(float(t["x0"]))].append(float(t["top"]))
    for cx in col_anchor_tops:
        col_anchor_tops[cx].sort()

    max_manager_block_height = 55.0
    max_column_snap_distance = 60.0
    within_line_gap = 10.0  # see docstring: tighter than any observed
    # intra-phrase word gap (~5pt), looser than any observed inter-column
    # gap on a shared line (~13pt+) in this document's manager panel.

    manager_words: dict = {}
    for row in _cluster_rows(block):
        ws = sorted(row["words"], key=lambda w: float(w["x0"]))
        if not ws:
            continue
        raw_clusters = [[ws[0]]]
        for w in ws[1:]:
            if float(w["x0"]) - float(raw_clusters[-1][-1]["x1"]) > within_line_gap:
                raw_clusters.append([w])
            else:
                raw_clusters[-1].append(w)

        # A cluster can still smuggle two managers together when the gap
        # right before a title token happens to be unusually small on
        # that particular row (observed as low as ~7pt on some title
        # rows, versus ~13pt+ elsewhere) -- but a title token is always
        # an unambiguous column boundary regardless of its gap, so split
        # any cluster at each internal (non-first) title word.
        clusters = []
        for raw in raw_clusters:
            start = 0
            for i in range(1, len(raw)):
                if _TITLE_RE.match(raw[i]["text"]):
                    clusters.append(raw[start:i])
                    start = i
            clusters.append(raw[start:])

        for cluster in clusters:
            if not cluster:
                continue
            cx0 = float(cluster[0]["x0"])
            col_x0 = _nearest_column(cx0)
            if abs(cx0 - col_x0) > max_column_snap_distance:
                continue  # stray text (a chart label, ...), not a manager
            anchors_here = col_anchor_tops.get(col_x0) or []
            candidate = None
            for atop in anchors_here:
                if atop - 3 <= float(row["top"]):
                    candidate = atop
                else:
                    break
            if (
                candidate is None
                or float(row["top"]) - candidate > max_manager_block_height
            ):
                continue
            manager_words.setdefault((col_x0, candidate), []).extend(cluster)

    managers: list = []
    for (_col_x0, _atop), ws in sorted(manager_words.items(), key=lambda kv: kv[0]):
        rows = sorted(_cluster_rows(ws), key=lambda r: r["top"])

        kept_rows = []
        for r in rows:
            guess = " ".join(w["text"] for w in r["words"])
            if _MANAGER_STOP_RE.search(guess):
                break
            kept_rows.append(r)
        if not kept_rows:
            continue

        # Reconstruct the manager's whole text block (name + designation +
        # dates + experience) in one pass and split it at the first "("
        # rather than assuming the name occupies an entire line by itself:
        # the designation sometimes starts on the very same line as the
        # name (e.g. "Mr. X (Fund Manager & Dealer - ...") rather than
        # wrapping to the next one.
        combined = _smart_join_rows(kept_rows)
        # Strip everything up to and including the title token, wherever
        # it falls in the combined text -- not just at the very start.
        # A hyphenated designation word occasionally wraps in a way that
        # places a stray fragment (e.g. a lone "Equity" split off
        # "Research-Equity") immediately before the title on the same
        # visual line in the source PDF; anchoring only at the start of
        # the string would leave that fragment glued onto the name.
        title_match = re.search(r"(Mr\.|Ms\.|Mrs\.|Dr\.|Smt\.)\s*", combined)
        combined = combined[title_match.end() :] if title_match else combined
        # The designation normally starts with "(" (e.g. "(Head-Equity)"),
        # or occasionally a bare "<dash> Designation" before the
        # parenthesised "(Managing Fund Since ...)" clause (e.g. "Paras
        # Matalia - Equity (Managing Fund Since ...)"). Split at whichever
        # marker comes first.
        paren_idx = combined.find("(")
        dash_match = re.search(r"\s[-\u2013\u2014]\s", combined)
        dash_idx = dash_match.start() if dash_match else -1
        split_candidates = [i for i in (paren_idx, dash_idx) if i != -1]
        split_idx = min(split_candidates) if split_candidates else -1
        # Every manager name in this document is two words ("First
        # Last"), so a marker that would leave more than ~3 tokens before
        # it isn't the name/designation boundary -- it's a dash *inside*
        # a designation that is missing its opening "(" entirely (a rare
        # gap in the source PDF itself, not an extraction artefact; e.g.
        # "...Fund Manager & Dealer - Equity)" with no leading "("). Fall
        # back to the two-word name in that case, so the designation text
        # (still useful for sleeve detection) doesn't get absorbed into
        # the name.
        if split_idx != -1 and len(combined[:split_idx].split()) <= 3:
            name, context = combined[:split_idx].strip(), combined[split_idx:]
        else:
            name_tokens = combined.split(" ")
            name, context = " ".join(name_tokens[:2]), " ".join(name_tokens[2:])
        if len(name) < 2:
            continue

        sleeve = None
        for pattern, label_val in _SLEEVE_PATTERNS:
            if pattern.search(context):
                sleeve = label_val
                break

        if any(
            m["name"].lower() == name.lower() and m["sleeve"] == sleeve
            for m in managers
        ):
            continue
        managers.append({"role": "Fund Manager", "name": name, "sleeve": sleeve})

    return managers


# --------------------------------------------------------------------------
# Public: per-scheme field aggregation
# --------------------------------------------------------------------------


def extract_scheme_fields(pdf, page_idxs: list) -> dict:
    benchmark = None
    additional_benchmark = None
    isin = ""
    fund_managers: list = []
    holdings: list = []

    for pi in page_idxs:
        page = pdf.pages[pi]

        if benchmark is None:
            benchmark = _extract_benchmark(page)

        if additional_benchmark is None:
            additional_benchmark = _extract_additional_benchmark(page)

        if not isin:
            found = extract_isin(_page_text(page))
            if found:
                isin = found

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
