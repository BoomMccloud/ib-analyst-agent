# Reclassification Detection — Spec

**Date**: 2026-04-12
**Parent spec**: `spec_multi_year_merge.md` — Phase 1, Step 1c (Priority 1)
**POC**: `poc_reclassification.py` — validated on TSLA, reduced NI Link errors from 96K to <150

---

## Problem

After merging 5 TSLA filings, `verify_model` reports:

```
NI Link (IS - CF): 2023-12-31 = 96,796
IS Revenue (Revenues): 2023-12-31 = -96,773
```

### Root cause

TSLA changed its revenue concept between filings:

| Filing | IS_REVENUE concept | Periods |
|--------|-------------------|---------|
| 2022 (filed 2022-02-07) | `us-gaap_Revenues` | 2020, 2021 |
| 2023 (filed 2023-01-31) | `us-gaap_Revenues` | 2021, 2022 |
| 2024 (filed 2024-01-29) | `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax` | 2022, 2023 |
| 2025 (filed 2025-01-30) | `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax` | 2023, 2024 |
| 2026 (filed 2026-01-29) | `us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax` | 2024, 2025 |

In the 2023 filing, `Revenues` is the parent and `RevenueFromContractWithCustomer...` is its empty child. In the 2024 filing, `RevenueFromContractWithCustomer...` IS the revenue node (same value at overlap period 2022-12-31: 81,462). `Revenues` no longer exists.

### What the current merge produces

The newest filing (2026) is the skeleton. It has `RevenueFromContractWithCustomer...` but not `Revenues`. During merge:

1. `Revenues` comes from older filings as an orphan, gets inserted under `GrossProfit`
2. `RevenueFromContractWithCustomer...` becomes its child (preserving the old structure)
3. `Revenues` has values for 2020-2022 only (from older filings)
4. `RevenueFromContractWithCustomer...` has values for 2022-2025 (from newer filings)
5. For 2023: `Revenues.declared = 0`, but `fv(Revenues) = 96,773` (from child)
6. `__OTHER__GrossProfit` absorbs the full revenue: `GrossProfit.declared(17,660) - 0 + 79,113 = 96,773`
7. This cascades: inflated GrossProfit → inflated INC_NET → NI Link fails

### Why existing rename detection doesn't catch it

`_build_rename_map()` (merge_trees.py:66) only detects renames when a concept in the older filing **doesn't exist** in the newer filing. But both `Revenues` and `RevenueFromContractWithCustomer...` exist in the 2023 filing. The rename detector sees them as two separate, co-existing concepts.

This is a structural reporting shift. Companies refine tags over time in three common structural patterns:
1. **Parent-Child Promotion:** The child concept was promoted to replace its parent (The TSLA case).
2. **Sibling Replacement:** A new concept replaces a sibling concept in the transition year.
3. **1-to-N Concept Split:** A parent concept (e.g., `Revenues`) is retired and split into multiple new children (e.g., `ProductRevenue` and `ServiceRevenue`).

---

## Design

### Detection: Structural Shift Scan

After merge Pass 3 (value population) and before Pass 5 (residual recomputation), scan the merged tree for structural reporting shifts based purely on overlapping mathematical values.

#### Pattern 1: Parent-Child Promotion & Pattern 2: Sibling Replacement
Detection criteria (all must be true):
1. Node A (old) and Node B (new) share the same value at some overlap period P (`abs(A.values[P] - B.values[P]) < 1.0`).
2. The shared value is non-zero.
3. Node B extends into newer periods that Node A lacks.
4. Node A and Node B share a structural relationship (Parent-Child, or share the same Parent).
5. Neither is an `__OTHER__` residual node.

When detected, Node B is the successor.
**Fix:**
1. Copy Node A's older-period values into Node B.
2. Transfer Node A's role to Node B.
3. Replace Node A with Node B in the tree hierarchy (Node B adopts Node A's position and any non-residual children).

#### Pattern 3: 1-to-N Concept Split — DEFERRED

> **Descoped per KISS evaluation**: No real-world case observed yet. Will add when a company exhibits this pattern. See parent spec for tracking.

### Where it fits in the merge pipeline

```
Pass 1: Collect all concepts+values
Pass 2: Build rename maps (existing — handles simple renames)
Pass 3: Fill values into base tree
Pass 4: Orphan insertion with gap-reduction gate
>>> NEW: Pass 4b: Detect and fix structural shifts (Promotions, Replacements) <<<
Pass 5: Recompute residuals
```

---

## Implementation

### File: `merge_trees.py`

Add `_detect_and_fix_structural_shifts(tree, periods)` called in `merge_filing_trees()` before `_recompute_residuals`.

This function must use a **two-phase detect-then-fix** approach (as validated in the POC):
1. **Scan phase**: Recursively walk the tree and collect all detected structural shifts into a list, without mutating the tree.
2. **Fix phase**: Iterate over the collected detections and apply fixes.

This avoids mutation-during-iteration bugs where fixing one node changes the tree structure and causes missed or duplicate detections downstream.

The function should return a dictionary of applied fixes for logging (e.g., `{'promotions': 1, 'splits': 0}`).

---

## What this spec does NOT cover

This spec implements fixes for **structural concept restructuring** relying strictly on mathematical overlap.

It explicitly defers **Value Restatements** (The parent spec's Tier 1/Tier 2 logic).
If a company restates a concept (e.g., Google Revenue is reported as $100 in the 2023 filing, but the 2024 filing says 2023 Revenue was $90), this spec will ignore it. Handling restatements requires complex business logic and LLM judgment to determine the "why" behind the change, which is out of scope for structural tree healing.

| Pattern | Handled by this Spec |
|---------|---------------------|
| Parent-Child Promotion | **YES** |
| Sibling concept replacement | **YES** |
| 1-to-N Concept split | No (Deferred — no real case observed yet) |
| Same concept, different value (restatement) | No (Deferred to LLM Phase) |

---

## Testing

### Unit tests

Build synthetic trees testing parent-child promotion and sibling replacement patterns and ensure the resulting tree correctly merges values and prevents orphaned residuals.

### Integration test: TSLA

Re-merge TSLA trees with the new logic. Verify:
- `IS Revenue` errors gone
- `NI Link` errors < 150 (Residual NCI noise, not 96K)
- All other companies still ALL PASS
