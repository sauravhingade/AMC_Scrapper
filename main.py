import argparse
import json

from scraper.db import get_all_sites, get_sites_by_name
from scraper.downloader import download_factsheet
from scraper.tracker import write_tracking_excel


def parse_steps(site):
    """interaction_steps comes back from MySQL JSON column as a string
    (or None). Parse it once here so downloader.py just gets a plain
    Python list/None."""
    raw = site.get("interaction_steps")
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            print(
                f"WARNING: interaction_steps for {site['amc_name']} is not valid JSON, ignoring it."
            )
            return None
    return raw  # already a list/dict, e.g. connector already decoded it


def run(sites):
    if not sites:
        print("No matching AMC found in the database.")
        return

    results = []

    for site in sites:
        name = site["amc_name"]
        url = site["downloads_page_url"]
        steps = parse_steps(site)
        print(f"\n=== Testing: {name} ===")
        print(f"URL: {url}")

        result = {"name": name, "url": url, "status": "FAIL", "files": [], "error": ""}

        try:
            files = download_factsheet(
                name,
                url,
                interaction_steps=steps,
            )
            if not files:
                print("No files downloaded.")
                result["error"] = "No files downloaded."
            else:
                for filepath in files:
                    print(f"SUCCESS -> saved to {filepath}")
                result["status"] = "PASS"
                result["files"] = files

        except (OSError, ValueError, RuntimeError) as e:
            print(f"FAILED -> {e}")
            result["error"] = str(e)

        except Exception as e:
            # Catch-all so one AMC's crash (a bad selector, a Playwright
            # error, anything unforeseen) never takes down the rest of
            # the batch -- log it against this AMC only, and move on.
            print(f"FAILED (unexpected) -> {e}")
            result["error"] = str(e)

        results.append(result)

    write_tracking_excel(results)


def main():
    parser = argparse.ArgumentParser(
        description="Download mutual fund factsheets by AMC."
    )
    parser.add_argument(
        "--amc", help="Partial AMC name to test, e.g. '360' or 'abakkus'"
    )
    parser.add_argument(
        "--all", action="store_true", help="Run against all AMCs in the DB"
    )
    args = parser.parse_args()

    if args.all:
        run(get_all_sites())
    elif args.amc:
        run(get_sites_by_name(args.amc))
    else:
        print('Usage: python main.py --amc "360"   (or)   python main.py --all')


if __name__ == "__main__":
    main()
