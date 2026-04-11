# Phase 3b Spec: Tree Integrity & Orphan Fact Supplementation

## Summary

Phase 3 made the sheet live — parent cells became formulas (`=SUM(children)`), check rows verified subtotals. But the formulas are wrong when the XBRL calculation linkbase is incomplete: a parent's declared value doesn't equal the sum of its children because children are missing from the calc linkbase.

**Phase 3b ensures every parent formula in the sheet produces the correct number.** It does this by finding XBRL-tagged facts that belong to a parent but aren't linked in the calc linkbase, and inserting them as children. It also fixes a bug where `_tag_is_positions` corrupts the IS tree by overwriting EBT values with Net Income values.

No plugs. No catch-alls with fabricated numbers. Every inserted child is a real XBRL fact with a real tag and real values. If the gap can't be filled with real data, the tree is flagged as incomplete and the sheet is not written.

**The Google Sheet is the product — it's what analysts use, share, and discuss. Python is the build system.** Python verifies the model is correct, then writes it to Sheets. If the model can't be verified, the sheet doesn't get written.

---

## The Problem Phase 3b Solves

### Problem 1: Incomplete calc linkbase trees

The XBRL calculation linkbase (`_cal.xml`) defines parent-child relationships. But filings routinely omit children. The parent's declared value (from iXBRL) is correct, but `SUM(children)` doesn't match because items are missing.

**NFLX example (2024):**

```
Current Liabilities (declared: $10,755,400)
  + Content Liabilities Current:     4,393,681
  + Accounts Payable Current:          899,909
  + Accrued Liabilities Current:     2,156,544
  + Contract With Customer:              1,521
  + Short Term Borrowings:           1,784,453
  ─────────────────────────────────────────────
  SUM(children):                     9,236,108
  GAP:                               1,519,292  (14.1%)
```

The missing items DO exist as XBRL facts in the filing:
- `us-gaap:OperatingLeaseLiabilityCurrent`: $428,482
- Other items tagged but not linked in the calc linkbase

The sheet formula `=SUM(children)` produces 9,236,108 instead of 10,755,400 — a $1.5M error. This isn't a rounding issue; it's missing line items.

### Problem 2: `_tag_is_positions` corrupts the IS tree

`_tag_is_positions` in `xbrl_tree.py` searches IS tree children for one whose values match CF's Net Income. When no value-match is found, it falls back to the first positive-weight child (EBT) and **overwrites its declared values**:

```python
# xbrl_tree.py:703 (current, broken)
fallback.values = dict(cf_ni_values)  # DESTROYS EBT's declared values
```

**NFLX result:**
```
Before _tag_is_positions (XBRL data correct):
  EBT = 12,722,552    Tax = 1,741,351    NI = 10,981,201
  EBT - Tax = NI  ✓

After _tag_is_positions (corrupted):
  EBT = 10,981,201 (OVERWRITTEN)    Tax = 1,741,351
  Formula: 10,981,201 - 1,741,351 = 9,239,850 ≠ 10,981,201  ✗
```

Root cause: NFLX's IS calc linkbase roots at `NetIncomeLoss`, not Revenue. The IS root IS Net Income. But `_tag_is_positions` searches only children for the NI value match, doesn't find one (the root matches, not a child), and falls back to overwriting the first child's values.

### Problem 3: No pre-write gate

`sheet_builder.py` writes to Google Sheets regardless of whether the tree is formula-consistent. There's no verification that `SUM(children) == declared` before formulas are committed.

### Problem 4: Tautological sheet checks

`_render_sheet_body` creates `*_COMPUTED_*` role aliases (e.g., `CF_COMPUTED_OPCF`) that point to the same cell as the base role (`CF_OPCF`). Check formulas like `=CF_COMPUTED_OPCF - CF_OPCF` compute `=E5-E5` = 0 always. These checks can never fail and catch nothing.

---

## Architecture

```
XBRL Filing
       │
       ├─► build_statement_trees()     Build IS/BS/CF trees from calc linkbase
       │         │
       │         ├─► _tag_bs_positions()      Tag BS nodes by position
       │         ├─► _tag_cf_positions()      Tag CF nodes + find CF_ENDC
       │         ├─► _tag_is_positions()      Tag IS NI (FIXED — no value overwrite)
       │         ├─► _override_bs_cash()      BS_CASH = CF_ENDC by construction
       │         ├─► _supplement_orphan_facts()   NEW — fill gaps from XBRL facts
       │         ├─► _filter_to_complete_periods()
       │         └─► _tag_da_sbc_nodes()      Tag D&A/SBC for sheet formulas
       │
       ├─► verify_tree_completeness()   NEW — parent == SUM(children)?
       │         │
       │         └─► FAIL → stop, report gaps
       │
       ├─► verify_model()              Existing 5 cross-statement invariants
       │         │
       │         └─► FAIL → stop, report errors
       │
       └─► sheet_builder.py            Write sheet (only if both pass)
                 │
                 └─► Formulas guaranteed correct
```

---

## What Changes

### 1. Fix `_tag_is_positions` — never overwrite `.values` (`xbrl_tree.py`)

The IS tree root may itself be Net Income (when the calc linkbase roots at `NetIncomeLoss` rather than `Revenue`). The fix:

1. Check if the IS tree ROOT's values match CF's NI. If yes, tag the root as `INC_NET`.
2. Only if the root doesn't match, search children.
3. **Never overwrite `.values`** — only set `.role`.

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
```

New helper:

```python
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

### 2. Orphan fact supplementation (`xbrl_tree.py`)

New function `_supplement_orphan_facts()` runs during `reconcile_trees()`, after tagging and BS_CASH override, before period filtering.

#### Algorithm

For every parent node where `abs(declared - SUM(children * weight)) > tolerance`:

1. Collect all XBRL tags already used by any tree node.
2. Search the XBRL facts dict for tags NOT already used, that have values for the gap periods.
3. For each candidate, check: does adding it (weight +1) reduce the gap without overshooting?
4. Insert qualifying candidates as new leaf children, largest first (greedy).
5. After all candidates inserted, the remaining gap (if any) will be caught by `verify_tree_completeness()`.

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

    # Collect all tags already used in trees
    used_tags = set()
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _collect_tags(tree, used_tags)

    # Walk every tree bottom-up and try to fill gaps
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _fill_gaps(tree, facts, used_tags, stmt, tolerance)
```

```python
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

        # Check: adding this fact must reduce gap without overshooting
        reduces_all = True
        for p, gap_val in gaps.items():
            fact_val = tag_values.get(p, 0)
            if fact_val == 0:
                continue
            # Same sign as gap, and doesn't overshoot
            if (gap_val > 0 and fact_val < 0) or (gap_val < 0 and fact_val > 0):
                reduces_all = False
                break
            if abs(fact_val) > abs(gap_val) + tolerance:
                reduces_all = False
                break
        if reduces_all:
            candidates.append((tag, tag_values))

    # Sort by total absolute value (largest first — greedy)
    candidates.sort(key=lambda c: -sum(abs(c[1].get(p, 0)) for p in gaps))

    for tag, tag_values in candidates:
        # Recompute remaining gap
        remaining = {}
        for p in periods:
            declared = node.values.get(p, 0)
            computed = sum(c.values.get(p, 0) * c.weight for c in node.children)
            gap = declared - computed
            if abs(gap) > tolerance:
                remaining[p] = gap

        if not remaining:
            break

        # Verify this candidate still helps with current gap
        helps = any(
            tag_values.get(p, 0) != 0 and abs(remaining[p] - tag_values.get(p, 0)) < abs(remaining[p])
            for p in remaining
        )
        if not helps:
            continue

        # Insert as new leaf child
        concept = tag.replace(':', '_')
        new_child = TreeNode(concept, weight=1.0)
        new_child.values = {p: v for p, v in tag_values.items() if p in periods}
        node.add_child(new_child)
        used_tags.add(tag)

        print(f"  SUPPLEMENT: {stmt} {node.name[:30]} += {new_child.name} "
              f"({tag})", file=sys.stderr)
```

### 3. Tree completeness verification (`pymodel.py`)

New function that checks every parent node. Returns errors if any parent's formula wouldn't match its declared value.

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

### 4. Pipeline gate (`run_pipeline.py`)

Stage 3 becomes a two-step verification that blocks sheet writing on failure:

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

# Stage 4: Write sheet (only reached if all checks pass)
```

### 5. Remove tautological checks (`sheet_builder.py`)

Delete the `*_COMPUTED_*` alias logic from `_render_sheet_body` (lines 66-77):

```python
# DELETE these lines:
if node.role == "IS_REVENUE":
    global_role_map["IS_COMPUTED_REVENUE"] = (sheet_name, row_num)
elif node.role == "IS_COGS":
    global_role_map["IS_COMPUTED_COGS"] = (sheet_name, row_num)
# ... etc
```

Delete the per-tab check formulas that referenced these aliases (`is_revenue_check`, `is_cogs_check`, `bs_equity_check`, `cf_opcf_check`, `cf_invcf_check`, `cf_fincf_check`). These are tautological — they compare a cell to itself.

Keep the Summary tab's 5 cross-statement invariant checks, which use real cross-tab references:
1. BS Balance: `=BS_TA - BS_TL - BS_TE`
2. Cash Link: `=CF_ENDC - BS_CASH`
3. NI Link: `=INC_NET - INC_NET_CF`
4. D&A Link: `=IS_DA - CF_DA` (when both roles exist)
5. SBC Link: `=IS_SBC - CF_SBC` (when both roles exist)

---

## Orphan Fact Matching — Design Constraints

### What qualifies as an orphan fact

A fact is "orphan" if:
1. Its XBRL tag exists in the filing's iXBRL data (parsed by `parse_xbrl_facts.py`)
2. It is NOT used by any node in any statement tree (IS/BS/BS_LE/CF)
3. It has values for at least one period where a gap exists

### How orphans are matched to parents

NOT by name. We don't guess which parent a fact belongs to by concept name or taxonomy position. We match purely by **gap-reduction**:

1. Adding the fact's values must reduce `abs(declared - SUM(children))` for every period where both the fact and the gap are non-zero.
2. The fact must not overshoot the gap (creating a new error in the opposite direction).
3. Greedy insertion: largest-value candidates first.

### What we do NOT do

- **No name matching**: no heuristics like "OperatingLeaseLiabilityCurrent sounds like it belongs under LiabilitiesCurrent."
- **No fabrication**: every inserted node has values directly from XBRL tags.
- **No plugs**: no "Other" or "Remainder" nodes. If orphan facts don't fully close the gap, the gap remains and `verify_tree_completeness()` blocks the sheet write.
- **No value mutation**: no overwriting of existing node values.

---

## Order of Operations in `reconcile_trees()`

```python
def reconcile_trees(trees: dict) -> dict:
    facts = trees.get("facts", {})

    # A: Tag BS positions
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))

    # B: Tag CF positions + find CF_ENDC
    cf_endc_values = _tag_cf_positions(trees.get("CF"), facts)

    # C: Tag IS positions (FIXED — no value overwriting)
    _tag_is_positions(trees.get("IS"), trees.get("CF"))

    # D: Override BS_CASH with CF_ENDC (only value mutation allowed)
    _override_bs_cash(trees.get("BS"), cf_endc_values)

    # E: Supplement orphan facts (NEW — insert real XBRL facts as children)
    _supplement_orphan_facts(trees)

    # F: Filter to complete periods
    _filter_to_complete_periods(trees)

    # G: Tag D&A and SBC nodes
    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))

    # Store cf_endc_values for sheet_builder
    trees["cf_endc_values"] = cf_endc_values or {}

    return trees
```

Orphan supplementation (E) runs BEFORE period filtering (F) so supplemented nodes have full period data before filtering trims to complete periods only.

---

## Testing

### `tests/test_tree_integrity.py`

**Tree completeness verification:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_complete_tree_passes` | Parent=100, children=[60, 40] | No errors |
| `test_incomplete_tree_fails` | Parent=100, children=[60, 30] | 1 error: gap=10 |
| `test_rounding_within_tolerance` | Parent=100, children=[60, 39.5] | No errors (gap=0.5 <= 1.0 tolerance) |
| `test_nested_gap_detected` | Grandchild missing → child gap | Error at child level |
| `test_leaf_node_skipped` | Leaf node (no children) | No errors |
| `test_zero_declared_skipped` | Parent with declared value 0 | No errors |

**Orphan fact supplementation:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_orphan_closes_gap` | Parent=100, children=[70], orphan fact=30 | Orphan inserted, gap=0 |
| `test_orphan_partial_close` | Parent=100, children=[50], orphan=30 | Orphan inserted, gap=20 remains |
| `test_orphan_overshoot_rejected` | Parent=100, children=[70], orphan=40 | NOT inserted (40 > gap of 30) |
| `test_orphan_wrong_sign_rejected` | Gap is negative, orphan is positive | NOT inserted |
| `test_orphan_already_used_skipped` | Orphan tag already in another tree | NOT inserted |
| `test_multiple_orphans_greedy` | Gap=50, orphans=[30, 20, 15] | Insert 30 then 20, skip 15 |
| `test_no_orphans_available` | Gap exists, no matching facts | No insertion, gap remains |
| `test_bottom_up_order` | Child has gap, parent depends on child | Child gap fixed before parent checked |

**IS tagging fix:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_is_root_is_ni` | IS root=NetIncomeLoss, values match CF NI | Root tagged INC_NET |
| `test_is_child_is_ni` | IS root=Revenue, child=NI matches CF | Child tagged INC_NET |
| `test_ebt_values_preserved` | IS root=NI, children=[EBT, Tax] | EBT.values unchanged |
| `test_no_value_overwrite_on_fallback` | No child matches CF NI | Last child tagged INC_NET, values NOT overwritten |

### Running

```bash
# New tests
pytest tests/test_tree_integrity.py

# Regression
pytest tests/test_model_historical.py
pytest tests/test_sheet_formulas.py
pytest tests/test_da_sbc_tagging.py

# End-to-end: NFLX should pass completeness + invariants
python xbrl_tree.py --url <nflx_filing_url> -o nflx_trees.json
python pymodel.py --trees nflx_trees.json --checkpoint
```

---

## Implementation Plan

```
Step 1: Fix _tag_is_positions + add _values_match helper     (xbrl_tree.py)
   ↓
Step 2: Add _supplement_orphan_facts + helpers                (xbrl_tree.py)
   ↓
Step 3: Add verify_tree_completeness                          (pymodel.py)
   ↓
Step 4: Remove *_COMPUTED_* aliases + tautological checks     (sheet_builder.py)
   ↓
Step 5: Update reconcile_trees order                          (xbrl_tree.py)
   ↓
Step 6: Add pipeline gate in run_pipeline.py
   ↓
Step 7: Write tests/test_tree_integrity.py
   ↓
Step 8: E2E validation on NFLX + all 10 Phase 1b companies
```

Steps 1-4 are independent code changes. Steps 5-6 integrate them. Steps 7-8 verify.

---

## Success Criteria

1. **IS tagging fix**: `_tag_is_positions` never overwrites `.values`. NFLX's EBT retains its original XBRL value (12,722,552 for 2025, not 10,981,201).
2. **Orphan supplementation**: NFLX's Current Liabilities gap ($1.5M) is closed by inserting real XBRL facts as children.
3. **Tree completeness gate**: `verify_tree_completeness()` passes for all nodes before sheet is written. Any remaining gaps block the write with a clear error message.
4. **Tautological checks removed**: No `*_COMPUTED_*` role aliases. No self-referencing check formulas in the sheet.
5. **Cross-statement checks work**: Summary tab's 5 invariant checks use real cross-tab cell references and evaluate to 0.
6. **All 10 Phase 1b companies pass**: Both tree completeness and cross-statement invariants.
7. **Sheet formulas match declared values**: For every parent row, `=SUM(children)` equals the XBRL-declared value within rounding tolerance.

---

## Known Risks

1. **Orphan facts may not fully close the gap.** Some filings have items not tagged in XBRL at all. If orphan supplementation can't close the gap, the pipeline stops. Manual inspection needed.

2. **False-positive orphan matches.** An orphan fact might coincidentally reduce the gap without being a real child. The all-period, no-overshoot constraints minimize this, but the risk isn't zero. Requiring gap reduction across ALL non-zero periods (not just one) makes false positives statistically unlikely.

3. **BS_CASH override is the only allowed value mutation.** `_override_bs_cash` changes BS_CASH values to match CF_ENDC by construction. This is the one exception to the "never overwrite values" rule, and it's justified because both represent the same real-world quantity (ending cash) — just reported under different XBRL contexts.
