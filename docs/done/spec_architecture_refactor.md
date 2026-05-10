# Architecture Refactor — Design Spec

**Date**: 2026-04-12
**Status**: Done

## Problem Statement

The codebase has grown organically and several core files have become monolithic, mixing disparate responsibilities. This makes adding new complex features (like structural shift detection and LLM-assisted reclassification detection) difficult to test and maintain.

Specifically:
1. `xbrl_tree.py` (1,800+ lines) mixes networking, XML parsing, tree data structures, reconciliation business logic, and segment detection.
2. `merge_trees.py` is becoming overloaded, mixing heuristic concept-matching with mathematical tree building and residual computation.
3. `sheet_builder.py` (700+ lines) mixes API calls, layout engines, formatting rules, and string math.

To maintain engineering velocity and ensure the upcoming LLM integrations are robustly testable, we need to extract these responsibilities into focused modules.

---

## Proposed Architecture Splits

### 1. High Priority: The XBRL Core (`xbrl_tree.py`)

Split `xbrl_tree.py` into a cohesive `xbrl/` package. This creates a clean foundation before touching the complex merge layer.

`xbrl_tree.py` will become a thin facade re-exporting from submodules to preserve the CLI.

**Proposed Structure:**
`xbrl/`
*   `__init__.py` — Public API (`TreeNode`, `reconcile_trees`, `build_statement_trees`).
*   `linkbase.py` — Network fetching and XML parsing (`fetch_linkbases`, `parse_calc_linkbase`, etc.).
*   `tree.py` — The `TreeNode` class, serialization, tree building, and tree traversal/search utilities.
*   `reconcile.py` — Cross-statement reconciliation logic (`_tag_bs_positions`, `merge_calc_pres`, `verify_tree_completeness`).
*   `segments.py` — Segment detection and processing logic (`_attach_is_segments`, `_build_revenue_segment_tree`).

### 2. High Priority: The Merge Layer (`merge_trees.py`)

Separate "figuring out what matches" from "building a mathematically sound tree". This is critical to support the upcoming Reclassification and Structural Shift features.

**Phase 1: Concept Alignment (`concept_matcher.py`)**
*   **Input**: N independent tree JSONs.
*   **Responsibilities**:
    *   Exact matches.
    *   Rename detection.
    *   Structural shift detection (Parent-Child Promotions, 1-to-N Splits).
    *   Value Restatement resolution (Tier 1 thresholds + Tier 2 LLM escalation).
*   **Output**: A `ConceptMap` (a unified dictionary mapping every concept/period to its canonical name and authoritative value, with audit metadata on *why* it merged that way).

**Phase 2: Unified Tree Building (`merge_trees.py`)**
*   **Input**: Skeleton tree + `ConceptMap`.
*   **Responsibilities**:
    *   Populating tree values using strictly the authoritative values from the `ConceptMap`.
    *   Orphan insertion & gap-reduction gates.
    *   Recomputing `__OTHER__` residuals to ensure `SUM(children) == declared`.
*   **Output**: Final merged tree.

### 3. Medium Priority: Sheet Builder (`sheet_builder.py`)

Split `sheet_builder.py` into a `sheets/` package. This isolates Google Sheets API interactions, visual layout engines, and mathematical string generation.

`sheet_builder.py` becomes a thin orchestrator/CLI.

**Proposed Structure:**
`sheets/`
*   `__init__.py` — Public API (`write_sheets`).
*   `api.py` — Google Sheets API wrappers (`gws_create`, `gws_write`).
*   `formulas.py` — Excel/Sheets formula generation (`dcol`, `_build_weight_formula`).
*   `layouts.py` — Spatial layout engines (`_cascade_layout`, `_totals_at_bottom_layout`).
*   `renderers.py` — Statement-specific rendering logic (`_render_sheet_body`, `_write_summary_tab`).
*   `formatting.py` — Formatting request builders (IB style fonts, borders, number formats).

### 4. Low Priority: Fact Parsing (`parse_xbrl_facts.py`)

Split `parse_xbrl_facts.py` (620 lines) into an `xbrl_facts/` package. Borderline priority, but useful to separate static tag maps from parsing logic.

**Proposed Structure:**
`xbrl_facts/`
*   `__init__.py` — `build_xbrl_facts_dict`, `build_segment_facts_dict`.
*   `parser.py` — iXBRL parsing and context extraction.
*   `tag_map.py` — Static data dictionaries (`IS_TAG_MAP`, `BS_TAG_MAP`, `CF_TAG_MAP`, `CODE_LABELS`).
*   `mapper.py` — Code mapping logic.

---

## Execution Plan

To avoid breaking the pipeline, these refactors should be executed sequentially, relying heavily on the existing test suite:

1.  **Execute XBRL Core Refactor**: Move code into the `xbrl/` package. Update imports. Verify all existing tests (`pytest tests/`) continue to pass without any business logic changes.
2.  **Execute Merge Layer Split**: Introduce the `ConceptMap` data structure. Extract existing rename detection into `concept_matcher.py`. Refactor `merge_trees.py` to consume the `ConceptMap`. Verify against the 10-company test set.
3.  **Execute Feature Dev**: Implement Structural Shifts (Promotions/Splits) and Reclassification Detection (Tier 1/Tier 2) cleanly on top of `concept_matcher.py`.
4.  **Execute Sheet Builder Refactor**: Move code into the `sheets/` package.
