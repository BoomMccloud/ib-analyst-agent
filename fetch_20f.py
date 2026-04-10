"""
Deterministic SEC EDGAR 20-F fetcher for foreign private issuers.

Usage: python fetch_20f.py BABA [--count 5]
Output: JSON to stdout
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

_contact = os.environ.get("SEC_CONTACT_EMAIL")
if not _contact:
    print("Error: SEC_CONTACT_EMAIL environment variable must be set (SEC EDGAR requires a real contact email)", file=sys.stderr)
    sys.exit(1)
HEADERS = {"User-Agent": f"SecFilingsAgent {_contact}"}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

REQUEST_INTERVAL = 1.0 / 8
_last_request_time = 0.0


def _throttle():
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def fetch_url(url: str, retries: int = 5) -> bytes:
    for attempt in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise


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
    """Fetch the most recent `count` 20-F filings for a given CIK."""
    data = fetch_json(SUBMISSIONS_URL.format(cik=cik))
    recent = data["filings"]["recent"]

    filings = []
    for i, form in enumerate(recent["form"]):
        if form == "20-F":
            filings.append({
                "form": form,
                "filing_date": recent["filingDate"][i],
                "period_of_report": recent["reportDate"][i],
                "accession_number": recent["accessionNumber"][i],
                "primary_document": recent["primaryDocument"][i],
                "url": build_filing_url(cik, recent["accessionNumber"][i], recent["primaryDocument"][i]),
            })
            if len(filings) >= count:
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
