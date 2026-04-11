# Phase 3e Verification Report

## BLOCKING ISSUES

### B1. TreeNode class line numbers are wrong
**Spec says**: `TreeNode` class at lines 197-254 of `xbrl_tree.py`
**Actual**: `TreeNode` class starts at line 221 and ends at line 278. The spec is off by ~24 lines.
**Impact**: Implementer relying on these line numbers will look at the wrong code.

### B2. No `tests/` directory infrastructure for new tests
**Spec says**: Tests should exist in `tests/`
**Actual**: `tests/` exists with 4 test files but no test for any Phase 3e functionality. The spec does not explicitly call out what new test files to create, but the TDD stage should handle this.
**Impact**: Non-blocking for spec correctness, but noting for completeness.

---

## WARNINGS

### W1. `reconcile_trees` signature change requires updating callers
**Spec says**: Updated signature `reconcile_trees(trees: dict, pres_index: dict) -> dict`
**Actual**: Current signature at line 814 is `reconcile_trees(trees: dict) -> dict`. It is called from `build_statement_trees()` at line 893 as `reconcile_trees(result)`.
**Impact**: The caller in `build_statement_trees()` must also be updated. The spec should explicitly mention this call site.

### W2. Tautological check closures -- count may not be exactly 14
**Spec says**: "Remove 14 tautological check closures"
**Actual**: In `sheet_builder.py`, there are:
- Lines 66-77: 6 role alias assignments in `_render_sheet_body()` (IS_COMPUTED_REVENUE, IS_COMPUTED_COGS, BS_COMPUTED_TE, CF_COMPUTED_OPCF, CF_COMPUTED_INVCF, CF_COMPUTED_FINCF)
- Lines 134-158 in `_write_summary_tab()`: 5 summary check closures (summ_ta_check, summ_tl_check, summ_balance_check, summ_opcf_check, summ_cash_proof)
- Lines 239-265 in `write_sheets()`: 9 tab check closures (is_revenue_check, is_cogs_check, bs_ta_check, bs_tl_check, bs_balance_check, bs_equity_check, cf_opcf_check, cf_invcf_check, cf_fincf_check)
- Total closures: 14 (9 in `write_sheets` + 5 in `_write_summary_tab`). Count is correct.
**Impact**: Low risk, but implementer should verify the 6 role aliases (lines 66-77) are also addressed since the spec mentions them separately.

### W3. `_render_sheet_body` is used by all statement tabs
**Spec says**: IS uses `cascade_layout()`, BS/CF use existing `_assign_rows()`
**Actual**: Currently all three statements (IS, BS, CF) use `_render_sheet_body()` which contains `_assign_rows()`. The spec needs to ensure the refactored IS path still produces compatible output format (list of row arrays) for `_write_sheet_tab()` and `_build_format_requests()`.
**Impact**: Medium -- the rendering output format must remain consistent or downstream formatting will break.

### W4. `temp_pre.py` is a standalone prototype, not integrated
**Spec says**: References `fetch_pre_linkbase()` alongside existing `fetch_cal_linkbase()`
**Actual**: `fetch_pre_linkbase()` already exists in `xbrl_tree.py` at line 63. `temp_pre.py` is a separate hardcoded prototype that duplicates the fetch logic for Netflix specifically. The spec correctly references the existing `fetch_pre_linkbase()` in `xbrl_tree.py` -- `temp_pre.py` can be ignored.
**Impact**: None -- just noting that `temp_pre.py` exists but is not part of the spec.

### W5. `verify_tree_completeness` is entirely new in `pymodel.py`
**Spec says**: Add `verify_tree_completeness(trees, tolerance)` and `_check_node_completeness()`
**Actual**: Neither function exists. `pymodel.py` currently only has `verify_model()`, `_find_is_value_by_label()`, `_find_cf_match_by_value()`, and `main()`. The spec correctly identifies these as new additions.
**Impact**: None -- correctly identified as new code.

### W6. `_build_all_format_requests` is new but replaces existing `_build_format_requests`
**Spec says**: References `_build_all_format_requests()`
**Actual**: The existing function is `_build_format_requests(sheet_id, rows, periods)` at line 162. The spec's `_build_all_format_requests` appears to be a renamed/refactored version. The implementer should understand the existing function's behavior before refactoring.
**Impact**: Low -- naming difference should be documented clearly in the implementation guide.

### W7. Pipeline gate location in `run_pipeline.py`
**Spec says**: "Block sheet write if verification fails"
**Actual**: `run_pipeline.py` already runs `pymodel.py --checkpoint` at line 97 before sheet writing at line 101. However, `run_command()` calls `sys.exit()` on failure (line 28), so verification failure already blocks the pipeline. The spec's gate may need to add `verify_tree_completeness` as an additional check, or modify the existing checkpoint.
**Impact**: Low -- the basic gate mechanism exists; the spec should clarify whether this is adding a NEW gate or modifying the existing one.

---

## VERIFIED ITEMS

### V1. File references -- all files exist
- `xbrl_tree.py` -- EXISTS (35,121 bytes)
- `sheet_builder.py` -- EXISTS (17,582 bytes)
- `pymodel.py` -- EXISTS (6,190 bytes)
- `run_pipeline.py` -- EXISTS (4,012 bytes)
- `temp_pre.py` -- EXISTS (2,386 bytes)

### V2. TreeNode attributes
- `.concept` -- EXISTS (line 225)
- `.weight` -- EXISTS (line 228)
- `.children` -- EXISTS (line 229)
- `.values` -- EXISTS (line 230)
- `.is_leaf` -- EXISTS (line 231)
- `.role` -- EXISTS (line 232)
- `.name` -- EXISTS (line 227)
- `.tag` -- EXISTS (line 226)
- `.to_dict()` -- EXISTS (line 252)
- `.from_dict()` -- EXISTS (line 268)

### V3. Existing functions the spec says to keep
- `_tag_bs_positions(assets_tree, liab_eq_tree)` -- EXISTS (line 430)
- `_tag_cf_positions(cf_tree, facts)` -- EXISTS (line 602)
- `_tag_is_positions(is_tree, cf_tree)` -- EXISTS (line 691)
- `_override_bs_cash(assets_tree, cf_endc_values)` -- EXISTS (line 767)
- `_filter_to_complete_periods(trees)` -- EXISTS (line 787)
- `_tag_da_sbc_nodes(is_tree, cf_tree)` -- EXISTS (line 548)
- `reconcile_trees(trees)` -- EXISTS (line 814)
- `classify_roles(roles)` -- EXISTS (line 172)
- `build_statement_trees(html, base_url)` -- EXISTS (line 838)
- `verify_model(trees)` -- EXISTS in pymodel.py (line 8)

### V4. `_tag_is_positions` value overwrite lines
- Line 741: `best_match.values = dict(cf_ni_values)` -- CONFIRMED
- Line 755: `fallback.values = dict(cf_ni_values)` -- CONFIRMED
- Line 760: `is_tree.values = dict(cf_ni_values)` -- CONFIRMED

### V5. `_assign_rows` in `_render_sheet_body` (lines 52-56)
- Line 52: `def _assign_rows(node, indent=0):` -- CONFIRMED
- Line 53: `row_num = start_row + len(layout)` -- CONFIRMED
- Line 54: `layout.append((row_num, indent, node))` -- CONFIRMED
- Line 55: `for child in node.children:` -- CONFIRMED
- Line 56: `_assign_rows(child, indent + 1)` -- CONFIRMED

### V6. Tautological check aliases (lines 66-77)
- Lines 66-77 contain the 6 `IS_COMPUTED_REVENUE`, `IS_COMPUTED_COGS`, `BS_COMPUTED_TE`, `CF_COMPUTED_OPCF`, `CF_COMPUTED_INVCF`, `CF_COMPUTED_FINCF` role aliases -- CONFIRMED

### V7. Pre-registered Summary rows (lines 228-234)
- Lines 228-234 contain pre-registered `SUMM_TA` through `SUMM_ENDC` -- CONFIRMED

### V8. `fetch_pre_linkbase` already exists
- EXISTS at line 63 of `xbrl_tree.py` -- fetches `_pre.xml` from schema, same pattern as `fetch_cal_linkbase`

### V9. `find_node_by_role` exists
- EXISTS at line 420 of `xbrl_tree.py`: `def find_node_by_role(tree: TreeNode, role: str) -> TreeNode | None:`

### V10. `classify_roles` at line 172
- CONFIRMED: `def classify_roles(roles: list[str]) -> dict:` at line 172

### V11. `write_sheets` exists in sheet_builder.py
- EXISTS at line 221: `def write_sheets(trees: dict, company: str) -> tuple[str, str]:`

### V12. `_cell_ref` exists in sheet_builder.py
- EXISTS at line 95: `def _cell_ref(role, col, global_role_map):`

### V13. `CROSS_STATEMENT_CHECKS` pattern is entirely new
- Does NOT exist in any .py file. Only referenced in spec docs.

### V14. Naming conventions are consistent
- All functions use `snake_case` -- CONSISTENT
- `TreeNode` class uses PascalCase -- CONSISTENT
- Private functions prefixed with `_` -- CONSISTENT
- Constants use `UPPER_SNAKE_CASE` (e.g., `STATEMENT_ROLE_PATTERNS`, `CF_ROLE_MAP`) -- CONSISTENT

### V15. Test files inventory
- `tests/test_da_sbc_tagging.py` -- Tests `_tag_da_sbc_nodes()` and FX tagging
- `tests/test_model_historical.py` -- Tests `reconcile_trees()`, `verify_model()`, sheet rendering
- `tests/test_model_historical_legacy.py` -- Legacy AAPL fixture baseline tests
- `tests/test_sheet_formulas.py` -- Tests `_build_weight_formula()`, `_render_sheet_body()`, check rows

### V16. `run_pipeline.py` orchestration
- Stage 1: `agent1_fetcher.py` (line 54)
- Stage 2: `xbrl_tree.py` per filing (line 83)
- Stage 3: `pymodel.py --checkpoint` per tree file (line 97)
- Stage 4: `sheet_builder.py` on first tree file (line 101)
- Gate mechanism: `run_command()` calls `sys.exit()` on non-zero return (line 28)

### V17. `verify_model()` signature
- `def verify_model(trees: dict) -> list[tuple]:` at line 8 of `pymodel.py` -- CONFIRMED
- Takes dict with keys IS/BS/BS_LE/CF/complete_periods, returns list of (check_name, period, delta) tuples

### V18. New functions correctly identified as new
The following functions referenced in the spec do NOT exist in the codebase (all new):
- `parse_pre_linkbase()`
- `build_presentation_index()`
- `sort_by_presentation()`
- `cascade_layout()`
- `_assign_rows_layout()`
- `_tag_is_semantic()`
- `_find_by_keywords_bfs()`
- `_supplement_orphan_facts()`
- `_collect_tags()`
- `_fill_gaps()`
- `verify_tree_completeness()`
- `_check_node_completeness()`
- `_render_from_layout()`
- `_layout_summary()`
- `_build_all_format_requests()`
- `_render_check_rows()`
- `CROSS_STATEMENT_CHECKS`
- `IS_SEMANTIC_TAGS`
- `_values_match()`
