import argparse
import json
from sheets import write_sheets

def main():
    parser = argparse.ArgumentParser(description="Render trees to Google Sheets")
    parser.add_argument("--trees", required=True, help="Path to trees JSON")
    parser.add_argument("--company", required=True, help="Company name")
    args = parser.parse_args()
    
    with open(args.trees) as f:
        raw_trees = json.load(f)
    
    from xbrl_tree import TreeNode
    trees = {}
    for k, v in raw_trees.items():
        if k in ("IS", "BS", "BS_LE", "CF") and isinstance(v, dict):
            trees[k] = TreeNode.from_dict(v)
        else:
            trees[k] = v
    
    sid, url = write_sheets(trees, args.company)
    print(json.dumps({"company": args.company, "url": url}))

if __name__ == '__main__':
    main()
