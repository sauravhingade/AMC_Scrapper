"""HDFC Mutual Fund factsheet extractor.

HDFC publishes (at least) two monthly factsheet PDFs that share the SAME
underlying page template:
  1. The main "HDFC MF Factsheet" -- actively managed equity, debt, and
     hybrid schemes, plus a handful of FOF schemes.
  2. The "HDFC MF Index Solutions Factsheet" -- index funds, ETFs, and
     G-Sec/SDL index funds.

Both documents use an identical per-scheme page structure: a page banner
("<page> | <Month> <Year>"), the scheme name, a "CATEGORY OF SCHEME" /
"INVESTMENT OBJECTIVE:" block, a left-hand sidebar (NAV, AUM, Quantitative
Data, Expense Ratio, #BENCHMARK INDEX / ##ADDL. BENCHMARK INDEX, FUND
MANAGER, Exit Load) and a PORTFOLIO table occupying the rest of the page.
Because the template is identical, a single unified extractor handles both
documents -- there is no genuine structural fork that would justify
axis.py-style dual-dispatch; the two documents differ only in which scheme
*types* they contain (active vs. passive), and the extraction logic below
already generalizes across that difference (e.g. the holdings table
gracefully degrades from a 3-column Company/Industry/% table to a
2-column Instrument/% table for simple passive schemes).

Output contract: benchmark, additional_benchmark, isin, fund_managers,
holdings, holdings_count.

This module is fully self-contained (no imports from a sibling `config`
module) -- scheme segmentation uses structural anchors present on every
HDFC scheme page ("CATEGORY OF SCHEME" + "INVESTMENT OBJECTIVE:") rather
than a hand-maintained keyword/exclude list, which both removes an
external dependency and fixes false-positive "scheme" detections on
annexure/performance pages that happened to start with a HDFC-ish-looking
line (see segment_schemes docstring).
"""

import collections
import re

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Lowercase and strip every non-alphanumeric character.

    Deliberately more aggressive than a whitespace/hyphen-only strip: real
    section-divider text varies in punctuation in ways that are easy to
    miss when hand-maintaining a lookup set (e.g. "Cash,Cash Equivalents
    and Net Current Assets" has a comma with NO following space -- a
    normalizer that only strips whitespace/hyphens leaves that comma in
    place and silently fails to match a label constant that omits it,
    letting the row leak through as a bogus holding). Stripping down to
    bare alphanumerics sidesteps the whole class of "did I remember the
    exact punctuation" bugs.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def extract_words_fixed(page, y_tolerance: float = 1.5) -> list[dict]:
    """Replacement for page.extract_words() that fixes a real rendering
    quirk in these two HDFC PDFs: certain (bold-weight) text runs render
    their individual glyphs with near-zero or slightly NEGATIVE horizontal
    gaps between consecutive characters (adjacent glyph bounding boxes
    overlap by a fraction of a point -- e.g. "Hindustan" measured as
    'H'[203.69-208.14], 'i'[208.01-209.81], overlapping by ~0.13pt). This
    is completely normal sub-pixel kerning, but pdfplumber's own
    extract_words() treats a negative gap as a hard word break regardless
    of x_tolerance (confirmed empirically: raising x_tolerance from 0.5 to
    3 has zero effect on the split), so words like "Hindustan Aeronautics
    Limited" come out shredded into single-character and tiny 2-3-letter
    fragments ("H", "i", "nd", "u", "s", "t", "an", "Aeronautics",
    "Li", "m", "it", "e", "d") -- garbage for every downstream consumer
    (holdings, fund manager names, benchmark text).

    This rebuilds words directly from page.chars instead, using the
    actual encoded space characters as the primary word-boundary signal
    (these DO reliably mark real word breaks in this document -- confirmed
    across dozens of samples) plus a generous positive-gap threshold as a
    secondary signal for the (rarer) case of two words placed via absolute
    positioning with no literal space character in between (e.g. a name
    column butting up against a numeric column). The threshold is set well
    above the largest observed intra-word overlap/kerning gap (~0.15pt)
    but far below any genuine inter-column gap (which run into double
    digits of points on every table on these pages), so it can't misfire
    either way.

    A related but DISTINCT quirk -- a handful of column headers
    ("Company", "Industry+") occasionally carry a genuinely spurious space
    character injected mid-word -- is deliberately NOT special-cased here.
    A corpus-wide check of ~31,000 space gaps showed real word-separating
    spaces routinely overlap their previous character too (by anywhere
    from a hair to several points -- there's no gap-size threshold that
    cleanly separates "real space" from "spurious mid-word space": the two
    distributions overlap). Trying to filter on gap size here corrupted
    ordinary words throughout the whole document (e.g. "HDFC Bank Ltd."
    losing its spaces to become "HDFCBankLtd."). That header-fragment
    quirk is instead repaired later, narrowly, by _repair_split_headers()
    -- see its docstring for why a targeted fix at the point of use is
    the safer place for it.
    """
    chars = page.chars
    if not chars:
        return []
    lines: dict[float, list[dict]] = {}
    for c in chars:
        y = round(c["top"] / y_tolerance) * y_tolerance
        lines.setdefault(y, []).append(c)

    words: list[dict] = []
    for y in sorted(lines):
        cs = sorted(lines[y], key=lambda c: c["x0"])
        cur: list[dict] = []
        prev_x1 = None
        for c in cs:
            is_space = c["text"] == "" or c["text"].isspace()
            big_gap = prev_x1 is not None and (c["x0"] - prev_x1) > 1.5
            if is_space or big_gap:
                if cur:
                    words.append(cur)
                    cur = []
            if not is_space:
                cur.append(c)
                prev_x1 = c["x1"]
        if cur:
            words.append(cur)

    out = []
    for w in words:
        out.append(
            {
                "text": "".join(c["text"] for c in w),
                "x0": min(c["x0"] for c in w),
                "x1": max(c["x1"] for c in w),
                "top": min(c["top"] for c in w),
                "bottom": max(c["bottom"] for c in w),
            }
        )
    return out


def reconstruct_lines(words: list[dict], y_tolerance: float = 1.5) -> str:
    """Group words into lines by y-position, sort each line left-to-right.

    y_tolerance of 1.5pt (rather than a looser 3.0pt) keeps genuinely
    same-line words together while separating near-miss cases where a
    wrapped name and an unrelated small tag land within a few points of
    each other but are not actually on the same rendered line.
    """
    lines: dict[float, list[dict]] = {}
    for w in words:
        y = round(w["top"] / y_tolerance) * y_tolerance
        lines.setdefault(y, []).append(w)
    return "\n".join(
        " ".join(w["text"] for w in sorted(lines[y], key=lambda w: w["x0"]))
        for y in sorted(lines)
    )


def _words_in_xrange(words: list[dict], x0: float, x1: float) -> list[dict]:
    """Select whole words whose start falls in [x0, x1).

    Deliberately filters by word x0 rather than physically cropping the
    page (page.within_bbox()). Cropping clips glyphs AT the boundary: a
    word that starts inside the desired range but ends past it (very
    common here -- HDFC's long benchmark-index names routinely wrap right
    up against the sidebar's right edge) gets its trailing character(s)
    silently sliced off ("Index" -> "Inde", "Debt" -> "Deb"). Selecting by
    x0 keeps every matched word completely intact; the tradeoff (a word
    that starts just left of the boundary but extends visually into the
    next column) is unimportant here because there is no real content
    overlap at that y-position on these pages -- sidebar text and the
    portfolio table never occupy the same row.
    """
    return [w for w in words if x0 <= w["x0"] < x1]


def get_column_text(page, x0: float, x1: float, y_tolerance: float = 1.5) -> str:
    """Extract clean text from a vertical band of a page without clipping
    glyphs (see _words_in_xrange)."""
    words = _words_in_xrange(extract_words_fixed(page), x0, x1)
    return reconstruct_lines(words, y_tolerance=y_tolerance)


# ---------------------------------------------------------------------------
# Scheme segmentation
# ---------------------------------------------------------------------------

# Every HDFC scheme's FIRST page carries both of these markers, in this
# order, near the top of the page. Continuation pages repeat "CATEGORY OF
# SCHEME" (and a "....Contd from previous/next page" note) but never
# "INVESTMENT OBJECTIVE:" again. This pair is a much more reliable anchor
# than a first-line heuristic + hand-maintained exclude list: it can't
# misfire on annexure/performance-summary pages (e.g. "FUND DETAILS
# ANNEXURE", or a per-manager "HDFC GILT FUND NAV as at ..." performance
# table heading), which carry neither marker.
_CATEGORY_ANCHOR_RE = re.compile(r"CATEGORY\s+OF\s+SCHEME", re.IGNORECASE)
_OBJECTIVE_ANCHOR_RE = re.compile(r"INVESTMENT\s+OBJECTIVE\s*:", re.IGNORECASE)
_CONTD_RE = re.compile(r"Contd\s+(?:from|on)\s+(?:previous|next)\s+page", re.IGNORECASE)
_PAGE_BANNER_RE = re.compile(r"^\d+\s*\|\s*[A-Za-z]+\s+\d{4}\s*$")
_FOR_PRODUCT_LABEL_RE = re.compile(r"^For\s+Product\s+label", re.IGNORECASE)


def _clean_scheme_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip("^~*").strip()


def _extract_scheme_name(text: str) -> str | None:
    """The scheme name is reliably the very FIRST non-blank line of a
    genuine start page's extracted text -- verified across every start
    page in both HDFC documents (100/100 samples checked). It is NOT
    reliably positioned relative to the "<page> | <Month> <Year>"
    page-footer banner: that banner is a page FOOTER, not a header, and
    pdfplumber's reading-order text extraction sometimes places it
    mid-page (interleaved with a donut-chart legend's scrambled text) or
    as the page's very last line, rather than at the top where a naive
    "text right after the banner" heuristic would expect it -- so that
    heuristic either grabs unrelated chart-legend garbage or (when the
    banner is the last line) finds nothing at all.

    Scanning upward from "CATEGORY OF SCHEME" for the nearest non-blank
    line (the previous fallback) is similarly unreliable: the line
    immediately above that anchor is usually the scheme's descriptive
    subtitle ("An open ended scheme replicating ...", bracketed or not),
    not its name -- the real name sits one or more lines further up, at
    the very top of the page, which that fallback never reaches because
    it returns on the FIRST candidate it finds scanning upward.

    Only the page-footer banner line and the "For Product label..."
    footer line are skipped when looking for the first candidate;
    everything else at the top of a genuine start page (confirmed
    empirically across all 100 start pages in both documents) IS the
    scheme name, never a subtitle or banner.
    """
    for line in text.split("\n"):
        candidate = line.strip()
        if not candidate:
            continue
        if _PAGE_BANNER_RE.match(candidate):
            continue
        if _FOR_PRODUCT_LABEL_RE.match(candidate):
            continue
        return _clean_scheme_name(candidate)
    return None


def segment_schemes(pdf) -> dict[str, list[int]]:
    """Returns {scheme_name: [page_index, ...]} in document order.

    A page starts a NEW scheme if it carries both structural anchors. A
    page CONTINUES the current scheme if it repeats "CATEGORY OF SCHEME"
    or an explicit "Contd ..." note (both true of every continuation page
    observed -- portfolio-continuation pages, SIP/performance pages, and
    Industry Allocation pages alike). Any other page resets `current` to
    None, so trailing annexure/performance-summary/riskometer/disclaimer
    pages -- none of which carry either marker -- can never get silently
    glommed onto the last real scheme (the bug that produced fake
    "FUND DETAILS ANNEXURE" / "... PRAVEEN JAIN" scheme entries before).
    """
    scheme_pages: dict[str, list[int]] = {}
    current: str | None = None

    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        has_category = bool(_CATEGORY_ANCHOR_RE.search(text))
        has_objective = bool(_OBJECTIVE_ANCHOR_RE.search(text))

        if has_category and has_objective:
            name = _extract_scheme_name(text)
            if name:
                current = name
                scheme_pages.setdefault(current, [])
                if i not in scheme_pages[current]:
                    scheme_pages[current].append(i)
                continue

        if current is not None and (has_category or _CONTD_RE.search(text)):
            if i not in scheme_pages[current]:
                scheme_pages[current].append(i)
            continue

        current = None

    return scheme_pages


# ---------------------------------------------------------------------------
# Sidebar boundary (dynamic, derived from the portfolio table header)
# ---------------------------------------------------------------------------

# Only used when no scheme page has a detectable Company/Instrument header
# at all (shouldn't normally happen -- every scheme has a portfolio table
# of some shape -- kept as a last-resort fallback).
_DEFAULT_SIDEBAR_WIDTH = 180.0

_TABLE_NAME_HEADER_WORDS = ("Company", "Instrument")


_NAME_HEADER_RE = re.compile(r"^(?:Company|Instrument)(?:/(?:Company|Instrument))?/?$")


def _is_name_header_word(text: str) -> bool:
    """True for any rendering of the portfolio table's name-column header:
    "Company" or "Instrument" alone, "Company/" (the header wraps onto two
    lines as "Company/" + "Instrument" whenever the column is narrow), or
    a single merged token "Company/Instrument" (whenever it isn't).

    Anchored with a full-string regex rather than the previous
    text.startswith(("Company", "Instrument")) check -- that looser check
    also matched ordinary body-text words that merely start with the same
    letters ("Instruments," in a scheme's INVESTMENT OBJECTIVE prose,
    "Instrument)" in an unrelated parenthetical elsewhere on the page),
    which could plant a false header candidate far over at the page's
    left margin and drag the whole sidebar/table boundary down to
    (near-)zero, silently blanking out benchmark/fund-manager extraction
    for that scheme.
    """
    return bool(_NAME_HEADER_RE.match(text.strip()))


def _detect_sidebar_boundary(pdf, page_idxs: list[int]) -> float:
    """Derive the sidebar/portfolio-table boundary from the portfolio
    table's own "Company"/"Instrument" header x-position for this scheme,
    rather than trusting a hardcoded pixel width. Hardcoded boundaries are
    a liability: they silently clip or leak content the moment a scheme's
    layout shifts even slightly.
    """
    for idx in page_idxs:
        page = pdf.pages[idx]
        header_words = [
            w for w in extract_words_fixed(page) if _is_name_header_word(w["text"])
        ]
        if header_words:
            return min(w["x0"] for w in header_words) - 8
    return _DEFAULT_SIDEBAR_WIDTH


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

_GARBAGE_MARKERS = [
    "EXIT LOAD",
    "For Product label",
    "Riskometers",
    "respect of each",
    "TRACKING ERROR",
    "LOCK-IN PERIOD",
    "NET EQUITY EXPOSURE",
    "Debt Index Replication Factor",
    "Scrip Code",
]


def _trim_garbage(value: str) -> str:
    """Safety net for when two sidebar sections merge onto one
    reconstructed line -- truncate at the first known next-section marker
    rather than returning the merged mess."""
    for marker in _GARBAGE_MARKERS:
        idx = value.upper().find(marker.upper())
        if idx != -1:
            value = value[:idx]
    return value.strip()


_STOP = (
    r"(?=\n#|\nEXIT LOAD|\nDATE OF ALLOTMENT|\nNAV\b|\nFor \b"
    r"|\nTRACKING ERROR|\nLOCK-IN PERIOD|\nNET EQUITY EXPOSURE"
    r"|\nDebt Index Replication Factor|\nScrip Code|$)"
)
_BENCH_PRIMARY_RE = re.compile(
    r"#\s*BENCHMARK(?:\s+INDEX)?\s*\n\s*(.+?)" + _STOP, re.DOTALL
)
_BENCH_ADDL_RE = re.compile(
    r"##\s*ADDL\.?\s*BENCHMARK(?:\s+INDEX)?\s*\n\s*(.+?)" + _STOP, re.DOTALL
)


def _clean_bench(raw: str | None) -> str | None:
    if not raw:
        return None
    value = re.sub(r"\s+", " ", raw).strip()
    value = _trim_garbage(value)
    value = _fix_stray_trailing_s(value)
    value = _balance_trailing_parens(value)
    return value or None


def _balance_trailing_parens(text: str) -> str:
    """Restores a missing closing parenthesis right before a sentence's
    trailing period, when the count of "(" exceeds ")" in the text.

    Confirmed on HDFC Developed World Overseas Equity Passive FOF's
    benchmark disclaimer -- "...(Due to time zone difference, benchmark
    performance will be calculated with a day's lag." -- which is missing
    its closing ")" before the final period. This isn't an extraction
    artifact: the character genuinely isn't present in the PDF's text
    layer at that position at all (confirmed at the char level -- the
    only ")" glyph anywhere near "lag" belongs to an unrelated, smaller
    decorative element in a different font sitting nearby, not this
    sentence), even though the page renders visually as if it were there.
    Rather than hardcode this one specific sentence, this applies the
    general, structurally-justified fix of balancing an excess of
    unclosed "(" by inserting the missing ")" -- safe because a genuinely
    unbalanced closing paren from a normal, correctly-formed sentence
    should never occur in this document's benchmark/label text otherwise.
    """
    excess = text.count("(") - text.count(")")
    if excess <= 0:
        return text
    if text.endswith("."):
        return text[:-1].rstrip() + (")" * excess) + "."
    return text.rstrip() + (")" * excess)


def extract_benchmark(sidebar_text: str) -> str | None:
    m = _BENCH_PRIMARY_RE.search(sidebar_text)
    return _clean_bench(m.group(1)) if m else None


def extract_additional_benchmark(sidebar_text: str) -> str | None:
    m = _BENCH_ADDL_RE.search(sidebar_text)
    return _clean_bench(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Fund managers
# ---------------------------------------------------------------------------

_MONTHS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}
_NOISE_WORDS = {
    "over",
    "since",
    "total",
    "exp",
    "name",
    "year",
    "years",
} | _MONTHS


def _is_noise_token(tok: str) -> bool:
    t = tok.strip(",.\u00a5").lower()
    if t in _NOISE_WORDS or t == "":
        return True
    if re.match(r"^\d+[.,]?\d*[,]?$", tok):  # bare numbers, "29,", "2026" etc
        return True
    # Letter-spacing rendering artifact: real name tokens are always
    # capitalized, so a stray lowercase fragment ("ye", "rs") is debris,
    # not part of a name. This also naturally drops connective words that
    # occasionally spill into the name column from a wrapped sleeve tag
    # ("for", "of", "and", "viz.") -- harmless there since those belong
    # to the sleeve text, which is captured separately from the regex
    # match, not from this token stream.
    if tok[0].islower():
        return True
    return False


# Matches ANY parenthetical, not just ones ending in "Portfolio"/"Assets".
# The narrower, suffix-specific version used previously never matched
# genuinely different sleeve phrasing (e.g. "(Dedicated Fund Manager for
# commodities related investments viz. Gold)"), which fell through to the
# no-sleeve algorithm and got shredded into several garbage entries.
_SLEEVE_RE = re.compile(r"\(([A-Za-z][^()]*)\)")


def _merge_paren_wrapped_lines(lines: list[str]) -> list[str]:
    """Rejoins a sleeve tag that wraps across two or more reconstructed
    lines, e.g. "(Arbitrage" / "Assets)" or a long tag spanning 4-5 lines
    ("(Dedicated" / "Fund" / "Manager for" / "commodities related" /
    "investments viz. Gold)"). Tracks a running open-paren balance across
    consecutive lines and only closes out a merged line once it returns to
    (or below) zero, so it copes with a sleeve tag of any length rather
    than assuming a fixed two-line wrap. Without this, matching the sleeve
    regex line-by-line fails whenever the closing paren isn't on the same
    reconstructed line as the opening one, and the accumulating name
    buffer silently swallows the next manager's name too.
    """
    merged: list[str] = []
    buf: str | None = None
    balance = 0
    for line in lines:
        buf = line if buf is None else f"{buf} {line}"
        balance += line.count("(") - line.count(")")
        if balance <= 0:
            merged.append(buf)
            buf = None
            balance = 0
    if buf is not None:
        merged.append(buf)
    return merged


def _is_departed_manager(raw_tokens: list[str]) -> bool:
    """HDFC prefixes an outgoing/departed manager's name with a standalone
    "Ex" marker (e.g. "Ex Chirag Setalvad"). Checked against the RAW
    (pre-noise-filter) tokens, since "ex" is itself filtered as noise --
    by the time a name string is built the marker is already gone, so
    without this check a departed manager would silently be reported as a
    current one.

    Searches the whole buffer rather than just the first token: leftover
    "Since"/"Total Exp" noise from the PREVIOUS manager's row commonly
    lands at the front of the next manager's raw buffer (the y-bucketed
    reconstruction doesn't guarantee those values are consumed exactly
    when the previous manager's sleeve closes), which would push the real
    "Ex" marker away from index 0.
    """
    return any(t.strip(",.\u00a5") == "Ex" for t in raw_tokens)


def _emit_sleeve_manager(
    managers: list[dict], raw_tokens: list[str], sleeve: str | None
) -> None:
    if _is_departed_manager(raw_tokens):
        return
    filtered = [t for t in raw_tokens if not _is_noise_token(t)]
    name = " ".join(filtered).strip()
    if name:
        managers.append({"role": "Fund Manager", "name": name, "sleeve": sleeve})


def _emit_plain_manager(managers: list[dict], raw_lines: list[str]) -> None:
    raw_tokens = " ".join(raw_lines).split()
    if _is_departed_manager(raw_tokens):
        return
    filtered = [t for t in raw_tokens if not _is_noise_token(t)]
    name = " ".join(filtered).strip()
    if name:
        managers.append({"role": "Fund Manager", "name": name, "sleeve": None})


def extract_fund_managers(sidebar_text: str) -> list[dict]:
    """Reconstructs the FUND MANAGER table into {role, name, sleeve} entries.

    Two algorithms depending on whether the block uses sleeve tags at all
    (checked once up front, not per-line, since a scheme either always or
    never uses them):

    - WITH sleeve tags (hybrid/multi-asset schemes): accumulate name
      tokens until a sleeve tag "(Equity Portfolio)" etc. closes out the
      current manager. Sleeve tags that wrap across multiple lines are
      rejoined first (see _merge_paren_wrapped_lines) so the sleeve regex
      always gets a chance to match on a single (merged) line.
    - WITHOUT sleeve tags (plain multi-manager -- most debt funds, all
      index funds/ETFs): each line's noise-stripped residual is either a
      complete 2+-word name (closes immediately) or a lone word that's
      part of a name wrapped across two lines (buffered until 2 words
      accumulate).
    """
    m = re.search(
        r"FUND MANAGER.*?\n(.*?)(?=\n\s*DATE OF ALLOTMENT|\n\s*NAV\b"
        r"|\n\s*ASSETS UNDER MANAGEMENT|\n\s*QUANTITATIVE DATA"
        r"|\n\s*INDEX FACTS|\n\s*Scrip Code|$)",
        sidebar_text,
        re.DOTALL,
    )
    if not m:
        return []
    lines = m.group(1).split("\n")
    if lines and re.match(r"^Name\b.*Sinc", lines[0].strip()):
        lines = lines[1:]
    lines = [l for l in lines if not l.strip().startswith("Scrip Code")]
    lines = [l for l in lines if l.strip() not in {"", "\u00a5"}]

    merged_lines = _merge_paren_wrapped_lines(lines)
    has_sleeve = any(_SLEEVE_RE.search(l) for l in merged_lines)
    managers: list[dict] = []

    if has_sleeve:
        raw_buffer: list[str] = []
        for line in merged_lines:
            sleeve_m = _SLEEVE_RE.search(line)
            if sleeve_m:
                # Any name text on the SAME (merged) line before the
                # sleeve tag (e.g. "Amit Sinha (Equity Portfolio)" all on
                # one line) belongs to this manager too.
                prefix = line[: sleeve_m.start()]
                raw_buffer.extend(prefix.split())
                _emit_sleeve_manager(managers, raw_buffer, sleeve_m.group(1).strip())
                raw_buffer = []
                continue
            raw_buffer.extend(line.split())
        if raw_buffer:
            _emit_sleeve_manager(managers, raw_buffer, None)
    else:
        pending: list[str] = []
        for line in merged_lines:
            filtered = [t for t in line.split() if not _is_noise_token(t)]
            if not filtered:
                continue
            if len(filtered) >= 2:
                if pending:
                    _emit_plain_manager(managers, pending)
                    pending = []
                _emit_plain_manager(managers, [line])
            else:
                pending.append(line)
                combined_filtered = [
                    t for pl in pending for t in pl.split() if not _is_noise_token(t)
                ]
                if len(combined_filtered) >= 2:
                    _emit_plain_manager(managers, pending)
                    pending = []
        if pending:
            _emit_plain_manager(managers, pending)

    return managers


# ---------------------------------------------------------------------------
# ISIN
# ---------------------------------------------------------------------------


def extract_isin(sidebar_text: str) -> str:
    """HDFC's per-scheme sidebar does not print an ISIN for regular
    open-ended schemes or for ETFs/index funds (ETFs print "Scrip Code:
    BSE:.../NSE:..." instead) -- across both factsheets tested, no page
    prints an "ISIN" label at all. Kept as a best-effort match against the
    generic "ISIN : <code>" label format in case a future scheme/print
    layout adds one; correctly returns "" otherwise rather than guessing."""
    m = re.search(r"ISIN\s*:\s*([A-Z0-9]{6,15})", sidebar_text)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------

_NUMERIC_PCT = re.compile(r"^-?\d+\.\d+$")

# Real holdings rows in this document are consistently ~7-9.5pt apart
# vertically (confirmed across every sampled table, including 2-line
# wrapped company names). A gap noticeably larger than that between two
# consecutive candidate rows is the most reliable available signal that
# the holdings table has genuinely ended and whatever comes next (SIP
# performance, returns tables, sidebar boxes) -- which can otherwise
# parse exactly like an ordinary holdings row -- has begun.
_MAX_ROW_GAP = 20.0

# Category-divider / sub-total rows that share the same column shape as a
# real holding but aren't securities. Normalized via _normalize (bare
# alphanumerics, lowercased) so punctuation variance (commas, ampersands,
# slashes, parens) across real-world renderings can't cause a silent
# mismatch.
_CATEGORY_LABELS = {
    "equityequityrelated",
    "equityequityrelatedtotal",
    "reitinvitinstruments",
    "unitsissuedbyreit",
    "unitsissuedbyreitequityotherequityinstrument",
    # The REIT/InvIT divider text routinely wraps across THREE separate
    # physical rows -- "UNITS ISSUED BY REIT" (all caps), then "Units
    # issued by ReIT" (mixed case, a literal repeat of the same line),
    # then "(Equity & other Equity Instrument)" on its own row below that
    # -- rather than appearing as the single already-registered combined
    # label above. Without this fragment registered on its own, that
    # third row wasn't recognized as a divider continuation and bled
    # straight into the FOLLOWING row's company/sector text instead
    # (confirmed across 14 schemes in the main factsheet: every one with
    # a REIT holding had its first REIT company name and/or sector
    # corrupted with "Instrument)" or "(Equity & other Equity" stuck onto
    # it).
    "equityotherequityinstrument",
    "unitsissuedbyreitinvit",
    "unitsissuedbyinvit",
    "stockexchange",
    "debtinstruments",
    "debtdebtrelated",
    "governmentsecurities",
    "governmentsecuritiescentralstate",
    "creditexposure",
    "creditexposurenonperpetual",
    "creditexposureperpetualbonds",
    "certificateofdeposit",
    "commercialpaper",
    "treasurybill",
    "tbills",
    "nonconvertibledebenturesbonds",
    "exchangetradedfunds",
    "subtotal",
    "total",
    "trepsreverserepo",
    "netreceivablespayables",
    "portfoliototal",
    "gold",
    "goldgoldrelated",
    "silver",
    "silversilverrelated",
    "moneymarketinstruments",
    "mutualfundunits",
    "mutualfundunitsequity",
    "mutualfundunitsdebt",
    "mutualfundunitsgold",
    "alternativeinvestmentfundunits",
    "cp",
    "cd",
    "cashcashequivalentsandnetcurrentassets",
    "grandtotal",
}

# Rows that mark the genuine, unambiguous end of a holdings (sub-)table.
# Deliberately just "Grand Total" -- an earlier version also stopped at a
# bare "Total", but real portfolios routinely contain several intermediate
# "Total" rows (e.g. an equity sub-total, then a REIT section, then a
# combined "Total", then a debt/government-securities section, THEN the
# real "Grand Total") and stopping at the first one truncated everything
# after it -- silently dropping REIT and debt/govt-securities holdings
# that make up the tail of many equity schemes' portfolios.
_TABLE_END_LABELS = {"grandtotal"}

# These specific labels are pure bookkeeping rows -- "Sub Total 98.49",
# "Total 100.00" -- that ALWAYS carry a percentage figure right alongside
# them (it's the subtotal's own value), unlike genuine asset-class section
# dividers ("Equity & Equity Related", "Government Securities", "Gold",
# ...) which never do. They must be skipped unconditionally, regardless of
# whether a percentage sits on their row -- unlike the broader
# _CATEGORY_LABELS check below, which only treats a row as a divider when
# it has NO percentage (see is_category_row).
_ALWAYS_SKIP_LABELS = {"subtotal", "total"}

# Company fields that are just a bare corporate suffix are a sign the real
# name got split across a row boundary and lost its first half -- drop
# rather than keep a visibly wrong entry.
_SUSPICIOUS_COMPANY = {
    "ltd",
    "ltd.",
    "limited",
    "inc",
    "inc.",
    "co",
    "co.",
    "company",
    "corp",
    "corp.",
    "total",
    "subtotal",
}

# Trailing footnote-marker glyphs HDFC appends directly to a company name
# with no separating space (e.g. "HDFC Bank Ltd.£" for the sponsor,
# "Anthem Biosciences Limited¥"). Stripped for a clean company name; the
# footnote's meaning (sponsor/less-than-threshold/etc.) isn't part of the
# security's identity.
_FOOTNOTE_MARKERS_RE = re.compile(r"[\u00a3\u00a5@\u2020\u2021\^*]+$")


def _strip_footnote_markers(name: str) -> str:
    return _FOOTNOTE_MARKERS_RE.sub("", name).strip()


# A rare but real rendering quirk in these PDFs inserts a stray space
# right before a trailing "s" -- present even in pdfplumber's own plain
# extract_text() (so it isn't an artifact of this module's word-building),
# e.g. "Automobile s" for "Automobiles", "Ferrou s" for "Ferrous", "Indu
# s" for "Indus" (a bank name). Confirmed via corpus scan: always a
# genuine single word plus a wrongly-detached final "s", never a real
# standalone word "s" -- safe to fold back onto the previous word
# wherever it appears in extracted text.
_STRAY_TRAILING_S_RE = re.compile(r"(?<=[A-Za-z])\s+s\b")


def _fix_stray_trailing_s(text: str) -> str:
    return _STRAY_TRAILING_S_RE.sub("s", text)


def _dedupe_repeated_phrase(text: str) -> str:
    """Collapses a whole phrase that's been rendered TWICE back down to
    one copy.

    Specific to a handful of debt-fund schemes (confirmed: HDFC Credit
    Risk Debt Fund) whose portfolio table has an unusual FOUR-column
    header -- "Instrument | Industry+/Security Rating | Instrument Rating
    | % to NAV" -- where the middle two columns show the exact same
    rating value side by side (e.g. "CRISIL - AAA" under both). Both
    columns fall inside this row-builder's single sector-text collection
    window (there being no signal in the header to tell it there are
    really two rating sub-columns, not one), so the value gets
    concatenated with itself.

    The two copies' individual WORDS can end up interleaved in more than
    one order depending on exactly how the two columns' x-positions
    happen to fall relative to each other -- all three confirmed present:
      - "CRISIL - AAA CRISIL - AAA" (each column's full phrase
        consecutive: first half of tokens equals the second half);
      - "Transport Transport Infrastructure Infrastructure" (columns'
        respective words alternate one-for-one: token[2i] == token[2i+1]
        for every i);
      - "CRISIL - CRISIL - AAA(SO) AAA(SO)" (a mixed order matching
        neither of the above cleanly).

    Rather than enumerate every possible interleaving, this uses one
    general rule that covers all three (and any other ordering the same
    root cause might produce): if EVERY distinct token in the string
    appears in it EXACTLY TWICE, it's collapsed to one copy of each
    token, kept in first-seen order. This is safe against real,
    non-duplicated dual-agency ratings like "CRISIL - AAA / ICRA - AAA"
    (a bond genuinely rated by two different agencies) because those have
    tokens appearing only ONCE each ("CRISIL", "/", "ICRA" all singular)
    even though "AAA" and "-" happen to repeat -- the ALL-tokens-exactly-
    twice condition only ever fires on a true duplicate.
    """
    tokens = text.split()
    if len(tokens) < 2:
        return text
    counts = collections.Counter(tokens)
    if any(n != 2 for n in counts.values()):
        return text
    seen: set[str] = set()
    deduped = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return " ".join(deduped)


def _label_matches(norm: str, labels: set) -> bool:
    """True if `norm` (an already-_normalize()d row text) IS one of
    `labels`, or is one of them with extra trailing digits stuck on. The
    trailing-digit case handles a wide right-aligned percentage value
    (most commonly "100.00", one digit wider than the ordinary "98.49"
    style figures a column boundary is normally sized for) landing just
    far enough left to get swept into the row's "company" text instead of
    parsed as its own percentage token -- e.g. "Grand Total 100.00"
    normalizes to "grandtotal10000", not the clean "grandtotal" a plain
    equality check expects.
    """
    if norm in labels:
        return True
    return any(
        norm.startswith(label) and norm[len(label) :].isdigit()
        for label in labels
        if label
    )


def _rows_to_holdings(
    words_in_range: list[dict], sector_start: float, pct_start: float
) -> list[dict]:
    """State machine turning one sub-table's words into holdings.

    Handles two ways a naive read goes wrong: category-divider rows (same
    column shape as a real holding, no percentage -- skipped outright), and
    company names wrapping across lines (accumulated until a percentage
    value appears, at which point the sector text collected so far is
    attached and the holding closes).

    When sector_start == pct_start (the 2-column "Instrument / % to NAV"
    layout used by Gold/Silver ETFs and simple fund-of-funds, which have
    no Industry/Rating column at all), the sector slice is always empty by
    construction and every row correctly gets sector="".
    """
    rows: dict[float, list[dict]] = {}
    for w in words_in_range:
        rows.setdefault(round(w["top"], 1), []).append(w)

    holdings: list[dict] = []
    company_buf: list[str] = []
    sector_buf: list[str] = []
    state = "idle"
    stop = False
    last_row_top: float | None = None

    def close(pct: str):
        nonlocal company_buf, sector_buf, stop
        company = _strip_footnote_markers(" ".join(company_buf).strip())
        company = _fix_stray_trailing_s(company)
        sector = _fix_stray_trailing_s(" ".join(sector_buf).strip())
        sector = _dedupe_repeated_phrase(sector)
        normalized = _normalize(company)
        if _label_matches(normalized, _TABLE_END_LABELS):
            stop = True
        # A category-label-shaped name is excluded here UNLESS it's one
        # that's always excluded regardless (_ALWAYS_SKIP_LABELS) -- a row
        # only reaches close() with `pct` set at all when the row-level
        # check already decided it's a genuine holding, not a divider (see
        # is_category_row below), so re-filtering unconditionally on the
        # same label set here would undo that decision right back to
        # dropping single-commodity schemes' one real "Gold."/"Silver"
        # holding.
        excluded = normalized in _CATEGORY_LABELS and (
            not pct or _label_matches(normalized, _ALWAYS_SKIP_LABELS)
        )
        if company and pct and not excluded and normalized not in _SUSPICIOUS_COMPANY:
            holdings.append(
                {"company": company, "sector": sector, "pct_to_net_assets": pct}
            )
        company_buf, sector_buf = [], []

    for y in sorted(rows):
        if stop:
            break
        if last_row_top is not None and (y - last_row_top) > _MAX_ROW_GAP:
            # A jump this large between two consecutive candidate rows
            # (real holdings rows in this document are consistently
            # ~7-9.5pt apart, even across a wrapped 2-line company name)
            # means we've left the holdings table entirely and wandered
            # into whatever comes next in this same x-range further down
            # the page -- almost always the SIP-performance or
            # performance-returns table below it, since those tables'
            # rows happen to parse exactly like holdings rows (a name-like
            # phrase followed by a percentage figure). This matters most
            # for asymmetric side-by-side sub-tables, where one column has
            # noticeably more real rows than the other and thus has no
            # "Grand Total" (or any other terminator) of its own to stop
            # at -- confirmed via HDFC BSE Sensex Index Fund, whose longer
            # left sub-table has 25 real holdings then jumps straight into
            # "Total Amount Invested" 30pt further down with nothing
            # in between to signal the table has ended.
            break
        last_row_top = y
        row = sorted(rows[y], key=lambda w: w["x0"])
        co = [
            w["text"] for w in row if w["x0"] < sector_start and w["text"] != "\u2022"
        ]
        sec = [w["text"] for w in row if sector_start <= w["x0"] < pct_start]
        pct = None
        for w in row:
            if pct_start <= w["x0"] and _NUMERIC_PCT.match(w["text"]):
                pct = w["text"]
                break  # leftmost numeric match in the bounded pct column
        if pct is None and co and _NUMERIC_PCT.match(co[-1]):
            # A wide, right-aligned percentage value can start just far
            # enough left of pct_start to land one column short and get
            # swept into "co" instead of recognized as its own token here
            # -- e.g. "100.00" starting at x0=353.71 against a pct_start
            # of 354.11, a 0.4pt miss (confirmed on HDFC NIFTY 1D RATE
            # LIQUID ETF's sole "Cash, Cash Equivalents and Net Current
            # Assets 100.00" row, and earlier on "Grand Total 100.00"
            # elsewhere) -- always because the value itself ("100.00",
            # "-8.18") is a touch wider than whatever pct_start was
            # originally sized against. Recovered here rather than by
            # further widening pct_start's margin, since that's a
            # never-ending game of chasing the next value that's wide
            # enough to repeat the problem; the true, general signal is
            # simply that the LAST token in "co" independently looks
            # exactly like a percentage figure.
            pct = co.pop()
        co_text = " ".join(co).strip()
        sec_text = " ".join(sec).strip()
        co_norm = _normalize(co_text)
        # A multi-line divider phrase (e.g. the REIT/InvIT section's
        # "Units issued by ReIT (Equity & other Equity Instrument)") can
        # land with its closing word(s) pushed past sector_start and
        # counted as "sec" rather than "co" purely because of where that
        # boundary happens to fall relative to the divider's own text --
        # confirmed: "... other Equity" (x0 up to 415.3) sits under
        # sector_start=422.26 while its own closing "Instrument)" (x0
        # 435.9) doesn't, splitting one continuous divider phrase across
        # co_text and sec_text on the very same row. Matching co_text
        # alone against the known divider labels misses this split form
        # entirely, so the combined "co_text sec_text" is checked too.
        combined_norm = _normalize(f"{co_text} {sec_text}")
        # A row with a percentage value attached is NEVER a bare
        # asset-class category divider, even when its name text happens to
        # normalize to one -- this matters for single-commodity schemes
        # (Gold/Silver ETFs) whose one real holding is literally named
        # "Gold." / "Silver", which normalizes identically to the "Gold &
        # Gold RELATED" section's own bare "Gold" category-divider row.
        # Without this distinction, that real (and only) holding row gets
        # silently treated as a second category divider and dropped,
        # leaving the scheme's entire holdings table empty.
        #
        # "Sub Total" / "Total" rows are the one exception: unlike a plain
        # section-divider row, they ALWAYS carry a genuine percentage
        # right alongside them (the subtotal's own value) -- so for THESE
        # specific labels the percentage can't be used to distinguish a
        # divider from a real holding, and they must be skipped
        # regardless of it (_ALWAYS_SKIP_LABELS).
        #
        # The REIT/InvIT divider ("Units issued by ReIT (Equity & other
        # Equity Instrument)") in particular keeps fragmenting at a
        # DIFFERENT point depending on the specific sub-table's column
        # widths (which shift scheme-to-scheme whenever an extra hedge/
        # derivative percentage column is present, shrinking how much of
        # the divider's tail lands in "sec" before running into pct_start)
        # -- confirmed truncating after "...other Equity" on one scheme
        # and after "...Equity Instrument)" was still incomplete on
        # another. Rather than registering yet another exact fragment
        # every time a new truncation point turns up, this recognizes
        # ANY combined text that starts with this divider's own
        # unmistakable opening ("units issued by re/invit...") as a
        # divider, no matter how much of its tail got cut off -- no real
        # holding name could ever coincidentally start with that phrase.
        is_reit_invit_divider_prefix = combined_norm.startswith(
            ("unitsissuedbyreit", "unitsissuedbyinvit")
        )
        is_category_row = (
            bool(co_text)
            and (
                _label_matches(co_norm, _CATEGORY_LABELS)
                or _label_matches(combined_norm, _CATEGORY_LABELS)
                or is_reit_invit_divider_prefix
            )
            and (
                not pct
                or _label_matches(co_norm, _ALWAYS_SKIP_LABELS)
                or _label_matches(combined_norm, _ALWAYS_SKIP_LABELS)
            )
        )

        # "Grand Total" is this table's definitive, unconditional end --
        # regardless of whether a percentage happens to parse on its row
        # (it normally does: "Grand Total 100.00"). Matched with
        # _label_matches rather than plain equality because a WIDE
        # right-aligned percentage value ("100.00" -- one digit wider than
        # the "98.49"-style values the pct column boundary was sized for)
        # can start just far enough left to land on the wrong side of that
        # boundary and get swept into the row's "company" text instead of
        # recognized as its own percentage token, leaving co_text as
        # "Grand Total 100.00" (normalizing to "grandtotal10000") rather
        # than a clean "Grand Total". _label_matches tolerates exactly
        # that kind of trailing-digit contamination.
        if co_text and _label_matches(co_norm, _TABLE_END_LABELS):
            break  # holdings table ends here; don't read into tables below it

        if state == "idle":
            if is_category_row:
                continue
            if co_text:
                company_buf = [co_text]
                sector_buf = [sec_text] if sec_text else []
                if pct:
                    close(pct)
                else:
                    state = "in_progress"
        else:  # in_progress
            if is_category_row:
                company_buf, sector_buf = [], []
                state = "idle"
            elif co_text:
                # Always append rather than resetting company_buf just
                # because sector_buf already has something in it. That
                # reset was meant to recover from an incomplete entry
                # (almost always a category label slipping past the
                # label check) being followed by a genuinely new one, but
                # it also fires on a full 3-line wrap where company AND
                # sector text interleave across lines (confirmed on "Sun
                # Pharmaceutical" / "Industries Ltd." / "Pharmaceuticals
                # &" / "Biotechnology" -- company text continues on line
                # 2 even though sector text already started on line 1),
                # silently discarding the first line of the company name.
                # A real, unclosed category label reaching here without
                # being caught is rare and, if it slips through, the
                # worst outcome is one merged name rather than a
                # truncated one -- a smaller loss than routinely
                # shredding legitimate multi-line company names.
                company_buf.append(co_text)
                if sec_text:
                    sector_buf.append(sec_text)
                if pct:
                    close(pct)
                    state = "idle"
            elif sec_text:
                sector_buf.append(sec_text)
                if pct:
                    close(pct)
                    state = "idle"
            elif pct:
                close(pct)
                state = "idle"

    return holdings


def _repair_split_headers(words: list[dict]) -> list[dict]:
    """Repairs a narrow but real rendering quirk: on some pages, the
    column header words "Company" and "Industry+" carry one genuinely
    spurious space character injected mid-word (e.g. "Company" rendering
    as chars 'C','o','m','p','a', a stray space, 'n','y'), which
    extract_words_fixed's normal space-splitting logic -- correctly,
    for ordinary text -- turns into two words: "Compa" + "ny",
    "Indu" + "stry+". Left unrepaired, _find_header_words can't recognize
    the shredded fragment as a real "Company"/"Industry+" header, and a
    scheme's second (or later) side-by-side sub-table silently vanishes
    entirely -- confirmed on HDFC Nifty 50 Index Fund, which lost its
    entire right-hand column (12 of 50 holdings, including a negative
    cash adjustment) this way.

    This is deliberately fixed HERE -- as a narrow, targeted merge of two
    specific known fragments back into their known whole words -- rather
    than by loosening extract_words_fixed's general space-handling rules.
    A corpus-wide check found no gap-size threshold that reliably tells
    this spurious space apart from a genuine one (real word-separating
    spaces overlap their previous character by anywhere from a hair to
    several points too), so any general fix there ends up corrupting
    ordinary running text instead (e.g. "HDFC Bank Ltd." losing its own
    spaces). Fixing it at the two known call sites avoids that entirely.
    """
    repaired: list[dict] = []
    i = 0
    while i < len(words):
        w = words[i]
        if i + 1 < len(words):
            nxt = words[i + 1]
            merged_text = w["text"] + nxt["text"]
            same_line = abs(w["top"] - nxt["top"]) < 1
            # Allows a small negative gap too (down to -2pt) -- these two
            # fragments can themselves overlap slightly where the
            # original spurious space briefly interrupted them (confirmed
            # gap of -0.03pt between "Indu" and "stry+"), the same kind of
            # sub-pixel kerning artifact seen throughout this document.
            close_enough = -2 <= nxt["x0"] - w["x1"] < 5
            if same_line and close_enough and merged_text in ("Company", "Industry+"):
                repaired.append(
                    {
                        "text": merged_text,
                        "x0": w["x0"],
                        "x1": nxt["x1"],
                        "top": min(w["top"], nxt["top"]),
                        "bottom": max(w["bottom"], nxt["bottom"]),
                    }
                )
                i += 2
                continue
        repaired.append(w)
        i += 1
    return repaired


def _find_header_words(words: list[dict]) -> list[dict]:
    """Company/Instrument header words that sit near an Industry/Rating
    header (the standard 3+-column layout). Searches within a Y-WINDOW
    rather than requiring near-exact y-equality, because on schemes with
    an extra "% exposure of Derivative" column the combined header text
    ("Company/Instrument", "Industry+/Rating", "% to NAV (Hedged &
    Unhedged)", "% exposure of Derivative") is long enough to wrap across
    2-3 lines, and the individual header words can end up several points
    apart in y even though they belong to the same logical header row."""
    words = _repair_split_headers(words)
    # Matched against "Industry+" (with its trailing "+" sigil) rather
    # than a bare "Industry" prefix -- the word "Industry" on its own
    # (no "+") also appears as ordinary prose in this document's
    # boilerplate footer text ("... + Industry Classification as
    # recommended by AMFI ..."), which sits close enough in y-position to
    # a genuine "Instrument" header on several fund-of-funds pages to get
    # mistaken for a real sector/industry column header, sending those
    # schemes down the wrong (3-column) extraction path and corrupting
    # every holding with interleaved disclaimer text. "Rating" has no
    # such false-positive source in this document, so it's left as a
    # bare prefix match.
    industry_tops = [
        w["top"]
        for w in words
        if w["text"].startswith("Industry+") or w["text"].startswith("Rating")
    ]
    if not industry_tops:
        return []

    def _is_instrument_rating_subheader(candidate: dict) -> bool:
        """True if `candidate` is really the "Instrument" half of an
        "Instrument Rating" TWO-WORD column title (a second, narrower
        rating-like column some actively-managed debt funds print
        alongside the ordinary "Industry+/Security Rating" one -- e.g.
        HDFC Credit Risk Debt Fund's header reads "Instrument Industry+/
        Instrument % to / Security Rating Rating NAV", wrapping "Instrument
        Rating" across two lines exactly like a genuine "Instrument"
        header wraps against its own sector column), rather than the
        table's actual name-column header.

        Both uses of the word "Instrument" match _is_name_header_word
        identically -- nothing about the word itself distinguishes them.
        What does: a GENUINE "Instrument"/"Company" header is always the
        FIRST word of its header row, with nothing directly beneath it in
        the header block (its own sub-label, if any, is the sector
        column's "Industry+"/"Rating" token, positioned well to its
        right, not directly under it). This false one has "Rating"
        sitting almost directly below it (within a few points of x and
        y) -- confirmed via the actual coordinates: fake candidate
        "Instrument" at (x0=315.55, top=120.86) has "Rating" at
        (x0=321.67, top=127.82), a scant 6pt right and 7pt down, whereas
        the real "Instrument" header at (x0=201.89, top=120.86) has
        nothing at all in that same small neighborhood.
        """
        return any(
            w["text"].strip() == "Rating"
            and abs(w["x0"] - candidate["x0"]) < 15
            and 0 < (w["top"] - candidate["top"]) < 15
            for w in words
        )

    return [
        w
        for w in words
        if _is_name_header_word(w["text"])
        and any(abs(w["top"] - t) < 20 for t in industry_tops)
        and not _is_instrument_rating_subheader(w)
    ]


def _extract_sector_subtables(words: list[dict]) -> list[dict]:
    """Standard path: one or more side-by-side Company|Industry|% to NAV
    sub-tables (HDFC commonly prints two such sub-tables side by side on
    wide equity-fund pages to fit more rows per page)."""
    header_words = _find_header_words(words)
    if not header_words:
        return []

    starts = sorted({w["x0"] for w in header_words})
    all_holdings: list[dict] = []
    for i, cs in enumerate(starts):
        next_start = (
            starts[i + 1]
            if i + 1 < len(starts)
            else max((w["x0"] for w in words), default=cs) + 999
        )
        candidate_words = [w for w in words if cs - 5 <= w["x0"] < next_start - 5]
        sector_hdr = [
            w
            for w in candidate_words
            if w["text"].startswith("Industry+") or w["text"].startswith("Rating")
        ]
        if not sector_hdr:
            continue
        header_top = sector_hdr[0]["top"]
        sector_start = min(w["x0"] for w in sector_hdr)

        # A scheme with hedged/derivative exposure prints a SECOND numeric
        # column ("% exposure of Derivative") after "% to NAV". Take the
        # LEFTMOST "%" header token as the boundary for the column we
        # actually want (% to net assets), and bound the right edge at
        # the SECOND "%" token (if any) so the row scan never wanders
        # into the derivative-exposure figures.
        pct_hdrs = sorted(
            (
                w
                for w in candidate_words
                if w["text"].strip() == "%" and abs(w["top"] - header_top) < 40
            ),
            key=lambda w: w["x0"],
        )
        if pct_hdrs:
            pct_start = pct_hdrs[0]["x0"] - 5
            right_edge = pct_hdrs[1]["x0"] - 5 if len(pct_hdrs) > 1 else pct_start + 55
        else:
            pct_start = cs + 140
            right_edge = pct_start + 55
        # The fallback "+55" guess (used whenever there's no second "%"
        # header to bound against) must never be allowed to reach past
        # the NEXT sub-table's own starting column -- on narrower
        # sub-tables the two are close enough together (observed: only
        # ~35pt apart) that the guess bleeds straight into the next
        # column's territory, picking up stray words (most damagingly, a
        # neighboring sub-table's own "Grand"/"Total" tokens landing in
        # this column's percentage-search zone without being recognized
        # as a stop signal, since they don't land in ITS company/sector
        # slice either) that silently suppress this column's own,
        # correctly-positioned stop condition.
        right_edge = min(right_edge, next_start - 5)

        sub_words = [w for w in candidate_words if w["x0"] < right_edge]
        body_top = max(header_top, sector_hdr[0]["top"])
        body = [w for w in sub_words if w["top"] > body_top + 5]
        all_holdings.extend(_rows_to_holdings(body, sector_start, pct_start))

    return all_holdings


def _extract_simple_instrument_pct_table(words: list[dict]) -> list[dict]:
    """Fallback for the 2-column "Instrument / % to NAV" layout used by
    Gold/Silver ETFs and several fund-of-funds, which have no
    Industry/Rating column at all. Without this fallback these schemes'
    (single- or few-line) holdings tables were skipped outright and
    surfaced as an empty holdings list.

    Handles multiple side-by-side instances of this same 2-column layout
    on one page (confirmed present: a wide fund-of-funds page can print
    two "Instrument / %" sub-tables next to each other, exactly like the
    3-column sector layout does) -- an earlier version only ever looked
    at the single leftmost "Instrument" header, silently dropping every
    holding in a second, further-right sub-table.
    """
    header_candidates = sorted(
        (w for w in words if _is_name_header_word(w["text"])),
        key=lambda w: w["x0"],
    )
    if not header_candidates:
        return []

    all_holdings: list[dict] = []
    for i, header in enumerate(header_candidates):
        # The next sub-table's header (if any) bounds this one's right
        # edge, mirroring _extract_sector_subtables' same-purpose logic.
        next_header_x0 = (
            header_candidates[i + 1]["x0"]
            if i + 1 < len(header_candidates)
            else float("inf")
        )
        table_left = header["x0"] - 5

        # The "%" (of a wrapped "% to NAV" header) routinely renders on
        # its OWN line a few points ABOVE the "Instrument" line, not
        # level with or below it (observed: "%" top=127.8 vs
        # "Instrument" top=131.7, a ~4pt gap) -- so the window must reach
        # comfortably above header["top"], not just a token 2pt of slack,
        # or the header search below misses the "%" token entirely and
        # falls back to a guessed (usually wrong) pct_start.
        below_header = [
            w
            for w in words
            if header["top"] - 10 <= w["top"] <= header["top"] + 25
            and table_left <= w["x0"] < next_header_x0 - 5
        ]
        pct_hdr_candidates = [w for w in below_header if w["text"].strip() == "%"]
        if pct_hdr_candidates:
            pct_start = min(w["x0"] for w in pct_hdr_candidates) - 5
            header_bottom = max(
                header["top"], max(w["top"] for w in pct_hdr_candidates)
            )
        else:
            pct_start = header["x0"] + 150
            header_bottom = header["top"]
        # A fixed +60pt margin turned out too generous on pages where a
        # right-side annexure/disclaimer text block ("Face Value /
        # Allotment NAV...", "Please refer Minimum Application
        # Amount...") sits in the same y-range as the holdings rows,
        # starting only ~30pt right of the "%" header (observed:
        # pct_start=356.9, sidebar text starts at x0=386.6) -- a +60
        # margin swallowed that sidebar text straight into the holdings
        # rows, corrupting every company name in the table. The widest
        # realistic value in this column, "100.00", only extends to
        # ~19pt past pct_start (confirmed: x1=375.8 for pct_start=356.9)
        # -- +25 comfortably covers every real percentage figure with
        # margin to spare while staying clear of that sidebar text. Also
        # capped at the next sub-table's header, when there is one.
        right_edge = min(pct_start + 25, next_header_x0 - 5)

        body = [
            w
            for w in words
            if table_left <= w["x0"] < right_edge and w["top"] > header_bottom + 5
        ]
        all_holdings.extend(
            _rows_to_holdings(body, sector_start=pct_start, pct_start=pct_start)
        )
    return all_holdings


def extract_holdings(page) -> list[dict]:
    """Finds every holdings sub-table on the page and extracts each as its
    own state machine. Tries the standard Company/Industry/% layout
    first; falls back to the simpler 2-column Instrument/% layout (no
    Industry/Rating column) if the standard layout isn't present. Returns
    [] only if genuinely neither layout is found on the page.
    """
    # Left bound intentionally very low (not the ~180 a first scheme page's
    # sidebar would suggest): on CONTINUATION pages that have no sidebar of
    # their own (fund-manager/NAV/benchmark boxes only ever print once, on
    # a scheme's first page), the portfolio table can legitimately start
    # much further left -- confirmed on HDFC Liquid Fund's page 2, where
    # its final "Sub Total"/"Cash, Cash Equivalents"/"Grand Total" rows
    # render at x0~33-80, well inside what would normally be sidebar
    # territory. A fixed 180pt cutoff silently dropped that entire
    # row -- including the fund's -8.18% cash/repo adjustment, which is
    # exactly why its total looked ~8pt over 100%. The header- and
    # column-position-based boundary logic below already keeps genuine
    # sidebar text out on pages that DO have one, so this low a bound
    # costs nothing there.
    words = _words_in_xrange(extract_words_fixed(page), 30, float("inf"))
    # Repaired once, up front, so every downstream consumer -- sector-hdr
    # detection inside _extract_sector_subtables, the simple 2-column
    # fallback, _find_header_words -- sees the same fixed-up "Company"/
    # "Industry+" tokens. Repairing only inside _find_header_words missed
    # _extract_sector_subtables's OWN separate "Industry+"/"Rating" search
    # over the raw word list, which still saw the shredded "Indu"+"stry+"
    # fragments and concluded (wrongly) that a real second sub-table had
    # no sector column at all -- silently dropping that entire sub-table.
    words = _repair_split_headers(words)

    holdings = _extract_sector_subtables(words)
    if not holdings:
        holdings = _extract_simple_instrument_pct_table(words)

    # Defensive final filter: on rare, unusually laid-out pages (seen on
    # one or two fund-of-funds pages with several stacked asset-class
    # sub-tables and no single common end-of-table marker for the state
    # machine to latch onto) the "close a holding on this row's
    # percentage" logic can occasionally run past the real table and
    # swallow the SIP/PERFORMANCE tables below it into one company_buf,
    # before finally closing on some unrelated stray percentage further
    # down the page. That failure mode is unmistakable -- the resulting
    # "company" text is many times longer than any real security or
    # underlying-fund name in this document (the longest genuine one,
    # "HDFC CRISIL-IBX Financial Services 3-6 Months Debt Index Fund -
    # Direct Plan - Growth Option", is under 100 characters) -- so it's
    # dropped here as a safety net rather than surfaced as a fake holding.
    return [h for h in holdings if len(h["company"]) <= 120]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
    """Runs all field extractors for one scheme's page group."""
    if not page_idxs:
        return {
            "benchmark": None,
            "additional_benchmark": None,
            "isin": "",
            "fund_managers": [],
            "holdings": [],
            "holdings_count": 0,
        }

    sidebar_boundary = _detect_sidebar_boundary(pdf, page_idxs)
    first_page = pdf.pages[page_idxs[0]]
    sidebar_text = get_column_text(first_page, 0, sidebar_boundary)

    holdings: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for idx in page_idxs:
        page = pdf.pages[idx]
        # Holdings tables on large portfolios (100+ constituent index
        # funds, or big multi-page active equity funds) continue across
        # several pages via "....Contd on/from ... page" -- every page in
        # the scheme's group is scanned and accumulated, not just the
        # first page that has any holdings at all.
        for holding in extract_holdings(page):
            key = (holding["company"], holding["sector"], holding["pct_to_net_assets"])
            if key in seen:
                continue
            seen.add(key)
            holdings.append(holding)

    return {
        "benchmark": extract_benchmark(sidebar_text),
        "additional_benchmark": extract_additional_benchmark(sidebar_text),
        "isin": extract_isin(sidebar_text),
        "fund_managers": extract_fund_managers(sidebar_text),
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
