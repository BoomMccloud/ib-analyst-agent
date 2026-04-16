import threading
import uuid
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from run_pipeline import run_pipeline
from lookup_company import search_tickers

app = FastAPI()

_static_dir = Path(__file__).parent / "static"

_lock = threading.Lock()
_state = {
    "id": None,
    "ticker": None,
    "status": "idle",
    "stage": "",
    "log": [],
    "sheet_url": None,
    "error": None,
}


def _update(stage, msg):
    with _lock:
        _state["stage"] = stage
        _state["log"] = (_state["log"] + [msg])[-20:]


def _worker(job_id, ticker, years):
    try:
        result = run_pipeline(ticker, years, on_progress=_update)
        with _lock:
            if _state["id"] == job_id:
                _state.update(status="done", sheet_url=result["sheet_url"])
    except Exception as e:
        with _lock:
            if _state["id"] == job_id:
                _state.update(status="error", error=f"{e}\n{traceback.format_exc()}")


@app.get("/api/search")
def search(q: str):
    return search_tickers(q, limit=10)


@app.post("/api/jobs")
def start_job(body: dict):
    ticker = body["ticker"]
    years = body.get("years", 5)
    with _lock:
        if _state["status"] == "running":
            if _state["ticker"] == ticker:
                return {"job_id": _state["id"]}
            raise HTTPException(409, "another job is running")
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
    threading.Thread(target=_worker, args=(jid, ticker, years), daemon=True).start()
    return {"job_id": jid}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    with _lock:
        if _state["id"] != jid:
            raise HTTPException(404)
        return dict(_state)


# Static mount must come AFTER API routes — a mount at "/" otherwise
# captures every request and shadows the /api/* endpoints.
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
