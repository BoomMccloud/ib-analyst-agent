import json
from pathlib import Path
from bs4 import BeautifulSoup

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

    total_parents = 0
    total_mismatches = 0
    mismatch_details = []

    for comp, files in companies.items():
        if 'cal' not in files or 'pre' not in files:
            continue
            
        with open(files['cal'], 'rb') as f:
            cal_data = f.read().decode('utf-8', errors='ignore')
        with open(files['pre'], 'rb') as f:
            pre_data = f.read().decode('utf-8', errors='ignore')
            
        cal_roles = parse_calc_linkbase(cal_data)
        pre_roles = parse_pre_linkbase(pre_data)
        
        # compare roles
        for role, cal_children in cal_roles.items():
            pre_children = pre_roles.get(role, {})
            
            for parent, cal_list in cal_children.items():
                total_parents += 1
                c_children = [c for c, w in cal_list]
                
                # The pre_children might not have the parent, or might have different children.
                # Since pre tree might have abstract nodes, we want to find if the same children 
                # exist in the pre tree under some common ancestor or if we just flatten pre order.
                # Actually, presentation linkbase usually just flattens them or puts them under an abstract parent.
                # A better way is to find the relative order of `c_children` in the entire presentation linkbase for this role.
                
                # Let's get a flat ordered list of all concepts in the presentation role
                def flatten_pre(node, pre_tree, result):
                    if node not in result:
                        result.append(node)
                    for child in pre_tree.get(node, []):
                        flatten_pre(child, pre_tree, result)
                        
                # Find roots in pre_tree (nodes that are not children of any node)
                all_children = set()
                for p, clist in pre_children.items():
                    all_children.update(clist)
                roots = [p for p in pre_children if p not in all_children]
                
                flat_pre = []
                for r in roots:
                    flatten_pre(r, pre_children, flat_pre)
                
                # Now find the order of c_children in flat_pre
                pre_order_idx = {}
                for i, c in enumerate(flat_pre):
                    pre_order_idx[c] = i
                    
                # We can now see if c_children are ordered monotonically in pre_order_idx
                # Ignore children that are not in pre_order_idx
                valid_c = [c for c in c_children if c in pre_order_idx]
                if len(valid_c) > 1:
                    pre_sorted = sorted(valid_c, key=lambda x: pre_order_idx[x])
                    if valid_c != pre_sorted:
                        total_mismatches += 1
                        mismatch_details.append(
                            f"[{comp}] Role: {role.split('/')[-1]} | Parent: {parent}\n" +
                            f"  Calc Order : {valid_c}\n" +
                            f"  Pre  Order : {pre_sorted}\n"
                        )

    print(f"Total parent nodes checked across companies: {total_parents}")
    print(f"Total parent nodes with mismatched calc/pre order: {total_mismatches}")
    print("\nSample mismatches:")
    for m in mismatch_details[:10]:
        print(m)
        
    print("Mismatches per company:")
    from collections import Counter
    import re
    comp_mismatches = Counter([re.search(r'\[(.*?)\]', m).group(1) for m in mismatch_details])
    for c, count in comp_mismatches.items():
        print(f"  {c}: {count}")

if __name__ == '__main__':
    main()
