#!/usr/bin/env bash
# ============================================================================
# Phase 1 End-to-End Test — Alphabet (GOOGL)
# ============================================================================
# Runs the full pipeline: fetch → extract → structure → checkpoint
# Requires: ANTHROPIC_API_KEY in .env or environment
#
# Usage: bash test_phase1_e2e.sh
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

# Load .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# Ensure API key is set
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY not set. Add it to .env or export it."
    exit 1
fi

# Set SEC contact email if not already set
export SEC_CONTACT_EMAIL="${SEC_CONTACT_EMAIL:-boommccloud@gmail.com}"

WORK_DIR=$(mktemp -d)
TICKER="${1:-GOOGL}"
echo "=== Phase 1 E2E Test: $TICKER ==="
echo "Working directory: $WORK_DIR"
echo ""

# --------------------------------------------------------------------------
# Step 1: Resolve ticker → CIK → filing URL (using fetch_10k.py directly)
# --------------------------------------------------------------------------
echo "--- Step 1: Fetching 10-K filing URL for $TICKER ---"
FILINGS_JSON="$WORK_DIR/filings.json"
python fetch_10k.py "$TICKER" --count 1 > "$FILINGS_JSON"

# Extract the filing URL from the output
FILING_URL=$(python -c "
import json, sys
data = json.load(open('$FILINGS_JSON'))
filings = data.get('filings', data.get('results', [data]))
if isinstance(filings, list):
    f = filings[0]
else:
    f = filings
url = f.get('url', f.get('filing_url', f.get('primary_document_url', '')))
if not url:
    # Try to construct from accession number
    acc = f.get('accession_number', f.get('accessionNumber', ''))
    doc = f.get('primary_document', f.get('primaryDocument', ''))
    cik = str(f.get('cik', data.get('cik', ''))).zfill(10)
    if acc and doc:
        url = f'https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace(\"-\", \"\")}/{doc}'
print(url)
")

if [ -z "$FILING_URL" ]; then
    echo "ERROR: Could not extract filing URL. Raw output:"
    cat "$FILINGS_JSON"
    exit 1
fi
echo "Filing URL: $FILING_URL"
echo ""

# --------------------------------------------------------------------------
# Step 2: Extract sections from the filing HTML
# --------------------------------------------------------------------------
SECTIONS_DIR="$WORK_DIR/sections"
echo "--- Step 2: Extracting sections ---"
python extract_sections.py "$FILING_URL" --output-dir "$SECTIONS_DIR"
echo ""

# Verify we got the 3 required financial statements
for section in income_statement balance_sheet cash_flows; do
    if [ ! -f "$SECTIONS_DIR/${section}.txt" ]; then
        echo "ERROR: Missing $section section"
        exit 1
    fi
    SIZE=$(wc -c < "$SECTIONS_DIR/${section}.txt")
    echo "  $section: ${SIZE} bytes"
done
echo ""

# --------------------------------------------------------------------------
# Step 3: Structure the sections (uses Tool Use now)
# --------------------------------------------------------------------------
STRUCTURED="$WORK_DIR/googl_structured.json"
echo "--- Step 3: Structuring financials (LLM with Tool Use) ---"
python structure_financials.py "$SECTIONS_DIR" \
    --sections income_statement balance_sheet cash_flows \
    -o "$STRUCTURED"
echo ""

# Quick sanity check on the structured output
python -c "
import json, sys
data = json.load(open('$STRUCTURED'))
sections = [s for s in ['income_statement', 'balance_sheet', 'cash_flows'] if s in data]
print(f'Structured sections: {sections}')
for s in sections:
    sec = data[s]
    has_flex = '_flex_categories' in sec
    print(f'  {s}: flex_categories={has_flex}')
if len(sections) < 3:
    print('WARNING: Missing financial statement sections')
    sys.exit(1)
"
echo ""

# --------------------------------------------------------------------------
# Step 4: Run Phase 1 checkpoint (with retry loop)
# --------------------------------------------------------------------------
echo "--- Step 4: Running checkpoint verification ---"
python pymodel.py --financials "$STRUCTURED" --checkpoint

echo ""
if [ -f historical_baseline.json ]; then
    python -c "
import json
d = json.load(open('historical_baseline.json'))
print(f'Baseline: {len(d[\"periods\"])} periods, {len(d[\"items\"])} codes')
print(f'Periods: {d[\"periods\"]}')
"
    # Move baseline to work dir
    mv historical_baseline.json "$WORK_DIR/"
    echo ""
    echo "=== PHASE 1 PASSED ==="
    echo "Baseline saved to: $WORK_DIR/historical_baseline.json"
    echo "Structured data:   $STRUCTURED"
else
    echo "=== PHASE 1 FAILED ==="
    echo "No historical_baseline.json produced"
    exit 1
fi
