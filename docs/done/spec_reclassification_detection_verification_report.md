# Spec Verification Report: Reclassification Detection

**Date**: 2026-04-12
**Spec**: Reclassification Detection (Phase 1, Step 1c)
**Overall Status**: PASS

---

## Summary

| Category | Count |
|----------|-------|
| Verified | 14 |
| Warnings | 2 |
| Blocking Issues | 0 |

---

## Blocking Issues

None.

---

## Warnings

### W1: Pass numbering mismatch
The spec says "Pass 5: Recompute residuals" but in the actual code, the current `_recompute_residuals` call is after Pass 4 (orphan insertion), at line 328. The code comments call it "Pass 5" already. The new pass would be inserted between line 326 (end of orphan insertion) and line 328 (residual recompute). The spec's numbering as "Pass 4b" is correct in intent but should reference the exact insertion point: **between the orphan loop ending at line 326 and `_recompute_residuals` at line 328**.

### W2: Spec says `_build_rename_map` is at "line 66" — confirmed
The spec's claim about line 66 holds exactly. However, the spec should note that `_build_rename_map` only detects renames when the **old** concept doesn't exist in the **newer** (base/prev) tree (line 82: `if n.concept not in base_concepts`). This means it cannot detect the TSLA case where both `Revenues` (parent) and `RevenueFromContractWithCustomer` (child) exist in the base tree — validating the need for the new structural shift detection.

---

## Verified Items

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 1 | `merge_filing_trees()` exists in `merge_trees.py` | VERIFIED | Line 220 |
| 2 | Pass structure: Pass 1 (collect), Pass 2 (renames), Pass 3 (values), Pass 4 (orphans), Pass 5 (residuals) | VERIFIED | Lines 249, 259, 287, 290, 328 |
| 3 | `_recompute_residuals` exists | VERIFIED | Line 159, called at line 328 |
| 4 | Pass 4b insertion point: between orphan loop (line 326) and `_recompute_residuals` (line 328) | VERIFIED | Clear insertion point exists |
| 5 | `TreeNode.values` attribute (dict[str, float]) | VERIFIED | `xbrl_tree.py` line 413 |
| 6 | `TreeNode.children` attribute (list[TreeNode]) | VERIFIED | `xbrl_tree.py` line 412 |
| 7 | `TreeNode.concept` attribute (str) | VERIFIED | `xbrl_tree.py` line 408 |
| 8 | `TreeNode.role` attribute (str or None) | VERIFIED | `xbrl_tree.py` line 415 |
| 9 | `TreeNode.weight` attribute (float) | VERIFIED | `xbrl_tree.py` line 411 |
| 10 | `TreeNode.add_child()` method | VERIFIED | `xbrl_tree.py` line 417 |
| 11 | `find_node_by_role()` exists in `xbrl_tree.py` | VERIFIED | Line 603 |
| 12 | `poc_reclassification.py` exists and validates approach | VERIFIED | File exists with parent-child rename detection logic matching spec's Pattern 1 |
| 13 | TSLA tree files exist in `pipeline_output/validation/TSLA/` | VERIFIED | 5 tree files (2022-02-07 through 2026-01-29) + merged.json + merged_proper.json |
| 14 | Naming convention `_detect_and_fix_structural_shifts` | VERIFIED | Matches codebase pattern: all private functions use `_snake_case` (e.g., `_build_rename_map`, `_find_orphans`, `_recompute_residuals`) |

---

## Recommendations

1. **Insertion point is clean**: The new `_detect_and_fix_structural_shifts()` call should go at line 327 (after the orphan `else` block, before `_recompute_residuals`). No refactoring needed.

2. **POC alignment**: The POC's `detect_parent_child_renames()` closely matches the spec's Pattern 1 detection criteria. The spec adds Pattern 2 (sibling replacement) which the POC doesn't cover — this is new work but follows the same detection structure.

3. **`_build_rename_map` limitation confirmed**: The existing rename detection (Pass 2) requires the old concept to be absent from the base tree. The TSLA reclassification case has both concepts present (parent and child), so it correctly falls through to the new Pass 4b detection.

4. **Available context**: `merge_filing_trees()` already has `all_periods`, `base_tree`, and the statement loop — all needed by the new function. No new data plumbing required.
