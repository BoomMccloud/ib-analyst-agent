import logging
import json
import sys
from anthropic import Anthropic

from llm_utils import call_llm
from xbrl_tree import TreeNode, find_node_by_role
from pymodel import verify_model

logger = logging.getLogger(__name__)

def _prune_tree_for_llm(node: TreeNode, periods: list[str]) -> dict:
    """Return a simplified representation of the tree for the LLM to read."""
    def _walk(n):
        res = {
            "concept": n.concept,
        }
        if n.role:
            res["role"] = n.role
        
        # Only include periods that matter to keep context size manageable
        vals = {p: n.values[p] for p in periods if p in n.values}
        if vals:
            res["values"] = vals
            
        if n.children:
            res["children"] = [_walk(c) for c in n.children]
        return res
    return _walk(node)


def prompt_llm_for_fixes(trees_dict: dict, errors: list[tuple], periods: list[str]) -> list[dict]:
    """Ask Claude to identify the issue and propose a fix."""
    statements_to_include = set()
    for err_name, err_p, err_delta in errors:
        if "BS" in err_name:
            statements_to_include.add("BS")
            statements_to_include.add("BS_LE")
        if "IS" in err_name or "NI Link" in err_name or "D&A" in err_name or "SBC" in err_name:
            statements_to_include.add("IS")
        if "CF" in err_name or "Cash" in err_name or "NI Link" in err_name or "D&A" in err_name or "SBC" in err_name:
            statements_to_include.add("CF")

    context_trees = {}
    for stmt in statements_to_include:
        if stmt in trees_dict:
            # We must convert dict to TreeNode first to prune properly
            if isinstance(trees_dict[stmt], dict):
                tree_node = TreeNode.from_dict(trees_dict[stmt])
            else:
                tree_node = trees_dict[stmt]
            context_trees[stmt] = _prune_tree_for_llm(tree_node, periods)

    prompt = f"""
We have a financial modeling pipeline that checks invariants across statement trees.
Some invariants failed. We need you to identify the structural mismatch and provide a JSON patch.

Errors:
{json.dumps(errors, indent=2)}

Relevant Trees (simplified):
{json.dumps(context_trees, indent=2)}

Your task is to propose operations to fix these errors.
For example, if CF uses ProfitLoss (which includes noncontrolling interest) but IS uses NetIncomeLoss, 
you might need to move the INC_NET role on IS to ProfitLoss.

Allowed operations:
1. "move_role": Moves a semantic role (e.g., "INC_NET") to a different concept in the tree.
   Example: {{"op": "move_role", "statement": "IS", "role": "INC_NET", "new_concept": "us-gaap_ProfitLoss"}}
2. "change_weight": Changes the weight (1.0 or -1.0) of a child concept under a parent.
   Example: {{"op": "change_weight", "statement": "IS", "parent_concept": "us-gaap_NetIncomeLoss", "child_concept": "us-gaap_MinorityInterest", "weight": -1.0}}

Return ONLY a JSON array of operations, like:
[
  {{"op": "move_role", "statement": "IS", "role": "INC_NET", "new_concept": "us-gaap_ProfitLoss"}}
]
"""
    client = Anthropic()
    # Call Sonnet as it is a complex reasoning task
    try:
        response = call_llm(client, "claude-3-5-sonnet-20241022", prompt, max_tokens=2048)
        if isinstance(response, list):
            return response
        elif isinstance(response, dict) and "fixes" in response:
            return response["fixes"]
        else:
            logger.error(f"Unexpected LLM response format: {response}")
            return []
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return []


def apply_fixes(trees_dict: dict, fixes: list[dict]):
    """Apply the LLM's proposed operations to the tree dictionary."""
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        if stmt in trees_dict and isinstance(trees_dict[stmt], dict):
            trees_dict[stmt] = TreeNode.from_dict(trees_dict[stmt])

    for fix in fixes:
        stmt = fix.get("statement")
        if not stmt or stmt not in trees_dict:
            continue
        tree = trees_dict[stmt]

        def find_node(n, concept):
            if n.concept == concept: return n
            for c in n.children:
                res = find_node(c, concept)
                if res: return res
            return None

        if fix["op"] == "move_role":
            old_node = find_node_by_role(tree, fix["role"])
            if old_node:
                old_node.role = None
            new_node = find_node(tree, fix["new_concept"])
            if new_node:
                new_node.role = fix["role"]
                print(f"LLM Fix: Moved role {fix['role']} to {fix['new_concept']}", file=sys.stderr)
            else:
                print(f"LLM Fix Warning: Could not find new concept {fix['new_concept']}", file=sys.stderr)

        elif fix["op"] == "change_weight":
            parent = find_node(tree, fix["parent_concept"])
            if parent:
                for c in parent.children:
                    if c.concept == fix["child_concept"]:
                        c.weight = float(fix["weight"])
                        print(f"LLM Fix: Changed weight of {fix['child_concept']} under {fix['parent_concept']} to {fix['weight']}", file=sys.stderr)
                        break

    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        if stmt in trees_dict and isinstance(trees_dict[stmt], TreeNode):
            trees_dict[stmt] = trees_dict[stmt].to_dict()


def fix_invariants(trees_dict: dict) -> bool:
    """Check invariants, use LLM to fix if failing.
    
    Returns:
        True if all invariants pass (either originally or after LLM fix), False otherwise.
    """
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        if stmt in trees_dict and hasattr(trees_dict[stmt], "to_dict"):
            trees_dict[stmt] = trees_dict[stmt].to_dict()
            
    # Clone the dictionary to avoid mutating state if we fail
    temp_trees = json.loads(json.dumps(trees_dict))
    errors = verify_model(temp_trees)
    
    if not errors:
        return True
        
    print(f"Attempting to fix {len(errors)} invariant errors using LLM...", file=sys.stderr)
    periods = trees_dict.get("complete_periods", [])
    
    fixes = prompt_llm_for_fixes(trees_dict, errors, periods)
    if not fixes:
        print("LLM did not propose any fixes.", file=sys.stderr)
        return False
        
    print(f"LLM proposed {len(fixes)} fixes: {json.dumps(fixes)}", file=sys.stderr)
    
    apply_fixes(trees_dict, fixes)
    
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        if stmt in trees_dict and hasattr(trees_dict[stmt], "to_dict"):
            trees_dict[stmt] = trees_dict[stmt].to_dict()
            
    # Verify again
    temp_trees_after = json.loads(json.dumps(trees_dict))
    new_errors = verify_model(temp_trees_after)
    
    if new_errors:
        print(f"LLM fixes failed. Remaining errors: {new_errors}", file=sys.stderr)
        return False
        
    print("LLM successfully fixed all invariants!", file=sys.stderr)
    return True
