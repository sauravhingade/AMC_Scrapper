"""
Bajaj Finserv Mutual Fund extractor.

Architecture mirrors abakkus.py: this module exposes the same two
framework entry points -

    segment_schemes(pdf)              -> {scheme_name: [page_idx, ...]}
    extract_scheme_fields(pdf, idxs)  -> {benchmark, additional_benchmark,
                                           isin, fund_managers, holdings,
                                           holdings_count}

Bajaj's factsheet PDF embeds a broken/obfuscated font: common English
words (labels like "Industry", "Equity", "Grand Total") render as
scrambled glyphs when the raw text layer is read with pdfplumber OR
PyMuPDF - but the *data* we actually care about (company names, sector/
industry names, percentages, ratings, fund-manager names in most cases)
comes through clean. Because of this we don't try to regex-match a
literal table structure the way abakkus.py does; instead we locate the
"Stock"/"Issuer" column header word(s) - which are never scrambled -
by (x, y) position, and slice out company / industry / % columns
purely from word coordinates. This makes the parser layout-driven
rather than hard-coded per fund, so it keeps working across month-to-
month template tweaks (1 vs 2 "Stock" columns, presence/absence of an
Industry column, extra "Futures %" sub-column, etc.) without per-scheme
special-casing.

A very useful, deterministic side-effect of the broken font: the "%"
glyph in *bold subtotal/category rows* (e.g. "Equity 97.94%", "Grand
Total 100.00%", "Certificate of Deposit 47.12%") gets mangled to a
non-ASCII character, while individual holding rows keep a literal
ASCII "%". We lean on that: any row where we can't find a literal
"<number>%" is a subtotal/category line, not a holding, and is
skipped automatically - no need to hard-code "Grand Total" etc.
"""

import re

try:
    from ..config import HEADING_EXCLUDE, SCHEME_KEYWORDS
except ImportError:  # pragma: no cover - keeps module usable standalone
    HEADING_EXCLUDE = {"SCHEME DETAILS", "FUND FEATURES", "PORTFOLIO", "PERFORMANCE"}
    SCHEME_KEYWORDS = {"FUND", "ETF", "PLAN"}

try:
    import fitz  # PyMuPDF - much better ligature/font handling than pdfplumber here
except ImportError:  # pragma: no cover
    fitz = None


BODY_MARKERS = re.compile(
    r"SCHEME DETAILS|FUND FEATURES|INVESTMENT OBJECTIVE|FUND MANAGER",
    re.IGNORECASE,
)

_HEADING_RE = re.compile(r"^Bajaj\s+Finserv\s+.+", re.IGNORECASE)
_AMC_NAME_RE = re.compile(r"^Bajaj\s+Finserv\s+Mutual\s+Fund$", re.IGNORECASE)

# Section-divider page titles that are never a scheme's own detail page.
# When we land on one of these, whatever scheme we were accumulating
# pages for is done - stops runaway page bleed into Performance/SIP/PRC
# annexure sections that just happen to also mention "Fund Manager".
_STOP_HEADING_RE = re.compile(
    r"^(Performance|Systematic Investment Plans|Potential Risk Class|"
    r"Risk-o-meter|Equity Funds|Hybrid Funds|Fixed Income Funds|"
    r"Passive Funds|The MacroScope|Content|Index|How To Read|"
    r"From The|From the)\b",
    re.IGNORECASE,
)

_PCT_RE = re.compile(r"-?\d+(?:\.\d+)?%")
_RATING_RE = re.compile(
    r"^(?:CRISIL|ICRA|CARE|FITCH|IND|SOVEREIGN)[A-Za-z0-9+()/\-.\s]*$", re.IGNORECASE
)

# Category / subtotal labels that can slip through with a clean ASCII "%"
# (e.g. "Reverse Repo / TREPS 8.00%") and must not be treated as holdings.
_EXCLUDE_ROWS = {
    "equity",
    "grand total",
    "cash & cash equivalent",
    "cash & cash equivalents",
    "reverse repo / treps",
    "reverse repo/treps",
    "government bond",
    "state government bond",
    "corporate bond",
    "certificate of deposit",
    "commercial paper",
    "treasury bill",
    "mutual fund units",
    "equity options",
    "exchange traded funds",
    "corporate debt market development fund class a2",
    "reit",
    "invit",
    "nifty",
}


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00a0", " ").replace("\u00ad", "").replace("\ufeff", "")
    text = re.sub(r"[\ue000-\uf8ff]", " ", text)
    # pdfplumber falls back to literal "(cid:123)" placeholders for glyphs
    # this factsheet's broken font has no usable ToUnicode mapping for
    # (PyMuPDF at least substitutes *some* character there); strip them
    # out rather than let them leak into names/benchmarks as text.
    text = re.sub(r"\(cid\s*:\s*\d+\)", " ", text)
    return re.sub(r"\s+", " ", text).strip(" \t\r\n:-")


def _looks_like_name(text: str) -> bool:
    """Reject fund-manager names that came out scrambled by the broken font,
    or regex over-captures that swallowed a chunk of surrounding prose."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z.\-' ]{2,45}", text):
        return False
    words = text.split()
    if not (1 <= len(words) <= 4):
        return False
    # A real name is title-cased words; catching stray sentence
    # fragments ("also manages Bajaj Finserv...") this way is more
    # robust than trying to enumerate every connector word.
    return all(w[0].isupper() or w in ("&",) for w in words if w not in ("&",))


# ---------------------------------------------------------------------------
# PDF access helpers - we prefer PyMuPDF (fitz) for its far cleaner glyph
# mapping on this AMC's PDFs, falling back to pdfplumber if fitz isn't
# available in the runtime. Both expose a simple list-of-words interface.
# ---------------------------------------------------------------------------


def _fitz_doc(pdf):
    if fitz is None:
        return None
    cached = getattr(pdf, "_bajaj_fitz_doc", None)
    if cached is not None:
        return cached

    doc = None

    # Try a real filesystem path first (cheapest / most common case).
    path = getattr(pdf, "path", None)
    if not path:
        stream = getattr(pdf, "stream", None)
        path = getattr(stream, "name", None)
    if path:
        try:
            import os

            if os.path.isfile(path):
                doc = fitz.open(path)
        except Exception:
            doc = None

    # Fall back to reading raw bytes off whatever stream pdfplumber has
    # open - this works no matter how the caller opened the PDF
    # (BytesIO, an already-open file object, a path with no usable
    # `.name`, etc.), so we don't silently degrade to the lower-quality
    # pdfplumber text layer just because `path` wasn't populated.
    if doc is None:
        stream = getattr(pdf, "stream", None)
        if stream is not None:
            try:
                pos = stream.tell()
                stream.seek(0)
                data = stream.read()
                stream.seek(pos)
                if data:
                    doc = fitz.open(stream=data, filetype="pdf")
            except Exception:
                doc = None

    if doc is None:
        return None

    try:
        pdf._bajaj_fitz_doc = doc
    except Exception:
        pass
    return doc


def _page_words(pdf, page_idx):
    """Returns a list of {x0, top, bottom, x1, text} dicts for a page."""
    doc = _fitz_doc(pdf)
    if doc is not None and page_idx < len(doc):
        page = doc[page_idx]
        words = page.get_text("words")
        return [
            {
                "x0": float(w[0]),
                "top": float(w[1]),
                "x1": float(w[2]),
                "bottom": float(w[3]),
                "text": w[4],
            }
            for w in words
        ]

    # Fallback: pdfplumber
    page = pdf.pages[page_idx]
    words = (
        page.extract_words(x_tolerance=2, y_tolerance=1.5, keep_blank_chars=False) or []
    )
    return [
        {
            "x0": float(w["x0"]),
            "top": float(w["top"]),
            "x1": float(w["x1"]),
            "bottom": float(w.get("bottom", w["top"])),
            "text": w["text"],
        }
        for w in words
    ]


def _page_size(pdf, page_idx):
    doc = _fitz_doc(pdf)
    if doc is not None and page_idx < len(doc):
        rect = doc[page_idx].rect
        return float(rect.width), float(rect.height)
    page = pdf.pages[page_idx]
    return float(page.width), float(page.height)


def _page_text(pdf, page_idx):
    doc = _fitz_doc(pdf)
    if doc is not None and page_idx < len(doc):
        return doc[page_idx].get_text()
    return pdf.pages[page_idx].extract_text() or ""


def _rows_to_text(rows):
    return "\n".join(
        " ".join(w["text"] for w in row["words"])
        for row in sorted(rows, key=lambda r: r["top"])
    )


def _page_left_text(pdf, page_idx):
    """Reconstructs text from just the left "fund details" info columns
    of a scheme page (SCHEME DETAILS / FUND FEATURES / BENCHMARK / FUND
    MANAGER), by grouping words into rows purely from their own (x, y)
    coordinates rather than trusting the PDF engine's own line-flow, and
    cut off wherever this page's holdings table starts.

    This sidesteps two real problems seen when PyMuPDF isn't available
    and we fall back to pdfplumber: (1) pdfplumber's extract_text() can
    glue this page's side-by-side columns onto the same line, so a
    "BENCHMARK:" line-based stop check runs straight into the holdings
    table's text; and (2) pdfplumber sometimes drops the space inside
    words like "FUND FEATURES" -> "FUNDFEATURES", breaking exact-string
    section boundaries. Restricting to the info columns' x-range (using
    the *same* table-column detection extract_holdings relies on, so the
    cut point is derived from this page rather than a hard-coded pixel
    value) and rebuilding lines from coordinates avoids both.
    """
    words = _page_words(pdf, page_idx)
    if not words:
        return ""
    page_width, _ = _page_size(pdf, page_idx)
    columns = _find_table_columns(words, page_width)
    if columns:
        cutoff_x = min(c["left"] for c in columns)
        cutoff_top = min(c["table_top"] for c in columns)
    else:
        cutoff_x, cutoff_top = page_width, None
    # The fund-details area occupies the *full page width* above where
    # the holdings table starts (it's laid out in its own sub-columns:
    # NAV table, AUM/benchmark/date, fund manager); only below that do we
    # need to start excluding anything to the right of the table itself.
    left_words = [
        w
        for w in words
        if (cutoff_top is None or w["top"] < cutoff_top) or w["x0"] < cutoff_x
    ]
    if not left_words:
        return ""
    return _rows_to_text(_words_to_rows(left_words))


def _words_to_rows(words, y_tolerance=2.5):
    if not words:
        return []
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        row = None
        for candidate in reversed(rows[-4:]):
            if abs(candidate["top"] - w["top"]) <= y_tolerance:
                row = candidate
                break
        if row is None:
            row = {"top": w["top"], "words": []}
            rows.append(row)
        row["words"].append(w)
    for row in rows:
        row["words"].sort(key=lambda w: w["x0"])
        row["top"] = min(w["top"] for w in row["words"])
    return rows


# ---------------------------------------------------------------------------
# Scheme segmentation
# ---------------------------------------------------------------------------


def _is_scheme_heading(line: str) -> bool:
    line = _clean(line)
    if not line or len(line) > 80:
        return False
    if _AMC_NAME_RE.match(line):
        return False
    if any(ex in line.upper() for ex in HEADING_EXCLUDE):
        return False
    if not _HEADING_RE.match(line):
        return False
    upper = line.upper()
    return any(kw in upper for kw in SCHEME_KEYWORDS) or True


def _fix_glued_heading(line: str) -> str:
    """pdfplumber's coordinate-based line reconstruction (used when
    PyMuPDF isn't available) occasionally drops the space right after
    "Finserv" - "Bajaj FinservBanking and PSU Fund" - which both fails
    the heading regex (dropping the scheme entirely) and would produce a
    wrong scheme-name key even if it didn't. Person names get the same
    treatment elsewhere; a scheme heading is similarly safe to re-split
    on a lower->Upper boundary right after the "Finserv" token, and to
    have a space restored after a dropped "- " separator (e.g. "ETF
    -Growth").
    """
    line = re.sub(r"(?<=Finserv)(?=[A-Z])", " ", line)
    line = re.sub(r"-(?=[A-Za-z])", "- ", line)
    return line


def _page_heading_pdfplumber(pdf, page_idx):
    """pdfplumber equivalent of the font-size heading pick above, used
    when PyMuPDF isn't available in the runtime. pdfplumber exposes
    per-character font size via page.chars, which is enough to find the
    same "biggest text near the top of the page" heading.
    """
    page = pdf.pages[page_idx]
    try:
        chars = page.chars
    except Exception:
        chars = None
    if not chars:
        text = _page_text(pdf, page_idx)
        return text.split("\n")[0].strip() if text else ""

    top_chars = [c for c in chars if c.get("top", 999) <= 70]
    if not top_chars:
        text = _page_text(pdf, page_idx)
        return text.split("\n")[0].strip() if text else ""

    best_size = max(c.get("size", 0) for c in top_chars)
    line_chars = sorted(
        (c for c in top_chars if abs(c.get("size", 0) - best_size) < 0.5),
        key=lambda c: (round(c.get("top", 0), 0), c.get("x0", 0)),
    )
    if not line_chars:
        return ""
    ref_top = line_chars[0].get("top", 0)
    line_chars = [c for c in line_chars if abs(c.get("top", 0) - ref_top) <= 3]
    return _clean("".join(c.get("text", "") for c in line_chars))


def _page_heading(pdf, page_idx):
    """The scheme title is set in the largest font at the top of the page.
    Plain reading-order text extraction can put it anywhere (this
    factsheet's two-column layout means "PORTFOLIO ..." sometimes comes
    first in flow order), so we pick it out by font size/position
    instead of assuming it's line 1 of the extracted text.
    """
    doc = _fitz_doc(pdf)
    if doc is None or page_idx >= len(doc):
        return _page_heading_pdfplumber(pdf, page_idx)

    page = doc[page_idx]
    try:
        d = page.get_text("dict")
    except Exception:
        text = page.get_text()
        return text.split("\n")[0].strip() if text else ""

    candidates = []
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            top = min(s["bbox"][1] for s in spans)
            if top > 70:
                continue
            size = max(s["size"] for s in spans)
            text = "".join(s["text"] for s in spans)
            candidates.append((size, top, text))

    if not candidates:
        return ""
    candidates.sort(key=lambda c: (-c[0], c[1]))
    best_size = candidates[0][0]
    top_lines = sorted(
        (c for c in candidates if abs(c[0] - best_size) < 0.5), key=lambda c: c[1]
    )
    return _clean(" ".join(c[2] for c in top_lines[:1]))


def segment_schemes(pdf) -> dict:
    """Returns {scheme_name: [page_index, ...]} in document order."""
    scheme_pages: dict = {}
    current = None

    n_pages = len(getattr(pdf, "pages", []))
    for i in range(n_pages):
        heading = _fix_glued_heading(_page_heading(pdf, i))

        if _is_scheme_heading(heading):
            current = _clean(heading)
            scheme_pages.setdefault(current, [])
        elif heading and _STOP_HEADING_RE.match(heading):
            current = None

        text = _page_text(pdf, i)
        if current and BODY_MARKERS.search(text):
            if i not in scheme_pages[current]:
                scheme_pages[current].append(i)

    return scheme_pages


# ---------------------------------------------------------------------------
# Scheme metadata (benchmark, fund managers, etc.)
# ---------------------------------------------------------------------------


_LABEL_STOP_RE = re.compile(r"^\s*(-?\d+(\.\d+)?%?|[A-Z][A-Z &/]{2,}:?)\s*$")
# A benchmark/index name is short, Title-Case-ish, numbers/%/TRI/index
# words - it never contains ordinary lowercase prose. If a wrapped
# continuation line looks like it wandered into a manager bio or the
# investment-objective paragraph (which can end up on a nearby
# coordinate-reconstructed "row" once table/column boundaries get
# fuzzy), stop rather than swallow it.
_PROSE_LEAK_RE = re.compile(
    r"\b(managing|portion|experience|inception|objective|assurance|achieved|"
    r"investing|invest|distribut\w*|withdrawal|scheme will)\b",
    re.IGNORECASE,
)


def _extract_wrapped_value(text, label_pattern, max_extra_lines=3):
    """Pull a "LABEL: value" field that may wrap across a couple of
    lines (e.g. "BENCHMARK: 65% Nifty 50 TRI + 25% NIFTY\nShort Duration
    Debt Index + ..."), stopping before the next real label, a stray
    chart/page-number line, prose that leaked in from a neighbouring
    column, or a garbled (non-ASCII) line from the broken font that
    isn't actually part of the value.
    """
    m = re.search(label_pattern, text, re.IGNORECASE)
    if not m:
        return None
    rest = text[m.end() :]
    collected = []
    for line in rest.split("\n")[: max_extra_lines + 1]:
        line = line.strip()
        if not line:
            break
        if (
            _LABEL_STOP_RE.match(line)
            or re.search(r"[^\x00-\x7f]", line)
            or _PROSE_LEAK_RE.search(line)
        ):
            break
        collected.append(line)
    return _clean(" ".join(collected)) or None


def extract_benchmark(text):
    if not text:
        return None
    return _extract_wrapped_value(text, r"\bBENCHMARK\s*:\s*")


def extract_additional_benchmark(text):
    if not text:
        return None
    return _extract_wrapped_value(text, r"\bAdditional\s+Benchmark\s*:?\s*")


def extract_isin(text):
    if not text:
        return ""
    m = re.search(r"\bISIN\s*:?\s*([A-Z0-9]{6,20})\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def _normalize_scheme_name(name):
    name = _clean(name)
    # pdfplumber occasionally drops the space between two words (e.g.
    # "Bajaj FinservConsumption Fund"); re-split on a lower->Upper glyph
    # boundary so normalized names still line up with the ones derived
    # from properly-spaced page headings.
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    name = name.lower().replace("&", "and")
    name = re.sub(r"[^a-z0-9]+", " ", name).strip()
    return name


_MANAGER_NOTE_RE = re.compile(
    r"(Bajaj\s+Finserv\s*[A-Za-z0-9&,\-'/ ]+?)\s*:\s*(.+?)(?=Bajaj\s+Finserv\s*[A-Za-z0-9&,\-'/ ]+?\s*:|$)",
    re.DOTALL,
)
_NOTE_NAME_RE = re.compile(
    r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s+([A-Za-z][A-Za-z.\-' ]{2,45}?)(?=[,.(&]| and\b|$)"
)


def _clean_person_name(name):
    """Person names are the one place it's safe to assume a lower->Upper
    letter boundary with no space is a dropped-space artifact (unlike
    company names, e.g. "GlaxoSmithKline", where that's legitimate) - so
    unlike _clean(), this repairs "SorbhGupta" -> "Sorbh Gupta".
    """
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return _clean(name)


def _parse_manager_notes(pdf):
    """The factsheet's broken font scrambles a handful of recurring
    manager names (e.g. "Siddharth Chaudhary", "Ilesh Savla") wherever
    they appear - *except* in one place: the clean, plain-text "Note:
    Fund Managers are managing these schemes since inception..." block
    that lists exactly which managers run each scheme whose roster
    changed. We parse that block once per document and use it to fill
    in names the per-scheme page couldn't recover.

    Returns {normalized_scheme_name: [{"role", "name", "sleeve"}, ...]}
    """
    cached = getattr(pdf, "_bajaj_manager_notes", None)
    if cached is not None:
        return cached

    notes = {}
    n_pages = len(getattr(pdf, "pages", []))
    for i in range(n_pages):
        text = _page_text(pdf, i)
        anchor = text.find("Fund Managers are managing these schemes")
        if anchor == -1:
            continue
        block = text[anchor : anchor + 4000]

        for m in _MANAGER_NOTE_RE.finditer(block):
            scheme = _normalize_scheme_name(m.group(1))
            clause = m.group(2)
            if not scheme or "fund" not in scheme:
                continue

            managers = []
            for portion, portion_text in re.findall(
                r"(Equity|Debt|Commodity[^:]*)\s*portion\s*:\s*([^:]+?)(?=(?:Equity|Debt|Commodity[^:]*)\s*portion\s*:|$)",
                clause,
                re.IGNORECASE,
            ):
                sleeve = (
                    "Debt"
                    if "debt" in portion.lower()
                    else "Commodity"
                    if "commodity" in portion.lower()
                    else "Equity"
                )
                for name in _NOTE_NAME_RE.findall(portion_text):
                    name = _clean_person_name(name)
                    if _looks_like_name(name):
                        managers.append(
                            {"role": "Fund Manager", "name": name, "sleeve": sleeve}
                        )

            if not managers:
                # No "<sleeve> portion:" labels at all - single-sleeve
                # scheme, e.g. "Bajaj Finserv Liquid Fund: Mr. X, Mr. Y."
                for name in _NOTE_NAME_RE.findall(clause):
                    name = _clean_person_name(name)
                    if _looks_like_name(name):
                        managers.append(
                            {"role": "Fund Manager", "name": name, "sleeve": None}
                        )

            if managers:
                notes[scheme] = managers
        break  # the note block only ever appears once in the document

    try:
        pdf._bajaj_manager_notes = notes
    except Exception:
        pass
    return notes


_PERF_BLOCK_RE = re.compile(
    r"(Bajaj\s+Finserv\s*[A-Za-z0-9&,\-'/() ]+?)\s*[-–]\s*(?:Regular|Direct)\b.*?"
    r"The\s+Fund\s+Manager[s]?\s+of\s+the\s+scheme[,:]\s*(.+?)"
    r"(?:For\s+the\s+performance|\n\n)",
    re.DOTALL,
)
# Looser fallback for schemes with no Regular/Direct plan split on their
# performance table (ETFs) - just anchor on the "Fund Manager" sentence
# within a bounded window after the scheme name.
_PERF_BLOCK_RE_LOOSE = re.compile(
    r"(Bajaj\s+Finserv\s+[A-Za-z0-9&,\-'/() ]+?)\n"
    r".{0,700}?The\s+Fund\s+Manager[s]?\s+of\s+the\s+scheme[,:]\s*(.+?)"
    r"(?:For\s+the\s+performance|\n\n)",
    re.DOTALL,
)


def _strip_plan_suffix(name):
    return re.sub(
        r"\s*[-–]?\s*(?:Regular|Direct)?\s*(?:Plan)?\s*[-–]?\s*Growth\s*$", "", name
    ).strip()


def _parse_performance_manager_notes(pdf):
    """Second fallback source for manager names the scheme's own detail
    page couldn't recover cleanly: the "Performance" annexure restates,
    in plain clean text, "The Fund Manager(s) of the scheme: Mr. X[, Mr.
    Y]." right under each scheme's returns table.
    """
    cached = getattr(pdf, "_bajaj_perf_manager_notes", None)
    if cached is not None:
        return cached

    notes = {}
    n_pages = len(getattr(pdf, "pages", []))
    for i in range(n_pages):
        heading = _page_heading(pdf, i)
        if not _STOP_HEADING_RE.match(heading or "") and "Performance" not in (
            heading or ""
        ):
            continue
        text = _page_text(pdf, i)
        for m in _PERF_BLOCK_RE.finditer(text):
            scheme = _normalize_scheme_name(_strip_plan_suffix(m.group(1)))
            if not scheme or scheme in notes:
                continue
            names = [
                {"role": "Fund Manager", "name": _clean_person_name(n), "sleeve": None}
                for n in _NOTE_NAME_RE.findall(m.group(2))
                if _looks_like_name(_clean_person_name(n))
            ]
            if names:
                notes[scheme] = names

        for m in _PERF_BLOCK_RE_LOOSE.finditer(text):
            scheme = _normalize_scheme_name(_strip_plan_suffix(m.group(1)))
            if not scheme or scheme in notes:
                continue
            names = [
                {"role": "Fund Manager", "name": _clean_person_name(n), "sleeve": None}
                for n in _NOTE_NAME_RE.findall(m.group(2))
                if _looks_like_name(_clean_person_name(n))
            ]
            if names:
                notes[scheme] = names

    try:
        pdf._bajaj_perf_manager_notes = notes
    except Exception:
        pass
    return notes


def extract_fund_managers(text):
    if not text:
        return []

    start = text.find("FUND MANAGER")
    if start == -1:
        return []
    end = len(text)
    for marker in ("FUND FEATURES", "SCHEME DETAILS", "PORTFOLIO ("):
        m = text.find(marker, start + 10)
        if m != -1:
            end = min(end, m)
    section = text[start:end]

    managers = []
    for match in re.finditer(
        r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s+([A-Za-z][A-Za-z.\-' ]{2,50}?)\s*\(([^)]*)\)",
        section,
    ):
        name = _clean_person_name(match.group(1))
        if not _looks_like_name(name):
            # The custom font in this factsheet occasionally scrambles a
            # manager's name; skip rather than emit garbage.
            continue
        paren = match.group(2).lower()
        if "debt" in paren or "fixed income" in paren:
            sleeve = "Debt"
        elif "commodity" in paren:
            sleeve = "Commodity"
        elif "equity" in paren:
            sleeve = "Equity"
        elif "arbitrage" in paren:
            sleeve = "Arbitrage"
        else:
            # Single-sleeve schemes (most debt funds, passive ETFs) list
            # managers with no "(... Portion)" tag at all, e.g.
            # "Mr. Nimesh Chandan (Managing fund since inception ...)".
            sleeve = None
        entry = {"role": "Fund Manager", "name": name, "sleeve": sleeve}
        if entry not in managers:
            managers.append(entry)
    return managers


# ---------------------------------------------------------------------------
# Holdings table extraction
# ---------------------------------------------------------------------------


def _find_table_columns(words, page_width):
    """Detect one or more Stock/Issuer table blocks on a page and return
    a list of column descriptors: {left, right, pct_start, kind, table_top}.
    """
    header_words = [w for w in words if w["text"] in ("Stock", "Issuer")]
    if not header_words:
        return []

    ref_top = min(w["top"] for w in header_words)
    row = sorted(
        (w for w in header_words if abs(w["top"] - ref_top) <= 6),
        key=lambda w: w["x0"],
    )

    columns = []
    for i, marker in enumerate(row):
        left = marker["x0"] - 6
        right = row[i + 1]["x0"] - 6 if i + 1 < len(row) else page_width

        # locate the "%" of this block's "% of NAV" header (may wrap to
        # its own line just below "Stock") purely to establish pct_start;
        # table_top is anchored to the "Stock"/"Issuer" marker's own
        # bottom edge - the wrapped "% of NAV"/"Industry" sub-header line
        # can visually overlap the first data row, so it's not a safe
        # anchor for where rows begin.
        pct_words = [
            w
            for w in words
            if w["text"].strip() == "%"
            and left <= w["x0"] < right
            and ref_top - 5 <= w["top"] <= ref_top + 25
        ]
        pct_start = min((w["x0"] for w in pct_words), default=right - 60)

        columns.append(
            {
                "left": left,
                "right": right,
                "pct_start": pct_start,
                "kind": "equity" if marker["text"] == "Stock" else "debt",
                "table_top": marker["bottom"] + 1,
            }
        )
    return columns


def _split_row(row_words, col):
    """Split a row's words (already restricted to this column) into
    (name, extra, pct_text). "extra" is the Industry / Rating sub-column
    when the table has one - detected per-row via the horizontal gap
    between word clusters rather than a fixed x-coordinate, since the
    header label ("Industry"/"Rating") isn't reliably aligned with where
    the data itself starts.
    """
    pct_start = col["pct_start"]
    left_segment = [w for w in row_words if w["x0"] < pct_start]
    pct_words = [w for w in row_words if w["x0"] >= pct_start]

    left_segment.sort(key=lambda w: w["x0"])
    split_idx = len(left_segment)
    max_gap = 0.0
    for i in range(len(left_segment) - 1):
        gap = left_segment[i + 1]["x0"] - left_segment[i]["x1"]
        if gap > max_gap:
            max_gap = gap
            split_idx = i + 1
    if max_gap <= 18 or split_idx == 0:
        split_idx = len(left_segment)

    name_words = left_segment[:split_idx]
    extra_words = left_segment[split_idx:]

    name = _clean(" ".join(w["text"] for w in name_words))
    extra = _clean(" ".join(w["text"] for w in extra_words))
    pct_text = " ".join(w["text"] for w in pct_words)
    return name, extra, pct_text


def _parse_column(words, col):
    col_words = [
        w
        for w in words
        if col["left"] <= w["x0"] < col["right"] and w["top"] >= col["table_top"]
    ]
    rows = _words_to_rows(col_words)

    holdings = []
    i = 0
    while i < len(rows):
        company, industry, pct_text = _split_row(rows[i]["words"], col)
        matches = _PCT_RE.findall(pct_text)

        # Handle a company name that wrapped onto the next physical line
        # (percentage not found yet): merge forward one line at most.
        # Debt tables never wrap a name across lines in this factsheet,
        # and merging there just risks swallowing the *next* real issuer
        # row into a bold subtotal/category line above it - so only try
        # this for equity-style tables.
        j = i
        if col["kind"] == "equity":
            while not matches and company and j + 1 < len(rows) and j - i < 1:
                j += 1
                nxt_company, nxt_industry, nxt_pct_text = _split_row(
                    rows[j]["words"], col
                )
                company = _clean(f"{company} {nxt_company}")
                if nxt_industry:
                    industry = (
                        _clean(f"{industry} {nxt_industry}")
                        if industry
                        else nxt_industry
                    )
                matches = _PCT_RE.findall(nxt_pct_text)

        if not matches:
            if not company:
                # Blank/spacer row - skip it, table may still continue.
                i += 1
                continue
            if col["kind"] == "debt":
                # Debt tables interleave bold category subtotals (e.g.
                # "Certificate of Deposit 47.12%") *between* groups of
                # issuers, not just at the end, so skip past this one
                # row instead of ending the whole column.
                i += 1
                continue
            # Equity tables only carry a subtotal/category row at the very
            # end of the holdings list, so treat this as the end of the
            # table (composition chart / footer follows).
            break

        if not company or len(company) > 70 or not re.search(r"[A-Za-z]", company):
            # Either an excluded category row, a bare number/axis-label
            # picked up from a composition chart below the table (no
            # letters at all), or a merge that dragged in unrelated
            # content (long garbage string) - not a real holding.
            if len(company) > 70:
                break
            i = j + 1
            continue

        if re.search(r"[^\x00-\x7f]", company):
            # The broken font scrambles bold subtotal/category labels
            # (e.g. "Equity", "Grand Total") into non-ASCII glyphs; real
            # holding names are always plain ASCII. Skip rather than
            # break, since debt tables interleave category subtotals
            # between groups of issuers.
            i = j + 1
            continue

        if any(label in company.lower() for label in _EXCLUDE_ROWS):
            i = j + 1
            continue

        pct = matches[0]

        if col["kind"] == "debt":
            # Issuer / Rating / % of NAV - "industry" slot actually holds
            # the credit rating for debt tables.
            rating = industry
            holdings.append(
                {"company": company, "sector": rating, "pct_to_net_assets": pct}
            )
        else:
            holdings.append(
                {"company": company, "sector": industry, "pct_to_net_assets": pct}
            )

        i = j + 1

    return holdings


def _dedupe_holdings(holdings):
    result = []
    seen = set()
    for h in holdings:
        company = _clean(h.get("company", ""))
        sector = _clean(h.get("sector", ""))
        pct = str(h.get("pct_to_net_assets", ""))
        if not company:
            continue
        key = (company.lower(), sector.lower(), pct)
        if key in seen:
            continue
        seen.add(key)
        result.append({"company": company, "sector": sector, "pct_to_net_assets": pct})
    return result


def extract_holdings(pdf, page_idx):
    words = _page_words(pdf, page_idx)
    if not words:
        return []
    page_width, _ = _page_size(pdf, page_idx)

    columns = _find_table_columns(words, page_width)
    if not columns:
        return []

    holdings = []
    for col in columns:
        holdings.extend(_parse_column(words, col))
    return _dedupe_holdings(holdings)


# ---------------------------------------------------------------------------
# Public entry point
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
    additional_benchmark = None
    isin = ""
    managers = []
    holdings = []

    for idx in page_idxs:
        text = _page_text(pdf, idx)
        left_text = _page_left_text(pdf, idx)

        if benchmark is None:
            benchmark = extract_benchmark(left_text) or extract_benchmark(text)
        if additional_benchmark is None:
            additional_benchmark = extract_additional_benchmark(
                left_text
            ) or extract_additional_benchmark(text)
        if not isin:
            isin = extract_isin(left_text) or extract_isin(text)
        for manager in extract_fund_managers(left_text) or extract_fund_managers(text):
            if manager not in managers:
                managers.append(manager)

        for holding in extract_holdings(pdf, idx):
            if holding not in holdings:
                holdings.append(holding)

    # Fill in any managers the per-page text couldn't recover (the broken
    # font scrambles a handful of recurring names like "Siddharth
    # Chaudhary"/"Ilesh Savla" wherever they appear) using the document's
    # own clean-text "Note: Fund Managers ..." roster, keyed by scheme
    # name and merged in by sleeve so we don't duplicate anyone we
    # already found.
    scheme_name = _page_heading(pdf, page_idxs[0])
    scheme_key = _normalize_scheme_name(scheme_name)
    scheme_key_stripped = _normalize_scheme_name(_strip_plan_suffix(scheme_name))
    note_managers = _parse_manager_notes(pdf).get(scheme_key) or _parse_manager_notes(
        pdf
    ).get(scheme_key_stripped, [])
    known_names = {m["name"].lower() for m in managers}
    for m in note_managers:
        if m["name"].lower() in known_names:
            continue
        managers.append(m)
        known_names.add(m["name"].lower())

    # Second fallback: the "Performance" annexure restates the manager(s)
    # of every scheme in plain clean text - use it to fill in any names
    # still missing (e.g. "Ilesh Savla" on ETFs/index funds, or
    # "Siddharth Chaudhary" on debt funds whose roster never changed and
    # so isn't covered by the "Note:" block above).
    perf_notes = _parse_performance_manager_notes(pdf)
    perf_managers = perf_notes.get(scheme_key) or perf_notes.get(
        scheme_key_stripped, []
    )
    for m in perf_managers:
        if m["name"].lower() in known_names:
            continue
        managers.append(m)
        known_names.add(m["name"].lower())

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
