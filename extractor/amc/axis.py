"""Axis Mutual Fund factsheet extractor.

Axis publishes TWO separate monthly PDFs under the same AMC name:
  1. The main "Axis Fund Factsheet" -- actively managed equity, debt, and
     hybrid schemes (plus a few ETF/Index/FOF schemes also reappear here).
     Dense, single-page-per-scheme layout with a real portfolio table
     (Instrument/Issuer Name, Industry or Rating, one-to-three % columns)
     and severe text-flow interleaving between side-by-side metadata boxes.
  2. The "Axis Passive Factsheet" -- index funds, debt index funds, ETFs,
     and fund-of-funds. Also one page per scheme, simpler Top-10-style
     holdings tables, with the same box-interleaving problem but no
     multi-column portfolio table.

Since both are called through the same `amc_name -> extractor module`
lookup, `segment_schemes`/`extract_scheme_fields` here detect which of the
two documents `pdf` actually is (from page-1 content) and dispatch
internally to the matching implementation. Detection re-runs on every call
rather than caching on the pdf object, since it's cheap, deterministic, and
avoids any risk of stale state across separate `extract_factsheet_data`
invocations that happen to reuse a pdfplumber object.

Output contract (both documents): benchmark, additional_benchmark, isin,
fund_managers, holdings, holdings_count.
"""

import re

# ---------------------------------------------------------------------------
# Document detection
# ---------------------------------------------------------------------------


def _is_passive_doc(pdf) -> bool:
    """Distinguish the two documents by which per-scheme section markers
    appear. The Passive factsheet's scheme pages always carry "Scheme
    Details:" and "Investment Objective:"; the Active factsheet's scheme
    pages always carry "Portfolio Snapshot" instead. (Marketing-blurb text
    like "Passive investment solutions" on the cover page turned out to be
    unreliable -- it can get truncated by the same box-interleaving that
    affects the rest of these documents -- so this checks the same
    structural markers each document's own segment_schemes relies on,
    voting across a window of pages rather than trusting any single one.)
    """
    passive_votes = 0
    active_votes = 0
    for page in pdf.pages[:20]:
        text = page.extract_text() or ""
        if re.search(r"Portfolio\s+Snapshot", text, re.IGNORECASE):
            active_votes += 1
        elif re.search(r"Scheme\s+Details\s*:", text, re.IGNORECASE) and re.search(
            r"Investment\s+Objective\s*:", text, re.IGNORECASE
        ):
            passive_votes += 1
        if passive_votes >= 2 or active_votes >= 2:
            break
    return passive_votes > active_votes


def segment_schemes(pdf) -> dict[str, list[int]]:
    if _is_passive_doc(pdf):
        return _passive_segment_schemes(pdf)
    return _active_segment_schemes(pdf)


def extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
    if _is_passive_doc(pdf):
        return _passive_extract_scheme_fields(pdf, page_idxs)
    return _active_extract_scheme_fields(pdf, page_idxs)


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


# ===========================================================================
# PASSIVE FACTSHEET (index funds, debt index funds, ETFs, FOFs)
# ===========================================================================
#
# One page per scheme. Each page is two side-by-side boxes -- a left box
# (Investment Objective, Type of Scheme, Fund Manager, Index Facts/Debt
# Quants, Top 10 Holdings/Portfolio Holdings, Quantitative Data) and a right
# box (Scheme Details incl. Benchmark/ISIN, Total Expense Ratio, Market Cap,
# Sectoral Allocation chart, Net Asset Value). pdfplumber's default text
# extraction interleaves the two boxes by vertical position, and a
# donut-chart legend scatters its own text into the same row band as the
# Holdings table -- both need the same fix: splitting page words at a fixed
# x-coordinate boundary before reconstructing lines. That boundary was
# derived empirically from this template (genuine left-box content never
# exceeds x0=230; genuine right-box content, including chart legends, never
# starts below x0=266), so a threshold of 250 reliably separates them and
# should hold steady across months since it comes from the page template,
# not organic content flow.

_PASSIVE_LEFT_RIGHT_SPLIT_X = 250

_PASSIVE_TITLE_BANNER_RE = re.compile(
    r"\s*MONTHLY\s+FACTSHEET\s*-\s*[A-Za-z]+\s+\d{1,2}\s*,?\s*\d{4}", re.IGNORECASE
)
_PASSIVE_TITLE_PAREN_RE = re.compile(r"^\(.*\)$")
_PASSIVE_TITLE_STOP_RE = re.compile(
    r"^(?:Scheme\s+Details:|Investment\s+Objective:)", re.IGNORECASE
)

_PASSIVE_HOLDINGS_TABLE_HEADER_RE = re.compile(
    r"^(?:Top\s*10\s*Holdings|Portfolio\s*Holdings)\s*:", re.IGNORECASE
)
_PASSIVE_HOLDINGS_ROW_RE = re.compile(r"^(.+?)\s+(-?\d+(?:\.\d+)?)\s*%\s*$")
_PASSIVE_HOLDINGS_STOP_RE = re.compile(
    r"^(?:Net\s+Asset\s+Value|Quantitative\s+Data|Tracking\s+Error|"
    r"The\s+DIRF\s+score|This\s+product\s+is\s+suitable|\*Note\s*:)",
    re.IGNORECASE,
)

_PASSIVE_MANAGER_NAME_RE = re.compile(r"^(Mr\.|Ms\.|Mrs\.|Dr\.)\s+(.+)$")
_PASSIVE_MANAGER_EXPERIENCE_RE = re.compile(r"Work\s+experience\s*:", re.IGNORECASE)

_ISIN_TOKEN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")

_PASSIVE_RIGHT_LABEL_STOP_RE = re.compile(
    r"^(?:Underlying\s+Index|Exchange\s+Listed|Exchange\s+Symbol|"
    r"iNAV\s+symbol|ISIN|Bloomberg\s+Code|Creation\s+Unit\s+Size|"
    r"Entry\s+Load|Load\s+Structure|Minimum\s+Investment|Basket\s+Size|"
    r"For\s+benchmark\s+riskometer)",
    re.IGNORECASE,
)


def _passive_split_column_text(page, want_left: bool) -> str:
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    if not words:
        return ""
    selected = [
        w for w in words if (float(w["x0"]) < _PASSIVE_LEFT_RIGHT_SPLIT_X) == want_left
    ]
    rows: dict[int, list] = {}
    for w in selected:
        top = round(float(w["top"]))
        matched_row = None
        for existing_top in rows:
            if abs(existing_top - top) <= 2:
                matched_row = existing_top
                break
        rows.setdefault(matched_row if matched_row is not None else top, []).append(w)
    lines = []
    for top in sorted(rows):
        ws = sorted(rows[top], key=lambda w: float(w["x0"]))
        lines.append(" ".join(w["text"] for w in ws))
    return "\n".join(lines)


def _passive_left_column_text(page) -> str:
    return _passive_split_column_text(page, want_left=True)


def _passive_right_column_text(page) -> str:
    return _passive_split_column_text(page, want_left=False)


def _passive_scheme_title(text: str) -> str | None:
    title_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _PASSIVE_TITLE_STOP_RE.match(stripped):
            return _clean(" ".join(title_lines)) if title_lines else None
        stripped = _clean(_PASSIVE_TITLE_BANNER_RE.sub("", stripped))
        if not stripped or _PASSIVE_TITLE_PAREN_RE.match(stripped):
            continue
        title_lines.append(stripped)
        if len(title_lines) >= 4:
            return None
    return None


def _passive_segment_schemes(pdf) -> dict[str, list[int]]:
    scheme_pages: dict[str, list[int]] = {}
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        if not (
            re.search(r"Scheme\s+Details\s*:", text, re.IGNORECASE)
            and re.search(r"Investment\s+Objective\s*:", text, re.IGNORECASE)
        ):
            continue
        title = _passive_scheme_title(text)
        if not title:
            continue
        scheme_pages.setdefault(title, []).append(i)
    return scheme_pages


def _passive_extract_benchmark_and_additional(page) -> tuple[str | None, str | None]:
    right_text = _passive_right_column_text(page)
    lines = right_text.splitlines()
    benchmark = None
    additional = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        m = re.match(
            r"^(Additional\s+Benchmark|Benchmark)\s*:\s*(.*)$", stripped, re.IGNORECASE
        )
        if not m:
            continue
        label, value = m.group(1), m.group(2).strip()
        for nxt in lines[i + 1 : i + 3]:
            nxt_stripped = nxt.strip()
            if not nxt_stripped or _PASSIVE_RIGHT_LABEL_STOP_RE.match(nxt_stripped):
                break
            if re.match(
                r"^(Additional\s+Benchmark|Benchmark)\s*:", nxt_stripped, re.IGNORECASE
            ):
                break
            value = f"{value} {nxt_stripped}".strip()
        value = _clean(value) or None
        if label.lower().startswith("additional"):
            additional = value
        elif benchmark is None:
            benchmark = value
    return benchmark, additional


def _passive_extract_isin(page) -> str:
    right_text = _passive_right_column_text(page)
    for line in right_text.splitlines():
        if not re.match(r"^ISIN\s*:", line.strip(), re.IGNORECASE):
            continue
        m = _ISIN_TOKEN_RE.search(line)
        if m:
            return m.group(0)
    return ""


def _passive_extract_fund_managers(page) -> list:
    left_text = _passive_left_column_text(page)
    lines = left_text.splitlines()
    managers = []
    for i, line in enumerate(lines):
        m = _PASSIVE_MANAGER_NAME_RE.match(line.strip())
        if not m:
            continue
        confirmed = any(
            _PASSIVE_MANAGER_EXPERIENCE_RE.search(nxt) for nxt in lines[i + 1 : i + 3]
        )
        if not confirmed:
            continue
        name = _clean(m.group(2))
        name = re.sub(r"[\^*†‡]+$", "", name).strip()
        name = f"{m.group(1)} {name}"
        entry = {"role": "Fund Manager", "name": name, "sleeve": None}
        if entry not in managers:
            managers.append(entry)
    return managers


def _passive_extract_holdings(page) -> list:
    left_text = _passive_left_column_text(page)
    lines = left_text.splitlines()
    holdings = []
    in_table = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        i += 1
        if not in_table:
            if _PASSIVE_HOLDINGS_TABLE_HEADER_RE.match(stripped):
                in_table = True
            continue
        if not stripped or _PASSIVE_HOLDINGS_STOP_RE.match(stripped):
            break
        m = _PASSIVE_HOLDINGS_ROW_RE.match(stripped)
        if not m:
            continue
        name = _clean(m.group(1))
        pct = m.group(2)
        merged = 0
        while i < len(lines) and merged < 2:
            nxt = lines[i].strip()
            if (
                not nxt
                or _PASSIVE_HOLDINGS_STOP_RE.match(nxt)
                or _PASSIVE_HOLDINGS_ROW_RE.match(nxt)
                or nxt.isupper()
            ):
                break
            name = _clean(f"{name} {nxt}")
            i += 1
            merged += 1
        if not name:
            continue
        holdings.append({"company": name, "sector": "", "pct_to_net_assets": pct})
    return holdings


def _passive_extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
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
        if benchmark is None or additional_benchmark is None:
            b, a = _passive_extract_benchmark_and_additional(page)
            if benchmark is None:
                benchmark = b
            if additional_benchmark is None:
                additional_benchmark = a
        if not isin:
            isin = _passive_extract_isin(page)
        for manager in _passive_extract_fund_managers(page):
            if manager not in managers:
                managers.append(manager)
        for holding in _passive_extract_holdings(page):
            holdings.append(holding)
    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }


# ===========================================================================
# ACTIVE FUND FACTSHEET (equity, debt, hybrid schemes; some ETF/Index/FOF too)
# ===========================================================================
#
# One page per equity/hybrid/FOF scheme, two-plus pages per debt/debt-index
# scheme (holdings page followed by a performance page with no further
# holdings). Layout is far denser than the Passive factsheet: multiple
# narrow metadata boxes packed side by side (Date of Allotment, Benchmark,
# Statistical Measures, Fund Manager, Market Cap...) plus a real portfolio
# table (Instrument/Issuer Name, Industry or Rating, one-to-three % of NAV
# columns for gross/derivative/net exposure). Plain text extraction
# interleaves these so badly that industry/rating text can end up
# character-scrambled -- so, unlike the Passive factsheet, this needs a
# genuinely coordinate-based row reconstruction rather than a left/right
# text-column split.
#
# The core idea: every holding row ends with 1-3 percentage tokens on its
# own physical line (the row's rightmost columns); everything else assigned
# to that row -- including a name that wraps across up to two extra lines,
# even when a name's tail line renders vertically *after* its own
# percentage line because a single-line rating/pct value is vertically
# centered against a two-line wrapped name -- falls inside the y-band
# between one such percentage line and the next. Within that band, words
# split into "name" vs "industry/rating" by the largest horizontal gap
# between word clusters, since the industry/rating column's actual left
# edge shifts row to row and can't be pinned to one fixed x boundary.

_ACTIVE_PCT_RE = re.compile(r"^#?-?\d+\.\d+%$")
_ACTIVE_ROW_BUFFER_MIN = 3
_ACTIVE_ROW_BUFFER_MAX = 10
# A fixed buffer past a row's own %-line (to catch a trailing wrapped-name
# or wrapped-sector continuation line before the next row's band begins)
# doesn't work: row spacing varies by page -- a buffer wide enough to catch
# a continuation on a loosely-spaced page merges two adjacent single-line
# rows together on a tightly-spaced one. Instead the buffer is derived per
# row from the actual gap to the NEXT anchor: half that gap safely reaches
# any continuation line sitting close to this row while staying clear of
# the next row's own first line, whatever the page's natural spacing is.

_ACTIVE_TITLE_RE = re.compile(r"^AXIS[A-Z' ]")
_ACRONYMS = {
    "ETF",
    "IT",
    "ESG",
    "ELSS",
    "BSE",
    "SDL",
    "IBX",
    "NBFC",
    "HFC",
    "IDCW",
    "NAV",
    "AAA",
    "US",
    "FOF",
    "NIFTY",
    "NASDAQ",
    "PSU",
    "CRISIL",
    "ICRA",
    "CARE",
    "IND",
    "SO",
    "SOV",
    "IDFC",
    "UK",
    "NSE",
}

_ACTIVE_CATEGORY_LABELS = {
    "equity",
    "debt securities",
    "corporate bond",
    "government bond",
    "government bond strips",
    "state government bond",
    "floating rate note",
    "pass through certificate",
    "certificate of deposit",
    "commercial paper",
    "zero coupon bond",
    "mutual fund units",
    "invit",
    "reit",
    "exchange traded fund",
    "exchange traded funds",
    "money market instruments",
    "grand total",
    "net assets",
    "unlisted",
    "unlisted (vedanta ltd demerger)",
    "treasury bill",
    "cash management bill",
    "domestic equities",
    "domestic equity",
    "international mutual fund units",
    "international exchange traded funds",
    "international equities",
    "foreign securities",
    "physical gold",
    "physical silver",
}
_ACTIVE_CASH_LABELS = {
    "net current assets",
    "cash & other net current assets",
    "cash, cash equivalents and others",
    "debt, cash & other current assets",
    "other current assets",
}


def _active_fix_token(core: str) -> str:
    if core.upper() in _ACRONYMS:
        return core.upper()
    if core.upper() == core and core.isalpha():
        return core.capitalize()
    return core


def _active_clean_title(raw: str) -> str:
    raw = re.sub(r"^AXIS(?=[A-Z])", "AXIS ", raw, flags=re.IGNORECASE)
    words = raw.split()
    out = []
    for w in words:
        if "-" in w:
            parts = w.split("-")
            out.append("-".join(_active_fix_token(p) for p in parts))
        else:
            out.append(_active_fix_token(w))
    return _clean(" ".join(out))


_ACTIVE_TITLE_TRAILING_BANNER_RE = re.compile(
    r"\s*Portfolio\s+Snapshot(?:\s+[A-Za-z]+\s+\d{4})?\s*$", re.IGNORECASE
)


def _active_scheme_title(page) -> str | None:
    """Titles on this page type are the leading uppercase "AXIS..." line(s),
    sometimes with no space after AXIS and sometimes preceded by stray
    risk-o-meter icon glyph lines ("MP" / "M P" / "G") -- skip those, take
    line(s) up to (not including) the "Portfolio Snapshot" subheading or a
    parenthetical scheme-type line that always follows the title. On some
    pages that subheading (plus the "<Month> <Year>" that follows it) ends
    up glued onto the end of the title's own last line rather than sitting
    on its own line, so it's also stripped from whatever text is collected.

    Matching is strictly uppercase (no re.IGNORECASE): later annexure pages
    (SIP tables, scheme-return summaries, product labelling) reference
    scheme names too, but always in mixed case, never as the page's own
    all-caps title -- so requiring uppercase is what keeps this from
    treating every such mention as a new scheme-start page. As a second
    check, a genuine scheme detail page's own "Portfolio Snapshot"
    subheading must also be present somewhere on the page.
    """
    text = page.extract_text() or ""
    if not re.search(r"Portfolio\s+Snapshot", text, re.IGNORECASE):
        return None
    lines = text.splitlines()
    title_lines = []
    for line in lines[:8]:
        stripped = line.strip()
        if not stripped:
            continue
        if not title_lines and not _ACTIVE_TITLE_RE.match(stripped):
            continue
        if re.match(r"^\(", stripped) or re.match(r"^[A-Za-z]+\s+\d{4}$", stripped):
            break
        stripped = _clean(_ACTIVE_TITLE_TRAILING_BANNER_RE.sub("", stripped))
        if not stripped:
            break
        title_lines.append(stripped)
        if len(title_lines) >= 2:
            break
    if not title_lines:
        return None
    return _active_clean_title(" ".join(title_lines))


def _active_segment_schemes(pdf) -> dict[str, list[int]]:
    """A scheme's pages run from its own title page up to (not including)
    the next scheme's title page -- this naturally captures the extra
    performance/statistics page(s) that follow a debt or hybrid scheme's
    holdings page, without needing to hardcode a page count per fund type.
    Capped at 5 pages (comfortably above the largest genuine span observed,
    3 pages) so the very last scheme in the document doesn't silently
    absorb the dozens of annexure/SIP/product-labelling pages that follow
    it with nothing bounding it from above.
    """
    starts: list[tuple[int, str]] = []
    for i, page in enumerate(pdf.pages):
        title = _active_scheme_title(page)
        if title:
            starts.append((i, title))

    scheme_pages: dict[str, list[int]] = {}
    for j, (start_idx, title) in enumerate(starts):
        next_start = starts[j + 1][0] if j + 1 < len(starts) else len(pdf.pages)
        end_idx = min(next_start, start_idx + 5)
        scheme_pages.setdefault(title, []).extend(range(start_idx, end_idx))
    return scheme_pages


def _active_cluster_near(words: list, target_x0: float, max_gap: float = 12) -> list:
    ws = sorted(words, key=lambda w: w["x0"])
    clusters = [[ws[0]]]
    for w in ws[1:]:
        if w["x0"] - clusters[-1][-1]["x1"] > max_gap:
            clusters.append([w])
        else:
            clusters[-1].append(w)
    return min(clusters, key=lambda c: abs(c[0]["x0"] - target_x0))


def _active_extract_benchmark(page) -> str | None:
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    bench_word = next((w for w in words if w["text"] == "BENCHMARK"), None)
    if not bench_word:
        return None
    bx0, btop = bench_word["x0"], bench_word["top"]
    candidates = [
        w
        for w in words
        if btop < w["top"] <= btop + 100 and bx0 - 50 <= w["x0"] <= bx0 + 250
    ]
    if not candidates:
        return None
    lines: dict[float, list] = {}
    for w in candidates:
        top = round(w["top"])
        matched = None
        for t in lines:
            if abs(t - top) <= 2:
                matched = t
                break
        lines.setdefault(matched if matched is not None else top, []).append(w)
    line_list = sorted(lines.items())

    value_words = []
    prev_top = None
    for top, ws in line_list:
        cluster = _active_cluster_near(ws, bx0)
        if abs(cluster[0]["x0"] - bx0) > 40:
            if value_words:
                break
            continue
        if value_words and prev_top is not None and (top - prev_top) > 15:
            break
        value_words.extend(sorted(cluster, key=lambda w: w["x0"]))
        prev_top = top
    value = " ".join(w["text"] for w in value_words)
    return _clean(value) or None


_ACTIVE_NAME_FRAGMENT_RE = re.compile(r"^[A-Z][a-zA-Z'.-]{1,20}$")
_ACTIVE_MANAGER_STOP_RE = re.compile(r"[:%0-9]")
_ACTIVE_MANAGER_STOP_WORDS = {
    "Small",
    "Large",
    "Mid",
    "Other",
    "Others:",
    "Cap:",
    "Foreign",
    "Equity",
    "Domestic",
    "Cash",
    "Securities",
    "Instruments)",
}


def _active_clean_manager_words(words: list) -> list:
    out = []
    for w in words:
        t = w["text"]
        if _ACTIVE_MANAGER_STOP_RE.search(t) or t in _ACTIVE_MANAGER_STOP_WORDS:
            break
        out.append(t)
        if len(out) >= 4:
            break
    return out


def _active_extract_fund_managers(page) -> list:
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    fm_word = next(
        (
            w
            for w in words
            if w["text"] == "FUND"
            and any(
                w2["text"] == "MANAGER" and abs(w2["top"] - w["top"]) < 2
                for w2 in words
            )
        ),
        None,
    )
    if not fm_word:
        return []
    fm_top = fm_word["top"]
    title_words = [
        w
        for w in words
        if w["text"] in ("Mr.", "Ms.", "Dr.", "Mrs.")
        and fm_top < w["top"] <= fm_top + 40
    ]
    if not title_words:
        return []
    line_top = title_words[0]["top"]
    line_titles = sorted(
        [w for w in title_words if abs(w["top"] - line_top) <= 2], key=lambda w: w["x0"]
    )
    line_words = sorted(
        [w for w in words if abs(w["top"] - line_top) <= 2], key=lambda w: w["x0"]
    )
    max_seg_width = 90
    split_points = [w["x0"] for w in line_titles]

    managers = []
    for i, sx0 in enumerate(split_points):
        upper = (
            split_points[i + 1] if i + 1 < len(split_points) else sx0 + max_seg_width
        )
        upper = min(upper, sx0 + max_seg_width)
        seg = [w for w in line_words if sx0 <= w["x0"] < upper]
        name_words = _active_clean_manager_words(seg)

        cont = [
            w
            for w in words
            if line_top < w["top"] <= line_top + 10 and sx0 - 5 <= w["x0"] <= sx0 + 45
        ]
        cont_sorted = sorted(cont, key=lambda w: w["x0"])
        if cont_sorted and _ACTIVE_NAME_FRAGMENT_RE.match(cont_sorted[0]["text"]):
            name_words += _active_clean_manager_words(cont_sorted[:1])

        name = _clean(" ".join(name_words))
        if not name:
            continue
        entry = {"role": "Fund Manager", "name": name, "sleeve": None}
        if entry not in managers:
            managers.append(entry)
    return managers


def _active_extract_isin(page) -> str:
    text = page.extract_text() or ""
    m = re.search(r"\bISIN\s*:?\s*([A-Z]{2}[A-Z0-9]{9}[0-9])\b", text)
    return m.group(1) if m else ""


def _active_group_lines(words: list) -> list:
    lines: dict[float, list] = {}
    for w in words:
        top = round(w["top"])
        matched = None
        for t in lines:
            if abs(t - top) <= 2:
                matched = t
                break
        lines.setdefault(matched if matched is not None else top, []).append(w)
    return [
        (top, sorted(ws, key=lambda w: w["x0"])) for top, ws in sorted(lines.items())
    ]


_ACTIVE_SPACED_LETTERS_RE = re.compile(r"\b(?:[A-Za-z]\s){2,}[A-Za-z]\b")


def _active_fix_spaced_letters(text: str) -> str:
    """Very rarely, a long security name's characters render with tiny
    baseline jitter that splits it into single-character "words" (e.g.
    "S a n s a r Trust" instead of "Sansar Trust") -- collapse runs of 3+
    single-letter tokens back together. Deliberately conservative (requires
    3+ consecutive single letters) so it can't misfire on genuine short
    words or initials in a normal name.
    """
    return _ACTIVE_SPACED_LETTERS_RE.sub(lambda m: re.sub(r"\s", "", m.group(0)), text)


def _active_split_name_sector(
    words: list, anchor_words: list | None = None
) -> tuple[str, str]:
    # Strip percentage-shaped tokens only from the row's OWN anchor line
    # (the physical line whose trailing word is the row's real % of NAV,
    # identified by the caller via _active_group_lines and passed in
    # directly as anchor_words), and only a trailing run of them from its
    # right end -- not every percentage-shaped word anywhere in the band.
    # Matching by identity against the caller's own anchor line -- rather
    # than re-deriving "the anchor line" here from a rounded top value --
    # matters because _active_group_lines' bucketing tolerance (+-2pt) can
    # merge two physically distinct lines that sit unusually close together
    # (a name line and a vertically-centered rating/pct line only ~1.8pt
    # apart, for one real example) into a single line-bucket; recomputing
    # "which words are on the pct line" from a plain rounded top comparison
    # would disagree with that bucketing and miss words it already grouped
    # in, letting a stray trailing "0.78%" slip into the sector text
    # instead of being stripped. Two more things would break without the
    # trailing-only, own-line-only restriction: (1) a wrapped-name
    # continuation line that renders *after* the anchor line (a real layout
    # quirk seen on several pages) would wrongly have its own words treated
    # as "trailing" and skipped, and (2) a residual-disclosure row like
    # "Other Equity (Less than 0.50% of the corpus)" has a percentage
    # embedded mid-sentence as part of its own text; stripping that one too
    # carves an artificial gap right where "0.50%" used to sit and
    # misfires the column split below.
    if anchor_words is None:
        ordered_all = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
        last_top = round(ordered_all[-1]["top"]) if ordered_all else None
        anchor_words = [w for w in ordered_all if round(w["top"]) == last_top]

    pct_line_words = sorted(anchor_words, key=lambda w: w["x0"])
    trailing_pct_count = 0
    for w in reversed(pct_line_words):
        if _ACTIVE_PCT_RE.match(w["text"]):
            trailing_pct_count += 1
        else:
            break
    pct_words_to_drop = set(
        id(w) for w in pct_line_words[len(pct_line_words) - trailing_pct_count :]
    )
    non_pct = [w for w in words if id(w) not in pct_words_to_drop]
    if not non_pct:
        return "", ""
    xs = sorted(non_pct, key=lambda w: w["x0"])
    best_gap, best_idx = 0, None
    for i in range(1, len(xs)):
        gap = xs[i]["x0"] - xs[i - 1]["x1"]
        if gap > best_gap:
            best_gap, best_idx = gap, i
    # A genuine name/sector (or name/rating) column gap is consistently
    # much wider than the ~1-1.5pt gap between words *within* a name or
    # sector phrase (e.g. "Larsen" / "&" / "Toubro") -- but how wide the
    # real column gap itself is varies by page (some tables run as tight as
    # ~6pt between columns, others 15pt+), so the threshold only needs to
    # clear intra-phrase spacing with a safety margin, not match any
    # specific page's column width.
    if best_gap < 4 or best_idx is None:
        ordered = sorted(non_pct, key=lambda w: (round(w["top"]), w["x0"]))
        return _clean(" ".join(w["text"] for w in ordered)), ""
    split_x = (xs[best_idx - 1]["x1"] + xs[best_idx]["x0"]) / 2
    name_words = sorted(
        [w for w in non_pct if w["x0"] < split_x],
        key=lambda w: (round(w["top"]), w["x0"]),
    )
    sector_words = sorted(
        [w for w in non_pct if w["x0"] >= split_x],
        key=lambda w: (round(w["top"]), w["x0"]),
    )
    return (
        _clean(" ".join(w["text"] for w in name_words)),
        _clean(" ".join(w["text"] for w in sector_words)),
    )


def _active_extract_holdings(page) -> list:
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    header_word = next(
        (w for w in words if w["text"] in ("Issuer", "Instrument")), None
    )
    if header_word is None:
        return []
    table_left = header_word["x0"] - 5
    table_top = header_word["top"] + 3
    table_words = sorted(
        [w for w in words if w["x0"] >= table_left and w["top"] >= table_top],
        key=lambda w: w["top"],
    )
    if not table_words:
        return []

    line_list = _active_group_lines(table_words)
    anchors = [
        (top, ws) for top, ws in line_list if _ACTIVE_PCT_RE.match(ws[-1]["text"])
    ]

    # "Grand Total" is this table's definitive end. Anything at a lower
    # y-position on the page belongs to a completely different section
    # (performance/returns tables further down commonly contain their own
    # percentage-shaped values, e.g. CAGR%) -- without this cutoff, a stray
    # numeric anchor down there would pull everything in between into one
    # enormous garbage "row".
    for k, (_, ws) in enumerate(anchors):
        name_probe, _ = _active_split_name_sector(ws, anchor_words=ws)
        if re.sub(r"\s+", " ", name_probe).strip().lower() == "grand total":
            anchors = anchors[: k + 1]
            break

    holdings = []
    prev_bottom = 0
    for i, (anchor_top, anchor_ws) in enumerate(anchors):
        next_top = (
            anchors[i + 1][0]
            if i + 1 < len(anchors)
            else anchor_top + 2 * _ACTIVE_ROW_BUFFER_MAX
        )
        buffer = max(
            _ACTIVE_ROW_BUFFER_MIN,
            min(_ACTIVE_ROW_BUFFER_MAX, (next_top - anchor_top) / 2),
        )
        bottom = anchor_top + buffer
        band_words = [w for w in table_words if prev_bottom <= w["top"] <= bottom]
        pct = anchor_ws[-1]["text"].lstrip("#").rstrip("%")
        name, sector = _active_split_name_sector(band_words, anchor_words=anchor_ws)
        prev_bottom = bottom

        if not name:
            continue
        normalized = re.sub(r"\s+", " ", name).strip().lower()
        if normalized in _ACTIVE_CATEGORY_LABELS:
            continue
        name = _active_fix_spaced_letters(name)
        sector = _active_fix_spaced_letters(sector)
        holdings.append({"company": name, "sector": sector, "pct_to_net_assets": pct})
    return holdings


def _active_extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
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
        if benchmark is None:
            benchmark = _active_extract_benchmark(page)
        if not isin:
            isin = _active_extract_isin(page)
        for manager in _active_extract_fund_managers(page):
            if manager not in managers:
                managers.append(manager)
        for holding in _active_extract_holdings(page):
            holdings.append(holding)
    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
