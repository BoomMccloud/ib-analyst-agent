"""
Company lookup script for SEC EDGAR.

Takes a company name or ticker, resolves to CIK, and determines
whether the company is a domestic filer (10-K) or foreign private issuer (20-F).

Usage: python lookup_company.py "Alibaba"
       python lookup_company.py AAPL

Output: JSON to stdout
{
  "company": "Alibaba Group Holding Ltd",
  "ticker": "BABA",
  "cik": "0001577552",
  "filer_type": "foreign",   // "domestic" or "foreign"
  "filing_type": "20-F",     // "10-K" or "20-F"
  "state_of_incorporation": "K3",
  "country": "Hong Kong"
}
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
    # Fallback for local/demo use. SEC EDGAR requires a real contact email in production.
    _contact = "demo@example.com"
    print(
        f"Warning: SEC_CONTACT_EMAIL not set, using fallback '{_contact}'. "
        "Set it for production use.",
        file=sys.stderr,
    )
HEADERS = {"User-Agent": f"SecFilingsAgent {_contact}"}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q=%22{query}%22&forms=10-K,20-F&dateRange=custom&startdt=2020-01-01&enddt=2026-12-31"

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


def lookup_by_ticker(query: str) -> dict | None:
    """Try to find by exact ticker match in SEC company_tickers.json."""
    data = fetch_json(TICKERS_URL)
    query_upper = query.upper().strip()
    for entry in data.values():
        if entry["ticker"].upper() == query_upper:
            return {
                "cik": str(entry["cik_str"]).zfill(10),
                "ticker": entry["ticker"],
                "company": entry["title"],
            }
    return None


def lookup_by_name(query: str) -> dict | None:
    """Search EDGAR full-text index by company name."""
    url = SEARCH_URL.format(query=urllib.request.quote(query))
    data = fetch_json(url)

    if data["hits"]["total"]["value"] == 0:
        return None

    # Get the first unique CIK from results
    for hit in data["hits"]["hits"]:
        src = hit["_source"]
        cik = src["ciks"][0]
        display = src["display_names"][0] if src["display_names"] else ""
        # Extract ticker from display name like "Apple Inc.  (AAPL)  (CIK ...)"
        import re

        ticker_match = re.search(r"\(([A-Z]{1,5})\)", display)
        ticker = ticker_match.group(1) if ticker_match else ""
        company_name = re.split(r"\s+\(", display)[0].strip()
        return {
            "cik": cik,
            "ticker": ticker,
            "company": company_name,
        }

    return None


def get_filer_info(cik: str) -> dict:
    """Fetch submissions metadata to determine filer type."""
    data = fetch_json(SUBMISSIONS_URL.format(cik=cik))

    addr = data.get("addresses", {}).get("business", {})
    is_foreign = bool(addr.get("isForeignLocation"))

    # Double-check by looking at what forms they actually file
    recent_forms = data.get("filings", {}).get("recent", {}).get("form", [])
    has_10k = any(f in ("10-K", "10-K405") for f in recent_forms)
    has_20f = any(f == "20-F" for f in recent_forms)

    # Address-based determination, confirmed by filing history
    if is_foreign or (has_20f and not has_10k):
        filer_type = "foreign"
        filing_type = "20-F"
    else:
        filer_type = "domestic"
        filing_type = "10-K"

    return {
        "filer_type": filer_type,
        "filing_type": filing_type,
        "state_of_incorporation": data.get("stateOfIncorporation", ""),
        "state_of_incorporation_desc": data.get("stateOfIncorporationDescription", ""),
        "country": addr.get("country", ""),
        "tickers": data.get("tickers", []),
        "name": data.get("name", ""),
    }


_ticker_cache: dict | None = None


def _get_ticker_cache() -> dict:
    """Fetch and cache SEC company_tickers.json. First call pays ~1s HTTP round-trip."""
    global _ticker_cache
    if _ticker_cache is None:
        _ticker_cache = fetch_json(TICKERS_URL)
    return _ticker_cache


def search_tickers(query: str, limit: int = 10) -> list[dict]:
    """Case-insensitive substring search across SEC company tickers and names.

    Returns list of {ticker, name, cik} dicts, ranked with exact ticker
    matches first, then substring matches by name.
    """
    data = _get_ticker_cache()
    q = query.strip().upper()
    if not q:
        return []

    exact_matches = []
    name_matches = []

    for entry in data.values():
        ticker = entry["ticker"]
        title = entry["title"]
        cik = str(entry["cik_str"]).zfill(10)

        if ticker.upper() == q:
            exact_matches.append({"ticker": ticker, "name": title, "cik": cik})
        elif q in ticker.upper() or q in title.upper():
            name_matches.append({"ticker": ticker, "name": title, "cik": cik})

    # Exact matches first, then name matches (capped at limit)
    results = exact_matches + name_matches
    return results[:limit]


def main():
    parser = argparse.ArgumentParser(description="Look up company on SEC EDGAR")
    parser.add_argument("query", help="Company name or stock ticker")
    args = parser.parse_args()

    query = args.query.strip()
    print(f"Looking up: {query}", file=sys.stderr)

    # Step 1: Try ticker lookup first (fast, exact match)
    result = lookup_by_ticker(query)
    if result:
        print(
            f"  Found by ticker: {result['company']} (CIK: {result['cik']})",
            file=sys.stderr,
        )
    else:
        # Step 2: Try name search
        print(f"  Not found as ticker, searching by name...", file=sys.stderr)
        result = lookup_by_name(query)
        if not result:
            print(f"Error: Could not find '{query}' on SEC EDGAR", file=sys.stderr)
            sys.exit(1)
        print(
            f"  Found by name: {result['company']} (CIK: {result['cik']})",
            file=sys.stderr,
        )

    # Step 3: Get filer type from submissions metadata
    print(f"  Fetching filer info...", file=sys.stderr)
    filer_info = get_filer_info(result["cik"])

    output = {
        "company": filer_info["name"] or result["company"],
        "ticker": result["ticker"]
        or (filer_info["tickers"][0] if filer_info["tickers"] else ""),
        "cik": result["cik"],
        "filer_type": filer_info["filer_type"],
        "filing_type": filer_info["filing_type"],
        "state_of_incorporation": filer_info["state_of_incorporation"],
        "country": filer_info["country"],
    }

    print(
        f"  Result: {output['filer_type']} filer ({output['filing_type']})",
        file=sys.stderr,
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
