import re

with open('xbrl_tree.py', 'r') as f:
    content = f.read()

new_funcs = """
def parse_pre_linkbase(pre_xml: str) -> dict[str, dict[str, float]]:
    \"\"\"Parse presentation linkbase into {role: {concept: order_position}}.\"\"\"
    role_orders = {}
    current_role = None
    position_counter = 0

    for line in pre_xml.split('\\n'):
        role_match = re.search(r'xlink:role="([^"]+)"', line)
        if role_match and 'presentationLink' in line:
            current_role = role_match.group(1)
            role_orders[current_role] = {}
            position_counter = 0

        if 'presentationArc' in line and current_role:
            to_match = re.search(r'xlink:to="([^"]+)"', line)
            order_match = re.search(r'order="([^"]+)"', line)

            if to_match:
                concept = to_match.group(1)
                order = float(order_match.group(1)) if order_match else position_counter
                role_orders[current_role][concept] = order
                position_counter += 1

    return role_orders

def build_presentation_index(role_orders: dict, role_url: str) -> dict[str, float]:
    for role, concepts in role_orders.items():
        if role_url in role or role in role_url:
            return concepts

    cal_segment = role_url.rsplit('/', 1)[-1].upper()
    for role, concepts in role_orders.items():
        pre_segment = role.rsplit('/', 1)[-1].upper()
        if cal_segment == pre_segment:
            return concepts

    return {}

def sort_by_presentation(node, pres_index: dict[str, float]):
    if node.children:
        node.children.sort(key=lambda c: pres_index.get(c.concept, 999))
        for child in node.children:
            sort_by_presentation(child, pres_index)
"""

content = content.replace("def parse_calc_linkbase(cal_xml: str) -> dict:", new_funcs + "\n\ndef parse_calc_linkbase(cal_xml: str) -> dict:")

# Update reconcile_trees
reconcile_trees_old = """def reconcile_trees(trees: dict) -> dict:
    \"\"\"Tag key nodes by position and apply cross-statement value overrides.\"\"\"
    facts = trees.get("facts", {})

    # --- Step A: Tag Balance Sheet positions ---
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))"""

reconcile_trees_new = """def reconcile_trees(trees: dict, pres_index: dict) -> dict:
    \"\"\"Tag key nodes by position and apply cross-statement value overrides.\"\"\"
    facts = trees.get("facts", {})

    # --- Step A: Sort all tree children by presentation order ---
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            sort_by_presentation(tree, pres_index.get(stmt, {}))

    # --- Step B: Tag Balance Sheet positions ---
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))"""

content = content.replace(reconcile_trees_old, reconcile_trees_new)

# Fix tag_is_positions root check
tag_is_old = """def _tag_is_positions(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    \"\"\"Tag IS Net Income node using CF's NI as authoritative source.

    Strategy:
    1. Look for matching values in IS children.
    2. Tag that child node as INC_NET so sheet builder can reference it.
    \"\"\"
    if not is_tree or not is_tree.children:
        return

    cf_ni_values = None
    if cf_tree:"""

tag_is_new = """def _tag_is_positions(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    \"\"\"Tag IS Net Income node using CF's NI as authoritative source.\"\"\"
    if not is_tree:
        return

    cf_ni_values = None
    if cf_tree:"""

content = content.replace(tag_is_old, tag_is_new)

tag_is_old_strat = """    # Look for a child node with matching values
    def _values_match(v1, v2):
        if not v1 or not v2: return False
        # Match if values are within 0.5 for all shared periods
        shared = set(v1.keys()) & set(v2.keys())
        if not shared: return False
        return all(abs(v1[p] - v2[p]) < 0.5 for p in shared)

    matched = False
    for child in is_tree.children:
        if child.values and _values_match(child.values, cf_ni_values):
            child.role = "INC_NET"
            matched = True
            break

    # If no match found, tag the last child as a fallback
    if not matched and is_tree.children:
        last_child = is_tree.children[-1]
        last_child.role = "INC_NET"
        # Overwrite values with CF's NI values to ensure balance
        last_child.values = cf_ni_values.copy()"""

tag_is_new_strat = """    # Look for a child node with matching values
    def _values_match(v1, v2):
        if not v1 or not v2: return False
        # Match if values are within 0.5 for all shared periods
        shared = set(v1.keys()) & set(v2.keys())
        if not shared: return False
        return all(abs(v1[p] - v2[p]) < 0.5 for p in shared)

    # Strategy 1: Check if IS ROOT is Net Income
    if _values_match(is_tree.values, cf_ni_values):
        is_tree.role = "INC_NET"
        return

    # Strategy 2: Search depth-1 children for value match
    for child in is_tree.children:
        if child.values and _values_match(child.values, cf_ni_values):
            child.role = "INC_NET"
            return

    # Strategy 3: Fall back to last positive-weight child
    # DO NOT overwrite values - just tag the role
    for child in reversed(is_tree.children):
        if getattr(child, 'weight', 1.0) > 0 and child.values:
            print(f"WARNING: No IS node value-matched CF NI - tagging last positive child: {child.concept}", file=sys.stderr)
            child.role = "INC_NET"
            return"""

content = content.replace(tag_is_old_strat, tag_is_new_strat)

# Update build_statement_trees
build_trees_old = """    # Determine complete periods
    all_periods = set()
    for tag_vals in facts.values():
        all_periods.update(tag_vals.keys())
    result["periods"] = sorted(all_periods)

    # Reconcile: tag positions + apply cross-statement overrides
    reconcile_trees(result)"""

build_trees_new = """    # Fetch and parse pre linkbase
    pre_xml = fetch_pre_linkbase(html, base_url)
    role_orders = parse_pre_linkbase(pre_xml) if pre_xml else {}

    pres_index = {}
    for stmt, role in stmt_roles.items():
        pres_index[stmt] = build_presentation_index(role_orders, role)
        if stmt == "BS":
            pres_index["BS_LE"] = pres_index["BS"]

    # Determine complete periods
    all_periods = set()
    for tag_vals in facts.values():
        all_periods.update(tag_vals.keys())
    result["periods"] = sorted(all_periods)

    # Reconcile: sort, tag positions + apply cross-statement overrides
    reconcile_trees(result, pres_index)"""

content = content.replace(build_trees_old, build_trees_new)

with open('xbrl_tree.py', 'w') as f:
    f.write(content)
