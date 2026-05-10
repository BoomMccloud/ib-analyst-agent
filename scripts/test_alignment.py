import json
from pathlib import Path
from bs4 import BeautifulSoup
from collections import Counter
import re

def parse_calc_linkbase(cal_xml: str) -> dict:
    soup = BeautifulSoup(cal_xml, 'xml')
    links = soup.find_all('calculationLink')
    result = {}
    for link in links:
        role = link.get('xlink:role')
        children = {}
        locators = {loc.get('xlink:label'): loc.get('xlink:href').split('#')[-1] 
                   for loc in link.find_all('loc')}
        for arc in link.find_all('calculationArc'):
            parent_id = locators.get(arc.get('xlink:from'))
            child_id = locators.get(arc.get('xlink:to'))
            weight = float(arc.get('weight', 1.0))
            order = float(arc.get('order', 0.0))
            if parent_id and child_id:
                if parent_id not in children:
                    children[parent_id] = []
                children[parent_id].append((order, child_id, weight))
        
        for parent in children:
            children[parent].sort(key=lambda x: x[0])
            children[parent] = [(c, w) for _, c, w in children[parent]]
            
        result[role] = children
    return result

def parse_pre_linkbase(pre_xml: str) -> dict:
    soup = BeautifulSoup(pre_xml, 'xml')
    links = soup.find_all('presentationLink')
    result = {}
    for link in links:
        role = link.get('xlink:role')
        children = {}
        locators = {loc.get('xlink:label'): loc.get('xlink:href').split('#')[-1] 
                   for loc in link.find_all('loc')}
        for arc in link.find_all('presentationArc'):
            parent_id = locators.get(arc.get('xlink:from'))
            child_id = locators.get(arc.get('xlink:to'))
            order = float(arc.get('order', 0.0))
            if parent_id and child_id:
                if parent_id not in children:
                    children[parent_id] = []
                children[parent_id].append((order, child_id))
                
        for parent in children:
            children[parent].sort(key=lambda x: x[0])
            children[parent] = [c for _, c in children[parent]]
            
        result[role] = children
    return result

def get_company_from_url(url):
    return url.split('/')[-1].split('-')[0].upper()

def flatten_pre(node, pre_tree, result):
    if node not in result:
        result.append(node)
    for child in pre_tree.get(node, []):
        flatten_pre(child, pre_tree, result)

def main():
    url_map_path = Path("tests/fixtures/sec_filings/url_map.json")
    with open(url_map_path) as f:
        url_map = json.load(f)
        
    fixtures_dir = Path("tests/fixtures/sec_filings")
    
    companies = {}
    for url, filename in url_map.items():
        if url.endswith('_cal.xml'):
            comp = get_company_from_url(url)
            if comp not in companies: companies[comp] = {}
            companies[comp]['cal'] = fixtures_dir / filename
        elif url.endswith('_pre.xml'):
            comp = get_company_from_url(url)
            if comp not in companies: companies[comp] = {}
            companies[comp]['pre'] = fixtures_dir / filename

    stats = {
        'total_parents': 0,
        'matched_all': 0,
        'had_placed_items': 0,
        'had_other_gaps': 0
    }
    
    sample_outputs = []

    for comp, files in companies.items():
        if 'cal' not in files or 'pre' not in files:
            continue
            
        with open(files['cal'], 'rb') as f:
            cal_data = f.read().decode('utf-8', errors='ignore')
        with open(files['pre'], 'rb') as f:
            pre_data = f.read().decode('utf-8', errors='ignore')
            
        cal_roles = parse_calc_linkbase(cal_data)
        pre_roles = parse_pre_linkbase(pre_data)
        
        for role, cal_children in cal_roles.items():
            pre_children = pre_roles.get(role, {})
            
            # Find roots in pre_tree
            all_pre_children = set()
            for p, clist in pre_children.items():
                all_pre_children.update(clist)
            roots = [p for p in pre_children if p not in all_pre_children]
            
            flat_pre = []
            for r in roots:
                flatten_pre(r, pre_children, flat_pre)
                
            pre_order_idx = {c: i for i, c in enumerate(flat_pre)}
            
            for parent, cal_list in cal_children.items():
                stats['total_parents'] += 1
                c_children = [(c, w) for c, w in cal_list]
                
                # 1. Match
                matched = [(c, w) for c, w in c_children if c in pre_order_idx]
                # sort matched by pre_order
                matched.sort(key=lambda x: pre_order_idx[x[0]])
                
                # 2. Place
                unmatched = [(c, w) for c, w in c_children if c not in pre_order_idx]
                
                # Final combined list for this parent
                # We place unmatched items at the end for this test
                combined = matched + unmatched
                
                # 3. Other Gap (simulated, as we don't have numbers here, we just insert a placeholder if there are many unmatched or as a simulated step)
                # In a real run, this would be if sum(children_values) != parent_value.
                # Here we just flag if we had to do 'Place'
                
                if len(unmatched) == 0:
                    stats['matched_all'] += 1
                else:
                    stats['had_placed_items'] += 1
                    
                # We also track if the calc order originally was different from combined
                orig_c = [c for c, w in c_children]
                comb_c = [c for c, w in combined]
                
                if orig_c != comb_c and len(sample_outputs) < 10:
                    sample_outputs.append(
                        f"[{comp}] Role: {role.split('/')[-1]} | Parent: {parent}\n" +
                        f"  Original Calc : {orig_c}\n" +
                        f"  1. Matched    : {[c for c,w in matched]}\n" +
                        f"  2. Placed     : {[c for c,w in unmatched]}\n" +
                        f"  -> Combined   : {comb_c}\n"
                    )

    print("=== Alignment Test Results ===")
    print(f"Total parents evaluated: {stats['total_parents']}")
    print(f"Parents with ALL children matched in Pre: {stats['matched_all']}")
    print(f"Parents requiring 'Place' (Calc items not in Pre): {stats['had_placed_items']}")
    
    print("\nSample Algorithm Applications:")
    for out in sample_outputs:
        print(out)

if __name__ == '__main__':
    main()