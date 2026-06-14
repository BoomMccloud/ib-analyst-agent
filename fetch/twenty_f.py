"""
Deterministic SEC EDGAR 20-F fetcher for foreign private issuers.

Usage: python -m fetch.twenty_f BABA [--count 5]
Output: JSON to stdout
"""

import argparse
import json
import sys

from fetch.http import fetch_url

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

def fetch_json(url: str) -> dict:
    return json.loads(fetch_url(url).decode())


def ticker_to_cik(ticker: str) -> tuple[str, str]:
    """Returns (padded_cik, company_name)."""
    data = fetch_json(TICKERS_URL)
    for entry in data.values():
        if entry["ticker"].upper() == ticker.upper():
            cik = str(entry["cik_str"]).zfill(10)
            return cik, entry["title"]
    raise ValueError(f"Ticker '{ticker}' not found in SEC company tickers")


def build_filing_url(cik: str, accession: str, primary_doc: str) -> str:
    cik_num = cik.lstrip("0")
    accession_no_dashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_num}/"
        f"{accession_no_dashes}/{primary_doc}"
    )


def fetch_20f_filings(cik: str, count: int) -> list[dict]:
    """Fetch the most recent `count` 20-F filings for a given CIK, handling pagination."""
    url = SUBMISSIONS_URL.format(cik=cik)
    data = fetch_json(url)

    recent = data["filings"]["recent"]
    seen_accessions = set()
    
    def extract_from_flat_dict(flat_dict, list_dest):
        forms = flat_dict.get("form", [])
        dates = flat_dict.get("filingDate", [])
        periods = flat_dict.get("reportDate", [])
        accessions = flat_dict.get("accessionNumber", [])
        primary_docs = flat_dict.get("primaryDocument", [])
        
        for i, form in enumerate(forms):
            if form == "20-F":
                acc = accessions[i]
                if acc in seen_accessions:
                    continue
                seen_accessions.add(acc)
                list_dest.append({
                    "form": form,
                    "filing_date": dates[i],
                    "period_of_report": periods[i],
                    "accession_number": acc,
                    "primary_document": primary_docs[i],
                    "url": build_filing_url(cik, acc, primary_docs[i]),
                })
                if len(list_dest) >= count:
                    return True
        return False

    filings = []
    # 1. Try extracting from recent
    if extract_from_flat_dict(recent, filings):
        return filings
        
    # 2. If we need more, check files
    files = data["filings"].get("files", [])
    files_sorted = sorted(files, key=lambda x: x.get("filingTo", ""), reverse=True)
    
    for f_info in files_sorted:
        f_name = f_info["name"]
        f_url = f"https://data.sec.gov/submissions/{f_name}"
        f_data = fetch_json(f_url)
        if extract_from_flat_dict(f_data, filings):
            break
            
    return filings


def main():
    parser = argparse.ArgumentParser(description="Fetch 20-F filings for a foreign private issuer")
    parser.add_argument("ticker", help="Stock ticker (e.g., BABA)")
    parser.add_argument("--count", type=int, default=5, help="Number of filings (default: 5)")
    args = parser.parse_args()

    cik, name = ticker_to_cik(args.ticker)
    print(f"Company: {name} (CIK: {cik})", file=sys.stderr)

    filings = fetch_20f_filings(cik, args.count)
    print(f"Found {len(filings)} 20-F filings", file=sys.stderr)

    result = {
        "company": name,
        "ticker": args.ticker.upper(),
        "cik": cik,
        "filer_type": "Foreign Private Issuer",
        "filing_count": len(filings),
        "filings": filings,
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
