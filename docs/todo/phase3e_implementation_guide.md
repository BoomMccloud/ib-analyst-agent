# Phase 3e Implementation Guide: Dual-Linkbase Architecture

## Coverage Map: Spec Requirements to Tests

| # | Spec Requirement | Test Class | Tests | Status |
|---|-----------------|------------|-------|--------|
| 1 | `parse_pre_linkbase()` + `build_presentation_index()` | TestPresentationLinkbase | 3 tests | ALL PASS |
| 2 | `sort_by_presentation()` | TestPresentationLinkbase | 3 tests | ALL PASS |
| 3 | `cascade_layout()` in xbrl_tree.py | TestCascadeLayout | 3 tests (2 fail) | ImportError |
| 4 | Fix `_tag_is_positions()` value overwrite | TestTagIsPositionsNoValueOverwrite | 3 tests (1 fails) | EBT regression |
| 5 | `_tag_is_semantic()` + `_find_by_keywords_bfs()` | TestSemanticBFSTagging | 3 tests (all fail) | ImportError |
| 6 | `_supplement_orphan_facts()` | TestOrphanFactSupplementation | 5 tests (all fail) | ImportError |
| 7 | `verify_tree_completeness()` | TestTreeCompletenessVerification | 5 tests (all fail) | ImportError |
| 8 | Three-pass rendering (write_sheets refactor) | N/A (no direct test) | — | Not tested directly |
| 9 | `CROSS_STATEMENT_CHECKS` + `_render_cross_checks()` | TestCrossStatementChecks | 3 tests (all fail) | ImportError |
| 10 | Pipeline gate | TestPipelineGate | 3 tests (2 fail) | ImportError (verify_tree_completeness) |

**Coverage gaps**: Spec requirement #8 (three-pass rendering) has no direct test. This is acceptable as it's a refactor of existing behavior tested via the existing pipeline.

**Total**: 28 tests, 7 pass, 21 fail.

---

## Files You Will Modify

| File | Lines (current) | Changes |
|------|----------------|---------|
| `xbrl_tree.py` | 1027 lines | Add 5 new functions, fix 1 bug |
| `sheet_builder.py` | 464 lines | Add 1 new function |
| `run_pipeline.py` | 112 lines | No changes needed (tests don't test run_pipeline directly) |
| `pymodel.py` | 169 lines | No changes needed (verify_model already exists) |

---

## Step-by-Step Implementation

### Step 1: Add `cascade_layout()` to xbrl_tree.py

**Fixes tests**: `TestCascadeLayout::test_cascade_puts_revenue_first_ni_last`, `TestCascadeLayout::test_cascade_subtotals_after_children`

**What tests expect**:
```python
from xbrl_tree import cascade_layout
rows = cascade_layout(tree)
# rows = list of TreeNode references in post-order (leaves first, root last)
# For IS: Revenue is first, NetIncome is last
concepts = [r.concept for r in rows]
```

The function takes a single `TreeNode` and returns a **flat list of TreeNode references** in post-order traversal (children before parent). This is different from `_cascade_layout` in sheet_builder.py which returns `[(row_num, indent, node)]` tuples.

**File**: `xbrl_tree.py`
**Location**: After `sort_by_presentation()` (line 131), before `parse_calc_linkbase()` (line 134)

**Add this code** after line 132 (`sort_by_presentation(child, pres_index)`):

```python

def cascade_layout(tree: TreeNode) -> list[TreeNode]:
    """Return nodes in post-order: children before parent (IS cascade layout).

    For income statements, this puts Revenue first and Net Income last,
    matching how analysts read IS top-to-bottom.
    """
    result = []

    def _postorder(node):
        for child in node.children:
            _postorder(child)
        result.append(node)

    _postorder(tree)
    return result
```

**Verify**: `.venv/bin/python -m pytest tests/test_dual_linkbase.py::TestCascadeLayout -v`

---

### Step 2: Fix `_tag_is_positions()` value overwrite bug in xbrl_tree.py

**Fixes test**: `TestTagIsPositionsNoValueOverwrite::test_nflx_ebt_regression_values_not_corrupted`

**Root cause**: Lines 787, 801, and 806 in `_tag_is_positions()` overwrite `.values` with `dict(cf_ni_values)`. When the root IS node IS Net Income (NFLX pattern), or when a child matches, the `.values` dict gets replaced — which corrupts the EBT parent's values if it was scanned during the search.

Actually the specific bug: when a depth-1 child (EBT) is scanned and does NOT match, it's fine. But when NI (root) matches, line 787 does `best_match.values = dict(cf_ni_values)`. The problem is the `best_match` search scans `is_tree.children` (depth-1). In the NFLX EBT regression test, the root has 2 children: EBT and IncomeTaxExpense. EBT does NOT match CF NI (12.7M vs 10.9M), IncomeTaxExpense does NOT match either. So `best_match` is None.

Then the fallback at line 790-801 takes the first positive-weight child (EBT!) and sets `fallback.values = dict(cf_ni_values)` on line 801. This overwrites EBT's values from 12,722,552 to 10,981,201.

Wait, let me re-check. The test builds NI as root with children [EBT, Tax]. NI values = {2024: 10_981_201}. CF NI values = {2024: 10_981_201} (same). So cf_ni_values = {2024: 10_981_201}.

The search over `is_tree.children` finds EBT (values {2024: 12_722_552}) — does NOT match CF NI. Tax (values {2024: 1_741_351}) — does NOT match. So best_match = None.

But wait - the test passes `ni` as `is_tree`. So `is_tree = ni` with values {2024: 10_981_201}. The code searches `is_tree.children` for a match. None match. Fallback: first positive-weight child = EBT. Line 801: `fallback.values = dict(cf_ni_values)` overwrites EBT from 12,722,552 to 10,981,201.

But actually the root itself HAS the matching values. The code should check the root too. But actually the test also expects is_tree NOT to get its values overwritten (tests 1 and 2 check that). So the fix is: **never overwrite .values, just assign the role**.

**File**: `xbrl_tree.py`
**Find** (line 787):
```python
            best_match.role = "INC_NET"
            best_match.values = dict(cf_ni_values)
```
**Replace with**:
```python
            best_match.role = "INC_NET"
```

**Find** (line 800-801):
```python
                fallback.role = "INC_NET"
                fallback.values = dict(cf_ni_values)
```
**Replace with**:
```python
                fallback.role = "INC_NET"
```

**Find** (line 805-806):
```python
                is_tree.role = "INC_NET"
                is_tree.values = dict(cf_ni_values)
```
**Replace with**:
```python
                is_tree.role = "INC_NET"
```

BUT WAIT: We also need to check the root itself as a potential match before falling back to depth-1 children. In the NFLX pattern, the root IS NetIncomeLoss with the same values as CF NI. The current code only searches `is_tree.children`, missing the root.

**Additional fix**: Before the depth-1 child loop, check if the root itself matches:

**Find** (line 767-783, the depth-1 child search):
```python
    if cf_ni_values:
        # Strategy 1: Find the IS depth-1 child whose values match CF's NI
        best_match = None
        for child in is_tree.children:
            if not child.values:
                continue
            # Count how many periods match CF's NI within tolerance
            matches = 0
            total = 0
            for p, cf_val in cf_ni_values.items():
                is_val = child.values.get(p)
                if is_val is not None:
                    total += 1
                    if abs(is_val - cf_val) < 0.5:
                        matches += 1
            if total > 0 and matches == total:
                best_match = child
                break
```

**Replace with**:
```python
    if cf_ni_values:
        # Strategy 0: Check if the IS root itself matches CF's NI
        best_match = None
        root_matches = 0
        root_total = 0
        for p, cf_val in cf_ni_values.items():
            is_val = is_tree.values.get(p)
            if is_val is not None:
                root_total += 1
                if abs(is_val - cf_val) < 0.5:
                    root_matches += 1
        if root_total > 0 and root_matches == root_total:
            best_match = is_tree

        # Strategy 1: Find the IS depth-1 child whose values match CF's NI
        if not best_match:
            for child in is_tree.children:
                if not child.values:
                    continue
                matches = 0
                total = 0
                for p, cf_val in cf_ni_values.items():
                    is_val = child.values.get(p)
                    if is_val is not None:
                        total += 1
                        if abs(is_val - cf_val) < 0.5:
                            matches += 1
                if total > 0 and matches == total:
                    best_match = child
                    break
```

**Verify**: `.venv/bin/python -m pytest tests/test_dual_linkbase.py::TestTagIsPositionsNoValueOverwrite -v`

---

### Step 3: Add `_tag_is_semantic()` to xbrl_tree.py

**Fixes tests**: All 3 tests in `TestSemanticBFSTagging`

**What tests expect**:
```python
from xbrl_tree import _tag_is_semantic
_tag_is_semantic(ni)  # Takes IS tree root, tags nodes with roles in-place
# Sets .role = "IS_REVENUE" on shallowest node matching "revenue" keyword
# Sets .role = "IS_COGS" on shallowest node matching "cost" keyword (if exists)
# If no COGS found (bank), gracefully skips (no error)
```

**Signature**: `_tag_is_semantic(is_tree: TreeNode) -> None`

The function does BFS (breadth-first search) to find the shallowest match. It must tag `IS_REVENUE` and `IS_COGS` roles by keyword matching on concept names.

**File**: `xbrl_tree.py`
**Location**: After `_find_leaf_by_keywords()` (line 560), before `_find_leaf_by_timeseries()` (line 563)

**Add this code** after line 560:

```python

def _find_by_keywords_bfs(tree: TreeNode, keywords: list[str]) -> TreeNode | None:
    """BFS search for shallowest node whose concept contains any keyword (case-insensitive).

    Returns the shallowest match, or None.
    """
    from collections import deque
    queue = deque()
    queue.append(tree)
    while queue:
        node = queue.popleft()
        concept_lower = node.concept.lower()
        if any(kw in concept_lower for kw in keywords):
            return node
        for child in node.children:
            queue.append(child)
    return None


def _tag_is_semantic(is_tree: TreeNode) -> None:
    """Tag IS nodes with semantic roles using BFS keyword matching.

    Finds IS_REVENUE and IS_COGS at shallowest depth. Gracefully skips
    roles that have no matching node (e.g., banks have no COGS).
    """
    if not is_tree:
        return

    # IS_REVENUE: match "revenue" or "sales"
    rev_node = _find_by_keywords_bfs(is_tree, ["revenue", "sales"])
    if rev_node:
        rev_node.role = "IS_REVENUE"

    # IS_COGS: match "costofgoods", "costofrevenue", "costofsales"
    cogs_node = _find_by_keywords_bfs(is_tree, ["costofgoods", "costofrevenue", "costofsales"])
    if cogs_node:
        cogs_node.role = "IS_COGS"
```

**Important**: The BFS keyword match uses `concept.lower()` (e.g., `us-gaap_revenuefromcontractwithcustomer...`) NOT `node.name`. The test concepts contain "Revenue" in the concept string, so `"revenue" in "us-gaap_revenue"` will match.

**Verify**: `.venv/bin/python -m pytest tests/test_dual_linkbase.py::TestSemanticBFSTagging -v`

---

### Step 4: Add `_supplement_orphan_facts()` to xbrl_tree.py

**Fixes tests**: All 5 tests in `TestOrphanFactSupplementation`

**What tests expect**:
```python
from xbrl_tree import _supplement_orphan_facts
_supplement_orphan_facts(parent, orphan_facts, used_tags)
# parent: TreeNode (root of subtree to supplement)
# orphan_facts: dict[str, dict[str, float]] — {concept: {period: value}}
# used_tags: set[str] — concepts already in the tree (skip these)
# Mutates parent in-place: adds new leaf children where orphan exactly closes a gap
# Bottom-up processing: fixes children before parents
# Never mutates .values on existing nodes
```

**Key behaviors**:
1. For each node with children, compute gap = parent_value - sum(child_value * child_weight)
2. If an orphan's value exactly matches the gap (within tolerance), add it as a child
3. Skip orphans in `used_tags`
4. Process bottom-up (children first, then parents)
5. Never modify existing node values

**File**: `xbrl_tree.py`
**Location**: After `_tag_is_semantic()` (from Step 3), before `_find_leaf_by_timeseries()`

```python

def _supplement_orphan_facts(parent: TreeNode, orphan_facts: dict[str, dict[str, float]],
                              used_tags: set[str]) -> None:
    """Fill tree gaps with unused XBRL facts that exactly close parent-children gaps.

    Processes bottom-up: children are supplemented before parents.
    Never mutates .values on existing nodes.
    """
    # Bottom-up: recurse into children first
    for child in list(parent.children):
        _supplement_orphan_facts(child, orphan_facts, used_tags)

    if not parent.children or not parent.values:
        return

    # Compute gap for each period
    # gap = parent_value - sum(child_value * child_weight)
    for concept, values in list(orphan_facts.items()):
        if concept in used_tags:
            continue

        # Check if this orphan closes the gap for ALL periods where both have data
        closes_gap = True
        periods_checked = 0
        for period in parent.values:
            parent_val = parent.values.get(period, 0)
            children_sum = sum(
                c.values.get(period, 0) * c.weight for c in parent.children
            )
            gap = parent_val - children_sum
            orphan_val = values.get(period, 0)

            if orphan_val == 0 and gap == 0:
                continue
            periods_checked += 1
            if abs(gap - orphan_val) > 0.5:
                closes_gap = False
                break

        if closes_gap and periods_checked > 0:
            # Add orphan as new leaf child
            new_node = TreeNode(concept, weight=1.0)
            new_node.values = dict(values)
            parent.add_child(new_node)
            used_tags.add(concept)
```

**Verify**: `.venv/bin/python -m pytest tests/test_dual_linkbase.py::TestOrphanFactSupplementation -v`

---

### Step 5: Add `verify_tree_completeness()` to xbrl_tree.py

**Fixes tests**: All 5 tests in `TestTreeCompletenessVerification`, plus 2 in `TestPipelineGate`

**What tests expect**:
```python
from xbrl_tree import verify_tree_completeness
errors = verify_tree_completeness(tree, ["2024"])
# tree: TreeNode
# periods: list[str]
# Returns: list — empty if balanced, non-empty with gap info if imbalanced
# Leaf nodes: skipped (no children to verify)
# Tolerance: rounding within ~1.0 is tolerated
# Negative-weight children are subtracted
```

**File**: `xbrl_tree.py`
**Location**: After `_supplement_orphan_facts()` (from Step 4)

```python

def verify_tree_completeness(tree: TreeNode, periods: list[str]) -> list:
    """Check that SUM(children * weight) == declared value for all branch nodes.

    Args:
        tree: Root TreeNode to verify
        periods: List of period strings to check

    Returns:
        List of (concept, period, gap) tuples. Empty = all balanced.
    """
    errors = []

    def _check(node):
        if not node.children:
            return  # Leaf nodes: nothing to verify

        for period in periods:
            declared = node.values.get(period, 0)
            if declared == 0:
                continue
            computed = sum(
                c.values.get(period, 0) * c.weight for c in node.children
            )
            gap = declared - computed
            if abs(gap) > 1.0:
                errors.append((node.concept, period, gap))

        for child in node.children:
            _check(child)

    _check(tree)
    return errors
```

**Verify**: `.venv/bin/python -m pytest tests/test_dual_linkbase.py::TestTreeCompletenessVerification tests/test_dual_linkbase.py::TestPipelineGate -v`

---

### Step 6: Add `CROSS_STATEMENT_CHECKS` to xbrl_tree.py and `_render_cross_checks()` to sheet_builder.py

**Fixes tests**: All 3 tests in `TestCrossStatementChecks`

**What tests expect**:

```python
from xbrl_tree import CROSS_STATEMENT_CHECKS
# Must be a list/tuple/dict of check definitions
# Each check has a "roles" key (list of role strings)
# No check should contain "_COMPUTED_" in any string value

from sheet_builder import _render_cross_checks
result = _render_cross_checks(CROSS_STATEMENT_CHECKS, role_map, PERIODS)
# role_map: dict[str, tuple[str, int]] — {role: (sheet_name, row_num)}
# PERIODS: list[str]
# Returns: list (may be empty if roles are missing)
# Must not raise when referenced roles are missing from role_map
```

**File**: `xbrl_tree.py`
**Location**: After `verify_tree_completeness()` (from Step 5), or near the top of the file after imports

**Add the constant**:

```python

CROSS_STATEMENT_CHECKS = [
    {
        "name": "NI Link (IS = CF)",
        "roles": ["INC_NET", "INC_NET_CF"],
        "formula": "={left}-{right}",
    },
    {
        "name": "BS Balance (TA = TL + TE)",
        "roles": ["BS_TA", "BS_TL", "BS_TE"],
        "formula": "={BS_TA}-{BS_TL}-{BS_TE}",
    },
    {
        "name": "Cash Link (CF_ENDC = BS_CASH)",
        "roles": ["CF_ENDC", "BS_CASH"],
        "formula": "={left}-{right}",
    },
]
```

**File**: `sheet_builder.py`
**Location**: After `_build_weight_formula()` (line 43), before `prev_period()` (line 45)

**Add the function**:

```python

def _render_cross_checks(checks, role_map: dict, periods: list[str]) -> list:
    """Render cross-statement check rows as spreadsheet row data.

    Args:
        checks: list of check dicts with "name", "roles", "formula" keys
        role_map: {role: (sheet_name, row_num)}
        periods: list of period strings

    Returns:
        List of row lists. Empty if all checks are skipped (missing roles).
    """
    rows = []
    for check in (checks if isinstance(checks, (list, tuple)) else list(checks.values())):
        roles = check.get("roles", [])
        # Skip check if any required role is missing
        if not all(r in role_map for r in roles):
            continue
        row = ["", "", check.get("name", "Check"), ""]
        for i in range(len(periods)):
            col = dcol(i)
            # Build cell references for each role
            refs = {}
            for role in roles:
                sheet_name, row_num = role_map[role]
                refs[role] = f"'{sheet_name}'!{col}{row_num}"
            # Simple two-role difference formula
            if len(roles) == 2:
                refs["left"] = refs[roles[0]]
                refs["right"] = refs[roles[1]]
            try:
                formula = check["formula"].format(**refs)
            except KeyError:
                formula = ""
            row.append(formula)
        rows.append(row)
    return rows
```

**Required import in sheet_builder.py**: `dcol` is already defined in the same file.

**Verify**: `.venv/bin/python -m pytest tests/test_dual_linkbase.py::TestCrossStatementChecks -v`

---

## Implementation Order Summary

Execute in this order to minimize line-number disruption:

1. **Step 2** — Fix `_tag_is_positions()` bug (modify existing code, lines 767-806)
2. **Step 1** — Add `cascade_layout()` after line 132
3. **Step 3** — Add `_find_by_keywords_bfs()` + `_tag_is_semantic()` after line 560
4. **Step 4** — Add `_supplement_orphan_facts()` after Step 3's new code
5. **Step 5** — Add `verify_tree_completeness()` after Step 4's new code
6. **Step 6a** — Add `CROSS_STATEMENT_CHECKS` after Step 5's new code
7. **Step 6b** — Add `_render_cross_checks()` to sheet_builder.py

Steps 1, 3, 4, 5, 6a are all in xbrl_tree.py. Steps 3-6a are all additions at the same general location (after `_find_leaf_by_keywords` around line 560). Step 2 is a modification of existing code at lines 767-806. Step 1 adds code at line 132.

---

## Common Mistakes to Avoid

1. **`cascade_layout` returns TreeNode list, not tuples** — The existing `_cascade_layout` in sheet_builder.py returns `(row_num, indent, node)` tuples. The test expects a plain `list[TreeNode]`.

2. **`_tag_is_semantic` BFS must use concept, not name** — The test concepts are like `us-gaap_RevenueFromContract...`. The keyword "revenue" must match against `node.concept.lower()`, not `node.name`.

3. **`_tag_is_positions` fix: remove ALL `.values = dict(cf_ni_values)` lines** — There are 3 occurrences (lines 787, 801, 806). All must be changed.

4. **`_supplement_orphan_facts` bottom-up** — Must recurse into children BEFORE checking the current node's gap.

5. **`verify_tree_completeness` tolerance** — Use `abs(gap) > 1.0` (not 0.5). The test has `values={"2024": 1000.4}` which is a gap of 0.4 and expects no error.

6. **`CROSS_STATEMENT_CHECKS` must not contain `_COMPUTED_`** — Test explicitly checks `"_COMPUTED_" not in str(check)`.

7. **`_render_cross_checks` must return `list`** — Even when all checks are skipped, return `[]` not `None`.

---

## Unit Test Specifications

The implementer should write the following unit tests in a new file or alongside the integration tests:

### For `cascade_layout()`:
- `test_single_node_returns_single_item` — leaf node returns `[leaf]`
- `test_two_level_tree_postorder` — parent with 2 leaves returns `[leaf1, leaf2, parent]`

### For `_find_by_keywords_bfs()`:
- `test_returns_none_on_no_match` — tree with no matching concepts returns None
- `test_prefers_shallow_over_deep` — verifies BFS order (same as integration test but isolated)

### For `_supplement_orphan_facts()`:
- `test_empty_orphan_dict_no_change` — passing `{}` orphans leaves tree unchanged
- `test_negative_weight_gap_calculation` — gap computed correctly with weight=-1 children

### For `verify_tree_completeness()`:
- `test_empty_periods_list_returns_empty` — no periods to check = no errors
- `test_multiple_periods_checked` — errors reported per-period

---

## Final Verification

After all steps are implemented, run:

```bash
.venv/bin/python -m pytest tests/test_dual_linkbase.py -v
```

Expected: **28 tests, 28 passed**.

Then verify no regressions in the broader test suite:

```bash
.venv/bin/python -m pytest tests/ -v
```

And verify all files compile:

```bash
python3 -m py_compile xbrl_tree.py sheet_builder.py pymodel.py run_pipeline.py
```
