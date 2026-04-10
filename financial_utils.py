"""
Financial Data Normalization Utilities
=======================================
Shared code definitions and flattening logic for Balance Sheet and Cash Flow
data, used by both structure_financials.py and build_model_sheet.py.
"""


BS_CODE_DEFS = {
    "BS_CASH": "Cash & Cash Equivalents (unrestricted only — restricted cash goes in BS_CA1/CA2/CA3)",
    "BS_AR": "Accounts Receivable, net",
    "BS_INV": "Inventories",
    "BS_CA1": "Other Current Asset bucket 1 (catch-all: marketable securities, prepaid, deferred tax assets, vendor receivables, etc.)",
    "BS_CA2": "Other Current Asset bucket 2 (catch-all)",
    "BS_CA3": "Other Current Asset bucket 3 (catch-all)",
    "BS_TCA": "Total Current Assets — SUBTOTAL only",
    "BS_PPE": "Property, Plant & Equipment, net",
    "BS_LTA1": "Other Long-term Asset bucket 1 (catch-all: goodwill, intangibles, long-term investments, operating lease ROU, etc.)",
    "BS_LTA2": "Other Long-term Asset bucket 2 (catch-all)",
    "BS_TNCA": "Total Non-Current Assets — SUBTOTAL only",
    "BS_TA": "Total Assets — GRAND TOTAL only",
    "BS_AP": "Accounts Payable",
    "BS_STD": "Short-term Debt / Current portion of long-term debt / Commercial Paper",
    "BS_OCL1": "Other Current Liability bucket 1 (catch-all: deferred revenue, accrued expenses, etc.)",
    "BS_OCL2": "Other Current Liability bucket 2 (catch-all)",
    "BS_TCL": "Total Current Liabilities — SUBTOTAL only",
    "BS_LTD": "Long-term Debt (non-current term debt, bonds)",
    "BS_NCL1": "Other Non-Current Liability bucket 1 (catch-all: deferred tax liabilities, operating lease liabilities, etc.)",
    "BS_NCL2": "Other Non-Current Liability bucket 2 (catch-all)",
    "BS_TNCL": "Total Non-Current Liabilities — SUBTOTAL only",
    "BS_TL": "Total Liabilities — GRAND TOTAL only",
    "BS_CS": "Common Stock & Additional Paid-In Capital",
    "BS_RE": "Retained Earnings / Accumulated Deficit",
    "BS_OE": "Other Equity (AOCI, treasury stock, minority interest, etc.)",
    "BS_TE": "Total Stockholders' Equity — SUBTOTAL only",
    "SKIP": "Skip — redundant totals like total_liabilities_and_shareholders_equity",
}

CF_CODE_DEFS = {
    "CF_NI": "Net Income",
    "CF_DA": "Depreciation & Amortization",
    "CF_SBC": "Stock-Based Compensation",
    "CF_OP1": "Other non-cash operating adjustment (catch-all: deferred taxes, impairments, amortization of debt discount/securities, gains/losses, etc.)",
    "CF_OP2": "Other working capital / operating change — assets side (catch-all: other current and non-current asset changes)",
    "CF_OP3": "Other working capital / operating change — liabilities side (catch-all: other current and non-current liability changes)",
    "CF_AR": "Change in Accounts Receivable",
    "CF_INV": "Change in Inventories",
    "CF_AP": "Change in Accounts Payable",
    "CF_OPCF": "Net Cash from Operations — SECTION SUBTOTAL only",
    "CF_CAPEX": "Capital Expenditures (purchases of property, plant, equipment)",
    "CF_SECPUR": "Purchases of Marketable Securities",
    "CF_SECSAL": "Proceeds from Sales/Maturities of Marketable Securities",
    "CF_INV1": "Other Investing (catch-all: acquisitions, divestitures, other investing activities)",
    "CF_INVCF": "Net Cash from Investing — SECTION SUBTOTAL only",
    "CF_FIN1": "Stock-related payments (taxes on RSU vesting, net share settlement) and other misc financing",
    "CF_DIV": "Dividends Paid",
    "CF_BUY": "Share Repurchases / Stock Buybacks",
    "CF_DISS": "Debt Issuance / Proceeds from borrowing",
    "CF_DREP": "Debt Repayment",
    "CF_FIN2": "Other Financing (catch-all: commercial paper, other financing activities)",
    "CF_FINCF": "Net Cash from Financing — SECTION SUBTOTAL only",
    "CF_NETCH": "Net increase/decrease in cash — GRAND TOTAL only",
    "CF_BEGC": "Cash and cash equivalents at BEGINNING of period (must match BS_CASH of prior period)",
    "CF_ENDC": "Cash and cash equivalents at END of period (must match BS_CASH of same period)",
}


def flatten_bs(bs_data: dict, periods: list[str]) -> list[dict]:
    """Flatten nested per-period BS data into items with unique IDs.

    Args:
        bs_data: Period-keyed balance sheet data (e.g., {"2024-09-28": {...}}).
        periods: Sorted list of period keys.

    Returns:
        List of dicts: [{"id": str, "key": str, "section": str, "values": {period: val}}].
    """
    all_items = {}

    for period in periods:
        pdata = bs_data.get(period, {})

        def collect(data, section_path):
            for key, value in data.items():
                if isinstance(value, (int, float)):
                    item_id = f"{section_path}/{key}" if section_path else key
                    if item_id not in all_items:
                        all_items[item_id] = {"id": item_id, "key": key, "section": section_path, "values": {}}
                    all_items[item_id]["values"][period] = value
                elif isinstance(value, dict):
                    collect(value, f"{section_path}/{key}" if section_path else key)

        collect(pdata, "")

    return list(all_items.values())


def flatten_cf(cf_data: dict, periods: list[str]) -> list[dict]:
    """Flatten nested CF JSON into items with unique IDs.

    Args:
        cf_data: Cash flow statement data with section keys.
        periods: Sorted list of period keys.

    Returns:
        List of dicts: [{"id": str, "key": str, "section": str, "values": {period: val}}].
    """
    items = []

    op_cf = cf_data.get("operating_activities",
                        cf_data.get("cash_flows_from_operating_activities", {}))
    inv_cf = cf_data.get("investing_activities",
                         cf_data.get("cash_flows_from_investing_activities", {}))
    fin_cf = cf_data.get("financing_activities",
                         cf_data.get("cash_flows_from_financing_activities", {}))

    skip_top = {"operating_activities", "cash_flows_from_operating_activities",
                "investing_activities", "cash_flows_from_investing_activities",
                "financing_activities", "cash_flows_from_financing_activities",
                "unit", "currency_note"}

    def collect(section_data, section_name):
        for key, value in section_data.items():
            if not isinstance(value, dict):
                continue
            period_vals = {p: value[p] for p in periods
                          if p in value and isinstance(value[p], (int, float))}
            if period_vals:
                item_id = f"{section_name}/{key}"
                items.append({"id": item_id, "key": key, "section": section_name, "values": period_vals})
            else:
                collect(value, f"{section_name}/{key}")

    collect(op_cf, "operating")
    collect(inv_cf, "investing")
    collect(fin_cf, "financing")

    # Top-level items: beginning/ending balances, net change
    for top_key, value in cf_data.items():
        if top_key in skip_top or not isinstance(value, dict):
            continue
        period_vals = {p: value[p] for p in periods
                       if p in value and isinstance(value[p], (int, float))}
        if period_vals:
            item_id = f"summary/{top_key}"
            items.append({"id": item_id, "key": top_key, "section": "summary", "values": period_vals})
        else:
            collect(value, top_key)

    return items


def clean_label(key: str) -> str:
    """Convert snake_case key to Title Case label."""
    return key.replace("_", " ").strip().title()
