"""
AMC extractor registry.

Each AMC module (three_sixty_one.py, hdfc.py, ...) is self-contained: it
owns its own sidebar width, label patterns, and holdings-table strategy,
and exposes one function with a consistent signature:

    extract_scheme_fields(pdf, page_idxs: list[int]) -> dict

...returning {"benchmark", "additional_benchmark", "isin", "fund_managers",
"holdings", "holdings_count"}.

To add AMC #3 (and #4...#50): write a new module here following the same
shape as hdfc.py, register it below, and test it against a real PDF from
that AMC before trusting it -- same "don't pre-guess, add once tested"
discipline the old AMC_LAYOUTS config comment already called for. Copy
whichever existing module's layout looks closest as a starting point.
"""

from . import (
    abakkus,
    aditya_birla,
    angel_one,
    axis,
    bajaj_finserv,
    hdfc,
    three_sixty_one,
    bandhan,
    bank_of_india,
    canara_robeco,
    baroda_bnp_paribas,
    capitalmind,
    choice,
    edelweiss,
    dsp,
    franklin_templeton,
    helios,
    groww,
)

REGISTRY = [
    ("360 ONE Mutual Fund", three_sixty_one),
    ("360 One", three_sixty_one),
    ("HDFC Mutual Fund", hdfc),
    ("HDFC", hdfc),
    ("Abakkus Mutual Fund", abakkus),
    ("Abakkus", abakkus),
    ("Aditya Birla", aditya_birla),
    ("Aditya", aditya_birla),
    ("Angel one", angel_one),
    ("Angel", angel_one),
    ("Axis", axis),
    ("bajaj finserv", bajaj_finserv),
    ("Bandhan", bandhan),
    ("Bank of india", bank_of_india),
    ("Canara Robeco", canara_robeco),
    ("Baroda BNP", baroda_bnp_paribas),
    ("Capitalmind", capitalmind),
    ("choice", choice),
    ("Edelweiss", edelweiss),
    ("DSP", dsp),
    ("Franklin Templeton", franklin_templeton),
    ("Helios", helios),
    ("Groww", groww),
]

# Fallback for any AMC without its own module yet. Deliberately NOT silent:
# a mismatched extractor will fail to find benchmark/managers/holdings, and
# schema.flag_issues() will mark every resulting record needs_review=True --
# so an unconfigured AMC surfaces as a pile of flagged records, not as a
# crash or (worse) plausible-looking wrong data. That's your signal to go
# write a real module for it.
DEFAULT = three_sixty_one


def get_extractor(amc_name: str):
    """Fuzzy-matches amc_name against registered AMC modules (substring,
    case-insensitive, either direction) -- same matching style as the old
    get_amc_layout()."""
    for key, module in REGISTRY:
        if key.lower() in amc_name.lower() or amc_name.lower() in key.lower():
            return module
    return DEFAULT
