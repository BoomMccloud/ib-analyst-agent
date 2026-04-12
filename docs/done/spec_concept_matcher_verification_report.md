# Spec Verification Report: BS_CASH Fix + Matching Cleanup

**Date**: 2026-04-12
**Spec**: `docs/todo/spec_concept_matcher.md` (inline spec provided)
**Overall Status**: WARNINGS

---

## Blocking Issues

None.

---

## Warnings

### 1. Line number for `_tag_bs_positions` is off
- **Spec says**: `_tag_bs_positions` at `xbrl_tree.py:626`
- **Actual**: Function definition is at **line 613**. Line 626 is the `children[0].role = "BS_CASH"` line itself.
- **Impact**: Low. The spec conflates the function line with the buggy line. The buggy line IS at 626.

### 2. `_find_leaf_by_keywords` and `_find_by_keywords_bfs` have meaningful differences
- **Spec says**: "similar DFS/BFS keyword searches that could be unified"
- **Actual differences**:
  - `_find_leaf_by_keywords` uses DFS, matches on `node.name`, requires **all** keywords match, only returns **leaf** nodes.
  - `_find_by_keywords_bfs` uses BFS, matches on `node.concept`, requires **any** keyword match, returns **any** node (not just leaves).
- **Impact**: Medium. Unification must account for these 3 behavioral differences (search order, match field, match logic). The spec's proposed `_find_by_keywords` signature with `mode/search/leaf_only` parameters addresses this, but implementers should be aware of the `all` vs `any` keyword matching difference.

### 3. `_find_is_value_by_label` and `_find_cf_match_by_value` are not called but are referenced in test comments
- **Spec says**: "never called"
- **Actual**: The functions are defined but never **invoked** in production code. However, `tests/test_merge_pipeline.py` references them in comments/assertions (lines 111, 124, 170, 225) as part of xfail test messages explaining expected behavior.
- **Impact**: Low. Deleting the functions won't break any test execution, but test docstrings/messages referencing them should be updated for clarity.

---

## Verified Items

| # | Claim | Status | Details |
|---|-------|--------|---------|
| 1 | `_tag_bs_positions` exists | PASS | Defined at `xbrl_tree.py:613` |
| 2 | `children[0].role = "BS_CASH"` at line 626 | PASS | Exact code at line 626: `child.children[0].role = "BS_CASH"` |
| 3 | Position-based tagging is the bug | PASS | Uses `children[0]` with no concept-name check; ordering depends on linkbase |
| 4 | `_find_is_value_by_label` exists at pymodel.py:161 | PASS | Exact line match |
| 5 | `_find_cf_match_by_value` exists at pymodel.py:184 | PASS | Exact line match |
| 6 | Both functions are dead code | PASS | No call sites in any `.py` file (only def sites and doc/comment references) |
| 7 | `_find_leaf_by_keywords` at xbrl_tree.py:684 | PASS | Exact line match, signature: `(tree: TreeNode, keywords: list[str]) -> TreeNode \| None` |
| 8 | `_find_by_keywords_bfs` at xbrl_tree.py:700 | PASS | Exact line match, signature: `(tree: 'TreeNode', keywords: list[str]) -> 'TreeNode \| None'` |
| 9 | TreeNode has `.concept` attribute | PASS | `self.concept = concept` at line 408 |
| 10 | TreeNode has `.name` attribute | PASS | `self.name = _clean_name(concept)` at line 410 |
| 11 | TreeNode has `.role` attribute | PASS | `self.role: str \| None = None` at line 415 |
| 12 | TreeNode has `.children` attribute | PASS | `self.children: list[TreeNode] = []` at line 412 |
| 13 | TSLA cached filing exists | PASS | `pipeline_output/validation/TSLA/trees_2026-01-29.json` exists |
| 14 | TSLA fixture exists in test fixtures | PASS | `tests/fixtures/sec_filings/TSLA/trees.json` also has `BS_CASH` role |

---

## Existing Tests That May Need Updating

| Test File | Relevance |
|-----------|-----------|
| `tests/test_model_historical.py` | Tests `BS_CASH` role lookup (lines 160, 200-204) |
| `tests/test_pymodel_units.py` | Creates nodes with `role="BS_CASH"` (line 169) |
| `tests/test_merge_pipeline.py` | Creates nodes with `role="BS_CASH"`, references dead functions in comments |
| `tests/test_dual_linkbase.py` | Manually sets `BS_CASH` role (line 649), creates nodes with it (line 695) |
| `tests/test_da_sbc_tagging.py` | Tests D&A/SBC tagging -- may use `_find_leaf_by_keywords` |
| `tests/test_offline_e2e.py` | End-to-end test that may exercise BS tagging path |

---

## Summary

The spec is accurate on all key claims. The BS_CASH bug at line 626, the dead code in pymodel.py, and the keyword search duplication all exist as described. The main caution for implementation is that the two keyword search functions differ in 3 ways (DFS vs BFS, name vs concept, all-match vs any-match), not just traversal order. The unified function must preserve both behaviors via parameters.
