"""
Helpers shared by more than one AMC extractor module.

Keep this file for genuinely cross-AMC logic only (holdings-row filtering
uses fairly standardized SEBI factsheet terminology, so it's a safe shared
default). If a new AMC needs a *variant* of one of these, don't bend the
shared function with more parameters -- copy it into that AMC's own module
and adjust. Divergence is cheaper to maintain than a shared function with
five AMC-specific flags.
"""

import re

from ..config import HOLDINGS_CATEGORY_LABELS

NUMERIC_PCT = re.compile(r"^-?\d+(\.\d+)?$")


def normalize_label(s: str) -> str:
    return re.sub(r"[\s\-]+", "", s.lower())


_NORMALIZED_CATEGORY_LABELS = {normalize_label(l) for l in HOLDINGS_CATEGORY_LABELS}


def is_real_holding_row(row) -> bool:
    """Most AMC tables are Company/Sector/% (3 cols); commodity ETFs (Gold,
    Silver) drop Sector since it doesn't apply -- 2 cols: Company/%."""
    if not row or len(row) < 2:
        return False
    company = row[0]
    pct = row[2] if len(row) >= 3 else row[1]
    if not company or not pct:
        return False
    # Normalize away whitespace/hyphens so "Sub Total", "SubTotal", and
    # "Sub-Total" all match the same exclusion entry -- found via testing
    # that a bare "SubTotal" (no space) slipped past an exact "sub total"
    # match.
    if normalize_label(company.replace("\n", " ")) in _NORMALIZED_CATEGORY_LABELS:
        return False
    pct_clean = str(pct).strip().replace("\n", "")
    return bool(NUMERIC_PCT.match(pct_clean))
