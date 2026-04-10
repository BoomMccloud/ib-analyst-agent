"""
Google Workspace CLI Wrappers
==============================
Shared helpers for interacting with Google Sheets via the `gws` CLI.
"""

import json
import subprocess
import sys


def _run_gws(*args) -> dict:
    """Run a gws CLI command and return parsed JSON output.

    Raises:
        RuntimeError: If the gws command fails.
    """
    result = subprocess.run(["gws", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"gws error: {result.stderr[:300]}", file=sys.stderr)
        raise RuntimeError("gws failed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def gws_write(sid: str, range_: str, values: list):
    """Write values to a Google Sheets range.

    Args:
        sid: Spreadsheet ID.
        range_: A1-notation range (e.g., "'Sheet1'!A1:C3").
        values: 2D list of values to write.
    """
    params = json.dumps({"spreadsheetId": sid, "range": range_, "valueInputOption": "USER_ENTERED"})
    body = json.dumps({"values": values})
    _run_gws("sheets", "spreadsheets", "values", "update", "--params", params, "--json", body)


def gws_batch_update(sid: str, requests: list):
    """Execute a batch update on a Google Sheets spreadsheet.

    Args:
        sid: Spreadsheet ID.
        requests: List of request objects per the Sheets API.
    """
    params = json.dumps({"spreadsheetId": sid})
    body = json.dumps({"requests": requests})
    _run_gws("sheets", "spreadsheets", "batchUpdate", "--params", params, "--json", body)
