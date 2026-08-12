# Factsheet Field Extractor (no LLM)

Extracts **Benchmark, Fund Manager(s), ISIN, and Stock Portfolio Holdings**
per scheme from AMC factsheet PDFs, using deterministic parsing only
(`pdfplumber` word/table extraction + regex) -- no LLM calls anywhere.

## Why no-LLM works for these specific fields
Benchmark, Fund Manager, and ISIN are always **labeled key-value pairs**
("Benchmark Index : X", "Fund Manager Mr. Y", "ISIN : Z") in AMFI-mandated
factsheets. Holdings are a **real table** with header row "Company Name /
Sector / % to Net Assets". None of these require semantic reading -- they
need reliable label anchors and structural table parsing, which is exactly
what this pipeline does.

## Validated against `360_ONE_MF_July_2026.pdf`
- 12/12 schemes correctly segmented (equity, debt, hybrid, ETF types)
- Benchmark: 12/12 extracted
- Fund Manager(s): 12/12 extracted, correctly split per role (Fund Manager /
  Co-Fund Manager) for hybrid funds with separate equity/debt managers
- ISIN: correctly blank for equity/debt funds, populated for the 3 ETFs
- Holdings: 9/12 clean; 3 flagged `needs_review` (self-caught, not silent)

## Real bugs found and fixed during testing (keep this list growing per AMC)
1. `extract_text(layout=True)` **clips words** near a crop boundary --
   switched to word-bbox reconstruction (`pdf_reader.reconstruct_lines`).
2. Heading detection using `str.isupper()` **silently drops mixed-case
   headings** (e.g. "360 ONE MSCI India ETF") -- switched to keyword-based
   detection.
3. Benchmark names **wrap across lines** for hybrid/multi-asset schemes --
   regex now captures until the next known sidebar label, not just `\n`.
4. Debt/liquid fund holdings tables mix **category subtotal rows**
   ("Commercial Paper", "REIT/InvIT Instruments") into the same 3-column
   shape as real holdings -- filtered via `HOLDINGS_CATEGORY_LABELS`.
5. Commodity ETF (Gold/Silver) holdings tables are **too simple for
   pdfplumber's line-based table detector** -- added a text-regex fallback.

## What still needs the other 49 PDFs to validate
- Sidebar column width (`DEFAULT_SIDEBAR_WIDTH = 152`) is specific to this
  AMC's layout. Other AMCs will need their own width (or a different
  column-detection strategy) -- same "opt-in per-AMC override" pattern as
  your scraper's `amc_specific.py`.
- Label wording may vary ("Benchmark Index" vs "Benchmark" vs "Scheme
  Benchmark") -- `config.FIELD_LABELS` is set up to hold variant lists per
  AMC as you encounter them.
- `amc_name` should come from your existing `amc_tracking_status.xlsx`
  tracker, not be parsed from the filename (see `batch._amc_name_from_filename`
  docstring) -- filename conventions won't be consistent across AMCs.

## Usage
```bash
python -m extractor.batch --all                       # process every PDF in downloads/
python -m extractor.batch --amc "360 ONE Mutual Fund"  # process one AMC
```
Output: `output/extracted_schemes.json`, one record per scheme, with a
`needs_review` flag + `review_reasons` list for anything the deterministic
checks couldn't confirm.

## Project structure
```
extractor/
  config.py             # sidebar width, keyword lists, category-row exclusions
  pdf_reader.py          # word-bbox line reconstruction, column cropping
  segmenter.py            # multi-scheme boundary detection per PDF
  field_extractors.py      # benchmark / ISIN / fund_manager / holdings logic
  schema.py                 # pydantic SchemeRecord + rule-based review flagging
  extractor.py                # extract_factsheet_data(amc, pdf_path, month) -> records
  batch.py                     # CLI: --amc / --all, writes output/extracted_schemes.json
```
