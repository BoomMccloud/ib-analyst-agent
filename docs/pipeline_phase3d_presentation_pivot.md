# Phase 3d: Pivot to Presentation Linkbase Architecture (Option B)

## Background & Motivation
The current pipeline parses the XBRL Calculation Linkbase (`_cal.xml`) to build the financial model trees. However, `_cal.xml` strictly defines mathematical hierarchies, completely ignoring the visual layout that human analysts expect. For example, Netflix nests "Revenues" three levels deep inside "Operating Income Loss" in its math tree.

To provide a deterministic, human-readable Google Sheet that visually matches the company's 10-K/20-F filing HTML perfectly, we must pivot the architecture to use the **Presentation Linkbase (`_pre.xml`)** as the primary source of truth for the tree structure and ordering.

## Scope & Impact
*   **Target File:** `xbrl_tree.py` (primary), `pymodel.py` (validation).
*   **Impact:** The trees built by the pipeline will now reflect the visual order (`order` attribute in `presentationArc`) of the filings instead of mathematical aggregations.
*   **Challenges:** The presentation linkbase does not define mathematical relationships (weights). We must still fetch `_cal.xml` and "zip" the weights into the presentation tree to ensure the Google Sheet formulas (`=SUM(children * weight)`) remain mathematically correct.

## Proposed Solution

1.  **Dual Linkbase Parsing:** 
    *   Update `xbrl_tree.py` to fetch *both* `_pre.xml` and `_cal.xml`.
    *   Implement `parse_pre_linkbase(pre_xml)` to extract the visual tree using `<link:presentationArc>` and sorting by the `order` attribute.
    *   Retain `parse_calc_linkbase(cal_xml)` to extract the `weight` mapping.

2.  **Tree Construction (Zipping):**
    *   Build the base tree from `_pre.xml` to guarantee the correct layout and hierarchy.
    *   Traverse the constructed tree and attach the `weight` from the `_cal.xml` mapping for each parent-child relationship.
    *   If a node in the presentation tree does not exist in the calculation tree (e.g., visual headers/abstracts), its weight will default to `0` or it will be marked as a non-calculating abstract node.

3.  **Statement Classification:**
    *   The `classify_roles()` logic will be applied to the Presentation roles. As shown in our tests, `_pre.xml` uses the same standardized role strings (e.g., `CONSOLIDATEDSTATEMENTSOFOPERATIONS`).

4.  **Preserving Phase 3b Integrity:**
    *   The orphan supplementation and math verification rules (from the earlier 3b spec) will still apply, but they will operate on the Presentation tree augmented with Calculation weights.

## Implementation Plan

1.  **Step 1: Linkbase Fetching & Parsing (`xbrl_tree.py`)**
    *   Add `fetch_pre_linkbase(html, base_url)` alongside the existing `fetch_cal_linkbase`.
    *   Add `parse_pre_linkbase(pre_xml)` to build ordered children maps.
2.  **Step 2: Tree Zipping (`xbrl_tree.py`)**
    *   Rewrite `build_statement_trees()`: 
        *   Extract `all_pre_trees` and `all_cal_trees`.
        *   Use `all_pre_trees` to generate the `TreeNode` hierarchy.
        *   Inject weights from `all_cal_trees` into the corresponding `TreeNode`s.
3.  **Step 3: Handle Abstract Nodes**
    *   Presentation linkbases contain "Abstract" nodes (e.g., `us-gaap_IncomeStatementAbstract`). These nodes just serve as structural headers and have no facts/values. Update tree parsing to handle nodes ending with `Abstract` cleanly (e.g., skipping them if they have no children, or rendering them as bold headers in the sheet).
4.  **Step 4: Pipeline Gate & Verification**
    *   Ensure `pymodel.py`'s `verify_tree_completeness` accurately validates the math. Since the layout is now visual, we must ensure that parent-child math checks still correctly apply when weights are present.

## Alternatives Considered
*   **Option A (Semantic BFS on Calculation Linkbase):** Rejected. While it correctly finds the tags mathematically, forcing the Google Sheet layout to conform to the calculation linkbase creates an unintuitive model that does not map 1:1 with the human-readable HTML filing.

## Verification
*   Execute `run_pipeline.py NFLX`.
*   Validate that the `pipeline_output/nflx_trees.json` IS tree shows `Revenues` as a top-level child (or first meaningful child) of the root, matching the exact order of the Netflix HTML.
*   Validate that weights are correctly attached (`Revenues` = +1, `CostOfRevenue` = -1).
*   Run `pytest tests/` to ensure no regressions in existing invariants.
