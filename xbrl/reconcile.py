import re
import sys
from .tree import TreeNode, find_node_by_role, _find_parent

CROSS_STATEMENT_CHECKS = [
    {"name": "BS Balance (TA-TL-TE)", "roles": ["BS_TA", "BS_TL", "BS_TE"], "formula": "={BS_TA}-{BS_TL}-{BS_TE}"},
    {"name": "Cash Link (CF_ENDC-BS_CASH)", "roles": ["CF_ENDC", "BS_CASH"], "formula": "={left}-{right}"},
    {"name": "NI Link (IS-CF)", "roles": ["INC_NET", "INC_NET_CF"], "formula": "={left}-{right}"},
    {"name": "D&A Link (IS-CF)", "roles": ["IS_DA", "CF_DA"], "formula": "={left}-{right}"},
    {"name": "SBC Link (IS-CF)", "roles": ["IS_SBC", "CF_SBC"], "formula": "={left}-{right}"},
]

def _tag_bs_positions(assets_tree: TreeNode | None, liab_eq_tree: TreeNode | None):
    if assets_tree and assets_tree.values:
        assets_tree.role = "BS_TA"
        found_tca = False
        for child in assets_tree.children:
            if child.children and not child.is_leaf:
                child.role = "BS_TCA"
                cash_node = None
                for grandchild in child.children:
                    bare = grandchild.concept
                    if ':' in bare:
                        bare = bare.split(':', 1)[1]
                    elif '_' in bare:
                        bare = bare.split('_', 1)[1]
                    if bare.lower().startswith("cashandcashequivalent"):
                        cash_node = grandchild
                        break
                if cash_node is None and child.children:
                    cash_node = child.children[0]
                    print(f"WARNING: No CashAndCashEquivalent* concept found in TCA children, falling back to position 0 ({cash_node.concept})", file=sys.stderr)
                if cash_node:
                    cash_node.role = "BS_CASH"
                found_tca = True
                break
        if not found_tca:
            print("WARNING: Could not identify BS_TCA (Current Assets) in Assets tree", file=sys.stderr)
    elif assets_tree:
        print("WARNING: Assets tree has no values — skipping BS tagging", file=sys.stderr)

    if liab_eq_tree and liab_eq_tree.children:
        liab_eq_tree.role = "BS_TLE"
        branch_children = [
            c for c in liab_eq_tree.children
            if c.values and any(v != 0 for v in c.values.values())
        ]

        if len(branch_children) >= 2:
            equity_node = branch_children[-1]
            equity_node.role = "BS_TE"

            liab_children = branch_children[:-1]

            if len(liab_children) == 1:
                liab_node = liab_children[0]
            else:
                liab_node = TreeNode("__LIABILITIES_SYNTHETIC", weight=1.0)
                liab_node.name = "Liabilities"
                liab_values = {}
                for child in liab_children:
                    for p, v in child.values.items():
                        liab_values[p] = liab_values.get(p, 0) + v
                    liab_node.add_child(child)
                liab_node.values = liab_values
                liab_eq_tree.children = [liab_node, equity_node]

            liab_node.role = "BS_TL"

            for child in liab_node.children:
                if child.children and not child.is_leaf:
                    child.role = "BS_TCL"
                    break

        elif len(branch_children) == 1:
            branch_children[0].role = "BS_TL"
            print("WARNING: Could not identify BS_TE (Equity) in L&E tree — only 1 non-zero branch child found", file=sys.stderr)
        else:
            print("WARNING: Could not identify BS_TL/BS_TE in L&E tree — no non-zero branch children found", file=sys.stderr)

def _find_by_keywords(tree: 'TreeNode', keywords: list[str], mode: str = "all", search: str = "dfs", leaf_only: bool = True, field: str = "name") -> 'TreeNode | None':
    match_fn = all if mode == "all" else any
    def _matches(node):
        if leaf_only and not node.is_leaf:
            return False
        text = getattr(node, field, "").lower()
        return match_fn(kw in text for kw in keywords)

    if search == "bfs":
        from collections import deque
        queue = deque([tree])
        while queue:
            node = queue.popleft()
            if _matches(node):
                return node
            for child in node.children:
                queue.append(child)
        return None
    else:
        if _matches(tree):
            return tree
        for child in tree.children:
            result = _find_by_keywords(child, keywords, mode=mode, search=search, leaf_only=leaf_only, field=field)
            if result:
                return result
        return None

def _tag_is_semantic(is_tree: 'TreeNode') -> None:
    if not is_tree:
        return
    cogs_keywords = ["costofgoods", "costofrevenue", "costofsales"]
    rev_keywords = ["revenue", "sales"]

    cogs_node = _find_by_keywords(is_tree, cogs_keywords, mode="any", search="bfs", leaf_only=False, field="concept")
    if cogs_node:
        cogs_node.role = "IS_COGS"

    from collections import deque
    queue = deque([is_tree])
    while queue:
        node = queue.popleft()
        concept_lower = node.concept.lower()
        is_rev = any(kw in concept_lower for kw in rev_keywords)
        is_cost = any(kw in concept_lower for kw in cogs_keywords)
        if is_rev and not is_cost:
            node.role = "IS_REVENUE"
            return
        for child in node.children:
            queue.append(child)

    if cogs_node:
        parent = _find_parent(is_tree, cogs_node)
        if parent:
            best, best_avg = None, 0
            for child in parent.children:
                if child is cogs_node or child.role:
                    continue
                avg = sum(abs(v) for v in child.values.values()) / max(len(child.values), 1)
                if avg > best_avg:
                    best, best_avg = child, avg
            if best:
                best.role = "IS_REVENUE"

def verify_tree_completeness(tree: 'TreeNode', periods: list[str]) -> list:
    errors = []
    def _check(node):
        if not node.children:
            return
        for period in periods:
            declared = node.values.get(period, 0)
            if declared == 0:
                continue
            computed = sum(c.values.get(period, 0) * c.weight for c in node.children)
            gap = declared - computed
            if abs(gap) > 1.0:
                errors.append((node.concept, period, gap))
        for child in node.children:
            _check(child)
    _check(tree)
    return errors

def merge_calc_pres(tree: 'TreeNode', pres_index: dict[str, float], periods: list[str]) -> 'TreeNode':
    for child in list(tree.children):
        merge_calc_pres(child, pres_index, periods)

    if not tree.children:
        return tree

    presented = []
    unpresented = []
    for child in tree.children:
        if child.concept in pres_index:
            presented.append(child)
        else:
            unpresented.append(child)

    presented.sort(key=lambda c: pres_index.get(c.concept, 999))
    tree.children = presented + unpresented

    residual_values = {}
    has_nonzero_residual = False
    for period in periods:
        declared = tree.values.get(period, 0)
        if declared == 0:
            continue
        computed = sum(c.values.get(period, 0) * c.weight for c in tree.children)
        gap = declared - computed
        residual_values[period] = gap
        if abs(gap) > 1.0:
            has_nonzero_residual = True

    if has_nonzero_residual:
        other = TreeNode(f"__OTHER__{tree.concept}", weight=1.0)
        other.name = "Other"
        other.values = residual_values
        other.is_leaf = True
        tree.add_child(other)

    return tree

def _find_leaf_by_timeseries(tree: TreeNode, periods: list[str], target_values: dict[str, float]) -> TreeNode | None:
    if tree.is_leaf and tree.values:
        matched = 0
        total = 0
        for p in periods:
            target = target_values.get(p, 0)
            actual = tree.values.get(p, 0)
            if target != 0:
                total += 1
                if abs(actual - target) < 0.5:
                    matched += 1
        if total > 0 and matched == total:
            return tree
    for child in tree.children:
        result = _find_leaf_by_timeseries(child, periods, target_values)
        if result:
            return result
    return None

def _tag_da_sbc_nodes(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    if not is_tree or not cf_tree:
        return
    
    cf_opcf = find_node_by_role(cf_tree, "CF_OPCF")
    if not cf_opcf:
        return
    
    periods = [p for p in (is_tree.values.keys() if is_tree.values else []) if is_tree.values.get(p, 0) != 0]
    if not periods:
        return
    
    is_da = _find_by_keywords(is_tree, ["depreciation"], mode="all", search="dfs", leaf_only=True, field="name")
    if not is_da:
        is_da = _find_by_keywords(is_tree, ["amortization"], mode="all", search="dfs", leaf_only=True, field="name")
    
    if is_da:
        is_da.role = "IS_DA"
        cf_da = _find_leaf_by_timeseries(cf_opcf, periods, is_da.values)
        if cf_da:
            cf_da.role = "CF_DA"
        else:
            print("WARNING: Could not find CF D&A node matching IS D&A values", file=sys.stderr)
    
    is_sbc = _find_by_keywords(is_tree, ["stock", "compensation"], mode="all", search="dfs", leaf_only=True, field="name")
    if not is_sbc:
        is_sbc = _find_by_keywords(is_tree, ["share", "compensation"], mode="all", search="dfs", leaf_only=True, field="name")
    
    if is_sbc:
        is_sbc.role = "IS_SBC"
        cf_sbc = _find_leaf_by_timeseries(cf_opcf, periods, is_sbc.values)
        if cf_sbc:
            cf_sbc.role = "CF_SBC"
        else:
            print("WARNING: Could not find CF SBC node matching IS SBC values", file=sys.stderr)

def _tag_cf_positions(cf_tree: TreeNode | None, facts: dict) -> dict | None:
    cf_endc_values = None

    if facts and cf_tree:
        root_tag = cf_tree.tag
        derived = re.sub(r'PeriodIncreaseDecrease.*$', '', root_tag)
        if derived != root_tag and derived in facts:
            cf_endc_values = facts[derived]

    if not cf_endc_values and facts:
        endc_tags = [
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        ]
        for tag in endc_tags:
            if tag in facts:
                cf_endc_values = facts[tag]
                break

    if not cf_tree:
        return cf_endc_values

    cf_tree.role = "CF_NETCH"

    CF_ROLE_MAP = {
        "NetCashProvidedByUsedInOperatingActivities": "CF_OPCF",
        "NetCashProvidedByUsedInInvestingActivities": "CF_INVCF",
        "NetCashProvidedByUsedInFinancingActivities": "CF_FINCF",
    }

    seen_roles = set()

    def _walk_and_tag(node: TreeNode):
        concept_name = node.concept.split('_', 1)[-1] if '_' in node.concept else node.concept

        for pattern, role in CF_ROLE_MAP.items():
            if concept_name.startswith(pattern) and role not in seen_roles and node.values:
                node.role = role
                seen_roles.add(role)
                return

        if concept_name in ("ProfitLoss", "NetIncomeLoss") and node.values and not node.children:
            node.role = "INC_NET_CF"

        for child in node.children:
            _walk_and_tag(child)

    _walk_and_tag(cf_tree)

    FX_PATTERNS = ["EffectOfExchangeRate", "EffectOfForeignExchangeRate"]
    fx_found = False
    for child in cf_tree.children:
        concept_name = child.concept.split('_', 1)[-1] if '_' in child.concept else child.concept
        for pat in FX_PATTERNS:
            if concept_name.startswith(pat) and child.values:
                child.role = "CF_FX"
                fx_found = True
                break

    if not fx_found and facts and "Excluding" in (cf_tree.tag or ""):
        FX_FACT_TAGS = [
            "us-gaap:EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "us-gaap:EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
            "us-gaap:EffectOfExchangeRateOnCashAndCashEquivalents",
        ]
        for tag in FX_FACT_TAGS:
            if tag in facts and facts[tag]:
                fx_node = TreeNode(tag)
                fx_node.concept = tag.replace(":", "_")
                fx_node.tag = tag
                fx_node.values = dict(facts[tag])
                fx_node.role = "CF_FX"
                cf_tree.add_child(fx_node)
                for period, fx_val in facts[tag].items():
                    cf_tree.values[period] = cf_tree.values.get(period, 0) + fx_val
                fx_found = True
                break

    opcf_node = find_node_by_role(cf_tree, "CF_OPCF")
    if opcf_node and not find_node_by_role(cf_tree, "INC_NET_CF"):
        def _find_ni_in_subtree(node: TreeNode):
            cn = node.concept.split('_', 1)[-1] if '_' in node.concept else node.concept
            if cn in ("ProfitLoss", "NetIncomeLoss") and node.values and not node.children:
                node.role = "INC_NET_CF"
                return True
            for child in node.children:
                if _find_ni_in_subtree(child):
                    return True
            return False
        _find_ni_in_subtree(opcf_node)

    expected_roles = {"CF_OPCF", "CF_INVCF", "CF_FINCF"}
    missing = expected_roles - seen_roles
    if missing:
        print(f"WARNING: Could not identify CF roles: {sorted(missing)}", file=sys.stderr)

    if not find_node_by_role(cf_tree, "INC_NET_CF"):
        print("WARNING: Could not identify INC_NET_CF (Net Income) in CF tree", file=sys.stderr)

    return cf_endc_values

def _tag_is_positions(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    if not is_tree:
        return

    cf_ni_values = None
    if cf_tree:
        cf_ni_node = find_node_by_role(cf_tree, "INC_NET_CF")
        if cf_ni_node:
            cf_ni_values = cf_ni_node.values

    if cf_ni_values:
        best_match = None
        root_matches = 0
        root_total = 0
        for p, cf_val in cf_ni_values.items():
            is_val = is_tree.values.get(p)
            if is_val is not None:
                root_total += 1
                if abs(is_val - cf_val) < 0.5:
                    root_matches += 1
        if root_total > 0 and root_matches == root_total:
            best_match = is_tree

        if not best_match:
            for child in is_tree.children:
                if not child.values:
                    continue
                matches = 0
                total = 0
                for p, cf_val in cf_ni_values.items():
                    is_val = child.values.get(p)
                    if is_val is not None:
                        total += 1
                        if abs(is_val - cf_val) < 0.5:
                            matches += 1
                if total > 0 and matches == total:
                    best_match = child
                    break

        if best_match:
            best_match.role = "INC_NET"
        else:
            fallback = None
            for child in is_tree.children:
                if child.weight > 0 and child.values:
                    fallback = child
                    break

            if fallback:
                print(f"WARNING: No IS child value-matched CF's NI — falling back to first positive-weight child: {fallback.name}", file=sys.stderr)
                fallback.role = "INC_NET"
            else:
                print("WARNING: Could not identify INC_NET in IS tree — tagging root as fallback", file=sys.stderr)
                is_tree.role = "INC_NET"
    else:
        print("WARNING: No CF NI values available — tagging IS root as INC_NET", file=sys.stderr)
        is_tree.role = "INC_NET"

def _override_bs_cash(assets_tree: TreeNode | None, cf_endc_values: dict | None):
    if not assets_tree or not cf_endc_values:
        return

    cash_node = find_node_by_role(assets_tree, "BS_CASH")
    tca_node = find_node_by_role(assets_tree, "BS_TCA")

    if not cash_node:
        return

    for period, new_val in cf_endc_values.items():
        old_val = cash_node.values.get(period, 0)
        delta = new_val - old_val
        cash_node.values[period] = new_val

        if tca_node and abs(delta) > 0.5:
            tca_node.values[period] = tca_node.values.get(period, 0) + delta
