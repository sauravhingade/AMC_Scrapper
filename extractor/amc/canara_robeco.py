"""
Canara Robeco Mutual Fund factsheet extractor.

Mirrors the calling contract of amc/bandhan.py (segment_schemes /
extract_scheme_fields, identical return schema) but every bit of the
internal parsing is specific to how Canara Robeco lays out its
factsheet:

  * One scheme per page (Fund Information panel on the left, a
    "PORTFOLIO" table on the right, one or two side-by-side columns).
  * Equity-style tables carry a "Market Cap" (L/M/S) sub-column and
    group holdings under an industry header row (e.g. "Banks 27.09%").
  * Debt-style tables carry a "Rating" sub-column (e.g. "AAA(CRISIL)",
    "Sovereign", "A1+(CRISIL)") instead.
  * Hybrid schemes show both a Rating column (populated only on debt
    rows) and a Market Cap column (populated only on equity rows) in
    the same table.
  * "Additional benchmark" is not printed next to the scheme's own
    Fund Information panel; it only appears many pages later, in the
    "Performance for all Schemes" section, keyed by scheme name.

Nothing here is wired to a specific page number, month, or scheme
name -- everything is derived from on-page text/coordinates so the
same code keeps working on next month's factsheet.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter

from ..config import HEADING_EXCLUDE, SCHEME_KEYWORDS

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
    """Drop trailing footnote markers such as '*', '^^', '$' from a heading."""
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


def _norm_key(text):
    """Loose normalisation for matching scheme names across sections."""
    text = text.upper()
    text = re.sub(r"\(FORMERLY[^)]*\)", " ", text)
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
    if not upper.startswith("CANARA ROBECO"):
        return False
    if any(ex in upper for ex in HEADING_EXCLUDE):
        return False
    return True


def _clean_scheme_name(line):
    name = _strip_trailing_footnote_symbols(line.strip())
    return _clean(name)


_NON_SCHEME_SECTION_RE = re.compile(
    r"^(?:Performance for all Schemes|Scheme Performance|SIP Performance|"
    r"Income Distribution|How to Read|Glossary|Disclaimers?|"
    r"Snapshot of|Economic Indicators|Equity Market Review|Debt Market Review)",
    re.IGNORECASE,
)


def segment_schemes(pdf):
    """Return {scheme_name: [page_index, ...]} in document order.

    Each Canara Robeco scheme starts with a "CANARA ROBECO <NAME>"
    heading as the very first line of its page, carrying a Fund
    Information panel and a PORTFOLIO table. A scheme can in principle
    spill onto a following page (e.g. a very long portfolio list); such
    a continuation page won't repeat the heading, but it will still
    have its own Name/Rating/Market-Cap/% of NAV table header, which is
    what we key off of rather than any month/page-specific text.
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
# left-column ("Fund Information") metadata extraction
# --------------------------------------------------------------------------


def _find_portfolio_headers(page):
    """Locate one or two "Name | [Rating] | [Market Cap] | % of/to NAV" header
    groups on a scheme page, returning left->right x0 boundaries for each
    sub-column so holdings rows can be sliced out by coordinate."""
    words = _page_words(page)
    port_tops = sorted(float(w["top"]) for w in words if w["text"] == "PORTFOLIO")
    if not port_tops:
        return []
    band_top = port_tops[0]
    band = [w for w in words if band_top <= float(w["top"]) <= band_top + 40]

    name_positions = [
        (float(w["top"]), float(w["x0"])) for w in band if w["text"] == "Name"
    ]
    rating_positions = [
        (float(w["top"]), float(w["x0"]))
        for w in band
        if w["text"] in ("Rating", "RATING")
    ]

    nav_positions = []
    for w in band:
        if w["text"] != "NAV":
            continue
        for ow in band:
            if ow["text"] not in ("of", "to"):
                continue
            if abs(ow["top"] - w["top"]) <= 2 and 0 < w["x0"] - ow["x1"] <= 8:
                nav_positions.append((float(w["top"]), float(w["x0"])))
                break

    mcap_positions = []
    for w in band:
        if w["text"] != "Market":
            continue
        for ow in band:
            if not ow["text"].startswith("Cap"):
                continue
            same_row_adjacent = (
                abs(ow["top"] - w["top"]) <= 2 and 0 <= ow["x0"] - w["x1"] <= 8
            )
            stacked_aligned = (
                2 < abs(ow["top"] - w["top"]) <= 8 and abs(ow["x0"] - w["x0"]) <= 15
            )
            if same_row_adjacent or stacked_aligned:
                mcap_positions.append((float(w["top"]), float(w["x0"])))
                break

    name_positions.sort(key=lambda p: p[1])
    nav_positions.sort(key=lambda p: p[1])

    headers = []
    for ntop, nx0 in name_positions:
        cands = [p for p in nav_positions if p[1] > nx0]
        if not cands:
            continue
        nav_x0 = min(cands, key=lambda p: p[1])[1]
        sector_x0 = None
        for rt, rx in rating_positions:
            if nx0 < rx < nav_x0 and abs(rt - ntop) <= 10:
                sector_x0 = rx
                break
        mcap_x0 = None
        for mt, mx in mcap_positions:
            if nx0 < mx < nav_x0 and abs(mt - ntop) <= 10:
                mcap_x0 = mx
                break
        headers.append(
            {
                "top": ntop,
                "name_x0": nx0,
                "sector_x0": sector_x0,
                "mcap_x0": mcap_x0,
                "nav_x0": nav_x0,
            }
        )
    headers.sort(key=lambda h: h["name_x0"])
    return headers


def _metadata_text(page, headers):
    """Reconstruct the left "Fund Information" column as plain text, bounded
    to the left of wherever the portfolio table starts."""
    if headers:
        boundary = min(h["name_x0"] for h in headers) - 8
    else:
        boundary = 216
    words = [w for w in _page_words(page) if float(w["x1"]) <= boundary]
    rows = _cluster_rows(words, y_tol=1.6)
    rows.sort(key=lambda r: r["top"])
    lines = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        lines.append(" ".join(w["text"] for w in ws))
    return "\n".join(lines)


_BENCHMARK_RE = re.compile(
    r"\bBENCHMARK\s*:\s*(.+?)(?=\n\s*(?:ASSET ALLOCATION|MINIMUM INVESTMENT|EXIT LOAD|"
    r"Month end Assets|FUND MANAGER|NAV\s*:)|\Z)",
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
    if not metadata_text:
        return ""
    m = _ISIN_RE.search(metadata_text)
    return m.group(1) if m else ""


_MANAGER_TITLE_RE = re.compile(
    r"\b(?:Mr|Ms|Mrs|Dr)\.\s*([A-Za-z][A-Za-z.]*(?:\s+[A-Za-z][A-Za-z.]*){0,4})"
)
_MANAGER_BLOCK_RE = re.compile(
    r"\bFUND\s+MANAGER\s*:\s*(.+?)(?=\n\s*(?:DATE OF ALLOTMENT|BENCHMARK|Month end Assets|"
    r"ASSET ALLOCATION|MINIMUM INVESTMENT|NAV\s*:)|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_SLEEVE_PATTERNS = (
    (re.compile(r"debt\s+portfolio", re.IGNORECASE), "Debt"),
    (re.compile(r"equity\s+portfolio", re.IGNORECASE), "Equity"),
    (re.compile(r"overseas\s+investment", re.IGNORECASE), "Overseas"),
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
        # trim a stray trailing "Fund"/"and" fragment picked up from prose
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
# additional-benchmark lookup (from the separate "Performance for all
# Schemes" section, matched back to each scheme by name)
# --------------------------------------------------------------------------

_STOP_WORDS = {
    "Scheme",
    "Period",
    "Returns",
    "(%)",
    "Value",
    "of",
    "Standard",
    "Investment",
    "Current",
    "`10,000/-",
}


def _walk_backward(pool, anchor_x0, max_gap=14):
    words = []
    prev_x0 = anchor_x0
    for pw in reversed(pool):
        text = pw["text"]
        if text in _STOP_WORDS or text.endswith(","):
            break
        if text.endswith("#") and not text.endswith("##"):
            break
        if _PCT_RE.match(text):
            break
        if prev_x0 - pw["x1"] > max_gap:
            break
        words.insert(0, text)
        prev_x0 = pw["x0"]
    return words


def _cluster_rows(words, y_tol=1.5):
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


def _additional_benchmarks_on_page(page):
    """Scan one "Performance for all Schemes" page and return
    {normalized_scheme_name: [additional_benchmark_candidate, ...]}."""
    words = _page_words(page)
    rows = _cluster_rows(words)
    rows.sort(key=lambda r: r["top"])

    heading_tops = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w["x0"])
        line = " ".join(w["text"] for w in ws)
        if line.upper().startswith("CANARA ROBECO"):
            heading_tops.append((r["top"], line.strip()))
    heading_tops.sort()
    if not heading_tops:
        return {}

    results = {}
    for ridx, r in enumerate(rows):
        ws = sorted(r["words"], key=lambda w: w["x0"])
        for i, w in enumerate(ws):
            text = w["text"]
            if not text.endswith("##") or "benchmark" in text.lower():
                continue

            same_row_prefix = list(ws[:i])
            wide_pool, narrow_pool = [], []
            for back in range(1, 3):
                if ridx - back < 0:
                    break
                pr = rows[ridx - back]
                if r["top"] - pr["top"] > 10:
                    break
                wide_m = sorted(
                    (
                        pw
                        for pw in pr["words"]
                        if pw["text"] not in _STOP_WORDS
                        and w["x0"] - 150 <= pw["x0"] <= w["x0"] + 60
                    ),
                    key=lambda pw: pw["x0"],
                )
                narrow_m = sorted(
                    (
                        pw
                        for pw in pr["words"]
                        if pw["text"] not in _STOP_WORDS
                        and w["x0"] - 60 <= pw["x0"] <= w["x0"] + 60
                    ),
                    key=lambda pw: pw["x0"],
                )
                wide_pool = wide_m + wide_pool
                narrow_pool = narrow_m + narrow_pool

            name_words = _walk_backward(wide_pool + same_row_prefix, w["x0"]) + [
                text[:-2]
            ]
            name = " ".join(name_words).strip()
            letters = re.sub(r"[^A-Za-z]", "", name)
            if len(letters) < 6:
                alt_words = _walk_backward(narrow_pool, w["x0"]) + [text[:-2]]
                alt = " ".join(alt_words).strip()
                if len(re.sub(r"[^A-Za-z]", "", alt)) >= 4:
                    name = alt
            name = _clean(name)
            if not name:
                continue

            cand = [h for h in heading_tops if h[0] <= r["top"]]
            if not cand:
                continue
            heading_key = _norm_key(cand[-1][1])
            results.setdefault(heading_key, []).append(name)
    return results


def _build_additional_benchmark_map(pdf):
    combined = {}
    for page in pdf.pages:
        text = page.extract_text() or ""
        if "Period" not in text or "Scheme" not in text or "##" not in text:
            continue
        if not re.search(r"CANARA ROBECO", text, re.IGNORECASE):
            continue
        page_results = _additional_benchmarks_on_page(page)
        for key, values in page_results.items():
            combined.setdefault(key, []).extend(values)

    resolved = {}
    for key, values in combined.items():
        counts = Counter(values)
        resolved[key] = counts.most_common(1)[0][0]
    return resolved


def _get_additional_benchmark_map(pdf):
    cache = getattr(pdf, "_canara_robeco_addl_benchmark_cache", None)
    if cache is not None:
        return cache
    cache = _build_additional_benchmark_map(pdf)
    try:
        pdf._canara_robeco_addl_benchmark_cache = cache
    except Exception:
        pass
    return cache


# --------------------------------------------------------------------------
# portfolio / holdings extraction
# --------------------------------------------------------------------------

# Recognised top-level asset-class rows. These are pure roll-ups: they
# carry their own "% of NAV" figure but the constituent instruments are
# itemised in the rows beneath them, so they are never emitted as
# holdings themselves.
_EQUITY_SECTION_LABELS = {
    "equities",
    "listed / awaiting listing on stock exchange",
    "listed awaiting listing on stock exchange",
}
_OTHER_TOP_LEVEL_BUCKETS = {
    "debt instruments",
    "government securities",
    "alternative investment fund",
    "money market instruments",
    "exchange traded fund",
    "exchange traded funds",
    "preference shares",
    "preference share",
}
_ALL_TOP_LEVEL_BUCKETS = _EQUITY_SECTION_LABELS | _OTHER_TOP_LEVEL_BUCKETS

# Word-tokenised bucket phrases, used to recognise a bucket header that
# wraps across two (or more) lines with its "% of NAV" figure sitting
# between the lines rather than after the last one -- which makes the
# trailing fragment (e.g. a lone "Exchange", or "on Stock Exchange")
# land on the *next* table row instead of its own. See
# `_consume_pending_bucket_words` below.
_BUCKET_WORD_LISTS = [phrase.split() for phrase in _ALL_TOP_LEVEL_BUCKETS]


def _partial_bucket_prefix(words_lower):
    """If `words_lower` is a proper, word-for-word prefix of some bucket
    phrase, return the remaining (still expected) words of that phrase."""
    for bucket_words in _BUCKET_WORD_LISTS:
        if (
            len(words_lower) < len(bucket_words)
            and bucket_words[: len(words_lower)] == words_lower
        ):
            return bucket_words[len(words_lower) :]
    return None


_STOP_ROW_RE = re.compile(r"^grand\s+total\b", re.IGNORECASE)

# A handful of schemes (e.g. Conservative Hybrid Fund) print a Rating
# column but no Market Cap column at all -- so an equity holding row
# there is *also* blank in both sub-columns, exactly like an industry
# header row. When that happens we fall back to a simple shape test:
# actual instrument names almost always end in a corporate/legal
# suffix (Ltd, Bank, ...), while GICS-style industry group labels
# ("Banks", "Finance", "Healthcare Services") never do.
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:Ltd\.?|Limited|Inc\.?|Bank|PLC|LLP|Corp\.?|Corporation|Fund|Trust|Co\.?|"
    r"Shares|Bonds?|Sovereign)$",
    re.IGNORECASE,
)


def _bucket_key_in(name_lower, bucket_set):
    """True if any known bucket phrase occurs in this (possibly polluted
    by a stray line-wrap) row label."""
    for bucket in bucket_set:
        if bucket in name_lower:
            return bucket
    return None


def _compute_bottom_bound(page, header_top):
    words = _page_words(page)
    tops = [
        float(w["top"])
        for w in words
        if w["top"] > header_top + 5
        and (
            w["text"] in ("RISKOMETER", "MARKET", "CAPITALIZATION", "RATING", "|")
            # the bulleted "Top" that starts the "Top Ten Holdings" /
            # "Top Holdings" legend beneath every portfolio table; no
            # real instrument name in this factsheet starts with a
            # bulleted "Top", so this is unambiguous.
            or _PUA_RE.sub("", w["text"]) == "Top"
        )
    ]
    return min(tops) if tops else float(page.height)


def _rows_for_group(page, header, right_edge, bottom_bound):
    words = [
        w
        for w in _page_words(page)
        if header["top"] - 3 <= float(w["top"]) < bottom_bound - 0.5
        and header["name_x0"] - 15 <= float(w["x0"]) < right_edge
    ]
    words = [w for w in words if float(w["top"]) > header["top"] + 3]

    anchor_start = header.get("sector_x0") or header.get("mcap_x0") or header["name_x0"]
    anchors = sorted(
        (
            w
            for w in words
            if _PCT_RE.match(w["text"]) and float(w["x0"]) >= anchor_start - 5
        ),
        key=lambda w: float(w["top"]),
    )
    if not anchors:
        return []
    anchor_tops = [float(a["top"]) for a in anchors]

    # A row's own name can wrap across two (or occasionally three) lines,
    # and its "% of NAV" figure is typographically centred on that
    # wrapped block rather than pinned to its last line -- so the
    # closest anchor by |top - anchor_top|, not the next one at/after a
    # word's own top, is what correctly reunites a wrapped name with its
    # own value instead of bleeding into the row above or below.
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

    sector_x0 = header.get("sector_x0")
    mcap_x0 = header.get("mcap_x0")
    first_col_end = sector_x0 or mcap_x0 or header["nav_x0"]

    # Sub-column *data* (rating strings, market-cap letters) routinely
    # starts a handful of points to the left of where the header *label*
    # ("Rating", "Market") itself sits -- observed 3-9px for Rating and
    # similar for Market Cap across every scheme page. A tight margin
    # here left the rating text (e.g. "AAA(CRISIL)") on the wrong side
    # of the boundary and merged into the company name instead of the
    # sector field. There is a wide (30px+) gap between the longest
    # wrapped company name and where any sub-column data starts, so a
    # generous margin is safe.
    margin = 12

    rows = []
    for anchor, bucket in zip(anchors, buckets):
        name_words, sector_words, mcap_words = [], [], []
        for w in bucket:
            if w is anchor:
                continue
            x0 = float(w["x0"])
            if x0 < first_col_end - margin:
                name_words.append(w)
            elif sector_x0 and (not mcap_x0 or x0 < mcap_x0 - margin):
                sector_words.append(w)
            elif mcap_x0:
                mcap_words.append(w)
            else:
                sector_words.append(w)

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
                "rating": _join(sector_words),
                "mcap": _join(mcap_words),
                "pct": anchor["text"],
            }
        )
    rows.sort(key=lambda r: r["top"])
    return rows


def _raw_portfolio_rows(page):
    """All raw table rows on this page, left group before right group,
    each top-to-bottom -- i.e. correct reading order for a two-column
    continuation layout."""
    headers = _find_portfolio_headers(page)
    if not headers:
        return []
    bottom_bound = _compute_bottom_bound(page, headers[0]["top"])

    all_rows = []
    for gi, header in enumerate(headers):
        if gi + 1 < len(headers):
            right_edge = (header["nav_x0"] + headers[gi + 1]["name_x0"]) / 2
        else:
            right_edge = page.width
        rows = _rows_for_group(page, header, right_edge, bottom_bound)
        for row in rows:
            if _STOP_ROW_RE.match(row["company"]):
                break
            all_rows.append(row)
    return all_rows


def _classify_rows(raw_rows, mcap_column_present):
    """Walk the raw rows top-to-bottom, tracking which asset-class
    section / equity-industry group we're in, and emit clean holdings."""
    holdings = []
    current_section = None  # "equity" | "other" | None (not yet seen)
    current_industry = ""
    pending_words = []  # remaining expected words of a bucket phrase
    # that wrapped across lines and hasn't been fully consumed yet

    for row in raw_rows:
        company = row["company"]
        if not company:
            continue
        pct = row["pct"]
        mcap = row["mcap"]
        rating = row["rating"]

        if not mcap and not rating and pending_words:
            words = company.split()
            consumed = 0
            for expected, actual in zip(pending_words, words):
                if expected == actual.lower():
                    consumed += 1
                else:
                    break
            words = words[consumed:]
            pending_words = pending_words[consumed:] if consumed else []
            company = " ".join(words)
            if not company:
                continue

        if mcap:
            # equity holding -- Market Cap column populated (L/M/S)
            holdings.append(
                {
                    "company": company,
                    "sector": current_industry,
                    "pct_to_net_assets": pct,
                }
            )
            continue

        if rating:
            # debt / money-market holding -- Rating column populated
            holdings.append(
                {"company": company, "sector": rating, "pct_to_net_assets": pct}
            )
            continue

        # Both sub-columns blank: either a roll-up bucket / industry
        # header (skip), or a terminal leaf allocation with no
        # rating/market-cap classification (TREPS, Treasury Bills,
        # Net Current Assets, CDMDF units, ...).
        name_lower = company.lower()
        bucket = _bucket_key_in(name_lower, _ALL_TOP_LEVEL_BUCKETS)
        if bucket:
            current_section = "equity" if bucket in _EQUITY_SECTION_LABELS else "other"
            continue

        partial = _partial_bucket_prefix(name_lower.split())
        if partial is not None:
            pending_words = partial
            continue

        if current_section == "equity":
            if not mcap_column_present and _COMPANY_SUFFIX_RE.search(company):
                # No Market Cap column exists on this page at all, so an
                # actual equity holding is indistinguishable from an
                # industry header by column alone -- fall back to a
                # name-shape check instead of treating it as a header.
                holdings.append(
                    {
                        "company": company,
                        "sector": current_industry,
                        "pct_to_net_assets": pct,
                    }
                )
                continue
            current_industry = company
            continue

        holdings.append({"company": company, "sector": "", "pct_to_net_assets": pct})

    return holdings


def extract_holdings(page):
    headers = _find_portfolio_headers(page)
    mcap_column_present = any(h.get("mcap_x0") for h in headers)
    raw_rows = _raw_portfolio_rows(page)
    return _classify_rows(raw_rows, mcap_column_present)


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
        headers = _find_portfolio_headers(page)
        metadata_text = _metadata_text(page, headers)

        if scheme_name is None:
            text = page.extract_text() or ""
            first_line = (text.split("\n") or [""])[0]
            if _is_scheme_heading(first_line):
                scheme_name = _clean_scheme_name(first_line)

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
