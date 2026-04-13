import json
from gws_utils import _run_gws

def gws_create(title, sheet_names):
    sheets = [{"properties": {"title": s}} for s in sheet_names]
    r = _run_gws("sheets", "spreadsheets", "create", "--json",
                  json.dumps({"properties": {"title": title}, "sheets": sheets}))
    sid = r["spreadsheetId"]
    url = r["spreadsheetUrl"]
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in r["sheets"]}
    return sid, url, sheet_ids
