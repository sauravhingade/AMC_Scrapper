"""
HDFC Mutual Fund extractor.

Layout looks superficially like 360 ONE's (narrow left sidebar + wide
holdings table) but every label format is different, so none of 360 ONE's
regexes match here. Tested against HDFC MF's June 2026 factsheet (140
pages, 53 detected scheme headings, 49 real schemes after excluding false
positives) -- benchmark + fund manager extraction came back clean on all 49.

Known layout quirks (found via testing, not guessed):

1. Sidebar column width: HDFC's sidebar text wraps close enough to a 152pt
   (360 ONE's) or even 165pt boundary that within_bbox() clips words
   mid-character right at the edge -- e.g. "(TRI)" becomes "(TRI". Widening
   to 180pt gives enough clearance that this stopped happening across every
   tested page. Do NOT widen further without re-testing: at 200pt the crop
   starts pulling in the holdings table's letter-spaced justified-text
   headers, which corrupts fund-manager name extraction in a different way
   (see point 2).

2. Some sidebar text on some pages is rendered with wide letter-spacing --
   single characters extracted as separate "words" ("B E N C H MARK" instead
   of "BENCHMARK"). This showed up when the crop boundary was too wide
   (>=200pt) and pulled in nearby justified-text headers. Staying at 180pt
   avoided the zone where this happens across all tested schemes, but if a
   future HDFC factsheet reintroduces it, don't try to fix it by adjusting
   x_tolerance globally (tested -- doesn't help, and risks merging genuinely
   separate words elsewhere). Better to widen the STOP boundary in the
   regexes below, or normalize the specific corrupted label text before
   matching.

3. No colon after "#BENCHMARK INDEX" / "##ADDL. BENCHMARK INDEX" -- label
   and value are on separate lines. The "#"/"##" are footnote markers, not
   decoration to strip -- they're part of how the two benchmark fields are
   told apart.

4. Fund manager info is a real "Name / Since / Total Exp" table, not
   inline "Fund Manager: Mr. X" text, and single/multi manager schemes
   render inconsistently once reconstructed into lines (column order
   varies, and a two-word surname can get split across lines that also
   contain a different column's date fragment). Handled by stripping known
   non-name tokens (months, "Over", "years", pure numbers) from each line
   and accumulating what's left until a sleeve tag "(X Portfolio)" /
   "(X Assets)" closes out one manager -- this correctly reassembles a name
   even when a wrapped surname landed on its own line.

TODO -- NOT YET IMPLEMENTED: holdings table extraction. HDFC lays holdings
out as two side-by-side sub-columns on the page; pdfplumber's
extract_tables() fragments this into ~20 partial tables instead of one
clean one (tested, confirmed broken -- not just untested). Needs a
word-position-based row reconstruction (similar technique to the sidebar
extraction here) rather than relying on ruling-line table detection.
Deliberately left returning [] rather than shipping something fragile --
this correctly surfaces as `holdings_table_not_found` in review_reasons
instead of silently producing wrong data.
"""

import re
from ..pdf_reader import get_column_text

SIDEBAR_WIDTH = 180

_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}
# "ex" excluded because HDFC prefixes an outgoing/departed manager's name
# with "Ex" (e.g. "¥ Ex Chirag Setalvad") -- found on schemes that recently
# changed fund managers. "¥" is a footnote marker, also noise.
_NOISE_WORDS = {"over", "since", "total", "exp", "name", "year", "years", "ex"} | _MONTHS


def _is_noise_token(tok: str) -> bool:
    t = tok.strip(",.\u00a5").lower()
    if t in _NOISE_WORDS or t == "":
        return True
    if re.match(r"^\d+[.,]?\d*[,]?$", tok):  # bare numbers, "29,", "2026" etc
        return True
    return False


_SLEEVE_RE = re.compile(r"\(([A-Za-z][A-Za-z\s]*?(?:Portfolio|Assets))\)")


def extract_fund_managers(sidebar_text: str) -> list[dict]:
    """Reconstructs the FUND MANAGER table into {role, name, sleeve} entries.

    Strategy: isolate the block between "FUND MANAGER" and the next known
    header, drop the "Name / Since / Total Exp" column-header line, then
    walk line by line -- strip non-name tokens (dates, "Over N years") from
    each line and accumulate what's left. A line containing a sleeve tag
    "(Equity Portfolio)" etc. closes out the current manager (join the
    accumulated name tokens, pair with that sleeve) and resets the buffer.
    If the block ends with no sleeve tag ever appearing, it's a
    single-manager scheme -- emit whatever's left in the buffer as one
    manager with sleeve=None.
    """
    m = re.search(
        r"FUND MANAGER.*?\n(.*?)(?=\n\s*DATE OF ALLOTMENT|\n\s*NAV\b|\n\s*ASSETS UNDER MANAGEMENT|$)",
        sidebar_text,
        re.DOTALL,
    )
    if not m:
        return []
    lines = m.group(1).split("\n")
    if lines and re.match(r"^Name\b.*Sinc", lines[0].strip()):
        lines = lines[1:]

    managers: list[dict] = []
    buffer: list[str] = []
    for line in lines:
        sleeve_m = _SLEEVE_RE.search(line)
        if sleeve_m:
            name = " ".join(buffer).strip()
            if name:
                managers.append({"role": "Fund Manager", "name": name, "sleeve": sleeve_m.group(1).strip()})
            buffer = []
            continue
        buffer.extend(t for t in line.split() if not _is_noise_token(t))

    leftover = " ".join(buffer).strip()
    if leftover:
        managers.append({"role": "Fund Manager", "name": leftover, "sleeve": None})
    return managers


_GARBAGE_MARKERS = ["EXIT LOAD", "For Product label", "Riskometers", "respect of each"]


def _trim_garbage(value: str) -> str:
    """Safety net for when two sidebar sections merge onto one reconstructed
    line (found on a page where a benchmark value and the following EXIT
    LOAD section collided) -- truncate at the first known next-section
    marker rather than returning the merged mess."""
    for marker in _GARBAGE_MARKERS:
        idx = value.upper().find(marker.upper())
        if idx != -1:
            value = value[:idx]
    return value.strip()


_STOP = r"(?=\n#|\nEXIT LOAD|\nDATE OF ALLOTMENT|\nNAV\b|\nFor \b|$)"
_BENCH_PRIMARY_RE = re.compile(r"#BENCHMARK(?:\s+INDEX)?\s*\n\s*(.+?)" + _STOP, re.DOTALL)
_BENCH_ADDL_RE = re.compile(r"##\s*ADDL\.?\s*BENCHMARK(?:\s+INDEX)?\s*\n\s*(.+?)" + _STOP, re.DOTALL)


def _clean_bench(raw: str | None) -> str | None:
    if not raw:
        return None
    value = re.sub(r"\s+", " ", raw).strip()
    value = _trim_garbage(value)
    return value or None


def extract_benchmark(sidebar_text: str) -> str | None:
    m = _BENCH_PRIMARY_RE.search(sidebar_text)
    return _clean_bench(m.group(1)) if m else None


def extract_additional_benchmark(sidebar_text: str) -> str | None:
    m = _BENCH_ADDL_RE.search(sidebar_text)
    return _clean_bench(m.group(1)) if m else None


def extract_isin(sidebar_text: str) -> str:
    """Same label format as 360 ONE's ("ISIN : <code>"), kept as a
    best-effort default -- UNTESTED against real HDFC ISIN data, since the
    June 2026 factsheet used for testing covers only open-ended equity/
    hybrid schemes (none of which carry an ISIN). Revisit once an HDFC
    factsheet with ETF schemes is available to test against."""
    m = re.search(r"ISIN\s*:\s*([A-Z0-9]{6,15})", sidebar_text)
    return m.group(1).strip() if m else ""


def extract_holdings(page, table_x0: float) -> list[dict]:
    """Not yet implemented -- see module docstring. Returns [] so callers
    get a clear holdings_table_not_found review flag instead of silently
    wrong data."""
    return []


def extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
    """Runs all field extractors for one scheme's page group."""
    first_page = pdf.pages[page_idxs[0]]
    sidebar_text = get_column_text(first_page, 0, SIDEBAR_WIDTH)

    holdings = []
    for idx in page_idxs:
        page = pdf.pages[idx]
        page_holdings = extract_holdings(page, SIDEBAR_WIDTH)
        holdings.extend(page_holdings)
        if page_holdings:
            break

    return {
        "benchmark": extract_benchmark(sidebar_text),
        "additional_benchmark": extract_additional_benchmark(sidebar_text),
        "isin": extract_isin(sidebar_text),
        "fund_managers": extract_fund_managers(sidebar_text),
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
