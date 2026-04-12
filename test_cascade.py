import json
from xbrl_tree import TreeNode
from sheet_builder import _cascade_layout

with open("pipeline_output/validation/NFLX/trees_2026-01-23.json") as f:
    trees_data = json.load(f)

is_tree_data = trees_data["IS"]
is_tree = TreeNode.from_dict(is_tree_data)

layout = _cascade_layout(is_tree, current_row=4, indent=0)
for row_num, indent, node in layout:
    print(f"Row {row_num}: {'  ' * indent}{node.concept}")
