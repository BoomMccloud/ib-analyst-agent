# Spec Evaluation: Multi-Year Tree Merge

## Spec File
`docs/spec_multi_year_merge.md` — evaluated against codebase as of 2026-04-12

## Research Findings

### 1. run_pipeline.py — Spec Claim vs Reality

**Spec Claim**: "Pipeline not wired — `run_pipeline.py` ignores `merge_trees.py` entirely, runs `sheet_builder.py` on only the most recent filing."

**Reality**: **OUTDATED / PARTIALLY FIXED.**
- `run_pipeline.py` DOES call `merge_trees.py` when multiple filings exist (line ~97)
- When `len(tree_files) > 1`: calls `merge_trees.py trees_0.json ... trees_N.json -o merged.json`
- When single filing: skips merge, uses single tree directly
- Pipeline order: agent1_fetcher → xbrl_tree (per filing) → merge_trees (if multi) → verify_tree_completeness (in-process) → pymodel --checkpoint → sheet_builder
- Does NOT call `xbrl_group.py`, `agent3_modeler.py`, or `structure_financials.py`
- Forecasting (Stage 5) is a stub

**Verdict**: This problem statement was true at spec-writing time but has since been partially addressed. The pipeline IS wired now.

### 2. pymodel.py Verification — Spec Claim vs Reality

**Spec Claim (Priority 1)**: "All checks use `fv()` not `nv()`" — this was a bug to fix.

**Reality**: **FIXED.** All 7 verification checks use `fv()`:
1. BS Balance: `fv(BS_TA) == fv(BS_TL) + fv(BS_TE)` ✅
2. Cash End: `CF_ENDC == fv(BS_CASH)` ✅
3. Cash Begin: `CF_BEGC[t] == fv(BS_CASH)[t-1]` ✅
4. NI Link: `fv(INC_NET_IS) == fv(INC_NET_CF)` ✅
5. Segment Sums: `_verify_segment_sums` via `fv()` recursively ✅
6. D&A Link: role tags (`IS_DA`, `CF_DA`) via `fv()` ✅
7. SBC Link: role tags (`IS_SBC`, `CF_SBC`) via `fv()` ✅

**Spec Claim**: D&A/SBC checks should use role tags, not value-matching heuristic.
**Reality**: **FIXED.** Uses `find_node_by_role()` for D&A and SBC. The heuristic functions (`_find_is_value_by_label`, `_find_cf_match_by_value`) exist but are NOT called by `verify_model()`.

**Spec Claim**: Add check #3: `CF_BEGC[t] == fv(BS_CASH)[t-1]`
**Reality**: **FIXED.** Present and implemented.

**Verdict**: All Priority 1 items are DONE. The verification logic matches the spec.

**Note**: README claims `set_cf_cash()`, `set_cf_totals()`, etc. exist as "Tautological API" — these are NOT implemented in pymodel.py. This is a documentation/codebase mismatch but NOT part of this spec's scope.

### 3. merge_trees.py — Spec Claim vs Reality

**File exists**: YES (355 lines)

**Phase 1 (Concept Alignment)**:

| Spec Step | Status | Details |
|-----------|--------|---------|
| 1a: Exact matches | ✅ Implemented | Same concept name maps 1:1 |
| 1b: Rename detection | ✅ Implemented | `_build_rename_map()` — value matching at overlap period, chains A→B→C |
| 1c: Reclassification detection | ❌ NOT implemented | No logic to detect same concept with different values at overlap period |
| 1d: Orphan detection | ✅ Implemented | `_find_orphans()` — concepts only in older filings |
| ConceptMap output | ❌ NOT implemented | merge_filing_trees returns merged tree dict directly, not ConceptMap |

**Phase 2 (Build Unified Tree)**:

| Spec Step | Status | Details |
|-----------|--------|---------|
| 2a: Skeleton + fill values | ✅ Implemented | Uses newest filing as skeleton |
| 2b: Orphan insertion with gap-reduction gate | ✅ Implemented | Pass 4 — helps/hurts logic matches spec |
| 2c: Compute residuals | ✅ Implemented | `_recompute_residuals()` — creates/updates/removes `__OTHER__{parent}` |
| 2d: Residual sanity check (logging) | ✅ Implemented | Logs warning if `|residual| > sibling_avg` |
| Invariant enforcement | ✅ Implemented | Halts if SUM(children) != declared after residuals |

**Verdict**: Phase 2 is COMPLETE. Phase 1 is ~75% complete — missing reclassification detection and ConceptMap intermediate structure.

### 4. xbrl_tree.py — Spec Claims vs Reality

**File**: 1780 lines — the deterministic extraction engine.

| Spec Claim ("already correct") | Reality | Details |
|-------------------------------|---------|---------|
| `__OTHER__{parent}` residuals with unique naming | ✅ CONFIRMED | `merge_calc_pres()` line 892: `TreeNode(f"__OTHER__{tree.concept}")` — named by concept ID |
| `SUM(children) == declared` guaranteed by construction | ⚠️ CONDITIONAL | Only AFTER `merge_calc_pres()` injects residuals. Raw calc trees have gaps. The guarantee is post-hoc, not by construction. |
| `reconcile_trees()` tags positions | ✅ CONFIRMED | Steps A–H: BS via structural position (first/last child), CF via concept-name patterns, IS via value-matching against CF |
| `reconcile_trees()` applies cross-statement overrides | ✅ CONFIRMED | `_override_bs_cash()` overrides BS_CASH with CF_ENDC, adjusts TCA to absorb delta |
| `reconcile_trees()` merges calc+presentation ordering | ✅ CONFIRMED | `merge_calc_pres()` step G |
| `verify_tree_completeness()` confirms formula integrity | ✅ CONFIRMED | Returns error tuples for gaps > 1.0. **BUT**: it's a passive checker, NOT a gate — doesn't halt the pipeline |
| Segment decomposition is sum-verified | ✅ CONFIRMED | `_attach_is_segments()` uses `_find_best_decomposition()` to find largest subset summing to total |
| `complete_periods` in output | ✅ CONFIRMED | `_filter_to_complete_periods()` — intersection of periods in ALL 4 statements |

**Nuance**: The spec says "guaranteed by construction" but the guarantee is actually **post-hoc** — `merge_calc_pres()` adds residuals to absorb gaps. This is a subtle but important distinction.

### 5. README vs Codebase Mismatches

| README Claim | Reality |
|-------------|---------|
| "Tautological API: `set_cf_cash()`, `set_cf_totals()`, etc." | ❌ NOT implemented in pymodel.py |
| "`verify_model()` checks 5 real invariants" | Now checks **7** invariants (added Cash Begin + Segment Sums) |
| "D&A/SBC value-matched" | Now uses **role tags**, not value-matching |
| "Two extraction paths: XBRL and LLM legacy" | `run_pipeline.py` only uses XBRL path |

## Overall Spec Assessment

### What's ALREADY DONE (spec describes bugs that are fixed)

1. **Priority 1: Fix verification** — ALL 5 items are DONE
   - All checks use `fv()` ✅
   - Cash Begin check added ✅
   - Segment sums use `fv()` recursively ✅
   - D&A/SBC use role tags ✅

2. **Priority 2: Wire pipeline** — MOSTLY DONE
   - `merge_trees.py` IS called by `run_pipeline.py` ✅
   - Pipeline order is correct ✅
   - Single filing skips merge ✅

3. **Phase 2: Build Unified Tree** — COMPLETE
   - Skeleton + fill values ✅
   - Orphan insertion with gap-reduction gate ✅
   - Residual computation ✅
   - Residual sanity logging ✅
   - Invariant enforcement ✅

### What's STILL MISSING from the spec

1. **Reclassification detection** (Spec Step 1c) — NOT implemented
   - Same concept exists in both filings with DIFFERENT values at overlap period
   - Should use newer filing's value for overlap, older filing's value for non-overlap
   - Currently: no logic to detect or handle this

2. **ConceptMap intermediate structure** — NOT implemented
   - Spec defines it as Phase 1 output
   - Current code: `merge_filing_trees` returns merged tree dict directly
   - This is an architectural gap — the implementation takes a more direct approach

3. **`verify_tree_completeness()` is NOT a gate** — it's a passive checker
   - Spec implies it "confirms formula integrity" as part of the trusted pipeline
   - Reality: it runs and prints warnings but does NOT halt on failure

### Spec Accuracy Score

| Section | Accuracy | Notes |
|---------|----------|-------|
| Problem Statement | 50% | 1 of 4 problems already fixed (pipeline wiring) |
| What's Already Correct | 90% | All claims verified, with minor nuance on "by construction" |
| Phase 0 (Parse All Filings) | 100% | No changes needed, matches current behavior |
| Phase 1 (Concept Alignment) | 75% | Missing reclassification detection + ConceptMap |
| Phase 2 (Build Unified Tree) | 100% | All steps implemented |
| Phase 3 (Cross-Statement Verification) | 100% | All 7 checks implemented with `fv()` |
| Phase 4 (Sheet Generation) | 100% | No changes needed |
| Priority 1 (Fix verification) | 100% | ALL DONE |
| Priority 2 (Wire pipeline) | 90% | Wired, but `verify_tree_completeness` not a hard gate |
| Priority 3 (Improve merge) | N/A | Deferred per spec |
| Key Design Decisions | 95% | All 6 decisions reflected in code |

### Recommendation

The spec is **largely accurate** but describes a state that has partially been implemented since it was written. The remaining gaps are:

1. **Reclassification detection** — the most significant missing piece. Without it, restated values in older filings could conflict with newer filings at overlap periods.
2. **Gate enforcement** — `verify_tree_completeness()` should be a hard gate (halt on failure), not just a warning printer.
3. **ConceptMap vs direct merge** — the implementation skips the ConceptMap intermediate. This works but loses auditability (can't trace which filing is authoritative for each concept/period).

The spec's Priority 1 and Priority 2 are essentially done. The remaining work is Priority 1c (reclassification) and making the completeness check a hard gate.
