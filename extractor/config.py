"""
Central config for the factsheet extractor.
No LLM calls anywhere in this pipeline -- every field is pulled via
labeled-text regex or structural table extraction with pdfplumber.

Per-AMC layout logic (sidebar widths, label patterns, table strategy) does
NOT live here anymore -- each AMC gets its own module under amc/, since
"one config dict of overrides" stopped scaling once 360 ONE and HDFC turned
out to need genuinely different extraction *code*, not just different
parameters. See amc/__init__.py for how an AMC name resolves to a module.
"""

INPUT_DIR = "downloads"          # where downloaded factsheet PDFs live (from the scraper)
OUTPUT_DIR = "output"            # where extracted JSON / xlsx land

# A scheme heading line must contain one of these keywords (case-insensitive)
# to be treated as a new scheme boundary. Using keyword search instead of
# str.isupper() avoids the MSCI-India-ETF-style mixed-case bug. Verified
# against both 360 ONE and HDFC factsheets -- keyword detection holds up
# across AMCs even though their sidebar layouts differ completely, so this
# stays shared/generic rather than moving into amc/.
SCHEME_KEYWORDS = ["FUND", "ETF", "SCHEME", "PLAN"]

# Lines that look like scheme headings but aren't (running headers / boilerplate).
# CONTD/RISKOMETERS/PERFORMANCE DETAILS added after testing against HDFC --
# its factsheet has continuation-page headers ("HDFC Gilt Fund ....Contd from
# previous page") and summary-section headers ("BENCHMARK AND SCHEME
# RISKOMETERS") that contain a scheme keyword and were false-positively
# treated as new scheme headings.
HEADING_EXCLUDE = [
    "MONTHLY", "GLOSSARY", "DISCLAIMER", "FACTSHEET",
    "CONTD", "RISKOMETERS", "PERFORMANCE DETAILS",
]

# Row labels that appear inside portfolio holdings tables but are category
# subtotal headers, not actual holdings -- must be filtered out. Shared
# across AMCs (amc/common.py uses this), since these labels are fairly
# standardized SEBI factsheet terminology.
HOLDINGS_CATEGORY_LABELS = {
    "equity & equity related total", "reit/invit instruments",
    "stock exchange", "debt instruments", "government securities",
    "certificate of deposit", "commercial paper", "treasury bill",
    "non-convertible debentures/bonds", "corporate debt market development fund",
    "exchange traded funds", "sub total", "subtotal", "treps / reverse repo",
    "net receivables / (payables)", "portfolio total", "gold", "silver",
}
