# Phase 3e: Dual-Linkbase Architecture (Cal Structure + Pre Ordering + Cascade Rendering)

## Background & Motivation

Phase 3d proposed using the Presentation Linkbase (`_pre.xml`) as the **primary** tree structure. This was rejected because `_pre.xml` defines a flat display hierarchy (all line items under abstract headers) — not the mathematical parent-child relationships needed for sheet formulas. Using pre as primary would produce `=SUM(all items under IncomeStatementAbstract)`, which is meaningless.

Instead, Phase 3e uses a **dual-linkbase** approach:

| Linkbase | Role | What it provides |
|----------|------|-----------------|
| `_cal.xml` (Calculation) | **Primary** — tree structure | Parent-child math, weights (+1/-1), formulas |
| `_pre.xml` (Presentation) | **Secondary** — ordering | Sibling display order, top-down sequence |

Neither linkbase is sufficient alone:
- **Cal alone**: IS tree is upside-down (NetIncomeLoss at root, Revenue buried 3 levels deep). Sibling order is arbitrary.
- **Pre alone**: Flat list under abstract headers. No math relationships, no weights, no formulas.

**Combined**: Cal provides the formula tree. Pre provides the display order. A cascade algorithm flips the IS tree from bottom-up to top-down for rendering.

See `docs/xbrl_linkbases.md` for the full explanation of how these linkbases work together.

---

## Scope & Impact

- **Files modified**: `xbrl_tree.py` (primary), `sheet_builder.py` (rendering), `pymodel.py` (validation), `run_pipeline.py` (gate)
- **Subsumes**: Phase 3c (tree integrity, orphan facts, three-pass rendering, declarative checks) + Phase 3d (presentation ordering)
- **Result**: Deterministic, company-specific Excel models that match each filer's 10-K/20-F line-for-line, with working formulas and verified invariants

---

## Architecture

```
XBRL Filing HTML
       │
       ├─► fetch_cal_linkbase()              Existing — get _cal.xml
       ├─► fetch_pre_linkbase()              NEW — get _pre.xml
       │
       ├─► parse_cal_linkbase()              Existing — build math trees with weights
       ├─► parse_pre_linkbase()              NEW — build presentation order index
       │
       ├─► build_statement_trees()           Build IS/BS/CF trees from cal linkbase
       │         │
       │         ├─► sort_by_presentation()        NEW — reorder siblings by _pre.xml
       │         ├─► _tag_bs_positions()            Existing
       │         ├─► _tag_cf_positions()            Existing
       │         ├─► _tag_is_positions()            FIXED — check root first, never overwrite values
       │         ├─► _tag_is_semantic()             NEW — BFS tag IS_REVENUE, IS_COGS
       │         ├─► _override_bs_cash()            Existing
       │         ├─► _supplement_orphan_facts()     NEW — fill gaps from orphan XBRL facts
       │         ├─► _filter_to_complete_periods()  Existing
       │         └─► _tag_da_sbc_nodes()            Existing
       │
       ├─► verify_tree_completeness()        NEW — parent == SUM(children * weight)?
       │         └─► FAIL → stop, report gaps
       │
       ├─► verify_model()                   Existing — 5 cross-statement invariants
       │         └─► FAIL → stop, report errors
       │
       └─► sheet_builder.py                 Write sheet (only if both gates pass)
                 │
                 │  (three labeled blocks inside write_sheets(), not separate functions)
                 ├─► Pass 1: Layout — IS via cascade_layout(), BS/CF via existing _assign_rows()
                 ├─► Pass 2: Render — formulas using complete global_role_map
                 └─► Pass 3: Write — single API call
```

---

## What Changes

### 1. Fetch and parse `_pre.xml` (`xbrl_tree.py`)

Add `fetch_pre_linkbase()` alongside the existing `fetch_cal_linkbase()`. Both find their respective linkbase files from the schema reference in the filing HTML.

```python
def fetch_pre_linkbase(html: str, base_url: str) -> str | None:
    """Find and fetch the presentation linkbase referenced by the filing."""
    schema_pat = re.compile(r'schemaRef[^>]*href="([^"]+)"', re.IGNORECASE)
    m = schema_pat.search(html)
    if not m:
        return None

    schema_href = m.group(1)
    if not schema_href.startswith('http'):
        schema_href = base_url + schema_href

    schema = fetch_url(schema_href).decode('utf-8', errors='replace')

    # Look for _pre.xml linkbase reference
    pre_pat = re.compile(r'href="([^"]*_pre\.xml)"', re.IGNORECASE)
    m = pre_pat.search(schema)
    if not m:
        return None

    pre_href = m.group(1)
    if not pre_href.startswith('http'):
        pre_href = base_url + pre_href

    return fetch_url(pre_href).decode('utf-8', errors='replace')
```

Parse into a concept → position index, grouped by role:

```python
def parse_pre_linkbase(pre_xml: str) -> dict[str, dict[str, float]]:
    """Parse presentation linkbase into {role: {concept: order_position}}.

    Returns a dict mapping each role URL to a concept→position dict,
    where position is the `order` attribute from presentationArc.
    Concepts are normalized to match calc linkbase format (prefix_LocalName).
    """
    role_orders = {}
    current_role = None
    position_counter = 0  # fallback when order attr missing

    for line in pre_xml.split('\n'):
        # Detect role
        role_match = re.search(r'xlink:role="([^"]+)"', line)
        if role_match and 'presentationLink' in line:
            current_role = role_match.group(1)
            role_orders[current_role] = {}
            position_counter = 0

        # Parse presentationArc
        if 'presentationArc' in line and current_role:
            to_match = re.search(r'xlink:to="([^"]+)"', line)
            order_match = re.search(r'order="([^"]+)"', line)

            if to_match:
                concept = to_match.group(1)
                order = float(order_match.group(1)) if order_match else position_counter
                role_orders[current_role][concept] = order
                position_counter += 1

    return role_orders


def build_presentation_index(role_orders: dict, role_url: str) -> dict[str, float]:
    """Get flat concept→position map for a specific statement role.

    Matches the role_url against available presentation roles using the
    same role classification logic as the calc linkbase.
    """
    for role, concepts in role_orders.items():
        if role_url in role or role in role_url:
            return concepts

    # Fuzzy match: find presentation role with the same terminal segment
    cal_segment = role_url.rsplit('/', 1)[-1].upper()
    for role, concepts in role_orders.items():
        pre_segment = role.rsplit('/', 1)[-1].upper()
        if cal_segment == pre_segment:
            return concepts

    return {}
```

### 2. Sort calc tree children by presentation order (`xbrl_tree.py`)

After building the calc tree, sort each node's children by their position in the presentation index. This fixes the arbitrary sibling order from `_cal.xml`.

```python
def sort_by_presentation(node: TreeNode, pres_index: dict[str, float]):
    """Recursively sort each node's children by presentation order.

    Children not found in pres_index sort to the end (order=999).
    """
    if node.children:
        node.children.sort(key=lambda c: pres_index.get(c.concept, 999))
        for child in node.children:
            sort_by_presentation(child, pres_index)
```

This runs once during `build_statement_trees()`, after the calc tree is built but before tagging.

### 3. Cascade rendering for IS (`sheet_builder.py`)

The IS calc tree is bottom-up: NetIncomeLoss at root, Revenue buried 3 levels deep. Analysts expect top-down: Revenue first, Net Income last.

The cascade algorithm walks the IS tree's "additive backbone" (the chain of +1-weight children that themselves have children), rendering each level's expenses before its subtotal. This produces the correct top-down display without altering the tree structure.

```python
def cascade_layout(node: TreeNode, start_row: int, indent: int,
                   global_role_map: dict, sheet_name: str,
                   periods: list) -> list[tuple]:
    """Layout IS tree top-down via cascade rendering.

    At each node with children:
    1. Find the 'backbone child' — the +1 weight child that itself has children.
       This is the next level up in the income statement (NI→EBT→OpInc→...).
    2. Recurse into the backbone first — this unwinds to Revenue.
    3. Render expense children (weight -1, and +1 leaves) indented.
    4. Render this node LAST as the cascade subtotal.

    Returns: [(row_num, indent, node, is_subtotal), ...]
    """
    rows = []

    if not node.children:
        # Leaf node — just render it
        if node.role:
            global_role_map[node.role] = (sheet_name, start_row)
        return [(start_row, indent, node, False)]

    # Find backbone child: +1 weight, has its own children
    backbone = None
    expense_children = []
    for child in node.children:
        if child.weight == 1.0 and child.children and backbone is None:
            backbone = child
        else:
            expense_children.append(child)

    current_row = start_row

    if backbone:
        # Recurse into backbone FIRST — unwinds toward Revenue
        backbone_rows = cascade_layout(
            backbone, current_row, indent,
            global_role_map, sheet_name, periods
        )
        rows.extend(backbone_rows)
        current_row = backbone_rows[-1][0] + 1

        # Render expense children (indented under the subtotal)
        for child in expense_children:
            if child.children:
                # Nested subtree — recurse with existing pre-order walk
                sub_rows = _assign_rows_layout(
                    child, current_row, indent + 1,
                    global_role_map, sheet_name
                )
            else:
                # Leaf expense
                sub_rows = [(current_row, indent + 1, child, False)]
                if child.role:
                    global_role_map[child.role] = (sheet_name, current_row)
            rows.extend(sub_rows)
            current_row = sub_rows[-1][0] + 1
    else:
        # Top of cascade (no backbone) — render +1 children, then -1 children
        plus_children = [c for c in node.children if c.weight == 1.0]
        minus_children = [c for c in node.children if c.weight != 1.0]

        for child in plus_children + minus_children:
            if child.children:
                sub_rows = _assign_rows_layout(
                    child, current_row, indent + 1,
                    global_role_map, sheet_name
                )
            else:
                sub_rows = [(current_row, indent + 1, child, False)]
                if child.role:
                    global_role_map[child.role] = (sheet_name, current_row)
            rows.extend(sub_rows)
            current_row = sub_rows[-1][0] + 1

    # This node renders LAST as the subtotal
    if node.role:
        global_role_map[node.role] = (sheet_name, current_row)
    rows.append((current_row, indent, node, True))

    return rows


```

**BS/CF do NOT need a new `preorder_layout()` function.** The existing `_assign_rows()` in `_render_sheet_body()` (lines 52-56) already does pre-order traversal (parent first, then children). For BS/CF, keep the existing code path unchanged and only add `cascade_layout()` for IS.

### Which statements use which layout

| Statement | Calc root | Layout method | Why |
|-----------|-----------|--------------|-----|
| IS | NetIncomeLoss (bottom) | `cascade_layout()` (NEW) | Revenue must be at top, NI at bottom |
| BS (Assets) | Assets (top) | `_assign_rows()` (existing) | Already top-down |
| BS (L&E) | LiabilitiesAndEquity (top) | `_assign_rows()` (existing) | Already top-down |
| CF | NetChangeInCash (top) | `_assign_rows()` (existing) | Already top-down (OPCF → INVCF → FINCF → NETCH) |

### 4. Fix `_tag_is_positions` — never overwrite `.values` (`xbrl_tree.py`)

*Unchanged from Phase 3c spec.* Check IS root first (it may itself be NetIncomeLoss), then search children. Never overwrite `.values`.

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

### 5. Semantic BFS tagging for IS_REVENUE and IS_COGS (`xbrl_tree.py`)

*Unchanged from Phase 3c spec.* Finds Revenue and COGS by keyword BFS regardless of tree depth. Banks without COGS gracefully skip.

### 6. Orphan fact supplementation (`xbrl_tree.py`)

*Unchanged from Phase 3c spec.* Fills gaps where `SUM(children * weight) != declared` by inserting real XBRL facts not linked in the calc linkbase. No plugs, no fabrication, no value mutation.

### 7. Tree completeness verification (`pymodel.py`)

*Unchanged from Phase 3c spec.* Checks every parent node: `SUM(children * weight) == declared`. Returns errors if any formula would be wrong. Pipeline gate blocks sheet write on failure.

### 8. Three-pass sheet rendering (`sheet_builder.py`)

*Architecture from Phase 3c, updated to use cascade for IS.*

The three passes are **labeled blocks inside `write_sheets()`**, not separate functions. This keeps the rendering logic in one place and minimizes indirection.

- **IS** uses `cascade_layout()` (new) to produce top-down row ordering from the bottom-up calc tree.
- **BS/CF** use the existing `_assign_rows()` pre-order walk (unchanged).

```python
def write_sheets(trees: dict, company: str) -> tuple[str, str]:
    periods = trees.get("complete_periods", [])
    sid, url, sheet_ids = gws_create(
        f"{company} — Financial Model", ["IS", "BS", "CF", "Summary"]
    )
    global_role_map = {}

    # ── Pass 1: Layout (dry run — populate global_role_map) ────
    # IS: cascade layout (bottom-up tree → top-down display)
    is_tree = trees.get("IS")
    is_layout = cascade_layout(is_tree, ...) if is_tree else []

    # BS/CF: existing _assign_rows pre-order walk (unchanged)
    bs_layout = _assign_rows(trees.get("BS"), ...)
    cf_layout = _assign_rows(trees.get("CF"), ...)

    # Summary: derive from global_role_map
    summ_layout = _layout_summary(periods, global_role_map)

    # global_role_map is now COMPLETE — every role from every tab registered

    # ── Pass 2: Render (formulas using complete role map) ──────
    # Use the complete global_role_map to resolve all cross-tab references.
    # Leaf nodes get values, parent nodes get formulas.
    tab_rows = {}
    for tab_name, layout in [("IS", is_layout), ("BS", bs_layout), ...]:
        tab_rows[tab_name] = _render_from_layout(
            layout, periods, global_role_map, tab_name
        )

    # ── Pass 3: Write (single API call) ───────────────────────
    for tab_name, rows in tab_rows.items():
        gws_write(sid, f"{tab_name}!A1:{dcol(len(periods)-1)}{len(rows)}", rows)

    requests = _build_all_format_requests(tab_rows, sheet_ids, periods, global_role_map)
    gws_batch_update(sid, requests)

    return sid, url
```

### 9. Declarative invariant checks (`sheet_builder.py`)

*Unchanged from Phase 3c spec.* Replace 14 tautological check closures with 5 real cross-statement checks. Skip checks when roles are missing.

### 10. Pipeline gate (`run_pipeline.py`)

*Unchanged from Phase 3c spec.* Block sheet write if tree completeness or cross-statement invariants fail.

---

## Order of Operations in `reconcile_trees()`

```python
def reconcile_trees(trees: dict, pres_index: dict) -> dict:
    facts = trees.get("facts", {})

    # A: Sort all tree children by presentation order
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            sort_by_presentation(tree, pres_index.get(stmt, {}))

    # B: Tag BS positions
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))

    # C: Tag CF positions + find CF_ENDC
    cf_endc_values = _tag_cf_positions(trees.get("CF"), facts)

    # D: Tag IS Net Income (FIXED — check root first, never overwrite values)
    _tag_is_positions(trees.get("IS"), trees.get("CF"))

    # E: Tag IS Revenue and COGS by keyword BFS
    _tag_is_semantic(trees.get("IS"))

    # F: Override BS_CASH with CF_ENDC (only allowed value mutation)
    _override_bs_cash(trees.get("BS"), cf_endc_values)

    # G: Supplement orphan facts
    _supplement_orphan_facts(trees)

    # H: Filter to complete periods
    _filter_to_complete_periods(trees)

    # I: Tag D&A and SBC nodes
    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))

    trees["cf_endc_values"] = cf_endc_values or {}
    return trees
```

---

## How Cascade Rendering Works (IS Only)

The calc linkbase builds the IS tree bottom-up:

```
NetIncomeLoss (root)
  + EBT (w=+1)
    + OperatingIncomeLoss (w=+1)
      + Revenues (w=+1)
      - CostOfRevenue (w=-1)
      - S&M (w=-1)
      - R&D (w=-1)
      - G&A (w=-1)
    - InterestExpense (w=-1)
    + OtherIncome (w=+1)
  - Tax (w=-1)
```

The cascade algorithm finds the **additive backbone** — the chain of +1-weight children that themselves have children:

```
NetIncomeLoss → backbone = EBT (+1, has children)
EBT → backbone = OperatingIncomeLoss (+1, has children)
OperatingIncomeLoss → backbone = none (Revenues is +1 but is a leaf)
```

It recurses into the backbone first, then renders expenses, then the subtotal:

```
Step 1: cascade(NI) → recurse into EBT
Step 2: cascade(EBT) → recurse into OpInc
Step 3: cascade(OpInc) → no backbone, render children:
          Revenue                              row 4
            - Cost of Revenue                  row 5
            - S&M                              row 6
            - R&D                              row 7
            - G&A                              row 8
        → render OpInc as subtotal:
          = Operating Income                   row 9  (=E4-E5-E6-E7-E8)
Step 4: back in cascade(EBT), render expenses:
            - Interest Expense                 row 10
            + Other Income                     row 11
        → render EBT as subtotal:
          = EBT                                row 12 (=E9-E10+E11)
Step 5: back in cascade(NI), render expenses:
            - Tax                              row 13
        → render NI as subtotal:
          = Net Income                         row 14 (=E12-E13)
```

Result matches the NFLX 10-K line-for-line.

**Formulas are correct by construction**: each subtotal row's formula references its children's rows using the weights from `_cal.xml`. The cascade only changes the **row ordering** — the **math** is unchanged.

---

## Industry Portability

This approach works across industries because:

1. **Cal linkbase is standardized**: every filer defines parent-child math relationships with weights. The tree structure is always valid for formulas.
2. **Pre linkbase is company-specific**: each filer defines their own display order. Sorting by pre gives each company's preferred layout.
3. **Cascade is structural, not semantic**: it follows the +1-weight backbone, which exists in any IS tree regardless of industry. Banks (no COGS) just have a shorter cascade. Insurance companies (earned premiums instead of revenue) have the same structure with different labels.
4. **BS and CF are already top-down**: the calc linkbase roots BS at Assets and CF at NetChangeInCash — both already top-down. No cascade needed.
5. **Abstract nodes are excluded**: `_cal.xml` doesn't contain abstract/header nodes. These only exist in `_pre.xml`, so they never enter the formula tree.

| Industry | IS backbone | BS/CF | Notes |
|----------|-------------|-------|-------|
| Tech (NFLX, AAPL) | Revenue → OpInc → EBT → NI | Standard | Typical 4-level cascade |
| Banking (JPM) | InterestIncome → ... → NI | Standard | No COGS, shorter cascade |
| Insurance (BRK) | EarnedPremiums → ... → NI | Standard | Different revenue concept, same structure |
| Pharma (PFE) | Revenue → OpInc → EBT → NI | Standard | R&D heavy, same cascade |
| Foreign filer (20-F) | Revenue → ... → NI | Standard | Same XBRL linkbases, same algorithm |

---

## Testing

### Unit tests (`tests/test_dual_linkbase.py`)

**Presentation parsing:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_parse_pre_linkbase` | Sample pre.xml with order attrs | Correct concept→position map |
| `test_pre_index_matches_cal_role` | Pre and cal with same role URL | `build_presentation_index()` returns match |
| `test_pre_index_fuzzy_match` | Slightly different role URLs | Matches on terminal segment |
| `test_missing_order_attr` | Pre.xml without order attrs | Uses sequential position |

**Presentation sorting:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_sort_reverses_arbitrary_order` | Children in [G&A, R&D, COGS, Revenue] | Sorted to [Revenue, COGS, R&D, G&A] |
| `test_sort_preserves_weights` | Sort children | All weights unchanged |
| `test_sort_unknown_concept` | Child not in pres_index | Sorts to end (999) |
| `test_sort_recursive` | Nested tree | All levels sorted |

**Cascade layout:**

| Test | Setup | Expected |
|------|-------|----------|
| `test_cascade_revenue_first` | NFLX-like IS tree | Revenue at row 4, NI at last row |
| `test_cascade_formulas_correct` | IS tree with known values | Each subtotal formula = SUM(children * weight) |
| `test_cascade_two_level` | Simple IS: Revenue, COGS → OpInc | Revenue row 4, COGS row 5, OpInc row 6 |
| `test_cascade_no_backbone` | All leaf children | +1 children first, -1 children second |
| `test_existing_assign_rows_bs` | BS tree | Assets at top, children below (existing behavior preserved) |

**Integration (from Phase 3c):**

| Test | Setup | Expected |
|------|-------|----------|
| `test_is_root_is_ni` | Root=NetIncomeLoss | Root tagged INC_NET, values preserved |
| `test_orphan_closes_gap` | Parent=100, children=[70], orphan=30 | Inserted, gap=0 |
| `test_complete_tree_passes` | Parent=100, children=[60, 40] | No errors |
| `test_check_skipped_when_role_missing` | No IS_DA | D&A check not rendered |

### E2E validation

```bash
# Single company
python xbrl_tree.py --url <nflx_filing_url> -o nflx_trees.json
python pymodel.py --trees nflx_trees.json --checkpoint

# Verify sheet matches filing
# Revenue should be row 4 (first data row), Net Income should be last IS row
# Every subtotal formula should equal the XBRL-declared value

# All 10 Phase 1b companies
for ticker in NFLX AAPL MSFT AMZN GOOG META TSLA JPM BRK PFE; do
    python run_pipeline.py $ticker
done
```

---

## Implementation Plan

```
Step 1: Add fetch_pre_linkbase + parse_pre_linkbase        (xbrl_tree.py)
Step 2: Add sort_by_presentation                           (xbrl_tree.py)
Step 3: Fix _tag_is_positions (check root first)           (xbrl_tree.py)
Step 4: Add _tag_is_semantic                               (xbrl_tree.py)
Step 5: Add _supplement_orphan_facts                       (xbrl_tree.py)
Step 6: Add verify_tree_completeness                       (pymodel.py)
   ↓ (steps 1-6 can be developed independently)
Step 7: Update reconcile_trees to include steps A-I        (xbrl_tree.py)
Step 8: Add cascade_layout (IS only; BS/CF keep _assign_rows) (sheet_builder.py)
Step 9: Refactor write_sheets into 3-pass labeled blocks     (sheet_builder.py)
Step 10: Replace checks with CROSS_STATEMENT_CHECKS        (sheet_builder.py)
Step 11: Add pipeline gate                                 (run_pipeline.py)
   ↓
Step 12: Delete temp_pre.py (superseded prototype)          (cleanup)
Step 13: Write tests/test_dual_linkbase.py
Step 14: E2E validation on NFLX + all 10 Phase 1b companies
```

---

## Success Criteria

1. **Revenue at top of IS**: NFLX sheet shows Revenue as the first data row, Net Income as the last.
2. **Sibling order matches 10-K**: COGS before S&M before R&D before G&A (not arbitrary cal order).
3. **Formulas correct by construction**: every subtotal row's formula = SUM(children * weight), verified by `verify_tree_completeness()`.
4. **IS tagging fix**: `_tag_is_positions` never overwrites `.values`. NFLX EBT = 12,722,552.
5. **Orphan supplementation**: NFLX Current Liabilities gap closed by inserting real XBRL facts.
6. **Pipeline gate**: sheet write blocked if tree completeness or cross-statement invariants fail.
7. **Three-pass rendering**: no hardcoded row indices, no circular dependencies, all cross-tab references resolve.
8. **5 real checks only**: zero tautological checks on the Summary tab.
9. **10/10 companies pass**: tree completeness + cross-statement invariants for all Phase 1b companies.
10. **Works across industries**: tech, banking, insurance, pharma, foreign filers all produce correct models.

---

## KISS Simplifications Applied

Per KISS review (Stage 2), three simplifications were incorporated:

1. **No separate `preorder_layout()` function.** The existing `_assign_rows()` in `_render_sheet_body()` already does pre-order traversal for BS/CF. Only `cascade_layout()` is new (for IS).
2. **Three-pass rendering as labeled blocks**, not separate functions. All three passes live inside `write_sheets()` to minimize indirection.
3. **Delete `temp_pre.py`** — superseded prototype of presentation linkbase parsing.

---

## Known Risks

1. **Presentation linkbase may be missing.** Some older or non-standard filings may not include `_pre.xml`. Fallback: skip sorting, use cal tree order as-is (arbitrary but functional).

2. **Pre and cal role URLs may not match exactly.** Different filers may use slightly different role URL formats. The fuzzy matching (terminal segment comparison) handles most cases. If it fails, children remain in cal order.

3. **Backbone detection may be ambiguous.** If a node has multiple +1-weight children with sub-children, cascade picks the first one. In practice, IS trees have exactly one backbone child at each level (OpInc under EBT, EBT under NI).

4. **Orphan facts may not fully close gaps.** Same risk as Phase 3c — pipeline stops, manual inspection needed.

5. **BS/CF may occasionally need cascade.** Some exotic filers might root BS at Equity (bottom-up). Current assumption: BS/CF are always top-down. If violated, the preorder layout produces wrong order. Detection: check if the root concept ends with a "total" concept that should be at the bottom. Mitigation: extend cascade to BS/CF if needed.

---

## Relationship to Prior Phases

| Phase | Status | What this phase takes from it |
|-------|--------|------------------------------|
| 3a (Refactor) | Spec complete | Formatting patterns, role naming conventions |
| 3b (Formatting) | Spec complete | Sheet formatting rules (bold subtotals, indentation, number formats) |
| 3c (Tree Integrity) | Spec complete | IS tagging fix, orphan facts, tree completeness, three-pass rendering, declarative checks |
| 3d (Pre Pivot) | **Rejected** | Motivation (match 10-K visually), but NOT the approach (pre as primary) |
| **3e (This)** | **Active** | Combines 3c's data integrity with correct rendering via dual-linkbase + cascade |
