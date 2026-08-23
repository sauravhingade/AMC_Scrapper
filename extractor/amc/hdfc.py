import re

# from ..pdf_reader import get_column_text

SIDEBAR_WIDTH = 180

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


def reconstruct_lines(words, y_tolerance: float = 1.5) -> str:
    """Group words into lines by y-position, sort each line left-to-right.

    y_tolerance was originally 3.0, which merges lines whose baselines sit
    within 3pt of each other. Found via testing: a name that wraps to a new
    line (e.g. "Mehta") can land within 3pt of a small sleeve-role tag
    ("Equity"/"Debt") positioned elsewhere on the page, causing the two to
    merge into one reconstructed line with WRONG x-order -- the tag (further
    left) sorts before the wrapped name, corrupting extraction. 1.5pt keeps
    genuinely-same-line words together while separating near-miss cases like
    this one.
    """
    lines = {}
    for w in words:
        y = round(w["top"] / y_tolerance) * y_tolerance
        lines.setdefault(y, []).append(w)
    return "\n".join(
        " ".join(w["text"] for w in sorted(lines[y], key=lambda w: w["x0"]))
        for y in sorted(lines)
    )


def get_column_text(page, x0: float, x1: float) -> str:
    """Extract clean text from a vertical column slice of a page, by
    physically cropping the page to the bbox first.

    GOTCHA (found via testing against an HDFC factsheet, doesn't show up on
    360 ONE's layout): page.within_bbox() clips glyphs AT the boundary --
    if a word straddles x1 (e.g. "(TRI)" starting inside the column but
    ending just past it), the clip cuts the word mid-character ("(TRI)"
    becomes "(TRI"), silently corrupting the value. This function is kept
    as-is (360 ONE's sidebar has a clean gap at its boundary, so it never
    hits this), but any AMC whose columns wrap text right up against the
    crop line should use get_column_text_by_start() instead.
    """
    cropped = page.within_bbox((x0, 0, x1, page.height))
    words = cropped.extract_words()
    return reconstruct_lines(words)


def _trim_garbage(value: str) -> str:
    """Safety net for when two sidebar sections merge onto one reconstructed
    line -- truncate at the first known next-section marker rather than
    returning the merged mess. TRACKING ERROR/NET EQUITY EXPOSURE/Debt Index
    Replication Factor/Scrip Code added after testing against the Index
    Solutions factsheet, whose ETF/index-fund pages follow the benchmark
    block with these instead of EXIT LOAD."""
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
    r"#BENCHMARK(?:\s+INDEX)?\s*\n\s*(.+?)" + _STOP, re.DOTALL
)
_BENCH_ADDL_RE = re.compile(
    r"##\s*ADDL\.?\s*BENCHMARK(?:\s+INDEX)?\s*\n\s*(.+?)" + _STOP, re.DOTALL
)


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
# "ex" excluded because HDFC prefixes an outgoing/departed manager's name
# with "Ex" (e.g. "¥ Ex Chirag Setalvad") -- found on schemes that recently
# changed fund managers. "¥" is a footnote marker, also noise.
_NOISE_WORDS = {
    "over",
    "since",
    "total",
    "exp",
    "name",
    "year",
    "years",
    "ex",
} | _MONTHS


def _is_noise_token(tok: str) -> bool:
    t = tok.strip(",.\u00a5").lower()
    if t in _NOISE_WORDS or t == "":
        return True
    if re.match(r"^\d+[.,]?\d*[,]?$", tok):  # bare numbers, "29,", "2026" etc
        return True
    # Letter-spacing rendering artifact (see module docstring point 2/3):
    # real name tokens are always capitalized, so a stray lowercase
    # fragment ("ye", "rs") is debris, not part of a name.
    if tok[0].islower():
        return True
    return False


# Trailing \s* before the closing paren: some pages render "(Equity Assets )"
# with a space before ")" -- found via testing, the tight version without
# \s* silently failed to match those and merged that manager into the next.
_SLEEVE_RE = re.compile(r"\(([A-Za-z][A-Za-z\s]*?(?:Portfolio|Assets))\s*\)")


def extract_fund_managers(sidebar_text: str) -> list[dict]:
    """Reconstructs the FUND MANAGER table into {role, name, sleeve} entries.

    Two algorithms depending on whether the block uses sleeve tags at all
    (checked once up front, not per-line, since a scheme either always or
    never uses them):

    - WITH sleeve tags (hybrid/multi-asset schemes): accumulate name tokens
      per line until a sleeve tag "(Equity Portfolio)" etc. closes out the
      current manager. Same technique 360 ONE's extractor uses.
    - WITHOUT sleeve tags (plain multi-manager -- most debt funds, all
      index funds/ETFs): each line's noise-stripped residual is either a
      complete 2+-word name (closes immediately) or a lone word that's part
      of a name wrapped across two lines (buffered until 2 words
      accumulate). Handles the common HDFC pattern where a two-word surname
      lands on its own line with no sleeve tag to anchor on.
    """
    m = re.search(
        r"FUND MANAGER.*?\n(.*?)(?=\n\s*DATE OF ALLOTMENT|\n\s*NAV\b|\n\s*ASSETS UNDER MANAGEMENT|\n\s*Scrip Code|$)",
        sidebar_text,
        re.DOTALL,
    )
    if not m:
        return []
    lines = m.group(1).split("\n")
    if lines and re.match(r"^Name\b.*Sinc", lines[0].strip()):
        lines = lines[1:]
    lines = [l for l in lines if not l.strip().startswith("Scrip Code")]

    has_sleeve = any(_SLEEVE_RE.search(l) for l in lines)
    managers: list[dict] = []

    if has_sleeve:
        buffer: list[str] = []
        for line in lines:
            sleeve_m = _SLEEVE_RE.search(line)
            if sleeve_m:
                name = " ".join(buffer).strip()
                if name:
                    managers.append(
                        {
                            "role": "Fund Manager",
                            "name": name,
                            "sleeve": sleeve_m.group(1).strip(),
                        }
                    )
                buffer = []
                continue
            buffer.extend(t for t in line.split() if not _is_noise_token(t))
        leftover = " ".join(buffer).strip()
        if leftover:
            managers.append({"role": "Fund Manager", "name": leftover, "sleeve": None})
    else:
        buffer: list[str] = []
        for line in lines:
            residual = [t for t in line.split() if not _is_noise_token(t)]
            if not residual:
                continue
            if len(residual) >= 2:
                if buffer:
                    managers.append(
                        {
                            "role": "Fund Manager",
                            "name": " ".join(buffer),
                            "sleeve": None,
                        }
                    )
                    buffer = []
                managers.append(
                    {"role": "Fund Manager", "name": " ".join(residual), "sleeve": None}
                )
            else:
                buffer.extend(residual)
                if len(buffer) >= 2:
                    managers.append(
                        {
                            "role": "Fund Manager",
                            "name": " ".join(buffer),
                            "sleeve": None,
                        }
                    )
                    buffer = []
        if buffer:
            managers.append(
                {"role": "Fund Manager", "name": " ".join(buffer), "sleeve": None}
            )

    return managers


# ---------------------------------------------------------------------------
# ISIN
# ---------------------------------------------------------------------------


def extract_isin(sidebar_text: str) -> str:
    """Same label format as 360 ONE's ("ISIN : <code>"), kept as a
    best-effort default -- UNTESTED against real HDFC ISIN data, since
    neither factsheet tested (regular open-ended schemes, or the Index
    Solutions ETF/index-fund factsheet) prints an ISIN anywhere -- HDFC
    uses "Scrip Code: BSE:.../NSE:..." for its ETFs instead. Revisit if an
    HDFC factsheet that actually prints ISIN turns up."""
    m = re.search(r"ISIN\s*:\s*([A-Z0-9]{6,15})", sidebar_text)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------

_NUMERIC_PCT = re.compile(r"^-?\d+\.\d+$")

# Category-divider rows that share the same column shape as a real holding
# but aren't securities. Kept local to this module (not the shared
# HOLDINGS_CATEGORY_LABELS in config.py) so this can't affect 360 ONE.
# Normalized (whitespace/hyphens stripped, lowercased) same as the shared
# list's matching convention.
_CATEGORY_LABELS = {
    "equity&equityrelated",
    "equity&equityrelatedtotal",
    "reit/invitinstruments",
    "stockexchange",
    "debtinstruments",
    "debt&debtrelated",
    "governmentsecurities",
    "governmentsecurities(central/state)",
    "creditexposure",
    "creditexposure(nonperpetual)",
    "certificateofdeposit",
    "commercialpaper",
    "treasurybill",
    "non-convertibledebentures/bonds",
    "corporatedebtmarketdevelopmentfund",
    "exchangetradedfunds",
    "subtotal",
    "treps/reverserepo",
    "netreceivables/(payables)",
    "portfoliototal",
    "gold",
    "silver",
    "moneymarketinstruments",
    "cp",
    "cd",
    "cashcashequivalentsandnetcurrentassets",
    "grandtotal",
}
_TABLE_END_LABELS = {"grandtotal", "total"}


def _normalize(s: str) -> str:
    return re.sub(r"[\s\-]+", "", s.lower())


# Company fields that are just a bare corporate suffix are a sign the real
# name got split across a row boundary and lost its first half (see module
# docstring, point 3) -- drop rather than keep a visibly wrong entry.
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
}

# Rows that mark the genuine end of the holdings table. Found via testing:
# without a lower bound, the row scan for the second (right-hand)
# sub-table kept reading straight into the SIP-performance table further
# down the same page, since it sits in the same x-range with no ruling
# line to stop at -- summed percentages over 400% on a plain equity fund
# gave this away. Stopping the scan the moment one of these rows closes
# keeps everything below the real table out.
_TABLE_END_MARKERS = {
    "grandtotal",
    "portfoliototal",
    "total",
    "cashcashequivalentsandnetcurrentassets",
}


def _words_from_chars(chars, x0_min: float) -> list[dict]:
    """Rebuilds words directly from the character stream instead of
    trusting pdfplumber's extract_words(), which was found (via testing)
    to split normal words into single-character tokens inside the
    PORTFOLIO table region despite near-zero real gaps between the
    characters -- see module docstring point 3. Splits on actual space
    characters and on any positive gap wider than 2.5pt as a backup."""
    chars = sorted(
        (c for c in chars if c["x0"] >= x0_min),
        key=lambda c: (round(c["top"], 1), c["x0"]),
    )
    words: list[dict] = []
    cur = None
    for c in chars:
        is_space = c["text"].isspace()
        if (
            cur
            and abs(c["top"] - cur["top"]) < 1.5
            and not is_space
            and (c["x0"] - cur["x1"]) < 2.5
        ):
            cur["text"] += c["text"]
            cur["x1"] = c["x1"]
        else:
            if cur and cur["text"].strip():
                words.append(cur)
            cur = (
                None
                if is_space
                else {"text": c["text"], "x0": c["x0"], "x1": c["x1"], "top": c["top"]}
            )
    if cur and cur["text"].strip():
        words.append(cur)
    return words


def _rows_to_holdings(
    words_in_range: list[dict], sector_start: float, pct_start: float
) -> list[dict]:
    """State machine turning one sub-table's words into holdings.

    Handles two ways a naive read goes wrong: category-divider rows (same
    column shape as a real holding, no percentage -- skipped outright), and
    company names wrapping across lines (accumulated until a percentage
    value appears, at which point the sector text collected so far is
    attached and the holding closes)."""
    rows: dict[float, list[dict]] = {}
    for w in words_in_range:
        rows.setdefault(round(w["top"], 1), []).append(w)

    holdings: list[dict] = []
    company_buf: list[str] = []
    sector_buf: list[str] = []
    state = "idle"
    stop = False

    def close(pct: str):
        nonlocal company_buf, sector_buf, stop
        company = " ".join(company_buf).strip()
        sector = " ".join(sector_buf).strip()
        normalized = _normalize(company)
        if normalized in _TABLE_END_MARKERS:
            stop = True
        if company and pct and normalized not in _CATEGORY_LABELS:
            if normalized not in _SUSPICIOUS_COMPANY:
                holdings.append(
                    {"company": company, "sector": sector, "pct_to_net_assets": pct}
                )
        company_buf, sector_buf = [], []

    for y in sorted(rows):
        if stop:
            break
        row = sorted(rows[y], key=lambda w: w["x0"])
        co = [
            w["text"] for w in row if w["x0"] < sector_start and w["text"] != "\u2022"
        ]
        sec = [w["text"] for w in row if sector_start <= w["x0"] < pct_start]
        pct = None
        for w in row:
            if w["x0"] >= pct_start and _NUMERIC_PCT.match(w["text"]):
                pct = w["text"]
        co_text = " ".join(co).strip()
        sec_text = " ".join(sec).strip()
        is_category_row = bool(co_text) and _normalize(co_text) in _CATEGORY_LABELS

        if co_text and _normalize(co_text) in _TABLE_END_LABELS:
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
                if not sector_buf:
                    company_buf.append(co_text)
                    if sec_text:
                        sector_buf.append(sec_text)
                    if pct:
                        close(pct)
                        state = "idle"
                else:
                    # A new company started before the previous one ever
                    # got a percentage -- previous buffer is incomplete
                    # (almost always a category label with no % that
                    # slipped past the label check); drop it and start
                    # fresh from this row.
                    company_buf, sector_buf = (
                        [co_text],
                        ([sec_text] if sec_text else []),
                    )
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


def extract_holdings(page) -> list[dict]:
    """Finds every "Company"/"Industry" (or "Instrument"/"Rating") header
    pair on the page -- HDFC renders holdings as 1-2 side-by-side
    sub-tables depending on scheme type -- and extracts each as its own
    state machine via _rows_to_holdings. Returns [] (not an exception) if
    no header pair is found, e.g. schemes with a portfolio too simple to
    have a header row at all (single-line Gold/Silver ETF holdings) --
    those correctly surface as holdings_table_not_found for now rather
    than guessing at a layout with no header to anchor on.
    """
    words = _words_from_chars(page.chars, x0_min=180)

    industry_rows = {
        round(w["top"], 1)
        for w in words
        if w["text"].startswith(("Industry", "Rating"))
    }
    header_words = [
        w
        for w in words
        if w["text"].startswith(("Company", "Instrument"))
        and any(abs(round(w["top"], 1) - t) < 1.0 for t in industry_rows)
    ]
    if not header_words:
        return []

    starts = sorted({w["x0"] for w in header_words})
    all_holdings: list[dict] = []
    for i, cs in enumerate(starts):
        next_start = starts[i + 1] if i + 1 < len(starts) else page.width + 999
        candidate_words = [w for w in words if cs - 5 <= w["x0"] < next_start - 5]
        sector_hdr = [
            w for w in candidate_words if w["text"].startswith(("Industry", "Rating"))
        ]
        pct_hdr = [w for w in candidate_words if w["text"] == "%"]
        if not sector_hdr:
            continue
        sector_start = sector_hdr[0]["x0"]
        pct_start = pct_hdr[0]["x0"] - 5 if pct_hdr else cs + 140
        # Right edge is bounded to just past the pct column itself (not to
        # the next header, and NOT left open-ended for the last sub-table)
        # -- found via testing that leaving the last sub-table's right edge
        # unbounded let it sweep in an unrelated box further right on the
        # same page (a SIP-performance table sharing the same x-range),
        # inflating the holdings sum well past 100%.
        right_edge = pct_start + 55
        sub_words = [w for w in candidate_words if w["x0"] < right_edge]
        body = [w for w in sub_words if w["top"] > sector_hdr[0]["top"] + 5]
        all_holdings.extend(_rows_to_holdings(body, sector_start, pct_start))

    return all_holdings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
    """Runs all field extractors for one scheme's page group."""
    first_page = pdf.pages[page_idxs[0]]
    sidebar_text = get_column_text(first_page, 0, SIDEBAR_WIDTH)

    holdings: list[dict] = []
    for idx in page_idxs:
        page = pdf.pages[idx]
        page_holdings = extract_holdings(page)
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
