# Phase 3f: Combined Calculation and Presentation Linkbase Architecture

## Background & Motivation
Previous iterations of the pipeline (Phase 3d) attempted to use the Presentation Linkbase (`_pre.xml`) as the primary source of truth for the financial tree structure to ensure visual accuracy, while attempting to "zip" weights from the Calculation Linkbase (`_cal.xml`). 

Analysis of SEC XBRL data reveals a fundamental flaw in that approach: **the parent-child relationships in the presentation tree do not match those in the calculation tree.** 
* The presentation linkbase groups items using non-mathematical "Abstract" nodes, meaning `OperatingIncomeLoss` and `Revenues` might appear as siblings.
* The calculation linkbase defines the strict mathematical hierarchy where `OperatingIncomeLoss` is the parent of `Revenues`.

Using `_pre.xml` as the base tree destroys the mathematical relationships needed to generate accurate Google Sheet formulas (`=SUM(children * weight)`). However, using only `_cal.xml` results in an upside-down tree with arbitrarily ordered siblings.

## The Combined Approach

To achieve both **mathematical integrity** and **visual fidelity**, we must adopt a combined approach where `_cal.xml` defines the structure and `_pre.xml` defines the sorting index.

### 1. Structural Source of Truth: Calculation Linkbase (`_cal.xml`)
The base tree `TreeNode` hierarchy must be constructed exclusively from the Calculation Linkbase. This ensures that:
* Every node correctly identifies its mathematical children.
* Weights (+1 or -1) are inherently tied to the correct parent-child pairs.
* Formula generation logic remains perfectly intact.

### 2. Ordering Source of Truth: Presentation Linkbase (`_pre.xml`)
The Presentation Linkbase will be parsed purely as a **sorting index**, not as a tree structure. 
* We parse `_pre.xml` for a given statement role and create a flat dictionary mapping each concept to its sequential `order` index.
  * Example: `{'us-gaap_Revenues': 0, 'us-gaap_CostOfRevenue': 1, 'us-gaap_OperatingIncomeLoss': 5}`

### 3. Zipping via Sorting
Once the calculation tree is built, we traverse the tree. For every node that has children, we **sort the children array** based on each child's position in the presentation index.
* This fixes the arbitrary sibling order inherent in `_cal.xml`, aligning it perfectly with the 10-K/20-F filing's visual sequence.

### 4. Rendering: The Cascade Algorithm
Because the Calculation Linkbase structures Income Statements bottom-up (e.g., Net Income at the root), a standard pre-order traversal will render the statement upside down.
To flip the display order top-down (Revenue to Net Income) without changing the mathematical tree, we implement a **cascade rendering algorithm** for the IS:
1. Identify the "backbone child" (the +1 weight child that itself has children).
2. Recurse into the backbone first (unwinding up to Revenue).
3. Render the expense/leaf children (presentation-ordered).
4. Render the current node as the subtotal.

*(Note: Balance Sheets and Cash Flow statements generally root at the top and do not require cascade rendering).*

## Implementation Plan

1. **Update `xbrl_tree.py` Linkbase Fetching:**
   * Implement `fetch_pre_linkbase(html, base_url)` to download `_pre.xml`.
   * Keep existing `fetch_cal_linkbase` for `_cal.xml`.

2. **Implement Presentation Indexing:**
   * Create `parse_pre_linkbase_order(pre_xml)` that returns a flat mapping of `concept -> index`.

3. **Modify Tree Construction:**
   * Continue building the primary `TreeNode` structure using `parse_calc_linkbase()`.
   * Add a `sort_children_by_presentation(tree_root, pre_index)` function.
   * Apply this sorting pass to the trees immediately after construction.

4. **Update Rendering/Sheet Building (`sheet_builder.py`):**
   * Implement the cascade algorithm specifically for Income Statement roles when traversing the `TreeNode` structure to write rows into the Google Sheet.

## Validation
* Execute `run_pipeline.py NFLX`.
* The resulting `pipeline_output/nflx_trees.json` should still show `NetIncomeLoss` as the root of the IS, but its nested children should be strictly ordered according to the presentation linkbase.
* The Google Sheet output should visually start with `Revenues` and end with `NetIncomeLoss`, while all `=SUM()` formulas remain mathematically correct.
