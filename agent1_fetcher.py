"""
Agent 1: Filing Fetcher
=======================
Takes a company name or ticker, looks up CIK + filer type, then fetches
the last N years of annual filing URLs.

Flow:
  1. lookup_company.py → determines ticker, CIK, domestic vs foreign
  2. fetch_10k.py or fetch_20f.py → gets filing URLs

Runs inside a Claude Managed Agent container.

Usage: python agent1_fetcher.py BABA [--years 5]
       python agent1_fetcher.py "Alibaba" [--years 5]
       python agent1_fetcher.py AAPL [--years 5]

Output: JSON to stdout
"""

import argparse
import json
import pathlib
import sys

from anthropic import Anthropic

SCRIPT_DIR = pathlib.Path(__file__).parent
LOOKUP_SOURCE = (SCRIPT_DIR / "lookup_company.py").read_text()
FETCH_10K_SOURCE = (SCRIPT_DIR / "fetch_10k.py").read_text()
FETCH_20F_SOURCE = (SCRIPT_DIR / "fetch_20f.py").read_text()

SYSTEM_PROMPT = """\
You are a filing fetcher agent. You will receive three Python scripts and a company query.

## Steps

1. Save all three scripts: lookup_company.py, fetch_10k.py, fetch_20f.py
2. Run lookup_company.py with the query to get the ticker, CIK, and filer type.
3. Based on the filer_type in the output:
   - If "domestic": run fetch_10k.py with the ticker
   - If "foreign": run fetch_20f.py with the ticker
4. Merge the lookup result and fetch result into a single JSON object.
5. Write the merged result to result.json
6. Run: cat result.json

## Important
- Run scripts with python3.
- Scripts output JSON to stdout and status to stderr. Capture stdout only.
- At the very end, just run `cat result.json`. Do NOT summarize in markdown.
"""


def run(query: str, years: int) -> dict:
    client = Anthropic()

    print("Creating agent...", file=sys.stderr)
    agent = client.beta.agents.create(
        name="Filing Fetcher",
        model="claude-sonnet-4-6",
        system=SYSTEM_PROMPT,
        tools=[{"type": "agent_toolset_20260401"}],
    )

    print("Creating environment...", file=sys.stderr)
    environment = client.beta.environments.create(
        name="fetcher-env",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )

    print("Creating session...", file=sys.stderr)
    session = client.beta.sessions.create(
        agent=agent.id,
        environment_id=environment.id,
        title=f"Fetch filings: {query}",
    )

    user_message = f"""Fetch the last {years} years of annual filings for: {query}

Save this as lookup_company.py:
```python
{LOOKUP_SOURCE}
```

Save this as fetch_10k.py:
```python
{FETCH_10K_SOURCE}
```

Save this as fetch_20f.py:
```python
{FETCH_20F_SOURCE}
```

Step 1: Run `python3 lookup_company.py "{query}"` and capture the JSON output.
Step 2: Based on the "filing_type" field:
  - If "10-K": run `python3 fetch_10k.py TICKER --count {years}` (use the ticker from step 1)
  - If "20-F": run `python3 fetch_20f.py TICKER --count {years}`
Step 3: Merge both JSON outputs into one object and write to result.json.
Step 4: Run `cat result.json`
"""

    collected_text = []

    print(f"Sending query for '{query}'...", file=sys.stderr)
    try:
        with client.beta.sessions.events.stream(session.id) as stream:
            client.beta.sessions.events.send(
                session.id,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": user_message}],
                }],
            )

            for event in stream:
                if hasattr(event, "content") and event.content:
                    for block in event.content:
                        if hasattr(block, "text") and block.text:
                            collected_text.append(block.text)

                match event.type:
                    case "agent.message":
                        for block in event.content:
                            if hasattr(block, "text"):
                                print(block.text, end="", flush=True, file=sys.stderr)
                    case "agent.tool_use":
                        print(f"\n  [tool: {event.name}]", file=sys.stderr, flush=True)
                    case "session.status_idle":
                        print("\n\nAgent finished.", file=sys.stderr, flush=True)
                        break

    finally:
        try:
            client.beta.agents.archive(agent.id)
        except Exception:
            pass
        try:
            client.beta.environments.delete(environment.id)
        except Exception:
            pass

    full_text = "".join(collected_text)
    return _extract_json(full_text)


def _extract_json(text: str) -> dict:
    """Extract the best JSON result — the one with the most filings."""
    import re

    candidates = []

    # Look for ```json ... ``` blocks
    for block in re.findall(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL):
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "filings" in data:
                candidates.append(data)
        except json.JSONDecodeError:
            continue

    # Look for { ... } blocks with bracket matching
    for match in re.finditer(r'\{', text):
        start = match.start()
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i+1])
                        if isinstance(data, dict) and "filings" in data:
                            candidates.append(data)
                    except json.JSONDecodeError:
                        pass
                    break

    if not candidates:
        raise ValueError("Could not extract JSON result from agent output")

    return max(candidates, key=lambda d: len(d.get("filings", [])))


def main():
    parser = argparse.ArgumentParser(description="Agent 1: Fetch SEC filing URLs")
    parser.add_argument("query", help="Company name or stock ticker")
    parser.add_argument("--years", type=int, default=5, help="Number of years (default: 5)")
    args = parser.parse_args()

    result = run(args.query, args.years)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
