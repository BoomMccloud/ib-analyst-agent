#!/usr/bin/env python3
import os
import subprocess
import sys
import json
from pathlib import Path

# Enable recording mode so sec_utils.py saves network responses to fixtures
os.environ["SEC_RECORD_FIXTURES"] = "1"

TICKERS = ["NFLX", "AAPL", "MSFT", "AMZN", "GOOG", "META", "TSLA", "JPM", "BRK-B", "PFE"]

def main():
    print("Starting fixture download...")
    
    for ticker in TICKERS:
        print(f"\n--- Processing {ticker} ---")
        try:
            # 1. Fetch the 10-K URL
            print("Fetching 10-K URL...")
            result = subprocess.run(
                [sys.executable, "fetch_10k.py", ticker, "--count", "1"],
                capture_output=True, text=True, check=True
            )
            # Parse output to get URL
            data = json.loads(result.stdout)
            url = data["filings"][0]["url"]
            print(f"URL: {url}")
            
            # 2. Extract sections (fetches HTML and triggers sec_utils to cache/record)
            print("Extracting sections (triggers HTML fetch)...")
            sections_dir = f"tests/fixtures/sec_filings/{ticker}/sections"
            subprocess.run(
                [sys.executable, "extract_sections.py", url, "--output-dir", sections_dir],
                check=True
            )
            
            # 3. xbrl_tree.py (fetches the cal and pre linkbases, triggering sec_utils to record)
            print("Building trees (triggers XML fetches)...")
            trees_file = f"tests/fixtures/sec_filings/{ticker}/trees.json"
            subprocess.run(
                [sys.executable, "xbrl_tree.py", "--url", url, "-o", trees_file],
                check=True
            )
            
            print(f"{ticker} complete.")
        except subprocess.CalledProcessError as e:
            print(f"Error processing {ticker}: {e}", file=sys.stderr)
            if e.stdout:
                print(e.stdout, file=sys.stderr)
            if e.stderr:
                print(e.stderr, file=sys.stderr)
        except Exception as e:
            print(f"Error processing {ticker}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
