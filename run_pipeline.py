"""
Agentic 4-Stage Financial Modeling Pipeline
===========================================
Orchestrates the full flow from ticker to Google Sheet.
Defaults to 5 years of historical data and 5 years of forecasts.

Usage: python run_pipeline.py AAPL
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

def run_command(cmd, input_data=None, capture_output=True):
    """Run a shell command and return stdout."""
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=capture_output,
        text=True
    )
    if result.returncode != 0:
        print(f"Error running {' '.join(cmd)}:\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)
    return result.stdout

def main():
    parser = argparse.ArgumentParser(description="Full Agentic SEC Modeling Pipeline")
    parser.add_argument("query", help="Company ticker or name (e.g. AAPL)")
    parser.add_argument("--years", type=int, default=5, help="Number of years (default: 5)")
    parser.add_argument("--outdir", default="./pipeline_output", help="Temporary output directory")
    args = parser.parse_args()

    out_dir = Path(args.outdir)
    out_dir.mkdir(exist_ok=True)

    print(f"=== STAGE 1: Fetching {args.years} years of filings for {args.query} ===")
    filings_json = run_command([sys.executable, "agent1_fetcher.py", args.query, "--years", str(args.years)])
    
    try:
        filings_data = json.loads(filings_json)
    except json.JSONDecodeError:
        print("Error: Could not parse output from agent1_fetcher.py", file=sys.stderr)
        sys.exit(1)

    filings = filings_data.get("filings", [])
    if not filings:
        print(f"No filings found for {args.query}")
        sys.exit(1)

    company_name = filings_data.get("company_name", args.query)
    print(f"Processing {len(filings)} filings for {company_name}...")

    structured_files = []

    for i, filing in enumerate(filings):
        url = filing.get("url")
        date = filing.get("filing_date", f"filing_{i}")
        if not url:
            continue

        print(f"\n=== STAGE 2: Processing filing {i+1}/{len(filings)} ({date}) ===")
        
        # 2a: Extract
        filing_dir = out_dir / f"sections_{date}"
        run_command([sys.executable, "extract_sections.py", url, "--output-dir", str(filing_dir)])
        
        # 2b: Structure
        struct_file = out_dir / f"structured_{date}.json"
        run_command([sys.executable, "structure_financials.py", str(filing_dir), "-o", str(struct_file)])
        structured_files.append(str(struct_file))

    print(f"\n=== STAGE 3 & 4: Building and Verifying Model for {company_name} ===")
    # Call pymodel.py with all structured files
    cmd = [sys.executable, "pymodel.py", "--company", company_name, "--financials"] + structured_files
    final_output = run_command(cmd)
    
    try:
        final_data = json.loads(final_output.splitlines()[-1])
        print(f"\nSUCCESS!")
        print(f"Company: {final_data.get('company')}")
        print(f"Google Sheet URL: {final_data.get('url')}")
    except Exception:
        print("\nModel produced, but could not parse final summary JSON.")
        print(final_output)

if __name__ == "__main__":
    main()
