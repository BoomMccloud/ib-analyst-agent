import sys

def dcol(i):
    """Data column letter(s). i=0 → E, i=1 → F, etc. (data starts col E)."""
    col_num = i + 4
    result = ""
    while col_num >= 0:
        result = chr(65 + col_num % 26) + result
        col_num = col_num // 26 - 1
    return result

def _build_weight_formula(col: str, child_rows: list[tuple[int, float]]) -> str:
    """Build a cell formula from child row numbers and XBRL weights."""
    if not child_rows:
        return ""
    if len(child_rows) == 1:
        r, w = child_rows[0]
        return f"={col}{r}" if w == 1.0 else f"=-{col}{r}"
    all_positive = all(w == 1.0 for _, w in child_rows)
    if all_positive:
        row_nums = [r for r, _ in child_rows]
        if row_nums == list(range(row_nums[0], row_nums[-1] + 1)):
            return f"=SUM({col}{row_nums[0]}:{col}{row_nums[-1]})"
        else:
            return "=" + "+".join(f"{col}{r}" for r, _ in child_rows)
    parts = []
    for r, w in child_rows:
        sign = "+" if w == 1.0 else "-"
        parts.append(f"{sign}{col}{r}")
    return "=" + "".join(parts).lstrip("+")

def _cell_ref(role, col, global_role_map):
    entry = global_role_map.get(role)
    if not entry:
        print(f"WARNING: Role {role} not found in global_role_map", file=sys.stderr)
        return "0"
    sheet_name, row_num = entry
    return f"'{sheet_name}'!{col}{row_num}"

def prev_period(p: str, periods: list[str]) -> str | None:
    idx = periods.index(p)
    return periods[idx - 1] if idx > 0 else None
