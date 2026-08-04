"""
Constants shared across the factsheet scraper: AMC classification sets
(which scraping strategy each AMC needs), HTTP headers, timeouts, and
month name/number lookups.
"""

import re

# -----------------------------------------------------------------------
# AMC classification -- which scraping strategy each AMC needs
# -----------------------------------------------------------------------

amc_with_genric_fallback = {
    "360 one mutual fund",
    "axis mutual fund",
    "choice mutual fund",
    "mahindra manulife mutual fund",
    "pgim india mutual fund",
    "trust mutual fund",
    "the wealth company mutual fund",
    # "hdfc mutual fund",
    "icici prudential mutual fund",
}
amc_with_download_fallback = {
    "iti mutual fund",
}

amc_need_month_selection_with_antnative = {"jio blackrock mutual fund"}
amc_need_month_selection_with_selectnative = {"navi mutual fund"}
amc_need_month_selection_with_date_picker = {"sundaram mutual fund"}
amc_need_date_selection = {"kotak mahindra mutual fund"}
amc_need_month_selection_tata = {"tata mutual fund"}
amc_need_year_selection = {
    "taurus mutual fund",
}
amc_need_slow_load = {"sbi mutual fund"}

# -----------------------------------------------------------------------
# AMCs that need a real (headed) browser window instead of headless.
#
# Some sites' WAF / anti-bot checks (Cloudflare, etc.) behave
# differently -- or outright block -- headless Chromium, even with
# "--headless=new". Rather than flipping the global HEADLESS flag for
# everyone, these AMCs are special-cased to always run headed while
# every other AMC keeps using the global default below.
# -----------------------------------------------------------------------

AMC_NEED_HEADFUL = {
    "edelweiss mutual fund",
    "hdfc mutual fund",
    "icici prudential mutual fund",
    "unifi mutual fund",
    # "sbi mutual fund",
}


def get_headless(amc_name: str | None) -> bool:
    """
    Resolve the effective headless mode for a given AMC.

    Returns False (i.e. run headed) for any AMC in AMC_NEED_HEADFUL,
    regardless of the global HEADLESS setting. Falls back to the
    global HEADLESS for every other AMC, or when amc_name is None
    (e.g. call sites that haven't been updated to pass it yet).
    """
    if amc_name and amc_name.strip().lower() in AMC_NEED_HEADFUL:
        return False
    return HEADLESS


# -----------------------------------------------------------------------
# HTTP / browser settings
# -----------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

HEADLESS = True
DOWNLOAD_DIR = "downloads"
PAGE_TIMEOUT_MS = 100000
DOWNLOAD_TIMEOUT_S = 20
DEBUG = True  # set False to silence link-scoring / trace logs

# -----------------------------------------------------------------------
# Month lookups
# -----------------------------------------------------------------------

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

months = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

MONTH_ALIASES = {
    "jan": ["jan", "january"],
    "feb": ["feb", "february"],
    "mar": ["mar", "march"],
    "apr": ["apr", "april"],
    "may": ["may"],
    "jun": ["jun", "june"],
    "jul": ["jul", "july"],
    "aug": ["aug", "august"],
    "sep": ["sep", "sept", "september"],
    "oct": ["oct", "october"],
    "nov": ["nov", "november"],
    "dec": ["dec", "december"],
}

month_pattern = re.compile(
    r"\b("
    r"jan(?:uary)?|"
    r"feb(?:ruary)?|"
    r"mar(?:ch)?|"
    r"apr(?:il)?|"
    r"may|"
    r"jun(?:e)?|"
    r"jul(?:y)?|"
    r"aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|"
    r"nov(?:ember)?|"
    r"dec(?:ember)?"
    r")\b",
    re.IGNORECASE,
)
MONTH_NAMES_PATTERN = "|".join(MONTHS.keys())
