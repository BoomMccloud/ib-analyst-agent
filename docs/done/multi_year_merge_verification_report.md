# Multi-Year Tree Merge — Verification Report

**Date**: 2026-04-12
**Spec**: Multi-Year Tree Merge Design Spec
**Codebase**: sec-agent/

---

## 1. Does `merge_trees.py` exist and implement what the spec describes?

**Status: EXISTS — PARTIAL MATCH**

`merge_trees.py` exists at `sec-agent/merge_trees.py` (340 lines). It implements:

- **Step 1a (Exact matches)**: YES — `_collect_all_concepts()` collects all concept+values, then `_merge_values_by_concept()` fills matching concepts.
- **Step 1b (Rename detection)**: YES — `_build_rename_map()` uses value matching at overlap period with uniqueness check. Skips `__OTHER__` nodes. Chains renames (lines 257-262).
- **Step 1c (Reclassification detection)**: NOT IMPLEMENTED — No explicit reclassification detection. Same-concept-different-value cases are not handled; the newest filing's value simply wins because `_merge_values_by_concept()` only fills periods not already present (`if p not in base_node.values`).
- **Step 1d (Orphan detection)**: YES — `_find_orphans()` finds concepts only in older filings, returns them grouped by parent.
- **Step 2a (Skeleton from newest)**: YES — `merge_filing_trees()` uses `all_data[0]` (newest filing) as skeleton.
- **Step 2b (Orphan insertion with gap-reduction gate)**: YES — Lines 283-307 check that adding an orphan reduces the parent gap without making it worse.
- **Step 2c (Residual recomputation)**: YES — `_recompute_residuals()` creates/updates/removes `__OTHER__` nodes.
- **Step 2d (Residual sanity check)**: NOT IMPLEMENTED — No logging of residuals after recomputation for sanity checking.

**Spec claim about `ConceptMap` output**: MISMATCH — The spec describes a `ConceptMap` data structure. The actual implementation does not create this intermediate structure; it operates directly on TreeNode objects.

---

## 2. Does `pymodel.py` have the functions mentioned?

**Status: EXISTS — MATCH with caveats**

### `verify_model()` — EXISTS (line 8)
- Takes `trees: dict`, returns `list[tuple]` of errors. 
- Currently runs **5 checks** (BS Balance, Cash Link, NI Link, D&A Link, SBC Link) plus segment sums.
- The spec says "7 checks" — the current 5 + segments + proposed Cash Begin check = 7. MATCH with the spec's Phase 3 description.

### `fv()` — EXISTS (line 59)
- Defined as nested function inside `verify_model()`.
- Recursively computes `sum(fv(child) * child.weight)` for branch nodes, returns declared value for leaves.
- **Spec claim "All checks use fv() not nv()"**: PARTIALLY TRUE. Checks 1 (BS Balance), 2 (Cash Link for BS_CASH side), and 3 (NI Link) use `fv()`. But checks 4 (D&A) and 5 (SBC) use `_find_is_value_by_label()` and `_find_cf_match_by_value()` which use `node.values.get()` (declared values), NOT `fv()`.

### `nv()` — EXISTS (line 35)
- Simple helper: `node.values.get(period, 0)` — returns the declared XBRL value.
- **Spec claim about nv() vs fv()**: MATCH. `nv()` = declared value, `fv()` = formula value (SUM of children).
- **Current usage**: `nv()` is defined but NOT used in any check. All checks either use `fv()` or direct `.values.get()` via helper functions. The `nv()` function is dead code.

### `_verify_segment_sums()` — EXISTS (line 121)

### Cash Begin check — NOT FOUND
- The spec proposes adding check #3: `CF_BEGC[t] == fv(BS_CASH)[t-1]`. This does not exist yet.

---

## 3. Does `run_pipeline.py` currently ignore `merge_trees.py`?

**Status: MATCH**

`run_pipeline.py` (133 lines) has zero references to `merge_trees` or `merge_filing_trees`. Confirmed via grep.

The pipeline:
1. Fetches filings via `agent1_fetcher.py`
2. Runs `xbrl_tree.py` per filing, collecting `tree_files`
3. Runs `verify_tree_completeness()` and `pymodel.py --checkpoint` per individual filing
4. Passes only `tree_files[0]` (most recent) to `sheet_builder.py`

**The spec's claim that "run_pipeline.py ignores merge_trees.py entirely, runs sheet_builder.py on only the most recent filing" is 100% accurate.**

---

## 4. Do the role tags (`IS_DA`, `CF_DA`, `IS_SBC`, `CF_SBC`) exist?

**Status: EXISTS**

- Defined in `xbrl_tree.py` at lines 972, 976, 988, 991 inside `_tag_da_sbc_nodes()`.
- Referenced in `CROSS_STATEMENT_CHECKS` (lines 906-907) for sheet formula generation.
- Confirmed present in actual output files (e.g., `pipeline_output/trees_UNH.json`, `pipeline_output/trees_HD.json`).
- Test coverage in `tests/test_da_sbc_tagging.py`.

**However**: `pymodel.py` does NOT use these role tags for D&A/SBC verification. It uses `_find_is_value_by_label()` (keyword search) and `_find_cf_match_by_value()` (value matching heuristic) instead. The spec correctly identifies this as a problem to fix.

---

## 5. Does `xbrl_tree.py` output the structure described?

**Status: MATCH**

The `build_statement_trees()` function (line 1628) returns a dict containing:
- `"IS"`, `"BS"`, `"BS_LE"`, `"CF"` — TreeNode objects (serialized via `to_dict()`)
- `"facts"` — `{tag: {period: value}}`
- `"complete_periods"` — list of period strings
- `"cf_endc_values"` — `{period: value}` (set during `reconcile_trees()` at line 1609)
- `"unit_label"` — string (line 1646)
- `"revenue_segments"` — TreeNode (optional, line 1706)

The spec's Phase 0 output structure matches exactly.

---

## 6. Does `reconcile_trees()` exist and do what's claimed?

**Status: EXISTS — MATCH**

`reconcile_trees()` at line 1588 performs:
- Step A: Tag BS positions (`_tag_bs_positions`)
- Step B: Tag CF positions + find CF_ENDC (`_tag_cf_positions`)
- Step C: Tag IS positions using CF's NI (`_tag_is_positions`)
- Step D: Tag IS Revenue/COGS semantically (`_tag_is_semantic`)
- Step E: Cross-statement override: BS_CASH = CF_ENDC (`_override_bs_cash`)
- Step F: Filter to complete periods (`_filter_to_complete_periods`)
- Step G: Merge calc+pres ordering + __OTHER__ rows (`merge_calc_pres`)
- Step H: Tag D&A/SBC nodes (`_tag_da_sbc_nodes`)

The spec's claim that it "tags positions, applies cross-statement overrides, merges calc+presentation ordering" is accurate.

---

## 7. Does `verify_tree_completeness()` exist?

**Status: EXISTS — MATCH**

Line 824. Checks `SUM(children * weight) == declared` for all branch nodes across all periods. Returns list of `(concept, period, gap)` errors. Threshold is `> 1.0`.

The spec's claim that it "confirms formula integrity per filing" is accurate.

---

## 8. Do `_attach_is_segments` and `_build_revenue_segment_tree` exist?

**Status: EXISTS — MATCH**

- `_attach_is_segments()` at line 1300: Attaches segment breakdowns to IS Revenue and COGS nodes. Tries shared dimension first, falls back to independent decomposition. Sum-verified before attachment.
- `_build_revenue_segment_tree()` at line 1378: Builds hierarchical revenue segment tree (BusinessSegmentsAxis outer, ProductOrServiceAxis inner). Sum-verified.

The spec's claim that "segment decomposition is sum-verified before attachment" is accurate.

---

## 9. Is `_verify_segment_sums` using declared values (bug)?

**Status: MATCH — BUG CONFIRMED**

`_verify_segment_sums()` at line 121:
```python
parent_val = node.values.get(p, 0)
children_sum = sum(c.values.get(p, 0) * c.weight for c in node.children)
```

This uses `node.values.get()` (declared values) for BOTH parent and children. It does NOT use `fv()`. This means it checks `declared_parent == SUM(declared_children)`, not `fv(parent) == SUM(fv(children))`.

For single-filing trees where `__OTHER__` residuals ensure `SUM(children) == declared`, this works. But for merged trees where values may have been patched, declared values and formula values can diverge. The spec correctly identifies this as needing a fix.

---

## 10. Does `_find_cf_match_by_value` exist?

**Status: EXISTS — MATCH**

Line 159. Searches CF tree leaves under `CF_OPCF` for one whose value matches the target within 0.5. Used for D&A and SBC checks as a heuristic.

The spec correctly identifies this as the heuristic to replace with role-tag-based lookup.

---

## 11. What does `nv()` actually do vs `fv()`?

**Status: MATCH**

- **`nv(node, period)`** (line 35): Returns `node.values.get(period, 0)` — the declared XBRL value stored on the node. Dead code (defined but never called).
- **`fv(node, period)`** (line 59): Recursively computes `sum(fv(child, period) * child.weight for child in node.children)` for branch nodes; returns `node.values.get(period, 0)` for leaves. This is what `=SUM(children)` would produce in the spreadsheet.

The distinction matters because after merging, a parent's declared value may not equal the sum of its children's declared values (e.g., if an orphan was added or a rename was applied).

---

## Summary of Findings

### Spec claims that are ACCURATE:
1. `xbrl_tree.py` output structure matches spec's Phase 0 description
2. `reconcile_trees()` does what's claimed (tagging, overrides, calc+pres merge)
3. `verify_tree_completeness()` confirms formula integrity
4. `_attach_is_segments` and `_build_revenue_segment_tree` exist with sum verification
5. `run_pipeline.py` ignores `merge_trees.py` and only uses most recent filing
6. Role tags `IS_DA`, `CF_DA`, `IS_SBC`, `CF_SBC` exist in xbrl_tree.py
7. `pymodel.py` D&A/SBC checks use value heuristics instead of role tags (correctly identified as problem)
8. `_verify_segment_sums` uses declared values not formula values (correctly identified as bug)
9. `nv()` vs `fv()` distinction is accurately described
10. `merge_trees.py` implements the core merge logic described

### Spec claims that are INACCURATE or MISSING:
1. **ConceptMap**: The spec describes an output data structure called `ConceptMap`. The actual `merge_trees.py` does not produce this; it works directly on TreeNode objects. This is a design proposal, not a description of existing code.
2. **Reclassification detection (Step 1c)**: Not explicitly implemented. Newest filing wins implicitly.
3. **Residual sanity check (Step 2d)**: Not implemented. No post-recomputation logging.
4. **`nv()` is dead code**: The spec implies `nv()` is actively used in checks that should be converted to `fv()`. In reality, `nv()` is defined but never called. The actual issue is that D&A/SBC checks use `.values.get()` via helper functions, and `_verify_segment_sums` uses `.values.get()` directly.
5. **Check count**: The spec says "7 checks" but the current code has 5 named checks + segment sums (which is variable). The proposed Cash Begin check would make 6 named checks + segments.

### BLOCKING ISSUES: None

The spec is fundamentally sound. The inaccuracies are minor (ConceptMap is a design choice, not a code reference error; nv() being dead code doesn't affect the fix plan). All referenced files, functions, and behaviors exist as described. The bugs identified (segment sums using declared values, D&A/SBC using heuristics instead of role tags, pipeline not wired) are confirmed.

**Overall Status: PASS (with warnings)**
