import re

with open('docs/impl_guide_phase3.md', 'r') as f:
    text = f.read()

# 1. Update Summary paragraph
text = text.replace(
    'The Summary tab has placeholder invariants (Cash Link = `=0`, D&A/SBC = "n/a"). If someone changes a number, nothing flows.',
    'The model lacks inline checks and proper number formatting.'
)
text = text.replace(
    'All 5 invariant checks become real cross-sheet formulas.',
    'All 5 invariant checks become real in-line formulas ("Check" rows). The sheet proves its own correctness, and formatting matches the exact spec.'
)

# 2. Update Problem 2
text = text.replace(
    '### Problem 2: Incomplete invariants\n\nThe Summary tab currently has:\n- BS Balance: real formula (`=BS!E4 - BS!E25 - BS!E30`)\n- Cash Link: hardcoded `=0`\n- NI Link: real formula\n- D&A Link: literal string "n/a"\n- SBC Link: literal string "n/a"\n\n3 of 5 invariants are broken or missing.',
    '### Problem 2: Incomplete invariants & Missing Formatting\n\nThe previous approach used a separate "Summary" tab with missing or placeholder checks. The actual specification requires:\n- **In-line Check Rows**: Invariants should be evaluated directly at the bottom of the relevant statements (e.g., `Check` below Total Assets).\n- **Metrics**: Key percentages (YoY Growth, Margins) must be appended to the bottom of the statements.\n- **Strict Number Formatting & Styling**: Specific Sheets API number formats (`CURRENCY`, `NUMBER`, zero-dashes) and styling (Italic labels for checks/metrics) are required.'
)

# 3. Update Architecture block
text = re.sub(
    r'- Summary: 5 cross-sheet invariant formulas\n\s+- All invariant rows must show 0',
    '- In-Line: "Check" rows and "Metrics" appended to statements\n                                       - Formatting: API batchUpdates applied to format numbers and styles',
    text
)

# 4. Replace Summary tab formula section
summary_section_regex = r'#### All 5 invariant formulas in Summary tab.*?---'
new_summary_section = """#### In-line Check Rows and Metrics Sections

Instead of a standalone `Summary` tab, the spec requires inserting `Check` and `Metrics` rows directly at the bottom of the relevant statement sections.

```python
def _append_statement_checks_and_metrics(rows, statement_type, periods, global_role_map):
    \"\"\"Appends in-line Check rows and Metrics at the end of a statement.\"\"\"
    
    def _cell_ref(role, col):
        entry = global_role_map.get(role)
        if not entry:
            return None
        _, row_num = entry
        return f"{col}{row_num}"

    def _add_check_row(formula_fn):
        row = ["", "", "Check", ""]
        for i in range(len(periods)):
            col = dcol(i)
            f = formula_fn(col)
            row.append(f if f else "n/a")
        rows.append(row)

    rows.append([""] * (4 + len(periods))) # Spacer

    if statement_type == "BS":
        # BS Balance check
        def bs_balance(col):
            ta = _cell_ref("BS_TA", col)
            tl = _cell_ref("BS_TL", col)
            te = _cell_ref("BS_TE", col)
            if ta and tl and te:
                return f"=ROUND({ta}-{tl}-{te},0)"
        _add_check_row(bs_balance)
        
    elif statement_type == "CF":
        # Cash Link check
        def cash_link(col):
            endc = _cell_ref("CF_ENDC", col)
            cash = _cell_ref("BS_CASH", col)
            if endc and cash:
                return f"=ROUND({endc}-{cash},0)"
        _add_check_row(cash_link)

        # Link checks (NI, D&A, SBC)
        def ni_link(col):
            is_ni = _cell_ref("INC_NET", col)
            cf_ni = _cell_ref("INC_NET_CF", col)
            if is_ni and cf_ni:
                return f"=ROUND({is_ni}-{cf_ni},0)"
        _add_check_row(ni_link)

    # Append Metrics section header
    rows.append(["", "", "Metrics", ""] + [""] * len(periods))
    # Note: Metrics generation logic to be added
```

---"""
text = re.sub(summary_section_regex, new_summary_section, text, flags=re.DOTALL)

# 5. Replace Formatting Rules section
fmt_rules_regex = r'## Formatting Rules \(DO NOT CHANGE\).*?---'
new_fmt_rules = """## Formatting Rules (Spec Alignment)

The Google Sheet must match the explicit formatting specification provided. `sheet_builder.py` must use the Google Sheets API (`batchUpdate`) to apply `repeatCell` styling.

### 1. Column Layout
The existing column architecture mapping will be preserved for the generation loop:
```
Columns:
  A-B: narrow (50px) — code/spacer, always ""
  C:   label (200px) — indented with "  " per tree depth
  D:   spacer, always ""
  E+:  data columns, one per period
```
*(Note: While the spec uses a 3-column prefix, we will retain the 4-column generation logic for now, adopting only points 2-4).*

### 2. Number Formats
Data cells use specific number format patterns based on the row type:
*   **Currency (Totals & Key Metrics):** `CURRENCY '"$"#,##0'`
    *   *Applied to roles like:* Revenue, Gross profit, Operating income, EBT, Net income.
*   **Standard Numbers:** `NUMBER '#,##0'`
    *   *Applied to:* All other standard XBRL line items (COGS, R&D, S&M, G&A).
*   **Check Rows / Zeroes:** `NUMBER '0.0x;(0.0x);-'`
    *   *Applied to:* "Check" row values (renders exactly 0 as a `-`).

### 3. Text Styling
*   **Italics:** Applied to the labels of specific structural rows. 
    *   The label `"Check"` must be italicized.
    *   The label `"Metrics"` must be italicized.
*   **Bolding:** No explicit bolding is required based on the spec.
*   **Background Colors:** Standard white background.

### 4. Structural Rows
Structural validation is enforced in-line, not in a separate Summary tab:
*   **Check Rows:** Inserted immediately after major totals (e.g., at the bottom of the Balance Sheet, after Cash Flows).
*   **Metrics Sections:** Inserted at the bottom of each statement (IS, BS, CF).

---"""
text = re.sub(fmt_rules_regex, new_fmt_rules, text, flags=re.DOTALL)

# 6. Update Test section references to Summary Tab
text = text.replace(
    'All 5 invariant rows in Summary contain strings starting with `=',
    'All `Check` rows contain strings starting with `='
)
text = text.replace(
    'TOTAL ERRORS formula is `=ABS(check1)+ABS(check2)+...+ABS(check5)`',
    'In-line Check rows render `0` as `-` using the proper NUMBER format'
)

# 7. Update Implementation Plan Step 5 and add Step 6
step5_regex = r'### Step 5: Rewrite `_write_summary_tab\(\)`.*?### Step 6'
new_step5 = """### Step 5: Inject In-Line Checks & Metrics

**Goal:** All 5 invariant checks are rendered as `Check` rows in-line at the bottom of the relevant statements.

1. Implement `_append_statement_checks_and_metrics()` to insert `Check` rows and the `Metrics` header at the end of the IS, BS, and CF sections.
2. Ensure formulas still use `ROUND(..., 0)` to absorb rounding noise.
3. Remove the standalone `_write_summary_tab()` logic.

### Step 6: Apply Google Sheets Formatting

**Goal:** Apply the exact `CURRENCY`, `NUMBER`, zero-dash, and Italic styles via the Sheets API.

1. Update the GWS writing utility to issue `batchUpdate` requests alongside the data payload.
2. Apply `CURRENCY '"$"#,##0'` to total nodes.
3. Apply `NUMBER '#,##0'` to standard leaves.
4. Apply `NUMBER '0.0x;(0.0x);-'` to `Check` rows.
5. Apply `{italic: true}` to `Check` and `Metrics` labels.

### Step 7"""
text = re.sub(step5_regex, new_step5, text, flags=re.DOTALL)

# Update Success Criteria
text = text.replace(
    '**All 5 invariant rows show 0** in the Summary tab for every historical period (including PG, which had a $1 rounding error — now absorbed by `ROUND()`).',
    '**All 5 invariant checks show `-` (zero)** in-line at the bottom of statements for every historical period.'
)
text = text.replace('Summary tab', 'in-line Check rows')

with open('docs/impl_guide_phase3.md', 'w') as f:
    f.write(text)
