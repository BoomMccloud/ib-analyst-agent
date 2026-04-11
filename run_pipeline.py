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

def try_command(cmd, **kwargs):
    """Run a command, returning (stdout, True) on success or (stderr, False) on failure.
    Unlike run_command(), does NOT sys.exit on failure.
    """
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(f"Command failed: {result.stderr}", file=sys.stderr)
        return result.stderr, False
    return result.stdout, True

def main():
    parser = argparse.ArgumentParser(description="Full SEC Modeling Pipeline")
    parser.add_argument("query", help="Company ticker or name (e.g. AAPL)")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--outdir", default="./pipeline_output")
    args = parser.parse_args()

    out_dir = Path(args.outdir)
    out_dir.mkdir(exist_ok=True)

    # Stage 1: Fetch filings
    print(f"=== STAGE 1: Fetching {args.years} years of filings for {args.query} ===")
    filings_json = run_command([sys.executable, "agent1_fetcher.py", args.query,
                                 "--years", str(args.years)])
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

    # Stage 2: Process each filing
    tree_files = []        # Phase 2 path outputs

    for i, filing in enumerate(filings):
        url = filing.get("url")
        date = filing.get("filing_date", f"filing_{i}")
        if not url:
            continue

        print(f"\n=== STAGE 2: Processing filing {i+1}/{len(filings)} ({date}) ===")

        # Tree path: xbrl_tree.py
        tree_file = out_dir / f"trees_{date}.json"
        _, ok = try_command([sys.executable, "xbrl_tree.py", "--url", url,
                              "-o", str(tree_file)])

        if ok and tree_file.exists():
            tree_files.append(str(tree_file))
            print(f"  XBRL tree extraction succeeded for {date}")
        else:
            print(f"  XBRL tree extraction failed for {date}")

    # Stage 3+4: Verify + render
    if tree_files:
        print(f"\n=== STAGE 3: Verifying model ===")
        # Checkpoint: verify tree completeness + cross-statement invariants
        for tf in tree_files:
            import json as _json
            from xbrl_tree import verify_tree_completeness, TreeNode
            with open(tf) as _f:
                _trees = _json.load(_f)
            for stmt in ["IS", "BS", "BS_LE", "CF"]:
                if stmt in _trees and isinstance(_trees[stmt], dict):
                    _trees[stmt] = TreeNode.from_dict(_trees[stmt])
            _periods = _trees.get("complete_periods", [])
            _all_errors = []
            for stmt in ["IS", "BS", "BS_LE", "CF"]:
                if _trees.get(stmt):
                    _all_errors.extend(verify_tree_completeness(_trees[stmt], _periods))
            if _all_errors:
                print(f"  Tree completeness: {len(_all_errors)} gap(s):", file=sys.stderr)
                for concept, period, gap in _all_errors:
                    print(f"    {concept[:50]:50s} {period} gap={gap:>10,.0f}", file=sys.stderr)
                print("  WARNING: Tree gaps detected — sheet formulas may not match declared values",
                      file=sys.stderr)
            else:
                print(f"  Tree completeness: ALL PASS")
            # Cross-statement invariants
            run_command([sys.executable, "pymodel.py", "--trees", tf, "--checkpoint"])

        print(f"\n=== STAGE 4: Writing Google Sheet (Phase 3) ===")
        # sheet_builder uses the first tree file (most recent filing)
        run_command([sys.executable, "sheet_builder.py", "--trees", tree_files[0],
                      "--company", company_name])
        
        print(f"\n=== STAGE 5: Forecasting (Phase 4 - Coming Soon) ===")
        print(f"Forecasting logic to be implemented in Phase 4.")
    else:
        print("No filings were successfully processed.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
