"""
Abakkus Mutual Fund extractor.

Abakkus factsheets use a multi-column layout which is different from the
other AMC extractors.  In particular, the equity portfolio is printed as
TWO side-by-side portfolio columns, while the liquid-fund portfolio is a
single wider table.

This extractor is intentionally self-contained.  It is coordinate driven:
we locate the actual portfolio header(s) on the page and then parse only the
words belonging to those table columns.  No page number or month is hardcoded.
"""

import re

# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

_PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")

_EQUITY_SECTORS = {
    "aerospace & defense",
    "agricultural, commercial & construction vehicles",
    "auto components",
    "automobiles",
    "banks",
    "beverages",
    "capital markets",
    "chemicals & petrochemicals",
    "commercial services & supplies",
    "construction",
    "consumer durables",
    "consumer services",
    "electrical equipment",
    "entertainment",
    "fertilizers & agrochemicals",
    "finance",
    "financial technology (fintech)",
    "food products",
    "healthcare equipment & supplies",
    "healthcare services",
    "industrial manufacturing",
    "industrial products",
    "insurance",
    "it - software",
    "leisure services",
    "metals & mining",
    "ferrous metals",
    "non - ferrous metals",
    "oil, gas & consumable fuels",
    "petroleum products",
    "other utilities",
    "pharmaceuticals & biotechnology",
    "power",
    "realty",
    "retailing",
    "telecom - services",
    "textiles & apparels",
    "transport services",
    "services",
    "chemicals",
    "healthcare",
    "fast moving consumer goods",
    "capital goods",
    "financial services",
    "information technology",
    "construction services",
}

_DEBT_CATEGORIES = {
    "certificate of deposit",
    "commercial paper",
    "government securities/treasury bills",
    "government securities",
    "corporate bond & ncds",
    "cash & cash equivalents",
    "treps / reverse repo",
    "corporate debt market development fund",
    "net receivables / (payables)",
    "net receivables/(payables)",
}

_RATING_RE = re.compile(
    r"(?:"
    r"CRISIL\s+[A-Za-z0-9+()\-/]+"
    r"|ICRA\s+[A-Za-z0-9+()\-/]+"
    r"|CARE\s+[A-Za-z0-9+()\-/]+"
    r"|IND\s+[A-Za-z0-9+()\-/]+"
    r"|A1\+/?AAA"
    r"|A1\+"
    r"|AAA"
    r"|AA\+"
    r"|AA"
    r"|Sovereign"
    r")",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00a0", " ")
    text = text.replace("\u00ad", "")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[•●▪◦]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" \t\r\n:-")


def _page_words(page):
    try:
        return (
            page.extract_words(
                x_tolerance=3,
                y_tolerance=1.5,
                keep_blank_chars=False,
            )
            or []
        )
    except TypeError:
        return page.extract_words() or []


def _words_to_lines(words, y_tolerance=1.5):
    """Group words into physical PDF lines while preserving coordinates."""
    if not words:
        return []

    rows = []
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(word["top"])
        row = None
        for candidate in reversed(rows[-3:]):
            if abs(candidate["top"] - top) <= y_tolerance:
                row = candidate
                break
        if row is None:
            row = {"top": top, "words": []}
            rows.append(row)
        row["words"].append(word)

    result = []
    for row in rows:
        ws = sorted(row["words"], key=lambda w: float(w["x0"]))
        result.append(
            {
                "top": row["top"],
                "bottom": max(float(w.get("bottom", w["top"])) for w in ws),
                "x0": min(float(w["x0"]) for w in ws),
                "x1": max(float(w["x1"]) for w in ws),
                "text": _clean(" ".join(w["text"] for w in ws)),
                "words": ws,
            }
        )
    return result


def _page_text(page) -> str:
    return "\n".join(
        line["text"] for line in _words_to_lines(_page_words(page)) if line["text"]
    )


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def extract_benchmark(text: str) -> str | None:
    """Extract the Fund Features benchmark only."""
    if not text:
        return None
    m = re.search(
        r"\bBenchmark\s*:\s*(.+?)(?=\s+(?:Minimum Investment Amount|"
        r"Minimum Additional Purchase Amount|Minimum Redemption Amount|"
        r"Entry Load|Exit Load|Plans and Options|Data as of)\b)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return _clean(m.group(1)) if m else None


def extract_additional_benchmark(text: str) -> str | None:
    if not text:
        return None
    m = re.search(
        r"\bAdditional\s+Benchmark\s*:?\s*(.+?)(?=\s+(?:Minimum Investment Amount|"
        r"Minimum Additional Purchase Amount|Minimum Redemption Amount|"
        r"Entry Load|Exit Load|Plans and Options|Data as of)\b)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return _clean(m.group(1)) if m else None


def extract_isin(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\bISIN\s*:?\s*([A-Z0-9]{6,20})\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def extract_fund_managers(text: str) -> list[dict]:
    """Extract managers from the explicit Fund Manager field only."""
    if not text:
        return []

    # Only inspect the Fund Manager field, ending at the next Fund Features
    # label. This prevents managers from the performance section leaking in.
    m = re.search(
        r"\bFund\s+Manager\s*:\s*(.+?)(?=\s+\b(?:Benchmark|Plans and Options|"
        r"Minimum Investment Amount|Minimum Additional Purchase Amount|"
        r"Minimum Redemption Amount|Entry Load|Exit Load|Data as of)\s*:?)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return []

    manager_text = m.group(1)
    matches = list(
        re.finditer(
            r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s+"
            r"([A-Za-z]+(?:\s+[A-Za-z]+){0,4})",
            manager_text,
            re.IGNORECASE,
        )
    )

    managers = []
    for i, match in enumerate(matches):
        name = _clean(match.group(1))
        name = re.split(
            r"\s+(?=(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s)", name, 1, flags=re.IGNORECASE
        )[0]
        name = re.sub(
            r"\s+(?:Equity|Fixed Income|Debt)$", "", name, flags=re.IGNORECASE
        )
        name = _clean(name)
        if not name:
            continue

        end = matches[i + 1].start() if i + 1 < len(matches) else len(manager_text)
        after = manager_text[match.end() : end]
        sleeve_match = re.search(
            r"\b(Equity|Fixed\s+Income|Debt|Commodity|Commodities)\b",
            after,
            re.IGNORECASE,
        )
        sleeve = None
        if sleeve_match:
            sleeve = sleeve_match.group(1)
            sleeve = (
                "Debt"
                if sleeve.lower() in {"fixed income", "debt"}
                else "Commodity"
                if sleeve.lower() in {"commodity", "commodities"}
                else "Equity"
            )

        entry = {"role": "Fund Manager", "name": name, "sleeve": sleeve}
        if entry not in managers:
            managers.append(entry)
    return managers


# ---------------------------------------------------------------------------
# Portfolio header / column detection
# ---------------------------------------------------------------------------


def _find_portfolio_headers(page):
    """Locate every Abakkus portfolio table header using word coordinates."""
    lines = _words_to_lines(_page_words(page))
    headers = []

    for i, line in enumerate(lines):
        text = line["text"]
        nearby = " ".join(x["text"] for x in lines[i : min(i + 5, len(lines))])

        # Equity header is frequently extracted as:
        #   Company % of Net Company % of Net
        #   Assets Assets
        if re.search(r"\bCompany\b.*%\s*of", text, re.IGNORECASE) and re.search(
            r"\bNet\b.*\bAssets\b", nearby, re.IGNORECASE
        ):
            for word in line["words"]:
                if re.fullmatch(r"Company", word["text"], re.IGNORECASE):
                    headers.append(
                        {
                            "x0": float(word["x0"]),
                            "top": float(line["top"]),
                            "type": "equity",
                        }
                    )
            continue

        # Liquid/debt header.
        if re.search(
            r"\b(?:Company/Issuer|Instrument/Issuer\s+Name).*%\s*of\s*Net\b",
            text,
            re.IGNORECASE,
        ) and re.search(r"\bAssets\b", nearby, re.IGNORECASE):
            for word in line["words"]:
                if re.match(
                    r"(?:Company/Issuer|Instrument/Issuer)", word["text"], re.IGNORECASE
                ):
                    headers.append(
                        {
                            "x0": float(word["x0"]),
                            "top": float(line["top"]),
                            "type": "debt",
                        }
                    )

    unique = []
    for h in headers:
        if not any(
            abs(h["x0"] - x["x0"]) < 2
            and abs(h["top"] - x["top"]) < 3
            and h["type"] == x["type"]
            for x in unique
        ):
            unique.append(h)
    return sorted(unique, key=lambda h: (h["top"], h["x0"]))


def _find_equity_table_ranges(page):
    headers = [h for h in _find_portfolio_headers(page) if h["type"] == "equity"]
    if not headers:
        return []

    _page_words(page)
    page_width = float(page.width)
    ranges = []

    for i, header in enumerate(headers):
        left = header["x0"] - 3
        if i + 1 < len(headers):
            right = headers[i + 1]["x0"] - 5
        else:
            # The second equity table ends before the page edge, but using
            # page width is safe because we stop at Grand Total.
            right = page_width
        ranges.append((left, right, header))

    return ranges


def _find_debt_table_range(page):
    headers = [h for h in _find_portfolio_headers(page) if h["type"] == "debt"]
    if not headers:
        return None
    h = headers[0]
    return (h["x0"] - 3, float(page.width), h)


def _column_lines(page, left, right, start_top):
    words = []
    for w in _page_words(page):
        x0 = float(w["x0"])
        top = float(w["top"])
        if top < start_top:
            continue
        if left <= x0 < right:
            words.append(w)
    return _words_to_lines(words)


# ---------------------------------------------------------------------------
# Equity portfolio parser
# ---------------------------------------------------------------------------


def _is_equity_sector(text: str) -> bool:
    value = _clean(text).lower()
    value_no_pct = _clean(_PERCENT_RE.sub("", value))
    return value in _EQUITY_SECTORS or value_no_pct in _EQUITY_SECTORS


def _is_stop_line(text: str) -> bool:
    return bool(
        re.match(
            r"^(?:Grand Total|Company/Issuer|Instrument/Issuer|Top Ten Holdings|"
            r"FUND MANAGER|TOP 10 SECTORS|MARKET CAP ALLOCATION|"
            r"INSTRUMENT ALLOCATION|RATING ALLOCATION|Disclaimer)\b",
            _clean(text),
            re.IGNORECASE,
        )
    )


def _parse_equity_column(lines, initial_sector=""):
    """Parse one physical equity portfolio column.

    Returns (holdings, last_sector).  The last sector is important because
    Abakkus sometimes puts a sector subtotal at the bottom of the left
    column and the corresponding issuer(s) at the top of the right column.
    """
    holdings = []
    current_sector = initial_sector
    i = 0

    while i < len(lines):
        text = _clean(lines[i]["text"])
        if not text:
            i += 1
            continue

        # Header continuation words can appear inside the table column
        # because the two-line PDF header is vertically staggered.
        if text.lower() in {
            "assets",
            "net",
            "company",
            "% of",
            "company % of",
            "company % of net",
        }:
            i += 1
            continue

        if _is_stop_line(text):
            break

        if re.fullmatch(r"[•●▪◦]+", text):
            i += 1
            continue

        # Direct sector subtotal.
        pct_match = re.fullmatch(r"(.+?)\s+(-?\d+(?:\.\d+)?)\s*%", text)
        if pct_match and _is_equity_sector(pct_match.group(1)):
            current_sector = _clean(pct_match.group(1))
            i += 1
            continue

        # Normal one-line issuer.
        if re.search(r"-?\d+(?:\.\d+)?\s*%\s*$", text):
            match = re.match(r"^(.*?)\s+(-?\d+(?:\.\d+)?)\s*%\s*$", text)
            company, pct = _clean(match.group(1)), match.group(2)
            if company and not _is_equity_sector(company):
                holdings.append(
                    {
                        "company": company,
                        "sector": current_sector,
                        "pct_to_net_assets": pct,
                    }
                )
            i += 1
            continue

        # Wrapped row. This can be either a wrapped sector subtotal or a
        # wrapped company name. Example sector:
        #   Agricultural, Commercial & Construction
        #   Vehicles 3.00%
        combined = text
        found = False
        for j in range(i + 1, min(i + 5, len(lines))):
            nxt = _clean(lines[j]["text"])
            if not nxt:
                continue
            if _is_stop_line(nxt):
                break
            combined = f"{combined} {nxt}"
            match = re.match(r"^(.*?)\s+(-?\d+(?:\.\d+)?)\s*%\s*$", combined)
            if not match:
                continue

            name = _clean(match.group(1))
            pct = match.group(2)

            if _is_equity_sector(name):
                current_sector = name
            elif name:
                holdings.append(
                    {
                        "company": name,
                        "sector": current_sector,
                        "pct_to_net_assets": pct,
                    }
                )

            i = j + 1
            found = True
            break

        if not found:
            i += 1

    return holdings, current_sector


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


def _parse_debt_lines(lines):
    """Parse the single-column liquid-fund issuer/rating table."""
    holdings = []
    i = 0

    while i < len(lines):
        text = _clean(lines[i]["text"])
        if not text:
            i += 1
            continue

        if _is_stop_line(text):
            break

        # Header itself.
        if re.search(
            r"(?:Instrument/Issuer\s+Name|Company/?Issuer).*%\s*of\s*Net\s*Assets",
            text,
            re.IGNORECASE,
        ):
            i += 1
            continue

        # Category subtotal.
        m = re.fullmatch(r"(.+?)\s+(-?\d+(?:\.\d+)?)\s*%", text)
        if m and _clean(m.group(1)).lower() in _DEBT_CATEGORIES:
            _clean(m.group(1))
            i += 1
            continue

        # Issuer + rating + pct, potentially wrapped over several lines.
        combined = text
        found = False
        for j in range(i, min(i + 5, len(lines))):
            if j != i:
                nxt = _clean(lines[j]["text"])
                if not nxt or _is_stop_line(nxt):
                    break
                combined = f"{combined} {nxt}"

            match = re.match(
                rf"^(.*?)\s+({_RATING_RE.pattern})\s+(-?\d+(?:\.\d+)?)\s*%\s*$",
                combined,
                re.IGNORECASE,
            )
            if match:
                company = _clean(match.group(1))
                rating = _clean(match.group(2))
                pct = match.group(3)
                if company and company.lower() not in _DEBT_CATEGORIES:
                    holdings.append(
                        {"company": company, "sector": rating, "pct_to_net_assets": pct}
                    )
                i = j + 1
                found = True
                break

        if not found:
            i += 1

    return holdings


def extract_holdings(page):
    """
    Extract Abakkus portfolio holdings using the actual table geometry.

    Equity pages have two side-by-side Company/% tables; both are parsed.
    Liquid pages have one Instrument/Issuer table and are parsed separately.
    """
    equity_ranges = _find_equity_table_ranges(page)
    if equity_ranges:
        holdings = []
        carry_sector = ""
        for left, right, header in equity_ranges:
            lines = _column_lines(page, left, right, header["top"] + 15)
            column_holdings, carry_sector = _parse_equity_column(lines, carry_sector)
            holdings.extend(column_holdings)
        return _dedupe_holdings(holdings)

    debt_range = _find_debt_table_range(page)
    if debt_range:
        left, right, header = debt_range
        lines = _column_lines(page, left, right, header["top"] + 15)
        return _dedupe_holdings(_parse_debt_lines(lines))

    return []


# ---------------------------------------------------------------------------
# Scheme extraction
# ---------------------------------------------------------------------------


def _scheme_is_performance_page(text: str) -> bool:
    t = _clean(text).lower()
    return "fund performance as on" in t[:400]


def extract_scheme_fields(pdf, page_idxs: list[int]) -> dict:
    """Entry point expected by the main AMC extractor."""
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
        text = _page_text(page)

        if _scheme_is_performance_page(text):
            continue

        # Portfolio page contains the clean Fund Features metadata in the
        # left column.  Extract metadata from words left of the portfolio
        # table, so right-hand holdings cannot contaminate it.
        headers = _find_portfolio_headers(page)
        metadata_text = text
        if headers:
            boundary = min(h["x0"] for h in headers)
            metadata_words = [
                w
                for w in _page_words(page)
                if float(w["x1"]) <= boundary + 2 and float(w["top"]) >= 0
            ]
            metadata_text = "\n".join(
                line["text"] for line in _words_to_lines(metadata_words) if line["text"]
            )

        if benchmark is None:
            benchmark = extract_benchmark(metadata_text)
        if not isin:
            isin = extract_isin(metadata_text)
        for manager in extract_fund_managers(metadata_text):
            if manager not in managers:
                managers.append(manager)

        page_holdings = extract_holdings(page)
        for holding in page_holdings:
            if holding not in holdings:
                holdings.append(holding)

        # Only take an explicitly labelled additional benchmark from metadata.
        if additional_benchmark is None:
            additional_benchmark = extract_additional_benchmark(metadata_text)

    return {
        "benchmark": benchmark,
        "additional_benchmark": additional_benchmark,
        "isin": isin,
        "fund_managers": managers,
        "holdings": holdings,
        "holdings_count": len(holdings),
    }
