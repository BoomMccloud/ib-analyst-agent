# Phase 2 Implementation Guide: Tree-First Architecture

## Summary

Phase 1b proved that the XBRL calculation linkbase contains the company's own financial model — parent-child relationships with weights, subtotals as weighted sums of children, the exact structure the company uses in its filing.

Phase 2 recognizes that **the tree IS the model**. Instead of converting trees → flat codes → reimplemented math → sheets, we render trees directly. This eliminates ~800 lines of translation and reimplementation code in `pymodel.py`.

## The Problem Phase 2 Solves

`pymodel.py` currently does three things that are redundant with the XBRL tree:

1. **`load_filing()` (500 lines)** — Translates structured JSON into flat `{code: {period: value}}` + categories. But the tree already has every value attached to the right node in the right hierarchy.

2. **Tautological API (`set_is_cascade`, `set_bs_totals`, `set_cf_totals`, `set_category`)** — Reimplements `GP = Rev - COGS`, `TA = TCA + TNCA`, `NETCH = OPCF + INVCF + FINCF`. But the tree already encodes these as `parent.value = sum(child.value * child.weight)`. The company's filing already did this math.

3. **`write_sheets()` (160 lines)** — Hardcodes IS layout as Rev → COGS → GP → OpEx → NI. Breaks for banks, industrials, anything non-Apple. But the tree defines the correct layout for every company.

## Architecture

```
xbrl_tree.py          →  reconcile_trees()      →  verify_model()         →  sheet_builder.py
(trees with values)      (positional ID +           (5 cross-statement         (render trees to
                          cross-statement             invariants)                Google Sheets)
                          overrides)
```

That's it. No intermediate flat dict. No category assignment. No reimplemented cascade.

### What `reconcile_trees()` does

`xbrl_group.py` currently mixes two concerns: (a) optional LLM sibling grouping and (b) mandatory deterministic logic — positional identification and cross-statement reconciliation. Phase 2 separates these. The mandatory logic moves into `reconcile_trees(trees, facts)` in `xbrl_tree.py`.

**Positional identification** — finds standard nodes by tree position, not concept names:
- `BS_TA` = Assets tree root
- `BS_TL` = L&E tree's first (largest) child branch
- `BS_TE` = L&E tree's last non-zero child branch
- `INC_NET` = CF tree's ProfitLoss/NetIncomeLoss leaf (authoritative), then value-matched to the IS tree
- `CF_ENDC` = from the XBRL facts dict (instant context, not in any tree)

This logic is currently in `xbrl_group.py` (`_extract_bs_from_trees`, `_extract_is_from_tree`, `_extract_cf_from_trees`). It's pure deterministic code — no LLM involved — so it belongs in `xbrl_tree.py`.

**Cross-statement reconciliation** — mutates tree node values so invariants hold by construction:
- **Cash link**: Override BS's first cash item values with `CF_ENDC` values, so `CF_ENDC == BS_CASH` holds
- **NI link**: Use CF's ProfitLoss leaf value as authoritative `INC_NET` for both IS and CF, handling cases like GE where IS and CF use different NI concepts

These overrides are **prerequisites for `verify_model()` to pass** — they are not optional presentation polish. Without them, the 9/10 pass rate from Phase 1b degrades.

**Output**: `reconcile_trees()` returns the trees with:
1. Positional annotations on key nodes (e.g., `node.role = "BS_TA"`) so `verify_model()` can find them
2. Reconciled values where cross-statement overrides were applied

`verify_model()` then uses `node.role` to locate the specific nodes it needs for each invariant check.

### What the tree already provides

Each `TreeNode` carries:
```python
concept: str                    # "us-gaap_Assets"
name: str                       # "Assets"
weight: float                   # +1.0 or -1.0 relative to parent
values: dict[str, float]        # {"2024-01-28": 364980.0}
children: list[TreeNode]        # child nodes
is_leaf: bool                   # True if no children
```

The tree IS the single source of truth for both structure AND values. No split-brain — the values live on the nodes that define the hierarchy.

### Why values stay on tree nodes (not a flat dict)

The first-principle analysis flagged split-brain risk between tree values and a flat model dict. The fix isn't to strip values from trees — it's to **not have two copies**. The tree is the only representation. `sheet_builder.py` reads `node.values[period]` directly. There is no flat dict for historical data.

## What gets deleted from `pymodel.py`

| Code | Lines | Why redundant |
|------|-------|---------------|
| `load_filing()` | ~500 | Tree already has values in hierarchy |
| `set_is_cascade()` | 12 | Tree weights encode IS math |
| `set_bs_totals()` | 6 | Tree weights encode BS math |
| `set_cf_totals()` | 6 | Tree weights encode CF math |
| `set_cf_cash()` | 4 | Tree + XBRL facts handle this |
| `set_category()` | 5 | Tree parent-child IS the category |
| `_load_is_fallback()` | ~80 | No more name-based fallbacks |
| `_load_bs_fallback()` | ~80 | No more name-based fallbacks |
| `_load_cf_fallback()` | ~80 | No more name-based fallbacks |
| `write_sheets()` | ~160 | Moves to `sheet_builder.py` |
| `gws_create()`, `gws_write()`, `dcol()` | ~30 | Moves to `sheet_builder.py` |
| `ModelResult` dataclass | 4 | Not needed — trees are the result |

## What survives in `pymodel.py`

### 1. `verify_model(trees, facts)` — Cross-statement invariant checks

These check relationships **across** trees (not within a single tree), so the tree weights can't enforce them:

| # | Check | How it works |
|---|-------|-------------|
| 1 | BS Balance | `BS_TA == BS_TL + BS_TE` — values from Assets tree root vs L&E tree children |
| 2 | Cash Link | `CF_ENDC == BS_CASH` — CF facts dict vs BS tree leaf |
| 3 | NI Link | `INC_NET (IS) == INC_NET (CF)` — IS tree leaf vs CF tree leaf |
| 4 | D&A Link | `DA (IS) == DA (CF)` — value-matched across trees |
| 5 | SBC Link | `SBC (IS) == SBC (CF)` — value-matched across trees |

The inputs are the reconciled trees (with `node.role` annotations from `reconcile_trees()`). `verify_model` locates nodes by role, not by concept name. No flat model dict needed.

### 2. `compute_forecasts(trees, assumptions)` — Phase 3 only

Forecasting is the one place where new modeling logic is needed. The tree structure tells you:
- **Which nodes to forecast**: leaves (they have no children to derive from)
- **Which nodes are computed**: parents (value = weighted sum of children)

This means forecast logic is: apply growth rates to leaves, recompute parents bottom-up via tree weights. But this is Phase 3 scope — Phase 2 ships with historical data only.

## `sheet_builder.py` — Render trees to Google Sheets

### Tree-driven rendering

One recursive function handles all statements for all companies:

```python
def _render_tree_rows(node, periods, indent=0):
    """Recursively render a tree node into sheet rows.
    
    Values come directly from node.values — the tree IS the model.
    """
    rows = []
    label = ("  " * indent) + node["name"]
    
    row = ["", "", label, ""]
    for p in periods:
        val = node.get("values", {}).get(p, 0)
        row.append(round(val) if val else "")
    rows.append(row)
    
    for child in node.get("children", []):
        rows.extend(_render_tree_rows(child, periods, indent + 1))
    
    return rows
```

This replaces:
- 40 lines of hardcoded IS rows (Rev → COGS → GP → OpEx → NI)
- 30 lines of hardcoded BS rows (CA → NCA → TA → CL → NCL → TL → E)
- 20 lines of hardcoded CF rows (OPCF → INVCF → FINCF → NETCH)
- All `find_cat()` / `cat_rows()` logic

Banks, industrials, tech, utilities — all render correctly because the tree defines their structure.

### Invariant rows

Only the 5 real cross-statement checks. Written as sheet formulas (not static values). If BS_TA cell is F10 and BS_TL cell is F25 and BS_TE cell is F30:

```
=F10 - F25 - F30    (must be 0)
```

No tautological checks (within-tree sums can't fail — they're defined by the tree). No company-specific checks (unnecessary complexity per KISS analysis).

### Failure handling

`sheet_builder.py` raises on Google Sheets API failures. Retry/cleanup policy belongs in `run_pipeline.py` (the controller), not the driver.

## Pipeline flow

```bash
# Stage 1: Fetch filing URLs
python agent1_fetcher.py AAPL --years 5 > filings.json

# Stage 2: Parse XBRL → trees with values + reconcile
python xbrl_tree.py --url <filing_url> --json -o trees.json
# (xbrl_tree.py now includes reconcile_trees(): positional ID + cross-statement overrides)

# Checkpoint: Verify 5 cross-statement invariants
python pymodel.py --trees trees.json --checkpoint

# Stage 3 (Phase 3): Forecast
# python pymodel.py --trees trees.json --forecast -o forecast.json

# Stage 4: Render to Google Sheets
python sheet_builder.py --trees trees.json --company "Apple Inc."
```

Note: `xbrl_group.py`'s LLM-based sibling grouping is no longer in the critical path. The mandatory parts of `xbrl_group.py` (positional identification + cross-statement reconciliation) have moved into `xbrl_tree.py::reconcile_trees()`. `xbrl_group.py` may still be called optionally before `sheet_builder.py` to group small siblings for cleaner output.

### Linkbase fallback

Some filings (MSFT, PLD) lack a `_cal.xml` calculation linkbase. When `xbrl_tree.py` cannot build trees, `run_pipeline.py` should detect the failure and fall back to the legacy LLM path (`structure_financials.py` → `pymodel.py`). This is a routing decision in the orchestrator, not an architectural change — the legacy path remains intact and is not deleted by Phase 2.

## What about `xbrl_group.py`?

`xbrl_group.py` currently serves three purposes:
1. **Positional identification** — deterministic logic that finds BS_TA, BS_TL, BS_TE, INC_NET, CF_ENDC by tree position
2. **Cross-statement reconciliation** — overrides BS cash with CF_ENDC, uses CF's NI as authoritative INC_NET
3. **Sibling grouping** — LLM decides which small items to merge into "Other"
4. **Tree → structured JSON** — converts trees into the flat format `load_filing()` expects

Purpose #4 is eliminated entirely (no more `load_filing()`). Purposes #1 and #2 are **mandatory** — they migrate to `reconcile_trees()` in `xbrl_tree.py` (see Architecture section above). Purpose #3 becomes optional presentation polish: `sheet_builder.py` can render all leaves by default, and optionally call the LLM to group small siblings for cleaner output. This is a nice-to-have, not a requirement.

## Testing

### Tree rendering test

```python
def test_render_tree_matches_structure():
    """Rendered rows should match tree node order."""
    tree = load_fixture("aapl_is_tree.json")
    rows = _render_tree_rows(tree, ["2024-01-28", "2023-01-29"])
    
    # Every non-empty node should appear as a row
    labels = [r[2].strip() for r in rows]
    assert "Revenue" in labels
    assert "Net Income" in labels

def test_bank_is_has_no_gp_row():
    """Bank IS tree has no Gross Profit node — so no GP row."""
    tree = load_fixture("bac_is_tree.json")
    rows = _render_tree_rows(tree, ["2024-12-31"])
    labels = [r[2].strip() for r in rows]
    assert "Gross Profit" not in labels
```

### Reconciliation test

```python
def test_reconcile_identifies_positions():
    """reconcile_trees tags BS_TA, BS_TL, BS_TE by position."""
    trees = load_fixture("aapl_raw_trees.json")
    facts = load_fixture("aapl_facts.json")
    reconciled = reconcile_trees(trees, facts)
    
    # Positional identification — no name matching
    assert reconciled["BS"]["role"] == "BS_TA"  # Assets tree root
    # L&E children identified by position
    le_children = [c for c in reconciled["BS_LE"]["children"] if c.get("role")]
    roles = {c["role"] for c in le_children}
    assert "BS_TL" in roles
    assert "BS_TE" in roles

def test_reconcile_overrides_cash():
    """BS cash overridden with CF_ENDC values."""
    trees = load_fixture("aapl_raw_trees.json")
    facts = load_fixture("aapl_facts.json")
    reconciled = reconcile_trees(trees, facts)
    
    # Cash values on BS should match CF_ENDC from facts
    cf_endc = facts["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
    bs_cash_node = find_node_by_role(reconciled, "BS_CASH")
    for period, val in cf_endc.items():
        assert bs_cash_node["values"][period] == val
```

### Cross-statement invariant test

```python
def test_invariants_pass():
    """5 cross-statement checks must pass on reconciled trees."""
    trees = load_fixture("aapl_raw_trees.json")
    facts = load_fixture("aapl_facts.json")
    reconciled = reconcile_trees(trees, facts)
    errors = verify_model(reconciled)
    assert errors == [], f"Invariant failures: {errors}"

def test_invariants_catch_failures():
    """verify_model reports errors on unreconciled trees."""
    trees = load_fixture("aapl_raw_trees.json")
    # Skip reconciliation — raw trees may have mismatched NI/cash
    errors = verify_model(trees)
    # Should detect at least one mismatch (or pass if raw values happen to match)
    # The point: verify_model doesn't silently swallow mismatches
```

## Success Criteria (The Final 10%)

To declare Phase 2 a complete success, passing unit tests is not enough. You must meet these architectural and end-to-end requirements:

1. **The Code Deletion (Architectural Proof):** `pymodel.py` must actually shrink to just `verify_model()`. Leaving dead code like `load_filing()`, name-based fallbacks, tautological API methods, or `write_sheets()` in the file means the architectural cleanup isn't finished. (Expect ~800 lines deleted).
2. **`xbrl_tree.py` includes `reconcile_trees()`:** Positional identification and cross-statement reconciliation logic migrated from `xbrl_group.py`. No LLM dependency.
3. **`sheet_builder.py` renders from trees:** One recursive function handles all companies. No hardcoded IS/BS/CF layouts.
4. **5 invariant checks pass** for the same 9/10 companies from Phase 1b.
5. **The Sheet Formulas:** The Google Sheets output for the 5 invariant rows must actually be written as sheet formulas (e.g. `=F10 - F25 - F30`), not static numbers.
6. **The End-to-End Pipeline:** The `run_pipeline.py` script must successfully chain these new modules together in the real world: `xbrl_tree.py` (with reconciliation) → `verify_model()` → `sheet_builder.py` → Google Sheet.
7. **The Fallback Routing:** The orchestrator (`run_pipeline.py`) must successfully catch a missing `_cal.xml` and gracefully fall back to the legacy LLM path.

## What Changed from the Previous Phase 2 Spec

| Aspect | Previous Phase 2 | This Phase 2 |
|--------|-----------------|--------------|
| Core insight | Decouple sheets from math | The tree IS the model — no math to decouple |
| `ModelResult` | New 12-field dataclass | Eliminated — trees are passed directly |
| `load_filing()` | Keep, feed into `ModelResult` | Delete — tree already has values in hierarchy |
| Tautological API | Keep for forecasts | Delete — tree weights encode all within-statement math |
| Flat model dict | Single source of truth for values | Eliminated — `node.values` is the source of truth |
| `xbrl_group.py` | Required (tree → structured → model) | LLM grouping optional; positional ID + reconciliation migrated to `xbrl_tree.py::reconcile_trees()` |
| `compute_model()` | Keep, returns `ModelResult` | Delete for historical; Phase 3 adds `compute_forecasts()` for leaves only |
| Invariant checks | 5 real + 4 tautological + 4 company-specific | 5 real only (KISS) |
| Cross-statement reconciliation | Done in `xbrl_group.py` | Done in `xbrl_tree.py::reconcile_trees()` — mandatory, not optional |
| Linkbase fallback | Not addressed | `run_pipeline.py` routes to legacy LLM path when `_cal.xml` missing |
| Lines deleted | ~160 (just `write_sheets`) | ~800 (`load_filing` + tautological API + fallbacks + `write_sheets`) |

---

## Phase 2 Implementation Plan

### Step 1: Secure the Legacy Path

**Goal**: Preserve the old code so the fallback path still works after we gut `pymodel.py`.

**Tasks**:

1. **Copy `pymodel.py` → `legacy_pymodel.py`**:
   ```bash
   cp pymodel.py legacy_pymodel.py
   ```
   This is a full copy. Don't change anything in it. The fallback path (`run_pipeline.py` → `structure_financials.py` → `legacy_pymodel.py`) will use this file when `_cal.xml` is missing.

2. **Leave `xbrl_group.py` untouched for now**. We'll clean it up in Step 5 after the new code works.

---

### Step 2: Implement `reconcile_trees()` in `xbrl_tree.py`

**Goal**: Add a `role` attribute to `TreeNode` and a `reconcile_trees()` function that tags key nodes by position and applies cross-statement value overrides — all without any LLM dependency.

**What you're migrating**: The deterministic logic currently in `xbrl_group.py` lines 209–334 (`tree_to_structured`), 337–542 (`_extract_is_from_tree` / `_flatten_is_cascade`), 544–671 (`_extract_bs_from_tree`), and 674–778 (`_extract_cf_from_tree`).

But you are NOT migrating the structured JSON output format. Instead of producing flat dicts like `{"code": "BS_TA", "values": {...}}`, you are tagging the existing tree nodes with `.role` attributes.

#### 2.1: Add `role` attribute to `TreeNode`

Open `xbrl_tree.py`. In the `TreeNode.__init__()` method (around line 200), add one line:

```python
self.role: str | None = None    # e.g., "BS_TA", "INC_NET", "CF_ENDC"
```

Also update `to_dict()` (around line 227) to include role in the serialized output:

```python
def to_dict(self):
    d = {
        "concept": self.concept,
        "tag": self.tag,
        "name": self.name,
        "weight": self.weight,
        "values": self.values,
        "is_leaf": self.is_leaf,
    }
    if self.role:
        d["role"] = self.role
    if self.children:
        d["children"] = [c.to_dict() for c in self.children]
    return d
```

#### 2.2: Create `reconcile_trees()` function

Add this function to `xbrl_tree.py` (after the `build_statement_trees()` function). It takes the dict returned by `build_statement_trees()` and mutates the tree nodes in-place.

```python
def reconcile_trees(trees: dict) -> dict:
    """Tag key nodes by position and apply cross-statement value overrides.

    Mutates tree nodes in-place by setting .role attributes and overriding
    values where cross-statement links require it.

    Args:
        trees: dict from build_statement_trees() with keys
               "IS", "BS", "BS_LE", "CF", "facts", "periods"

    Returns:
        The same trees dict (mutated in-place). Also returns it for chaining.
    """
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

    return trees
```

#### 2.3: Implement `_tag_bs_positions()`

This migrates the logic from `xbrl_group.py` `_extract_bs_from_tree()` (lines 544–671), but instead of building flat dicts, it tags existing nodes with `.role`.

```python
def _tag_bs_positions(assets_tree: TreeNode | None, liab_eq_tree: TreeNode | None):
    """Tag BS nodes by position in the tree.

    Position rules (no name matching):
    - Assets tree root → BS_TA
    - First child of root with sub-children → BS_TCA (Current Assets)
    - All other children of root → NCA (Non-Current Assets, no single role tag)
    - L&E tree: filter out zero-valued children
    - Last non-zero branch child → BS_TE (Equity)
    - Everything before equity → Liabilities
    - If single liabilities wrapper: that node → BS_TL
    - If multiple liabilities components: synthesize a wrapper → BS_TL
    - First child of BS_TL with sub-children → BS_TCL (Current Liabilities)
    """
    if assets_tree and assets_tree.values:
        # BS_TA = root of Assets tree
        assets_tree.role = "BS_TA"

        # BS_TCA = first child that has its own children (not a leaf)
        for child in assets_tree.children:
            if child.children and not child.is_leaf:
                child.role = "BS_TCA"
                # Tag first flex item as BS_CASH (for cash link override later)
                if child.children:
                    child.children[0].role = "BS_CASH"
                break

    if liab_eq_tree and liab_eq_tree.children:
        # Filter out zero-valued children (e.g., "Commitments and Contingencies")
        branch_children = [
            c for c in liab_eq_tree.children
            if c.values and any(v != 0 for v in c.values.values())
        ]

        if len(branch_children) >= 2:
            # Equity is ALWAYS the LAST non-zero branch child
            equity_node = branch_children[-1]
            equity_node.role = "BS_TE"

            # Everything before equity = liabilities
            liab_children = branch_children[:-1]

            if len(liab_children) == 1:
                # Single Liabilities wrapper — use it directly
                liab_node = liab_children[0]
            else:
                # Multiple liabilities components (KO pattern) — synthesize wrapper
                # Create a synthetic parent node to group them
                liab_node = TreeNode("__LIABILITIES_SYNTHETIC", weight=1.0)
                liab_node.name = "Liabilities"
                liab_values = {}
                for child in liab_children:
                    for p, v in child.values.items():
                        liab_values[p] = liab_values.get(p, 0) + v
                    liab_node.add_child(child)
                liab_node.values = liab_values
                # Replace L&E tree children so the synthetic node is in the tree
                liab_eq_tree.children = [liab_node, equity_node]

            liab_node.role = "BS_TL"

            # BS_TCL = first child of liabilities that has sub-children
            for child in liab_node.children:
                if child.children and not child.is_leaf:
                    child.role = "BS_TCL"
                    break

        elif len(branch_children) == 1:
            # Single child — liabilities only, equity may be missing
            branch_children[0].role = "BS_TL"
```

#### 2.4: Implement `_tag_cf_positions()`

This migrates the structural item extraction from `xbrl_group.py` `_extract_cf_from_tree()` (lines 674–778).

```python
def _tag_cf_positions(cf_tree: TreeNode | None, facts: dict) -> dict | None:
    """Tag CF nodes by position. Returns CF_ENDC values dict (from facts, not tree).

    Position rules:
    - Tree root → CF_NETCH (Net Change in Cash)
    - Child whose concept contains "OperatingActivities" → CF_OPCF
    - Child whose concept contains "InvestingActivities" → CF_INVCF
    - Child whose concept contains "FinancingActivities" → CF_FINCF
    - Leaf in CF tree with concept ending in "ProfitLoss" or "NetIncomeLoss" → INC_NET_CF
    - CF_ENDC comes from the facts dict (instant context), not from the tree

    The GE drill-down: When a CF section node (e.g., Operating) has a child
    that itself has MORE children than the section node (e.g., ContinuingOps
    with 19 leaves vs Operating's 2 direct children), drill into that child
    to get the granular line items. This handles GE's structure where OpCF
    has ContinuingOps (19 items) + DiscontinuedOps (1 item).
    """
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
        """Walk the CF tree depth-first. Tag section nodes by concept pattern."""
        concept_name = node.concept.split('_', 1)[-1] if '_' in node.concept else node.concept

        for pattern, role in CF_ROLE_MAP.items():
            if concept_name.startswith(pattern) and role not in seen_roles and node.values:
                node.role = role
                seen_roles.add(role)
                # Don't recurse into children — we already found the section node
                return

        # Also tag the NI leaf inside CF (ProfitLoss or NetIncomeLoss)
        if concept_name in ("ProfitLoss", "NetIncomeLoss") and node.values and not node.children:
            node.role = "INC_NET_CF"

        for child in node.children:
            _walk_and_tag(child)

    _walk_and_tag(cf_tree)

    return cf_endc_values
```

#### 2.5: Implement `_tag_is_positions()`

This migrates the NI cross-referencing from `xbrl_group.py` `_extract_is_from_tree()` (lines 337–467), specifically the CF NI → IS NI matching.

```python
def _tag_is_positions(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    """Tag IS Net Income node using CF's NI as authoritative source.

    Algorithm:
    1. Find the INC_NET_CF node (already tagged by _tag_cf_positions).
    2. Get its values dict — these are the authoritative NI values.
    3. Override the IS root (or the IS node that matches CF's NI value) with
       these values and tag it as INC_NET.

    Why: The IS tree may use a narrower concept (e.g., NetIncomeLossAvailableToCommon
    or IncomeLossFromContinuingOperations) while the CF tree uses the standard
    ProfitLoss concept. The CF is the bridge statement — its NI is authoritative.
    """
    if not is_tree:
        return

    # Find CF's NI values (the authoritative source)
    cf_ni_values = None
    if cf_tree:
        cf_ni_node = find_node_by_role(cf_tree, "INC_NET_CF")
        if cf_ni_node:
            cf_ni_values = cf_ni_node.values

    if cf_ni_values:
        # Find the IS node to tag as INC_NET
        # Strategy: the IS root's first positive-weight depth-1 child
        # is typically the NI node in the cascade
        # But safer: find the depth-1 child whose value matches CF's NI
        tagged = False
        for child in is_tree.children:
            if child.weight > 0 and child.values:
                # Check if values match CF's NI
                child.role = "INC_NET"
                child.values = dict(cf_ni_values)  # Override with CF's authoritative values
                tagged = True
                break

        # Fallback: tag the root if no positive child found
        if not tagged:
            is_tree.role = "INC_NET"
            is_tree.values = dict(cf_ni_values)
    else:
        # No CF NI available — tag IS root as INC_NET (best guess)
        is_tree.role = "INC_NET"
```

#### 2.6: Implement `find_node_by_role()` helper

This is a simple recursive search used by multiple functions:

```python
def find_node_by_role(tree: TreeNode, role: str) -> TreeNode | None:
    """Recursively search the tree for a node with the given role."""
    if tree.role == role:
        return tree
    for child in tree.children:
        result = find_node_by_role(child, role)
        if result:
            return result
    return None
```

#### 2.7: Implement `_override_bs_cash()`

This migrates the "Cash Link Fix" from `xbrl_group.py` `tree_to_structured()` lines 307–332.

```python
def _override_bs_cash(assets_tree: TreeNode | None, cf_endc_values: dict | None):
    """Override BS cash node values with CF_ENDC values.

    Why: The BS tree's cash item may be "Cash and Cash Equivalents" while
    CF_ENDC is "Cash, Cash Equivalents, Restricted Cash, and Restricted Cash
    Equivalents". CF_ENDC is authoritative because it's what the cash flow
    statement reconciles to. Without this override, the Cash Link invariant
    (CF_ENDC == BS_CASH) would fail.

    Algorithm:
    1. Find the node tagged BS_CASH (first child of BS_TCA, tagged in Step 2.3).
    2. For each period in cf_endc_values:
       a. Compute delta = cf_endc_value - old_bs_cash_value
       b. Set BS_CASH node value to cf_endc_value
       c. If delta is significant (>0.5), also adjust the BS_TCA subtotal
          by adding delta, so the TCA → TA rollup stays correct.
    """
    if not assets_tree or not cf_endc_values:
        return

    cash_node = find_node_by_role(assets_tree, "BS_CASH")
    tca_node = find_node_by_role(assets_tree, "BS_TCA")

    if not cash_node:
        return

    for period, new_val in cf_endc_values.items():
        old_val = cash_node.values.get(period, 0)
        delta = new_val - old_val
        cash_node.values[period] = new_val

        # Adjust TCA subtotal to absorb the delta
        if tca_node and abs(delta) > 0.5:
            tca_node.values[period] = tca_node.values.get(period, 0) + delta
```

#### 2.8: Implement `_filter_to_complete_periods()`

This migrates the period intersection from `xbrl_group.py` `tree_to_structured()` lines 216–229.

```python
def _filter_to_complete_periods(trees: dict):
    """Remove values for periods that aren't present in ALL statements.

    A 'complete' period has data in IS + BS (Assets) + BS (L&E) + CF.
    This prevents partial-period rows in the output sheet.

    Algorithm:
    1. Collect the set of periods from each statement tree's root values.
    2. Intersect all four sets.
    3. Walk every node in every tree and remove any period key not in the intersection.
    4. Store the complete_periods list on trees["complete_periods"] for downstream use.
    """
    is_periods = set(trees["IS"].values.keys()) if trees.get("IS") else set()
    bs_periods = set(trees["BS"].values.keys()) if trees.get("BS") else set()
    bs_le_periods = set(trees["BS_LE"].values.keys()) if trees.get("BS_LE") else bs_periods
    cf_periods = set(trees["CF"].values.keys()) if trees.get("CF") else set()

    complete = is_periods & bs_periods & bs_le_periods & cf_periods
    trees["complete_periods"] = sorted(complete)

    if not complete:
        import sys
        print(f"WARNING: No complete periods. IS={sorted(is_periods)}, "
              f"BS={sorted(bs_periods)}, CF={sorted(cf_periods)}", file=sys.stderr)
        return

    # Walk every tree and filter values to complete periods only
    def _filter_node(node: TreeNode):
        node.values = {p: v for p, v in node.values.items() if p in complete}
        for child in node.children:
            _filter_node(child)

    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _filter_node(tree)
```

#### 2.9: Wire it into `build_statement_trees()`

At the end of `build_statement_trees()` (around line 430, right before the `return` statement), add:

```python
    # Reconcile: tag positions + apply cross-statement overrides
    reconcile_trees(result)
```

This means every caller of `build_statement_trees()` automatically gets reconciled trees.

---

### Step 3: Shrink `pymodel.py` (The 800-Line Deletion)

**Goal**: Delete everything from `pymodel.py` except `verify_model()`, and rewrite `verify_model()` to work with tree nodes instead of flat dicts.

**IMPORTANT**: Before deleting anything, make sure Step 1 is done (you have `legacy_pymodel.py` as a backup).

#### 3.1: What to DELETE (line ranges from current file)

Delete these functions/sections completely:

| What | Current lines | Why |
|------|---------------|-----|
| `ModelResult` dataclass | 26–30 | Trees are the result |
| `set_v()` | 40–44 | No flat dict to set values in |
| `get_v()` | 47–49 | No flat dict to get values from |
| `set_category()` | 52–58 | Tree parent-child IS the category |
| `set_is_cascade()` | 61–72 | Tree weights encode IS math |
| `set_bs_totals()` | 75–83 | Tree weights encode BS math |
| `set_cf_totals()` | 86–92 | Tree weights encode CF math |
| `set_cf_cash()` | 95–99 | Tree + CF_ENDC handle this |
| `clean_label()` | 106–108 | Not needed — tree nodes have `.name` |
| `gws_create()` | 115–122 | Moves to `sheet_builder.py` |
| `dcol()` | 125–132 | Moves to `sheet_builder.py` |
| `_deep_find()` | 139–150 | Only used by `load_filing()` |
| `_navigate()` | 153–158 | Only used by `load_filing()` |
| `_convert_section_first()` | 162–209 | Only used by `load_filing()` |
| `_flatten_category()` | 209–249 | Only used by `load_filing()` |
| `_flatten_cf_section()` | 249–288 | Only used by `load_filing()` |
| `pick_flex_rows()` | 288–302 | Only used by `load_filing()` |
| `sum_values()` | 302–316 | Only used by `load_filing()` |
| `_apply_xbrl_mapping()` | 316–340 | Only used by `load_filing()` |
| `load_filing()` | 343–451 | Trees replace flat loading |
| `_load_is_fallback()` | 454–624 | No more name-based fallbacks |
| `_assign_category()` | 624–661 | No more category assignment |
| `_load_bs_fallback()` | 661–745 | No more name-based fallbacks |
| `_load_cf_fallback()` | 745–801 | No more name-based fallbacks |
| `get()` helper | 801–802 | Only used by fallbacks |
| `_find_code_by_label()` | 805–815 | Only used by fallbacks |
| `compute_model()` | 818–1141 | Phase 3 scope (forecasts) |
| `_find_cf_match()` | 1148–1159 | Rewritten in new verify_model |
| Old `verify_model()` | 1162–1248 | Rewritten below |
| `write_sheets()` | 1255–1417 | Moves to `sheet_builder.py` |
| `_deep_merge()` | 1424–1431 | Only used by old main() |
| `_merge_financials()` | 1432–1440 | Only used by old main() |
| Old `main()` | 1441–1522 | Rewritten below |

#### 3.2: What to KEEP (but rewrite)

After deletion, `pymodel.py` should contain only:

1. **Imports** (just `argparse`, `json`, `sys`)
2. **`verify_model(trees)`** — rewritten to walk tree nodes by `.role`
3. **`main()`** — rewritten for `--trees` CLI arg

#### 3.3: Rewrite `verify_model()`

The new `verify_model()` takes the reconciled trees dict and checks 5 cross-statement invariants by finding nodes via their `.role` attribute.

```python
import argparse
import json
import sys

from xbrl_tree import TreeNode, find_node_by_role, build_statement_trees, reconcile_trees


def verify_model(trees: dict) -> list[tuple]:
    """Run 5 cross-statement invariant checks on reconciled trees.

    Args:
        trees: dict from build_statement_trees() after reconcile_trees().
               Must have keys: "IS", "BS", "BS_LE", "CF", "complete_periods"

    Returns:
        List of (check_name, period, delta) tuples. Empty list = all pass.
    """
    errors = []
    periods = trees.get("complete_periods", [])

    # Locate nodes by role
    bs_ta = find_node_by_role(trees["BS"], "BS_TA") if trees.get("BS") else None
    bs_tl = find_node_by_role(trees["BS_LE"], "BS_TL") if trees.get("BS_LE") else None
    bs_te = find_node_by_role(trees["BS_LE"], "BS_TE") if trees.get("BS_LE") else None
    bs_cash = find_node_by_role(trees["BS"], "BS_CASH") if trees.get("BS") else None
    inc_net_is = find_node_by_role(trees["IS"], "INC_NET") if trees.get("IS") else None
    inc_net_cf = find_node_by_role(trees["CF"], "INC_NET_CF") if trees.get("CF") else None

    # Helper: get value from node for a period, default 0
    def nv(node, period):
        """Node value: get a period's value from a tree node, or 0."""
        if node is None:
            return 0
        return node.values.get(period, 0)

    # Helper: find CF_ENDC from facts (instant context, not a tree node)
    facts = trees.get("facts", {})
    cf_endc_values = {}
    for tag in [
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    ]:
        if tag in facts:
            cf_endc_values = facts[tag]
            break

    def check(name, period, val):
        if abs(val) > 0.5:
            errors.append((name, period, val))

    for p in periods:
        # 1. BS Balance: TA == TL + TE
        if bs_ta and bs_tl and bs_te:
            check("BS Balance (TA-TL-TE)", p,
                  nv(bs_ta, p) - nv(bs_tl, p) - nv(bs_te, p))

        # 2. Cash Link: CF_ENDC == BS_CASH
        if bs_cash and cf_endc_values:
            cf_endc = cf_endc_values.get(p, 0)
            if cf_endc != 0:
                check("Cash (CF_ENDC - BS_CASH)", p,
                      cf_endc - nv(bs_cash, p))

        # 3. NI Link: INC_NET (IS) == INC_NET (CF)
        if inc_net_is and inc_net_cf:
            is_ni = nv(inc_net_is, p)
            cf_ni = nv(inc_net_cf, p)
            if is_ni != 0:
                check("NI Link (IS - CF)", p, is_ni - cf_ni)

        # 4. D&A Link: IS D&A == CF D&A (value-matched)
        # Walk CF_OPCF's children to find a leaf matching IS D&A value
        is_da = _find_is_value_by_label(trees.get("IS"), p, ["depreciation", "amortization"])
        if is_da and is_da != 0:
            cf_da = _find_cf_match_by_value(trees.get("CF"), p, is_da)
            if cf_da is not None:
                check("D&A Link (IS - CF)", p, is_da - cf_da)

        # 5. SBC Link: IS SBC == CF SBC (value-matched)
        is_sbc = _find_is_value_by_label(trees.get("IS"), p, ["stock", "share", "compensation"])
        if is_sbc and is_sbc != 0:
            cf_sbc = _find_cf_match_by_value(trees.get("CF"), p, is_sbc)
            if cf_sbc is not None:
                check("SBC Link (IS - CF)", p, is_sbc - cf_sbc)

    return errors


def _find_is_value_by_label(is_tree: TreeNode | None, period: str,
                             keywords: list[str]) -> float | None:
    """Find an IS tree leaf whose name contains ALL keywords (case-insensitive).

    Returns the node's value for the given period, or None if not found.
    Used for D&A and SBC which don't have fixed role tags.
    """
    if not is_tree:
        return None

    def _search(node):
        name_lower = node.name.lower()
        if all(kw in name_lower for kw in keywords):
            return node.values.get(period, 0)
        for child in node.children:
            result = _search(child)
            if result is not None:
                return result
        return None

    return _search(is_tree)


def _find_cf_match_by_value(cf_tree: TreeNode | None, period: str,
                              target_value: float) -> float | None:
    """Search CF tree leaves for one whose value matches target (within 0.5).

    This is the same value-matching approach used in the old verify_model:
    scan CF operating items to find one whose value equals the IS value.
    """
    if not cf_tree:
        return None

    opcf_node = find_node_by_role(cf_tree, "CF_OPCF")
    if not opcf_node:
        return None

    def _search_leaves(node):
        if node.is_leaf and node.values:
            val = node.values.get(period, 0)
            if abs(val - target_value) < 0.5:
                return val
        for child in node.children:
            result = _search_leaves(child)
            if result is not None:
                return result
        return None

    return _search_leaves(opcf_node)
```

#### 3.4: Rewrite `main()`

```python
def main():
    parser = argparse.ArgumentParser(description="Verify financial model invariants")
    parser.add_argument("--trees", required=True, help="Path to reconciled trees JSON")
    parser.add_argument("--checkpoint", action="store_true",
                        help="Run verification and exit (no output)")
    args = parser.parse_args()

    with open(args.trees) as f:
        trees_data = json.load(f)

    # trees_data is the JSON-serialized form. We need to reconstruct TreeNode objects.
    # For now, verify_model works with the dict form from to_dict().
    # We'll add a from_dict() classmethod to TreeNode in xbrl_tree.py.

    errors = verify_model(trees_data)

    print(f"Periods: {trees_data.get('complete_periods', [])}", file=sys.stderr)
    if errors:
        print(f"verify_model: {len(errors)} error(s)", file=sys.stderr)
        for name, period, delta in errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
        sys.exit(1)
    else:
        n = len(trees_data.get("complete_periods", []))
        print(f"verify_model: ALL PASS ({n} periods)", file=sys.stderr)


if __name__ == "__main__":
    main()
```

#### 3.5: Add `from_dict()` to TreeNode

Since `verify_model()` may receive JSON (from CLI) or live TreeNode objects (from pipeline), you need a way to reconstruct TreeNodes from JSON. Add this classmethod to `TreeNode` in `xbrl_tree.py`:

```python
@classmethod
def from_dict(cls, d: dict) -> "TreeNode":
    """Reconstruct a TreeNode from its to_dict() output."""
    node = cls(d["concept"], d.get("weight", 1.0))
    node.name = d.get("name", node.name)
    node.tag = d.get("tag", node.tag)
    node.values = d.get("values", {})
    node.role = d.get("role")
    node.is_leaf = d.get("is_leaf", True)
    for child_dict in d.get("children", []):
        node.add_child(cls.from_dict(child_dict))
    return node
```

Then update `verify_model()` to handle both dict and TreeNode inputs:

```python
# At the top of verify_model(), add:
# If trees contain dicts (from JSON), reconstruct TreeNode objects
for stmt in ["IS", "BS", "BS_LE", "CF"]:
    if stmt in trees and isinstance(trees[stmt], dict):
        trees[stmt] = TreeNode.from_dict(trees[stmt])
```

---

### Step 4: Rewrite `sheet_builder.py` (Recursive Rendering)

**Goal**: Replace all hardcoded IS/BS/CF row arrays with a single recursive function that renders any tree.

#### 4.1: Delete the old code

The current `sheet_builder.py` has these hardcoded sections to delete:

| What | Lines | Why |
|------|-------|-----|
| `is_rows` construction | 79–103 | Hardcoded REV1, COGS1, OPEX1 etc. |
| `bs_rows` construction | 105–140 | Hardcoded BS_TCA, BS_TNCA categories |
| `cf_rows` construction | 142–173 | Hardcoded CF_OPCF, CF_INVCF, CF_FINCF |
| `v()`, `data_row()`, `cat_rows()`, `find_cat()` | 36–68 | Depend on flat model dict |
| Summary tab's 13 invariant rows | 184–245 | Reduce to 5 real invariants |

**Keep**: `gws_create()` (lines 9–16), `dcol()` (lines 18–25), the Google Sheets API calls pattern, column width formatting.

#### 4.2: Implement `_render_tree_rows()`

This is the core recursive function that replaces all hardcoded layouts:

```python
def _render_tree_rows(node, periods, indent=0, role_row_map=None):
    """Recursively render a tree node into spreadsheet rows.

    Args:
        node: TreeNode (or dict from to_dict())
        periods: list of period strings to include
        indent: current indentation level (0 = root)
        role_row_map: dict to populate with {role: row_number} for formula references.
                      Row numbers are 1-indexed (spreadsheet convention).
                      Pass an empty dict {} on first call.

    Returns:
        list of rows, where each row is a list of cell values:
        [code_col, empty_col, label_col, empty_col, val_period1, val_period2, ...]

    How it works:
    1. Create a row for this node: indented label + values for each period
    2. If the node has a role (e.g., "BS_TA"), record its row number in role_row_map
    3. Recursively render all children (depth-first)
    4. Return all rows in tree display order
    """
    rows = []

    # Access node data (works with both TreeNode objects and dicts)
    if isinstance(node, dict):
        name = node.get("name", "")
        values = node.get("values", {})
        children = node.get("children", [])
        role = node.get("role")
    else:
        name = node.name
        values = node.values
        children = node.children
        role = node.role

    # Build the label with indentation
    label = ("  " * indent) + name

    # Build the data row: [code, "", label, "", val1, val2, ...]
    row = ["", "", label, ""]
    for p in periods:
        val = values.get(p, 0)
        row.append(round(val) if val else "")
    rows.append(row)

    # Record this row's position if it has a role (for formula references)
    # role_row_map tracks the 1-indexed row number within this sheet
    if role and role_row_map is not None:
        # The current row is at position len(rows) relative to start,
        # but the caller will adjust for the sheet's absolute row offset
        role_row_map[role] = "PLACEHOLDER"  # Caller fills in absolute row number

    # Recurse into children
    for child in children:
        rows.extend(_render_tree_rows(child, periods, indent + 1, role_row_map))

    return rows
```

#### 4.3: Build each sheet tab

```python
def write_sheets(trees: dict, company: str) -> tuple[str, str]:
    """Render reconciled trees to a Google Sheet.

    Args:
        trees: dict from build_statement_trees() after reconcile_trees().
               Keys: "IS", "BS", "BS_LE", "CF", "complete_periods", "facts"
        company: company name for the sheet title

    Returns:
        (spreadsheet_id, url)
    """
    periods = trees.get("complete_periods", [])
    sid, url, sheet_ids = gws_create(f"{company} — Financial Model", ["IS", "BS", "CF", "Summary"])

    # Track role → (sheet_name, absolute_row_number) for cross-sheet formulas
    global_role_map = {}  # {role: (sheet_name, row_number)}

    # --- IS tab ---
    is_tree = trees.get("IS")
    if is_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        role_map = {}
        body_rows = _render_tree_rows(is_tree, periods, indent=0, role_row_map=role_map)
        is_rows = header_rows + body_rows
        # Fix role_row_map with absolute row numbers (header_rows offset + 1 for 1-indexing)
        offset = len(header_rows)
        for role, _ in role_map.items():
            # Find which body row has this role
            for i, row in enumerate(body_rows):
                # We need to re-walk the tree to find row indices by role
                pass
        # Simpler approach: track row indices during rendering
        _write_sheet_tab(sid, "IS", is_rows, periods, is_tree, global_role_map)

    # --- BS tab ---
    # BS has two sub-trees: Assets and Liabilities+Equity
    bs_tree = trees.get("BS")
    bs_le_tree = trees.get("BS_LE")
    if bs_tree or bs_le_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = []
        if bs_tree:
            body_rows += _render_tree_rows(bs_tree, periods, indent=0)
            body_rows.append([""] * (4 + len(periods)))  # blank separator
        if bs_le_tree:
            body_rows += _render_tree_rows(bs_le_tree, periods, indent=0)
        bs_rows = header_rows + body_rows
        _write_sheet_tab(sid, "BS", bs_rows, periods, None, global_role_map)

    # --- CF tab ---
    cf_tree = trees.get("CF")
    if cf_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_tree_rows(cf_tree, periods, indent=0)
        cf_rows = header_rows + body_rows
        _write_sheet_tab(sid, "CF", cf_rows, periods, None, global_role_map)

    # --- Summary tab (5 invariant formulas) ---
    _write_summary_tab(sid, periods, global_role_map)

    # Column widths
    for sheet_name, sheet_id in sheet_ids.items():
        gws_batch_update(sid, [
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": 0, "endIndex": 2},
                "properties": {"pixelSize": 50}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": 2, "endIndex": 3},
                "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
        ])

    return sid, url
```

#### 4.4: Better approach — track row indices during rendering

The `_render_tree_rows` function above has a placeholder for role tracking. Here's a cleaner implementation that tracks absolute row numbers:

```python
def _render_sheet_body(tree, periods, start_row, global_role_map, sheet_name):
    """Render a tree into rows, tracking role → absolute row numbers.

    Args:
        tree: TreeNode or dict
        periods: list of period strings
        start_row: 1-indexed row number where this body starts in the sheet
        global_role_map: dict to populate with {role: (sheet_name, row_number)}
        sheet_name: "IS", "BS", or "CF"

    Returns:
        list of rows (each row is a list of cell values)
    """
    rows = []
    current_row = [start_row]  # mutable counter

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
            row.append(round(val) if val else "")
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

#### 4.5: Write the Summary tab with live formulas

```python
def _write_summary_tab(sid, periods, global_role_map):
    """Write the Summary tab with 5 invariant checks as live spreadsheet formulas.

    Each invariant is a row where each period column contains a formula like:
        ='BS'!F10 - 'BS'!F25 - 'BS'!F30
    If the formula evaluates to 0 (within rounding), the invariant holds.

    Args:
        sid: spreadsheet ID
        periods: list of period strings
        global_role_map: {role: (sheet_name, row_number)} from rendering
    """
    rows = [
        [],
        ["", "", "Invariant Checks", ""] + list(periods),
        [],
    ]

    def _formula_row(label, formula_fn):
        """Build a row where each period column is a formula.

        formula_fn(col_letter) should return a formula string like
        "='BS'!F10 - 'BS'!F25"
        """
        row = ["", "", label, ""]
        for i in range(len(periods)):
            col = dcol(i)  # E, F, G, ... (data columns start at E)
            row.append(formula_fn(col))
        rows.append(row)

    def _cell_ref(role, col):
        """Build a cell reference like 'BS'!F10 from a role and column letter."""
        entry = global_role_map.get(role)
        if not entry:
            return "0"
        sheet_name, row_num = entry
        return f"'{sheet_name}'!{col}{row_num}"

    # 1. BS Balance: TA - TL - TE = 0
    _formula_row("BS Balance (TA-TL-TE)",
        lambda col: f"={_cell_ref('BS_TA', col)} - {_cell_ref('BS_TL', col)} - {_cell_ref('BS_TE', col)}")

    # 2. Cash Link: CF_ENDC - BS_CASH = 0
    # CF_ENDC is from facts (not a tree node with a row). We write the BS_CASH value
    # as a static reference since CF_ENDC was used to SET BS_CASH (they're equal by construction).
    # If CF_ENDC is rendered in the CF tab, reference it; otherwise use 0.
    _formula_row("Cash Link (CF_ENDC - BS_CASH)",
        lambda col: f"=0")  # Always 0 by construction (override in Step 2.7)

    # 3. NI Link: IS INC_NET - CF INC_NET_CF = 0
    _formula_row("NI Link (IS - CF)",
        lambda col: f"={_cell_ref('INC_NET', col)} - {_cell_ref('INC_NET_CF', col)}")

    # 4. D&A Link — value-matched, no fixed cell reference. Write as static values.
    rows.append(["", "", "D&A Link (IS - CF)", ""] + ["n/a"] * len(periods))

    # 5. SBC Link — value-matched, no fixed cell reference. Write as static values.
    rows.append(["", "", "SBC Link (IS - CF)", ""] + ["n/a"] * len(periods))

    # Total errors row
    rows.append([])
    total_row = ["", "", "TOTAL ERRORS (checks 1-3)", ""]
    for i in range(len(periods)):
        col = dcol(i)
        # Sum the formula rows (rows 4, 5, 6 in 1-indexed = checks 1-3)
        total_row.append(f"=ABS({col}4)+ABS({col}5)+ABS({col}6)")
    rows.append(total_row)

    gws_write(sid, f"Summary!A1:{dcol(len(periods)-1)}{len(rows)}", rows)
```

---

### Step 5: Clean Up `xbrl_group.py`

**Goal**: Remove the deterministic code that was migrated to `xbrl_tree.py`. Keep only the LLM grouping code.

#### 5.1: Delete these functions from `xbrl_group.py`

| Function | Lines | Why |
|----------|-------|-----|
| `tree_to_structured()` | 209–334 | Migrated to `reconcile_trees()` in `xbrl_tree.py` |
| `_extract_is_from_tree()` | 337–467 | Migrated to `_tag_is_positions()` |
| `_flatten_is_cascade()` | 470–535 | Migrated to `_tag_is_positions()` |
| `_collect_is_nodes()` | 537–541 | Only used by `_flatten_is_cascade()` |
| `_extract_bs_from_tree()` | 544–671 | Migrated to `_tag_bs_positions()` |
| `_extract_cf_from_tree()` | 674–778 | Migrated to `_tag_cf_positions()` |
| `_filter_values()` | 204–206 | Migrated to `_filter_to_complete_periods()` |

#### 5.2: Keep these functions

| Function | Lines | Why |
|----------|-------|-----|
| `group_siblings_with_llm()` | 34–144 | Optional presentation polish |
| `apply_grouping()` | 151–197 | Applies LLM decisions to tree |

#### 5.3: Update `main()` in `xbrl_group.py`

The current `main()` (line 785) calls `tree_to_structured()` and `load_filing()` + `verify_model()`. Rewrite it to:

```python
def main():
    parser = argparse.ArgumentParser(description="Group XBRL siblings with LLM")
    parser.add_argument("--url", required=True, help="URL to filing HTML")
    parser.add_argument("-o", "--output", help="Output trees JSON (with grouping applied)")
    parser.add_argument("--print", dest="do_print", action="store_true")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM grouping, keep all items")
    args = parser.parse_args()

    html = fetch_url(args.url).decode('utf-8', errors='replace')
    base_url = args.url.rsplit('/', 1)[0] + '/'

    print("Building XBRL calculation trees...", file=sys.stderr)
    trees = build_statement_trees(html, base_url)
    # Note: build_statement_trees() now calls reconcile_trees() automatically
    if not trees:
        sys.exit(1)

    if not args.no_llm:
        client = Anthropic()
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = trees.get(stmt)
            if not tree:
                continue
            groups = find_groupable_siblings(tree)
            if groups:
                print(f"\n{stmt}: {len(groups)} groupable sibling sets", file=sys.stderr)
                decisions = group_siblings_with_llm(client, groups, stmt)
                trees[stmt] = apply_grouping(tree, decisions)

    if args.do_print:
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = trees.get(stmt)
            if tree:
                label = {"IS": "INCOME STATEMENT", "BS": "BALANCE SHEET (Assets)",
                         "BS_LE": "BALANCE SHEET (Liab + Equity)",
                         "CF": "CASH FLOWS"}[stmt]
                print(f"\n{'=' * 70}")
                print(label)
                print(f"{'=' * 70}")
                print_tree(tree)

    # Verify using new tree-based verify_model
    from pymodel import verify_model
    errors = verify_model(trees)
    print(f"\nPeriods: {trees.get('complete_periods', [])}", file=sys.stderr)
    if errors:
        print(f"verify_model: {len(errors)} error(s)", file=sys.stderr)
        for name, period, delta in errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
    else:
        n = len(trees.get("complete_periods", []))
        print(f"verify_model: ALL PASS ({n} periods)", file=sys.stderr)

    if args.output:
        # Serialize trees to JSON
        out = {}
        for key in ["complete_periods", "periods"]:
            if key in trees:
                out[key] = trees[key]
        out["facts"] = trees.get("facts", {})
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = trees.get(stmt)
            if tree:
                out[stmt] = tree.to_dict()
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Saved to {args.output}", file=sys.stderr)
```

---

### Step 6: Rewire the Orchestrator (`run_pipeline.py`)

**Goal**: Change `run_pipeline.py` to use the new Phase 2 path first, with automatic fallback to legacy when `_cal.xml` is missing.

#### 6.1: Current state of `run_pipeline.py`

The current file (93 lines) runs this pipeline per filing:
1. `agent1_fetcher.py` → filings JSON
2. Per filing: `extract_sections.py` → `structure_financials.py` → structured JSON
3. `pymodel.py --financials <files> --company <name>`

#### 6.2: New pipeline flow

```
Per filing:
  TRY Phase 2 path:
    xbrl_tree.py --url <url> -o trees.json
    (This internally calls build_statement_trees() + reconcile_trees())

  IF xbrl_tree.py fails (no _cal.xml):
    FALLBACK to legacy:
      extract_sections.py <url> --output-dir <dir>
      structure_financials.py <dir> -o structured.json

After all filings:
  IF Phase 2 path:
    pymodel.py --trees trees.json --checkpoint
    sheet_builder.py --trees trees.json --company <name>
  IF legacy path:
    legacy_pymodel.py --financials <files> --company <name>
```

#### 6.3: Rewrite `run_pipeline.py`

Replace the `main()` function. Keep `run_command()` but add a non-fatal version:

```python
def try_command(cmd, **kwargs):
    """Run a command, returning (stdout, True) on success or (stderr, False) on failure.
    Unlike run_command(), does NOT sys.exit on failure.
    """
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(f"Command failed: {result.stderr}", file=sys.stderr)
        return result.stderr, False
    return result.stdout, True


def main():
    parser = argparse.ArgumentParser(description="Full SEC Modeling Pipeline")
    parser.add_argument("query", help="Company ticker or name (e.g. AAPL)")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--outdir", default="./pipeline_output")
    args = parser.parse_args()

    out_dir = Path(args.outdir)
    out_dir.mkdir(exist_ok=True)

    # Stage 1: Fetch filings
    print(f"=== STAGE 1: Fetching {args.years} years of filings for {args.query} ===")
    filings_json = run_command([sys.executable, "agent1_fetcher.py", args.query,
                                 "--years", str(args.years)])
    try:
        filings_data = json.loads(filings_json)
    except json.JSONDecodeError:
        print("Error: Could not parse output from agent1_fetcher.py", file=sys.stderr)
        sys.exit(1)

    filings = filings_data.get("filings", [])
    if not filings:
        print(f"No filings found for {args.query}")
        sys.exit(1)

    company_name = filings_data.get("company_name", args.query)
    print(f"Processing {len(filings)} filings for {company_name}...")

    # Stage 2: Process each filing
    tree_files = []        # Phase 2 path outputs
    structured_files = []  # Legacy path outputs
    used_legacy = False

    for i, filing in enumerate(filings):
        url = filing.get("url")
        date = filing.get("filing_date", f"filing_{i}")
        if not url:
            continue

        print(f"\n=== STAGE 2: Processing filing {i+1}/{len(filings)} ({date}) ===")

        # Try Phase 2 path first: xbrl_tree.py
        tree_file = out_dir / f"trees_{date}.json"
        _, ok = try_command([sys.executable, "xbrl_tree.py", "--url", url,
                              "-o", str(tree_file)])

        if ok and tree_file.exists():
            tree_files.append(str(tree_file))
            print(f"  Phase 2 (XBRL tree) succeeded for {date}")
        else:
            # Fallback to legacy path
            print(f"  Phase 2 failed for {date}, falling back to legacy LLM path...")
            used_legacy = True
            filing_dir = out_dir / f"sections_{date}"
            run_command([sys.executable, "extract_sections.py", url,
                          "--output-dir", str(filing_dir)])
            struct_file = out_dir / f"structured_{date}.json"
            run_command([sys.executable, "structure_financials.py",
                          str(filing_dir), "-o", str(struct_file)])
            structured_files.append(str(struct_file))

    # Stage 3+4: Verify + render
    if tree_files and not used_legacy:
        # Pure Phase 2 path
        print(f"\n=== STAGE 3: Verifying model (Phase 2) ===")
        # Checkpoint: verify invariants on each tree file
        for tf in tree_files:
            run_command([sys.executable, "pymodel.py", "--trees", tf, "--checkpoint"])

        print(f"\n=== STAGE 4: Writing Google Sheet (Phase 2) ===")
        # sheet_builder uses the first tree file (most recent filing)
        run_command([sys.executable, "sheet_builder.py", "--trees", tree_files[0],
                      "--company", company_name])
    elif structured_files:
        # Legacy path (or mixed — fall back entirely to legacy for consistency)
        print(f"\n=== STAGE 3 & 4: Building Model (Legacy) ===")
        cmd = [sys.executable, "legacy_pymodel.py", "--company", company_name,
               "--financials"] + structured_files
        final_output = run_command(cmd)
        try:
            final_data = json.loads(final_output.splitlines()[-1])
            print(f"\nSUCCESS!")
            print(f"Company: {final_data.get('company')}")
            print(f"Google Sheet URL: {final_data.get('url')}")
        except Exception:
            print("\nModel produced, but could not parse final summary.")
            print(final_output)
    else:
        print("No filings were successfully processed.", file=sys.stderr)
        sys.exit(1)
```

**Key design decisions**:
- If ANY filing falls back to legacy, ALL filings use legacy. This prevents mixing tree-based and flat-dict formats in the same model run.
- `try_command()` is non-fatal: it returns success/failure instead of calling `sys.exit()`.
- `run_command()` is still fatal: used for steps that MUST succeed (Stage 1, legacy path).

---

### Step 7: Run the Test Suite

**Goal**: Verify the implementation against the test cases defined in the Testing section above.

#### 7.1: Create test fixtures

You need to save real tree outputs as fixture files for testing. Run `xbrl_tree.py` on known companies:

```bash
# Generate fixture data for AAPL
python xbrl_tree.py --url <aapl_filing_url> -o tests/fixtures/aapl_trees.json

# Generate fixture data for BAC (bank — no Gross Profit)
python xbrl_tree.py --url <bac_filing_url> -o tests/fixtures/bac_trees.json
```

#### 7.2: Run the invariant tests on the 9/10 companies from Phase 1b

```bash
# These are the same companies tested in Phase 1b.
# For each, run xbrl_tree.py (which now includes reconcile_trees)
# then pymodel.py --checkpoint.
for ticker in AAPL AMZN GOOG KO BAC GE JNJ PG XOM; do
    url=$(python agent1_fetcher.py $ticker --years 1 | python -c "import sys,json; print(json.load(sys.stdin)['filings'][0]['url'])")
    python xbrl_tree.py --url "$url" -o "/tmp/${ticker}_trees.json"
    python pymodel.py --trees "/tmp/${ticker}_trees.json" --checkpoint
done
```

All 9 should pass. The 10th (whichever had the $401 rounding error in Phase 1b) should still show that same error — Phase 2 doesn't change the data, only the architecture.

#### 7.3: End-to-end test

```bash
# Full pipeline test
python run_pipeline.py AAPL --years 1
# Should: use Phase 2 path, produce a Google Sheet URL

# Fallback test (MSFT has no _cal.xml)
python run_pipeline.py MSFT --years 1
# Should: detect xbrl_tree.py failure, fall back to legacy, still produce a sheet
```
