import re

_TITLE_STOP_RE = re.compile(r"^\(")

# A holdings/industry row is "<name> <pct>%" possibly repeated twice on one
# physical line (the Top 10 Holdings / Top 10 Industry Allocation tables
# print both tables' same-row data on a single flattened text line). Name
# text never contains a literal "%", so a non-greedy run up to the first
# percentage is a safe, simple boundary.
_ROW_RE = re.compile(r"([A-Za-z][^%]*?)\s+(-?\d+(?:\.\d+)?)\s*%")

_HOLDINGS_TABLE_HEADER_RE = re.compile(r"^Particulars\s+Weightage\b", re.IGNORECASE)
_TABLE_STOP_RE = re.compile(r"^Data as on\b", re.IGNORECASE)
# A "Total ... Holdings" row (e.g. "Total Equity & Equity Related Holdings")
# is a subtotal of the individual stock rows printed just above it, not a
# separate holding in its own right -- storing it alongside those rows
# double-counts the same money. "Grand Total" is the same kind of subtotal
# check for the whole table. Both are skipped the same way ABSL's extractor
# skips a pure category/subtotal line rather than storing it as a holding.
_SUBTOTAL_ROW_RE = re.compile(r"^(?:Grand Total|Total\s+.*\bHoldings)$", re.IGNORECASE)

_MANAGER_NAME_RE = re.compile(r"^(Mr\.|Ms\.|Mrs\.|Dr\.)\s+(.+)$")
_MANAGER_EXPERIENCE_RE = re.compile(r"^Overall Experience\s*:", re.IGNORECASE)

# Section labels that terminate a Benchmark/Additional Benchmark value once
# it starts wrapping across lines within the left ("Fund Snapshot") column.
_SNAPSHOT_LABEL_RE = re.compile(
    r"^(?:Benchmark(?:\s+Index)?|Additional\s+Benchmark|ISIN|NSE\s+Symbol|"
    r"NAV|Investment\s+Objective|Fund\s+Manager\s+Details)\s*$",
    re.IGNORECASE,
)

_ISIN_TOKEN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _page_text(page) -> str:
    return page.extract_text() or ""


def _left_column_text(page) -> str:
    """Reconstruct just the "Fund Snapshot" (left) box's own text.

    Splitting at the page's horizontal midpoint keeps every left-box field
    (Benchmark, ISIN, Fund Manager Details) in its own correct top-to-bottom
    order, undoing pdfplumber's default row-interleaving of the two
    side-by-side "Fund Snapshot" / "Key Metrics" boxes.
    """
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    if not words:
        return ""
    mid = float(page.width) / 2
    left_words = [w for w in words if float(w["x0"]) < mid]

    rows: dict[int, list] = {}
    for w in left_words:
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


def _scheme_title(text: str) -> str | None:
    """The scheme title is every non-empty line up to the first line that
    starts the "(An open-ended ...)" scheme-type parenthetical, which always
    immediately follows the title (wrapped across 1-2 lines) in this
    template. Returns None if that parenthetical marker never appears
    within a plausible title-length window, which means this isn't actually
    a scheme detail page at all (e.g. the AMC contact/address page also
    happens to start with "Angel One").
    """
    title_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _TITLE_STOP_RE.match(stripped):
            return _clean(" ".join(title_lines))
        title_lines.append(stripped)
        if len(title_lines) >= 3:
            # A real title never wraps more than 2 lines in this template;
            # not finding the stop marker by now means this isn't a scheme
            # title at all.
            return None
    return None


def segment_schemes(pdf) -> dict[str, list[int]]:
    """
    Returns {scheme_name: [page_index, ...]}.

    A page belongs to a scheme when its own first line of text is the
    scheme title itself ("Angel One ..."), which is true for every page of
    every scheme's detail spread in this template and false for the table
    of contents, performance-comparison pages, and disclaimer/contact pages
    (those start with their own section heading instead, e.g. "Performance
    of the schemes"). Consecutive pages sharing the same title are grouped
    into one scheme; pages are naturally already in document order.
    """
    scheme_pages: dict[str, list[int]] = {}
    for i, page in enumerate(pdf.pages):
        text = _page_text(page)
        lines = text.splitlines()
        if not lines or not lines[0].strip().startswith("Angel One"):
            continue
        title = _scheme_title(text)
        if not title:
            continue
        scheme_pages.setdefault(title, []).append(i)
    return scheme_pages


def _extract_labelled_value(lines: list[str], label_pattern: str) -> str | None:
    """Find a line that is exactly a section label (e.g. "Benchmark Index")
    and return the value that follows, merging wrapped continuation lines
    until the next recognised section label or a blank line.
    """
    label_re = re.compile(label_pattern, re.IGNORECASE)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not label_re.match(stripped):
            continue

        value_parts = []
        for nxt in lines[i + 1 : i + 4]:
            nxt_stripped = nxt.strip()
            if not nxt_stripped:
                break
            if _SNAPSHOT_LABEL_RE.match(nxt_stripped):
                break
            # The "(as on ...)" qualifier that sometimes sits directly under
            # a label (e.g. under NAV) is not part of a Benchmark value, but
            # Benchmark itself is never followed by one, so no special case
            # is needed here beyond the general label stop above.
            value_parts.append(nxt_stripped)
            # A benchmark value is at most 2 physical lines in this
            # template; stop once we plausibly have the whole thing (a line
            # ending with a closing paren, e.g. "(Total Return Index)", or
            # a line that doesn't look like a continuation fragment).
            if nxt_stripped.endswith(")") or len(value_parts) >= 2:
                break
        value = _clean(" ".join(value_parts))
        return value or None
    return None


def extract_benchmark_and_additional(page) -> tuple[str | None, str | None]:
    left_text = _left_column_text(page)
    lines = left_text.splitlines()
    benchmark = _extract_labelled_value(lines, r"^Benchmark(?:\s+Index)?\s*$")
    additional = _extract_labelled_value(lines, r"^Additional\s+Benchmark\s*$")
    return benchmark, additional


def extract_isin(page) -> str:
    left_text = _left_column_text(page)
    lines = left_text.splitlines()
    for i, line in enumerate(lines):
        if not re.match(r"^ISIN\s*$", line.strip(), re.IGNORECASE):
            continue
        # The ISIN value sometimes has a stray short fragment (an icon
        # glyph misread as text, e.g. a lone "IN") on the line immediately
        # after the label before the real value -- scan the next few lines
        # for a token that actually matches ISIN format rather than
        # assuming the very next line is it.
        for nxt in lines[i + 1 : i + 4]:
            m = _ISIN_TOKEN_RE.search(nxt.strip())
            if m:
                return m.group(0)
    return ""


def extract_fund_managers(page) -> list:
    left_text = _left_column_text(page)
    lines = left_text.splitlines()
    managers = []
    for i, line in enumerate(lines):
        m = _MANAGER_NAME_RE.match(line.strip())
        if not m:
            continue
        # Require the very next non-empty line to look like the
        # "Overall Experience: N years" field that always follows a real
        # manager name in this template, so a stray "Mr./Ms." mention
        # elsewhere in body prose is never mistaken for a manager entry.
        following = None
        for nxt in lines[i + 1 : i + 3]:
            if nxt.strip():
                following = nxt.strip()
                break
        if not following or not _MANAGER_EXPERIENCE_RE.match(following):
            continue

        name = _clean(m.group(2))
        # Strip a trailing footnote marker such as "^" (used here to flag
        # "ceased to be the Fund Manager..." footnotes) without assuming
        # any particular marker character.
        name = re.sub(r"[\^*†‡]+$", "", name).strip()
        name = f"{m.group(1)} {name}"
        entry = {"role": "Fund Manager", "name": name, "sleeve": None}
        if entry not in managers:
            managers.append(entry)
    return managers


def _iter_table_rows(lines: list[str]):
    """Yield (name, pct) for every row inside a Particulars/Weightage table,
    bounded from just after the header line to the first blank line or
    "Data as on ..." footer. A bare category/group line with no percentage
    (e.g. "Money Market Instruments" heading a debt scheme's Portfolio
    table) is silently skipped rather than stored, the same way a plain
    section title is skipped in the ABSL extractor. "Grand Total" is always
    a subtotal check, not a holding, so it is skipped explicitly.
    """
    in_table = False
    for line in lines:
        stripped = line.strip()
        if not in_table:
            if _HOLDINGS_TABLE_HEADER_RE.match(stripped):
                in_table = True
            continue

        if not stripped or _TABLE_STOP_RE.match(stripped):
            break

        matches = _ROW_RE.findall(stripped)
        if not matches:
            # A bare group/category label with no trailing percentage.
            continue

        # Two-column pages ("Top 10 Holdings" beside "Top 10 Industry
        # Allocation") print both tables' same-row data on one flattened
        # line; only the FIRST match is this scheme's own Holdings-side
        # entry -- the second, if present, belongs to the Industry
        # Allocation table and is intentionally not captured here (that
        # mirrors the ABSL extractor's own convention of not storing a
        # sector/category subtotal line as a holding in its own right).
        name, pct = matches[0]
        name = _clean(name)
        if not name or _SUBTOTAL_ROW_RE.match(name):
            continue
        yield name, pct


def extract_holdings(page) -> list:
    text = _page_text(page)
    lines = text.splitlines()
    holdings = []
    for name, pct in _iter_table_rows(lines):
        holdings.append({"company": name, "sector": "", "pct_to_net_assets": pct})
    return holdings


def extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
    """
    Same public output contract as the ABSL extractor. Returns exactly:
        benchmark, additional_benchmark, isin, fund_managers, holdings,
        holdings_count
    """
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
            page_benchmark, page_additional = extract_benchmark_and_additional(page)
            if benchmark is None:
                benchmark = page_benchmark
            if additional_benchmark is None:
                additional_benchmark = page_additional

        if not isin:
            isin = extract_isin(page)

        for manager in extract_fund_managers(page):
            if manager not in managers:
                managers.append(manager)

        for holding in extract_holdings(page):
            holdings.append(holding)

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
