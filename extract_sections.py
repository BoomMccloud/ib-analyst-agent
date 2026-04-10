"""
Stage 2a: Section Extractor
==========================
Downloads a filing HTML and slices it into individual section .txt files
based on TOC anchors. Pure stdlib + regex, no LLM required.

Usage: python extract_sections.py <url> --output-dir ./sections
Output: One file per section in output_dir, plus toc.json
"""

import argparse
import html as html_mod
import json
import os
import re
import sys

from sec_utils import fetch_url  # noqa: F401 — also provides HEADERS, but not needed here

# Sections we want to extract, with keyword patterns to match TOC entries
TARGET_SECTIONS = [
    {
        "id": "income_statement",
        "label": "Consolidated Income Statement / Statements of Operations",
        "patterns": [r"Statements of Operations", r"Statements of Income", r"Consolidated Statements of Income"],
    },
    {
        "id": "balance_sheet",
        "label": "Consolidated Balance Sheets",
        "patterns": [r"Balance Sheets"],
    },
    {
        "id": "cash_flows",
        "label": "Consolidated Statements of Cash Flows",
        "patterns": [r"Statements of Cash Flows"],
    },
    {
        "id": "mda",
        "label": "MD&A (Item 7/Item 5)",
        "patterns": [r"Management's Discussion and Analysis", r"Operating and Financial Review and Prospects"],
    },
    {
        "id": "notes",
        "label": "Notes to Financial Statements (Item 8)",
        "patterns": [r"Notes to Consolidated Financial Statements", r"Notes to Financial Statements"],
    },
]


def extract_toc(html_content: str) -> list[dict]:
    """Extract table of contents: list of {anchor, text} from <a href="#..."> links."""
    links = re.findall(
        r'<a\s+[^>]*href=["\']#([^"\']+)["\'][^>]*>(.*?)</a>',
        html_content,
        re.DOTALL | re.IGNORECASE
    )
    toc = []
    for anchor, raw_text in links:
        # Clean text: remove HTML tags, nbsp, and normalize whitespace
        text = re.sub(r'<[^>]+>', ' ', raw_text)
        text = text.replace('&nbsp;', ' ').replace('&#160;', ' ')
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            toc.append({"anchor": anchor, "text": text})
    return toc


def find_section_anchors(toc: list[dict]) -> dict:
    """Map TARGET_SECTIONS to the most likely TOC anchors."""
    anchors = {}
    for target in TARGET_SECTIONS:
        best_anchor = None
        for entry in toc:
            for pattern in target["patterns"]:
                if re.search(pattern, entry["text"], re.IGNORECASE):
                    best_anchor = entry["anchor"]
                    break
            if best_anchor:
                break
        if best_anchor:
            anchors[target["id"]] = best_anchor
    return anchors


def get_section_text(html_content: str, start_anchor: str, end_anchor: str | None) -> str:
    """Extract text between two anchors."""
    # Anchors are usually id="..." or name="..."
    # We find the start of the start_anchor block and the start of the end_anchor block
    start_match = re.search(rf'(?:id|name)=["\']{re.escape(start_anchor)}["\']', html_content, re.IGNORECASE)
    if not start_match:
        return ""

    if end_anchor:
        end_match = re.search(rf'(?:id|name)=["\']{re.escape(end_anchor)}["\']', html_content, re.IGNORECASE)
        if end_match:
            chunk = html_content[start_match.start():end_match.start()]
        else:
            chunk = html_content[start_match.start():]
    else:
        chunk = html_content[start_match.start():]

    # Convert HTML to text
    # 1. Replace <div>, <p>, <br>, <tr> with newlines to preserve structure
    text = re.sub(r'<(?:div|p|br|tr)[^>]*>', '\n', chunk, flags=re.IGNORECASE)
    # 2. Replace <td> with tabs
    text = re.sub(r'<td[^>]*>', '\t', text, flags=re.IGNORECASE)
    # 3. Strip all other tags
    text = re.sub(r'<[^>]+>', '', text)
    # 4. Decode entities and normalize whitespace
    text = html_mod.unescape(text)
    text = text.replace('\xa0', ' ')
    
    # Final cleanup: reduce multiple newlines and spaces
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    
    return text.strip()


def main():
    parser = argparse.ArgumentParser(description="Extract sections from SEC filing")
    parser.add_argument("url", help="URL to the SEC filing HTML")
    parser.add_argument("--output-dir", default="./sections", help="Output directory")
    args = parser.parse_args()

    out_dir = os.path.abspath(args.output_dir)
    os.makedirs(out_dir, exist_ok=True)

    print(f"Fetching {args.url}...")
    html_bytes = fetch_url(args.url)
    html_content = html_bytes.decode('utf-8', errors='ignore')

    print("Extracting TOC...")
    toc = extract_toc(html_content)
    section_map = find_section_anchors(toc)
    
    # Save the section map and TOC for debugging
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump({"url": args.url, "sections": section_map}, f, indent=2)

    # Sort anchors by their position in the HTML to find "next" anchors
    # We find all anchor locations
    anchor_positions = []
    for entry in toc:
        m = re.search(rf'(?:id|name)=["\']{re.escape(entry["anchor"])}["\']', html_content, re.IGNORECASE)
        if m:
            anchor_positions.append((m.start(), entry["anchor"]))
    anchor_positions.sort()

    def get_next_anchor(current_anchor):
        for i, (pos, name) in enumerate(anchor_positions):
            if name == current_anchor:
                if i + 1 < len(anchor_positions):
                    return anchor_positions[i+1][1]
        return None

    # Extract each target section
    for section_id, start_anchor in section_map.items():
        print(f"Extracting {section_id}...")
        end_anchor = get_next_anchor(start_anchor)
        text = get_section_text(html_content, start_anchor, end_anchor)
        
        if text:
            with open(os.path.join(out_dir, f"{section_id}.txt"), "w") as f:
                f.write(text)
            print(f"  Saved {len(text)} chars to {section_id}.txt")
        else:
            print(f"  Warning: No text found for {section_id}")

    print(f"Done. Extracted sections saved to {out_dir}")


if __name__ == "__main__":
    main()
