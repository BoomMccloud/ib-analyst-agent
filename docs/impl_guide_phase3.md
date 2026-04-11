# Phase 3 Implementation Guide: Live Sheet Formulas

This is a step-by-step guide for implementing Phase 3. Every code snippet is included inline — you should not need to reference the spec while following this guide.

**Pre-requisite:** Read the spec (`docs/pipeline_phase3_spec.md`) first to understand the "why". This guide focuses on the "how".

---

## Dependency Order

```
Step 1: xbrl_tree.py (D&A/SBC/FX tagging)        ← can be done in parallel with Step 2
Step 2: sheet_builder.py (_build_weight_formula)   ← can be done in parallel with Step 1
Step 3: sheet_builder.py (_render_sheet_body)      ← depends on Step 2
Step 4: sheet_builder.py (CF cash proof rows)      ← depends on Steps 1 + 3
Step 5: sheet_builder.py (14 Check rows)           ← depends on Steps 1 + 3 + 4
Step 6: sheet_builder.py (Sheets API formatting)   ← depends on Step 5
Step 7: End-to-end verification                    ← depends on all
```

---

## Step 1: Tag D&A, SBC, and FX nodes in `xbrl_tree.py`

**File:** `./xbrl_tree.py`
**Goal:** Tag D&A, SBC, and FX nodes so the sheet builder can reference their row numbers in Check formulas.

### 1a. Add two helper functions

Insert these **above** `_tag_cf_positions()` (before line 476):

```python
def _find_leaf_by_keywords(tree: TreeNode, keywords: list[str]) -> TreeNode | None:
    """Find a leaf node whose name contains all keywords (case-insensitive).
    
    Example: _find_leaf_by_keywords(is_tree, ["depreciation"]) finds
    "Depreciation And Amortization" but not "Accumulated Depreciation" (which is on BS).
    """
    name_lower = tree.name.lower()
    if tree.is_leaf and all(kw in name_lower for kw in keywords):
        return tree
    for child in tree.children:
        result = _find_leaf_by_keywords(child, keywords)
        if result:
            return result
    return None


def _find_leaf_by_timeseries(tree: TreeNode, periods: list[str],
                              target_values: dict[str, float]) -> TreeNode | None:
    """Find a leaf node whose values match target across ALL periods (within 0.5).
    
    Why ALL periods? A single-period match risks collisions — e.g., D&A = $150M in 2024,
    but "Changes in Inventory" also happens to be $150M in 2024. Matching across ALL
    5 years eliminates this: a coincidental collision across 5 periods is near impossible.
    
    Example: If IS D&A has values {2020: 100, 2021: 110, 2022: 120, 2023: 130, 2024: 150},
    this finds the CF node with the same 5-year pattern.
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
        # ALL non-zero periods must match
        if total > 0 and matched == total:
            return tree
    for child in tree.children:
        result = _find_leaf_by_timeseries(child, periods, target_values)
        if result:
            return result
    return None
```

### 1b. Add `_tag_da_sbc_nodes()` function

Insert this **after** the two helpers you just added (still above `_tag_cf_positions()`):

```python
def _tag_da_sbc_nodes(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    """Tag D&A and SBC leaf nodes in IS and CF trees.
    
    Strategy:
    1. Find IS leaf by keyword (e.g., "depreciation" for D&A)
    2. Find matching CF leaf by full time-series value match (collision-safe)
    3. Tag both with roles (IS_DA/CF_DA, IS_SBC/CF_SBC)
    """
    if not is_tree or not cf_tree:
        return
    
    # We only search inside CF's Operating Cash Flow section for D&A and SBC,
    # because that's where they appear in the indirect method CF statement.
    cf_opcf = find_node_by_role(cf_tree, "CF_OPCF")
    if not cf_opcf:
        return
    
    # Get periods from IS tree (only non-zero periods)
    periods = [p for p in (is_tree.values.keys() if is_tree.values else [])
               if is_tree.values.get(p, 0) != 0]
    if not periods:
        return
    
    # --- D&A ---
    # Try "depreciation" first, fall back to "amortization"
    is_da = _find_leaf_by_keywords(is_tree, ["depreciation"])
    if not is_da:
        is_da = _find_leaf_by_keywords(is_tree, ["amortization"])
    
    if is_da:
        is_da.role = "IS_DA"
        # Find the matching CF node by full time-series match
        cf_da = _find_leaf_by_timeseries(cf_opcf, periods, is_da.values)
        if cf_da:
            cf_da.role = "CF_DA"
        else:
            print("WARNING: Could not find CF D&A node matching IS D&A values",
                  file=sys.stderr)
    
    # --- SBC ---
    # Try "stock" + "compensation" first, fall back to "share" + "compensation"
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

### 1c. Add FX tagging to `_tag_cf_positions()`

Open `_tag_cf_positions()` (line 476). Find this line inside the function (around line 524):

```python
    _walk_and_tag(cf_tree)
```

Insert the following **immediately after** that line:

```python
    # --- Tag FX impact node (if present) ---
    # Multinational companies have an "Effect of Exchange Rate" node as a sibling
    # of OPCF/INVCF/FINCF under the CF root. If omitted, the cash proof will show
    # non-zero errors for companies like AAPL, AMZN, GE.
    FX_PATTERNS = ["EffectOfExchangeRate", "EffectOfForeignExchangeRate"]
    for child in cf_tree.children:
        concept_name = child.concept.split('_', 1)[-1] if '_' in child.concept else child.concept
        for pat in FX_PATTERNS:
            if concept_name.startswith(pat) and child.values:
                child.role = "CF_FX"
                break
```

### 1d. Add Step F to `reconcile_trees()`

Open `reconcile_trees()` (line 663). Find this line (line 682):

```python
    return trees
```

Insert **before** `return trees`:

```python
    # --- Step F: Tag D&A and SBC nodes for sheet formula references ---
    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))
```

The function should now look like:

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

    # --- Step F: Tag D&A and SBC nodes for sheet formula references ---
    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))

    return trees
```

### 1e. Verify Step 1

Run the existing tests to make sure nothing broke:

```bash
pytest tests/test_model_historical.py -v
```

Then test manually:

```bash
python xbrl_tree.py --url <apple_filing_url> --print 2>&1 | grep -E '"role": "(IS_DA|CF_DA|IS_SBC|CF_SBC|CF_FX)"'
```

You should see all 5 roles in the output.

---

## Step 2: Create `tests/test_da_sbc_tagging.py`

**File:** `./tests/test_da_sbc_tagging.py` (new file)
**Goal:** Validate that D&A/SBC/FX tagging works correctly.

```python
"""
Phase 3 Tests: D&A, SBC, and FX node tagging
=============================================
Tests _tag_da_sbc_nodes() and FX tagging in _tag_cf_positions().
Uses synthetic tree fixtures (no network access).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_tree import TreeNode, reconcile_trees, find_node_by_role


# ---------------------------------------------------------------------------
# Fixture helpers (same pattern as test_model_historical.py)
# ---------------------------------------------------------------------------

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


PERIODS = ["2020", "2021", "2022", "2023", "2024"]
DA_VALUES = {"2020": 100, "2021": 110, "2022": 120, "2023": 130, "2024": 150}
SBC_VALUES = {"2020": 50, "2021": 55, "2022": 60, "2023": 65, "2024": 70}


def _build_trees_with_da_sbc():
    """Build a minimal 3-statement tree set with D&A and SBC nodes.
    
    IS tree:
      NetIncomeLoss (root)
        + Revenue (leaf)
        - CostOfRevenue (leaf)
        + DepreciationAndAmortization (leaf)    ← should become IS_DA
        + StockBasedCompensation (leaf)          ← should become IS_SBC
    
    CF tree:
      CashAndCashEquivalentsPeriodIncreaseDecrease (root = CF_NETCH)
        + NetCashProvidedByUsedInOperatingActivities (= CF_OPCF)
            + ProfitLoss (leaf = INC_NET_CF)
            + DepreciationDepletionAndAmortization (leaf)  ← should become CF_DA
            + ShareBasedCompensation (leaf)                 ← should become CF_SBC
            + ChangesInInventory (leaf, DIFFERENT values)   ← should NOT be tagged
        + NetCashProvidedByUsedInInvestingActivities (= CF_INVCF)
            + CapitalExpenditures (leaf)
        + NetCashProvidedByUsedInFinancingActivities (= CF_FINCF)
            + DividendsPaid (leaf)
        + EffectOfExchangeRateOnCash (leaf)     ← should become CF_FX
    
    BS tree: minimal, just enough for reconcile_trees() to not error.
    """
    ni_values = {"2020": 200, "2021": 220, "2022": 240, "2023": 260, "2024": 280}
    
    # IS tree
    is_tree = _make_parent("us-gaap_NetIncomeLoss", [
        _make_leaf("us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                   weight=1.0, values={"2020": 1000, "2021": 1100, "2022": 1200,
                                        "2023": 1300, "2024": 1400}),
        _make_leaf("us-gaap_CostOfGoodsAndServicesSold",
                   weight=-1.0, values={"2020": 400, "2021": 450, "2022": 500,
                                         "2023": 550, "2024": 600}),
        _make_leaf("us-gaap_DepreciationAndAmortization",
                   weight=1.0, values=DA_VALUES),
        _make_leaf("us-gaap_AllocatedShareBasedCompensationExpense",
                   weight=1.0, values=SBC_VALUES),
    ], values=ni_values)
    
    # CF tree
    # Inventory values: match D&A for 2024 only (collision test)
    inventory_values = {"2020": 30, "2021": 40, "2022": 50, "2023": 60, "2024": 150}
    
    opcf = _make_parent("us-gaap_NetCashProvidedByUsedInOperatingActivities", [
        _make_leaf("us-gaap_ProfitLoss", weight=1.0, values=ni_values),
        _make_leaf("us-gaap_DepreciationDepletionAndAmortization",
                   weight=1.0, values=DA_VALUES),
        _make_leaf("us-gaap_ShareBasedCompensation",
                   weight=1.0, values=SBC_VALUES),
        _make_leaf("us-gaap_IncreaseDecreaseInInventories",
                   weight=1.0, values=inventory_values),
    ], values={"2020": 380, "2021": 425, "2022": 470, "2023": 515, "2024": 650})
    
    invcf = _make_parent("us-gaap_NetCashProvidedByUsedInInvestingActivities", [
        _make_leaf("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
                   weight=-1.0, values={"2020": -80, "2021": -90, "2022": -100,
                                         "2023": -110, "2024": -120}),
    ], values={"2020": -80, "2021": -90, "2022": -100, "2023": -110, "2024": -120})
    
    fincf = _make_parent("us-gaap_NetCashProvidedByUsedInFinancingActivities", [
        _make_leaf("us-gaap_PaymentsOfDividends",
                   weight=-1.0, values={"2020": -50, "2021": -55, "2022": -60,
                                         "2023": -65, "2024": -70}),
    ], values={"2020": -50, "2021": -55, "2022": -60, "2023": -65, "2024": -70})
    
    fx_node = _make_leaf("us-gaap_EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                          weight=1.0, values={"2020": -5, "2021": -3, "2022": -8,
                                               "2023": -2, "2024": -6})
    
    cf_tree = _make_parent("us-gaap_CashCashEquivalentsPeriodIncreaseDecrease",
                            [opcf, invcf, fincf, fx_node],
                            values={"2020": 245, "2021": 277, "2022": 302,
                                    "2023": 338, "2024": 454})
    
    # Minimal BS trees
    bs_tree = _make_parent("us-gaap_Assets", [
        _make_leaf("us-gaap_CashAndCashEquivalentsAtCarryingValue",
                   values={"2020": 500, "2021": 600, "2022": 700, "2023": 800, "2024": 900}),
    ], values={"2020": 500, "2021": 600, "2022": 700, "2023": 800, "2024": 900})
    
    bs_le_tree = _make_parent("us-gaap_LiabilitiesAndStockholdersEquity", [
        _make_parent("us-gaap_Liabilities", [
            _make_leaf("us-gaap_AccountsPayable",
                       values={"2020": 200, "2021": 250, "2022": 300, "2023": 350, "2024": 400}),
        ], values={"2020": 200, "2021": 250, "2022": 300, "2023": 350, "2024": 400}),
        _make_parent("us-gaap_StockholdersEquity", [
            _make_leaf("us-gaap_CommonStockValue",
                       values={"2020": 300, "2021": 350, "2022": 400, "2023": 450, "2024": 500}),
        ], values={"2020": 300, "2021": 350, "2022": 400, "2023": 450, "2024": 500}),
    ], values={"2020": 500, "2021": 600, "2022": 700, "2023": 800, "2024": 900})
    
    trees = {
        "IS": is_tree,
        "CF": cf_tree,
        "BS": bs_tree,
        "BS_LE": bs_le_tree,
        "facts": {
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": {
                "2020": 500, "2021": 600, "2022": 700, "2023": 800, "2024": 900
            }
        },
    }
    return reconcile_trees(trees)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDATagging:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.trees = _build_trees_with_da_sbc()
    
    def test_is_da_tagged(self):
        """IS tree should have a leaf with role IS_DA containing 'depreciation'."""
        node = find_node_by_role(self.trees["IS"], "IS_DA")
        assert node is not None, "IS_DA role not found"
        assert node.is_leaf, "IS_DA should be a leaf node"
        assert "depreciation" in node.name.lower()
    
    def test_cf_da_tagged(self):
        """CF tree should have a leaf with role CF_DA."""
        node = find_node_by_role(self.trees["CF"], "CF_DA")
        assert node is not None, "CF_DA role not found"
        assert node.is_leaf, "CF_DA should be a leaf node"
    
    def test_da_values_match_across_statements(self):
        """IS_DA and CF_DA values must match across all complete periods."""
        is_da = find_node_by_role(self.trees["IS"], "IS_DA")
        cf_da = find_node_by_role(self.trees["CF"], "CF_DA")
        assert is_da and cf_da
        periods = self.trees.get("complete_periods", [])
        for p in periods:
            assert abs(is_da.values.get(p, 0) - cf_da.values.get(p, 0)) < 0.5, \
                f"D&A mismatch in {p}: IS={is_da.values.get(p)} vs CF={cf_da.values.get(p)}"


class TestSBCTagging:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.trees = _build_trees_with_da_sbc()
    
    def test_is_sbc_tagged(self):
        """IS tree should have a leaf with role IS_SBC."""
        node = find_node_by_role(self.trees["IS"], "IS_SBC")
        assert node is not None, "IS_SBC role not found"
        assert node.is_leaf
        name = node.name.lower()
        assert "stock" in name or "share" in name or "compensation" in name
    
    def test_cf_sbc_tagged(self):
        """CF tree should have a leaf with role CF_SBC."""
        node = find_node_by_role(self.trees["CF"], "CF_SBC")
        assert node is not None, "CF_SBC role not found"
        assert node.is_leaf
    
    def test_sbc_values_match_across_statements(self):
        """IS_SBC and CF_SBC values must match across all complete periods."""
        is_sbc = find_node_by_role(self.trees["IS"], "IS_SBC")
        cf_sbc = find_node_by_role(self.trees["CF"], "CF_SBC")
        assert is_sbc and cf_sbc
        periods = self.trees.get("complete_periods", [])
        for p in periods:
            assert abs(is_sbc.values.get(p, 0) - cf_sbc.values.get(p, 0)) < 0.5, \
                f"SBC mismatch in {p}"


class TestCollisionSafety:
    def test_timeseries_match_rejects_single_period_collision(self):
        """A CF node matching IS D&A for ONE period but not others must NOT be tagged.
        
        In our fixture, ChangesInInventory has value 150 in 2024 (same as D&A),
        but different values in all other years. It should NOT get the CF_DA role.
        """
        trees = _build_trees_with_da_sbc()
        cf_opcf = find_node_by_role(trees["CF"], "CF_OPCF")
        
        # Find the inventory node
        inventory_node = None
        for child in cf_opcf.children:
            if "inventor" in child.name.lower():
                inventory_node = child
                break
        
        assert inventory_node is not None, "Test fixture missing inventory node"
        assert inventory_node.role != "CF_DA", \
            "Inventory node was incorrectly tagged as CF_DA (single-period collision!)"


class TestFXTagging:
    def test_cf_fx_tagged_when_present(self):
        """CF_FX role assigned when EffectOfExchangeRate concept exists."""
        trees = _build_trees_with_da_sbc()
        node = find_node_by_role(trees["CF"], "CF_FX")
        assert node is not None, "CF_FX role not found"
        assert node.values  # should have values


class TestExistingRolesPreserved:
    def test_tagging_does_not_break_existing_roles(self):
        """Adding D&A/SBC/FX tagging must not break existing role assignments."""
        trees = _build_trees_with_da_sbc()
        
        # These roles should still exist from Phase 2 reconciliation
        expected = ["BS_TA", "BS_TL", "BS_TE", "CF_OPCF", "CF_INVCF",
                    "CF_FINCF", "CF_NETCH", "INC_NET_CF"]
        for role in expected:
            # Search in the appropriate tree
            found = False
            for tree_key in ["IS", "BS", "BS_LE", "CF"]:
                tree = trees.get(tree_key)
                if tree and find_node_by_role(tree, role):
                    found = True
                    break
            assert found, f"Role {role} missing after D&A/SBC tagging"
```

Run the tests:

```bash
pytest tests/test_da_sbc_tagging.py -v
```

All should pass if Step 1 was implemented correctly.

---

## Step 3: Implement `_build_weight_formula()` in `sheet_builder.py`

**File:** `./sheet_builder.py`
**Goal:** New function that converts XBRL tree weights into spreadsheet formula strings.

### 3a. Add the function

Insert this **after** `dcol()` (after line 23, before the old `_render_sheet_body()`):

```python
def _build_weight_formula(col: str, child_rows: list[tuple[int, float]]) -> str:
    """Build a cell formula from child row numbers and XBRL weights.
    
    Args:
        col: column letter (e.g., "E")
        child_rows: [(row_num, weight), ...] where weight is +1.0 or -1.0
    
    Returns:
        A formula string like "=SUM(E5:E7)", "=E5+E8", "=E4-E7", "=E5", or ""
    
    Examples:
        _build_weight_formula("E", [(5, 1.0), (6, 1.0), (7, 1.0)])  → "=SUM(E5:E7)"
        _build_weight_formula("E", [(5, 1.0), (8, 1.0)])             → "=E5+E8"
        _build_weight_formula("E", [(4, 1.0), (7, -1.0)])            → "=E4-E7"
        _build_weight_formula("E", [(5, 1.0)])                       → "=E5"
        _build_weight_formula("E", [])                               → ""
    """
    if not child_rows:
        return ""
    
    if len(child_rows) == 1:
        r, w = child_rows[0]
        return f"={col}{r}" if w == 1.0 else f"=-{col}{r}"
    
    # Check if all weights are +1 (simple SUM case)
    all_positive = all(w == 1.0 for _, w in child_rows)
    
    if all_positive:
        row_nums = [r for r, _ in child_rows]
        # Contiguous range → =SUM(E5:E7)
        if row_nums == list(range(row_nums[0], row_nums[-1] + 1)):
            return f"=SUM({col}{row_nums[0]}:{col}{row_nums[-1]})"
        # Non-contiguous → =E5+E8
        else:
            return "=" + "+".join(f"{col}{r}" for r, _ in child_rows)
    
    # Mixed weights → =E4-E7
    # Build each term with its sign, then strip the leading "+"
    parts = []
    for r, w in child_rows:
        sign = "+" if w == 1.0 else "-"
        parts.append(f"{sign}{col}{r}")
    
    return "=" + "".join(parts).lstrip("+")
```

### 3b. Add formula unit tests to `tests/test_sheet_formulas.py`

**File:** `./tests/test_sheet_formulas.py` (new file — start it now, we'll add more tests in later steps)

```python
"""
Phase 3 Tests: Sheet formula generation and two-pass rendering
==============================================================
Tests _build_weight_formula(), _render_sheet_body() (two-pass),
Check row formulas, and CF cash proof rows.
No Google Sheets API — tests formula strings and row structure.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheet_builder import _build_weight_formula, dcol


# ---------------------------------------------------------------------------
# _build_weight_formula tests
# ---------------------------------------------------------------------------

class TestBuildWeightFormula:
    def test_all_positive_contiguous(self):
        """3 contiguous +1 children → =SUM(E5:E7)"""
        result = _build_weight_formula("E", [(5, 1.0), (6, 1.0), (7, 1.0)])
        assert result == "=SUM(E5:E7)"
    
    def test_all_positive_noncontiguous(self):
        """2 non-contiguous +1 children → =E5+E8"""
        result = _build_weight_formula("E", [(5, 1.0), (8, 1.0)])
        assert result == "=E5+E8"
    
    def test_mixed_weights(self):
        """+1 and -1 children → =E4-E7"""
        result = _build_weight_formula("E", [(4, 1.0), (7, -1.0)])
        assert result == "=E4-E7"
    
    def test_single_child(self):
        """Single +1 child → =E5"""
        result = _build_weight_formula("E", [(5, 1.0)])
        assert result == "=E5"
    
    def test_single_child_negative(self):
        """Single -1 child → =-E5"""
        result = _build_weight_formula("E", [(5, -1.0)])
        assert result == "=-E5"
    
    def test_empty(self):
        """No children → empty string"""
        result = _build_weight_formula("E", [])
        assert result == ""
    
    def test_column_f(self):
        """Works with different column letters."""
        result = _build_weight_formula("F", [(5, 1.0), (6, 1.0)])
        assert result == "=SUM(F5:F6)"
    
    def test_three_mixed(self):
        """Three children: +1, +1, -1 → =E4+E5-E7"""
        result = _build_weight_formula("E", [(4, 1.0), (5, 1.0), (7, -1.0)])
        assert result == "=E4+E5-E7"
```

Run:

```bash
pytest tests/test_sheet_formulas.py -v
```

---

## Step 4: Rewrite `_render_sheet_body()` with two-pass architecture

**File:** `./sheet_builder.py`
**Goal:** Replace the old mutable-counter approach. Parents get formulas, leaves get values.

### 4a. Delete the old function and replace

Delete lines 25-59 (the entire old `_render_sheet_body()` function). Replace with:

```python
def _render_sheet_body(tree, periods, start_row, global_role_map, sheet_name):
    """Render a tree into rows. Leaves get values, parents get formulas.
    
    Uses two-pass rendering:
      Pass 1 (Layout): Walk the tree depth-first, assign a row number to every node.
      Pass 2 (Render): Build rows. Leaves → hardcoded values. Parents → formulas.
    
    Args:
        tree: TreeNode root of the statement tree
        periods: list of period strings (e.g., ["2020", "2021", ...])
        start_row: 1-indexed row number where this tree starts in the sheet
        global_role_map: dict to populate with role → (sheet_name, row_num)
        sheet_name: e.g., "IS", "BS", "CF"
    
    Returns:
        List of rows, where each row is ["", "", label, "", val_or_formula, ...]
    """
    # --- Pass 1: Layout ---
    # Assign a row number to each node in tree-display order (parent before children).
    # This is a flat list, NOT a recursive structure.
    layout = []  # [(row_num, indent, node)]
    
    def _assign_rows(node, indent=0):
        row_num = start_row + len(layout)
        layout.append((row_num, indent, node))
        for child in node.children:
            _assign_rows(child, indent + 1)
    
    _assign_rows(tree)
    
    # Build a lookup: node identity → row_num
    # We use id(node) because the same TreeNode object appears in both layout and
    # node.children — identity-based lookup is O(1) and correct.
    node_row = {id(entry[2]): entry[0] for entry in layout}
    
    # --- Pass 2: Render ---
    rows = []
    for row_num, indent, node in layout:
        label = ("  " * indent) + node.name
        
        # Record role → (sheet_name, row_num) for cross-tab formula references
        if node.role:
            global_role_map[node.role] = (sheet_name, row_num)
        
        # Every row starts with 4 prefix columns: ["", "", label, ""]
        row = ["", "", label, ""]
        
        if not node.children:
            # LEAF: hardcoded historical values from XBRL data
            for p in periods:
                val = node.values.get(p, 0)
                row.append(round(val) if val else "")
        else:
            # PARENT: formula referencing children's rows using XBRL weights
            child_rows = [(node_row[id(c)], c.weight) for c in node.children]
            for i in range(len(periods)):
                col = dcol(i)  # E, F, G, ...
                row.append(_build_weight_formula(col, child_rows))
        
        rows.append(row)
    
    return rows
```

### 4b. Add `prev_period()` helper

Insert after `_build_weight_formula()`:

```python
def prev_period(p: str, periods: list[str]) -> str | None:
    """Return the period immediately before p in the sorted periods list, or None.
    
    Example: prev_period("2022", ["2020", "2021", "2022", "2023"]) → "2021"
             prev_period("2020", ["2020", "2021", "2022", "2023"]) → None
    """
    idx = periods.index(p)
    return periods[idx - 1] if idx > 0 else None
```

### 4c. Add two-pass rendering tests

Append to `tests/test_sheet_formulas.py`:

```python
from xbrl_tree import TreeNode, reconcile_trees, find_node_by_role
from sheet_builder import _render_sheet_body


# ---------------------------------------------------------------------------
# Fixture helper (reuse from test_model_historical.py pattern)
# ---------------------------------------------------------------------------

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


PERIODS = ["2022", "2023", "2024"]


def _build_simple_tree():
    """Build a simple tree for rendering tests.
    
    Revenue (parent, weight=+1)
        Products (leaf, weight=+1)
        Services (leaf, weight=+1)
    """
    return _make_parent("us-gaap_Revenue", [
        _make_leaf("us-gaap_ProductRevenue", weight=1.0,
                   values={"2022": 300, "2023": 350, "2024": 400}),
        _make_leaf("us-gaap_ServiceRevenue", weight=1.0,
                   values={"2022": 100, "2023": 110, "2024": 120}),
    ], values={"2022": 400, "2023": 460, "2024": 520})


# ---------------------------------------------------------------------------
# Two-pass rendering tests
# ---------------------------------------------------------------------------

class TestTwoPassRendering:
    def test_leaf_cells_are_numbers(self):
        """Leaf rows must contain numbers (int/float), not formulas."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        # Rows 1 and 2 are children (leaves)
        for leaf_row in rows[1:]:  # skip parent row
            for cell in leaf_row[4:]:  # skip prefix columns
                assert not isinstance(cell, str) or not cell.startswith("="), \
                    f"Leaf cell should be a number, got: {cell}"
    
    def test_parent_cells_are_formulas(self):
        """Parent rows must contain formula strings starting with '='."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        parent_row = rows[0]  # first row = parent
        for cell in parent_row[4:]:  # data columns
            assert isinstance(cell, str) and cell.startswith("="), \
                f"Parent cell should be a formula, got: {cell}"
    
    def test_row_count_matches_tree_nodes(self):
        """Total rows = total nodes in tree (1 parent + 2 leaves = 3)."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        assert len(rows) == 3
    
    def test_two_pass_row_order_matches_tree_order(self):
        """Rows appear parent-first, then children depth-first."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        labels = [row[2].strip() for row in rows]
        assert labels[0] == "Revenue"
        assert labels[1] == "Product Revenue"
        assert labels[2] == "Service Revenue"
    
    def test_row_format_four_prefix_columns(self):
        """Every row starts with ["", "", label, ""]."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        for row in rows:
            assert row[0] == ""
            assert row[1] == ""
            assert isinstance(row[2], str) and len(row[2]) > 0
            assert row[3] == ""
    
    def test_indentation_matches_depth(self):
        """Depth-0 node has no indent, depth-1 nodes have 2-space indent."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        assert not rows[0][2].startswith(" "), "Root should have no indent"
        assert rows[1][2].startswith("  "), "Child should have 2-space indent"
        assert not rows[1][2].startswith("    "), "Child should have exactly 2-space indent"
    
    def test_role_map_populated(self):
        """Roles from the tree should appear in global_role_map."""
        tree = _build_simple_tree()
        tree.role = "IS_REVENUE"
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        assert "IS_REVENUE" in role_map
        assert role_map["IS_REVENUE"] == ("IS", 4)
    
    def test_parent_formula_references_correct_rows(self):
        """Parent formula should reference its children's actual row numbers."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        # Parent is row 4, children are rows 5 and 6
        # Children are contiguous +1, so formula should be =SUM(E5:E6)
        parent_formula = rows[0][4]  # first data column
        assert parent_formula == "=SUM(E5:E6)", f"Expected =SUM(E5:E6), got {parent_formula}"
    
    def test_data_columns_start_at_E(self):
        """Data values/formulas begin at index 4 (column E)."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        assert len(rows[0]) == 4 + len(PERIODS)
```

Run:

```bash
pytest tests/test_sheet_formulas.py -v
```

---

## Step 5: Add CF cash proof rows

**File:** `./sheet_builder.py`
**Goal:** Add Beginning Cash, Net Change, FX Impact, Ending Cash rows below the CF tree.

### 5a. Modify the CF tab section of `write_sheets()`

Find the CF tab section in `write_sheets()` (currently lines 149-154):

```python
    # --- CF tab ---
    cf_tree = trees.get("CF")
    if cf_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_sheet_body(cf_tree, periods, start_row=len(header_rows)+1, global_role_map=global_role_map, sheet_name="CF")
        cf_rows = header_rows + body_rows
        _write_sheet_tab(sid, "CF", cf_rows, periods, None, global_role_map)
```

Replace with:

```python
    # --- CF tab ---
    cf_tree = trees.get("CF")
    if cf_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_sheet_body(cf_tree, periods, start_row=len(header_rows)+1,
                                        global_role_map=global_role_map, sheet_name="CF")
        
        # --- CF Cash Proof Rows ---
        # These 4 rows sit below the CF tree and prove: ENDC = BEGC + NETCH + FX
        current_row = len(header_rows) + len(body_rows) + 1
        
        # Blank separator row
        body_rows.append([""] * (4 + len(periods)))
        current_row += 1
        
        # 1. Beginning Cash (hardcoded from XBRL facts — instant context values)
        cf_endc_values = trees.get("cf_endc_values", {})
        begc_row_num = current_row
        begc_row = ["", "", "Beginning Cash", ""]
        for p in periods:
            prev_p = prev_period(p, periods)
            # BEGC for period X = ENDC of the previous period
            begc_row.append(round(cf_endc_values.get(prev_p, 0)) if prev_p else "")
        body_rows.append(begc_row)
        global_role_map["CF_BEGC"] = ("CF", begc_row_num)
        current_row += 1
        
        # 2. Net Change in Cash = reference CF tree root (CF_NETCH)
        netch_row_num = current_row
        netch_ref = global_role_map.get("CF_NETCH")  # tagged during reconcile_trees()
        netch_row = ["", "", "Net Change in Cash", ""]
        for i in range(len(periods)):
            col = dcol(i)
            if netch_ref:
                netch_row.append(f"={col}{netch_ref[1]}")  # e.g., =E4
            else:
                netch_row.append("")
        body_rows.append(netch_row)
        current_row += 1
        
        # 3. FX Impact (if multinational company has CF_FX node; else 0)
        fx_ref = global_role_map.get("CF_FX")  # tagged during reconcile_trees()
        cf_fx_values = trees.get("cf_fx_values")  # fallback from facts dict
        fx_row_num = current_row
        fx_row = ["", "", "FX Impact", ""]
        for i in range(len(periods)):
            col = dcol(i)
            if fx_ref:
                fx_row.append(f"={col}{fx_ref[1]}")  # reference the FX node in the tree
            elif cf_fx_values:
                fx_row.append(round(cf_fx_values.get(periods[i], 0)))
            else:
                fx_row.append(0)  # domestic company, no FX impact
        body_rows.append(fx_row)
        global_role_map["CF_FX_PROOF"] = ("CF", fx_row_num)
        current_row += 1
        
        # 4. Ending Cash = BEGC + NETCH + FX (always a formula)
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

### 5b. Add CF cash proof tests

Append to `tests/test_sheet_formulas.py`:

```python
# ---------------------------------------------------------------------------
# CF Cash Proof tests
# ---------------------------------------------------------------------------

class TestCFCashProof:
    """Test the CF cash proof rows (BEGC, NETCH, FX, ENDC)."""
    
    # TODO: These tests require the full write_sheets() flow.
    # For now, test the individual row construction logic.
    # Full integration tests will be added in Step 7.
    
    def test_prev_period_middle(self):
        from sheet_builder import prev_period
        assert prev_period("2023", ["2021", "2022", "2023", "2024"]) == "2022"
    
    def test_prev_period_first(self):
        from sheet_builder import prev_period
        assert prev_period("2021", ["2021", "2022", "2023"]) is None
    
    def test_prev_period_last(self):
        from sheet_builder import prev_period
        assert prev_period("2024", ["2021", "2022", "2023", "2024"]) == "2023"
```

---

## Step 6: Inject 14 In-Line Check Rows

**File:** `./sheet_builder.py`
**Goal:** Add Check rows in-line after each section total.

### 6a. Extract `_cell_ref()` to module level

The current `_cell_ref()` is nested inside `_write_summary_tab()`. Move it to module level so all Check row functions can use it:

```python
def _cell_ref(role, col, global_role_map):
    """Build a cross-sheet cell reference from the role map.
    
    Args:
        role: e.g., "BS_TA", "CF_OPCF"
        col: column letter, e.g., "E"
        global_role_map: {role: (sheet_name, row_num)}
    
    Returns:
        e.g., "'BS'!E5" or "0" if role not found
    """
    entry = global_role_map.get(role)
    if not entry:
        return "0"
    sheet_name, row_num = entry
    return f"'{sheet_name}'!{col}{row_num}"
```

### 6b. Add `_add_check_row()` helper

```python
def _add_check_row(rows, periods, formula_fn):
    """Append a 'Check' row with formulas.
    
    Args:
        rows: list to append to
        periods: list of period strings
        formula_fn: callable(col: str) → formula string or None
    """
    row = ["", "", "Check", ""]
    for i in range(len(periods)):
        col = dcol(i)
        f = formula_fn(col)
        row.append(f if f else "")
    rows.append(row)
```

### 6c. Define the 14 Check formula functions

These are defined as closures inside `write_sheets()` (since they need access to `global_role_map`). Add them after `global_role_map = {}`:

```python
    # --- Check formula closures (all 14 invariants) ---
    # Each returns a formula string for a given column letter.
    
    def _ref(role, col):
        """Shortcut for _cell_ref with this function's global_role_map."""
        return _cell_ref(role, col, global_role_map)
    
    # IS (2 checks)
    def is_revenue_check(col):
        r1, r2 = _ref("IS_COMPUTED_REVENUE", col), _ref("IS_REVENUE", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    def is_cogs_check(col):
        r1, r2 = _ref("IS_COMPUTED_COGS", col), _ref("IS_COGS", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    # BS (4 checks)
    def bs_ta_check(col):
        r1, r2 = _ref("SUMM_TA", col), _ref("BS_TA", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    def bs_tl_check(col):
        r1, r2 = _ref("SUMM_TL", col), _ref("BS_TL", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    def bs_balance_check(col):
        tle, ta = _ref("BS_TLE", col), _ref("BS_TA", col)
        return f"={tle}-{ta}" if tle != "0" and ta != "0" else None
    
    def bs_equity_check(col):
        r1, r2 = _ref("BS_COMPUTED_TE", col), _ref("BS_TE", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    # CF (3 checks)
    def cf_opcf_check(col):
        r1, r2 = _ref("CF_COMPUTED_OPCF", col), _ref("CF_OPCF", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    def cf_invcf_check(col):
        r1, r2 = _ref("CF_COMPUTED_INVCF", col), _ref("CF_INVCF", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    def cf_fincf_check(col):
        r1, r2 = _ref("CF_COMPUTED_FINCF", col), _ref("CF_FINCF", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    # 3-Statement Summary (5 checks)
    def summ_ta_check(col):
        r1, r2 = _ref("SUMM_TA", col), _ref("BS_TA", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    def summ_tl_check(col):
        r1, r2 = _ref("SUMM_TL", col), _ref("BS_TL", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    def summ_balance_check(col):
        tle, ta = _ref("SUMM_TLE", col), _ref("SUMM_TA", col)
        return f"={tle}-{ta}" if tle != "0" and ta != "0" else None
    
    def summ_opcf_check(col):
        r1, r2 = _ref("SUMM_OPCF", col), _ref("CF_OPCF", col)
        return f"={r1}-{r2}" if r1 != "0" and r2 != "0" else None
    
    def summ_cash_proof(col):
        begc = _ref("SUMM_BEGC", col)
        endc = _ref("SUMM_ENDC", col)
        netch = _ref("SUMM_NETCH", col)
        if begc != "0" and endc != "0" and netch != "0":
            return f"={begc}-{endc}+{netch}"
        return None
```

### 6d. Inject Check rows after each section

In the IS tab section of `write_sheets()`, after rendering body rows:

```python
    # --- IS tab ---
    is_tree = trees.get("IS")
    if is_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_sheet_body(is_tree, periods, start_row=len(header_rows)+1,
                                        global_role_map=global_role_map, sheet_name="IS")
        
        # Inject IS Check rows after the body
        _add_check_row(body_rows, periods, is_revenue_check)
        _add_check_row(body_rows, periods, is_cogs_check)
        
        is_rows = header_rows + body_rows
        _write_sheet_tab(sid, "IS", is_rows, periods, is_tree, global_role_map)
```

Similarly for BS and CF tabs — inject the appropriate Check rows after each section's body.

### 6e. Replace `_write_summary_tab()` with 3-Statement Summary

Delete the old `_write_summary_tab()` (lines 65-112) and replace with a new version that:
1. Renders summary rows referencing the IS, BS, CF tabs
2. Injects 5 Check rows in-line
3. Registers `SUMM_*` roles in `global_role_map`

The exact implementation depends on your summary layout design. The key requirement is that each summary section ends with a Check row using the formula functions defined above.

---

## Step 7: Apply Google Sheets Formatting

**File:** `./sheet_builder.py`
**Goal:** Apply number formats and text styles via the Sheets API `batchUpdate`.

### 7a. Add `_build_format_requests()`

Add this function to `sheet_builder.py`:

```python
def _build_format_requests(sheet_id, rows, periods):
    """Build batchUpdate requests for number formatting and text styling.
    
    Args:
        sheet_id: integer sheet ID from gws_create()
        rows: the full list of rows for this tab (including headers)
        periods: list of period strings (determines data column count)
    
    Returns:
        List of request dicts for gws_batch_update()
    """
    requests = []
    num_data_cols = len(periods)
    data_start_col = 4  # column E = index 4
    
    for row_idx, row in enumerate(rows):
        if len(row) < 3:
            continue
        label = row[2].strip() if isinstance(row[2], str) else ""
        
        # --- Check rows: zero-dash format + italic label ---
        if label == "Check":
            # Number format: 0 renders as "-"
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx,
                        "endRowIndex": row_idx + 1,
                        "startColumnIndex": data_start_col,
                        "endColumnIndex": data_start_col + num_data_cols
                    },
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "0.0x;(0.0x);-"}
                    }},
                    "fields": "userEnteredFormat.numberFormat"
                }
            })
            # Italic label
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx,
                        "endRowIndex": row_idx + 1,
                        "startColumnIndex": 2,  # column C
                        "endColumnIndex": 3
                    },
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"italic": True}
                    }},
                    "fields": "userEnteredFormat.textFormat.italic"
                }
            })
        
        # --- Metrics rows: italic label ---
        elif label == "Metrics":
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx,
                        "endRowIndex": row_idx + 1,
                        "startColumnIndex": 2,
                        "endColumnIndex": 3
                    },
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"italic": True}
                    }},
                    "fields": "userEnteredFormat.textFormat.italic"
                }
            })
        
        # --- Standard number format for all data rows ---
        # (Check rows already have their own format above, so skip them)
        elif label and label != "$m" and label != "Check":
            # Check if any data cell is a number (leaf) vs formula (parent)
            has_data = any(
                isinstance(cell, (int, float)) or
                (isinstance(cell, str) and cell.startswith("="))
                for cell in row[4:]
            )
            if has_data:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx,
                            "endRowIndex": row_idx + 1,
                            "startColumnIndex": data_start_col,
                            "endColumnIndex": data_start_col + num_data_cols
                        },
                        "cell": {"userEnteredFormat": {
                            "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}
                        }},
                        "fields": "userEnteredFormat.numberFormat"
                    }
                })
    
    return requests
```

### 7b. Call `_build_format_requests()` in `write_sheets()`

In the existing column-width batchUpdate section at the bottom of `write_sheets()`, add format requests:

```python
    # Column widths + formatting
    requests = []
    for sheet_name, sheet_id in sheet_ids.items():
        # Column widths (existing)
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
    
    # Number formatting + text styling (new)
    # You need to track the rows for each tab — e.g., store them in a dict:
    # tab_rows = {"IS": is_rows, "BS": bs_rows, "CF": cf_rows, "Summary": summ_rows}
    for sheet_name, sheet_id in sheet_ids.items():
        tab_data = tab_rows.get(sheet_name, [])
        if tab_data:
            requests.extend(_build_format_requests(sheet_id, tab_data, periods))
    
    gws_batch_update(sid, requests)
```

**Note:** You'll need to collect `tab_rows` as a dict during the tab rendering loop. Store each tab's final rows:

```python
    tab_rows = {}
    
    # ... in the IS section:
    tab_rows["IS"] = is_rows
    
    # ... in the BS section:
    tab_rows["BS"] = bs_rows
    
    # ... etc.
```

---

## Step 8: End-to-End Verification

### 8a. Run all tests

```bash
# All tests (should all pass)
pytest tests/ -v

# Phase 3 specific
pytest tests/test_sheet_formulas.py tests/test_da_sbc_tagging.py -v

# Regression check
pytest tests/test_model_historical.py -v
```

### 8b. Manual verification on Apple

```bash
python sheet_builder.py --trees aapl_trees.json --company "Apple Inc."
```

Open the Google Sheet and verify:

1. **IS tab:** Click on Revenue cell → should show `=SUM(...)` or `=child1+child2` (not a number)
2. **IS tab:** Click on Gross Profit → should show formula with Revenue and COGS refs
3. **BS tab:** Click on Total Assets → should show formula with TCA and TNCA refs
4. **CF tab:** Bottom should have: Beginning Cash (number), Net Change (formula), FX Impact (formula), Ending Cash (formula)
5. **All tabs:** Every "Check" row shows `-` (zero) across every period
6. **Count Check rows:**
   - IS: 2 Check rows (after Revenue, after COGS)
   - BS: 4 Check rows (after TA, after TL, after TL+TE, after equity rollforward)
   - CF: 3 Check rows (after OPCF, after INVCF, after FINCF)
   - Summary: 5 Check rows (TA, TL, BS balance, OPCF decomposition, cash proof)

### 8c. Cross-industry verification

```bash
python sheet_builder.py --trees ko_trees.json --company "The Coca-Cola Company"
python sheet_builder.py --trees bac_trees.json --company "Bank of America"
```

- KO: Tree-weight formulas work for non-tech companies
- BAC: Banks don't have COGS/GP separation — IS renders without GP row, IS COGS Check may be absent

---

## Checklist

Use this to track your progress:

- [ ] **Step 1a:** Added `_find_leaf_by_keywords()` to `xbrl_tree.py`
- [ ] **Step 1a:** Added `_find_leaf_by_timeseries()` to `xbrl_tree.py`
- [ ] **Step 1b:** Added `_tag_da_sbc_nodes()` to `xbrl_tree.py`
- [ ] **Step 1c:** Added FX tagging to `_tag_cf_positions()` in `xbrl_tree.py`
- [ ] **Step 1d:** Added Step F call to `reconcile_trees()` in `xbrl_tree.py`
- [ ] **Step 1e:** Existing tests still pass (`pytest tests/test_model_historical.py`)
- [ ] **Step 2:** Created `tests/test_da_sbc_tagging.py` — all tests pass
- [ ] **Step 3a:** Added `_build_weight_formula()` to `sheet_builder.py`
- [ ] **Step 3b:** Formula unit tests pass (`pytest tests/test_sheet_formulas.py`)
- [ ] **Step 4a:** Replaced `_render_sheet_body()` with two-pass version
- [ ] **Step 4b:** Added `prev_period()` helper
- [ ] **Step 4c:** Two-pass rendering tests pass
- [ ] **Step 5a:** Modified CF tab section with cash proof rows
- [ ] **Step 5b:** CF cash proof tests pass
- [ ] **Step 6a:** Extracted `_cell_ref()` to module level
- [ ] **Step 6b:** Added `_add_check_row()` helper
- [ ] **Step 6c:** Defined 14 Check formula functions
- [ ] **Step 6d:** Injected Check rows in IS, BS, CF tabs
- [ ] **Step 6e:** Replaced `_write_summary_tab()` with new 3-Statement Summary
- [ ] **Step 7a:** Added `_build_format_requests()`
- [ ] **Step 7b:** Wired formatting into `write_sheets()` batchUpdate
- [ ] **Step 8a:** All tests pass (`pytest tests/ -v`)
- [ ] **Step 8b:** Manual Apple sheet verification passes
- [ ] **Step 8c:** Cross-industry verification (KO, BAC) passes
