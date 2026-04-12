#!/usr/bin/env python3
"""
Validate the multi-year merge pipeline on 10 companies.
Runs: fetch → xbrl_tree (5 years) → merge_trees → pymodel checkpoint → sheet_builder
Reports pass/fail per ticker and any residual warnings.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TICKERS = ["NFLX", "AAPL", "MSFT", "AMZN", "GOOG", "META", "TSLA", "JPM", "BRK-B", "PFE"]

ROOT = Path(__file__).resolve().parent.parent
OUT_BASE = ROOT / "pipeline_output" / "validation"


def run(cmd, **kwargs):
    """Run command, return (stdout, stderr, returncode)."""
    print(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), **kwargs)
    return r.stdout, r.stderr, r.returncode


def validate_ticker(ticker, out_dir):
    """Run the full pipeline for one ticker. Returns (status, details)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {"stages": {}, "warnings": [], "errors": []}

    # Stage 1: Fetch filings (use fetch_10k.py directly — no LLM agent needed)
    stdout, stderr, rc = run([sys.executable, "fetch_10k.py", ticker, "--count", "5"])
    if rc != 0:
        results["stages"]["fetch"] = "FAIL"
        results["errors"].append(f"fetch failed: {stderr[:500]}")
        return "FAIL", results
    results["stages"]["fetch"] = "PASS"

    try:
        filings_data = json.loads(stdout)
    except json.JSONDecodeError:
        results["stages"]["fetch"] = "FAIL"
        results["errors"].append(f"fetch output not JSON: {stdout[:200]}")
        return "FAIL", results

    filings = filings_data.get("filings", [])
    company_name = filings_data.get("company", ticker)
    results["num_filings"] = len(filings)

    # Stage 2: Build trees per filing
    tree_files = []
    for i, filing in enumerate(filings):
        url = filing.get("url")
        date = filing.get("filing_date", f"filing_{i}")
        if not url:
            continue
        tree_file = out_dir / f"trees_{date}.json"
        stdout, stderr, rc = run([sys.executable, "xbrl_tree.py", "--url", url, "-o", str(tree_file)])
        if rc == 0 and tree_file.exists():
            tree_files.append(str(tree_file))
        else:
            results["warnings"].append(f"xbrl_tree failed for {date}: {stderr[:200]}")
    results["stages"]["xbrl_tree"] = f"{len(tree_files)}/{len(filings)} OK"

    if not tree_files:
        results["errors"].append("No trees built")
        return "FAIL", results

    # Stage 3a: Merge
    if len(tree_files) > 1:
        merged_file = str(out_dir / "merged.json")
        stdout, stderr, rc = run([sys.executable, "merge_trees.py"] + tree_files + ["-o", merged_file])
        if rc != 0:
            results["stages"]["merge"] = "FAIL"
            results["errors"].append(f"merge failed: {stderr[:500]}")
            return "FAIL", results
        results["stages"]["merge"] = "PASS"
        # Capture residual warnings from stderr
        for line in stderr.splitlines():
            if "Large residual" in line:
                results["warnings"].append(line.strip())
    else:
        merged_file = tree_files[0]
        results["stages"]["merge"] = "SKIP (single filing)"

    # Stage 3b: Tree completeness
    from xbrl_tree import verify_tree_completeness, TreeNode
    with open(merged_file) as f:
        trees = json.load(f)
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        if stmt in trees and isinstance(trees[stmt], dict):
            trees[stmt] = TreeNode.from_dict(trees[stmt])
    periods = trees.get("complete_periods", [])
    all_errors = []
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        if trees.get(stmt):
            all_errors.extend(verify_tree_completeness(trees[stmt], periods))
    if all_errors:
        results["stages"]["completeness"] = f"WARN ({len(all_errors)} gaps)"
        for c, p, g in all_errors[:5]:
            results["warnings"].append(f"  gap: {c[:40]} {p} {g:,.0f}")
    else:
        results["stages"]["completeness"] = "PASS"

    # Stage 3c: Checkpoint (pymodel.py)
    stdout, stderr, rc = run([sys.executable, "pymodel.py", "--trees", merged_file, "--checkpoint"])
    if rc != 0:
        results["stages"]["checkpoint"] = "FAIL"
        results["errors"].append(f"checkpoint failed: {stderr[:500]}\n{stdout[:500]}")
        return "FAIL", results
    results["stages"]["checkpoint"] = "PASS"

    # Stage 4: Sheet builder
    stdout, stderr, rc = run([sys.executable, "sheet_builder.py", "--trees", merged_file,
                              "--company", company_name])
    if rc != 0:
        results["stages"]["sheet"] = "FAIL"
        results["errors"].append(f"sheet_builder failed: {stderr[:500]}")
        return "FAIL", results
    # Extract sheet URL from stdout
    sheet_url = ""
    for line in stdout.splitlines():
        if "spreadsheets" in line or "docs.google" in line:
            sheet_url = line.strip()
            break
    results["stages"]["sheet"] = "PASS"
    results["sheet_url"] = sheet_url

    return "PASS", results


def main():
    # Ensure we're running from the right directory
    os.chdir(str(ROOT))
    sys.path.insert(0, str(ROOT))

    tickers = sys.argv[1:] if len(sys.argv) > 1 else TICKERS
    print(f"Validating {len(tickers)} companies: {', '.join(tickers)}\n")

    summary = {}
    t0 = time.time()

    for ticker in tickers:
        print(f"\n{'='*60}")
        print(f"  {ticker}")
        print(f"{'='*60}")
        out_dir = OUT_BASE / ticker
        status, details = validate_ticker(ticker, out_dir)
        summary[ticker] = {"status": status, **details}
        print(f"\n  → {ticker}: {status}")
        if details.get("errors"):
            for e in details["errors"]:
                print(f"    ERROR: {e}")

    elapsed = time.time() - t0

    # Final report
    print(f"\n{'='*60}")
    print(f"  SUMMARY ({elapsed:.0f}s)")
    print(f"{'='*60}")
    pass_count = sum(1 for v in summary.values() if v["status"] == "PASS")
    print(f"\n  {pass_count}/{len(tickers)} PASS\n")

    for ticker, info in summary.items():
        stages = " → ".join(f"{k}:{v}" for k, v in info.get("stages", {}).items())
        print(f"  {ticker:6s} {info['status']:4s}  {stages}")
        if info.get("sheet_url"):
            print(f"         Sheet: {info['sheet_url']}")
        if info.get("warnings"):
            for w in info["warnings"]:
                print(f"         ⚠ {w}")
        if info.get("errors"):
            for e in info["errors"]:
                print(f"         ✗ {e[:120]}")

    # Save JSON report
    report_file = OUT_BASE / "validation_report.json"
    with open(report_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Full report: {report_file}")


if __name__ == "__main__":
    main()
