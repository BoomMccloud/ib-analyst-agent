# Priority 1 Undo-Revert: Restore verify_model to fv()-based checks

**Date**: 2026-04-12
**Context**: `pymodel.py` was reverted to the pre-upgrade version. The test suite (`test_pymodel_units.py`, `test_merge_pipeline.py`) was written for the upgraded version. This spec documents the exact delta to restore the intended state.
**Parent spec**: `spec_multi_year_merge.md` — "Priority 1 (original): Fix verification" (marked ✅ DONE but code was reverted)

---

## Problem

`pymodel.py:verify_model()` currently uses `nv()` (declared node values) for all checks. The sheet uses `=SUM(children)` formulas. These diverge whenever children don't sum to the parent's declared value. The tests expect `fv()` (formula values = what the sheet actually shows).

### Current state (broken)

```python
def verify_model(trees):
    # Uses nv() — reads node.values[period] directly
    # D&A/SBC checks use _find_is_value_by_label / _find_cf_match_by_value (heuristic)
    # No Cash Begin check
    # No segment sums check
    # Periods not sorted
    # 5 checks total
```

### Target state (what tests expect)

```python
def verify_model(trees):
    # Uses fv() — computes SUM(children) recursively, matching sheet formulas
    # D&A/SBC checks use role tags (IS_DA, CF_DA, IS_SBC, CF_SBC)
    # Cash Begin check: CF_BEGC[t] == fv(BS_CASH)[t-1]
    # Calls _verify_segment_sums for IS Revenue/COGS
    # Periods sorted
    # 7 checks total
```

### Failing tests (20 failures)

| Test file | Failures | Why |
|-----------|----------|-----|
| `test_pymodel_units.py` | 3+ | Imports `_verify_segment_sums`, expects `fv()` behavior, expects role-tag D&A, expects Cash Begin |
| `test_merge_pipeline.py` | 5+ | Imports `_verify_segment_sums`, expects `fv()` segment checking |
| `test_model_historical.py` | 2 | Expects `fv()` in BS Balance and NI Link |
| `test_offline_e2e.py` | 1 | PFE $1 rounding (tolerance) |

---

## Changes to `pymodel.py`

### 1. Sort periods

```python
# CURRENT:
periods = trees.get("complete_periods", [])

# TARGET:
periods = sorted(trees.get("complete_periods", []))
```

### 2. Add `fv()` helper

Add inside `verify_model()`, after `nv()`:

```python
def fv(node, period):
    """Formula value: what =SUM(children) would produce in the sheet.
    Falls back to declared value for leaves."""
    if node is None:
        return 0
    if not node.children:
        return node.values.get(period, 0)
    return sum(fv(c, period) * c.weight for c in node.children)
```

### 3. Add role-tag node lookups

Add after existing role lookups:

```python
is_da = find_node_by_role(trees["IS"], "IS_DA") if trees.get("IS") else None
cf_da = find_node_by_role(trees["CF"], "CF_DA") if trees.get("CF") else None
is_sbc = find_node_by_role(trees["IS"], "IS_SBC") if trees.get("IS") else None
cf_sbc = find_node_by_role(trees["CF"], "CF_SBC") if trees.get("CF") else None
cf_begc = find_node_by_role(trees["CF"], "CF_BEGC") if trees.get("CF") else None
```

### 4. Replace `nv()` with `fv()` in checks 1-3

```python
# Check 1: BS Balance
check("BS Balance (TA-TL-TE)", p, fv(bs_ta, p) - fv(bs_tl, p) - fv(bs_te, p))

# Check 2: Cash Link
check("Cash (CF_ENDC - BS_CASH)", p, cf_endc - fv(bs_cash, p))

# Check 3: NI Link
is_ni = fv(inc_net_is, p)
cf_ni = fv(inc_net_cf, p)
```

### 5. Replace D&A/SBC heuristic checks with role-tag checks

```python
# Check 4: D&A Link (role-tag-based)
if is_da and cf_da:
    is_da_val = fv(is_da, p)
    cf_da_val = fv(cf_da, p)
    if is_da_val != 0:
        check("D&A Link (IS - CF)", p, is_da_val - cf_da_val)

# Check 5: SBC Link (role-tag-based)
if is_sbc and cf_sbc:
    is_sbc_val = fv(is_sbc, p)
    cf_sbc_val = fv(cf_sbc, p)
    if is_sbc_val != 0:
        check("SBC Link (IS - CF)", p, is_sbc_val - cf_sbc_val)
```

### 6. Add Cash Begin check

```python
# Check 6: Cash Begin: CF_BEGC[t] == BS_CASH[t-1]
if cf_begc and bs_cash and len(periods) > 1:
    p_idx = periods.index(p)
    if p_idx > 0:
        prev_p = periods[p_idx - 1]
        begc_val = fv(cf_begc, p)
        bs_cash_prev = fv(bs_cash, prev_p)
        if begc_val != 0 and bs_cash_prev != 0:
            check("Cash Begin (CF_BEGC - BS_CASH[t-1])", p,
                  begc_val - bs_cash_prev)
```

### 7. Add segment sums check (after the per-period loop)

```python
# Check 7: Segment sums
is_rev = find_node_by_role(trees["IS"], "IS_REVENUE") if trees.get("IS") else None
is_cogs = find_node_by_role(trees["IS"], "IS_COGS") if trees.get("IS") else None
for label, node in [("IS Revenue", is_rev), ("IS COGS", is_cogs)]:
    if node and node.children:
        _verify_segment_sums(node, periods, errors, label_prefix=label)
```

### 8. Delete dead code

Remove `_find_is_value_by_label` and `_find_cf_match_by_value`. They are replaced by role-tag lookups.

### 9. Update docstring

```python
def verify_model(trees: dict) -> list[tuple]:
    """Run 7 cross-statement invariant checks on reconciled trees using fv().
    ...
```

---

## What stays the same

- `_verify_segment_sums()` — already restored in working tree
- `check()` with tolerance 1.0 — already set
- `cf_endc_values` lookup — unchanged
- `main()` — unchanged

---

## Testing

### Success condition

```bash
python -m pytest tests/test_pymodel_units.py tests/test_merge_pipeline.py tests/test_model_historical.py tests/test_offline_e2e.py -v
# All pass (except test_offline_e2e PFE if SEC_CONTACT_EMAIL not set)
```

### Regression

```bash
python -m pytest tests/ --ignore=tests/test_model_historical_legacy.py -v
# 104 pass, 0 fail
```

### Validation

```bash
for ticker in NFLX AAPL MSFT AMZN GOOG META PFE; do
  python pymodel.py --trees pipeline_output/validation/$ticker/merged.json --checkpoint
done
# All should show ALL PASS
```
