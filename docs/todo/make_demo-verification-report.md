# Spec Verification Report (Round 2)

**Spec**: `sec-agent/docs/todo/make_demo.md`
**Verified**: 2026-04-13 (post-edits)
**Overall Status**: ❌ ONE BLOCKING ISSUE + 1 minor naming nit

Round 1 warnings (WARN-001 duplicate, WARN-002 fetch source, WARN-003 manifest) are all resolved or addressed. New verification of the "Internal flow" section turned up one real bug.

---

## Blocking Issues

### [ISSUE-001] `agent1_fetcher.run()` calls `sys.exit(1)` on lookup failure — will wedge the demo

**Spec says** (line 143, 152): *"`agent1_fetcher.run(query, years) -> dict` already exists (line 26) — use it as-is, no refactor needed."* and the internal flow calls it directly.

**Reality** (`agent1_fetcher.py:34-36`):
```python
result = lookup_by_name(query)
if not result:
    print(f"Error: Could not find '{query}' on SEC EDGAR", file=sys.stderr)
    sys.exit(1)
```

**Why this is blocking, not minor:** `sys.exit(1)` raises `SystemExit`, which inherits from `BaseException`, **not** `Exception`. The web layer's worker uses `except Exception` (line 77 of the skeleton):

```python
def _worker(job_id, ticker, years):
    try:
        result = run_pipeline(ticker, years, on_progress=_update)
        ...
    except Exception as e:                    # <-- does NOT catch SystemExit
        ...
```

If a user searches for a typo or a ticker SEC doesn't recognize, `agent1_fetcher.run()` calls `sys.exit(1)` → `SystemExit` propagates past the `except Exception` → the worker thread dies silently → `_state["status"]` stays `"running"` forever → the UI polls indefinitely → next `start_job` returns 409 because status is still `"running"` → **the entire demo is wedged until you ctrl-C the server.**

This is exactly the "stuck running" failure mode the plan claims to be immune to ("Bulletproof termination" on line 115).

**Fix options** (in order of preference):

1. **Refactor `agent1_fetcher.run()`** to raise instead of exit. Two-line change at line 34-36:
   ```python
   if not result:
       raise RuntimeError(f"Could not find '{query}' on SEC EDGAR")
   ```
   The script's CLI `main()` (line 66) is unaffected because it doesn't catch — `RuntimeError` will propagate, Python prints traceback, exit code is non-zero. Same UX from the CLI.

2. **Wrap the call in `run_pipeline`** with `try/except SystemExit` and re-raise as `RuntimeError`. Works but is a hack — the right answer is to fix `agent1_fetcher.run()`.

3. **Change the worker to `except BaseException`.** Catches SystemExit but also catches `KeyboardInterrupt` — bad, breaks ctrl-C cleanup of the worker thread.

**Recommendation:** Option 1, and add to the plan that `agent1_fetcher.py:34-36` needs a `sys.exit → raise RuntimeError` flip (same treatment the plan already prescribes for `pymodel.py`).

---

## Warnings

### [WARN-001] Module name typo in Internal flow

**Spec says** (line 153): *"call `xbrl_tree.build_statement_trees(html, base_url)`"*

**Reality**: After the architecture refactor, the function lives at `xbrl/__init__.py:36`, so the import path is `xbrl.build_statement_trees`, not `xbrl_tree.build_statement_trees`. (`xbrl_tree.py` is now a thin facade — it may re-export, but the canonical path matches what step 2 of the same section says: *"`xbrl.*`, `sheets.write_sheets`, ..."*.)

**Fix**: One-character edit in line 153, `xbrl_tree.` → `xbrl.`.

---

## Verified Items ✅

| Category | Reference | Status |
|---|---|---|
| Function | `agent1_fetcher.run(query, years) -> dict` at line 26 | ✅ Exists. Returns `{company, ticker, cik, filer_type, filing_type, state_of_incorporation, country, filing_count, filings}` — superset of the spec's claimed shape. |
| Function | `xbrl.build_statement_trees(html, base_url)` at `xbrl/__init__.py:36` | ✅ Exists, signature matches |
| Function | `merge_trees.merge_filing_trees(tree_files)` at `merge_trees.py:131` | ✅ Exists |
| Function | `pymodel.verify_model(trees: dict) -> list[tuple]` at `pymodel.py:8` | ✅ Exists, signature matches |
| Function | `pymodel.main()` calls `sys.exit(1)` at line 173 | ✅ Confirmed — spec correctly identifies this as needing extraction |
| Function | `pymodel.main()` imports `llm_invariant_fixer.fix_invariants` at line 160 | ✅ Confirmed |
| Function | `sheets.write_sheets(trees, company)` returns `(sid, url)` at `sheets/__init__.py:169` | ✅ Confirmed |
| Function | `lookup_company.lookup_by_ticker` at line 72 | ✅ Exists |
| File | `lookup_company.py:33` env var hard-fail | ✅ Confirmed (acceptable per spec) |
| File | `lookup_company.py:162` `sys.exit(1)` | ✅ Confirmed in `main()`, not import-time |
| File | Spec location `sec-agent/docs/todo/make_demo.md` | ✅ Now canonical (root duplicate gone) |

---

## Recommendations

1. **Fix ISSUE-001 before starting build step 3.** Add a one-liner to the `run_pipeline.py` rewrite section: *"`agent1_fetcher.py:34-36` — replace `sys.exit(1)` with `raise RuntimeError(...)` (same treatment as `pymodel.main()`). Without this, the demo wedges on any misspelled ticker because `SystemExit` escapes the worker's `except Exception`."*
2. **Apply WARN-001** — `xbrl_tree.` → `xbrl.` in line 153.
3. Everything else verified clean. The plan is implementable as soon as ISSUE-001 is acknowledged.
