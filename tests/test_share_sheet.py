"""Integration tests for the Share Sheet by Email feature.

Covers acceptance criteria from docs/todo/spec_share_sheet.md:
1. done view share UI (frontend — covered indirectly here via backend contract)
2. /api/share success → 200 with {ok, email}
3. invalid email → 400, no Drive call
4. repeated shares on same job succeed
5. gws_share invokes `gws drive permissions create` with correct argv/JSON
6. run_pipeline() return dict includes sheet_id
7. _state["sheet_id"] populated on done
8. unknown job → 404; not-done job → 409; bad email → 400
9. resetToSearch (frontend) — not covered by backend tests

Plus:
- driver boundary: web/app.py still must not import directly from `sheets`.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

# Ensure sec-agent root is importable regardless of pytest invocation cwd.
SEC_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(SEC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(SEC_AGENT_ROOT))

os.environ.setdefault("SEC_CONTACT_EMAIL", "test@example.com")


# ---------------------------------------------------------------------------
# 1) gws_share argv shape
# ---------------------------------------------------------------------------


def test_gws_share_invokes_drive_permissions_create_with_correct_argv(monkeypatch):
    """Acceptance criterion 5: gws_share builds the correct gws CLI invocation."""
    from sheets import gws as gws_mod
    from sheets.api import gws_share  # ImportError pre-impl → legitimate failure

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(argv, capture_output=True, text=True, timeout=30):
        captured["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr(gws_mod.subprocess, "run", fake_run)

    gws_share("SID123", "x@y.z")

    argv = captured["argv"]
    # First three args after `gws` must be the verb tokens
    assert argv[0] == "gws"
    assert argv[1:4] == ["drive", "permissions", "create"]

    # Remaining flags: --params <json> --json <json> (order per spec)
    assert "--params" in argv
    assert "--json" in argv
    p_idx = argv.index("--params")
    j_idx = argv.index("--json")
    params = json.loads(argv[p_idx + 1])
    body = json.loads(argv[j_idx + 1])

    assert params == {"fileId": "SID123", "sendNotificationEmail": False}
    assert body == {"type": "user", "role": "writer", "emailAddress": "x@y.z"}


# ---------------------------------------------------------------------------
# 2) gws_share role parameter
# ---------------------------------------------------------------------------


def test_gws_share_default_role_is_writer(monkeypatch):
    """Spec retains the role kwarg; default is 'writer', explicit override applies."""
    from sheets import gws as gws_mod
    from sheets.api import gws_share

    captured = []

    class _FakeCompleted:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(argv, capture_output=True, text=True, timeout=30):
        captured.append(argv)
        return _FakeCompleted()

    monkeypatch.setattr(gws_mod.subprocess, "run", fake_run)

    gws_share("SID1", "a@b.com")
    gws_share("SID2", "c@d.com", role="reader")

    body0 = json.loads(captured[0][captured[0].index("--json") + 1])
    body1 = json.loads(captured[1][captured[1].index("--json") + 1])

    assert body0["role"] == "writer"
    assert body1["role"] == "reader"


# ---------------------------------------------------------------------------
# 3) run_pipeline return dict includes sheet_id
# ---------------------------------------------------------------------------


def test_run_pipeline_return_includes_sheet_id(monkeypatch, tmp_path):
    """Acceptance criterion 6: run_pipeline returns {sheet_url, sheet_id, company_name}."""
    import run_pipeline as rp_mod

    # Stub all heavy stages so we exercise only the return-dict-shape contract.
    monkeypatch.setattr(
        rp_mod,
        "fetch_filings",
        lambda q, y: {
            "filings": [{"url": "http://example.com/f.htm", "filing_date": "2024-01-01"}],
            "company": "Acme Corp",
        },
    )

    class _FakeBytes:
        def decode(self, *a, **kw):
            return "<html></html>"

    monkeypatch.setattr(rp_mod, "fetch_url", lambda url: _FakeBytes())

    # Build a minimal "tree" object with .to_dict() so the loop persists it.
    class _FakeTree:
        def to_dict(self):
            return {}

    monkeypatch.setattr(
        rp_mod,
        "build_statement_trees",
        lambda html, base: {
            "complete_periods": [],
            "periods": [],
            "facts": {},
            "IS": _FakeTree(),
            "BS": _FakeTree(),
            "BS_LE": _FakeTree(),
            "CF": _FakeTree(),
        },
    )

    class _CheckpointResult:
        passed = True
        first_error = None
        periods = []

    monkeypatch.setattr(rp_mod, "run_checkpoint", lambda merged: _CheckpointResult())
    monkeypatch.setattr(
        rp_mod,
        "write_sheets",
        lambda merged, company: ("SID-XYZ", "https://sheets.example/SID-XYZ"),
    )

    result = rp_mod.run_pipeline("AAPL", years=1, outdir=str(tmp_path))

    assert "sheet_url" in result
    assert "company_name" in result
    assert result["sheet_id"] == "SID-XYZ"
    assert result["sheet_url"] == "https://sheets.example/SID-XYZ"


# ---------------------------------------------------------------------------
# 4) _state populates sheet_id on done
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client(monkeypatch):
    """TestClient with run_pipeline stubbed and _state reset (incl. sheet_id)."""
    from fastapi.testclient import TestClient
    import web.app as webapp

    with webapp._lock:
        webapp._state.update(
            id=None,
            ticker=None,
            status="idle",
            stage="",
            log=[],
            sheet_url=None,
            sheet_id=None,
            error=None,
        )

    def fast_pipeline(query, years=5, on_progress=None):
        if on_progress:
            on_progress("done", "stub done")
        return {
            "sheet_url": f"https://sheets.example/{query}",
            "sheet_id": f"SID-{query}",
            "company_name": query,
        }

    monkeypatch.setattr(webapp, "run_pipeline", fast_pipeline)

    def fake_search(q, limit=10):
        return [{"ticker": "AAPL", "name": "Apple Inc.", "cik": "0000320193"}]

    monkeypatch.setattr(webapp, "search_tickers", fake_search)

    return TestClient(webapp.app), webapp


def _wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_state_populates_sheet_id_on_done(app_client, monkeypatch):
    """Acceptance criterion 7: _state['sheet_id'] is set on done alongside sheet_url."""
    client, webapp = app_client

    def fast_pipeline(query, years=5, on_progress=None):
        return {
            "sheet_url": "https://sheets.example/X",
            "sheet_id": "SID999",
            "company_name": "X",
        }

    monkeypatch.setattr(webapp, "run_pipeline", fast_pipeline)

    r = client.post("/api/jobs", json={"ticker": "AAPL"})
    assert r.status_code == 200
    jid = r.json()["job_id"]

    assert _wait_until(lambda: webapp._state["status"] == "done"), webapp._state
    with webapp._lock:
        assert webapp._state["sheet_id"] == "SID999"
        assert webapp._state["sheet_url"] == "https://sheets.example/X"
        assert webapp._state["id"] == jid


# ---------------------------------------------------------------------------
# 5) /api/share happy path
# ---------------------------------------------------------------------------


def test_share_endpoint_success(app_client, monkeypatch):
    """Acceptance criterion 2: POST /api/share returns 200 with {ok, email}."""
    client, webapp = app_client

    calls = []

    def fake_share(sid, email, role="writer"):
        calls.append((sid, email))

    # Patch the symbol where /api/share looks it up — webapp.gws_share.
    monkeypatch.setattr(webapp, "gws_share", fake_share, raising=False)

    with webapp._lock:
        webapp._state.update(
            id="jid-1",
            ticker="AAPL",
            status="done",
            stage="done",
            log=[],
            sheet_url="https://sheets.example/X",
            sheet_id="SID1",
            error=None,
        )

    r = client.post("/api/share", json={"job_id": "jid-1", "email": "good@example.com"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "email": "good@example.com"}
    assert calls == [("SID1", "good@example.com")]


# ---------------------------------------------------------------------------
# 6) /api/share invalid email → 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_email", ["", "foo", "a@b"])
def test_share_endpoint_invalid_email_returns_400(app_client, monkeypatch, bad_email):
    """Acceptance criterion 3 / 8: malformed email → 400, no Drive call."""
    client, webapp = app_client

    calls = []

    def fake_share(sid, email, role="writer"):
        calls.append((sid, email))

    monkeypatch.setattr(webapp, "gws_share", fake_share, raising=False)

    with webapp._lock:
        webapp._state.update(
            id="jid-1",
            ticker="AAPL",
            status="done",
            stage="done",
            log=[],
            sheet_url="https://sheets.example/X",
            sheet_id="SID1",
            error=None,
        )

    r = client.post("/api/share", json={"job_id": "jid-1", "email": bad_email})
    assert r.status_code == 400, (bad_email, r.text)
    assert calls == [], f"gws_share should not be invoked for {bad_email!r}"


# ---------------------------------------------------------------------------
# 7) /api/share unknown job → 404
# ---------------------------------------------------------------------------


def test_share_endpoint_unknown_job_returns_404(app_client, monkeypatch):
    """Acceptance criterion 8: unknown job_id → 404."""
    client, webapp = app_client

    calls = []
    monkeypatch.setattr(
        webapp,
        "gws_share",
        lambda *a, **kw: calls.append(a),
        raising=False,
    )

    with webapp._lock:
        webapp._state.update(
            id="some-other-jid",
            ticker="AAPL",
            status="done",
            stage="done",
            log=[],
            sheet_url="https://sheets.example/X",
            sheet_id="SID1",
            error=None,
        )

    r = client.post("/api/share", json={"job_id": "stale-jid", "email": "x@y.zz"})
    assert r.status_code == 404
    assert calls == []


# ---------------------------------------------------------------------------
# 8) /api/share not-done → 409
# ---------------------------------------------------------------------------


def test_share_endpoint_not_done_returns_409(app_client, monkeypatch):
    """Acceptance criterion 8: job_id matches but status != done → 409."""
    client, webapp = app_client

    calls = []
    monkeypatch.setattr(
        webapp,
        "gws_share",
        lambda *a, **kw: calls.append(a),
        raising=False,
    )

    with webapp._lock:
        webapp._state.update(
            id="jid-1",
            ticker="AAPL",
            status="running",
            stage="fetching",
            log=[],
            sheet_url=None,
            sheet_id=None,
            error=None,
        )

    r = client.post("/api/share", json={"job_id": "jid-1", "email": "x@y.zz"})
    assert r.status_code == 409
    assert calls == []


# ---------------------------------------------------------------------------
# 9) /api/share subprocess failure → 502 with detail
# ---------------------------------------------------------------------------


def test_share_endpoint_subprocess_failure_returns_502(app_client, monkeypatch):
    """Failure mode: gws subprocess fails → 502 with the underlying message."""
    client, webapp = app_client

    def boom(sid, email, role="writer"):
        raise RuntimeError("permission denied: invalid email")

    monkeypatch.setattr(webapp, "gws_share", boom, raising=False)

    with webapp._lock:
        webapp._state.update(
            id="jid-1",
            ticker="AAPL",
            status="done",
            stage="done",
            log=[],
            sheet_url="https://sheets.example/X",
            sheet_id="SID1",
            error=None,
        )

    r = client.post("/api/share", json={"job_id": "jid-1", "email": "x@y.zz"})
    assert r.status_code == 502
    body = r.json()
    # FastAPI default error envelope is {"detail": "..."}
    detail = body.get("detail", "")
    assert "permission denied: invalid email" in detail


# ---------------------------------------------------------------------------
# 10) repeated shares succeed for the same done job
# ---------------------------------------------------------------------------


def test_share_endpoint_repeated_shares_succeed(app_client, monkeypatch):
    """Acceptance criterion 4: user can share with multiple addresses without reload."""
    client, webapp = app_client

    calls = []

    def fake_share(sid, email, role="writer"):
        calls.append((sid, email))

    monkeypatch.setattr(webapp, "gws_share", fake_share, raising=False)

    with webapp._lock:
        webapp._state.update(
            id="jid-1",
            ticker="AAPL",
            status="done",
            stage="done",
            log=[],
            sheet_url="https://sheets.example/X",
            sheet_id="SID1",
            error=None,
        )

    r1 = client.post("/api/share", json={"job_id": "jid-1", "email": "first@example.com"})
    r2 = client.post("/api/share", json={"job_id": "jid-1", "email": "second@example.com"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls == [("SID1", "first@example.com"), ("SID1", "second@example.com")]


# ---------------------------------------------------------------------------
# 11) Driver boundary: web/app.py still must not import directly from sheets
# ---------------------------------------------------------------------------


def test_app_py_still_does_not_import_from_sheets():
    """The existing driver boundary must remain intact: web/app.py imports
    gws_share via run_pipeline (which is allowed), never directly from sheets."""
    app_path = SEC_AGENT_ROOT / "web" / "app.py"
    src = app_path.read_text()

    assert "from sheets" not in src, "web/app.py must not contain `from sheets ...`"
    assert "import sheets" not in src, "web/app.py must not contain `import sheets`"
