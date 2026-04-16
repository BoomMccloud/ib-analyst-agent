"""
Agent 1: Filing Fetcher
=======================
Takes a company name or ticker, looks up CIK + filer type, then fetches
the last N years of annual filing URLs.

Flow:
  1. lookup_company.py → determines ticker, CIK, domestic vs foreign
  2. fetch_10k.py or fetch_20f.py → gets filing URLs

Usage: python agent1_fetcher.py BABA [--years 5]
       python agent1_fetcher.py "Alibaba" [--years 5]
       python agent1_fetcher.py AAPL [--years 5]

Output: JSON to stdout
"""

import argparse
import json
import sys

from lookup_company import lookup_by_ticker, lookup_by_name, get_filer_info
from fetch_10k import fetch_10k_filings
from fetch_20f import fetch_20f_filings


def run(query: str, years: int) -> dict:
    print(f"Looking up: {query}", file=sys.stderr)
    result = lookup_by_ticker(query)
    if result:
        print(
            f"  Found by ticker: {result['company']} (CIK: {result['cik']})",
            file=sys.stderr,
        )
    else:
        print(f"  Not found as ticker, searching by name...", file=sys.stderr)
        result = lookup_by_name(query)
        if not result:
            raise RuntimeError(f"Could not find '{query}' on SEC EDGAR")
        print(
            f"  Found by name: {result['company']} (CIK: {result['cik']})",
            file=sys.stderr,
        )

    print(f"  Fetching filer info...", file=sys.stderr)
    filer_info = get_filer_info(result["cik"])

    filing_type = filer_info["filing_type"]
    cik = result["cik"]
    company_name = filer_info["name"] or result["company"]
    ticker = result["ticker"] or (
        filer_info["tickers"][0] if filer_info["tickers"] else ""
    )

    print(
        f"  Result: {filer_info['filer_type']} filer ({filing_type})", file=sys.stderr
    )

    if filing_type == "10-K":
        filings = fetch_10k_filings(cik, years)
    else:
        filings = fetch_20f_filings(cik, years)

    return {
        "company": company_name,
        "ticker": ticker.upper() if ticker else "",
        "cik": cik,
        "filer_type": filer_info["filer_type"],
        "filing_type": filing_type,
        "state_of_incorporation": filer_info["state_of_incorporation"],
        "country": filer_info["country"],
        "filing_count": len(filings),
        "filings": filings,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Agent 1: Fetch SEC filing URLs locally"
    )
    parser.add_argument("query", help="Company name or stock ticker")
    parser.add_argument(
        "--years", type=int, default=5, help="Number of years (default: 5)"
    )
    args = parser.parse_args()

    result = run(args.query, args.years)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
