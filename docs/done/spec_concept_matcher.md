# BS_CASH Fix + Matching Cleanup — Spec

**Date**: 2026-04-12
**Context**: The TSLA BS_CASH bug (see `spec_multi_year_merge_bugs.md`) exposed that position-based `children[0]` in `_tag_bs_positions` tags the wrong node when calc vs presentation order differs. KISS evaluation determined a module extraction is premature — fix the bug inline and clean up dead code.

---

## Problem

1. **BS_CASH bug**: `_tag_bs_positions` (xbrl_tree.py:613, buggy line at 626) does `child.children[0].role = "BS_CASH"`. When calculation linkbase ordering differs from presentation ordering (TSLA), this tags `PrepaidExpenseAndOtherAssetsCurrent` instead of `CashAndCashEquivalentsAtCarryingValue`.

2. **Dead code**: `pymodel.py` contains two matching functions (`_find_is_value_by_label` at line 161, `_find_cf_match_by_value` at line 184) that are never called — D&A/SBC verification already uses `find_node_by_role()`.

3. **Minor duplication**: `_find_leaf_by_keywords` (xbrl_tree.py:684) and `_find_by_keywords_bfs` (xbrl_tree.py:700) are similar DFS/BFS keyword searches that could be unified.

---

## Changes

### 1. Fix BS_CASH tagging in `_tag_bs_positions`

Replace `children[0].role = "BS_CASH"` with a concept-name match:

```python
# Instead of: child.children[0].role = "BS_CASH"
# Do: find the child whose concept starts with a cash-related pattern
cash_node = None
for c in child.children:
    concept = c.concept.split(":")[-1] if c.concept else ""
    if concept.startswith("CashAndCashEquivalent"):
        cash_node = c
        break
if cash_node is None:
    cash_node = child.children[0]  # position fallback
cash_node.role = "BS_CASH"
```

### 2. Delete dead code from pymodel.py

Remove `_find_is_value_by_label` (line 161) and `_find_cf_match_by_value` (line 184). They are never called. Also update references to these functions in `tests/test_merge_pipeline.py` comments/docstrings.

### 3. Unify keyword search functions in xbrl_tree.py

Merge `_find_leaf_by_keywords` and `_find_by_keywords_bfs` into a single function:

```python
def _find_by_keywords(tree, keywords, mode="all", search="dfs", leaf_only=True, field="name"):
    """Search tree for a node matching keywords.
    
    mode: "all" = all keywords must match, "any" = any keyword matches
    search: "dfs" or "bfs"
    leaf_only: if True, only match leaf nodes
    field: "name" (cleaned display name) or "concept" (raw XBRL concept)
    
    Note: existing callers differ in 3 ways:
    - _find_leaf_by_keywords: dfs, all-match, leaf-only, searches .name
    - _find_by_keywords_bfs: bfs, any-match, non-leaf, searches .concept
    """
```

Update all call sites of the old functions to use the unified one.

---

## What stays as-is

- `_tag_cf_positions`, `_tag_is_positions`, `_tag_is_semantic`, `_tag_da_sbc_nodes` — unchanged
- `_override_bs_cash()` — unchanged (it writes values, not matching)
- `merge_trees.py` — unchanged (`_find_by_concept` is not a duplicate)
- No new files created

---

## Deferred (future PR)

- Module extraction into `concept_matcher.py` — deferred until a third file needs matching logic or LLM fallback is being built
- Named chain definitions (BS_CASH_CHAIN, etc.) — deferred with module extraction
- `match_chain` combinator — deferred

---

## Testing

### Unit test: BS_CASH concept-name match

Test that when children are ordered [PrepaidExpense, CashAndCashEquivalents], the concept-name match finds the cash node correctly (not position 0).

### Integration test: TSLA BS_CASH

Use the cached TSLA filing (`pipeline_output/validation/TSLA/trees_2026-01-29.json`). After `_tag_bs_positions`, verify:
- `BS_CASH` role is on `CashAndCashEquivalentsAtCarryingValue`, not `PrepaidExpenseAndOtherAssetsCurrent`

### Regression: existing pass rate

Run existing tests to confirm no regressions from the keyword function unification.
