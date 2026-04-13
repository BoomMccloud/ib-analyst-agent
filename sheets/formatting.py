def _build_format_requests(sheet_id, rows, periods, row_types=None, unit_label="$m"):
    """Build Google Sheets formatting requests matching Wall Street/IB style.

    Formatting rules (from template):
    - Header row: SOLID_MEDIUM bottom border, right-aligned years
    - Parent/subtotal rows: "$"#,##0 currency format + SOLID top border
    - Leaf rows: #,##0 plain number format
    - Check rows: 0;(0);- italic
    - Font size 10 throughout, no bold
    """
    requests = []
    num_data_cols = len(periods)
    data_start_col = 4  # col E
    end_col = data_start_col + num_data_cols
    if row_types is None:
        row_types = []

    # Solid border style helpers
    SOLID = {"style": "SOLID", "color": {"red": 0, "green": 0, "blue": 0}}
    SOLID_MEDIUM = {"style": "SOLID_MEDIUM", "color": {"red": 0, "green": 0, "blue": 0}}
    # IB convention: blue = hardcoded input, black = formula
    BLUE = {"red": 0, "green": 0, "blue": 1}
    BLACK = {"red": 0, "green": 0, "blue": 0}

    for row_idx, row in enumerate(rows):
        if len(row) < 3:
            continue
        label = row[2].strip() if isinstance(row[2], str) else ""
        rtype = row_types[row_idx] if row_idx < len(row_types) else None

        # --- Header row ($m + years): SOLID_MEDIUM bottom border ---
        if label == unit_label or label == "3-Statement Summary":
            requests.append({
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 2, "endColumnIndex": end_col
                    },
                    "bottom": SOLID_MEDIUM
                }
            })
            # Right-align year headers in data columns
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": data_start_col, "endColumnIndex": end_col
                    },
                    "cell": {"userEnteredFormat": {
                        "horizontalAlignment": "RIGHT",
                        "textFormat": {"fontSize": 10}
                    }},
                    "fields": "userEnteredFormat(horizontalAlignment,textFormat.fontSize)"
                }
            })

        # --- Check rows: italic, 0;(0);- format ---
        elif label == "Check":
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 0, "endColumnIndex": end_col
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"italic": True, "fontSize": 10}}},
                    "fields": "userEnteredFormat.textFormat"
                }
            })
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": data_start_col, "endColumnIndex": end_col
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0;(0);-"}}},
                    "fields": "userEnteredFormat.numberFormat"
                }
            })

        # --- Parent/subtotal rows: "$"#,##0 + SOLID top border + black text ---
        elif rtype == "parent":
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": data_start_col, "endColumnIndex": end_col
                    },
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0'},
                        "horizontalAlignment": "RIGHT",
                        "textFormat": {"fontSize": 10, "foregroundColorStyle": {"rgbColor": BLACK}}
                    }},
                    "fields": "userEnteredFormat(numberFormat,horizontalAlignment,textFormat)"
                }
            })
            requests.append({
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 2, "endColumnIndex": end_col
                    },
                    "top": SOLID
                }
            })

        # --- Leaf rows: plain #,##0 + blue text (hardcoded values) ---
        elif rtype == "leaf" or (label and label not in (unit_label, "3-Statement Summary", "Check")):
            has_data = any(isinstance(cell, (int, float)) or (isinstance(cell, str) and cell.startswith("=")) for cell in row[4:])
            if has_data:
                # Determine color: blue for hardcoded values, black for formulas
                has_formula = any(isinstance(cell, str) and cell.startswith("=") for cell in row[4:])
                color = BLACK if has_formula else BLUE
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                            "startColumnIndex": data_start_col, "endColumnIndex": end_col
                        },
                        "cell": {"userEnteredFormat": {
                            "numberFormat": {"type": "NUMBER", "pattern": "#,##0"},
                            "horizontalAlignment": "RIGHT",
                            "textFormat": {"fontSize": 10, "foregroundColorStyle": {"rgbColor": color}}
                        }},
                        "fields": "userEnteredFormat(numberFormat,horizontalAlignment,textFormat)"
                    }
                })

    # --- Global: set font size 10 and left-align label column for all rows ---
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": len(rows),
                "startColumnIndex": 2, "endColumnIndex": 3
            },
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "LEFT",
                "textFormat": {"fontSize": 10}
            }},
            "fields": "userEnteredFormat(horizontalAlignment,textFormat.fontSize)"
        }
    })

    return requests
