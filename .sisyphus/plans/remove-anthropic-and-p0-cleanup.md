# Remove Anthropic References + P0 Cleanup

## Overview

Three related tasks:
1. **P0 #1**: Deduplicate `fetch/lookup.py` — replace ~30 lines of duplicate SEC fetch utilities with imports from `fetch/http.py`
2. **P1 #7**: Add LLM_API_KEY + gws CLI startup validation to `run_pipeline.py`
3. **Anthropic cleanup**: Remove all `ANTHROPIC_API_KEY` and Claude model references from docs/config

---

## Part A: P0 #1 — lookup.py dedup

**File**: `fetch/lookup.py`

### Current state
Lines 26–70 contain local duplicates of functionality in `fetch/http.py`:
- `import time` (only used by local `_throttle()`)
- `import urllib.error` (only used by local `fetch_url()`)
- L30–38: `_contact`/SEC_CONTACT_EMAIL warning + fallback
- L39: `HEADERS = {"User-Agent": f"SecFilingsAgent {_contact}"}`
- L44: `REQUEST_INTERVAL = 1.0 / 8`
- L45: `_last_request_time = 0.0`
- L48–54: `_throttle()` function
- L57–70: `fetch_url()` function

### Changes
1. **Remove** `import time` (line 26)
2. **Remove** `import urllib.error` (line 28)
3. **Remove** lines 30–70 (contact email check, HEADERS, REQUEST_INTERVAL, _last_request_time, _throttle, fetch_url)
4. **Add**: `from fetch.http import HEADERS, fetch_url`
5. **Keep**: `import urllib.request` (needed for `urllib.request.quote()` at line 93)

### Verification
- `fetch/lookup.py` still imports and uses `urllib.request.quote()` correctly
- `fetch_url()` from `http.py` handles rate limiting, caching, offline mode — a superset of the old local version
- `HEADERS` from `http.py` uses `SEC_CONTACT_EMAIL` with `boom.mccloud@gmail.com` fallback (no warning needed)

---

## Part B: P1 #7 — startup validation in run_pipeline.py

**File**: `run_pipeline.py`

### Changes
1. **Add imports**: `import os`, `import shutil` (both are stdlib, no new deps)
2. **Add to `main()`** function, before `try:` block:
```python
# Fast-fail checks
if "LLM_API_KEY" not in os.environ:
    print("Warning: LLM_API_KEY not set. Invariant repair will be unavailable.", file=sys.stderr)
if shutil.which("gws") is None:
    print("Error: gws CLI not found on PATH. Sheet generation requires gws.", file=sys.stderr)
    sys.exit(1)
```

### Rationale
- `LLM_API_KEY`: not fatal (pipeline can complete Stage 4 without LLM if invariants pass), but warn early
- `gws`: fatal — Stage 5 cannot run without it. Fail before wasting time on Stages 1-4
- Commit `9f8af48` added this to legacy scripts but missed `run_pipeline.py`

---

## Part C: Anthropic References Removal

Python code is already clean — zero `import anthropic` or `ANTHROPIC_API_KEY` in any `.py` file.

### Files to update

| File | Line(s) | Change |
|------|---------|--------|
| `requirements.txt` | 1 | Remove `anthropic>=0.40` — dead dependency |
| `test_phase1_e2e.sh` | 6, 20–21 | `ANTHROPIC_API_KEY` → `LLM_API_KEY` |
| `docs/todo/backlog.md` | 34 | `ANTHROPIC_API_KEY` → `LLM_API_KEY` in P1 #7 text |
| `docs/todo/backlog.md` | 38–40 | Update P1 #8 text (Anthropic SDK coupling is already resolved) |
| `docs/todo/forecast-module.md` | 78, 336, 725, 786 | `ANTHROPIC_API_KEY` → `LLM_API_KEY` in all preconditions |
| `docs/todo/forecast-module-spec1.md` | 80, 155, 226, 233 | Replace `anthropic.Anthropic()` / `claude-sonnet-4-6` with OpenAI-compatible patterns |
| `README.md` | 143, 182 | Update model names (Claude → Groq), remove `anthropic` from setup deps |
| `CLAUDE.md` | 105 | Update model names |
| `GEMINI.md` | 149 | Update model names |

### Files to NOT touch
- `docs/FILE_MAP.md` — historical documentation of legacy state (acceptable)
- `tests/fixtures/sec_filings/*.bin` — false positives (SEC filing text contains "Anthropic" as company name)

---

## Execution Order

1. Part A (P0 #1) — independent
2. Part B (P1 #7) — independent
3. Part C (Anthropic cleanup) — doc-only changes, independent

All three can run in any order or parallel. No dependencies between them.
