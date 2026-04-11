# Phase 3c Spec: Tree Integrity, Orphan Facts & Sheet Architecture

## Summary

Phase 3 made the sheet live — parent cells became formulas, check rows verified subtotals. But the formulas are wrong for two reasons: (1) the XBRL calc linkbase is incomplete (missing children), and (2) the sheet architecture has circular dependencies and tautological checks.

**Phase 3c fixes the data layer (tree integrity) and the rendering layer (sheet architecture) together.** These must ship together because fixing the tree without fixing the renderer still produces broken sheets, and vice versa.

**The Google Sheet is the product** — it's what analysts use, share, and discuss. Python is the build system. Python verifies the model is correct, then writes it to Sheets. If the model can't be verified, the sheet doesn't get written.

---

## Problems

### 1. Incomplete calc linkbase trees

The XBRL calculation linkbase (`_cal.xml`) omits children. Parent declared values are correct, but `SUM(children)` doesn't match.

**NFLX Current Liabilities (2024):**
```
Declared:       $10,755,400
SUM(children):   $9,236,108
GAP:             $1,519,292  (14.1%)
```
The missing items (`OperatingLeaseLiabilityCurrent`, etc.) exist as XBRL facts — just not linked in the calc linkbase.

### 2. `_tag_is_positions` corrupts the IS tree

When no IS child value-matches CF's Net Income, the fallback **overwrites EBT's declared values** with NI values (`fallback.values = dict(cf_ni_values)` at `xbrl_tree.py:703`). For NFLX, EBT goes from 12,722,552 to 10,981,201, making `EBT - Tax ≠ NI`.

Root cause: NFLX's IS calc linkbase roots at `NetIncomeLoss`. The root IS Net Income, but `_tag_is_positions` only searches children.

### 3. Missing IS_REVENUE / IS_COGS tags

`xbrl_tree.py` never tags Revenue or COGS in the IS tree. The Phase 3a formatting spec needs these roles for `CURRENCY_ROLES`, and the sheet checks reference them. Current depth-based tagging misses them when the IS tree has non-standard structure (banks without COGS, nested revenue trees).

### 4. Circular write-order dependencies

`bs_ta_check` references `SUMM_TA` before the Summary tab is rendered. The current fix pre-registers hardcoded row indices (`global_role_map["SUMM_TA"] = ("Summary", 4)`). This breaks when row counts change.

### 5. Tautological sheet checks

`_render_sheet_body` aliases `CF_OPCF` → `CF_COMPUTED_OPCF` (same cell). Check formulas like `=CF_COMPUTED_OPCF - CF_OPCF` compute `=E5-E5` = 0 always. These 9 per-tab checks can never fail and catch nothing.

### 6. No pre-write gate

`sheet_builder.py` writes to Sheets regardless of whether the tree is formula-consistent.

---

## Architecture

```
XBRL Filing
       │
       ├─► build_statement_trees()           Build IS/BS/CF trees from calc linkbase
       │         │
       │         ├─► _tag_bs_positions()           Tag BS nodes by position
       │         ├─► _tag_cf_positions()           Tag CF nodes + find CF_ENDC
       │         ├─► _tag_is_positions()           FIXED — check root first, never overwrite values
       │         ├─► _tag_is_semantic()            NEW — BFS tag IS_REVENUE, IS_COGS by keyword
       │         ├─► _override_bs_cash()           BS_CASH = CF_ENDC by construction
       │         ├─► _supplement_orphan_facts()    NEW — fill gaps from orphan XBRL facts
       │         ├─► _filter_to_complete_periods()
       │         └─► _tag_da_sbc_nodes()           Tag D&A/SBC for sheet formulas
       │
       ├─► verify_tree_completeness()        NEW — parent == SUM(children) for all nodes?
       │         │
       │         └─► FAIL → stop, report gaps
       │
       ├─► verify_model()                   Existing 5 cross-statement invariants
       │         │
       │         └─► FAIL → stop, report errors
       │
       └─► sheet_builder.py                 Write sheet (only if both gates pass)
                 │
                 ├─► Pass 1: Layout (dry run — populate global_role_map)
                 ├─► Pass 2: Render (formulas using complete role map)
                 └─► Pass 3: Write (single API call)
```

---

## What Changes

### 1. Fix `_tag_is_positions` — never overwrite `.values` (`xbrl_tree.py`)

Check the IS root first (it may itself be Net Income), then search children. Never overwrite `.values`.

```python
def _tag_is_positions(is_tree, cf_tree):
    if not is_tree:
        return

    cf_ni_values = None
    if cf_tree:
        cf_ni_node = find_node_by_role(cf_tree, "INC_NET_CF")
        if cf_ni_node:
            cf_ni_values = cf_ni_node.values

    if not cf_ni_values:
        print("WARNING: No CF NI values available", file=sys.stderr)
        return

    # Strategy 1: Check if IS ROOT is Net Income
    if _values_match(is_tree.values, cf_ni_values):
        is_tree.role = "INC_NET"
        return

    # Strategy 2: Search depth-1 children for value match
    for child in is_tree.children:
        if child.values and _values_match(child.values, cf_ni_values):
            child.role = "INC_NET"
            return

    # Strategy 3: Fall back to last positive-weight child
    # DO NOT overwrite values — just tag the role
    for child in reversed(is_tree.children):
        if child.weight > 0 and child.values:
            print(f"WARNING: No IS node value-matched CF NI — "
                  f"tagging last positive child: {child.name}", file=sys.stderr)
            child.role = "INC_NET"
            return


def _values_match(a: dict, b: dict, tol=0.5) -> bool:
    """Check if two value dicts match across all shared non-zero periods."""
    matches = total = 0
    for p, v in b.items():
        if v != 0:
            total += 1
            if abs(a.get(p, 0) - v) < tol:
                matches += 1
    return total > 0 and matches == total
```

**Key invariant**: after `_tag_is_positions`, every node's `.values` dict is unchanged from what `build_tree()` originally set.

### 2. Semantic BFS tagging for IS_REVENUE and IS_COGS (`xbrl_tree.py`)

New function `_tag_is_semantic()` finds Revenue and COGS by keyword search regardless of tree depth.

```python
IS_SEMANTIC_TAGS = {
    "IS_REVENUE": {
        "keywords": [["revenue"], ["sales"]],
        "prefer_root": True,  # If multiple matches, prefer the one closest to root
    },
    "IS_COGS": {
        "keywords": [["cost", "revenue"], ["cost", "goods"], ["cost", "sales"]],
        "prefer_root": True,
    },
}


def _tag_is_semantic(is_tree: TreeNode | None):
    """Tag IS_REVENUE and IS_COGS by keyword BFS, regardless of tree depth."""
    if not is_tree:
        return

    for role, config in IS_SEMANTIC_TAGS.items():
        # Skip if already tagged (by a prior step)
        if find_node_by_role(is_tree, role):
            continue

        match = _find_by_keywords_bfs(is_tree, config["keywords"])
        if match:
            match.role = role


def _find_by_keywords_bfs(tree: TreeNode, keyword_sets: list[list[str]]) -> TreeNode | None:
    """BFS for a node whose name contains ALL keywords in any keyword set.

    Returns the shallowest match (closest to root). If multiple matches at
    the same depth, returns the one with the largest absolute values.
    """
    from collections import deque
    queue = deque([(tree, 0)])
    best = None
    best_depth = float('inf')

    while queue:
        node, depth = queue.popleft()
        if depth > best_depth:
            break  # BFS guarantees all shallower nodes processed first

        name_lower = node.name.lower()
        for kw_set in keyword_sets:
            if all(kw in name_lower for kw in kw_set):
                if node.values and any(v != 0 for v in node.values.values()):
                    if depth < best_depth:
                        best = node
                        best_depth = depth
                    elif depth == best_depth:
                        # Prefer larger values (more likely to be the real subtotal)
                        best_avg = sum(abs(v) for v in best.values.values()) if best else 0
                        node_avg = sum(abs(v) for v in node.values.values())
                        if node_avg > best_avg:
                            best = node
                break

        for child in node.children:
            queue.append((child, depth + 1))

    return best
```

This replaces depth-based assumptions (`children[0]`) with structure-agnostic search. Works for:
- Standard IS (Revenue at depth 1)
- Bank IS (no COGS — `IS_COGS` simply won't be tagged, gracefully skipped)
- Nested IS (Revenue under Operating Income)

### 3. Orphan fact supplementation (`xbrl_tree.py`)

New function `_supplement_orphan_facts()` runs during `reconcile_trees()`, after tagging and BS_CASH override, before period filtering.

**Algorithm:** For every parent where `abs(declared - SUM(children * weight)) > tolerance`:

1. Collect all XBRL tags already used by any tree node.
2. Search facts dict for unused tags with values for gap periods.
3. For each candidate: does adding it (weight +1) reduce the gap without overshooting?
4. Insert qualifying candidates as new leaf children, largest first (greedy).
5. Remaining gaps caught by `verify_tree_completeness()`.

```python
def _supplement_orphan_facts(trees: dict, tolerance: float = 0.5):
    """Find XBRL facts not in the calc linkbase and insert them where they close gaps.

    Only inserts facts that:
    - Exist as tagged XBRL values in the filing
    - Are not already used by any tree node
    - Reduce the declared-vs-computed gap when added as children
    - Do not overshoot the gap
    """
    facts = trees.get("facts", {})
    if not facts:
        return

    used_tags = set()
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _collect_tags(tree, used_tags)

    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _fill_gaps(tree, facts, used_tags, stmt, tolerance)


def _collect_tags(node, used_tags):
    """Recursively collect all XBRL tags used in the tree."""
    used_tags.add(node.tag)
    for child in node.children:
        _collect_tags(child, used_tags)


def _fill_gaps(node, facts, used_tags, stmt, tolerance=0.5):
    """For a parent node, check if SUM(children) matches declared.
    If not, search orphan facts that close the gap."""
    if not node.children or not node.values:
        return

    # Recurse first (bottom-up — fix children before parents)
    for child in node.children:
        _fill_gaps(child, facts, used_tags, stmt, tolerance)

    # Compute gap per period
    periods = list(node.values.keys())
    gaps = {}
    for p in periods:
        declared = node.values.get(p, 0)
        computed = sum(c.values.get(p, 0) * c.weight for c in node.children)
        gap = declared - computed
        if abs(gap) > tolerance:
            gaps[p] = gap

    if not gaps:
        return

    # Find orphan facts that reduce the gap
    candidates = []
    for tag, tag_values in facts.items():
        if tag in used_tags:
            continue
        if not any(p in tag_values for p in gaps):
            continue

        reduces_all = True
        for p, gap_val in gaps.items():
            fact_val = tag_values.get(p, 0)
            if fact_val == 0:
                continue
            if (gap_val > 0 and fact_val < 0) or (gap_val < 0 and fact_val > 0):
                reduces_all = False
                break
            if abs(fact_val) > abs(gap_val) + tolerance:
                reduces_all = False
                break
        if reduces_all:
            candidates.append((tag, tag_values))

    # Greedy: largest first
    candidates.sort(key=lambda c: -sum(abs(c[1].get(p, 0)) for p in gaps))

    for tag, tag_values in candidates:
        remaining = {}
        for p in periods:
            declared = node.values.get(p, 0)
            computed = sum(c.values.get(p, 0) * c.weight for c in node.children)
            gap = declared - computed
            if abs(gap) > tolerance:
                remaining[p] = gap

        if not remaining:
            break

        helps = any(
            tag_values.get(p, 0) != 0 and abs(remaining[p] - tag_values.get(p, 0)) < abs(remaining[p])
            for p in remaining
        )
        if not helps:
            continue

        concept = tag.replace(':', '_')
        new_child = TreeNode(concept, weight=1.0)
        new_child.values = {p: v for p, v in tag_values.items() if p in periods}
        node.add_child(new_child)
        used_tags.add(tag)

        print(f"  SUPPLEMENT: {stmt} {node.name[:30]} += {new_child.name} "
              f"({tag})", file=sys.stderr)
```

**Design constraints:**
- **No name matching**: no heuristics like "OperatingLeaseLiabilityCurrent sounds like it belongs under LiabilitiesCurrent." Match purely by gap-reduction.
- **No fabrication**: every inserted node has values directly from XBRL tags.
- **No plugs**: no "Other" or "Remainder" nodes. Unfilled gaps block the sheet write.
- **No value mutation**: no overwriting of existing node values.

### 4. Tree completeness verification (`pymodel.py`)

New function checks every parent node. Returns errors if any parent's formula wouldn't match its declared value.

```python
def verify_tree_completeness(trees: dict, tolerance: float = 1.0) -> list[tuple]:
    """Check that every parent node's SUM(children * weight) matches declared value.

    This ensures sheet formulas will produce correct numbers.

    Returns:
        List of (stmt, node_name, period, declared, computed, gap) tuples.
        Empty list = all parents are formula-consistent.
    """
    errors = []
    periods = trees.get("complete_periods", [])

    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree and isinstance(tree, dict):
            tree = TreeNode.from_dict(tree)
            trees[stmt] = tree
        if tree:
            _check_node_completeness(tree, stmt, periods, tolerance, errors)

    return errors


def _check_node_completeness(node, stmt, periods, tolerance, errors):
    """Recursively check a node and its children."""
    if node.children and node.values:
        for p in periods:
            declared = node.values.get(p, 0)
            if declared == 0:
                continue
            computed = sum(c.values.get(p, 0) * c.weight for c in node.children)
            gap = declared - computed
            if abs(gap) > tolerance:
                errors.append((stmt, node.name, p, declared, computed, gap))

    for child in node.children:
        _check_node_completeness(child, stmt, periods, tolerance, errors)
```

### 5. Three-pass sheet rendering (`sheet_builder.py`)

Replace the current single-pass `write_sheets()` with three passes. This eliminates hardcoded pre-registration of Summary row indices and all circular dependency issues.

```python
def write_sheets(trees: dict, company: str) -> tuple[str, str]:
    periods = trees.get("complete_periods", [])
    sid, url, sheet_ids = gws_create(
        f"{company} — Financial Model", ["IS", "BS", "CF", "Summary"]
    )
    global_role_map = {}

    # ── Pass 1: Layout (dry run) ─────────────────────────────────
    # Walk all trees and summary logic. Compute row numbers only.
    # Populate global_role_map completely. No cell values rendered.

    tab_layouts = {}  # {tab_name: [(row_num, indent, node), ...]}

    # IS layout
    is_layout = _layout_tree(trees.get("IS"), periods, start_row=4,
                              global_role_map=global_role_map, sheet_name="IS")
    tab_layouts["IS"] = is_layout

    # BS layout (Assets + L&E)
    bs_layout = _layout_bs(trees, periods, global_role_map)
    tab_layouts["BS"] = bs_layout

    # CF layout (tree + cash proof rows)
    cf_layout = _layout_cf(trees, periods, global_role_map)
    tab_layouts["CF"] = cf_layout

    # Summary layout
    summ_layout = _layout_summary(periods, global_role_map)
    tab_layouts["Summary"] = summ_layout

    # At this point, global_role_map is COMPLETE.
    # Every role from every tab is registered with its final row number.

    # ── Pass 2: Render (generate cell values and formulas) ────────
    # Use the complete global_role_map to resolve all cross-tab references.

    tab_rows = {}
    for tab_name, layout in tab_layouts.items():
        tab_rows[tab_name] = _render_from_layout(
            layout, periods, global_role_map, tab_name
        )

    # ── Pass 3: Write (single API call) ──────────────────────────
    for tab_name, rows in tab_rows.items():
        gws_write(sid, f"{tab_name}!A1:{dcol(len(periods)-1)}{len(rows)}", rows)

    # Formatting + column widths in one batchUpdate
    requests = _build_all_format_requests(tab_rows, sheet_ids, periods, global_role_map)
    gws_batch_update(sid, requests)

    return sid, url
```

**`_layout_tree()`** walks the tree and returns `[(row_num, indent, node), ...]` without rendering values. It populates `global_role_map` with every node's role → (sheet, row) mapping.

**`_render_from_layout()`** takes the layout + complete role map and generates the actual row data (leaf values, parent formulas, check formulas). Because the role map is complete, any formula can reference any tab.

This completely eliminates:
- Pre-registered hardcoded indices (`global_role_map["SUMM_TA"] = ("Summary", 4)`)
- Write-order dependencies between tabs
- The need for `_cell_ref()` to return `"0"` as a fallback

### 6. Declarative invariant checks (`sheet_builder.py`)

Replace 14 hardcoded check closures with a single declarative array of the 5 real cross-statement invariants. Checks that reference missing roles are gracefully skipped.

```python
CROSS_STATEMENT_CHECKS = [
    {
        "label": "BS Balance (TA-TL-TE)",
        "formula": lambda col, ref: f"={ref('BS_TA', col)}-{ref('BS_TL', col)}-{ref('BS_TE', col)}",
        "requires": ["BS_TA", "BS_TL", "BS_TE"],
    },
    {
        "label": "Cash Link (CF_ENDC-BS_CASH)",
        "formula": lambda col, ref: f"={ref('CF_ENDC', col)}-{ref('BS_CASH', col)}",
        "requires": ["CF_ENDC", "BS_CASH"],
    },
    {
        "label": "NI Link (IS-CF)",
        "formula": lambda col, ref: f"={ref('INC_NET', col)}-{ref('INC_NET_CF', col)}",
        "requires": ["INC_NET", "INC_NET_CF"],
    },
    {
        "label": "D&A Link (IS-CF)",
        "formula": lambda col, ref: f"={ref('IS_DA', col)}-{ref('CF_DA', col)}",
        "requires": ["IS_DA", "CF_DA"],
    },
    {
        "label": "SBC Link (IS-CF)",
        "formula": lambda col, ref: f"={ref('IS_SBC', col)}-{ref('CF_SBC', col)}",
        "requires": ["IS_SBC", "CF_SBC"],
    },
]


def _render_check_rows(periods, global_role_map):
    """Generate check rows for the Summary tab. Skip checks whose roles are missing."""
    rows = []
    for check in CROSS_STATEMENT_CHECKS:
        if not all(r in global_role_map for r in check["requires"]):
            continue  # Graceful skip — this company doesn't have these items

        row = ["", "", check["label"], ""]
        ref = lambda role, col: _cell_ref(role, col, global_role_map)
        for i in range(len(periods)):
            col = dcol(i)
            row.append(check["formula"](col, ref))
        rows.append(row)
    return rows
```

**What's removed:**
- All `*_COMPUTED_*` role aliases from `_render_sheet_body` (lines 66-77)
- All 9 per-tab tautological check closures (`is_revenue_check`, `is_cogs_check`, `bs_equity_check`, `cf_opcf_check`, `cf_invcf_check`, `cf_fincf_check`, `bs_ta_check`, `bs_tl_check`, `bs_balance_check`)
- The pre-registered Summary row indices

**What's kept:**
- 5 real cross-statement invariant checks on the Summary tab
- Graceful skip when a company lacks D&A, SBC, or other optional items

### 7. Pipeline gate (`run_pipeline.py`)

```python
# Stage 3: Verify model
print(f"\n=== STAGE 3: Verifying model ===")
for tf in tree_files:
    with open(tf) as f:
        trees_data = json.load(f)

    # Step 1: Tree completeness (formula consistency)
    completeness_errors = verify_tree_completeness(trees_data)
    if completeness_errors:
        print(f"Tree completeness: {len(completeness_errors)} gap(s):", file=sys.stderr)
        for stmt, name, period, declared, computed, gap in completeness_errors:
            pct = abs(gap / declared * 100) if declared else 0
            print(f"  {stmt} {name[:40]:40s} {period} "
                  f"declared={declared:>12,.0f} formula={computed:>12,.0f} "
                  f"gap={gap:>10,.0f} ({pct:.1f}%)", file=sys.stderr)
        sys.exit(1)

    # Step 2: Cross-statement invariants
    errors = verify_model(trees_data)
    if errors:
        print(f"verify_model: {len(errors)} error(s):", file=sys.stderr)
        for name, period, delta in errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
        sys.exit(1)

    print(f"  ALL PASS (completeness + invariants)")

# Stage 4: Write sheet (only reached if both gates pass)
```

---

## Order of Operations in `reconcile_trees()`

```python
def reconcile_trees(trees: dict) -> dict:
    facts = trees.get("facts", {})

    # A: Tag BS positions
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))

    # B: Tag CF positions + find CF_ENDC
    cf_endc_values = _tag_cf_positions(trees.get("CF"), facts)

    # C: Tag IS Net Income (FIXED — check root first, never overwrite values)
    _tag_is_positions(trees.get("IS"), trees.get("CF"))

    # D: Tag IS Revenue and COGS by keyword BFS (NEW)
    _tag_is_semantic(trees.get("IS"))

    # E: Override BS_CASH with CF_ENDC (only allowed value mutation)
    _override_bs_cash(trees.get("BS"), cf_endc_values)

    # F: Supplement orphan facts (NEW)
    _supplement_orphan_facts(trees)

    # G: Filter to complete periods
    _filter_to_complete_periods(trees)

    # H: Tag D&A and SBC nodes
    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))

    # Store cf_endc_values for sheet_builder
    trees["cf_endc_values"] = cf_endc_values or {}

    return trees
```

---

## Testing

### `tests/test_tree_integrity.py`

**Tree completeness:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_complete_tree_passes` | Parent=100, children=[60, 40] | No errors |
| `test_incomplete_tree_fails` | Parent=100, children=[60, 30] | 1 error: gap=10 |
| `test_rounding_within_tolerance` | Parent=100, children=[60, 39.5] | No errors (gap <= 1.0) |
| `test_nested_gap_detected` | Grandchild missing → child gap | Error at child level |
| `test_leaf_node_skipped` | Leaf node (no children) | No errors |
| `test_zero_declared_skipped` | Parent with value 0 | No errors |

**Orphan supplementation:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_orphan_closes_gap` | Parent=100, children=[70], orphan=30 | Inserted, gap=0 |
| `test_orphan_partial_close` | Parent=100, children=[50], orphan=30 | Inserted, gap=20 remains |
| `test_orphan_overshoot_rejected` | Parent=100, children=[70], orphan=40 | NOT inserted |
| `test_orphan_wrong_sign_rejected` | Gap negative, orphan positive | NOT inserted |
| `test_orphan_already_used_skipped` | Tag in another tree | NOT inserted |
| `test_multiple_orphans_greedy` | Gap=50, orphans=[30, 20, 15] | Insert 30+20, skip 15 |
| `test_bottom_up_order` | Child gap, parent depends on child | Child fixed first |

**IS tagging:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_is_root_is_ni` | Root=NetIncomeLoss, matches CF NI | Root tagged INC_NET |
| `test_is_child_is_ni` | Root=Revenue, child matches CF NI | Child tagged INC_NET |
| `test_ebt_values_preserved` | Root=NI, children=[EBT, Tax] | EBT.values unchanged |
| `test_no_value_overwrite_on_fallback` | No match | Last child tagged, values intact |

**Semantic BFS tagging:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_revenue_at_depth_1` | Standard IS tree | IS_REVENUE tagged |
| `test_revenue_nested` | Revenue under Operating Income | IS_REVENUE tagged |
| `test_no_cogs_bank` | Bank IS (no cost of revenue) | IS_COGS not tagged, no error |
| `test_cogs_by_keyword` | "Cost Of Revenue" node exists | IS_COGS tagged |
| `test_shallowest_match_wins` | "Revenue" at depth 1 and depth 3 | Depth 1 tagged |

**Three-pass rendering:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_layout_populates_all_roles` | Full tree set | global_role_map has all roles |
| `test_no_hardcoded_summary_rows` | Run layout | No pre-registered SUMM_* indices |
| `test_cross_tab_formula_resolves` | Summary check references BS tab | Valid cell ref, not "0" |
| `test_check_skipped_when_role_missing` | No IS_DA in tree | D&A check row not rendered |

### Running

```bash
pytest tests/test_tree_integrity.py

# Regression
pytest tests/test_model_historical.py
pytest tests/test_sheet_formulas.py
pytest tests/test_da_sbc_tagging.py

# E2E
python xbrl_tree.py --url <nflx_filing_url> -o nflx_trees.json
python pymodel.py --trees nflx_trees.json --checkpoint
```

---

## Implementation Plan

```
Step 1: Fix _tag_is_positions + add _values_match          (xbrl_tree.py)
Step 2: Add _tag_is_semantic + _find_by_keywords_bfs       (xbrl_tree.py)
Step 3: Add _supplement_orphan_facts + helpers              (xbrl_tree.py)
Step 4: Add verify_tree_completeness                        (pymodel.py)
   ↓ (steps 1-4 are independent)
Step 5: Update reconcile_trees order (A-H)                  (xbrl_tree.py)
Step 6: Refactor write_sheets into 3-pass architecture      (sheet_builder.py)
Step 7: Replace check closures with CROSS_STATEMENT_CHECKS  (sheet_builder.py)
Step 8: Add pipeline gate                                   (run_pipeline.py)
   ↓
Step 9: Write tests/test_tree_integrity.py
Step 10: E2E validation on NFLX + all 10 Phase 1b companies
```

---

## Success Criteria

1. **IS tagging fix**: `_tag_is_positions` never overwrites `.values`. NFLX EBT = 12,722,552 (not 10,981,201).
2. **Semantic tagging**: IS_REVENUE and IS_COGS tagged for standard companies. Banks gracefully skip IS_COGS.
3. **Orphan supplementation**: NFLX Current Liabilities gap closed by inserting real XBRL facts.
4. **Tree completeness gate**: Blocks sheet write if any parent's `SUM(children) != declared`.
5. **Three-pass rendering**: No hardcoded row indices. No pre-registered Summary roles. Cross-tab references always resolve.
6. **Real checks only**: 5 cross-statement invariant checks on Summary tab. Zero tautological checks.
7. **All 10 Phase 1b companies pass**: Both tree completeness and cross-statement invariants.
8. **Sheet formulas match declared values**: For every parent row, `=SUM(children)` equals the XBRL-declared value within rounding tolerance.

---

## Known Risks

1. **Orphan facts may not fully close the gap.** Some items aren't tagged in XBRL at all. Pipeline stops; manual inspection needed.

2. **False-positive orphan matches.** A fact might coincidentally reduce the gap. The all-period, no-overshoot constraints make this statistically unlikely but not impossible.

3. **Semantic keyword tagging is heuristic.** "Revenue" in a node name doesn't guarantee it's the Revenue subtotal. BFS preference for shallowest match + requiring non-zero values mitigates this. If a company uses unusual terminology, IS_REVENUE may not be tagged — formatting falls back to label-based detection.

4. **BS_CASH override is the only allowed value mutation.** `_override_bs_cash` changes BS_CASH values to match CF_ENDC. Justified because both represent the same real-world quantity (ending cash) under different XBRL contexts.
