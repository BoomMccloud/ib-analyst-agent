# Spec Verification Report

**Spec**: `sec-agent/make_demo.md`
**Verified**: 2026-04-13
**Overall Status**: ⚠️ WARNINGS (no blockers — plan is implementable as written, with two clarifications)

---

## Summary

- **Files**: 6 verified, 1 duplicate-location issue
- **Methods/Functions**: 5 verified, 1 to-be-created (correctly identified by spec)
- **Libraries**: 0 verified (no manifest), 1 note
- **Data Models**: N/A
- **Naming**: Consistent with codebase

---

## Blocking Issues

**None.** Every claim the spec makes about existing code is accurate, and every "to be created" item is correctly flagged.

---

## Warnings

### [WARN-001] Spec exists in two locations

The same file lives at:
- `sec-agent/make_demo.md` ← the one being actively edited
- `sec-agent/docs/todo/make_demo.md` ← stale duplicate

**Recommendation**: Decide on a canonical location (likely `docs/todo/`, matching `spec_architecture_refactor.md` and `forecast-module.md`) and delete the other. Otherwise future edits will drift.

### [WARN-002] `company_tickers.json` is HTTP-fetched, not a local file

**Spec says** (line 129–130): *"Loads `company_tickers.json` once, caches in module global."*

**Reality**: `lookup_company.py:74` calls `fetch_json(TICKERS_URL)` — i.e., it pulls from `https://www.sec.gov/files/company_tickers.json` over the network on every call. There is no local copy of `company_tickers.json` in the repo or `.cache/` (the `.cache/` directory contains opaque `.bin` HTTP-response blobs, not a file with that name).

**Implication**: The "module-global cache" pattern is still the right design, but the spec should be explicit that the *first* `search_tickers` call will trigger an SEC EDGAR HTTP request (rate-limited, ~125ms minimum). Subsequent calls hit the in-memory cache.

**Recommendation**: Add one line to the `search_tickers` bullet:
> *"First call fetches from SEC EDGAR via `fetch_json(TICKERS_URL)`; subsequent calls use the module-global cache."*

### [WARN-003] No dependency manifest in the project

The project has **no** `requirements.txt`, `pyproject.toml`, or `setup.py` (verified across the repo root and `sec-agent/`). Dependencies are managed ad-hoc.

**Implication**: The spec's `pip install fastapi uvicorn[standard]` instruction will work, but there's no manifest to record the new dependencies in. The "Dependencies" section just says "Document in README" — that's consistent with how the rest of the project handles deps, so this is more an FYI than an issue.

**Recommendation**: No change needed unless you want this demo to be the forcing function for adding a `requirements.txt`. Otherwise, the README note is sufficient.

---

## Verified Items ✅

| Category | Reference | Status |
|---|---|---|
| File | `sec-agent/run_pipeline.py` | ✅ Exists, is fully subprocess-driven (`subprocess.run` shelling to `agent1_fetcher.py`, `xbrl_tree.py`, `merge_trees.py`, `sheet_builder.py`) — matches spec's description exactly |
| File | `sec-agent/lookup_company.py` | ✅ Exists |
| File | `sec-agent/sheets/__init__.py` | ✅ Exists (post-refactor package) |
| File | `sec-agent/xbrl/` package | ✅ Exists with `linkbase.py`, `tree.py`, `reconcile.py`, `segments.py` |
| File | `sec-agent/merge_trees.py` | ✅ Exists |
| File | `sec-agent/pymodel.py` | ✅ Exists |
| Function | `sheets.write_sheets(trees, company)` | ✅ Exists at `sheets/__init__.py:11`, **returns `(sid, url)` at line 169** — exactly the signature the spec relies on. WARN-002 in earlier draft is resolved. |
| Function | `lookup_company.lookup_by_ticker` | ✅ Exists at `lookup_company.py:72` (the existing function the spec promises to leave untouched) |
| Function | `lookup_company.search_tickers` | ⚪ **Does not exist** — correctly identified by spec as needing creation |
| Function | `merge_trees.merge_filing_trees(tree_files)` | ✅ Exists at `merge_trees.py:131` (available for in-process call from the rewritten `run_pipeline`) |
| Directory | `sec-agent/web/` | ⚪ Does not exist — correctly identified as new |
| Symbol | `TICKERS_URL` constant in `lookup_company.py:35` | ✅ Available for `search_tickers` to reuse |
| Architecture | xbrl/ and sheets/ refactor packages | ✅ Done — the spec's "Decision: in-process vs subprocess" rationale is grounded in real, existing import surfaces |

---

## Recommendations

1. **No blockers.** The plan is implementable as written. Proceed with build order step 1.
2. **Apply WARN-001**: Pick one location for `make_demo.md` and delete the other before they drift.
3. **Apply WARN-002**: Add the one-line clarification to the `search_tickers` bullet that the first call fetches from SEC EDGAR. Avoids a "why is the first search slow?" surprise during the demo.
4. **WARN-003 is optional** — no action needed unless you want this to be the moment you finally add a `requirements.txt`.

The strongest verification signal: the spec's most load-bearing claim — that `sheets.write_sheets()` returns `(sid, url)` after the refactor — is **confirmed exactly**. The "verify signature" step in the build order (step 2) can be checked off without writing any code.
