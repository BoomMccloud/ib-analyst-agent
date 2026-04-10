"""
SEC Filings Agent - Uses Claude Managed Agents to fetch annual financial filings from SEC EDGAR.

For US companies: fetches 10-K filings
For foreign private issuers: fetches 20-F filings, or 6-K filings containing "Annual Results"
"""

import argparse
import sys

from anthropic import Anthropic

SYSTEM_PROMPT = """\
You are an expert SEC EDGAR research agent. Your job is to fetch annual financial filings for a given company from the SEC website (https://www.sec.gov/cgi-bin/browse-edgar and https://efts.sec.gov/LATEST/search-index).

## Workflow

1. **Identify the company's CIK number** using the SEC EDGAR company search API:
   - Use: `https://efts.sec.gov/LATEST/search-index?q="{company_name}"&dateRange=custom&startdt=2020-01-01&enddt=2025-12-31&forms=10-K`
   - Or look up the CIK via: `https://www.sec.gov/cgi-bin/browse-edgar?company={name}&CIK=&type=10-K&dateb=&owner=include&count=40&search_text=&action=getcompany`
   - Or use the EDGAR full-text search: `https://efts.sec.gov/LATEST/search-index?q=%22{company}%22&forms=10-K`
   - Also try the company tickers JSON: `https://www.sec.gov/files/company_tickers.json`
   - And the ticker-to-CIK mapping: `curl -s https://www.sec.gov/files/company_tickers.json | python3 -c "import json,sys; data=json.load(sys.stdin); [print(v) for v in data.values() if '{ticker}'.upper() in v.get('ticker','').upper()]"`

2. **Determine if the company is a US domestic filer or a foreign private issuer (FPI).**
   - If the company files 10-K → it's a US domestic filer.
   - If the company files 20-F or 6-K → it's a foreign private issuer.
   - You can check by searching for both filing types on EDGAR.

3. **Fetch the filings:**

   **For US domestic companies (10-K filers):**
   - Use the EDGAR full-text search API: `https://efts.sec.gov/LATEST/search-index?q=%22{company}%22&forms=10-K&dateRange=custom&startdt=2015-01-01&enddt=2025-12-31`
   - Or the submissions API: `https://data.sec.gov/submissions/CIK{cik_padded_to_10_digits}.json`
   - List ALL available 10-K filings with their filing dates and links.

   **For foreign private issuers (6-K / 20-F filers):**
   - First check for 20-F filings (the annual report equivalent for FPIs):
     `https://efts.sec.gov/LATEST/search-index?q=%22{company}%22&forms=20-F`
   - Also search for 6-K filings that contain annual results:
     `https://efts.sec.gov/LATEST/search-index?q=%22{company}%22+%22Annual+Results%22&forms=6-K`
   - Or: `https://efts.sec.gov/LATEST/search-index?q=%22{company}%22+%22Fiscal+Year%22+%22Annual+Results%22&forms=6-K`
   - List ALL available annual filings with their dates and links.

4. **Output a clean summary table** with:
   - Filing type (10-K, 20-F, or 6-K)
   - Filing date
   - Period of report (fiscal year end)
   - Direct link to the filing on SEC EDGAR

## Important notes
- The SEC EDGAR API requires a User-Agent header. Use: `User-Agent: SecFilingsAgent boommccloud@gmail.com`
- Always pad CIK numbers to 10 digits with leading zeros (e.g., CIK 320193 → 0000320193)
- The submissions API endpoint is: `https://data.sec.gov/submissions/CIK{padded_cik}.json`
- EDGAR full-text search: `https://efts.sec.gov/LATEST/search-index?q={query}&forms={form_type}`
- For filing documents, the base URL is: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/`
- Always use `curl` with the proper User-Agent header for all requests.
- Be thorough — fetch ALL available annual filings, not just the most recent one.
- If one API endpoint doesn't work, try alternative endpoints.
- Write a final summary as a markdown table.
"""

MODEL = "claude-sonnet-4-6"
AGENT_NAME = "SEC Filings Research Agent"
ENV_NAME = "sec-filings-env"


def main():
    parser = argparse.ArgumentParser(description="Fetch SEC annual filings for a company")
    parser.add_argument("company", help="Company name or stock ticker (e.g., 'AAPL', 'Toyota', 'BABA')")
    parser.add_argument("--keep", action="store_true", help="Keep the agent and environment after running (don't clean up)")
    args = parser.parse_args()

    client = Anthropic()

    # --- Step 1: Create the agent ---
    print(f"Creating agent...")
    agent = client.beta.agents.create(
        name=AGENT_NAME,
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=[{"type": "agent_toolset_20260401"}],
    )
    print(f"  Agent ID: {agent.id}")

    # --- Step 2: Create the environment ---
    print(f"Creating environment...")
    environment = client.beta.environments.create(
        name=ENV_NAME,
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
        },
    )
    print(f"  Environment ID: {environment.id}")

    # --- Step 3: Create a session ---
    print(f"Creating session...")
    session = client.beta.sessions.create(
        agent=agent.id,
        environment_id=environment.id,
        title=f"SEC filings lookup: {args.company}",
    )
    print(f"  Session ID: {session.id}")

    # --- Step 4: Send message and stream response ---
    user_message = (
        f"Find all annual financial filings on SEC EDGAR for: {args.company}\n\n"
        f"If this is a US domestic company, find all 10-K filings.\n"
        f"If this is a foreign private issuer, find 20-F filings and/or 6-K filings "
        f"that contain 'Annual Results' in the filing description.\n\n"
        f"List every available annual filing with filing date, period of report, "
        f"and a direct link to the filing."
    )

    print(f"\nSending query for: {args.company}")
    print("=" * 60)

    try:
        with client.beta.sessions.events.stream(session.id) as stream:
            client.beta.sessions.events.send(
                session.id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": user_message}],
                    }
                ],
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
        if not args.keep:
            print("\nCleaning up...")
            try:
                client.beta.agents.archive(agent.id)
                print(f"  Archived agent {agent.id}")
            except Exception as e:
                print(f"  Warning: could not delete agent: {e}")
            try:
                client.beta.environments.delete(environment.id)
                print(f"  Deleted environment {environment.id}")
            except Exception as e:
                print(f"  Warning: could not delete environment: {e}")
        else:
            print(f"\nAgent ID: {agent.id}")
            print(f"Environment ID: {environment.id}")
            print(f"Session ID: {session.id}")


if __name__ == "__main__":
    main()
