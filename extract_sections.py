"""
SEC Filing Section Extractor
=============================
Downloads a 10-K or 20-F filing, extracts the table of contents,
and slices out specific sections by anchor ID.

Step 1 (deterministic): Download HTML, parse TOC, extract section HTML between anchors.
Step 2 (LLM):           Caller sends extracted sections to an LLM for structuring.

Usage:
  python extract_sections.py <filing_url> --output-dir ./output
  python extract_sections.py <filing_url> --toc-only   # just print the TOC

Output: One file per section in output_dir, plus toc.json
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

_contact = os.environ.get("SEC_CONTACT_EMAIL")
if not _contact:
    print("Error: SEC_CONTACT_EMAIL environment variable must be set (SEC EDGAR requires a real contact email)", file=sys.stderr)
    sys.exit(1)
HEADERS = {"User-Agent": f"SecFilingsAgent {_contact}"}

# Sections we want to extract, with keyword patterns to match TOC entries
TARGET_SECTIONS = [
    {
        "id": "income_statement",
        "label": "Consolidated Income Statement / Statements of Operations",
        "patterns": [
            r"consolidated\s+(income\s+statement|statements?\s+of\s+operations)",
        ],
    },
    {
        "id": "comprehensive_income",
        "label": "Statements of Comprehensive Income",
        "patterns": [
            r"(statements?\s+of\s+)?comprehensive\s+income",
        ],
    },
    {
        "id": "balance_sheet",
        "label": "Consolidated Balance Sheets",
        "patterns": [
            r"consolidated\s+balance\s+sheet",
        ],
    },
    {
        "id": "shareholders_equity",
        "label": "Statements of Changes in Shareholders' Equity",
        "patterns": [
            r"(changes?\s+in\s+)?(shareholders?|stockholders?).{0,5}(equity|equities)",
        ],
    },
    {
        "id": "cash_flows",
        "label": "Consolidated Statements of Cash Flows",
        "patterns": [
            r"(consolidated\s+)?(statements?\s+of\s+)?cash\s+flows?",
        ],
    },
    {
        "id": "notes",
        "label": "Notes to Consolidated Financial Statements",
        "patterns": [
            r"notes?\s+to\s+(the\s+)?(consolidated\s+)?financial\s+statements?",
        ],
    },
    {
        "id": "mda",
        "label": "Management's Discussion and Analysis",
        "patterns": [
            r"management.{0,5}s?\s+discussion\s+and\s+analysis",
            r"operating\s+and\s+financial\s+review\s+and\s+prospects",  # 20-F equivalent
        ],
    },
    {
        "id": "business",
        "label": "Business Overview (Item 1 / Item 4)",
        "patterns": [
            r"^Business$",
            r"^INFORMATION\s+ON\s+THE\s+COMPANY$",  # 20-F Item 4
        ],
    },
]


def fetch_url(url: str) -> bytes:
    for attempt in range(3):
        try:
            time.sleep(0.15)  # rate limit
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(10 * (attempt + 1))
            else:
                raise


def extract_toc(html_content: str) -> list[dict]:
    """Extract table of contents: list of {anchor, text} from <a href="#..."> links."""
    links = re.findall(
        r'<a\s[^>]*href=["\']#([^"\']*)["\'][^>]*>(.*?)</a>',
        html_content, re.IGNORECASE | re.DOTALL
    )

    toc = []
    seen = set()  # dedupe by (anchor, text) pair
    for anchor, raw_text in links:
        text = re.sub(r'<[^>]+>', '', raw_text).strip()
        text = re.sub(r'\s+', ' ', text)
        text = html_mod.unescape(text)
        key = (anchor, text)
        if text and len(text) > 3 and key not in seen:
            seen.add(key)
            toc.append({"anchor": anchor, "text": text})

    return toc


def match_toc_to_sections(toc: list[dict]) -> dict[str, dict]:
    """Match TOC entries to our target sections. Returns {section_id: {anchor, text}}."""
    matches = {}
    for section in TARGET_SECTIONS:
        for entry in toc:
            for pattern in section["patterns"]:
                if re.search(pattern, entry["text"], re.IGNORECASE):
                    if section["id"] not in matches:
                        matches[section["id"]] = {
                            "anchor": entry["anchor"],
                            "text": entry["text"],
                            "label": section["label"],
                        }
                    break
    return matches


def find_anchor_positions(html_content: str, anchors: list[str]) -> dict[str, int]:
    """Find byte positions of anchor targets (id= or name=) in the HTML."""
    positions = {}
    for anchor in anchors:
        # Match id="anchor" or name="anchor" — these are the targets
        pattern = re.compile(
            rf'(?:id|name)\s*=\s*["\']({re.escape(anchor)})["\']',
            re.IGNORECASE
        )
        match = pattern.search(html_content)
        if match:
            positions[anchor] = match.start()
    return positions


def extract_section_html(html_content: str, start_anchor: str,
                         all_anchor_positions: dict[str, int]) -> str:
    """Extract HTML content between start_anchor and the next anchor."""
    if start_anchor not in all_anchor_positions:
        return ""

    start_pos = all_anchor_positions[start_anchor]

    # Find the next anchor after this one
    next_pos = len(html_content)
    for anchor, pos in all_anchor_positions.items():
        if pos > start_pos and pos < next_pos:
            next_pos = pos

    # Extract and limit size (some sections like notes can be huge)
    section_html = html_content[start_pos:next_pos]
    return section_html


def html_to_text(html_content: str) -> str:
    """Simple HTML to text conversion, preserving table structure."""
    # Replace table cells with tabs, rows with newlines
    text = re.sub(r'</tr>', '\n', html_content, flags=re.IGNORECASE)
    text = re.sub(r'</td>', '\t', text, flags=re.IGNORECASE)
    text = re.sub(r'</th>', '\t', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    text = html_mod.unescape(text)
    # Collapse whitespace but preserve structure
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


def extract_headcount(html_content: str) -> dict | None:
    """Search the full filing for employee/headcount numbers."""
    text = html_to_text(html_content)

    patterns = [
        # "approximately 164,000 full-time equivalent employees"
        r'approximately\s+([\d,]+)\s+(?:full.time\s+(?:equivalent\s+)?)?employees',
        # "had 164,000 employees"
        r'had\s+([\d,]+)\s+(?:full.time\s+(?:equivalent\s+)?)?employees',
        # "164,000 full-time employees"
        r'([\d,]+)\s+full.time\s+(?:equivalent\s+)?employees',
        # "total of 164,000 employees"
        r'total\s+of\s+([\d,]+)\s+(?:full.time\s+)?employees',
        # "employed approximately 164,000 people"
        r'employed\s+approximately\s+([\d,]+)\s+(?:full.time\s+)?(?:people|persons|employees)',
        # "our workforce of approximately 164,000"
        r'workforce\s+of\s+approximately\s+([\d,]+)',
        # "XXX,XXX employees as of"
        r'([\d,]+)\s+employees\s+as\s+of',
    ]

    results = []
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            num_str = m.group(1).replace(",", "")
            try:
                num = int(num_str)
                if 100 < num < 10_000_000:  # reasonable headcount range
                    # Get surrounding context
                    start = max(0, m.start() - 50)
                    end = min(len(text), m.end() + 100)
                    context = text[start:end].strip()
                    context = re.sub(r'\s+', ' ', context)
                    results.append({"count": num, "context": context[:200]})
            except ValueError:
                continue

    if results:
        # Return the largest number (likely total headcount, not a subset)
        best = max(results, key=lambda r: r["count"])
        return best
    return None


def main():
    parser = argparse.ArgumentParser(description="Extract sections from SEC filing")
    parser.add_argument("url", help="URL to the SEC filing HTML")
    parser.add_argument("--output-dir", default="./sections", help="Output directory")
    parser.add_argument("--toc-only", action="store_true", help="Only print the TOC")
    parser.add_argument("--max-section-chars", type=int, default=500_000,
                        help="Max chars per section (default: 500k)")
    args = parser.parse_args()

    # Download the filing
    print(f"Downloading: {args.url}", file=sys.stderr)
    raw = fetch_url(args.url)
    html_content = raw.decode("utf-8", errors="replace")
    print(f"  Size: {len(html_content):,} chars", file=sys.stderr)

    # Extract TOC
    toc = extract_toc(html_content)
    print(f"  TOC entries: {len(toc)}", file=sys.stderr)

    if args.toc_only:
        print(json.dumps(toc, indent=2))
        return

    # Match TOC to target sections
    section_matches = match_toc_to_sections(toc)

    print(f"\nMatched sections:", file=sys.stderr)
    for sid, info in section_matches.items():
        print(f"  {sid}: #{info['anchor']} -> {info['text'][:80]}", file=sys.stderr)

    missing = [s["id"] for s in TARGET_SECTIONS if s["id"] not in section_matches]
    if missing:
        print(f"\n  WARNING: Could not match: {', '.join(missing)}", file=sys.stderr)

    # Get all anchor positions for slicing
    all_anchors = [entry["anchor"] for entry in toc]
    anchor_positions = find_anchor_positions(html_content, all_anchors)

    # Extract each section
    os.makedirs(args.output_dir, exist_ok=True)

    results = {}
    for section_id, info in section_matches.items():
        section_html = extract_section_html(
            html_content, info["anchor"], anchor_positions
        )

        if not section_html:
            print(f"  WARNING: Empty section: {section_id}", file=sys.stderr)
            continue

        # Convert to text
        section_text = html_to_text(section_html)

        # Truncate if needed
        if len(section_text) > args.max_section_chars:
            print(f"  {section_id}: {len(section_text):,} chars (truncated to {args.max_section_chars:,})",
                  file=sys.stderr)
            section_text = section_text[:args.max_section_chars] + "\n\n[TRUNCATED]"
        else:
            print(f"  {section_id}: {len(section_text):,} chars", file=sys.stderr)

        # Save section text
        out_path = os.path.join(args.output_dir, f"{section_id}.txt")
        with open(out_path, "w") as f:
            f.write(section_text)

        results[section_id] = {
            "label": info["label"],
            "toc_text": info["text"],
            "anchor": info["anchor"],
            "chars": len(section_text),
            "file": out_path,
        }

    # Extract headcount from full filing
    headcount = extract_headcount(html_content)
    if headcount:
        results["_headcount"] = headcount
        print(f"\n  Headcount found: {headcount}", file=sys.stderr)

    # Save manifest
    manifest_path = os.path.join(args.output_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nSaved {len(results)} sections to {args.output_dir}/", file=sys.stderr)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
