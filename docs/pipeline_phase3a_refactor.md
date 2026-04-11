# Phase 3a: Architecture Refactor & Layout Adaptability Spec

## Objective
The current Phase 3 pipeline successfully constructs formulas but suffers from tight coupling between layout and data rendering. This leads to circular dependencies, hardcoded line references, and failures when encountering non-standard financial structures (e.g., banks missing COGS, or deeply nested revenue trees like Netflix).

This specification outlines a refactor to make the pipeline **adaptable, dynamic, and layout-agnostic** using a Three-Pass Architecture and Declarative Invariants.

---

## 1. Semantic Breadth-First Search (BFS) Tagging
**File:** `xbrl_tree.py`

**Problem:** The tree tagger assumes concepts like Revenue or COGS sit at specific depths (e.g., first child of Net Income). When companies nest these items deeper (e.g., under "Operating Income"), the tags are missed.

**Solution:** Use a recursive Breadth-First Search (BFS) or deep tree traversal looking for keyword matches (e.g., `"revenue"`, `"cost of sales"`), regardless of where they sit in the XBRL hierarchy.

### Implementation Requirements:
*   Replace depth-based array index assumptions (e.g., `children[0]`) with a `_find_by_kw()` helper.
*   Tag `IS_REVENUE` and `IS_COGS` semantically.
*   Tag `BS_TLE` reliably by ensuring the "Liabilities & Equity" root node is captured, even if synthesized.

---

## 2. Data-Driven Role Aliasing
**File:** `sheet_builder.py`

**Problem:** Mapping tree roots to their "Computed" equivalents relies on repetitive `if/elif` blocks inside the rendering loop.

**Solution:** Define a constant dictionary mapping base roles to their computed alias names. Apply these automatically during the layout pass.

### Implementation Requirements:
Add a top-level constant:
```python
COMPUTED_ROLE_ALIASES = {
    "IS_REVENUE": "IS_COMPUTED_REVENUE",
    "IS_COGS": "IS_COMPUTED_COGS",
    "BS_TE": "BS_COMPUTED_TE",
    "CF_OPCF": "CF_COMPUTED_OPCF",
    "CF_INVCF": "CF_COMPUTED_INVCF",
    "CF_FINCF": "CF_COMPUTED_FINCF",
}
```
During layout processing:
```python
if node.role:
    global_role_map[node.role] = (sheet_name, row_num)
    if node.role in COMPUTED_ROLE_ALIASES:
        global_role_map[COMPUTED_ROLE_ALIASES[node.role]] = (sheet_name, row_num)
```

---

## 3. Three-Pass Rendering Architecture
**File:** `sheet_builder.py`

**Problem:** The script attempts to write formulas referencing the Summary tab before the Summary tab's layout has been calculated, causing circular dependency crashes (unless line numbers are hardcoded).

**Solution:** Separate the "Layout Calculation" from the "Cell Rendering".

### Implementation Requirements:
Redesign `write_sheets()` to operate in exactly three passes:
1.  **Pass 1: Global Layout (Dry Run).** Iterate through IS, BS, CF, and Summary logic. Do *not* generate cell values or formulas. Only calculate the `row_num` for every line item and populate `global_role_map` globally.
2.  **Pass 2: Render Cells.** Iterate through the tabs again. Since `global_role_map` is fully populated, any formula on any tab can reference any other tab without `#REF!` or `KeyError` issues.
3.  **Pass 3: API Write.** Send the batched rows and formatting to Google Sheets.

*Note: This completely eliminates the need to pre-register hardcoded indices like `global_role_map["SUMM_TA"] = ("Summary", 4)`.*

---

## 4. Declarative Invariant Rules
**File:** `sheet_builder.py`

**Problem:** There are 14 separate python closures representing checks (e.g., `is_revenue_check()`), and 14 hardcoded injection points. If a company lacks a section (e.g., Bank of America lacks COGS), the injection fails or throws an error.

**Solution:** Define all checks as a single declarative array of dictionaries. The injector will loop over these rules and gracefully skip checks if a company's layout doesn't support them.

### Implementation Requirements:
Define the invariants at the module level:
```python
INVARIANT_CHECKS = [
    {"tab": "IS", "label": "Revenue Check", "role_1": "IS_COMPUTED_REVENUE", "role_2": "IS_REVENUE"},
    {"tab": "IS", "label": "COGS Check", "role_1": "IS_COMPUTED_COGS", "role_2": "IS_COGS"},
    {"tab": "BS", "label": "Total Assets Check", "role_1": "SUMM_TA", "role_2": "BS_TA"},
    {"tab": "BS", "label": "Total Liab Check", "role_1": "SUMM_TL", "role_2": "BS_TL"},
    # ... Remaining 10 checks
]
```

Create a single dynamic injection function:
```python
def inject_checks_for_tab(tab_name, rows, periods, global_role_map):
    for check in INVARIANT_CHECKS:
        if check["tab"] == tab_name:
            r1 = check["role_1"]
            r2 = check["role_2"]
            
            # ADAPTABILITY: Gracefully skip if this company's layout lacks these roles
            if r1 not in global_role_map or r2 not in global_role_map:
                continue 
            
            row = ["", "", check["label"], ""]
            for i in range(len(periods)):
                col = dcol(i)
                ref1 = _cell_ref(r1, col, global_role_map)
                ref2 = _cell_ref(r2, col, global_role_map)
                row.append(f"={ref1}-{ref2}")
            rows.append(row)
```
Replace the hardcoded `_add_check_row()` injections with a call to `inject_checks_for_tab()` at the end of each section's rendering loop.

---

## 5. Pass Criteria
1. **Layout Agnostic:** The pipeline must generate successfully for Apple (Standard Tech), Netflix (Nested Revenue), and Bank of America (No COGS).
2. **No Hardcoded Indices:** All instances of manual row assignments (e.g., `"Summary", 4`) must be removed from the codebase.
3. **Graceful Degradation:** Check rows must only appear if the corresponding sections exist in the company's financial filings.
4. **Automated Tests:** All unit tests (`pytest tests/ -v`) must continue to pass, specifically the formula and tagging verifications.