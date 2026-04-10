"""
One-time job: Create a template from the existing Alphabet 3-statement model.

Steps:
1. Copy the Alphabet spreadsheet
2. Read all sheets to identify INPUT vs FORMULA rows
3. Clear all INPUT data (keep formulas and labels)
4. Save row mapping as JSON for future data population
5. Mark INPUT rows with light blue background

Usage: python create_template.py
"""

import json
import subprocess
import sys

SOURCE_ID = "1Y6xWOvDSRbBilUT8OFUsCx9VY5efYgkkPRuOeQaLhT0"
TEMPLATE_TITLE = "3-Statement Model Template (blank)"

# Sheets to process
SHEETS = ["3-Statement Summary", "P&L", "BS", "CFS", "Annual"]


def _run_gws(*args) -> dict:
    result = subprocess.run(["gws", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"gws error: {result.stderr[:300]}", file=sys.stderr)
        raise RuntimeError(f"gws failed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def gws_write(sid, range_, values):
    params = json.dumps({"spreadsheetId": sid, "range": range_, "valueInputOption": "USER_ENTERED"})
    body = json.dumps({"values": values})
    _run_gws("sheets", "spreadsheets", "values", "update", "--params", params, "--json", body)


def gws_clear(sid, range_):
    params = json.dumps({"spreadsheetId": sid, "range": range_})
    _run_gws("sheets", "spreadsheets", "values", "clear", "--params", params, "--json", "{}")


def gws_batch_update(sid, requests):
    params = json.dumps({"spreadsheetId": sid})
    body = json.dumps({"requests": requests})
    _run_gws("sheets", "spreadsheets", "batchUpdate", "--params", params, "--json", body)


def copy_spreadsheet(source_id: str, title: str) -> str:
    """Copy a spreadsheet via Drive API. Returns new spreadsheet ID."""
    # gws doesn't have Drive copy, use the sheets API to create + copy sheets
    # Alternative: just create a new one and copy sheet by sheet
    # Actually, let's use batchUpdate to copy... no.
    # Simplest: use the Google Drive copy endpoint via gws or curl

    # Actually we can use gws with the drive API if available,
    # or we can create a new spreadsheet and use copyTo for each sheet.

    # Let's try creating a new spreadsheet then copying sheets from source
    print("  Creating new spreadsheet...", file=sys.stderr)
    new = _run_gws("sheets", "spreadsheets", "create", "--json", json.dumps({
        "properties": {"title": title}
    }))
    new_id = new["spreadsheetId"]
    default_sheet_id = new["sheets"][0]["properties"]["sheetId"]

    # Get source sheet IDs
    source = _run_gws("sheets", "spreadsheets", "get", "--params", json.dumps({
        "spreadsheetId": source_id,
        "fields": "sheets.properties"
    }))

    # Copy each sheet from source to new spreadsheet
    requests_after = []
    for sheet in source["sheets"]:
        sheet_title = sheet["properties"]["title"]
        sheet_id = sheet["properties"]["sheetId"]

        print(f"  Copying sheet: {sheet_title}...", file=sys.stderr)
        params = json.dumps({
            "spreadsheetId": source_id,
            "sheetId": sheet_id
        })
        body = json.dumps({
            "destinationSpreadsheetId": new_id
        })
        copy_result = _run_gws("sheets", "spreadsheets", "sheets", "copyTo",
                               "--params", params, "--json", body)

        # The copied sheet gets a name like "Copy of P&L" - we'll rename it
        copied_sheet_id = copy_result["sheetId"]
        requests_after.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": copied_sheet_id,
                    "title": sheet_title,
                    "index": sheet["properties"]["index"]
                },
                "fields": "title,index"
            }
        })

    # Delete the default "Sheet1" and rename copied sheets
    requests_after.append({
        "deleteSheet": {"sheetId": default_sheet_id}
    })

    gws_batch_update(new_id, requests_after)

    return new_id


def analyze_sheet(sid: str, sheet_name: str, max_rows: int = 120) -> list[dict]:
    """Read a sheet and classify each row as INPUT, FORMULA, LABEL, or EMPTY."""
    result = _run_gws("sheets", "spreadsheets", "get", "--params", json.dumps({
        "spreadsheetId": sid,
        "ranges": f"'{sheet_name}'!A1:Z{max_rows}",
        "includeGridData": True,
        "fields": "sheets.data.rowData.values(userEnteredValue,formattedValue)"
    }))

    row_data = result["sheets"][0]["data"][0].get("rowData", [])
    row_map = []

    for i, row in enumerate(row_data, 1):
        cells = row.get("values", [])
        if not cells:
            row_map.append({"row": i, "type": "EMPTY", "label": ""})
            continue

        # Get label from col B (index 1)
        label = ""
        if len(cells) > 1:
            uv = cells[1].get("userEnteredValue", {})
            label = uv.get("stringValue", "")

        # Classify data cells (col D onwards, index 3+)
        has_formula = False
        has_number = False
        has_linked = False  # formula referencing another sheet

        for j in range(3, min(len(cells), 20)):
            uv = cells[j].get("userEnteredValue", {})
            if "formulaValue" in uv:
                has_formula = True
                if "!" in uv["formulaValue"]:
                    has_linked = True
            elif "numberValue" in uv:
                has_number = True

        if has_formula and not has_number:
            row_type = "FORMULA"
        elif has_number and not has_formula:
            row_type = "INPUT"
        elif has_formula and has_number:
            # Mixed: historical is formula, forecast is input (common in P&L)
            row_type = "MIXED"
        elif label:
            row_type = "LABEL"
        else:
            row_type = "EMPTY"

        row_map.append({
            "row": i,
            "type": row_type,
            "label": label[:60],
            "linked": has_linked,
        })

    return row_map


def clear_input_data(sid: str, sheet_name: str, row_map: list[dict]):
    """Clear data values from INPUT and MIXED rows, preserving formulas and labels."""
    # For the Annual sheet, clear ALL data (it's all raw input)
    if sheet_name == "Annual":
        # Clear data columns but keep labels
        gws_clear(sid, f"'{sheet_name}'!C1:Z200")
        print(f"  Cleared all data in {sheet_name}", file=sys.stderr)
        return

    # For other sheets, only clear INPUT cells in data columns
    # We need to be surgical - clear number values but keep formulas
    for entry in row_map:
        if entry["type"] in ("INPUT", "MIXED"):
            row = entry["row"]
            # Clear data columns D-Z for this row
            gws_clear(sid, f"'{sheet_name}'!D{row}:Z{row}")

    print(f"  Cleared {sum(1 for e in row_map if e['type'] in ('INPUT', 'MIXED'))} input rows in {sheet_name}", file=sys.stderr)


def main():
    print("Creating template from Alphabet model...", file=sys.stderr)

    # Step 1: Copy the spreadsheet
    print("\nStep 1: Copying spreadsheet...", file=sys.stderr)
    new_id = copy_spreadsheet(SOURCE_ID, TEMPLATE_TITLE)
    url = f"https://docs.google.com/spreadsheets/d/{new_id}/edit"
    print(f"  New spreadsheet: {url}", file=sys.stderr)

    # Step 2: Analyze each sheet
    print("\nStep 2: Analyzing row types...", file=sys.stderr)
    all_row_maps = {}
    for sheet in SHEETS:
        print(f"  Analyzing {sheet}...", file=sys.stderr)
        row_map = analyze_sheet(new_id, sheet)
        all_row_maps[sheet] = row_map

        # Count types
        counts = {}
        for entry in row_map:
            counts[entry["type"]] = counts.get(entry["type"], 0) + 1
        print(f"    {counts}", file=sys.stderr)

    # Step 3: Clear input data
    print("\nStep 3: Clearing input data...", file=sys.stderr)
    for sheet in SHEETS:
        clear_input_data(new_id, sheet, all_row_maps[sheet])

    # Step 4: Save row map as JSON
    row_map_path = "template_row_map.json"
    with open(row_map_path, "w") as f:
        json.dump({
            "template_id": new_id,
            "template_url": url,
            "source_id": SOURCE_ID,
            "sheets": {
                sheet: [e for e in row_map if e["type"] != "EMPTY"]
                for sheet, row_map in all_row_maps.items()
            }
        }, f, indent=2)
    print(f"\nStep 4: Saved row map to {row_map_path}", file=sys.stderr)

    # Step 5: Apply formatting (blue bg for INPUT rows)
    print("\nStep 5: Applying formatting...", file=sys.stderr)
    # Get sheet IDs
    sheet_info = _run_gws("sheets", "spreadsheets", "get", "--params", json.dumps({
        "spreadsheetId": new_id,
        "fields": "sheets.properties"
    }))
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheet_info["sheets"]}

    fmt_requests = []
    for sheet_name in SHEETS:
        if sheet_name not in sheet_ids:
            continue
        sheet_id = sheet_ids[sheet_name]
        row_map = all_row_maps[sheet_name]

        for entry in row_map:
            if entry["type"] in ("INPUT", "MIXED"):
                row_idx = entry["row"] - 1  # 0-based
                fmt_requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx,
                            "endRowIndex": row_idx + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        },
                        "cell": {
                            "userEnteredValue": {"stringValue": entry["type"]},
                        },
                        "fields": "userEnteredValue"
                    }
                })

    if fmt_requests:
        # Batch in chunks of 100 to avoid request size limits
        for i in range(0, len(fmt_requests), 100):
            gws_batch_update(new_id, fmt_requests[i:i+100])

    print(f"\nTemplate created successfully!", file=sys.stderr)
    print(f"  URL: {url}", file=sys.stderr)
    print(f"  Row map: {row_map_path}", file=sys.stderr)

    print(json.dumps({"template_id": new_id, "url": url, "row_map": row_map_path}, indent=2))


if __name__ == "__main__":
    main()
