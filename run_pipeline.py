"""
Agentic 4-Stage Financial Modeling Pipeline
===========================================
Orchestrates the full flow from ticker to Google Sheet.
Defaults to 5 years of historical data and 5 years of forecasts.

Usage: python run_pipeline.py AAPL
       from run_pipeline import run_pipeline; run_pipeline("AAPL")
"""

import argparse
import json
import sys
from pathlib import Path

from agent1_fetcher import run as fetch_filings
from sec_utils import fetch_url
from xbrl import build_statement_trees
from merge_trees import merge_filing_trees
from pymodel import run_checkpoint
from sheets import write_sheets


def run_pipeline(query, years=5, outdir="./pipeline_output", on_progress=None):
    """Run the full SEC modeling pipeline in-process.

    Args:
        query: Company ticker or name (e.g. "AAPL", "Apple Inc.")
        years: Number of years of filings to process (default 5)
        outdir: Directory for intermediate JSON outputs
        on_progress: Optional callback(stage: str, msg: str) called at each stage boundary

    Returns:
        dict with keys: sheet_url (str), company_name (str)

    Raises:
        RuntimeError on any failure (no sys.exit).
    """
    if on_progress is None:
        on_progress = lambda stage, msg: None

    out_dir = Path(outdir)
    out_dir.mkdir(exist_ok=True)

    # Stage 1: Fetch filings
    on_progress("fetching", f"Looking up {query}...")
    filings_data = fetch_filings(query, years)
    filings = filings_data.get("filings", [])
    if not filings:
        raise RuntimeError(f"No filings found for {query}")

    company_name = filings_data.get("company", query)
    on_progress("fetching", f"Found {len(filings)} filings for {company_name}")

    # Stage 2: Build XBRL trees for each filing
    tree_files = []
    for i, filing in enumerate(filings):
        url = filing.get("url")
        date = filing.get("filing_date", f"filing_{i}")
        if not url:
            continue

        on_progress(
            "building_trees", f"Processing filing {i + 1}/{len(filings)} ({date})"
        )
        html = fetch_url(url).decode("utf-8", errors="replace")
        base_url = url.rsplit("/", 1)[0] + "/"

        result = build_statement_trees(html, base_url)
        if result is None:
            on_progress("building_trees", f"  No XBRL linkbase for {date}, skipping")
            continue

        tree_file = out_dir / f"trees_{date}.json"
        out = {}
        for key in ["complete_periods", "periods", "cf_endc_values", "unit_label"]:
            if key in result:
                out[key] = result[key]
        out["facts"] = result.get("facts", {})
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = result.get(stmt)
            if tree:
                out[stmt] = tree.to_dict()
        rev_seg = result.get("revenue_segments")
        if rev_seg:
            out["revenue_segments"] = rev_seg.to_dict()

        with open(tree_file, "w") as f:
            json.dump(out, f, indent=2)
        tree_files.append(str(tree_file))
        on_progress("building_trees", f"  Tree saved for {date}")

    if not tree_files:
        raise RuntimeError(f"No XBRL trees built for {query}")

    # Stage 3: Merge trees
    if len(tree_files) > 1:
        on_progress("merging", f"Merging {len(tree_files)} filings")
        merged = merge_filing_trees(tree_files)
    else:
        on_progress("merging", "Single filing, no merge needed")
        with open(tree_files[0]) as f:
            merged = json.load(f)

    merged_file = str(out_dir / "merged.json")
    with open(merged_file, "w") as f:
        json.dump(merged, f, indent=2)

    # Stage 4: Verify invariants (checkpoint)
    on_progress("checkpoint", "Running cross-statement invariant checks")
    result = run_checkpoint(merged)
    if not result.passed:
        raise RuntimeError(f"verify_model: {result.first_error}")
    on_progress("checkpoint", f"ALL PASS ({len(result.periods)} periods)")

    # Stage 5: Write Google Sheet
    on_progress("writing_sheet", f"Creating Google Sheet for {company_name}")
    sid, url = write_sheets(merged, company_name)
    on_progress("done", f"Sheet ready: {url}")

    return {"sheet_url": url, "company_name": company_name}


def main():
    parser = argparse.ArgumentParser(description="Full SEC Modeling Pipeline")
    parser.add_argument("query", help="Company ticker or name (e.g. AAPL)")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--outdir", default="./pipeline_output")
    args = parser.parse_args()

    try:
        result = run_pipeline(
            args.query,
            args.years,
            args.outdir,
            on_progress=lambda stage, msg: print(f"[{stage}] {msg}"),
        )
        print(f"\nDone! Sheet: {result['sheet_url']}")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
