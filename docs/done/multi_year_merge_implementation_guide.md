# Multi-Year Tree Merge -- Implementation Guide

## Overview

Fix 4 verification bugs in `pymodel.py`, wire the merge pipeline in `run_pipeline.py`, and add residual sanity logging in `merge_trees.py`.

### Success Criteria

All 7 failing tests in `tests/test_merge_pipeline.py` pass. The 1 existing regression guard continues to pass.

### Files to Modify

| File | Changes |
|------|---------|
| `sec-agent/pymodel.py` | Fix `_verify_segment_sums`, replace D&A/SBC value-matching with role tags, add Cash Begin check |
| `sec-agent/run_pipeline.py` | Call `merge_filing_trees()`, run checkpoint on merged file, pass merged file to `sheet_builder.py` |
| `sec-agent/merge_trees.py` | Add WARNING log when residual > sibling average in `_recompute_residuals` |

### Test Command

```bash
python -m pytest sec-agent/tests/test_merge_pipeline.py -v
```

---

## Step 1: Fix `_verify_segment_sums` to use `fv()` instead of `.values.get()`

**File:** `sec-agent/pymodel.py`, lines 121-133
**Passes test:** `TestSegmentSumsUseFv::test_segment_sum_catches_formula_mismatch_at_parent_level`

### Problem

`_verify_segment_sums` currently uses `node.values.get(p, 0)` (declared values) for both the parent and children. When a child has sub-children that don't sum to its declared value, the parent-level check passes incorrectly because it compares declared values, not what `=SUM(children)` would actually produce in the sheet.

### Current Code (lines 121-133)

```python
def _verify_segment_sums(node: TreeNode, periods: list[str],
                          errors: list, label_prefix: str = "Segments"):
    """Recursively verify that children sum to parent at every level."""
    if not node.children:
        return
    for p in periods:
        parent_val = node.values.get(p, 0)
        children_sum = sum(c.values.get(p, 0) * c.weight for c in node.children)
        delta = parent_val - children_sum
        if abs(delta) > 0.5:
            errors.append((f"{label_prefix} ({node.name})", p, delta))
    for child in node.children:
        _verify_segment_sums(child, periods, errors, label_prefix=label_prefix)
```

### Fix

Replace the function. The key change: use a local `fv()` helper that computes formula values recursively (same logic as the module-level `fv()` inside `verify_model`, but standalone so `_verify_segment_sums` doesn't depend on closure scope). The parent value stays as `node.values.get()` (that's the declared target), but children_sum must use `fv()` of each child.

```python
def _verify_segment_sums(node: TreeNode, periods: list[str],
                          errors: list, label_prefix: str = "Segments"):
    """Recursively verify that children sum to parent at every level.

    Uses fv() (formula values) for children, so the check reflects what
    =SUM(children) would actually produce in the sheet.
    """
    def _fv(n, period):
        """Formula value: what =SUM(children) would produce."""
        if not n.children:
            return n.values.get(period, 0)
        return sum(_fv(c, period) * c.weight for c in n.children)

    if not node.children:
        return
    for p in periods:
        parent_val = node.values.get(p, 0)
        children_sum = sum(_fv(c, p) * c.weight for c in node.children)
        delta = parent_val - children_sum
        if abs(delta) > 0.5:
            errors.append((f"{label_prefix} ({node.name})", p, delta))
    for child in node.children:
        _verify_segment_sums(child, periods, errors, label_prefix=label_prefix)
```

### Why This Works

The test creates a parent (Revenue, declared=500) with:
- Child A (ProductRevenue, declared=300) whose sub-children sum to 280
- Child B (ServiceRevenue, leaf, value=200)

Old code: `children_sum = 300 + 200 = 500 == parent_val` -- no error at parent level.
New code: `children_sum = fv(A) + fv(B) = 280 + 200 = 480 != 500` -- reports delta=20 at parent level.

### Common Mistakes

- Do NOT change `parent_val` to use `fv()`. The parent's declared value is the target; only the children sum should use formula values.
- The sub-level check (ProductRevenue: 300 != 280) should still fire too. The recursion handles that.

### Verify

```bash
python -m pytest sec-agent/tests/test_merge_pipeline.py::TestSegmentSumsUseFv -v
```

---

## Step 2: Replace D&A value-matching with role tags

**File:** `sec-agent/pymodel.py`, lines 89-95 (inside the `for p in periods:` loop in `verify_model`)
**Passes test:** `TestDACheckUsesRoleTags::test_da_mismatch_detected_via_role_tags`

### Problem

The D&A check (check #4, lines 89-95) uses `_find_is_value_by_label()` + `_find_cf_match_by_value()`. The CF value-matching heuristic can match a decoy node that happens to have the same value as IS D&A, hiding a real mismatch.

### Current Code (lines 89-95)

```python
        # 4. D&A Link: IS D&A == CF D&A (value-matched)
        # Walk CF_OPCF's children to find a leaf matching IS D&A value
        is_da = _find_is_value_by_label(trees.get("IS"), p, ["depreciation", "amortization"])
        if is_da and is_da != 0:
            cf_da = _find_cf_match_by_value(trees.get("CF"), p, is_da)
            if cf_da is not None:
                check("D&A Link (IS - CF)", p, is_da - cf_da)
```

### Fix

Replace the D&A check block with role-tag-based lookup. Add these lookups near the other `find_node_by_role` calls (around line 31), then replace the check logic:

**Add after line 32 (after `inc_net_cf = ...`):**

```python
    is_da = find_node_by_role(trees["IS"], "IS_DA") if trees.get("IS") else None
    cf_da = find_node_by_role(trees["CF"], "CF_DA") if trees.get("CF") else None
    is_sbc = find_node_by_role(trees["IS"], "IS_SBC") if trees.get("IS") else None
    cf_sbc = find_node_by_role(trees["CF"], "CF_SBC") if trees.get("CF") else None
```

**Replace lines 89-95 with:**

```python
        # 4. D&A Link: IS D&A == CF D&A (role-tag-based)
        if is_da and cf_da:
            is_da_val = fv(is_da, p)
            cf_da_val = fv(cf_da, p)
            if is_da_val != 0:
                check("D&A Link (IS - CF)", p, is_da_val - cf_da_val)
```

### Why This Works

The test creates IS_DA=100 and CF_DA=120, plus a decoy CF node with value=100. Old code matches the decoy via value, sees 100-100=0. New code uses role tags, finds CF_DA=120, reports delta=-20.

### Common Mistakes

- Do NOT remove `_find_is_value_by_label` or `_find_cf_match_by_value` yet -- they may be used elsewhere or as fallbacks in the future. Just stop calling them for D&A/SBC.
- The variable names `is_da`, `cf_da` are now TreeNode objects (not floats). Use `fv(is_da, p)` to get the value.

### Verify

```bash
python -m pytest sec-agent/tests/test_merge_pipeline.py::TestDACheckUsesRoleTags -v
```

---

## Step 3: Replace SBC value-matching with role tags

**File:** `sec-agent/pymodel.py`, lines 97-102
**Passes test:** `TestSBCCheckUsesRoleTags::test_sbc_mismatch_detected_via_role_tags`

### Problem

Identical pattern to D&A. The SBC check uses `_find_cf_match_by_value()` which matches a decoy.

### Current Code (lines 97-102)

```python
        # 5. SBC Link: IS SBC == CF SBC (value-matched)
        is_sbc = _find_is_value_by_label(trees.get("IS"), p, ["stock", "share", "compensation"])
        if is_sbc and is_sbc != 0:
            cf_sbc = _find_cf_match_by_value(trees.get("CF"), p, is_sbc)
            if cf_sbc is not None:
                check("SBC Link (IS - CF)", p, is_sbc - cf_sbc)
```

### Fix

Replace with role-tag-based lookup (the node variables `is_sbc`, `cf_sbc` were already declared in Step 2):

```python
        # 5. SBC Link: IS SBC == CF SBC (role-tag-based)
        if is_sbc and cf_sbc:
            is_sbc_val = fv(is_sbc, p)
            cf_sbc_val = fv(cf_sbc, p)
            if is_sbc_val != 0:
                check("SBC Link (IS - CF)", p, is_sbc_val - cf_sbc_val)
```

### Common Mistakes

- Make sure the variable names don't shadow the node variables. The nodes are `is_sbc`/`cf_sbc` (TreeNode), the per-period values are `is_sbc_val`/`cf_sbc_val` (float).
- If you named the nodes differently in Step 2, update accordingly.

### Verify

```bash
python -m pytest sec-agent/tests/test_merge_pipeline.py::TestSBCCheckUsesRoleTags -v
```

---

## Step 4: Add Cash Begin check (`CF_BEGC[t] == BS_CASH[t-1]`)

**File:** `sec-agent/pymodel.py`, inside `verify_model()`, add after the SBC check (after line ~102) and before the segment sums section (line 104).
**Passes test:** `TestCashBeginCheck::test_cash_begin_mismatch_detected`

### Problem

There is no check that beginning cash on the CF statement matches the prior period's BS cash. This is a fundamental cross-statement invariant for multi-period models.

### Fix

Add a new node lookup (near line 32 area, with the other lookups):

```python
    cf_begc = find_node_by_role(trees["CF"], "CF_BEGC") if trees.get("CF") else None
```

Then add the check inside the `for p in periods:` loop, after the SBC check. The check needs the prior period, so it requires sorted periods and an index lookup:

**Add this block after the SBC check (still inside the `for p in periods:` loop):**

```python
        # 6. Cash Begin: CF_BEGC[t] == BS_CASH[t-1]
        if cf_begc and bs_cash and len(periods) > 1:
            p_idx = periods.index(p)
            if p_idx > 0:
                prev_p = periods[p_idx - 1]
                begc_val = fv(cf_begc, p)
                bs_cash_prev = fv(bs_cash, prev_p)
                if begc_val != 0 and bs_cash_prev != 0:
                    check("Cash Begin (CF_BEGC - BS_CASH[t-1])", p,
                          begc_val - bs_cash_prev)
```

### Why This Works

The test creates:
- BS_CASH: 2023=500, 2024=700
- CF_BEGC: 2023=400, 2024=600

For period 2024 (p_idx=1): `begc_val=600`, `bs_cash_prev=fv(BS_CASH, "2023")=500`, delta=100. Error reported.
For period 2023 (p_idx=0): skipped because there's no prior period.

### Important: `periods` must be sorted

The `periods` list comes from `trees.get("complete_periods", [])` at line 24. This is already sorted chronologically by convention (see `merge_trees.py` line 224: `all_periods = sorted(all_periods)`). If you want to be defensive, sort it:

```python
    periods = sorted(trees.get("complete_periods", []))
```

This change is optional but safe. Add it at line 24 if you want belt-and-suspenders.

### Also update the comment at line 104

The old comment says "6. Segment sums" -- update it to "7. Segment sums" since Cash Begin is now check #6.

### Verify

```bash
python -m pytest sec-agent/tests/test_merge_pipeline.py::TestCashBeginCheck -v
```

---

## Step 5: Wire `merge_trees` into `run_pipeline.py`

**File:** `sec-agent/run_pipeline.py`, lines 93-128
**Passes tests:** `TestPipelineCallsMerge::test_pipeline_merges_multiple_filings` and `TestPipelineHaltsOnCheckpointFailure::test_pipeline_does_not_call_sheet_builder_after_checkpoint_failure`

### Problem

`run_pipeline.py` currently:
1. Runs `xbrl_tree.py` per filing, producing individual `trees_<date>.json` files
2. Runs `pymodel.py --checkpoint` on each individual file (line 118)
3. Passes only `tree_files[0]` to `sheet_builder.py` (line 122)
4. Never calls `merge_trees.py`

### Fix

Replace the Stage 3+4 block (lines 93-128) with:

```python
    # Stage 3: Merge all filings into one tree
    if tree_files:
        if len(tree_files) > 1:
            print(f"\n=== STAGE 3a: Merging {len(tree_files)} filings ===")
            merged_file = str(out_dir / "merged.json")
            run_command([sys.executable, "merge_trees.py"] + tree_files +
                        ["-o", merged_file])
        else:
            print(f"\n=== STAGE 3a: Single filing, no merge needed ===")
            merged_file = tree_files[0]

        # Stage 3b: Verify tree completeness on merged output
        print(f"\n=== STAGE 3b: Verifying model ===")
        import json as _json
        from xbrl_tree import verify_tree_completeness, TreeNode
        with open(merged_file) as _f:
            _trees = _json.load(_f)
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            if stmt in _trees and isinstance(_trees[stmt], dict):
                _trees[stmt] = TreeNode.from_dict(_trees[stmt])
        _periods = _trees.get("complete_periods", [])
        _all_errors = []
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            if _trees.get(stmt):
                _all_errors.extend(verify_tree_completeness(_trees[stmt], _periods))
        if _all_errors:
            print(f"  Tree completeness: {len(_all_errors)} gap(s):", file=sys.stderr)
            for concept, period, gap in _all_errors:
                print(f"    {concept[:50]:50s} {period} gap={gap:>10,.0f}", file=sys.stderr)
            print("  WARNING: Tree gaps detected -- sheet formulas may not match declared values",
                  file=sys.stderr)
        else:
            print(f"  Tree completeness: ALL PASS")

        # Stage 3c: Cross-statement invariant checkpoint on merged file
        # run_command() calls sys.exit(1) on failure, halting before sheet_builder
        run_command([sys.executable, "pymodel.py", "--trees", merged_file, "--checkpoint"])

        # Stage 4: Write Google Sheet from merged tree
        print(f"\n=== STAGE 4: Writing Google Sheet ===")
        run_command([sys.executable, "sheet_builder.py", "--trees", merged_file,
                      "--company", company_name])

        print(f"\n=== STAGE 5: Forecasting (Phase 4 - Coming Soon) ===")
        print(f"Forecasting logic to be implemented in Phase 4.")
    else:
        print("No filings were successfully processed.", file=sys.stderr)
        sys.exit(1)
```

### Key Changes

1. **Merge step:** Calls `merge_trees.py` with all tree files, outputs `merged.json`
2. **Checkpoint on merged:** Runs `pymodel.py --checkpoint` on `merged.json` (not individual files)
3. **Halt on failure:** `run_command()` already calls `sys.exit(1)` on non-zero return -- this is unchanged
4. **Sheet from merged:** Passes `merged_file` (not `tree_files[0]`) to `sheet_builder.py`

### Why This Passes Both Tests

- **Test 5** checks that `run_pipeline.py` contains the word "merge" (case-insensitive). The new code imports and calls `merge_trees.py`.
- **Test 6** checks for both "merge" and "merged" in the source. The new code uses `merged_file` and `merged.json`.

### Common Mistakes

- Do NOT remove the `tree_completeness` check. It should run on the merged file, not per-filing.
- The `run_command()` for checkpoint MUST come before `sheet_builder.py`. If checkpoint fails, `run_command` exits and `sheet_builder` never runs.
- Keep `tree_files` as a list (Stage 2 still populates it). Only the merge/verify/sheet stages change.

### Verify

```bash
python -m pytest sec-agent/tests/test_merge_pipeline.py::TestPipelineCallsMerge -v
python -m pytest sec-agent/tests/test_merge_pipeline.py::TestPipelineHaltsOnCheckpointFailure -v
```

---

## Step 6: Add residual sanity check logging to `merge_trees.py`

**File:** `sec-agent/merge_trees.py`, function `_recompute_residuals` (lines 154-199)
**Passes test:** `TestResidualSanityLogging::test_large_residual_produces_warning`
**Regression guard:** `TestResidualSanityLogging::test_small_residual_no_warning` (already passes)

### Problem

`_recompute_residuals` creates `__OTHER__` nodes but never warns when the residual is suspiciously large relative to its siblings.

### Fix

**Add at the top of the file (line 3, after `import sys`):**

```python
import logging

logger = logging.getLogger(__name__)
```

**Add the warning logic inside `_recompute_residuals`, after the residuals are computed and the `__OTHER__` node is created/updated. Insert after line 199 (before the `elif other_child:` branch), inside the `if new_values:` block.**

The full replacement of the `if new_values:` / `elif` block (lines 187-199):

```python
    if new_values:
        if other_child:
            other_child.values = new_values
        else:
            # Create new __OTHER__ node
            other_child = TreeNode(f"__OTHER__{node.concept}", weight=1.0)
            other_child.name = "Other"
            other_child.values = new_values
            other_child.is_leaf = True
            node.add_child(other_child)
        # Sanity check: warn if residual is larger than sibling average
        for p, residual_val in new_values.items():
            residual_abs = abs(residual_val)
            child_abs_values = [abs(c.values.get(p, 0)) for c in real_children]
            if child_abs_values:
                sibling_avg = sum(child_abs_values) / len(child_abs_values)
                if residual_abs > sibling_avg:
                    logger.warning(
                        "Large residual for %s period %s: "
                        "residual=%.0f, sibling_avg=%.0f",
                        node.concept, p, residual_abs, sibling_avg
                    )
    elif other_child:
        # No residual needed - remove __OTHER__
        node.children.remove(other_child)
```

### Why This Works

The test creates parent=1000 with children summing to 300. Residual=700, sibling_avg=150. Since 700 > 150, the WARNING is logged. The test captures it via `caplog` and checks for records with level >= WARNING containing "residual" or "Revenue".

The regression guard test has parent=310, children=300, residual=10, sibling_avg=150. Since 10 < 150, no warning.

### Common Mistakes

- Use `logging.getLogger(__name__)` not `logging.getLogger("merge_trees")`. The `__name__` form is standard and works correctly with pytest's `caplog`.
- The `real_children` variable is already computed at line 176. Make sure your warning code is inside the scope where `real_children` is defined (it is, since it's all inside the outer `if not node.children` guard).
- Do NOT use `print()` for the warning. The test uses `caplog` which only captures `logging` module output.

### Verify

```bash
python -m pytest sec-agent/tests/test_merge_pipeline.py::TestResidualSanityLogging -v
```

---

## Unit Tests to Add

These test internal logic at a finer grain than the integration tests above. Add to a new file `sec-agent/tests/test_pymodel_units.py`:

### 1. `test_fv_returns_leaf_value`
Verify that `fv(leaf_node, period)` returns the node's declared value.

### 2. `test_fv_returns_sum_of_children`
Verify that `fv(parent, period)` returns `sum(fv(child) * weight)`, not the declared value.

### 3. `test_fv_respects_negative_weights`
Create a child with `weight=-1` and verify `fv()` subtracts it.

### 4. `test_da_role_tag_lookup_returns_none_when_missing`
Verify that `find_node_by_role(tree, "IS_DA")` returns `None` when no node has that role, and the D&A check is silently skipped (no error, no crash).

### 5. `test_cash_begin_skipped_for_single_period`
Verify that with only 1 period, the Cash Begin check produces no errors (no prior period to compare).

### 6. `test_residual_warning_multiple_periods`
Create a tree with 2 periods where only one has a large residual. Verify warning fires for the bad period only.

---

## Full Verification

After all steps:

```bash
python -m pytest sec-agent/tests/test_merge_pipeline.py -v
```

Expected: 8/8 pass (7 previously failing + 1 regression guard).

Then run the full test suite to check for regressions:

```bash
python -m pytest sec-agent/tests/ -v
```

Also compile-check modified files:

```bash
python -m py_compile sec-agent/pymodel.py
python -m py_compile sec-agent/run_pipeline.py
python -m py_compile sec-agent/merge_trees.py
```
