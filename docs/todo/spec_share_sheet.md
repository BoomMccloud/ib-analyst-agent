# Share Sheet by Email — Design Spec

## Problem Statement

When the pipeline finishes, the generated Google Sheet is owned by the
authenticated `gws` service account / user and visible only to that owner.
There is currently no path for the user driving the web UI to share the
finished sheet with someone else (a teammate, an analyst, etc.) without
manually opening Drive and adding a permission.

## Goal

On the "model ready" view of the web UI, allow the user to type an email
address and grant that address **edit** access to the sheet that the
just-completed job produced.

Out of scope: viewer/commenter roles, multi-recipient batch share, removing
or listing existing permissions, sharing for jobs other than the most recent
completed one.

## User Flow

1. User runs a model from the search view as today.
2. Pipeline finishes; UI swaps to the existing `done-view` with "Open
   Spreadsheet" + "Run Another".
3. New row on the `done-view`: an email text input and a "Share" button.
4. User types an email, clicks Share.
   - While the request is in flight: button disabled, status text
     "Sharing…".
   - On success: status text "Shared with `<email>`", input cleared,
     button re-enabled. User may share with another address.
   - On failure: status text shows the error message in red, input keeps
     its value, button re-enabled.
5. "Run Another" still resets to the search view as today.

## Architecture

### Backend

**`sheets/api.py` — new helper `gws_share`**

```python
def gws_share(sid: str, email: str, role: str = "writer") -> None:
    """Grant `email` the given role on Drive file `sid`.

    Wraps `gws drive permissions create`. Suppresses the notification
    email so the share is silent.
    """
    params = json.dumps({"fileId": sid, "sendNotificationEmail": False})
    body = json.dumps({"type": "user", "role": role, "emailAddress": email})
    _run_gws("drive", "permissions", "create",
             "--params", params, "--json", body)
```

This is the only new call against the Google Workspace API surface. It
reuses the existing `_run_gws` subprocess wrapper in `sheets/gws.py`, so
no new auth, no new dependency.

**`run_pipeline.py` — surface the spreadsheet id**

`write_sheets` already returns `(sid, url)` (line 118). The `run_pipeline`
return dict drops `sid` on the floor today; add it:

```python
return {"sheet_url": url, "sheet_id": sid, "company_name": company_name}
```

**`web/app.py` — track sheet id + share endpoint**

1. Add `"sheet_id": None` to the `_state` dict.
2. In `_worker`, on success also set `sheet_id=result["sheet_id"]`.
3. New route:

```python
@app.post("/api/share")
def share(body: dict):
    jid = body.get("job_id")
    email = (body.get("email") or "").strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "invalid email")
    with _lock:
        if _state["id"] != jid:
            raise HTTPException(404, "job not found")
        if _state["status"] != "done":
            raise HTTPException(409, "job not finished")
        sid = _state["sheet_id"]
    try:
        gws_share(sid, email)
    except RuntimeError as e:
        raise HTTPException(502, f"share failed: {e}")
    return {"ok": True, "email": email}
```

`_EMAIL_RE` = `re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")` — same regex
the frontend will use; backend is the source of truth.

The endpoint accepts repeated calls for the same job: the user may share
the same sheet with several addresses while the done view is open. Each
call hits Drive once.

### Frontend (`web/static/index.html`)

Add a fieldset to `done-view`, above the existing button row:

```html
<fieldset>
  <legend>Share Spreadsheet</legend>
  <div style="display:flex; gap:12px; align-items:center;">
    <input id="share-email" type="text" placeholder="name@example.com"
           autocomplete="off" style="flex:1;">
    <button id="share-btn">Share</button>
  </div>
  <div id="share-status" style="margin-top:10px; min-height:1.2em;"></div>
</fieldset>
```

JS handler:

```js
$("#share-btn").addEventListener("click", async () => {
  const email = $("#share-email").value.trim();
  const status = $("#share-status");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    status.textContent = "Enter a valid email address.";
    status.style.color = "#a00";
    return;
  }
  $("#share-btn").disabled = true;
  status.style.color = "#000";
  status.textContent = "Sharing…";
  try {
    const resp = await fetch("/api/share", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({job_id: currentJobId, email}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    status.style.color = "#008000";
    status.textContent = `Shared with ${email}`;
    $("#share-email").value = "";
  } catch (e) {
    status.style.color = "#a00";
    status.textContent = String(e.message || e);
  } finally {
    $("#share-btn").disabled = false;
  }
});
```

`resetToSearch()` should clear `#share-email` and `#share-status` so a
subsequent run starts clean.

## Data Flow

```
done-view "Share" click
  → POST /api/share {job_id, email}
    → web/app.py validates email, looks up sheet_id from in-memory state
      → sheets/api.gws_share(sid, email)
        → subprocess `gws drive permissions create ...`
          → Google Drive grants `writer` role
    ← {"ok": true, "email": "..."}
  ← UI shows "Shared with <email>"
```

## Failure Modes

| Failure | Surface | Behavior |
|---------|---------|----------|
| Empty/malformed email | client + server | client blocks before fetch; server returns 400 |
| Job id stale (after Run Another, then share late) | server | 404; UI shows error |
| Job not done yet | server | 409; UI shows error (should be unreachable from done-view) |
| `gws drive permissions create` non-zero exit | server | 502 with stderr snippet; UI shows error |
| Email belongs to a non-Google account | server | gws returns the Drive API error verbatim; UI shows it |

No retry logic: a transient failure leaves the user able to click Share
again with the same email.

## Acceptance Criteria

1. After a successful pipeline run, the done view contains an email input
   and a Share button.
2. Submitting a valid email POSTs to `/api/share` and on a 200 response
   displays "Shared with `<email>`".
3. Submitting an invalid email (`"foo"`, `""`, `"a@b"`) does not POST and
   shows a validation error inline.
4. After Share succeeds, the input is empty and a second different email
   can be shared without reloading.
5. `gws_share(sid, "x@y.z")` invokes
   `gws drive permissions create --params '...' --json '...'` with
   `type=user`, `role=writer`, `emailAddress=x@y.z`,
   `sendNotificationEmail=false`.
6. `run_pipeline()` return value includes `sheet_id`.
7. `_state["sheet_id"]` is populated on job completion alongside
   `sheet_url`.
8. Hitting `/api/share` for an unknown job_id returns 404; for a not-done
   job returns 409; for a malformed email returns 400.
9. `resetToSearch()` clears the share input and status text.

## Non-Goals

- Persisting share history across job runs.
- Removing or downgrading existing permissions.
- Sharing with Google Groups or domains (only individual user emails).
- Bulk paste of multiple emails — one share at a time.
- Auth: the user driving the web UI is implicitly trusted (same trust
  model as starting a pipeline run).
