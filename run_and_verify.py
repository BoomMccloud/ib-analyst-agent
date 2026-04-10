"""
Sends fetch_10k.py to a managed agent, has it run the script,
then asks the agent to independently verify the results.
"""

import argparse
import json
import pathlib
import subprocess
import sys

from anthropic import Anthropic

MODEL = "claude-sonnet-4-6"
SCRIPT_PATH = pathlib.Path(__file__).parent / "fetch_10k.py"

SYSTEM_PROMPT = """\
You are a QA verification agent for SEC EDGAR data. You will:
1. Receive a Python script and run it to fetch 10-K filing data.
2. Independently verify the results by querying SEC EDGAR yourself.
3. Report any discrepancies.

Always use `User-Agent: SecFilingsAgent admin@example.com` when making curl requests to SEC.
"""


def run_local(ticker: str, count: int) -> dict:
    """Run fetch_10k.py locally and return parsed JSON."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), ticker, "--count", str(count)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"Local script failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description="Run fetch_10k.py locally and verify via managed agent")
    parser.add_argument("ticker", help="Stock ticker (e.g., AAPL)")
    parser.add_argument("--count", type=int, default=5, help="Number of filings (default: 5)")
    args = parser.parse_args()

    # --- Step 1: Run the script locally ---
    print(f"Running fetch_10k.py locally for {args.ticker}...")
    local_result = run_local(args.ticker, args.count)
    local_json = json.dumps(local_result, indent=2)
    print(f"Local result: {local_result['filing_count']} filings found\n")

    # --- Step 2: Read the script source to send to the agent ---
    script_source = SCRIPT_PATH.read_text()

    # --- Step 3: Set up managed agent ---
    client = Anthropic()

    print("Creating agent...")
    agent = client.beta.agents.create(
        name="SEC Filing Verifier",
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=[{"type": "agent_toolset_20260401"}],
    )

    print("Creating environment...")
    environment = client.beta.environments.create(
        name="sec-verify-env",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )

    print("Creating session...")
    session = client.beta.sessions.create(
        agent=agent.id,
        environment_id=environment.id,
        title=f"Verify 10-K filings: {args.ticker}",
    )

    # --- Step 4: Send the script + verification task ---
    user_message = f"""I have a Python script that fetches 10-K filings from SEC EDGAR.

## Task 1: Run the script
Save the following script as `fetch_10k.py` and run it with: `python3 fetch_10k.py {args.ticker} --count {args.count}`

```python
{script_source}
```

## Task 2: Independently verify
The script produced this output when I ran it locally:

```json
{local_json}
```

Now independently verify these results:
1. Use curl to fetch `https://data.sec.gov/submissions/CIK{local_result['cik']}.json` (with User-Agent header)
2. Parse the JSON to find the {args.count} most recent 10-K filings
3. Compare: do the filing dates, periods of report, and URLs match between the script output and the raw API data?

## Task 3: Report
Output a comparison table showing:
- Each filing from the script vs what you found independently
- Whether each field matches (YES/NO)
- Any discrepancies found

End with a verdict: PASS (all match) or FAIL (discrepancies found).
"""

    print(f"\nSending verification task to agent...")
    print("=" * 60)

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
                match event.type:
                    case "agent.message":
                        for block in event.content:
                            if hasattr(block, "text"):
                                print(block.text, end="", flush=True)
                    case "agent.tool_use":
                        print(f"\n  [tool: {event.name}]", flush=True)
                    case "session.status_idle":
                        print("\n\nDone.", flush=True)
                        break

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    finally:
        print("\nCleaning up...")
        try:
            client.beta.agents.archive(agent.id)
        except Exception:
            pass
        try:
            client.beta.environments.delete(environment.id)
        except Exception:
            pass


if __name__ == "__main__":
    main()
