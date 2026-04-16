"""Integration tests for the SEC Modeler demo website (sec-agent/docs/todo/make_demo.md).

Covers acceptance criteria from the spec:
- lookup_company.search_tickers shape, ranking, substring match
- agent1_fetcher.run raises RuntimeError (not SystemExit) on bad ticker
- pymodel.run_checkpoint returns a CheckpointResult instead of sys.exit
- web/app.py: search/jobs endpoints, idempotent re-click, 409 conflict,
  404 unknown id, worker terminates on success AND on exception
- Driver boundary: web/app.py imports nothing from xbrl/sheets/etc.
"""

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
# search_tickers
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ticker_cache(monkeypatch):
    """Inject a deterministic ticker cache so tests don't hit SEC EDGAR."""
    import lookup_company

    fake = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
        "2": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
        "3": {"cik_str": 1018724, "ticker": "AMZN", "title": "AMAZON COM INC"},
        "4": {"cik_str": 12345, "ticker": "APLE", "title": "Apple Hospitality REIT"},
    }
    monkeypatch.setattr(lookup_company, "_ticker_cache", fake)
    yield fake
    monkeypatch.setattr(lookup_company, "_ticker_cache", None)


def test_search_tickers_exact_ticker_ranked_first(fake_ticker_cache):
    from lookup_company import search_tickers

    results = search_tickers("AAPL")
    assert len(results) >= 1
    assert results[0]["ticker"] == "AAPL"
    assert results[0]["name"] == "Apple Inc."
    # 10-digit zero-padded CIK
    assert results[0]["cik"] == "0000320193"


def test_search_tickers_returns_required_shape(fake_ticker_cache):
    from lookup_company import search_tickers

    results = search_tickers("apple")
    assert results, "expected at least one match for 'apple'"
    row = results[0]
    assert set(row.keys()) == {"ticker", "name", "cik"}
    # Spec: filing_type must NOT be in the search result shape.
    assert "filing_type" not in row


def test_search_tickers_case_insensitive_substring_on_name(fake_ticker_cache):
    from lookup_company import search_tickers

    tickers = {r["ticker"] for r in search_tickers("micro")}
    assert "MSFT" in tickers


def test_search_tickers_substring_on_ticker(fake_ticker_cache):
    from lookup_company import search_tickers

    tickers = {r["ticker"] for r in search_tickers("APL")}
    # Both AAPL and APLE contain "APL"
    assert "AAPL" in tickers
    assert "APLE" in tickers


def test_search_tickers_limit(fake_ticker_cache):
    from lookup_company import search_tickers

    # 'inc' substring matches several entries; ensure limit caps the result.
    results = search_tickers("inc", limit=2)
    assert len(results) <= 2


def test_search_tickers_empty_query_returns_empty(fake_ticker_cache):
    from lookup_company import search_tickers

    assert search_tickers("") == []
    assert search_tickers("   ") == []


# ---------------------------------------------------------------------------
# agent1_fetcher.run — RuntimeError, not SystemExit
# ---------------------------------------------------------------------------


def test_agent1_fetcher_bad_ticker_raises_runtime_error(monkeypatch):
    """Spec: replace sys.exit(1) with RuntimeError so the web worker's
    `except Exception` actually catches it (SystemExit inherits BaseException
    and would otherwise wedge the demo on a misspelled ticker)."""
    import agent1_fetcher

    monkeypatch.setattr(agent1_fetcher, "lookup_by_ticker", lambda q: None)
    monkeypatch.setattr(agent1_fetcher, "lookup_by_name", lambda q: None)

    with pytest.raises(RuntimeError, match="Could not find"):
        agent1_fetcher.run("ZZZZNOTAREALTICKER", years=1)


# ---------------------------------------------------------------------------
# pymodel.run_checkpoint — structured result, no sys.exit
# ---------------------------------------------------------------------------


def test_run_checkpoint_returns_structured_result_when_passing():
    from pymodel import run_checkpoint, CheckpointResult

    # Minimal trees_data with no errors → verify_model returns []
    trees_data = {"complete_periods": [], "trees": {}}
    result = run_checkpoint(trees_data)

    assert isinstance(result, CheckpointResult)
    assert result.passed is True
    assert result.errors == []
    # Does NOT raise SystemExit even on empty input
    assert result.first_error is None


# ---------------------------------------------------------------------------
# web/app.py — endpoints, idempotency, error handling
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client(monkeypatch):
    """TestClient with run_pipeline stubbed and _state reset."""
    from fastapi.testclient import TestClient
    import web.app as webapp

    # Reset module-level state between tests
    with webapp._lock:
        webapp._state.update(
            id=None, ticker=None, status="idle", stage="",
            log=[], sheet_url=None, error=None,
        )

    # Default stub: fast successful pipeline. Individual tests may override.
    def fast_pipeline(query, years=5, on_progress=None):
        if on_progress:
            on_progress("fetching", "stub fetch")
            on_progress("done", "stub done")
        return {"sheet_url": f"https://sheets.example/{query}", "company_name": query}

    monkeypatch.setattr(webapp, "run_pipeline", fast_pipeline)

    # Stub search_tickers so we don't hit SEC EDGAR
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


def test_search_endpoint_returns_results(app_client):
    client, _ = app_client
    r = client.get("/api/search", params={"q": "appl"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert body[0]["ticker"] == "AAPL"
    assert "filing_type" not in body[0]


def test_post_jobs_starts_pipeline_and_completes(app_client):
    client, webapp = app_client
    r = client.post("/api/jobs", json={"ticker": "AAPL"})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    assert jid

    # Worker thread should reach 'done' quickly with our stub
    assert _wait_until(lambda: webapp._state["status"] == "done"), webapp._state
    j = client.get(f"/api/jobs/{jid}")
    assert j.status_code == 200
    body = j.json()
    assert body["status"] == "done"
    assert body["sheet_url"] == "https://sheets.example/AAPL"
    assert body["error"] is None


def test_post_jobs_idempotent_on_same_ticker(app_client, monkeypatch):
    """Spam-clicking the same ticker should return the same job_id, not 409."""
    client, webapp = app_client

    started = threading.Event()
    release = threading.Event()

    def slow_pipeline(query, years=5, on_progress=None):
        started.set()
        release.wait(timeout=5)
        return {"sheet_url": "https://sheets.example/x", "company_name": query}

    monkeypatch.setattr(webapp, "run_pipeline", slow_pipeline)

    r1 = client.post("/api/jobs", json={"ticker": "AAPL"})
    assert r1.status_code == 200
    jid1 = r1.json()["job_id"]
    assert started.wait(timeout=2)

    r2 = client.post("/api/jobs", json={"ticker": "AAPL"})
    assert r2.status_code == 200
    assert r2.json()["job_id"] == jid1, "same ticker should reuse job_id"

    release.set()
    _wait_until(lambda: webapp._state["status"] == "done")


def test_post_jobs_conflict_on_different_ticker(app_client, monkeypatch):
    client, webapp = app_client

    started = threading.Event()
    release = threading.Event()

    def slow_pipeline(query, years=5, on_progress=None):
        started.set()
        release.wait(timeout=5)
        return {"sheet_url": "https://sheets.example/x", "company_name": query}

    monkeypatch.setattr(webapp, "run_pipeline", slow_pipeline)

    r1 = client.post("/api/jobs", json={"ticker": "AAPL"})
    assert r1.status_code == 200
    assert started.wait(timeout=2)

    r2 = client.post("/api/jobs", json={"ticker": "MSFT"})
    assert r2.status_code == 409

    release.set()
    _wait_until(lambda: webapp._state["status"] == "done")


def test_get_unknown_job_returns_404(app_client):
    client, _ = app_client
    r = client.get("/api/jobs/deadbeefdoesnotexist")
    assert r.status_code == 404


def test_worker_catches_exception_and_lands_in_error_state(app_client, monkeypatch):
    """Spec: bare `except Exception` ensures a buggy stage surfaces as
    status=error rather than wedging the app in 'running' forever."""
    client, webapp = app_client

    def exploding_pipeline(query, years=5, on_progress=None):
        raise RuntimeError("verify_model: BS_TA != BS_TL + BS_TE")

    monkeypatch.setattr(webapp, "run_pipeline", exploding_pipeline)

    r = client.post("/api/jobs", json={"ticker": "AAPL"})
    jid = r.json()["job_id"]

    assert _wait_until(lambda: webapp._state["status"] == "error"), webapp._state
    body = client.get(f"/api/jobs/{jid}").json()
    assert body["status"] == "error"
    assert "verify_model" in body["error"]
    assert body["sheet_url"] is None


def test_worker_does_not_overwrite_state_for_stale_job_id(app_client, monkeypatch):
    """If a second job starts before the first finishes resolving, the
    first worker's late completion must NOT clobber the new job's state."""
    client, webapp = app_client

    release = threading.Event()
    started = threading.Event()

    def slow_pipeline(query, years=5, on_progress=None):
        started.set()
        release.wait(timeout=5)
        return {"sheet_url": "stale", "company_name": query}

    monkeypatch.setattr(webapp, "run_pipeline", slow_pipeline)

    r1 = client.post("/api/jobs", json={"ticker": "AAPL"})
    jid1 = r1.json()["job_id"]
    assert started.wait(timeout=2)

    # Simulate operator forcibly resetting state to a new job (mimics what
    # a fresh start_job after a server-side reset would do)
    with webapp._lock:
        webapp._state.update(
            id="newjob", ticker="MSFT", status="running", stage="starting",
            log=[], sheet_url=None, error=None,
        )

    release.set()
    # Give the stale worker a moment to (incorrectly) try to write
    time.sleep(0.2)

    with webapp._lock:
        assert webapp._state["id"] == "newjob"
        assert webapp._state["sheet_url"] != "stale"


# ---------------------------------------------------------------------------
# Driver boundary — web/app.py imports only the two allowed modules
# ---------------------------------------------------------------------------


def test_app_py_driver_boundary():
    """Spec hard rule: web/app.py must not import xbrl/sheets/pymodel/etc."""
    app_path = SEC_AGENT_ROOT / "web" / "app.py"
    src = app_path.read_text()

    forbidden = [
        "xbrl",
        "sheets",
        "merge_trees",
        "concept_matcher",
        "pymodel",
        "parse_xbrl_facts",
        "gws_utils",
        "sec_utils",
        "llm_utils",
        "llm_invariant_fixer",
        "agent1_fetcher",
    ]
    for mod in forbidden:
        assert f"import {mod}" not in src, f"web/app.py must not import {mod}"
        assert f"from {mod}" not in src, f"web/app.py must not import from {mod}"
