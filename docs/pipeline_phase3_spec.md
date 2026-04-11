# Phase 3 Spec: Live Sheet Formulas

## Summary

Phase 2 established the tree as the single source of truth for historical data. `reconcile_trees()` tags key nodes by position, `verify_model()` checks 5 cross-statement invariants in Python, and `sheet_builder.py` renders trees directly to Google Sheets.

But the current sheet is **dead** — every cell is a hardcoded number. Parent nodes that the XBRL tree defines as `sum(child * weight)` are written as static values. The model lacks inline checks and proper number formatting.

**Phase 3 makes the Google Sheet a working financial model.** Parent cells become `=SUM()` or `=A-B` formulas based on XBRL tree weights (+1/-1). 14 in-line "Check" rows across all 4 tabs (IS, BS, CF, 3-Statement Summary) verify every subtotal against its source. The sheet proves its own correctness, and formatting matches the exact spec.

---

## The Problem Phase 3 Solves

### Problem 1: Dead sheet

`_render_sheet_body()` writes `round(node.values[period])` for every node — leaves AND parents alike. But the tree already knows:

- `node.weight = +1` means this child **adds** to parent
- `node.weight = -1` means this child **subtracts** from parent
- Parent value = `sum(child.value * child.weight)`

The sheet should reflect this. Revenue (weight +1) + Cost of Revenue (weight -1) = Gross Profit should be `=E4+E5` where E5 contains a negative number, or `=E4-E5` if we show costs as positive. The XBRL tree defines the exact formula for every company.

### Problem 2: Incomplete invariants & Missing Formatting

The previous approach used a separate "Summary" tab with missing or placeholder checks. The actual specification requires:
- **In-line Check Rows**: Invariants should be evaluated directly at the bottom of the relevant statements (e.g., `Check` below Total Assets).
- **Metrics**: Key percentages (YoY Growth, Margins) must be appended to the bottom of the statements.
- **Strict Number Formatting & Styling**: Specific Sheets API number formats (`CURRENCY`, `NUMBER`, zero-dashes) and styling (Italic labels for checks/metrics) are required.

---

## Architecture

```
reconciled trees → sheet_builder.py → Google Sheet with live formulas
                                       - Leaves: hardcoded values (XBRL data)
                                       - Parents: =SUM/=DIFF formulas from tree weights
                                       - CF tab: BEGC / NETCH / ENDC cash proof rows
                                       - In-Line: "Check" rows and "Metrics" appended to statements
                                       - Formatting: API batchUpdates applied to format numbers and styles
```

No new files. No changes to `pymodel.py` or `run_pipeline.py`. This is purely a `sheet_builder.py` rewrite + a small addition to `reconcile_trees()` in `xbrl_tree.py` for D&A/SBC node tagging.

---

## How XBRL tree weights map to sheet formulas

Every `TreeNode` has a `weight` (+1.0 or -1.0) relative to its parent. The parent's value = `sum(child_value * child_weight)`.

In the sheet, each node occupies one row. The parent row's formula references its children's rows:

```
Row 4:  Revenue                    391035    ← leaf, hardcoded value
Row 5:    Products                 295347    ← leaf, hardcoded value  
Row 6:    Services                  95688    ← leaf, hardcoded value
```

If Revenue has children Products (weight +1) and Services (weight +1):
```
Row 4:  Revenue    =E5+E6                   ← parent formula: sum of children
Row 5:    Products  295347                   ← leaf, hardcoded
Row 6:    Services   95688                   ← leaf, hardcoded
```

For subtraction (e.g., IS cascade where COGS has weight -1 under Gross Profit):
```
Row 4:  Revenue              =E5+E6         ← parent = Products + Services
Row 7:  Cost of Revenue      170782          ← leaf (shown positive, weight = -1)
Row 8:  Gross Profit         =E4-E7         ← parent formula using weights
```

**General formula pattern:**
```
=child1_cell * weight1 + child2_cell * weight2 + ...
```

Where `weight` is from `child.weight` (always +1 or -1). When all children have weight +1, this simplifies to `=SUM(child_range)`. When mixed, it becomes explicit additions and subtractions.

---

## Existing Code Reference

These are the key existing functions/classes you need to understand before making changes. Read these carefully — they are the "before" state.

### `TreeNode` class (`xbrl_tree.py:197-254`)

```python
class TreeNode:
    """A node in the XBRL calculation tree."""

    def __init__(self, concept: str, weight: float = 1.0):
        self.concept = concept
        self.tag = _concept_to_tag(concept)
        self.name = _clean_name(concept)
        self.weight = weight  # +1 or -1 relative to parent
        self.children: list[TreeNode] = []
        self.values: dict[str, float] = {}  # {period: value}
        self.is_leaf = True
        self.role: str | None = None    # e.g., "BS_TA", "INC_NET", "CF_ENDC"

    def add_child(self, child: 'TreeNode'):
        self.children.append(child)
        self.is_leaf = False

    # ... also has to_dict(), from_dict(), leaf_children, branch_children properties
```

Key fields for Phase 3:
- `node.weight` → +1.0 or -1.0, used to build formulas
- `node.children` → list of child TreeNodes
- `node.values` → `{period_string: float_value}`
- `node.role` → string like `"BS_TA"`, `"CF_OPCF"`, or `None`
- `node.is_leaf` → `True` if no children (hardcoded value), `False` if parent (formula)
- `node.name` → human-readable label (e.g., "Revenue", "Cost Of Revenue")
- `node.concept` → XBRL concept (e.g., `"us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax"`)

### `dcol()` — existing helper (`sheet_builder.py:16-23`)

```python
def dcol(i):
    """Data column letter(s). i=0 → E, i=1 → F, etc. (data starts col E)."""
    col_num = i + 4
    result = ""
    while col_num >= 0:
        result = chr(65 + col_num % 26) + result
        col_num = col_num // 26 - 1
    return result
```

This already exists. `dcol(0)` = `"E"`, `dcol(1)` = `"F"`, etc. Data columns start at E because columns A-D are prefix columns (spacer, spacer, label, spacer).

### `_cell_ref()` — existing helper (`sheet_builder.py:80-85`)

```python
def _cell_ref(role, col):
    entry = global_role_map.get(role)
    if not entry:
        return "0"
    sheet_name, row_num = entry
    return f"'{sheet_name}'!{col}{row_num}"
```

This already exists inside `_write_summary_tab()`. In Phase 3 you'll need to extract it to module level so it can be used by Check row formula functions too.

### `_render_sheet_body()` — CURRENT code to be REPLACED (`sheet_builder.py:25-59`)

```python
def _render_sheet_body(tree, periods, start_row, global_role_map, sheet_name):
    """Render a tree into rows, tracking role → absolute row numbers."""
    rows = []
    current_row = [start_row]  # mutable counter  ← THIS IS THE PROBLEM

    def _walk(node, indent=0):
        # Access node data (dict or TreeNode)
        if isinstance(node, dict):
            name = node.get("name", "")
            values = node.get("values", {})
            children = node.get("children", [])
            role = node.get("role")
        else:
            name = node.name
            values = node.values
            children = node.children
            role = getattr(node, "role", None)

        label = ("  " * indent) + name
        row = ["", "", label, ""]
        for p in periods:
            val = values.get(p, 0)
            row.append(round(val) if val else "")   # ← EVERY cell is hardcoded
        rows.append(row)

        # Record role → absolute row number
        if role:
            global_role_map[role] = (sheet_name, current_row[0])
        current_row[0] += 1

        for child in children:
            _walk(child, indent + 1)

    _walk(tree)
    return rows
```

**Problems with this code:**
1. `current_row = [start_row]` — mutable counter in a closure, error-prone
2. Every cell is `round(val)` — parents should be formulas, only leaves should be hardcoded
3. Supports both `dict` and `TreeNode` inputs — Phase 3 only needs `TreeNode` (trees are always `TreeNode` by this point)

### `_write_summary_tab()` — CURRENT code to be REPLACED (`sheet_builder.py:65-112`)

```python
def _write_summary_tab(sid, periods, global_role_map):
    """Write the Summary tab with 5 invariant checks as live spreadsheet formulas."""
    rows = [
        [],
        ["", "", "Invariant Checks", ""] + list(periods),
        [],
    ]

    def _formula_row(label, formula_fn):
        row = ["", "", label, ""]
        for i in range(len(periods)):
            col = dcol(i)
            row.append(formula_fn(col))
        rows.append(row)

    def _cell_ref(role, col):
        entry = global_role_map.get(role)
        if not entry:
            return "0"
        sheet_name, row_num = entry
        return f"'{sheet_name}'!{col}{row_num}"

    # 1. BS Balance: TA - TL - TE = 0
    _formula_row("BS Balance (TA-TL-TE)",
        lambda col: f"={_cell_ref('BS_TA', col)} - {_cell_ref('BS_TL', col)} - {_cell_ref('BS_TE', col)}")

    # 2. Cash Link: CF_ENDC - BS_CASH = 0
    _formula_row("Cash Link (CF_ENDC - BS_CASH)", lambda col: f"=0")   # ← placeholder!

    # 3. NI Link: IS INC_NET - CF INC_NET_CF = 0
    _formula_row("NI Link (IS - CF)",
        lambda col: f"={_cell_ref('INC_NET', col)} - {_cell_ref('INC_NET_CF', col)}")

    # 4. D&A Link
    rows.append(["", "", "D&A Link (IS - CF)", ""] + ["n/a"] * len(periods))  # ← placeholder!

    # 5. SBC Link
    rows.append(["", "", "SBC Link (IS - CF)", ""] + ["n/a"] * len(periods))  # ← placeholder!

    # Total errors row
    rows.append([])
    total_row = ["", "", "TOTAL ERRORS (checks 1-3)", ""]
    for i in range(len(periods)):
        col = dcol(i)
        total_row.append(f"=ABS({col}4)+ABS({col}5)+ABS({col}6)")
    rows.append(total_row)

    gws_write(sid, f"Summary!A1:{dcol(len(periods)-1)}{len(rows)}", rows)
```

**Problems:** Cash Link is `=0` (placeholder), D&A and SBC are `"n/a"`. Phase 3 replaces this entire approach with 14 in-line Check rows.

### `write_sheets()` — orchestration function (`sheet_builder.py:114-174`)

```python
def write_sheets(trees: dict, company: str) -> tuple[str, str]:
    """Render reconciled trees to a Google Sheet."""
    periods = trees.get("complete_periods", [])
    sid, url, sheet_ids = gws_create(f"{company} — Financial Model", ["IS", "BS", "CF", "Summary"])

    global_role_map = {}

    # --- IS tab ---
    is_tree = trees.get("IS")
    if is_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_sheet_body(is_tree, periods, start_row=len(header_rows)+1,
                                        global_role_map=global_role_map, sheet_name="IS")
        is_rows = header_rows + body_rows
        _write_sheet_tab(sid, "IS", is_rows, periods, is_tree, global_role_map)

    # --- BS tab ---
    bs_tree = trees.get("BS")
    bs_le_tree = trees.get("BS_LE")
    if bs_tree or bs_le_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = []
        current_row = len(header_rows) + 1
        if bs_tree:
            assets_rows = _render_sheet_body(bs_tree, periods, start_row=current_row,
                                              global_role_map=global_role_map, sheet_name="BS")
            body_rows += assets_rows
            current_row += len(assets_rows)
            body_rows.append([""] * (4 + len(periods)))  # blank separator
            current_row += 1
        if bs_le_tree:
            le_rows = _render_sheet_body(bs_le_tree, periods, start_row=current_row,
                                          global_role_map=global_role_map, sheet_name="BS")
            body_rows += le_rows
        bs_rows = header_rows + body_rows
        _write_sheet_tab(sid, "BS", bs_rows, periods, None, global_role_map)

    # --- CF tab ---
    cf_tree = trees.get("CF")
    if cf_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_sheet_body(cf_tree, periods, start_row=len(header_rows)+1,
                                        global_role_map=global_role_map, sheet_name="CF")
        cf_rows = header_rows + body_rows
        _write_sheet_tab(sid, "CF", cf_rows, periods, None, global_role_map)

    # --- Summary tab ---
    _write_summary_tab(sid, periods, global_role_map)

    # Column widths
    requests = []
    for sheet_name, sheet_id in sheet_ids.items():
        requests.extend([
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": 0, "endIndex": 2},
                "properties": {"pixelSize": 50}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": 2, "endIndex": 3},
                "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
        ])
    gws_batch_update(sid, requests)

    return sid, url
```

### `reconcile_trees()` — where Step F goes (`xbrl_tree.py:663-682`)

```python
def reconcile_trees(trees: dict) -> dict:
    """Tag key nodes by position and apply cross-statement value overrides."""
    facts = trees.get("facts", {})

    # --- Step A: Tag Balance Sheet positions ---
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))

    # --- Step B: Tag CF structural positions + find CF_ENDC ---
    cf_endc_values = _tag_cf_positions(trees.get("CF"), facts)

    # --- Step C: Tag IS positions using CF's NI as authoritative ---
    _tag_is_positions(trees.get("IS"), trees.get("CF"))

    # --- Step D: Apply cross-statement value overrides ---
    _override_bs_cash(trees.get("BS"), cf_endc_values)

    # --- Step E: Filter to complete periods ---
    _filter_to_complete_periods(trees)

    # ← Step F goes HERE (before return)

    return trees
```

### `_tag_cf_positions()` — where FX tagging goes (`xbrl_tree.py:476-551`)

```python
def _tag_cf_positions(cf_tree: TreeNode | None, facts: dict) -> dict | None:
    """Tag CF nodes by position. Returns CF_ENDC values dict (from facts, not tree)."""
    cf_endc_values = None

    # Look up CF_ENDC from XBRL facts (instant context, not in any tree)
    if facts:
        endc_tags = [
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        ]
        for tag in endc_tags:
            if tag in facts:
                cf_endc_values = facts[tag]
                break

    if not cf_tree:
        return cf_endc_values

    # Tag the root as net change in cash
    cf_tree.role = "CF_NETCH"

    # Map concept name patterns to roles
    CF_ROLE_MAP = {
        "NetCashProvidedByUsedInOperatingActivities": "CF_OPCF",
        "NetCashProvidedByUsedInInvestingActivities": "CF_INVCF",
        "NetCashProvidedByUsedInFinancingActivities": "CF_FINCF",
    }

    seen_roles = set()

    def _walk_and_tag(node: TreeNode):
        concept_name = node.concept.split('_', 1)[-1] if '_' in node.concept else node.concept
        for pattern, role in CF_ROLE_MAP.items():
            if concept_name.startswith(pattern) and role not in seen_roles and node.values:
                node.role = role
                seen_roles.add(role)
                return
        if concept_name in ("ProfitLoss", "NetIncomeLoss") and node.values and not node.children:
            node.role = "INC_NET_CF"
        for child in node.children:
            _walk_and_tag(child)

    _walk_and_tag(cf_tree)

    # ... (NI search in OPCF subtree, warnings for missing roles)
    
    return cf_endc_values
```

### `find_node_by_role()` (`xbrl_tree.py:396-404`)

```python
def find_node_by_role(tree: TreeNode, role: str) -> TreeNode | None:
    """Recursively search the tree for a node with the given role."""
    if getattr(tree, "role", None) == role:
        return tree
    for child in tree.children:
        result = find_node_by_role(child, role)
        if result:
            return result
    return None
```

### `gws_batch_update()` (`gws_utils.py:38-47`)

```python
def gws_batch_update(sid: str, requests: list):
    """Execute a batch update on a Google Sheets spreadsheet."""
    params = json.dumps({"spreadsheetId": sid})
    body = json.dumps({"requests": requests})
    _run_gws("sheets", "spreadsheets", "batchUpdate", "--params", params, "--json", body)
```

### `gws_write()` (`gws_utils.py:25-35`)

```python
def gws_write(sid: str, range_: str, values: list):
    """Write values to a Google Sheets range."""
    params = json.dumps({"spreadsheetId": sid, "range": range_,
                          "valueInputOption": "USER_ENTERED"})
    body = json.dumps({"values": values})
    _run_gws("sheets", "spreadsheets", "values", "update", "--params", params, "--json", body)
```

### Existing test patterns (`tests/test_model_historical.py`)

```python
# Fixture helpers used in existing tests:
def _make_leaf(concept, weight=1.0, values=None):
    node = TreeNode(concept, weight)
    node.values = values or {}
    return node

def _make_parent(concept, children, weight=1.0, values=None):
    node = TreeNode(concept, weight)
    node.values = values or {}
    for c in children:
        node.add_child(c)
    return node
```

Use these same helpers in your new test files.

---

## What Changes

### `xbrl_tree.py` — Tag D&A, SBC, and FX nodes

The current `reconcile_trees()` tags BS and CF structural nodes but does NOT tag D&A or SBC nodes. The D&A/SBC invariant checks in `verify_model()` use value-matching at runtime. That works for Python verification, but the sheet needs **specific cell references**.

**New helper: `_find_leaf_by_keywords()`**

```python
def _find_leaf_by_keywords(tree: TreeNode, keywords: list[str]) -> TreeNode | None:
    """Find a leaf node whose name contains all keywords (case-insensitive)."""
    name_lower = tree.name.lower()
    if tree.is_leaf and all(kw in name_lower for kw in keywords):
        return tree
    for child in tree.children:
        result = _find_leaf_by_keywords(child, keywords)
        if result:
            return result
    return None
```

**New helper: `_find_leaf_by_timeseries()`**

```python
def _find_leaf_by_timeseries(tree: TreeNode, periods: list[str],
                              target_values: dict[str, float]) -> TreeNode | None:
    """Find a leaf node whose values match target across ALL periods (within 0.5).
    
    This is the "collision-safe" version of single-period value matching.
    A coincidental match on one period is possible; matching across all periods
    is statistically definitive.
    """
    if tree.is_leaf and tree.values:
        matched = 0
        total = 0
        for p in periods:
            target = target_values.get(p, 0)
            actual = tree.values.get(p, 0)
            if target != 0:
                total += 1
                if abs(actual - target) < 0.5:
                    matched += 1
        if total > 0 and matched == total:
            return tree
    for child in tree.children:
        result = _find_leaf_by_timeseries(child, periods, target_values)
        if result:
            return result
    return None
```

**New function: `_tag_da_sbc_nodes()`**

```python
def _tag_da_sbc_nodes(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    """Tag D&A and SBC leaf nodes in IS and CF trees by name + time-series value matching.
    
    Uses full time-series matching to prevent collisions (e.g., D&A = $150M in 2024,
    but "Changes in Inventory" also happens to be $150M in 2024).
    """
    if not is_tree or not cf_tree:
        return
    
    cf_opcf = find_node_by_role(cf_tree, "CF_OPCF")
    if not cf_opcf:
        return
    
    periods = [p for p in (is_tree.values.keys() if is_tree.values else [])
               if is_tree.values.get(p, 0) != 0]
    if not periods:
        return
    
    # D&A: find IS leaf with "depreciation" or "amortization" in name
    is_da = _find_leaf_by_keywords(is_tree, ["depreciation"])
    if not is_da:
        is_da = _find_leaf_by_keywords(is_tree, ["amortization"])
    
    if is_da:
        is_da.role = "IS_DA"
        cf_da = _find_leaf_by_timeseries(cf_opcf, periods, is_da.values)
        if cf_da:
            cf_da.role = "CF_DA"
        else:
            print("WARNING: Could not find CF D&A node matching IS D&A values",
                  file=sys.stderr)
    
    # SBC: find IS leaf with "stock" and "compensation" in name
    is_sbc = _find_leaf_by_keywords(is_tree, ["stock", "compensation"])
    if not is_sbc:
        is_sbc = _find_leaf_by_keywords(is_tree, ["share", "compensation"])
    
    if is_sbc:
        is_sbc.role = "IS_SBC"
        cf_sbc = _find_leaf_by_timeseries(cf_opcf, periods, is_sbc.values)
        if cf_sbc:
            cf_sbc.role = "CF_SBC"
        else:
            print("WARNING: Could not find CF SBC node matching IS SBC values",
                  file=sys.stderr)
```

**FX tagging — add to `_tag_cf_positions()` (insert after the `_walk_and_tag(cf_tree)` call, around line 524):**

```python
    # --- Tag FX impact node (if present) ---
    FX_PATTERNS = ["EffectOfExchangeRate", "EffectOfForeignExchangeRate"]
    for child in cf_tree.children:
        concept_name = child.concept.split('_', 1)[-1] if '_' in child.concept else child.concept
        for pat in FX_PATTERNS:
            if concept_name.startswith(pat) and child.values:
                child.role = "CF_FX"
                break
```

**Step F — add to `reconcile_trees()` (insert before `return trees`, line 682):**

```python
    # --- Step F: Tag D&A and SBC nodes for sheet formula references ---
    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))
```

### `sheet_builder.py` — Rewrite for live formulas

#### New function: `_build_weight_formula()`

```python
def _build_weight_formula(col: str, child_rows: list[tuple[int, float]]) -> str:
    """Build a cell formula from child row numbers and XBRL weights.
    
    Args:
        col: column letter (e.g., "E")
        child_rows: [(row_num, weight), ...] where weight is +1.0 or -1.0
    
    Returns:
        A formula string like "=E5+E6" or "=E5-E7"
    """
    if not child_rows:
        return ""
    
    # Check if all weights are +1 (simple SUM case)
    all_positive = all(w == 1.0 for _, w in child_rows)
    
    if all_positive and len(child_rows) > 1:
        # Contiguous range check
        row_nums = [r for r, _ in child_rows]
        if row_nums == list(range(row_nums[0], row_nums[-1] + 1)):
            return f"=SUM({col}{row_nums[0]}:{col}{row_nums[-1]})"
        else:
            return "=" + "+".join(f"{col}{r}" for r, _ in child_rows)
    
    if len(child_rows) == 1:
        r, w = child_rows[0]
        return f"={col}{r}" if w == 1.0 else f"=-{col}{r}"
    
    # Mixed weights: build explicit formula with clean sign handling
    parts = []
    for r, w in child_rows:
        sign = "+" if w == 1.0 else "-"
        parts.append(f"{sign}{col}{r}")
    
    return "=" + "".join(parts).lstrip("+")
```

**Examples:**
- Revenue = Products + Services → `=SUM(E5:E6)` (contiguous, all +1)
- Gross Profit = Revenue - COGS → `=E4-E7` (mixed weights)
- Net Income = EBT - Tax → `=E15-E16` (mixed weights)
- Total Assets = TCA + TNCA → `=SUM(E4:E20)` or `=E4+E15` (all +1, maybe non-contiguous)

#### Replace `_render_sheet_body()` — Two-Pass Architecture

**Delete the old function (lines 25-59) and replace with:**

```python
def _render_sheet_body(tree, periods, start_row, global_role_map, sheet_name):
    """Render a tree into rows. Leaves get values, parents get formulas.
    
    Uses two-pass rendering:
      Pass 1: Assign row numbers to every node (layout)
      Pass 2: Build rows using the layout (render)
    """
    # --- Pass 1: Layout ---
    # Assign a row number to each node in tree-display order (parent before children).
    layout = []  # [(row_num, indent, node)]
    
    def _assign_rows(node, indent=0):
        row_num = start_row + len(layout)
        layout.append((row_num, indent, node))
        for child in node.children:
            _assign_rows(child, indent + 1)
    
    _assign_rows(tree)
    
    # Build a lookup: node identity → row_num (for formula references)
    node_row = {id(entry[2]): entry[0] for entry in layout}
    
    # --- Pass 2: Render ---
    rows = []
    for row_num, indent, node in layout:
        label = ("  " * indent) + node.name
        
        # Record role → (sheet_name, row_num)
        if node.role:
            global_role_map[node.role] = (sheet_name, row_num)
        
        row = ["", "", label, ""]
        
        if not node.children:
            # Leaf: hardcoded historical values
            for p in periods:
                val = node.values.get(p, 0)
                row.append(round(val) if val else "")
        else:
            # Parent: formula referencing children's rows
            child_rows = [(node_row[id(c)], c.weight) for c in node.children]
            for i in range(len(periods)):
                col = dcol(i)
                row.append(_build_weight_formula(col, child_rows))
        
        rows.append(row)
    
    return rows
```

**Why two passes?** The layout pass is purely functional (no mutation, no mutable counters). The render pass reads from a complete layout, so every node already knows its row number and its children's row numbers. No placeholders, no back-patching, no off-by-one risk.

**Key differences from the old code:**
1. No `current_row = [start_row]` mutable counter
2. No `isinstance(node, dict)` branch — only `TreeNode` objects
3. Parents generate formulas via `_build_weight_formula()`, not hardcoded values
4. `node_row` dict enables instant lookup of any child's row number

#### New helper: `prev_period()`

```python
def prev_period(p: str, periods: list[str]) -> str | None:
    """Return the period immediately before p in the sorted periods list, or None."""
    idx = periods.index(p)
    return periods[idx - 1] if idx > 0 else None
```

#### CF cash proof rows

`CF_ENDC` comes from the XBRL facts dict (instant context), not from the CF tree. Currently it's not rendered in any sheet tab, so the Cash Link formula has nothing to reference.

**The FX Impact problem:** For multinational companies, the cash flow identity is NOT simply `ENDC = BEGC + NETCH`. The correct formula is:

```
Ending Cash = Beginning Cash + Net Change + Effect of Exchange Rate Changes
```

**Cash proof rows — add after CF tree body rendering in `write_sheets()`:**

```python
    # --- CF tab ---
    cf_tree = trees.get("CF")
    if cf_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_sheet_body(cf_tree, periods, start_row=len(header_rows)+1,
                                        global_role_map=global_role_map, sheet_name="CF")
        
        # --- CF Cash Proof Rows ---
        current_row = len(header_rows) + len(body_rows) + 1
        
        # Blank separator
        body_rows.append([""] * (4 + len(periods)))
        current_row += 1
        
        # Beginning Cash row (hardcoded from XBRL facts)
        cf_endc_values = trees.get("cf_endc_values", {})
        begc_row_num = current_row
        begc_row = ["", "", "Beginning Cash", ""]
        for p in periods:
            prev_p = prev_period(p, periods)
            begc_row.append(round(cf_endc_values.get(prev_p, 0)) if prev_p else "")
        body_rows.append(begc_row)
        global_role_map["CF_BEGC"] = ("CF", begc_row_num)
        current_row += 1
        
        # Net Change row = reference CF tree root (CF_NETCH, already a formula)
        netch_row_num = current_row
        netch_ref = global_role_map.get("CF_NETCH")
        netch_row = ["", "", "Net Change in Cash", ""]
        for i in range(len(periods)):
            col = dcol(i)
            if netch_ref:
                netch_row.append(f"={col}{netch_ref[1]}")
            else:
                netch_row.append("")
        body_rows.append(netch_row)
        current_row += 1
        
        # FX Impact row
        fx_ref = global_role_map.get("CF_FX")
        cf_fx_values = trees.get("cf_fx_values")  # fallback from facts dict
        fx_row_num = current_row
        fx_row = ["", "", "FX Impact", ""]
        for i in range(len(periods)):
            col = dcol(i)
            if fx_ref:
                fx_row.append(f"={col}{fx_ref[1]}")
            elif cf_fx_values:
                fx_row.append(round(cf_fx_values.get(periods[i], 0)))
            else:
                fx_row.append(0)
        body_rows.append(fx_row)
        global_role_map["CF_FX_PROOF"] = ("CF", fx_row_num)
        current_row += 1
        
        # Ending Cash row = BEGC + NETCH + FX (formula)
        endc_row_num = current_row
        endc_row = ["", "", "Ending Cash", ""]
        for i in range(len(periods)):
            col = dcol(i)
            endc_row.append(f"={col}{begc_row_num}+{col}{netch_row_num}+{col}{fx_row_num}")
        body_rows.append(endc_row)
        global_role_map["CF_ENDC"] = ("CF", endc_row_num)
        
        cf_rows = header_rows + body_rows
        _write_sheet_tab(sid, "CF", cf_rows, periods, None, global_role_map)
```

#### In-line Check Rows — Complete Invariant Catalog

Every section of every tab gets a "Check" row immediately after its computed total. The check formula verifies the computed value against its source. All check values should evaluate to 0 (displayed as `-` with the `0.0x;(0.0x);-` format).

**Helper function:**

```python
def _add_check_row(rows, periods, formula_fn, global_role_map):
    """Append a Check row with formulas. Label is always 'Check'."""
    row = ["", "", "Check", ""]
    for i in range(len(periods)):
        col = dcol(i)
        f = formula_fn(col)
        row.append(f if f else "")
    rows.append(row)
```

**`_cell_ref()` — extract to module level (currently nested inside `_write_summary_tab`):**

```python
def _cell_ref(role, col, global_role_map):
    """Build a cell reference from the global role map. Returns '0' if role not found."""
    entry = global_role_map.get(role)
    if not entry:
        return "0"
    sheet_name, row_num = entry
    return f"'{sheet_name}'!{col}{row_num}"
```

##### IS Tab — 2 Check rows

| After row | Check formula | What it validates |
|-----------|--------------|-------------------|
| Total Revenue | `=computed_revenue - IS_tree_root` | Re-summed revenue segments match the XBRL IS tree root |
| Total COGS | `=computed_cogs - COGS_tree_root` | Re-summed COGS components match the XBRL COGS subtotal |

```python
# IS Check after Revenue
def is_revenue_check(col):
    computed = _cell_ref("IS_COMPUTED_REVENUE", col, global_role_map)
    tree = _cell_ref("IS_REVENUE", col, global_role_map)
    if computed != "0" and tree != "0":
        return f"={computed}-{tree}"

# IS Check after COGS
def is_cogs_check(col):
    computed = _cell_ref("IS_COMPUTED_COGS", col, global_role_map)
    tree = _cell_ref("IS_COGS", col, global_role_map)
    if computed != "0" and tree != "0":
        return f"={computed}-{tree}"
```

##### BS Tab — 4 Check rows

| After row | Check formula | What it validates |
|-----------|--------------|-------------------|
| Total Assets | `=summ_TA - BS_tree_TA` | Summary's asset rollup matches BS tab tree total |
| Total Liabilities | `=summ_TL - BS_tree_TL` | Summary's liability rollup matches BS tab tree total |
| Total L&E (BS Balance) | `=TL_plus_TE - TA` | Accounting identity: Total Liabilities + Equity = Total Assets |
| Total Stockholders' Equity | `=computed_TE - tree_TE` | Equity rollforward (stock + AOCI + RE) matches XBRL tree equity |

```python
# BS Balance check: (Total Liabilities + Total Equity) - Total Assets = 0
def bs_balance_check(col):
    tle = _cell_ref("BS_TLE", col, global_role_map)
    ta = _cell_ref("BS_TA", col, global_role_map)
    if tle != "0" and ta != "0":
        return f"={tle}-{ta}"

# BS Equity rollforward check
def bs_equity_check(col):
    computed_te = _cell_ref("BS_COMPUTED_TE", col, global_role_map)
    tree_te = _cell_ref("BS_TE", col, global_role_map)
    if computed_te != "0" and tree_te != "0":
        return f"={computed_te}-{tree_te}"
```

##### CF Tab — 3 Check rows

| After row | Check formula | What it validates |
|-----------|--------------|-------------------|
| Net cash from operations (OPCF) | `=computed_OPCF - tree_OPCF` | Re-summed operating items match XBRL OPCF |
| Net cash from investing (INVCF) | `=computed_INVCF - tree_INVCF` | Re-summed investing items match XBRL INVCF |
| Net cash from financing (FINCF) | `=computed_FINCF - tree_FINCF` | Re-summed financing items match XBRL FINCF |

```python
def cf_opcf_check(col):
    computed = _cell_ref("CF_COMPUTED_OPCF", col, global_role_map)
    tree = _cell_ref("CF_OPCF", col, global_role_map)
    if computed != "0" and tree != "0":
        return f"={computed}-{tree}"

# Same pattern for INVCF and FINCF
```

##### 3-Statement Summary — 5 Check rows

| After row | Check formula | What it validates |
|-----------|--------------|-------------------|
| Total Assets | `=summ_TA - 'BS'!TA_row` | Summary's asset rollup matches BS tab |
| Total Liabilities | `=summ_TL - 'BS'!TL_row` | Summary's liability rollup matches BS tab |
| Total L&E | `=summ_TLE - summ_TA` | BS balance on the summary tab |
| Cash from operations | `=summ_OPCF - 'CF'!OPCF_row` | Summary's OPCF decomposition matches CF tab total |
| Ending Cash | `=BEGC - ENDC + NETCH` | Cash proof: beginning cash + net change = ending cash |

```python
# Summary: OPCF decomposition check
def summ_opcf_check(col):
    summ_opcf = _cell_ref("SUMM_OPCF", col, global_role_map)
    cf_opcf = _cell_ref("CF_OPCF", col, global_role_map)
    if summ_opcf != "0" and cf_opcf != "0":
        return f"={summ_opcf}-{cf_opcf}"

# Summary: CF cash proof
def summ_cash_proof(col):
    begc = _cell_ref("SUMM_BEGC", col, global_role_map)
    endc = _cell_ref("SUMM_ENDC", col, global_role_map)
    netch = _cell_ref("SUMM_NETCH", col, global_role_map)
    if begc != "0" and endc != "0" and netch != "0":
        return f"={begc}-{endc}+{netch}"
```

##### Total Invariant Count: 14 Check rows

| Tab | Count | Checks |
|-----|-------|--------|
| IS | 2 | Revenue, COGS |
| BS | 4 | Total Assets, Total Liabilities, BS Balance (TL+TE=TA), Equity rollforward |
| CF | 3 | OPCF, INVCF, FINCF |
| 3-Statement Summary | 5 | Total Assets, Total Liabilities, BS Balance, OPCF decomposition, Cash proof |
| **Total** | **14** | |

##### Role map requirements

For all 14 checks to work, `global_role_map` must track these roles:

**Already exist from Phase 2:** `BS_TA`, `BS_TL`, `BS_TE`, `INC_NET`, `INC_NET_CF`, `CF_OPCF`, `CF_INVCF`, `CF_FINCF`, `CF_NETCH`

**New roles needed in Phase 3:**

| Role | Where registered | What it references |
|------|------------------|--------------------|
| `IS_DA` / `CF_DA` | `_tag_da_sbc_nodes()` in `xbrl_tree.py` | D&A leaf nodes |
| `IS_SBC` / `CF_SBC` | `_tag_da_sbc_nodes()` in `xbrl_tree.py` | SBC leaf nodes |
| `CF_FX` | `_tag_cf_positions()` in `xbrl_tree.py` | FX impact node |
| `IS_REVENUE` / `IS_COGS` | `_render_sheet_body()` during Pass 2 | IS tree section roots |
| `IS_COMPUTED_REVENUE` / `IS_COMPUTED_COGS` | Check row injection in `write_sheets()` | Re-summed section totals |
| `BS_TLE` / `BS_COMPUTED_TE` | Check row injection in `write_sheets()` | L&E total, equity rollforward |
| `CF_COMPUTED_OPCF` / `CF_COMPUTED_INVCF` / `CF_COMPUTED_FINCF` | Check row injection in `write_sheets()` | Re-summed CF section totals |
| `CF_BEGC` / `CF_FX_PROOF` / `CF_ENDC` | CF cash proof rows in `write_sheets()` | Cash proof components |
| `SUMM_TA` / `SUMM_TL` / `SUMM_TLE` / `SUMM_OPCF` / `SUMM_BEGC` / `SUMM_ENDC` / `SUMM_NETCH` | Summary tab rendering | Summary-level references |

---

## What Gets Deleted

Nothing. Phase 3 is purely additive — it replaces static values with formulas in `sheet_builder.py` and adds D&A/SBC role tags in `xbrl_tree.py`. The old `_write_summary_tab()` is replaced by the new 3-Statement Summary tab with in-line checks.

---

## Formatting

Formatting (number formats, text styling, column widths) is specified in **[Phase 3a](pipeline_phase3a_spec.md)**. Phase 3 writes correct data and formulas; Phase 3a makes it look right.

Phase 3 should still apply basic column widths (A-B: 50px spacer, C: 200px label, D: spacer, E+: data) so the sheet is usable during development. Phase 3a will refine these and add all number/text formatting.

---

## Testing

### Testing Philosophy

The financial model is already structured correctly — `reconcile_trees()` and `verify_model()` prove the math in Python. Phase 3 is about **replicating that same model in the sheet using formulas**. The tests verify that:

1. The sheet formula structure mirrors the XBRL tree structure
2. Evaluating the formulas on the actual values produces the same numbers `verify_model()` already verified
3. The row structure is correct (prefix columns, indentation, separators)

### `tests/test_sheet_formulas.py`

Tests the formula generation functions directly. No Google Sheets API — tests formula strings, row structure, and value correctness against the Apple fixture.

**Formula generation tests:**

| Test | Input | Expected Output |
|------|-------|-----------------|
| `test_build_weight_formula_all_positive_contiguous` | `[(5, 1.0), (6, 1.0), (7, 1.0)]` | `=SUM(E5:E7)` |
| `test_build_weight_formula_all_positive_noncontiguous` | `[(5, 1.0), (8, 1.0)]` | `=E5+E8` |
| `test_build_weight_formula_mixed_weights` | `[(4, 1.0), (7, -1.0)]` | `=E4-E7` |
| `test_build_weight_formula_single_child` | `[(5, 1.0)]` | `=E5` |
| `test_build_weight_formula_empty` | `[]` | `""` |

**Tree-to-row tests (using Apple fixture):**

| Test | What it checks |
|------|---------------|
| `test_leaf_cells_are_numbers` | Every leaf node row has `int` or `float` values in data columns, not strings starting with `=` |
| `test_parent_cells_are_formulas` | Every parent node row has strings starting with `=` in data columns |
| `test_formula_evaluates_to_tree_value` | For each parent: parse its formula, substitute child cell values, evaluate — result must equal `node.values[period]` within 0.5 |
| `test_row_count_matches_tree_nodes` | Total rows emitted by `_render_sheet_body()` equals total node count in tree (every node gets exactly one row) |

**Formatting preservation tests:**

| Test | What it checks |
|------|---------------|
| `test_row_format_four_prefix_columns` | Every row starts with `["", "", label, ""]` — first 4 elements |
| `test_header_row_format` | Second row is `["", "", "$m", ""] + periods` |
| `test_indentation_matches_depth` | Label for depth-N node starts with `"  " * N` (two spaces per level) |
| `test_separator_rows_unchanged` | Empty rows between sections are `[""] * (4 + num_periods)` |
| `test_data_columns_start_at_E` | Data values/formulas begin at index 4 (column E) |

**Invariant formula tests (14 Check rows across all tabs):**

| Test | What it checks |
|------|---------------|
| `test_all_check_rows_are_formulas` | All 14 `Check` rows contain strings starting with `=`, not "n/a" or `=0` or numbers |
| `test_is_revenue_check` | IS Revenue Check formula references IS_COMPUTED_REVENUE and IS_REVENUE rows |
| `test_is_cogs_check` | IS COGS Check formula references IS_COMPUTED_COGS and IS_COGS rows |
| `test_bs_total_assets_check` | BS Total Assets Check formula references SUMM_TA and BS_TA rows |
| `test_bs_total_liabilities_check` | BS Total Liabilities Check references SUMM_TL and BS_TL rows |
| `test_bs_balance_check` | BS Balance Check formula: `=TLE - TA` (accounting identity) |
| `test_bs_equity_rollforward_check` | BS Equity Check: computed TE (stock+AOCI+RE) vs tree TE |
| `test_cf_opcf_check` | CF OPCF Check references CF_COMPUTED_OPCF and CF_OPCF rows |
| `test_cf_invcf_check` | CF INVCF Check references CF_COMPUTED_INVCF and CF_INVCF rows |
| `test_cf_fincf_check` | CF FINCF Check references CF_COMPUTED_FINCF and CF_FINCF rows |
| `test_summ_total_assets_check` | Summary TA Check references SUMM_TA and BS tab TA row |
| `test_summ_total_liabilities_check` | Summary TL Check references SUMM_TL and BS tab TL row |
| `test_summ_bs_balance_check` | Summary BS Balance: TLE - TA on summary tab |
| `test_summ_opcf_decomposition_check` | Summary OPCF Check: re-summed NI+D&A+SBC+NWC+tax+other vs CF tab OPCF |
| `test_summ_cash_proof_check` | Summary Cash Proof: BEGC - ENDC + NETCH = 0 |
| `test_check_rows_render_zero_as_dash` | Check rows use `0.0x;(0.0x);-` format (0 renders as `-`) |

**CF cash proof tests:**

| Test | What it checks |
|------|---------------|
| `test_cf_begc_is_hardcoded` | Beginning Cash row contains numbers (from XBRL facts), not formulas |
| `test_cf_netch_references_tree_root` | Net Change row formula references the CF tree root row (CF_NETCH) |
| `test_cf_fx_row_present_for_multinational` | FX Impact row exists when CF_FX node is tagged; references the FX node's row |
| `test_cf_fx_row_zero_when_no_fx` | FX Impact row contains 0 when no FX node exists (domestic company) |
| `test_cf_endc_is_formula` | Ending Cash row = `=BEGC_cell + NETCH_cell + FX_cell` |
| `test_cf_cash_proof_values_match_facts` | BEGC values match XBRL facts; ENDC = BEGC + NETCH + FX for each period |

**Two-pass rendering tests:**

| Test | What it checks |
|------|---------------|
| `test_row_count_matches_tree_nodes` | Total rows emitted by `_render_sheet_body()` equals total node count in tree |
| `test_two_pass_row_order_matches_tree_order` | Rows appear in tree display order: parent, then children depth-first |
| `test_no_mutable_counter_in_render` | Layout pass produces the same row assignments regardless of tree size |

### `tests/test_da_sbc_tagging.py`

Tests the new D&A/SBC node tagging in `reconcile_trees()`. Uses Apple fixture.

| Test | What it checks |
|------|---------------|
| `test_is_da_tagged` | IS tree has a leaf with role `IS_DA` whose name contains "depreciation" |
| `test_cf_da_tagged` | CF tree has a leaf with role `CF_DA` |
| `test_da_values_match_across_statements` | `IS_DA.values[p] == CF_DA.values[p]` for all complete periods (within 0.5) |
| `test_is_sbc_tagged` | IS tree has a leaf with role `IS_SBC` whose name contains "stock" or "share" |
| `test_cf_sbc_tagged` | CF tree has a leaf with role `CF_SBC` |
| `test_sbc_values_match_across_statements` | `IS_SBC.values[p] == CF_SBC.values[p]` for all complete periods (within 0.5) |
| `test_tagging_does_not_break_existing_roles` | BS_TA, BS_TL, BS_TE, INC_NET, INC_NET_CF, CF_OPCF, CF_NETCH etc. still tagged correctly |
| `test_timeseries_match_rejects_single_period_collision` | A CF node that matches IS D&A value for one period but differs in other periods is NOT tagged |
| `test_cf_fx_tagged_when_present` | CF tree has a node with role `CF_FX` when EffectOfExchangeRate concept exists |

### Running Tests

```bash
# All tests
pytest tests/

# Phase 3: sheet formulas
pytest tests/test_sheet_formulas.py

# Phase 3: D&A/SBC tagging
pytest tests/test_da_sbc_tagging.py

# Regression: historical invariants still pass
pytest tests/test_model_historical.py
```

---

## Implementation Plan (Dependency Order)

```
Step 1: xbrl_tree.py (D&A/SBC/FX tagging)
   ↓
Step 2: sheet_builder.py (_build_weight_formula)     ← no dependencies on Step 1
   ↓
Step 3: sheet_builder.py (_render_sheet_body rewrite) ← depends on Step 2
   ↓
Step 4: sheet_builder.py (CF cash proof rows)         ← depends on Steps 1 + 3
   ↓
Step 5: sheet_builder.py (14 Check rows)              ← depends on Steps 1 + 3 + 4
   ↓
Step 6: End-to-end verification                       ← depends on all
```

Formatting (number formats, text styling) is deferred to **Phase 3a**.

**Steps 1 and 2 can be done in parallel** — they have no dependencies on each other.

### Step 1: Tag D&A, SBC, and FX nodes in `xbrl_tree.py`

**Goal:** Give `sheet_builder.py` cell references for D&A/SBC Check rows + FX cash proof.

1. Add `_find_leaf_by_keywords()` and `_find_leaf_by_timeseries()` — place them above `_tag_cf_positions()` (around line 475).
2. Add `_tag_da_sbc_nodes()` — place it after `_find_leaf_by_timeseries()`.
3. Add FX tagging to `_tag_cf_positions()` — insert after line 524 (`_walk_and_tag(cf_tree)`).
4. Add Step F call to `reconcile_trees()` — insert before `return trees` on line 682.
5. Create `tests/test_da_sbc_tagging.py`.

### Step 2: Implement `_build_weight_formula()` in `sheet_builder.py`

**Goal:** Translate XBRL tree weights into sheet formula strings.

1. Add `_build_weight_formula()` after `dcol()` (after line 23).
2. Write the 5 unit tests for it in `tests/test_sheet_formulas.py`.

### Step 3: Rewrite `_render_sheet_body()` with two-pass architecture

**Goal:** Parent cells become formulas.

1. Delete lines 25-59 (old `_render_sheet_body`).
2. Insert the new two-pass version in its place.
3. Add `prev_period()` helper.
4. Write tree-to-row unit tests.

### Step 4: Add CF cash proof rows

**Goal:** Cash Link invariant has real cell references.

1. Modify the CF tab section of `write_sheets()` (currently lines 149-154).
2. Add BEGC, NETCH, FX, ENDC rows after the tree body.
3. Register roles in `global_role_map`.

### Step 5: Inject 14 In-Line Check Rows

**Goal:** All 14 invariant checks rendered in-line.

1. Extract `_cell_ref()` from `_write_summary_tab()` to module level.
2. Add `_add_check_row()` helper.
3. Define the 14 formula functions.
4. Inject check rows after each section in `write_sheets()`.
5. Replace `_write_summary_tab()` with the new 3-Statement Summary tab.

### Step 6: End-to-end verification

1. Run `sheet_builder.py` on Apple trees.
2. Verify parent cells are formulas, all 14 Check rows show `-`.
3. Test on KO, BAC for cross-industry validation.

---

## Success Criteria

1. **Every parent cell is a formula.** No parent node in IS, BS, or CF has a hardcoded value.
2. **Every leaf cell is a hardcoded value.** Leaf nodes contain the actual XBRL data.
3. **All 14 Check rows show `-` (zero)** in-line after each subtotal for every historical period.
4. **No "n/a" or `=0` placeholders** in Check row formulas.
5. **CF cash proof rows exist** with BEGC (value), NETCH (formula), FX (formula or value), ENDC (formula).
6. **FX Impact handled.** Multinational companies have a non-zero FX row; domestic companies show 0.
7. **D&A/SBC tagging uses time-series matching.** Must reject single-period collisions.
8. **Two-pass rendering.** No mutable `current_row[0]` counter.
9. **Clean formula syntax.** No `replace("+-", "-")` hacks.
10. **`pytest tests/test_sheet_formulas.py` passes.**
11. **`pytest tests/test_da_sbc_tagging.py` passes.**
12. **Existing tests still pass.** `pytest tests/test_model_historical.py` unaffected.
13. **Works across industries.** Correct invariants for Apple, KO, PG, and at least one more.
