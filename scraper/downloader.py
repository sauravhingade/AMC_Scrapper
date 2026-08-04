"""
Main entry point.

    from downloader import download_factsheet

    download_factsheet(
        amc_name="Axis Mutual Fund",
        page_url="https://...",
        interaction_steps=[...],   # optional, AMC-specific
    )

download_factsheet() looks at which bucket the AMC falls into (see
config.py), runs the matching scrape strategy to find the latest PDF
link(s), then downloads each one to DOWNLOAD_DIR.
"""

import os
from urllib.parse import parse_qs, unquote, urlparse

import urllib3

from .amc_specific import (
    fallback_direct_download,
    fallback_direct_download_bandhan,
    fallback_get_latest_pdf_links_new,
    get_rendered_html,
    get_sundaram_latest_pdf_links,
    prepare_kotak_latest_factsheet,
)
from .config import (
    DEBUG,
    DOWNLOAD_DIR,
    amc_need_date_selection,
    amc_need_month_selection_with_date_picker,
    amc_with_download_fallback,
    amc_with_genric_fallback,
)
from .utils import download_pdf_bytes, find_pdf_links, pick_latest_links, safe_filename

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def download_factsheet(amc_name: str, page_url: str, interaction_steps=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print(f"amc name : {amc_name}")

    # -----------------------------------------
    # Determine latest PDF links
    # -----------------------------------------
    if amc_name.lower() in amc_with_download_fallback:
        if DEBUG:
            print(f"Using generic direct download fallback for {amc_name}")

        return fallback_direct_download(
            page_url,
            amc_name,
            interaction_steps,
        )
    elif amc_name.lower() == "bandhan mutual fund":
        if DEBUG:
            print(f"Using generic direct download fallback for {amc_name}")

        return fallback_direct_download_bandhan(
            page_url,
            amc_name,
            interaction_steps,
        )

    elif amc_name.lower() in amc_need_date_selection:
        if DEBUG:
            print(f"Using prepare factsheet by date selection{amc_name}")
        # cannot automate kotak as its protected by radware bot detection
        pdf_links, link_contexts = prepare_kotak_latest_factsheet(
            page_url, interaction_steps, amc_name=amc_name
        )
        latest_links = pick_latest_links(pdf_links, link_contexts)

    elif amc_name.lower() in amc_with_genric_fallback:
        if DEBUG:
            print(f"Using generic fallback for {amc_name}")

        pdf_links, link_contexts = fallback_get_latest_pdf_links_new(
            page_url,
            interaction_steps,
            amc_name=amc_name,
        )

        latest_links = pick_latest_links(
            pdf_links,
            link_contexts,
        )

    elif amc_name.lower() in amc_need_month_selection_with_date_picker:
        if DEBUG:
            print(f"Using date selection for {amc_name}")
            # cannot automate kotak as its protected by radware bot detection
        pdf_links = get_sundaram_latest_pdf_links(
            page_url, interaction_steps, amc_name=amc_name
        )
        latest_links = pick_latest_links(pdf_links)

    else:
        if DEBUG:
            print(f"Using rendered HTML extraction for {amc_name}")

        html = get_rendered_html(
            amc_name,
            page_url,
            interaction_steps=interaction_steps,
        )

        pdf_links, link_contexts = find_pdf_links(
            page_url,
            html,
        )

        latest_links = pick_latest_links(
            pdf_links,
            link_contexts,
        )

    if not latest_links:
        raise ValueError("No relevant factsheet PDFs found.")

    # -----------------------------------------
    # Download PDFs
    # -----------------------------------------
    saved_files = []

    for pdf_url in latest_links:
        try:
            content = download_pdf_bytes(
                pdf_url, referer_url=page_url, amc_name=amc_name
            )
        except Exception as e:
            print(f"Failed to download {pdf_url}: {e}")
            continue

        filename = unquote(
            parse_qs(urlparse(pdf_url).query).get(
                "file", [os.path.basename(urlparse(pdf_url).path)]
            )[0]
        )

        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        filename = f"{safe_filename(amc_name)}_{filename}"

        filepath = os.path.join(
            DOWNLOAD_DIR,
            filename,
        )

        with open(filepath, "wb") as f:
            f.write(content)

        saved_files.append(filepath)

    return saved_files
