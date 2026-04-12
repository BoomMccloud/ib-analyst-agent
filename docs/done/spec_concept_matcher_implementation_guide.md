# BS_CASH Fix + Matching Cleanup -- Implementation Guide

## Overview

Three changes:
1. **Fix BS_CASH tagging** -- match by concept name (`CashAndCashEquivalent` prefix) instead of `children[0]`; fall back to position if no match
2. **Delete dead code** -- remove `_find_is_value_by_label` and `_find_cf_match_by_value` from `pymodel.py`; update test comments
3. **Unify keyword search** -- replace `_find_leaf_by_keywords` and `_find_by_keywords_bfs` with a single `_find_by_keywords` function

## Success Criteria

All 9 tests in `tests/test_bs_cash_fix.py` pass. All existing tests (`test_da_sbc_tagging.py`, `test_dual_linkbase.py`, `test_merge_pipeline.py`) continue to pass. Build check passes.

```bash
python -m pytest tests/test_bs_cash_fix.py -v       # 9 pass
python -m pytest tests/ -v                           # all pass
python -m py_compile xbrl_tree.py pymodel.py merge_trees.py
```

## Files to Modify

| File | What changes |
|------|-------------|
| `xbrl_tree.py` | Fix `_tag_bs_positions` (line 626), add `_find_by_keywords`, delete old functions, update callers |
| `pymodel.py` | Delete `_find_is_value_by_label` (lines 161-181) and `_find_cf_match_by_value` (lines 184-209) |
| `tests/test_merge_pipeline.py` | Update comment references to deleted functions |

---

## Step-by-step Implementation

### Step 1: Fix BS_CASH in `_tag_bs_positions` (xbrl_tree.py)

**File**: `xbrl_tree.py`
**Location**: Lines 624-626 inside `_tag_bs_positions`

**Current code** (lines 620-628):
```python
        found_tca = False
        for child in assets_tree.children:
            if child.children and not child.is_leaf:
                child.role = "BS_TCA"
                # Tag first flex item as BS_CASH (for cash link override later)
                if child.children:
                    child.children[0].role = "BS_CASH"
                found_tca = True
                break
```

**Replace lines 624-626** with concept-name matching + fallback:
```python
                # Tag BS_CASH: prefer child whose concept matches CashAndCashEquivalent*
                cash_node = None
                for grandchild in child.children:
                    # Strip namespace prefix (colon or underscore separator)
                    bare = grandchild.concept
                    if ':' in bare:
                        bare = bare.split(':', 1)[1]
                    elif '_' in bare:
                        bare = bare.split('_', 1)[1]
                    if bare.lower().startswith("cashandcashequivalent"):
                        cash_node = grandchild
                        break
                # Fallback: first child (preserves old behavior when no cash concept)
                if cash_node is None and child.children:
                    cash_node = child.children[0]
                if cash_node:
                    cash_node.role = "BS_CASH"
```

**Tests this makes pass**: `test_cash_tagged_by_concept_not_position`, `test_namespace_prefix_stripped_for_match`, `test_fallback_to_first_child_when_no_cash_concept` (stays passing).

---

### Step 2: Add unified `_find_by_keywords` (xbrl_tree.py)

**File**: `xbrl_tree.py`
**Location**: Insert AFTER `_find_by_keywords_bfs` (after line 711), BEFORE `_tag_is_semantic` (line 713)

**Add this function**:
```python
def _find_by_keywords(tree: 'TreeNode', keywords: list[str],
                      mode: str = "all", search: str = "dfs",
                      leaf_only: bool = True, field: str = "name") -> 'TreeNode | None':
    """Unified keyword search over a TreeNode tree.

    Args:
        tree: Root node to search.
        keywords: Keywords to match (case-insensitive).
        mode: "all" = node must contain ALL keywords; "any" = at least one.
        search: "dfs" = depth-first; "bfs" = breadth-first (shallowest match).
        leaf_only: If True, skip non-leaf nodes.
        field: "name" = search node.name; "concept" = search node.concept.
    """
    match_fn = all if mode == "all" else any

    def _matches(node):
        if leaf_only and not node.is_leaf:
            return False
        text = getattr(node, field, "").lower()
        return match_fn(kw in text for kw in keywords)

    if search == "bfs":
        from collections import deque
        queue = deque([tree])
        while queue:
            node = queue.popleft()
            if _matches(node):
                return node
            for child in node.children:
                queue.append(child)
        return None
    else:  # dfs
        if _matches(tree):
            return tree
        for child in tree.children:
            result = _find_by_keywords(child, keywords, mode=mode, search=search,
                                       leaf_only=leaf_only, field=field)
            if result:
                return result
        return None
```

**Tests this makes pass**: All 6 tests in `TestUnifiedKeywordSearchDFSAll` and `TestUnifiedKeywordSearchBFSAny`.

---

### Step 3: Update callers to use `_find_by_keywords` (xbrl_tree.py)

**3a. Update `_tag_da_sbc_semantic` callers** (around lines 967-985)

**Current** (line 967):
```python
    is_da = _find_leaf_by_keywords(is_tree, ["depreciation"])
```
**Replace with**:
```python
    is_da = _find_by_keywords(is_tree, ["depreciation"], mode="all", search="dfs", leaf_only=True, field="name")
```

**Current** (line 969):
```python
        is_da = _find_leaf_by_keywords(is_tree, ["amortization"])
```
**Replace with**:
```python
        is_da = _find_by_keywords(is_tree, ["amortization"], mode="all", search="dfs", leaf_only=True, field="name")
```

**Current** (line 983):
```python
    is_sbc = _find_leaf_by_keywords(is_tree, ["stock", "compensation"])
```
**Replace with**:
```python
    is_sbc = _find_by_keywords(is_tree, ["stock", "compensation"], mode="all", search="dfs", leaf_only=True, field="name")
```

**Current** (line 985):
```python
        is_sbc = _find_leaf_by_keywords(is_tree, ["share", "compensation"])
```
**Replace with**:
```python
        is_sbc = _find_by_keywords(is_tree, ["share", "compensation"], mode="all", search="dfs", leaf_only=True, field="name")
```

**3b. Update `_tag_is_semantic` caller** (line 727)

**Current** (line 727):
```python
    cogs_node = _find_by_keywords_bfs(is_tree, cogs_keywords)
```
**Replace with**:
```python
    cogs_node = _find_by_keywords(is_tree, cogs_keywords, mode="any", search="bfs", leaf_only=False, field="concept")
```

---

### Step 4: Delete old keyword search functions (xbrl_tree.py)

**File**: `xbrl_tree.py`

**Delete** `_find_leaf_by_keywords` (lines 684-697) -- the entire function.

**Delete** `_find_by_keywords_bfs` (lines 700-711) -- the entire function.

After this step, only `_find_by_keywords` remains as the unified search function.

---

### Step 5: Delete dead code from pymodel.py

**File**: `pymodel.py`

**Delete** `_find_is_value_by_label` (lines 161-181) -- the entire function.

**Delete** `_find_cf_match_by_value` (lines 184-209) -- the entire function.

These functions are never called. The D&A/SBC verification in `verify_model` uses `find_node_by_role()` instead.

---

### Step 6: Update test comments in test_merge_pipeline.py

**File**: `tests/test_merge_pipeline.py`

Update references to deleted functions. These are in assertion error messages and comments:

**Line 111**: Change `_find_cf_match_by_value() heuristic.` to `value-matching heuristic (removed).`

**Line 124**: Change `Current code: _find_cf_match_by_value finds the decoy (value=100),` to `Old code: value-matching heuristic found the decoy (value=100),`

**Line 170**: Change `"It matched the decoy node (value=100) via _find_cf_match_by_value "` to `"It matched the decoy node (value=100) via value-matching heuristic (removed) "`

**Line 225**: Change `"It matched the decoy node (value=50) via _find_cf_match_by_value "` to `"It matched the decoy node (value=50) via value-matching heuristic (removed) "`

---

## Unit Test Specifications

The integration tests in `test_bs_cash_fix.py` already cover the public behavior. These additional unit-level checks verify internal logic:

### UT-1: `_find_by_keywords` with empty tree
```python
def test_find_by_keywords_empty_tree():
    """A single leaf node with no matching keywords returns None."""
    leaf = TreeNode("us-gaap_Revenue")
    leaf.values = {"2024": 100}
    result = _find_by_keywords(leaf, ["cash"], mode="all", search="dfs",
                               leaf_only=True, field="name")
    assert result is None
```

### UT-2: `_find_by_keywords` field="concept" vs field="name"
```python
def test_find_by_keywords_field_concept_vs_name():
    """field='concept' searches raw concept string; field='name' searches cleaned name."""
    leaf = TreeNode("us-gaap_CostOfGoodsSold")
    leaf.values = {"2024": 100}
    root = TreeNode("root")
    root.add_child(leaf)

    # "us-gaap" is in concept but not in name
    by_concept = _find_by_keywords(root, ["us-gaap"], mode="any", search="dfs",
                                    leaf_only=True, field="concept")
    assert by_concept is not None

    by_name = _find_by_keywords(root, ["us-gaap"], mode="any", search="dfs",
                                 leaf_only=True, field="name")
    assert by_name is None
```

### UT-3: BS_CASH concept matching with underscore separator
```python
def test_bs_cash_underscore_separator():
    """Concept with underscore namespace prefix (us-gaap_CashAnd...) is matched."""
    # Already covered by test_cash_tagged_by_concept_not_position,
    # but this isolates the stripping logic.
    cash = TreeNode("us-gaap_CashAndCashEquivalents")
    bare = cash.concept.split('_', 1)[1] if '_' in cash.concept else cash.concept
    assert bare.lower().startswith("cashandcashequivalent")
```

These unit tests can be added to `test_bs_cash_fix.py` or a separate file at the implementer's discretion.

---

## Edit Order

To avoid line-number drift, apply edits bottom-to-top within each file:

1. **xbrl_tree.py**: Step 3a (lines 983-985), then Step 3a (lines 967-969), then Step 3b (line 727), then Step 4 (delete lines 700-711, then 684-697), then Step 2 (insert after old line 711), then Step 1 (lines 624-626)
2. **pymodel.py**: Step 5 (delete lines 184-209, then 161-181)
3. **tests/test_merge_pipeline.py**: Step 6 (lines 225, 170, 124, 111 -- bottom to top)

---

## Final Verification Commands

```bash
# 1. All new tests pass
python -m pytest tests/test_bs_cash_fix.py -v

# 2. All existing tests still pass (regression)
python -m pytest tests/ -v

# 3. Build check
python -m py_compile xbrl_tree.py pymodel.py merge_trees.py

# 4. Confirm dead code is gone
grep -n "_find_is_value_by_label\|_find_cf_match_by_value" pymodel.py
# Should return nothing

# 5. Confirm old functions are gone
grep -n "def _find_leaf_by_keywords\|def _find_by_keywords_bfs" xbrl_tree.py
# Should return nothing

# 6. Confirm no remaining callers of old functions
grep -rn "_find_leaf_by_keywords\|_find_by_keywords_bfs" xbrl_tree.py pymodel.py
# Should return nothing
```
