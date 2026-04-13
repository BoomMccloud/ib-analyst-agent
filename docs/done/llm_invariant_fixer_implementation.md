# LLM Invariant Fixer Implementation

**Date**: 2026-04-13
**Related Specs**: 
- `docs/todo/spec_multi_year_merge.md`
- `docs/todo/spec_reclassification_detection.md`

## Overview
Implemented an LLM-powered safety net in Phase 3 (`pymodel.py`) to automatically resolve structural and semantic mismatches that cause cross-statement invariants to fail.

## How It Relates to Existing Specs
This feature is highly related to the **Safety net** concept described in `docs/todo/spec_multi_year_merge.md` (Step 1c). 
While `spec_multi_year_merge.md` proposes an LLM-escalation phase in Phase 1 (`merge_trees.py`) to detect restatements, this implementation acts as an active safety net in **Phase 3 (Cross-Statement Verification)**. 

If deterministic alignment (or the lack of restatement handling) causes an invariant like `NI Link` or `Cash Link` to fail, the LLM Invariant Fixer is triggered. Instead of just halting the pipeline, it:
1. Prunes the XBRL trees to the relevant concepts, roles, and values.
2. Prompts Claude (Sonnet) to analyze the imbalance.
3. Dynamically applies structural patches (`move_role`, `change_weight`).
4. Re-verifies the model.

This directly addresses the edge cases where strict deterministic rules fail due to semantic shifts (e.g., NCI vs. Net Income differences), and provides a robust fallback for the deferred "Value Restatements" logic from `docs/todo/spec_reclassification_detection.md`.

## Changes Made
- **Created `llm_invariant_fixer.py`**: A new module that handles pruning the tree, prompting the LLM, parsing the JSON patch, and applying the `move_role` or `change_weight` operations.
- **Modified `pymodel.py`**: Integrated the fixer into `main()`. If `verify_model` returns errors, the script attempts an LLM fix before giving up. If successful, it rewrites the corrected trees back to disk.
