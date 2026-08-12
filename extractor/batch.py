"""
CLI-shaped batch runner, mirroring your downloader's --amc / --all pattern.

Usage:
    python -m extractor.batch --amc "HDFC Mutual Fund"
    python -m extractor.batch --all
"""

import argparse
import json
import os

from .extractor import extract_factsheet_data
from .config import INPUT_DIR, OUTPUT_DIR


def _amc_name_from_filename(filename: str) -> str:
    """
    Best-effort fallback only. In production, pull amc_name from your
    existing amc_tracking_status.xlsx (indexed by the same safe_filename()
    prefix your downloader already uses) instead of re-deriving it here --
    that tracker is the source of truth and avoids this kind of guesswork.
    """
    stem = filename.rsplit(".", 1)[0]
    parts = stem.split("_")
    while parts and (parts[-1].isdigit() or len(parts[-1]) in (8, 10) or parts[-1] in
                     ("Jan", "Feb", "Mar", "Apr", "May", "June", "July", "Aug", "Sep", "Oct", "Nov", "Dec")):
        parts.pop()
    return " ".join(parts).replace("MF", "Mutual Fund").strip()


def process_pdf(pdf_path: str, amc_name: str, factsheet_month: str = "Unknown") -> list[dict]:
    records = extract_factsheet_data(amc_name, pdf_path, factsheet_month)
    return [r.model_dump() for r in records]


def run(amc_filter: str | None = None):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    all_results = []
    seen_content = {}  # content_key -> source_file, for flagging only (never drops data)

    for filename in sorted(os.listdir(INPUT_DIR)):
        if not filename.lower().endswith(".pdf"):
            continue
        amc_name = _amc_name_from_filename(filename)
        if amc_filter and amc_filter.lower() not in amc_name.lower():
            continue

        pdf_path = os.path.join(INPUT_DIR, filename)
        print(f"Processing {filename} ...")
        try:
            results = process_pdf(pdf_path, amc_name)
            possible_dupes = 0
            for r in results:
                r["source_file"] = filename
                # Content-based key -- used ONLY to flag, never to drop.
                # Two different document types (e.g. a short factsheet vs.
                # the full regular factsheet) can legitimately produce
                # identical scheme-level data for the same reporting month;
                # dropping either would silently lose provenance and risk
                # discarding real data if the documents ever DO differ.
                # It's your call which source to prefer, not this script's.
                key = (
                    r["scheme_name"],
                    r["isin"],
                    tuple(h["company"] for h in r["holdings"][:5]),
                )
                if key in seen_content:
                    r["possible_duplicate_of"] = seen_content[key]
                    possible_dupes += 1
                else:
                    r["possible_duplicate_of"] = None
                    seen_content[key] = filename

            all_results.extend(results)
            flagged = sum(1 for r in results if r["needs_review"])
            dupe_note = f", {possible_dupes} flagged as possible_duplicate (kept, not dropped)" if possible_dupes else ""
            print(f"  -> {len(results)} schemes ({flagged} flagged for review{dupe_note})")
        except Exception as e:
            print(f"  !! FAILED: {e}")

    out_path = os.path.join(OUTPUT_DIR, "extracted_schemes.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nWrote {len(all_results)} scheme records -> {out_path}")
    return all_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--amc", type=str, default=None, help="Filter to one AMC by name substring")
    parser.add_argument("--all", action="store_true", help="Process all PDFs in downloads/")
    args = parser.parse_args()
    run(amc_filter=args.amc)
