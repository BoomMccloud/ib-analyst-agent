# How _cal.xml and _pre.xml Work Together

## The Problem

Every SEC filing references two linkbase files that describe financial statement structure from different angles:

- **`_cal.xml`** (Calculation Linkbase) — defines the **math**: which items sum to which totals, with weights (+1 for addition, -1 for subtraction).
- **`_pre.xml`** (Presentation Linkbase) — defines the **display order**: which items appear first, second, third on each statement.

Neither is sufficient alone. The calc linkbase builds the IS tree upside-down (Net Income at root, Revenue buried 3 levels deep). The presentation linkbase has the right order but no math relationships. **We use both: calc for formulas, presentation for ordering.**

## Calculation Linkbase (`_cal.xml`)

### What it contains

Parent-child relationships grouped by statement role, with weights:

```xml
<link:calculationLink xlink:role=".../CONSOLIDATEDSTATEMENTSOFOPERATIONS">
  <!-- NetIncomeLoss = EBT(+1) + Tax(-1) -->
  <link:calculationArc xlink:from="NetIncomeLoss" xlink:to="EBT" weight="1"/>
  <link:calculationArc xlink:from="NetIncomeLoss" xlink:to="IncomeTaxExpenseBenefit" weight="-1"/>

  <!-- EBT = OperatingIncome(+1) + Interest(-1) + OtherIncome(+1) -->
  <link:calculationArc xlink:from="EBT" xlink:to="OperatingIncomeLoss" weight="1"/>
  <link:calculationArc xlink:from="EBT" xlink:to="InterestExpense" weight="-1"/>
  <link:calculationArc xlink:from="EBT" xlink:to="NonoperatingIncomeExpense" weight="1"/>

  <!-- OperatingIncome = Revenue(+1) + COGS(-1) + SGA(-1) + R&D(-1) + G&A(-1) -->
  <link:calculationArc xlink:from="OperatingIncomeLoss" xlink:to="Revenues" weight="1"/>
  <link:calculationArc xlink:from="OperatingIncomeLoss" xlink:to="CostOfRevenue" weight="-1"/>
  ...
</link:calculationLink>
```

### The tree it builds

The root is always the **bottom** of the financial statement — the final computed value:

```
NetIncomeLoss (root)
  + EBT
    + OperatingIncomeLoss
      + Revenues
      - CostOfRevenue
      - SellingAndMarketingExpense
      - ResearchAndDevelopmentExpense
      - GeneralAndAdministrativeExpense
    - InterestExpenseNonoperating
    + NonoperatingIncomeExpense
  - IncomeTaxExpenseBenefit
```

This is correct mathematically — NI = EBT - Tax, EBT = OpInc - Interest + Other, etc. But it's **upside-down** for display. An income statement starts with Revenue and ends with Net Income, not the reverse.

### What it gives us

- **Formulas**: every parent's value = `SUM(child * weight)`. These become sheet formulas like `=E4-E5-E6-E7-E8`.
- **Tree structure**: which items are subtotals of which line items.
- **Weights**: +1 (additive) or -1 (subtractive) per child.

### What it doesn't give us

- **Display order**: siblings under the same parent have no defined order. COGS, SGA, R&D, G&A could appear in any sequence.
- **Presentation direction**: the tree is always bottom-up (root = final total). No indication that Revenue should appear before COGS.

## Presentation Linkbase (`_pre.xml`)

### What it contains

An ordered tree of concepts per statement role, matching the exact display order in the filing:

```xml
<link:presentationLink xlink:role=".../CONSOLIDATEDSTATEMENTSOFOPERATIONS">
  <link:presentationArc order="1" xlink:from="IncomeStatementAbstract" xlink:to="Revenues"/>
  <link:presentationArc order="2" xlink:from="IncomeStatementAbstract" xlink:to="CostOfRevenue"/>
  <link:presentationArc order="3" xlink:from="IncomeStatementAbstract" xlink:to="SellingAndMarketingExpense"/>
  <link:presentationArc order="4" xlink:from="IncomeStatementAbstract" xlink:to="ResearchAndDevelopmentExpense"/>
  <link:presentationArc order="5" xlink:from="IncomeStatementAbstract" xlink:to="GeneralAndAdministrativeExpense"/>
  <link:presentationArc order="6" xlink:from="IncomeStatementAbstract" xlink:to="OperatingIncomeLoss"/>
  ...
</link:presentationLink>
```

### The ordered list it produces

A flat, top-down sequence — exactly as the line items appear in the 10-K:

```
 0. Revenues
 1. CostOfRevenue
 2. SellingAndMarketingExpense
 3. ResearchAndDevelopmentExpense
 4. GeneralAndAdministrativeExpense
 5. OperatingIncomeLoss
 6. InterestExpenseNonoperating
 7. NonoperatingIncomeExpense
 8. EBT
 9. IncomeTaxExpenseBenefit
10. NetIncomeLoss
11. EarningsPerShareBasic
12. EarningsPerShareDiluted
```

### What it gives us

- **Sibling order**: COGS before S&M before R&D before G&A (matching the filing).
- **Presentation direction**: Revenue at position 0, Net Income at position 10.
- **Abstract grouping nodes**: items like `NonoperatingIncomeExpenseAbstract` that create visual sections (not in the calc tree).

### What it doesn't give us

- **Math relationships**: no weights, no parent-child calculation structure. `OperatingIncomeLoss` appearing after `G&A` doesn't tell you that OpInc = Revenue - COGS - S&M - R&D - G&A.

## How They Work Together

### Step 1: Build the tree from `_cal.xml`

The calc linkbase gives us the tree with correct formulas:

```
NI (root)
  + EBT
    + OpInc
      + Revenue, - COGS, - S&M, - R&D, - G&A  ← arbitrary sibling order
    - Interest, + OtherIncome
  - Tax
```

### Step 2: Build the presentation order from `_pre.xml`

Parse `_pre.xml` into a concept → position index:

```python
pres_index = {
    "us-gaap_Revenues": 0,
    "us-gaap_CostOfRevenue": 1,
    "us-gaap_SellingAndMarketingExpense": 2,
    "us-gaap_ResearchAndDevelopmentExpense": 3,
    "us-gaap_GeneralAndAdministrativeExpense": 4,
    "us-gaap_OperatingIncomeLoss": 5,
    ...
}
```

### Step 3: Sort calc tree children by presentation order

At each level of the calc tree, sort the children by their position in `pres_index`:

```python
def sort_by_presentation(node, pres_index):
    if node.children:
        node.children.sort(key=lambda c: pres_index.get(c.concept, 999))
        for child in node.children:
            sort_by_presentation(child, pres_index)
```

After sorting, OpInc's children go from `[Revenue, G&A, R&D, COGS, S&M]` (arbitrary) to `[Revenue, COGS, S&M, R&D, G&A]` (matching the filing).

### Step 4: Cascade-render the IS tree

The calc tree is bottom-up (NI at root). For the sheet, we need top-down (Revenue at top). The cascade algorithm flips the display order without changing the tree structure:

At each node with children:
1. Find the **backbone child** — the +1 weight child that itself has children. This is the next cascade level up (e.g., NI's backbone is EBT, EBT's backbone is OpInc).
2. **Recurse into the backbone first** — this unwinds all the way to Revenue.
3. Render the **expense children** (weight -1 and +1 leaf children) indented.
4. Render **this node last** as the cascade subtotal.

Result:

```
Revenue               45,183,036     ← leaf value (deepest backbone)
  - Cost of Revenue   23,275,329     ← expenses, presentation-ordered
  - S&M                3,301,306
  - R&D                3,391,390
  - G&A                1,888,408
= Operating Income    13,326,603     ← formula: =Rev-COGS-S&M-R&D-G&A
  - Interest             776,510     ← expenses at this cascade level
  + Other Income         172,459
= EBT                12,722,552     ← formula: =OpInc-Interest+Other
  - Tax                1,741,351
= Net Income          10,981,201     ← formula: =EBT-Tax
```

This matches the NFLX 10-K line-for-line.

## Which Statements Need Cascade Rendering

| Statement | Calc root | Needs cascade? | Why |
|-----------|-----------|---------------|-----|
| IS | NetIncomeLoss (bottom) | **Yes** | Revenue must be at top, NI at bottom |
| BS | Assets (top) | No | Assets is already the top of the balance sheet |
| BS (L&E) | LiabilitiesAndEquity (top) | No | Already top-down |
| CF | NetChangeInCash (top) | No | Already top-down (OPCF → INVCF → FINCF → NETCH) |

Only the IS tree needs cascade rendering. BS and CF calc linkbases root at the top of their respective statements, so normal pre-order rendering (parent → children) produces the correct display order.

## Summary

| Linkbase | File | Gives us | Doesn't give us |
|----------|------|----------|-----------------|
| Calculation (`_cal.xml`) | Parent-child math with weights | Formulas, tree structure | Display order, presentation direction |
| Presentation (`_pre.xml`) | Ordered concept list per statement | Sibling order, top-down direction | Math relationships, weights |

**Combined**: calc provides the formulas, presentation provides the order. The cascade algorithm bridges the gap for IS where the calc tree is inverted relative to how analysts read the statement.
