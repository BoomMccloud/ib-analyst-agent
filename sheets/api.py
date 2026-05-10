import json
from sheets.gws import _run_gws

def gws_create(title, sheet_names):
    sheets = [{"properties": {"title": s}} for s in sheet_names]
    r = _run_gws("sheets", "spreadsheets", "create", "--json",
                  json.dumps({"properties": {"title": title}, "sheets": sheets}))
    sid = r["spreadsheetId"]
    url = r["spreadsheetUrl"]
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in r["sheets"]}
    return sid, url, sheet_ids


def gws_share(sid: str, email: str, role: str = "writer") -> None:
    """Grant `email` the given role on Drive file `sid`.

    Wraps `gws drive permissions create`. Suppresses the notification email
    so the share is silent. Raises RuntimeError if the gws CLI exits non-zero.
    """
    params = json.dumps({"fileId": sid, "sendNotificationEmail": False})
    body = json.dumps({"type": "user", "role": role, "emailAddress": email})
    _run_gws("drive", "permissions", "create",
             "--params", params, "--json", body)
