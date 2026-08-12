"""
Structural validation -- no LLM involved. "Confidence" here means
"did the deterministic extraction find what it expected", not a model's
self-reported certainty.
"""

from pydantic import BaseModel, Field


class FundManager(BaseModel):
    role: str
    name: str
    sleeve: str | None = None  # "Equity"/"Debt"/"Commodity" for multi-manager schemes


class Holding(BaseModel):
    company: str
    sector: str = ""
    pct_to_net_assets: str


class SchemeRecord(BaseModel):
    amc_name: str
    scheme_name: str
    factsheet_month: str
    benchmark: str | None = None
    additional_benchmark: str | None = None  # some AMCs (e.g. HDFC) report a
    # secondary/"tier-2" benchmark alongside the primary one. None for AMCs
    # (e.g. 360 ONE) whose factsheets don't carry this field -- not an error.
    isin: str = ""
    fund_managers: list[FundManager] = Field(default_factory=list)
    holdings: list[Holding] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    def flag_issues(self):
        reasons = []
        if self.benchmark is None:
            reasons.append("benchmark_not_found")
        if not self.fund_managers:
            reasons.append("fund_manager_not_found")
        if not self.holdings:
            reasons.append("holdings_table_not_found")
        else:
            total_pct = sum(
                float(h.pct_to_net_assets) for h in self.holdings
                if h.pct_to_net_assets.replace(".", "", 1).lstrip("-").isdigit()
            )
            # Holdings should sum to roughly the equity/debt sub-portfolio %,
            # not necessarily 100 (hybrid funds split equity/debt) -- so we
            # only flag wildly implausible totals, not exact mismatches.
            if total_pct > 105:
                reasons.append(f"holdings_pct_sum_suspicious({total_pct:.1f})")
        self.review_reasons = reasons
        self.needs_review = bool(reasons)
        return self
