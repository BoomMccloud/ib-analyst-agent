"""
Deterministic SEC EDGAR 10-K fetcher.
Uses only stdlib so it runs in any Python environment without pip install.

Usage: python fetch_10k.py AAPL [--count 5]
Output: JSON to stdout
"""

import json
import sys
import time
import urllib.request
import urllib.error
import argparse


HEADERS = {"User-Agent": "SecFilingsAgent admin@example.com"}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# SEC EDGAR rate limit: 10 req/s. We stay under at 8 req/s.
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


def fetch_10k_filings(cik: str, count: int) -> list[dict]:
    """Fetch the most recent `count` 10-K filings for a given CIK."""
    url = SUBMISSIONS_URL.format(cik=cik)
    data = fetch_json(url)

    recent = data["filings"]["recent"]
    forms = recent["form"]
    dates = recent["filingDate"]
    periods = recent["reportDate"]
    accessions = recent["accessionNumber"]
    primary_docs = recent["primaryDocument"]

    filings = []
    for i, form in enumerate(forms):
        if form in ("10-K", "10-K405"):
            accession_no_dashes = accessions[i].replace("-", "")
            cik_num = cik.lstrip("0")
            link = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_num}/"
                f"{accession_no_dashes}/{primary_docs[i]}"
            )
            filings.append({
                "form": form,
                "filing_date": dates[i],
                "period_of_report": periods[i],
                "accession_number": accessions[i],
                "primary_document": primary_docs[i],
                "url": link,
            })
            if len(filings) >= count:
                break

    return filings


def main():
    parser = argparse.ArgumentParser(description="Fetch recent 10-K filings from SEC EDGAR")
    parser.add_argument("ticker", help="Stock ticker (e.g., AAPL)")
    parser.add_argument("--count", type=int, default=5, help="Number of filings to fetch (default: 5)")
    args = parser.parse_args()

    cik, name = ticker_to_cik(args.ticker)

    filings = fetch_10k_filings(cik, args.count)

    result = {
        "company": name,
        "ticker": args.ticker.upper(),
        "cik": cik,
        "filing_count": len(filings),
        "filings": filings,
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
