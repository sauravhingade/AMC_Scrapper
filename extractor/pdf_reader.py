"""
Layout-aware PDF reading utilities, shared across all AMC extractors.

Key lesson baked in here (found by testing against a real factsheet):
pdfplumber's extract_text(layout=True) approximates a monospace character
grid and can CLIP words that sit near a crop boundary. The reliable approach
is: extract_words() (gives exact x0/x1/top per word) -> reconstruct lines by
clustering on y-position -> sort each line by x-position. This never drops
characters and is what every function below uses instead of layout=True.
"""

import pdfplumber


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


def get_column_text_by_start(page, x0: float, x1: float, x_tolerance: float = 3) -> str:
    """Extract text from a vertical column slice, filtering by each word's
    START position (x0) instead of physically cropping the page.

    Use this instead of get_column_text() whenever a column's text wraps
    right up against the crop boundary (no clean whitespace gap) -- keeps
    whole words that merely *start* inside the column, even if they render
    a few points past x1, instead of clipping them mid-character. Safe to
    use with a generous x1 (e.g. up to where the next column's content
    actually starts) since it can only ever pull in whole words, never
    partial ones.
    """
    words = [w for w in page.extract_words(x_tolerance=x_tolerance) if x0 <= w["x0"] < x1]
    return reconstruct_lines(words)


def get_column_tables(page, x0: float, x1: float):
    """Extract tables from a vertical column slice of a page."""
    cropped = page.within_bbox((x0, 0, x1, page.height))
    return cropped.extract_tables()


def open_pdf(path: str):
    return pdfplumber.open(path)
