# Implementation Guide: Reclassification Detection

## Coverage Map

| Spec Requirement | TDD Test(s) | Status |
|---|---|---|
| Parent-child promotion detection + fix | `TestParentChildPromotion` (4 tests) | Covered |
| Sibling replacement detection + fix | `TestSiblingReplacement.test_newer_sibling_absorbs_older_values` | Covered |
| `__OTHER__` nodes excluded | `TestNegativeCases.test_other_nodes_not_detected` | Covered |
| Only structurally related nodes matched | `TestNegativeCases.test_no_structural_relationship_no_match` | Covered |
| No match when same periods (no newer extension) | `TestNegativeCases.test_same_periods_no_newer_extension` | Covered |
| Two-phase detect-then-fix | Implicitly covered (mutation-during-iteration would break tests) | Covered |
| TSLA integration (revenue errors gone) | `TestTSLAReMerge.test_tsla_no_is_revenue_errors_after_merge` | BUG: wrong `verify_model` signature |
| TSLA integration (NI Link < 150) | `TestTSLAReMerge.test_tsla_ni_link_errors_under_threshold` | BUG: wrong `verify_model` signature |

---

## Step 1: Fix TSLA Integration Tests

**File:** `tests/test_reclassification.py`, lines 356-412

### Problem

The tests call `verify_model(trees, periods, cf_endc_values)` with 3 arguments, but the actual signature is:

```python
# pymodel.py line 8
def verify_model(trees: dict) -> list[tuple]:
```

`verify_model` expects a single dict with keys `"IS"`, `"BS"`, `"BS_LE"`, `"CF"`, `"complete_periods"`. It internally reconstructs `TreeNode` objects from dicts. It returns `list[tuple]` where each tuple is `(check_name, period, delta)`.

The tests also filter errors as strings (`"Revenue" in e`), but errors are tuples, so they need to check `e[0]`.

### Fix for `test_tsla_no_is_revenue_errors_after_merge` (line 356)

Replace lines 363-378 with:

```python
        tree_files = sorted(glob.glob(os.path.join(TSLA_DIR, "trees_*.json")))
        # merge_filing_trees expects newest first
        tree_files = list(reversed(tree_files))

        merged = merge_filing_trees(tree_files)

        # verify_model expects the full merged dict (has IS, BS, BS_LE, CF,
        # complete_periods, cf_endc_values — all set by merge_filing_trees)
        errors = verify_model(merged)

        # errors are (check_name, period, delta) tuples
        revenue_errors = [e for e in errors if "Revenue" in e[0] or "IS_REVENUE" in e[0]]
        assert len(revenue_errors) == 0, \
            f"Should have no IS Revenue errors after reclassification fix, got: {revenue_errors}"
```

### Fix for `test_tsla_ni_link_errors_under_threshold` (line 382)

Replace lines 388-412 with:

```python
        tree_files = sorted(glob.glob(os.path.join(TSLA_DIR, "trees_*.json")))
        tree_files = list(reversed(tree_files))

        merged = merge_filing_trees(tree_files)
        errors = verify_model(merged)

        # Find NI Link errors — tuples are (check_name, period, delta)
        ni_errors = [e for e in errors if "NI Link" in e[0] or "INC_NET" in e[0]]
        for err in ni_errors:
            delta = abs(err[2])  # third element is the delta
            if delta > 150:
                pytest.fail(
                    f"NI Link error exceeds 150 threshold: {err}"
                )
```

### What to delete

Remove these now-unused lines from both tests:
- `periods = sorted(merged.get("complete_periods", []))`
- The `trees = {}` dict construction loop
- The `import re` and regex-based number extraction

---

## Step 2: Add `_detect_and_fix_structural_shifts()` to `merge_trees.py`

**File:** `merge_trees.py`
**Insert at:** line 218, right before the `merge_filing_trees` function (line 220). This keeps all helper functions above the main function, matching the existing convention.

### Function signature

```python
def _detect_and_fix_structural_shifts(tree, periods):
    """Detect and fix structural reclassifications (parent-child promotion, sibling replacement).

    Two-phase approach:
      Phase 1 (detect): Walk tree, find pairs where:
        - Node A and B share the same value at an overlap period (abs diff < 1.0, non-zero)
        - Node B extends into newer periods that A lacks
        - Structural relationship: parent-child OR shared parent (siblings)
        - Neither is __OTHER__
      Phase 2 (fix): For each detection:
        - Copy A's older-period values into B
        - Transfer A's role to B (if B has no role)
        - Replace A with B in hierarchy (B adopts A's position and non-residual children)

    Args:
        tree: TreeNode root of a statement tree (IS, BS, CF, etc.)
        periods: list of period strings, sorted chronologically

    Returns:
        dict with keys: "detections" (list of dicts), "fixes_applied" (int)
    """
```

### Phase 1: Detection

Based on POC's `detect_parent_child_renames()` (poc_reclassification.py lines 40-77), extended to also handle siblings.

```python
    detections = []

    def _scan(node):
        if node.concept.startswith("__"):
            return
        # Pattern 1: Parent-child — check each non-OTHER child against this node
        for child in node.children:
            if child.concept.startswith("__"):
                continue
            _check_pair(node, child, "parent_child", detections)
            # Recurse into child
            _scan(child)

        # Pattern 2: Sibling — check each pair of non-OTHER children
        real_children = [c for c in node.children if not c.concept.startswith("__")]
        for i in range(len(real_children)):
            for j in range(i + 1, len(real_children)):
                a, b = real_children[i], real_children[j]
                _check_pair(a, b, "sibling", detections)

    def _check_pair(node_a, node_b, relationship, results):
        """Check if node_a is being replaced by node_b (or vice versa)."""
        shared_periods = set(node_a.values.keys()) & set(node_b.values.keys())
        for p in shared_periods:
            if node_a.values[p] == 0:
                continue
            if abs(node_a.values[p] - node_b.values[p]) >= 1.0:
                continue
            # Same value at overlap period — check who extends newer
            a_only = set(node_a.values.keys()) - set(node_b.values.keys())
            b_only = set(node_b.values.keys()) - set(node_a.values.keys())
            if b_only:
                # B extends newer — B replaces A
                results.append({
                    "old_node": node_a,
                    "new_node": node_b,
                    "relationship": relationship,
                    "overlap_period": p,
                })
                return  # one detection per pair is enough
            elif a_only:
                # A extends newer — A replaces B
                results.append({
                    "old_node": node_b,
                    "new_node": node_a,
                    "relationship": relationship,
                    "overlap_period": p,
                })
                return

    _scan(tree)
```

**IMPORTANT: The `_scan` recursion must happen inside the parent-child loop (after checking each child), NOT as a separate pass. This ensures children are scanned even when they are not part of a detection. Look at the POC line 74: `_scan(child)` is inside the child loop.**

### Phase 2: Fix

Based on POC's `apply_rename_fix()` (poc_reclassification.py lines 80-108) and `_replace_in_tree()` (lines 111-123).

```python
    # Phase 2: Apply fixes
    fixes_applied = 0
    for det in detections:
        old_node = det["old_node"]
        new_node = det["new_node"]

        # Copy old node's values into new node (for periods new node doesn't have)
        for period, value in old_node.values.items():
            if period not in new_node.values:
                new_node.values[period] = value

        # Transfer role
        if old_node.role and not new_node.role:
            new_node.role = old_node.role

        # Replace old_node with new_node in the tree
        _replace_in_tree(tree, old_node, new_node)
        fixes_applied += 1

        logger.info(
            "Reclassification fix: %s -> %s (%s, overlap=%s)",
            old_node.concept, new_node.concept,
            det["relationship"], det["overlap_period"],
        )

    return {"detections": detections, "fixes_applied": fixes_applied}
```

### Helper: `_replace_in_tree`

Add this as a module-level private function (above `_detect_and_fix_structural_shifts`), matching the POC's version at poc_reclassification.py lines 111-123.

**Insert at:** line 218 (before `_detect_and_fix_structural_shifts`)

```python
def _replace_in_tree(root, old_node, new_node):
    """Replace old_node with new_node in the tree.

    Moves old_node's non-residual children (except new_node itself) to new_node.
    """
    for i, child in enumerate(root.children):
        if child is old_node:
            # Move old_node's other children to new_node
            for oc in old_node.children:
                if oc is not new_node and not oc.concept.startswith("__"):
                    new_node.add_child(oc)
            root.children[i] = new_node
            return True
        if _replace_in_tree(child, old_node, new_node):
            return True
    return False
```

**CRITICAL: Use `child is old_node` (identity check), NOT `child == old_node` or concept comparison. Two different nodes can have the same concept. The POC uses `is` at line 113.**

**CRITICAL: The `if oc is not new_node` guard (line 117 in POC) prevents adding new_node as its own child. Do not omit this.**

---

## Step 3: Call the New Function in `merge_filing_trees()`

**File:** `merge_trees.py`
**Location:** Between line 326 (end of orphan insertion loop) and line 328 (`_recompute_residuals`)

### Current code (lines 326-328):

```python
                          f"(would {'hurt' if hurts else 'not help'})", file=sys.stderr)

        # Pass 5: Recompute __OTHER__ residuals
```

### Insert between them:

```python
        # Pass 4b: Detect and fix structural reclassifications
        stats = _detect_and_fix_structural_shifts(base_tree, all_periods)
        if stats["fixes_applied"] > 0:
            print(f"  {stmt}: {stats['fixes_applied']} reclassification fix(es) applied",
                  file=sys.stderr)

        # Pass 5: Recompute __OTHER__ residuals
```

The comment numbering changes from "Pass 5" to keeping it as "Pass 5" since we label the new pass "Pass 4b". This is consistent with the spec saying "between Pass 4 and Pass 5."

---

## Step 4: Unit Tests for Internal Detection Logic

**File:** `tests/test_reclassification.py`
**Add after** `TestNegativeCases` class (line 335), before the TSLA integration section.

These test edge cases not covered by the TDD integration tests:

### Test 1: Zero-value overlap should NOT match

```python
class TestEdgeCases:
    """Unit tests for internal detection edge cases."""

    def test_zero_value_overlap_no_match(self):
        """If the shared value at overlap period is 0, skip it — zero is not
        a meaningful signal for reclassification."""
        old = _make_leaf("us-gaap_Revenues",
                         values={"2020": 300, "2021": 400, "2022": 0})
        new = _make_leaf("us-gaap_RevenueFromContract",
                         values={"2022": 0, "2023": 600, "2024": 700})
        parent = _make_parent("Root", children=[old, new],
                              values={"2020": 300, "2021": 400, "2022": 0,
                                      "2023": 600, "2024": 700})

        periods = ["2020", "2021", "2022", "2023", "2024"]
        concepts_before = _all_concepts(parent)
        _detect_and_fix_structural_shifts(parent, periods)
        concepts_after = _all_concepts(parent)
        assert concepts_before == concepts_after
```

### Test 2: Multiple overlapping periods (should still detect)

```python
    def test_multiple_overlap_periods_still_detects(self):
        """If nodes share values at multiple periods, detection should still fire."""
        old = _make_leaf("us-gaap_Revenues",
                         values={"2020": 300, "2021": 400, "2022": 500})
        new = _make_leaf("us-gaap_RevenueFromContract",
                         values={"2021": 400, "2022": 500, "2023": 600})
        parent = _make_parent("Root", children=[old, new],
                              values={"2020": 300, "2021": 400, "2022": 500,
                                      "2023": 600})

        periods = ["2020", "2021", "2022", "2023"]
        _detect_and_fix_structural_shifts(parent, periods)

        assert _find_by_concept(parent, "us-gaap_Revenues") is None
        promoted = _find_by_concept(parent, "us-gaap_RevenueFromContract")
        assert promoted is not None
        assert promoted.values.get("2020") == 300
```

### Test 3: Child with role already set (don't overwrite)

```python
    def test_child_role_not_overwritten(self):
        """If the new node already has a role, don't overwrite it with the
        old node's role."""
        child = _make_leaf("us-gaap_RevenueFromContract",
                           values={"2022": 500, "2023": 600},
                           role="EXISTING_ROLE")
        parent = _make_parent("us-gaap_Revenues",
                              children=[child],
                              values={"2020": 300, "2022": 500},
                              role="IS_REVENUE")
        root = _make_parent("Root", children=[parent],
                            values={"2020": 300, "2022": 500, "2023": 600})

        periods = ["2020", "2022", "2023"]
        _detect_and_fix_structural_shifts(root, periods)

        promoted = _find_by_concept(root, "us-gaap_RevenueFromContract")
        assert promoted is not None
        assert promoted.role == "EXISTING_ROLE", \
            "Should not overwrite existing role"
```

### Test 4: Stats dict is returned correctly

```python
    def test_returns_stats_dict(self):
        """Function should return a stats dict with detections and fixes_applied."""
        child = _make_leaf("us-gaap_RevenueFromContract",
                           values={"2022": 500, "2023": 600})
        parent = _make_parent("us-gaap_Revenues",
                              children=[child],
                              values={"2020": 300, "2022": 500})
        root = _make_parent("Root", children=[parent],
                            values={"2020": 300, "2022": 500, "2023": 600})

        periods = ["2020", "2022", "2023"]
        stats = _detect_and_fix_structural_shifts(root, periods)

        assert isinstance(stats, dict)
        assert "detections" in stats
        assert "fixes_applied" in stats
        assert stats["fixes_applied"] == 1
```

---

## Common Mistakes to Avoid

### 1. Mutating children list during iteration

**Wrong:**
```python
for child in node.children:
    if should_remove(child):
        node.children.remove(child)  # modifies list during iteration
```

**Right:** The two-phase approach (detect all, then fix all) avoids this. The `_replace_in_tree` function modifies `root.children[i]` by index, which is safe since it returns immediately after the replacement.

### 2. Forgetting the `is not new_node` guard in `_replace_in_tree`

Without this check, when a child replaces its parent, the child gets added as its own child, creating an infinite loop.

### 3. Checking `==` instead of `is` for node identity

TreeNodes don't implement `__eq__`, so `==` falls back to `is` in Python. But relying on this is fragile. Always use `is` for node identity comparisons.

### 4. Not handling the case where both nodes extend

If A has periods B doesn't have AND B has periods A doesn't have, this is NOT a reclassification -- it's two genuinely different concepts. The `_check_pair` function handles this by only matching when one side has exclusive newer periods and the other doesn't.

Wait -- actually re-read the `_check_pair` logic: if `b_only` is non-empty, it fires regardless of whether `a_only` is also non-empty. This is correct for the reclassification case: A has old periods (2020, 2021, 2022), B has (2022, 2023, 2024). Both have exclusive periods. B extends newer. B replaces A. The key signal is "B extends into newer periods that A lacks" -- the fact that A also has older periods B lacks is expected (those get copied over).

### 5. Not logging/printing the fix

The existing codebase prints merge actions to stderr. Follow the same convention with `print(f"  {stmt}: ...", file=sys.stderr)` in the `merge_filing_trees` caller, and use `logger.info()` inside the function itself.

---

## File-by-File Summary of Changes

| File | Change | Lines Affected |
|---|---|---|
| `merge_trees.py` | Add `_replace_in_tree()` helper | Insert before line 220 |
| `merge_trees.py` | Add `_detect_and_fix_structural_shifts()` | Insert before line 220 (after `_replace_in_tree`) |
| `merge_trees.py` | Call new function between Pass 4 and Pass 5 | Insert at line 327 |
| `tests/test_reclassification.py` | Fix `test_tsla_no_is_revenue_errors_after_merge` | Lines 363-378 |
| `tests/test_reclassification.py` | Fix `test_tsla_ni_link_errors_under_threshold` | Lines 388-412 |
| `tests/test_reclassification.py` | Add `TestEdgeCases` class (4 unit tests) | Insert after line 335 |

## Execution Order

1. Fix TSLA tests first (Step 1) -- they are broken regardless of implementation
2. Add `_replace_in_tree` and `_detect_and_fix_structural_shifts` to `merge_trees.py` (Step 2)
3. Wire it into `merge_filing_trees` (Step 3)
4. Add edge-case unit tests (Step 4)
5. Run `python -m pytest tests/test_reclassification.py -v` -- all 8 TDD tests + 4 unit tests should pass
6. Run `python -m pytest` -- full suite should pass (no regressions)
