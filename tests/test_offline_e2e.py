import os
import sys
import subprocess
import pytest
import json
from pathlib import Path

TICKERS = ["NFLX", "AAPL", "AMZN", "GOOG", "META", "TSLA", "JPM", "PFE"]

@pytest.fixture(autouse=True)
def setup_offline_mode():
    os.environ["SEC_OFFLINE_MODE"] = "1"
    os.environ["SEC_CONTACT_EMAIL"] = "boommccloud@gmail.com"
    yield

@pytest.mark.parametrize("ticker", TICKERS)
def test_pipeline_invariants(ticker, tmp_path):
    # 1. Fetch URL (Offline)
    res = subprocess.run(
        [sys.executable, "fetch_10k.py", ticker, "--count", "1"],
        capture_output=True, text=True, check=True
    )
    data = json.loads(res.stdout)
    url = data["filings"][0]["url"]
    
    # 2. Build Trees
    trees_json = tmp_path / "trees.json"
    res = subprocess.run(
        [sys.executable, "xbrl_tree.py", "--url", url, "-o", str(trees_json)],
        capture_output=True, text=True
    )
    assert res.returncode == 0, f"xbrl_tree.py failed for {ticker}:\n{res.stderr}\n{res.stdout}"
    
    # 3. Verify Invariants
    res = subprocess.run(
        [sys.executable, "pymodel.py", "--trees", str(trees_json), "--checkpoint"],
        capture_output=True, text=True
    )
    assert res.returncode == 0, f"Invariants failed for {ticker}:\n{res.stderr}\n{res.stdout}"
