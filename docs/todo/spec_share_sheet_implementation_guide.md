# Implementation Guide: Share Sheet by Email

**Based on Spec**: `docs/todo/spec_share_sheet.md`
**Verification Report**: `docs/todo/spec_share_sheet_verification_report.md`
**Test File(s)**: `tests/test_share_sheet.py`
**Generated**: 2026-05-10

---

## Overview

### What You're Building

A "Share Spreadsheet" row on the existing `done-view` of the web UI that lets the user grant another email address writer access to the just-completed Google Sheet. End-to-end: HTML form → POST `/api/share` → in-process state lookup → `gws drive permissions create` subprocess → JSON response → status text in the UI.

### Core Concept (The "North Star")

**"`web/app.py` is the driver; it never touches `sheets/` directly."**
The existing test `tests/test_demo_website.py::test_app_py_driver_boundary` forbids the strings `from sheets` and `import sheets` anywhere in `web/app.py`. We satisfy this by **re-exporting** `gws_share` from `run_pipeline.py` (which already legitimately imports from `sheets`) and importing it into `web/app.py` from `run_pipeline`.

---

## Phase 0 — Coverage Map

Every numbered acceptance criterion (AC) in `docs/todo/spec_share_sheet.md` mapped to the `tests/test_share_sheet.py` test that covers it.

| AC # | Spec Requirement | Covered By | Notes |
|------|------------------|-----------|-------|
| 1 | done-view contains email input + Share button | (frontend, manual) | Not pytest-testable — validated indirectly via the backend contract (ACs 2/7/8). User will manually verify in browser. |
| 2 | Valid email POST → 200 `{ok, email}` | `test_share_endpoint_success` | |
| 3 | Invalid email (`""`, `"foo"`, `"a@b"`) → no POST + inline error | `test_share_endpoint_invalid_email_returns_400` (parametrized) | Server-side guard. Client guard is the same regex; manual UI check covers the "no POST" half. |
| 4 | After success input clears, second share works without reload | `test_share_endpoint_repeated_shares_succeed` | Backend half. UI clearing is manual. |
| 5 | `gws_share(sid, "x@y.z")` argv shape | `test_gws_share_invokes_drive_permissions_create_with_correct_argv` + `test_gws_share_default_role_is_writer` | |
| 6 | `run_pipeline()` return dict includes `sheet_id` | `test_run_pipeline_return_includes_sheet_id` | |
| 7 | `_state["sheet_id"]` populated on completion | `test_state_populates_sheet_id_on_done` | |
| 8 | unknown job → 404; not-done → 409; bad email → 400 | `test_share_endpoint_unknown_job_returns_404`, `test_share_endpoint_not_done_returns_409`, `test_share_endpoint_invalid_email_returns_400` | |
| 9 | `resetToSearch()` clears `#share-email` and `#share-status` | (frontend, manual) | Pure DOM mutation; no pytest. User will manually verify after a Run Another click. |

Plus: `test_app_py_still_does_not_import_from_sheets` enforces the driver boundary (WARN-001 from the verification report).

**Coverage gaps**: ACs 1 and 9 are frontend-only and not directly pytest-testable. The user has been told to verify them manually. **Do not add new tests for them** — the backend contract tests are the binding success criteria.

**TDD smell scan**: Tests drive the public API (FastAPI endpoints, `gws_share()` function, `run_pipeline()` return value) and patch only at the subprocess boundary (`gws_mod.subprocess.run`) or at the swap-in symbol on `webapp` (`monkeypatch.setattr(webapp, "gws_share", ...)`). No direct internal-state writes that simulate writer output. **Tests are clean — proceed with implementation.**

---

## Files You Will Modify

In implementation order (top-to-bottom, so each step's tests can run cleanly):

| Step | File | Action | Rough Lines | Summary |
|------|------|--------|-------------|---------|
| 1 | `sheets/gws.py` | Modify | ~22 | Surface stderr snippet in the `RuntimeError` message |
| 2 | `sheets/api.py` | Modify | after line 11 | Add `gws_share(sid, email, role="writer")` |
| 3 | `run_pipeline.py` | Modify | top imports + ~121 | Re-export `gws_share`; add `sheet_id` to return dict |
| 4 | `web/app.py` | Modify | top imports, `_state`, `_worker`, `start_job`, new route | Track `sheet_id`, add `_EMAIL_RE`, add `/api/share` |
| 5 | `web/static/index.html` | Modify | ~206-218, ~246-254, bottom `<script>` | Add fieldset, JS handler, reset clearing |
| 6 | `tests/test_share_sheet_unit.py` | Create | new file | Unit tests for the email regex + `sendNotificationEmail` flag |

### Out of Scope — DO NOT MODIFY

- `tests/test_share_sheet.py` — these are the acceptance tests; **never edit them to make them pass** (fix the implementation instead).
- `tests/test_demo_website.py` — the driver-boundary test is binding. Don't loosen it.
- `sheets/__init__.py` — empty by convention; do not add re-exports.
- Any pipeline/XBRL/merge/model code — share is a pure UI/Drive concern.

---

## Prerequisites

```bash
# Confirm the failing tests exist and fail
python -m pytest tests/test_share_sheet.py -v
# Expect ImportError on gws_share + 404/AttributeError on /api/share — that's correct pre-impl.

# Confirm the existing site tests still pass (baseline)
python -m pytest tests/test_demo_website.py -v
```

---

## Step 1: Surface stderr in `_run_gws` RuntimeError

### Goal

The `/api/share` 502 path must surface the underlying gws CLI error so the UI can display something more useful than `"gws failed"`. Today `_run_gws` prints stderr to stderr but raises a generic message. Embed a stderr snippet in the exception message.

**This step helps pass**: `test_share_endpoint_subprocess_failure_returns_502` (indirectly — the test stubs `gws_share` to `raise RuntimeError("permission denied: invalid email")`, but real-world stderr surfacing is a spec requirement (Failure Modes table) and a precondition for the snippet showing up in 502 details).

### File

`sheets/gws.py`

### Find This Location

Open the file and navigate to **lines 19-21**:

```python
    if result.returncode != 0:
        print(f"gws error: {result.stderr[:300]}", file=sys.stderr)
        raise RuntimeError("gws failed")
```

### Action

Replace those three lines with:

```python
    if result.returncode != 0:
        print(f"gws error: {result.stderr[:300]}", file=sys.stderr)
        raise RuntimeError(f"gws failed: {result.stderr.strip()[:300]}")
```

The print to stderr stays (it preserves existing log behaviour for any consumer that scrapes server logs); the new RuntimeError message is what propagates up the stack.

### Common Mistakes

- **Removing the `print(...)` line.** Don't — `tests/test_demo_website.py` and other consumers may rely on existing stderr output. Keep it.
- **Using `result.stderr` raw without `.strip()`** — trailing newlines bloat the 502 response.
- **Raising a different exception type.** The callers only catch `RuntimeError`; switching to `subprocess.CalledProcessError` would silently break them.

### Verify This Step

```bash
python -m py_compile sheets/gws.py
python -m pytest tests/test_demo_website.py -v
```

Expect: all existing tests still pass. (No tests directly assert the message text yet, so this change is backward compatible.)

---

## Step 2: Add `gws_share` to `sheets/api.py`

### Goal

Single function that wraps `gws drive permissions create` with `sendNotificationEmail=False` and the user/role/email body. Default role is `writer`; tests assert that explicit `role="reader"` overrides it.

**This step helps pass**: `test_gws_share_invokes_drive_permissions_create_with_correct_argv`, `test_gws_share_default_role_is_writer`.

### File

`sheets/api.py`

### Find This Location

Current contents (read in full — file is 11 lines):

```python
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
```

### Action

Append after line 11 (end of file):

```python


def gws_share(sid: str, email: str, role: str = "writer") -> None:
    """Grant `email` the given role on Drive file `sid`.

    Wraps `gws drive permissions create`. Suppresses the notification email
    so the share is silent. Raises RuntimeError if the gws CLI exits non-zero.
    """
    params = json.dumps({"fileId": sid, "sendNotificationEmail": False})
    body = json.dumps({"type": "user", "role": role, "emailAddress": email})
    _run_gws("drive", "permissions", "create",
             "--params", params, "--json", body)
```

No new imports needed — `json` and `_run_gws` are already imported at the top of the file.

### Common Mistakes

- **Reordering `--params` and `--json`** — test #1 doesn't care about order between them (uses `argv.index(...)`), but keep the convention consistent with `gws_write`/`gws_batch_update` for grep-ability.
- **Hardcoding `role="writer"` inside the body** — test #2 explicitly calls `gws_share("SID2", "c@d.com", role="reader")` and asserts `body["role"] == "reader"`. The `role` parameter must flow through.
- **Returning the parsed `_run_gws` result** — spec types `gws_share` as `-> None`. Don't return anything.
- **Using `False` as a Python bool then expecting JSON `false`** — `json.dumps({"sendNotificationEmail": False})` produces `"sendNotificationEmail": false`, which is what we want. No special handling needed.

### Verify This Step

```bash
python -m py_compile sheets/api.py
python -m pytest tests/test_share_sheet.py::test_gws_share_invokes_drive_permissions_create_with_correct_argv -v
python -m pytest tests/test_share_sheet.py::test_gws_share_default_role_is_writer -v
```

Expect: both pass.

---

## Step 3: Re-export `gws_share` from `run_pipeline.py` + add `sheet_id` to return

### Goal

Two changes in one file:

1. Add `sheet_id` to the `run_pipeline()` return dict (AC #6).
2. Add `from sheets.api import gws_share` at the top of `run_pipeline.py` so `web/app.py` can import it via the driver-allowed path: `from run_pipeline import run_pipeline, gws_share`. (This is the WARN-001 fix from verification.)

**This step helps pass**: `test_run_pipeline_return_includes_sheet_id`, plus enables Step 4.

### File

`run_pipeline.py`

### Find Location 1 — Imports (lines 16-21)

Current:

```python
from fetch.agent import run as fetch_filings
from fetch.http import fetch_url
from xbrl import build_statement_trees
from merge.trees import merge_filing_trees
from model.verify import run_checkpoint
from sheets import write_sheets
```

### Action 1

Add a new line **after `from sheets import write_sheets`** (line 21):

```python
from fetch.agent import run as fetch_filings
from fetch.http import fetch_url
from xbrl import build_statement_trees
from merge.trees import merge_filing_trees
from model.verify import run_checkpoint
from sheets import write_sheets
from sheets.api import gws_share  # re-exported for web/app.py (driver boundary)
```

This makes `gws_share` an attribute of the `run_pipeline` module so `from run_pipeline import gws_share` works.

### Find Location 2 — Return dict (line 121)

Current:

```python
    sid, url = write_sheets(merged, company_name)
    on_progress("done", f"Sheet ready: {url}")

    return {"sheet_url": url, "company_name": company_name}
```

### Action 2

Replace line 121 with:

```python
    return {"sheet_url": url, "sheet_id": sid, "company_name": company_name}
```

`sid` is already in scope from line 118 (`sid, url = write_sheets(...)`).

Optional but recommended: update the docstring on line 34:

Before:
```python
    Returns:
        dict with keys: sheet_url (str), company_name (str)
```

After:
```python
    Returns:
        dict with keys: sheet_url (str), sheet_id (str), company_name (str)
```

### Common Mistakes

- **Importing `gws_share` from `sheets` (not `sheets.api`).** `sheets/__init__.py` is empty (verified) — the only working path is `from sheets.api import gws_share`.
- **Forgetting that `from sheets import write_sheets` already exists.** The boundary test only checks `web/app.py`, not `run_pipeline.py`. `run_pipeline.py` is allowed to import from `sheets`.
- **Putting `sheet_id` first or in a different position.** Test asserts on key membership, not order — but consistent ordering is good style.

### Verify This Step

```bash
python -m py_compile run_pipeline.py
python -m pytest tests/test_share_sheet.py::test_run_pipeline_return_includes_sheet_id -v
# Sanity: the import still works in isolation
python -c "from run_pipeline import run_pipeline, gws_share; print(gws_share)"
```

Expect: test passes; the `python -c` line prints a function reference.

---

## Step 4: Add `sheet_id` tracking + `/api/share` endpoint to `web/app.py`

### Goal

Four sub-changes in this file:

1. Import `gws_share` via `run_pipeline` (NOT from `sheets`).
2. Add `re` import + compile `_EMAIL_RE` at module scope.
3. Add `"sheet_id": None` to `_state`; reset it in `start_job`; populate it in `_worker`.
4. Register `@app.post("/api/share")` BEFORE the `app.mount("/", ...)` call on line 84.

**This step helps pass**: `test_state_populates_sheet_id_on_done`, `test_share_endpoint_success`, `test_share_endpoint_invalid_email_returns_400`, `test_share_endpoint_unknown_job_returns_404`, `test_share_endpoint_not_done_returns_409`, `test_share_endpoint_subprocess_failure_returns_502`, `test_share_endpoint_repeated_shares_succeed`, `test_app_py_still_does_not_import_from_sheets`.

### File

`web/app.py`

### Find Location 1 — Imports (lines 1-10)

Current:

```python
import threading
import uuid
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from run_pipeline import run_pipeline
from fetch.lookup import search_tickers
```

### Action 1

Replace those lines with:

```python
import re
import threading
import uuid
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from run_pipeline import run_pipeline, gws_share
from fetch.lookup import search_tickers

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
```

**Critical**: do NOT write `from sheets.api import gws_share` here. The boundary test forbids the substring `from sheets` in this file. Use `from run_pipeline import ...`.

### Find Location 2 — `_state` dict (lines 17-25)

Current:

```python
_state = {
    "id": None,
    "ticker": None,
    "status": "idle",
    "stage": "",
    "log": [],
    "sheet_url": None,
    "error": None,
}
```

### Action 2

Insert `"sheet_id": None` between `"sheet_url"` and `"error"`:

```python
_state = {
    "id": None,
    "ticker": None,
    "status": "idle",
    "stage": "",
    "log": [],
    "sheet_url": None,
    "sheet_id": None,
    "error": None,
}
```

### Find Location 3 — `_worker` success branch (lines 37-39)

Current:

```python
        with _lock:
            if _state["id"] == job_id:
                _state.update(status="done", sheet_url=result["sheet_url"])
```

### Action 3

Add `sheet_id`:

```python
        with _lock:
            if _state["id"] == job_id:
                _state.update(
                    status="done",
                    sheet_url=result["sheet_url"],
                    sheet_id=result["sheet_id"],
                )
```

### Find Location 4 — `start_job` reset (lines 60-69)

Current:

```python
        jid = uuid.uuid4().hex
        _state.update(
            id=jid,
            ticker=ticker,
            status="running",
            stage="starting",
            log=[],
            sheet_url=None,
            error=None,
        )
```

### Action 4

Add `sheet_id=None` so a stale id from a prior job doesn't leak into a new run:

```python
        jid = uuid.uuid4().hex
        _state.update(
            id=jid,
            ticker=ticker,
            status="running",
            stage="starting",
            log=[],
            sheet_url=None,
            sheet_id=None,
            error=None,
        )
```

### Find Location 5 — Insert `/api/share` route BEFORE the static mount (line 84)

The current file ends at line 84 with `app.mount("/", StaticFiles(...), ...)`. The `/api/share` route MUST be registered before that mount, or the mount's catch-all `/` will swallow it.

### Action 5

Insert after `get_job` (after line 79, before the comment block at line 82) the following block:

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
    # Release lock BEFORE the subprocess call — gws takes seconds and
    # holding the lock would block /api/jobs/{jid} polling for that whole time.
    try:
        gws_share(sid, email)
    except RuntimeError as e:
        raise HTTPException(502, f"share failed: {e}")
    return {"ok": True, "email": email}
```

After this insertion, the existing comment + `app.mount(...)` lines should still be the LAST thing in the file.

### Common Mistakes

- **Importing from `sheets`.** Even `from sheets.api import gws_share` will fail `test_app_py_still_does_not_import_from_sheets`. Use `from run_pipeline import gws_share`.
- **Holding `_lock` during `gws_share(...)`.** The subprocess takes seconds; holding the lock would freeze the entire app's `/api/jobs/{jid}` polling. Read `sid` inside the lock, then exit the `with` block before calling `gws_share`.
- **Putting `@app.post("/api/share")` AFTER `app.mount("/", ...)`.** The static-files mount catches every path including `/api/share`, returning 404 from the static handler. The mount must remain the LAST registration.
- **Forgetting to reset `sheet_id` in `start_job`.** A second run started after a successful first run would otherwise inherit the previous sheet's id, opening a window where `/api/share` could share the OLD sheet with someone after the user thought a new pipeline was running. The test `test_share_endpoint_not_done_returns_409` explicitly resets `sheet_id=None` in its setup — your `start_job` must do the same.
- **Using `body["email"]` (raises KeyError if missing) instead of `body.get("email") or ""`.** The 400 path expects a clean error response, not a 500.
- **Not stripping whitespace on email.** `(body.get("email") or "").strip()` matches the spec.

### Verify This Step

```bash
python -m py_compile web/app.py
python -m pytest tests/test_share_sheet.py -v
python -m pytest tests/test_demo_website.py -v
```

Expect: every test in `tests/test_share_sheet.py` passes; `tests/test_demo_website.py` continues to pass (the driver-boundary test is the key one).

---

## Step 5: Frontend — fieldset, JS handler, reset clearing

### Goal

Add the visible UI: a fieldset on `done-view` with email input + Share button + status text; a JS click handler that posts to `/api/share`; clear the input + status in `resetToSearch()`.

**This step helps pass**: ACs 1, 4 (UI half), 9 (manual verification).

### File

`web/static/index.html`

### Change 1 — Add fieldset to done-view (lines 206-218)

Current `done-view`:

```html
<!-- Done View -->
<div id="done-view" class="hidden">
  <div class="flex-row">
    <div class="icon-large">📊</div>
    <div>
      <div id="done-company" style="font-weight: bold; font-size: 28px; margin-bottom: 8px;"></div>
      <div style="color: #008000;">Operation completed successfully.</div>
    </div>
  </div>
  <div style="display: flex; gap: 16px; justify-content: flex-end; margin-top: 28px;">
    <a id="sheet-link" href="#" target="_blank" class="btn">Open Spreadsheet</a>
    <button id="run-another">Run Another</button>
  </div>
</div>
```

Insert a new `<fieldset>` block between the `flex-row` div (closes at line 213) and the button row div (opens at line 214):

```html
<!-- Done View -->
<div id="done-view" class="hidden">
  <div class="flex-row">
    <div class="icon-large">📊</div>
    <div>
      <div id="done-company" style="font-weight: bold; font-size: 28px; margin-bottom: 8px;"></div>
      <div style="color: #008000;">Operation completed successfully.</div>
    </div>
  </div>
  <fieldset style="margin-top: 20px;">
    <legend>Share Spreadsheet</legend>
    <div style="display:flex; gap:12px; align-items:center;">
      <input id="share-email" type="text" placeholder="name@example.com"
             autocomplete="off" style="flex:1;">
      <button id="share-btn">Share</button>
    </div>
    <div id="share-status" style="margin-top:10px; min-height:1.2em;"></div>
  </fieldset>
  <div style="display: flex; gap: 16px; justify-content: flex-end; margin-top: 28px;">
    <a id="sheet-link" href="#" target="_blank" class="btn">Open Spreadsheet</a>
    <button id="run-another">Run Another</button>
  </div>
</div>
```

### Change 2 — Clear inputs in `resetToSearch()` (lines 246-254)

Current:

```js
function resetToSearch() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  currentJobId = null;
  selectedTicker = null;
  $("#search-input").value = "";
  $("#results").innerHTML = "";
  $("#results").classList.add("hidden");
  showView("search-view");
}
```

Add two clearing lines:

```js
function resetToSearch() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  currentJobId = null;
  selectedTicker = null;
  $("#search-input").value = "";
  $("#results").innerHTML = "";
  $("#results").classList.add("hidden");
  $("#share-email").value = "";
  $("#share-status").textContent = "";
  showView("search-view");
}
```

### Change 3 — Add the share button handler at the bottom of the `<script>` block (after line 334, before `</script>` on line 335)

Current end of script:

```js
$("#run-another").addEventListener("click", resetToSearch);
$("#run-another-error").addEventListener("click", resetToSearch);
</script>
```

Insert a new handler block before `</script>`:

```js
$("#run-another").addEventListener("click", resetToSearch);
$("#run-another-error").addEventListener("click", resetToSearch);

// --- Share Sheet ---
$("#share-btn").addEventListener("click", async () => {
  const email = $("#share-email").value.trim();
  const status = $("#share-status");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    status.style.color = "#a00";
    status.textContent = "Enter a valid email address.";
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
</script>
```

### Common Mistakes

- **Putting the `<fieldset>` outside `<div id="done-view">`.** Then it would always be visible. It must be INSIDE so `showView("done-view")` toggles it.
- **Listening to a form `submit` event.** The fieldset is not inside a `<form>` — using `submit` will not fire. Use the button's `click` event.
- **Forgetting `event.preventDefault()`.** Not needed here because we use a `<button>` outside a form, which is type `submit` by default but with no form to submit. Safe.
- **Not disabling the button on click.** A double-click would fire two POSTs.
- **Not clearing `#share-status` in `resetToSearch`.** Stale status text would persist into the next run's done view.

### Verify This Step

```bash
# Static HTML — no compile step. Just confirm the file is well-formed:
python -c "from html.parser import HTMLParser; HTMLParser().feed(open('web/static/index.html').read())"
```

Manual UI check (only after deploying or running the server locally):
1. Run a pipeline to completion → "Share Spreadsheet" fieldset appears.
2. Type `notanemail`, click Share → red status text, no network call (check devtools Network tab).
3. Type `me@example.com`, click Share → status goes to "Sharing…", then either green "Shared with me@example.com" or a red error message.
4. Click "Run Another" → return to search view. Run another pipeline. The share input and status from the previous run are clear.

---

## Step 6: Unit Tests for Internal Logic

### Goal

Add small, focused unit tests for the email regex (compiled at module import) and the `sendNotificationEmail: false` flag in the `gws_share` params JSON. The integration tests in `tests/test_share_sheet.py` exercise the full happy path; these unit tests guard against subtle regressions in the two single-line behaviours.

### File

Create new file: `tests/test_share_sheet_unit.py`

### Action

```python
"""Unit tests for share-sheet internals (email regex + gws_share params).

Complements the integration coverage in tests/test_share_sheet.py with
focused guards on two atomic behaviours:
- _EMAIL_RE accepts/rejects the right shapes
- gws_share always sets sendNotificationEmail=False (silent share)
"""

import json
import os
import sys
from pathlib import Path

import pytest

SEC_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(SEC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(SEC_AGENT_ROOT))

os.environ.setdefault("SEC_CONTACT_EMAIL", "test@example.com")


def test_email_regex_accepts_valid_addresses():
    """_EMAIL_RE must accept typical valid addresses and reject malformed ones."""
    import web.app as webapp

    rx = webapp._EMAIL_RE

    # Valid forms
    assert rx.match("a@b.co")
    assert rx.match("first.last@sub.example.com")
    assert rx.match("user+tag@example.io")
    assert rx.match("x@y.z")

    # Invalid forms
    assert rx.match("") is None
    assert rx.match("foo") is None  # no @
    assert rx.match("a@b") is None  # no dot in domain
    assert rx.match("a @b.co") is None  # whitespace
    assert rx.match("a@b .co") is None  # whitespace
    assert rx.match("@b.co") is None  # empty local-part
    assert rx.match("a@.co") is None  # empty domain-label-before-dot


def test_gws_share_passes_sendNotificationEmail_false(monkeypatch):
    """gws_share must always pass sendNotificationEmail=False so shares are silent."""
    from sheets import gws as gws_mod
    from sheets.api import gws_share

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(argv, capture_output=True, text=True, timeout=30):
        captured["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr(gws_mod.subprocess, "run", fake_run)

    gws_share("any-sid", "user@example.com")

    argv = captured["argv"]
    p_idx = argv.index("--params")
    params = json.loads(argv[p_idx + 1])
    assert params["sendNotificationEmail"] is False
    assert params["fileId"] == "any-sid"
```

### Verify This Step

```bash
python -m pytest tests/test_share_sheet_unit.py -v
```

Expect: both tests pass.

---

<!-- UNIT-TESTS-REQUIRED -->
### Unit Tests Required

| Function/Logic | Test File | Test Name | Edge Case / Behavior |
|----------------|-----------|-----------|----------------------|
| `_EMAIL_RE` (compiled at `web/app.py` import) | `tests/test_share_sheet_unit.py` | `test_email_regex_accepts_valid_addresses` | Accepts typical valid addresses (with dots, plus-tags, subdomains); rejects empty, no-@, no-dot, whitespace, empty-local-part, empty-pre-dot-label |
| `gws_share()` params JSON | `tests/test_share_sheet_unit.py` | `test_gws_share_passes_sendNotificationEmail_false` | `params["sendNotificationEmail"]` is the JSON literal `false` (subset of integration argv test, but a focused guard against regression) |

---

## Final Verification

### Run Full Test Suite for the Feature

```bash
python -m pytest tests/test_share_sheet.py tests/test_share_sheet_unit.py -v
```

All 13 tests (10 integration + 1 boundary + 2 unit) must pass.

### Run the Full Repo Test Suite — No Regressions

```bash
python -m pytest tests/ -v
```

In particular, `tests/test_demo_website.py::test_app_py_driver_boundary` must still pass.

### Lint / Compile

```bash
python -m py_compile sheets/gws.py sheets/api.py run_pipeline.py web/app.py
```

No errors.

### Manual UI Verification (ACs 1 & 9)

Boot the web app (`uvicorn web.app:app --reload --host 0.0.0.0 --port 8000` or your container deploy) and:

1. Run a pipeline → done-view shows the Share Spreadsheet fieldset with email input + Share button.
2. Type a valid email + click Share → status text "Sharing…" then "Shared with `<email>`" in green; input clears.
3. Type a second different email + click Share → second share succeeds without reload.
4. Click "Run Another" → search view appears. Start another pipeline. When it completes, the share input and status from the previous run are EMPTY.

---

## Common Mistakes Summary

1. **Don't import `gws_share` from `sheets` in `web/app.py`.** Use `from run_pipeline import gws_share`. The driver-boundary regex test forbids the literal substrings `from sheets` and `import sheets` in `web/app.py`.
2. **Don't hold `_lock` during the `gws_share(...)` subprocess call.** The CLI takes seconds; holding the lock blocks `/api/jobs/{jid}` polling. Read `sid` inside the lock, then release before invoking the subprocess.
3. **Don't forget to reset `sheet_id=None` in `start_job` and in the test fixture's `_state.update(...)`.** Stale ids from prior runs leak into new runs and create a window where `/api/share` could grant access to the wrong sheet. The test fixture in `tests/test_share_sheet.py:185-195` already resets it; your `start_job` must too.
4. **Register `@app.post("/api/share")` BEFORE `app.mount("/", StaticFiles(...))` on line 84.** The static mount catches every path; routes registered after it are unreachable.
5. **Don't change `tests/test_share_sheet.py` to make tests pass.** If a test fails, fix the implementation. The test file is the binding success criterion.

---

## Pre-Submission Checklist

- [ ] All `tests/test_share_sheet.py` tests pass (10 integration + 1 boundary)
- [ ] All `tests/test_share_sheet_unit.py` tests pass (2 unit)
- [ ] All `tests/test_demo_website.py` tests still pass (no regressions, especially `test_app_py_driver_boundary`)
- [ ] `python -m py_compile sheets/gws.py sheets/api.py run_pipeline.py web/app.py` exits clean
- [ ] `web/app.py` contains neither `from sheets` nor `import sheets`
- [ ] `_state` dict has `sheet_id` populated on done and reset to `None` on new job
- [ ] `/api/share` route registered before the StaticFiles mount
- [ ] Manual UI check: fieldset appears on done-view; Run Another clears share input + status
