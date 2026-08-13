"""
extract_factsheet_data() is the single entry point -- same call shape as
your scraper's download_factsheet(), just no download step:

    records = extract_factsheet_data("HDFC Mutual Fund", pdf_path, "June 2026")
    # -> list[SchemeRecord], one per scheme found in the PDF
"""

from .amc import get_extractor
from .pdf_reader import open_pdf
from .schema import FundManager, Holding, SchemeRecord
from .segmenter import segment_schemes


def extract_factsheet_data(
    amc_name: str, pdf_path: str, factsheet_month: str
) -> list[SchemeRecord]:
    records: list[SchemeRecord] = []
    extractor_module = get_extractor(amc_name)
    print(f"extractor module : {extractor_module}")

    with open_pdf(pdf_path) as pdf:
        scheme_pages = segment_schemes(pdf)

        for scheme_name, page_idxs in scheme_pages.items():
            if not page_idxs:
                continue
            fields = extractor_module.extract_scheme_fields(pdf, page_idxs)

            record = SchemeRecord(
                amc_name=amc_name,
                scheme_name=scheme_name.title(),
                factsheet_month=factsheet_month,
                benchmark=fields["benchmark"],
                additional_benchmark=fields.get("additional_benchmark"),
                isin=fields["isin"],
                fund_managers=[FundManager(**m) for m in fields["fund_managers"]],
                holdings=[Holding(**h) for h in fields["holdings"]],
            ).flag_issues()

            records.append(record)

    return records
