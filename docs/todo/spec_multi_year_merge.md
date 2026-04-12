# Multi-Year Tree Merge — Design Spec

> **Status**: Updated 2026-04-12 — reflects current codebase state after Priority 1 and Priority 2 implementation.
> **Remaining work**: Reclassification detection (Step 1c), hard gate on `verify_tree_completeness()`.

## Problem Statement

The current pipeline builds each filing's tree independently, then patches them together post-hoc. This causes:

1. **Silent invariant violations** — `verify_model` checks declared XBRL values but the sheet uses `=SUM(children)` formulas. These diverge whenever children don't sum to parent, and the check passes while the sheet is wrong. **[FIXED — all checks now use `fv()`]**
2. **Reactive patching** — renames, reclassifications, and orphans are discovered during merge and fixed with heuristics that sometimes fail (e.g., depreciation double-count). **[PARTIALLY FIXED — rename detection and orphan gate implemented; reclassification detection still missing]**
3. **No single source of truth** — declared values and formula values are two separate systems that can disagree. **[FIXED — `fv()` is now the single source of truth]**
4. **Pipeline not wired** — `run_pipeline.py` ignores `merge_trees.py` entirely, runs `sheet_builder.py` on only the most recent filing. **[FIXED — pipeline now wires merge → verify → sheet correctly]**

## Core Principle

**The sheet is the product. Every number in the sheet must be verifiable before the sheet is written. If a formula won't match, don't write the sheet — fix the data first.**

## What's Already Correct

The **single-filing pipeline** is sound and does not need changes:

- `xbrl_tree.py` builds calc trees with `__OTHER__{parent}` residuals (uniquely named) so `SUM(children) == declared` for every parent, every period — guaranteed after `merge_calc_pres()` injects residuals.
- `reconcile_trees()` tags positions (structural for BS, pattern-matching for CF, value-matching for IS), applies cross-statement overrides, merges calc+presentation ordering.
- `verify_tree_completeness()` checks formula integrity per filing. **Note**: currently a passive checker (prints warnings but doesn't halt). Should be upgraded to a hard gate.
- `verify_model()` checks 7 cross-statement invariants using `fv()` formula values. **[FIXED — was 5 checks, now 7]**
- Segment decomposition (`_attach_is_segments`, `_build_revenue_segment_tree`) is sum-verified before attachment.

**The multi-year merge is the only broken part.** This spec addresses only the merge.

### Current implementation state (as of 2026-04-12)

| Component | Status | Notes |
|-----------|--------|-------|
| `xbrl_tree.py` — single-filing trees | ✅ Sound | No changes needed |
| `pymodel.py` — `verify_model()` | ✅ Fixed | All 7 checks use `fv()`, role tags for D&A/SBC |
| `run_pipeline.py` — pipeline wiring | ✅ Fixed | Calls `merge_trees.py` for multi-filing |
| `merge_trees.py` — rename detection | ✅ Implemented | `_build_rename_map()` with chaining |
| `merge_trees.py` — orphan insertion | ✅ Implemented | Gap-reduction gate in Pass 4 |
| `merge_trees.py` — residuals | ✅ Implemented | `_recompute_residuals()` with sanity warnings |
| `merge_trees.py` — reclassification detection | ❌ Missing | Step 1c — see Priority 1 below |
| `verify_tree_completeness()` — hard gate | ❌ Missing | Currently warns only, doesn't halt |

## Architecture

### Phase 0: Parse All Filings (existing — no changes)

`xbrl_tree.py --url <url> -o trees_<date>.json` runs independently per filing, producing a verified single-filing tree. This is the trusted foundation.

Each output contains:
```python
{
  "complete_periods": ["2024-12-31", "2025-12-31"],
  "IS": TreeNode.to_dict(),   # calc tree with __OTHER__ residuals
  "BS": TreeNode.to_dict(),
  "BS_LE": TreeNode.to_dict(),
  "CF": TreeNode.to_dict(),
  "facts": {tag: {period: value}},
  "cf_endc_values": {period: value},
  "unit_label": "$m",
  "revenue_segments": TreeNode.to_dict(),  # optional
}
```

No changes to `xbrl_tree.py`. Each filing's tree is self-consistent by construction.

### Phase 1: Concept Alignment (`merge_trees.py`)

**Input**: N tree JSON files, ordered newest → oldest.

Chain filings using overlapping periods. For each adjacent pair (filing N-1, filing N) sharing overlap period P:

**Step 1a: Exact matches** — same concept name exists in both filings. These map 1:1. Record values for all periods. **[IMPLEMENTED]**

**Step 1b: Rename detection** — for concepts in filing N that don't exist in filing N-1: **[IMPLEMENTED]**
- Skip internal nodes (names starting with `__`) — these are residuals, not real concepts
- Search filing N-1's tree for a node with the same value at period P
- Must be unique (only one candidate with that value)
- Record: `old_concept → new_concept`
- Chain renames: if A→B in one hop and B→C in another, resolve to A→C

**Step 1c: Reclassification detection** — same concept exists in both filings but with different values at period P: **[NOT IMPLEMENTED — PRIORITY 1]**

Reclassifications cannot be detected purely deterministically. A value difference could be a restatement, a data error, rounding noise, or a unit conversion change. The approach is a **deterministic-first, LLM-escalation** pipeline:

**Tier 1: Deterministic threshold (fast, catches obvious cases)**
- For each shared concept at overlap period P:
  - Compute `delta_pct = abs(newer_value - older_value) / abs(newer_value)`
  - If `delta_pct > 0.05` (5% threshold): flag as **definite reclassification**
  - If `delta_pct < 0.005` (0.5% threshold): treat as noise, ignore
  - If between thresholds: flag as **ambiguous** → escalate to Tier 2
- For definite reclassifications:
  - Use filing N-1's (newer) value for period P — it is authoritative
  - The older filing's value is "restated" — don't use it for period P
  - DO use the older filing's value for its non-overlapping periods

**Tier 2: LLM-assisted (Haiku, catches ambiguous cases)**
- Only triggered when Tier 1 finds ambiguous cases (0.5% < delta < 5%)
- Sends structured context to `claude-haiku-4-5-20251001`:

```python
{
    "company": "Alphabet Inc.",
    "concept": "us-gaap_Revenue",
    "newer_filing": {"period": "2023-12-31", "value": 307394, "filed": "2025-02-01"},
    "older_filing": {"period": "2023-12-31", "value": 282836, "filed": "2024-02-01"},
    "delta_pct": 8.67,
    "sibling_changes": [
        {"concept": "us-gaap_CostOfRevenue", "delta_pct": 9.1},
        {"concept": "us-gaap_OperatingExpenses", "delta_pct": 12.3},
    ],
    "footnote_excerpt": "...acquisition of XYZ Corp contributed $18B to revenue...",
}
```

- The LLM evaluates: is this a restatement or noise?
- Returns: `{"is_reclassification": true, "reason": "..."}`
- If `true`: treat same as Tier 1 definite reclassification
- If `false`: treat as noise — use older filing's value for all its periods
- Cost: ~2-3 Haiku calls per multi-filing merge (only for ambiguous cases)

**Why LLM here?** This is fundamentally a judgment call — "is this difference meaningful or noise?" — which matches the project's principle: "LLMs only handle tasks requiring judgment." The deterministic threshold catches the obvious $24B restatements; Haiku handles the subtle 2-10% cases where context matters.

**Safety net**: Even if the LLM gets it wrong, Phase 3's `fv()` invariant checks catch the resulting imbalance. The sheet never gets written if numbers don't balance. So the LLM is an optimization, not a correctness dependency.

**Step 1d: Orphan detection** — concepts that only exist in older filings: **[IMPLEMENTED]**
- Record their parent concept from the older filing's calc tree
- Translate parent through rename chain to canonical name
- Mark as candidate for insertion (evaluated in Phase 2)

**Output**: `ConceptMap` **[NOT IMPLEMENTED — current code returns merged tree directly]**

```python
ConceptMap = {
  canonical_concept: {
    "values": {period: value},          # authoritative value per period
    "parent": canonical_parent,          # in the unified tree
    "weight": 1.0 or -1.0,
    "source": {period: filing_index},    # which filing is authoritative
    "renames": [old_concept, ...],       # historical names
    "is_orphan": bool,                   # only in older filings
    "reclassification": {                # NEW — Step 1c output
        "is_reclassified": bool,
        "delta_pct": float,
        "tier": "deterministic" | "llm",
        "llm_reason": str,               # only if tier == "llm"
    },
  }
}
```

### Phase 2: Build Unified Tree (`merge_trees.py`)

**Input**: `ConceptMap` + newest filing's tree (skeleton). **[Note: current implementation skips ConceptMap and builds directly from tree dicts]**

**Step 2a**: Start with newest filing's tree as the skeleton. Fill in values from `ConceptMap` for all periods. Skip `__OTHER__*` nodes (they'll be recomputed). **[IMPLEMENTED]**

**Step 2b: Orphan insertion with gap-reduction gate.** For each orphan concept: **[IMPLEMENTED]**
- Find its parent in the skeleton (using canonical name from ConceptMap)
- Compute current gap: `parent.declared[p] - sum(real_children[p])`
- Compute new gap: `parent.declared[p] - sum(real_children[p]) - orphan.value[p]`
- **Gate**: only insert if `|new_gap| < |current_gap|` for at least one period AND `|new_gap| <= |current_gap|` for ALL periods
- If gate fails: the orphan was absorbed into another concept — skip it, log reason

**Step 2c: Compute residuals.** For every parent node, for every period: **[IMPLEMENTED]**
```
residual[p] = parent.declared[p] - sum(real_children[p] * weight)
```
- If `|residual| > 0.5`: create `__OTHER__{parent_concept}` child with those values
- If existing `__OTHER__` node: update its values
- If no residual needed: remove `__OTHER__` if it exists

**Step 2d: Residual sanity check (logging only — no auto-fix).** For each parent with a residual: **[IMPLEMENTED]**

```python
residual_abs = abs(residual[p])
sibling_avg = mean(abs(child.value[p]) for child in real_children)
```

If `residual_abs > sibling_avg` for any period, log a warning with the parent concept, residual amount, and sibling average. The `__OTHER__` node absorbs the gap (sum invariant is maintained) but the warning flags concepts worth investigating.

**Invariant (enforced immediately after Step 2c)**:
```
∀ parent, ∀ period: sum(children_formula_values) == parent.declared
```
If this fails after residual insertion, something is structurally wrong — halt and report.

### Phase 3: Cross-Statement Verification (`pymodel.py`)

All checks use formula values:

```python
def fv(node, period):
    """What =SUM(children) produces — the sheet value."""
    if not node.children:
        return node.values.get(period, 0)
    return sum(fv(c, period) * c.weight for c in node.children)
```

**Invariant checks** (all periods, all using `fv()`):

| # | Check | Formula | Status |
|---|-------|---------|--------|
| 1 | BS Balance | `fv(BS_TA) == fv(BS_TL) + fv(BS_TE)` | ✅ Implemented |
| 2 | Cash End | `CF_ENDC == fv(BS_CASH)` | ✅ Implemented |
| 3 | Cash Begin | `CF_BEGC[t] == fv(BS_CASH)[t-1]` (for t > first period) | ✅ Implemented |
| 4 | NI Link | `fv(INC_NET_IS) == fv(INC_NET_CF)` | ✅ Implemented |
| 5 | Segment Sums | `fv(parent) == sum(fv(children))` for all segment nodes | ✅ Implemented |
| 6 | D&A Link | `fv(IS_DA) == fv(CF_DA)` (using role tags, not value heuristics) | ✅ Implemented |
| 7 | SBC Link | `fv(IS_SBC) == fv(CF_SBC)` (using role tags, not value heuristics) | ✅ Implemented |

**Segment sum check** must use `fv()` recursively, not `node.values.get()`.

**D&A and SBC checks** use the role tags (`IS_DA`, `CF_DA`, `IS_SBC`, `CF_SBC`) assigned during `reconcile_trees`, not the `_find_cf_match_by_value` heuristic. The role-based approach is deterministic; the value-matching heuristic can match the wrong node.

**If any check fails: halt. Do not generate sheet.** Error report includes:
- Which check failed
- Which period
- The formula delta
- The concepts involved
- Suggested investigation path

### Phase 4: Sheet Generation (`sheet_builder.py`)

Only reached if Phase 3 passes. No changes to rendering logic — the data is guaranteed correct.

## Implementation Plan

### Priority 1: Reclassification detection (merge_trees.py Step 1c)

The most significant remaining gap. Without it, restated values in older filings can silently corrupt the merged tree at overlap periods.

| Tier | What | Why | Cost |
|------|------|-----|------|
| **Tier 1: Deterministic threshold** | Flag concepts with >5% value delta at overlap period as definite reclassifications; <0.5% as noise; 0.5-5% as ambiguous → escalate to Tier 2 | Catches obvious restatements ($24B revenue changes) without LLM calls | Free |
| **Tier 2: LLM-assisted (Haiku)** | Send ambiguous cases to `claude-haiku-4-5-20251001` with context (concept, values, sibling changes, footnote excerpt) to judge if it's a restatement | Catches subtle reclassifications that thresholds miss — this is judgment, not math | ~2-3 calls per merge |

**Files to modify**: `merge_trees.py` — add `_detect_reclassifications()` function with Tier 1 + Tier 2 logic.

**Files to add**: `merge_trees_llm.py` (or inline in `llm_utils.py`) — Haiku prompt for reclassification judgment.

**Testing**: Run on the 10-company test set. Verify that known restatements (e.g., Google's acquisition-driven revenue changes) are detected. Verify that rounding differences (<0.5%) are not flagged.

### Priority 2: Hard gate on `verify_tree_completeness()`

Currently `verify_tree_completeness()` in `run_pipeline.py` prints warnings but doesn't halt the pipeline. It should be a hard gate.

**Change**: In `run_pipeline.py`, after calling `verify_tree_completeness()`:
```python
if gaps:
    print(f"FAIL: {len(gaps)} tree completeness gaps found", file=sys.stderr)
    for concept, period, gap in gaps:
        print(f"  {concept} @ {period}: gap = {gap:+,.0f}", file=sys.stderr)
    sys.exit(1)
```

**Files to modify**: `run_pipeline.py` — change from warning to exit-on-failure.

### Priority 3: ConceptMap intermediate (optional, for auditability)

Current implementation skips the `ConceptMap` intermediate and builds the merged tree directly. This works but loses traceability of which filing is authoritative for each concept/period.

**When to do this**: Only if users need to audit "why does this cell show this value?" — i.e., trace a value back to its source filing. Not needed for correctness (Phase 3 gates catch errors), but valuable for debugging.

**Defer** until Priority 1-2 are running on real data and auditability becomes a user need.

### ~~Priority 1 (original): Fix verification (pymodel.py)~~ — ✅ DONE

| Change | Status |
|--------|--------|
| All checks use `fv()` not `nv()` | ✅ Done |
| Add check #3: `CF_BEGC[t] == fv(BS_CASH)[t-1]` | ✅ Done |
| `_verify_segment_sums` uses `fv()` recursively | ✅ Done |
| D&A/SBC checks use role tags when available | ✅ Done |

### ~~Priority 2 (original): Wire pipeline (run_pipeline.py)~~ — ✅ DONE

| Change | Status |
|--------|--------|
| `merge_trees.py` called for multi-filing | ✅ Done |
| Pipeline order: fetch → tree → merge → verify → sheet | ✅ Done |
| Single filing skips merge | ✅ Done |

### Priority 4: Improve merge (merge_trees.py) — deferred, needs evidence

**Not in scope for this implementation.** Build Priority 1-3, run on the 10-company test set, and review residual warnings. Only pursue these if large residuals are a real problem:

| Change | Why | Gate |
|--------|-----|------|
| Tier 1 auto-fix (re-scan orphans for residual reduction) | Large residuals hide bugs | Only if warnings fire on real data |
| Better orphan parent resolution | Currently uses first parent found; should prefer canonical | Only if orphan insertion produces wrong results |

### Priority 5: Raw-facts refactor (future)

Move Phase 0 output from built trees to raw parsed data (calc links, facts dict, pres index). `merge_trees.py` builds the tree once from the unified concept map. This is architecturally cleaner but not fixing a broken behavior — the current merge-built-trees approach works with the Priority 1-3 fixes in place. Defer to a separate spec.

## Key Design Decisions

### 1. Formula values are the only truth

The sheet shows `=SUM(children)`, not declared values. Every check must use `fv()`. Declared XBRL values determine what formulas *should* produce (via residuals) but are never checked directly as "correct."

### 2. Residuals are first-class, uniquely named

Every parent node has either:
- Children that exactly sum to its declared value, OR
- An explicit `__OTHER__{parent_concept}` residual that makes them sum

Unique naming prevents value collisions during merge. Residuals are recomputed after value population, not carried from individual filings.

### 3. Authoritative period sourcing

For each (concept, period) pair, exactly one filing is authoritative:
- Most recent filing that contains that period wins
- For overlapping periods: newer filing wins (may be restated)
- Reclassification detection (Step 1c) determines whether the older filing's value should be used for non-overlapping periods

### 4. Orphan gate: must reduce gap

An orphan from an older filing is only added if it makes the parent's formula closer to correct for at least one period and doesn't make it worse for any period. This prevents double-counting from reclassified items.

### 5. Halt on invariant failure

The pipeline does NOT generate a sheet if any Phase 3 check fails. Error report tells the user exactly what's wrong and where to investigate.

### 6. Single-filing pipeline is trusted

`xbrl_tree.py` + `reconcile_trees()` produce correct trees by construction. The merge layer builds on this — it doesn't rebuild trees from scratch, it combines verified trees. This keeps the architecture simple and the blast radius of changes small.

### 7. Deterministic-first, LLM-escalation for reclassifications

Reclassification detection is not fully deterministic — a value difference could be a restatement, data error, rounding noise, or unit change. The approach:
- **Tier 1**: Deterministic threshold catches obvious cases (>5% delta)
- **Tier 2**: LLM (Haiku) judges ambiguous cases (0.5-5% delta) with full context
- **Safety net**: Phase 3 invariant gates catch damage from missed cases

This matches the project principle: "LLMs only handle tasks requiring judgment."

## Example: Google 2021-2025

**Phase 0**: 4 filings parsed independently, each self-consistent.

**Phase 1** (concept alignment):
```
PropertyPlantAndEquipmentNet (filings 1,2,3) 
  → PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAsset (filing 0)
  Rename detected via value match at 2024-12-31: both = 171,036

LongTermDebtAndCapitalLeaseObligations (filings 1,2,3)
  → LongTermDebtNoncurrent (filing 0)
  Rename detected via value match at 2024-12-31: both = 10,883

us-gaap_Revenue (all filings)
  → Reclassification detected at 2023-12-31:
    newer = 307,394, older = 282,836, delta = 8.67%
    Tier 1: definite reclassification (>5%)
    → Use newer value (307,394) for 2023-12-31
    → Use older value (282,836) for 2022-12-31 (non-overlapping)

InventoryNet (filings 2,3 only)
  → orphan, parent = AssetsCurrent
  
us-gaap_Depreciation (filing 3 only)
  → orphan, parent = NetCashProvidedByUsedInOperatingActivities
```

**Phase 2** (tree building):
```
InventoryNet: gate check — reduces AssetsCurrent gap for 2021-2022 → INSERT
Depreciation: gate check — would INCREASE OPCF gap (already in DepreciationAndImpairment) → SKIP
Residual sanity: all residuals < sibling average → no LLM calls needed
Tree invariant: SUM(children) == declared for all 20 parent nodes × 5 periods → PASS
```

**Phase 3** (verification):
```
BS Balance:  0, 0, 0, 0, 0 → PASS
Cash End:    0, 0, 0, 0, 0 → PASS
Cash Begin:  0, 0, 0, 0    → PASS (4 checks, first period skipped)
NI Link:     0, 0, 0, 0, 0 → PASS
Segments:    all pass       → PASS
```

**Phase 4**: sheet generated, guaranteed correct.
