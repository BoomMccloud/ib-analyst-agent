import re
from collections import defaultdict
from sec_utils import fetch_url

def fetch_cal_linkbase(html: str, base_url: str) -> str | None:
    schema_pat = re.compile(r'schemaRef[^>]*href="([^"]+)"', re.IGNORECASE)
    m = schema_pat.search(html)
    if not m:
        return None
    schema_href = m.group(1)
    if not schema_href.startswith('http'):
        schema_href = base_url + schema_href
    schema = fetch_url(schema_href).decode('utf-8', errors='replace')
    cal_pat = re.compile(r'href="([^"]*_cal\.xml)"', re.IGNORECASE)
    cal_m = cal_pat.search(schema)
    if not cal_m:
        return None
    cal_href = cal_m.group(1)
    if not cal_href.startswith('http'):
        cal_href = base_url + cal_href
    return fetch_url(cal_href).decode('utf-8', errors='replace')

def fetch_pre_linkbase(html: str, base_url: str) -> str | None:
    schema_pat = re.compile(r'schemaRef[^>]*href="([^"]+)"', re.IGNORECASE)
    m = schema_pat.search(html)
    if not m:
        return None
    schema_href = m.group(1)
    if not schema_href.startswith('http'):
        schema_href = base_url + schema_href
    schema = fetch_url(schema_href).decode('utf-8', errors='replace')
    pre_pat = re.compile(r'href="([^"]*_pre\.xml)"', re.IGNORECASE)
    pre_m = pre_pat.search(schema)
    if not pre_m:
        return None
    pre_href = pre_m.group(1)
    if not pre_href.startswith('http'):
        pre_href = base_url + pre_href
    return fetch_url(pre_href).decode('utf-8', errors='replace')

def fetch_lab_linkbase(html: str, base_url: str) -> str | None:
    schema_pat = re.compile(r'schemaRef[^>]*href="([^"]+)"', re.IGNORECASE)
    m = schema_pat.search(html)
    if not m:
        return None
    schema_href = m.group(1)
    if not schema_href.startswith('http'):
        schema_href = base_url + schema_href
    schema = fetch_url(schema_href).decode('utf-8', errors='replace')
    lab_pat = re.compile(r'href="([^"]*_lab\.xml)"', re.IGNORECASE)
    lab_m = lab_pat.search(schema)
    if not lab_m:
        return None
    lab_href = lab_m.group(1)
    if not lab_href.startswith('http'):
        lab_href = base_url + lab_href
    return fetch_url(lab_href).decode('utf-8', errors='replace')

def parse_lab_linkbase(lab_xml: str) -> dict[str, dict[str, str]]:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(lab_xml)
    ns = {
        'link': 'http://www.xbrl.org/2003/linkbase',
        'xlink': 'http://www.w3.org/1999/xlink',
    }
    labels = {}
    for label_link in root.findall('.//link:labelLink', ns):
        locs = {}
        for loc in label_link.findall('link:loc', ns):
            locs[loc.get('{http://www.w3.org/1999/xlink}label')] = \
                loc.get('{http://www.w3.org/1999/xlink}href', '')
        lab_texts = {}
        for lab in label_link.findall('link:label', ns):
            role = lab.get('{http://www.w3.org/1999/xlink}role', '')
            role_suffix = role.rsplit('/', 1)[-1] if '/' in role else role
            xlink_label = lab.get('{http://www.w3.org/1999/xlink}label')
            if xlink_label not in lab_texts:
                lab_texts[xlink_label] = {}
            lab_texts[xlink_label][role_suffix] = lab.text or ''
        for arc in label_link.findall('link:labelArc', ns):
            from_label = arc.get('{http://www.w3.org/1999/xlink}from')
            to_label = arc.get('{http://www.w3.org/1999/xlink}to')
            href = locs.get(from_label, '')
            texts = lab_texts.get(to_label, {})
            if href and texts:
                concept = href.split('#')[-1] if '#' in href else href
                if concept not in labels:
                    labels[concept] = {}
                labels[concept].update(texts)
    return labels

def get_label(concept_or_member: str, lab_labels: dict, prefer_terse: bool = True) -> str:
    key = concept_or_member.replace(':', '_', 1) if ':' in concept_or_member else concept_or_member
    entry = lab_labels.get(key, {})
    if prefer_terse and entry.get("terseLabel"):
        return entry["terseLabel"]
    if entry.get("label"):
        return entry["label"]
    name = key.split('_', 1)[-1] if '_' in key else key
    if name.endswith("Member"):
        name = name[:-6]
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    return name

def parse_pre_linkbase(pre_xml: str) -> dict[str, dict[str, float]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(pre_xml, 'xml')
    role_orders = {}
    for link in soup.find_all('presentationLink'):
        role = link.get('xlink:role')
        if not role:
            continue
        locators = {}
        for loc in link.find_all('loc'):
            label = loc.get('xlink:label')
            href = loc.get('xlink:href', '')
            concept = href.split('#')[-1] if '#' in href else href
            if label and concept:
                locators[label] = concept
        children_map = defaultdict(list)
        for arc in link.find_all('presentationArc'):
            from_label = arc.get('xlink:from')
            to_label = arc.get('xlink:to')
            order = float(arc.get('order', 0.0))
            parent_concept = locators.get(from_label)
            child_concept = locators.get(to_label)
            if parent_concept and child_concept:
                children_map[parent_concept].append((order, child_concept))
        for parent in children_map:
            children_map[parent].sort(key=lambda x: x[0])
            children_map[parent] = [c for _, c in children_map[parent]]
        all_children = set()
        for clist in children_map.values():
            all_children.update(clist)
        roots = [p for p in children_map if p not in all_children]
        flat_order = []
        def _flatten(node):
            if node not in flat_order:
                flat_order.append(node)
            for child in children_map.get(node, []):
                _flatten(child)
        for root in roots:
            _flatten(root)
        role_orders[role] = {concept: i for i, concept in enumerate(flat_order)}
    return role_orders

def parse_calc_linkbase(cal_xml: str) -> dict:
    loc_pat = re.compile(r'<link:loc[^>]*xlink:label="([^"]+)"[^>]*xlink:href="[^#]*#([^"]+)"', re.IGNORECASE)
    arc_pat = re.compile(r'<link:calculationArc[^>]*?xlink:from="([^"]+)"[^>]*?xlink:to="([^"]+)"[^>]*?weight="([^"]+)"', re.IGNORECASE | re.DOTALL)
    arc_pat2 = re.compile(r'<link:calculationArc[^>]*?weight="([^"]+)"[^>]*?xlink:from="([^"]+)"[^>]*?xlink:to="([^"]+)"', re.IGNORECASE | re.DOTALL)
    section_pat = re.compile(r'<link:calculationLink[^>]*xlink:role="([^"]+)"[^>]*>([\s\S]*?)</link:calculationLink>', re.IGNORECASE)
    results = {}
    for section_m in section_pat.finditer(cal_xml):
        role = section_m.group(1).split('/')[-1]
        body = section_m.group(2)
        sec_locs = {}
        for m in loc_pat.finditer(body):
            sec_locs[m.group(1)] = m.group(2)
        children = defaultdict(list)
        seen = set()
        for m in arc_pat.finditer(body):
            parent = sec_locs.get(m.group(1), m.group(1))
            child = sec_locs.get(m.group(2), m.group(2))
            weight = float(m.group(3))
            key = (parent, child)
            if key not in seen:
                children[parent].append((child, weight))
                seen.add(key)
        for m in arc_pat2.finditer(body):
            parent = sec_locs.get(m.group(2), m.group(2))
            child = sec_locs.get(m.group(3), m.group(3))
            weight = float(m.group(1))
            key = (parent, child)
            if key not in seen:
                children[parent].append((child, weight))
                seen.add(key)
        if children:
            results[role] = dict(children)
    return results

STATEMENT_ROLE_PATTERNS = {
    "IS": [r"consolidatedstatements?of(?:net)?(?:income|operations|earnings)",
           r"statements?of(?:consolidated)?(?:net)?(?:income|operations|earnings)",
           r"incomestatements?"],
    "BS": [r"consolidatedbalancesheets?",
           r"statements?of(?:consolidated)?financialposition",
           r"balancesheets?"],
    "CF": [r"consolidatedstatements?ofcashflows?",
           r"statements?of(?:consolidated)?cashflows?",
           r"cashflows?statements?"],
}

def classify_roles(roles: list[str]) -> dict:
    result = {}
    for role in roles:
        role_lower = role.lower().replace("_", "").replace("-", "")
        if "alternative" in role_lower:
            continue
        for stmt, patterns in STATEMENT_ROLE_PATTERNS.items():
            if stmt in result:
                continue
            for pat in patterns:
                if re.search(pat, role_lower):
                    result[stmt] = role
                    break
    return result
