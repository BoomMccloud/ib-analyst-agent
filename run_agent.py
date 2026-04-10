"""
Runs a Python script inside a Claude Managed Agent container.
Sends the script source, has the agent save and execute it, streams output.

Usage: python run_agent.py fetch_20f.py BABA --count 5
       python run_agent.py fetch_10k.py AAPL --count 5
"""

import argparse
import pathlib
import sys

from anthropic import Anthropic


def main():
    parser = argparse.ArgumentParser(description="Run a Python script via Claude Managed Agent")
    parser.add_argument("script", help="Path to the Python script to run")
    parsed, extra_args = parser.parse_known_args()

    script_path = pathlib.Path(parsed.script)
    if not script_path.exists():
        print(f"Error: {script_path} not found", file=sys.stderr)
        sys.exit(1)

    script_source = script_path.read_text()
    script_args = " ".join(extra_args)

    client = Anthropic()

    print("Creating agent...")
    agent = client.beta.agents.create(
        name="Script Runner",
        model="claude-sonnet-4-6",
        system="You are a script execution agent. Save the given script and run it exactly as instructed. Show the full output.",
        tools=[{"type": "agent_toolset_20260401"}],
    )

    print("Creating environment...")
    environment = client.beta.environments.create(
        name="script-runner-env",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )

    print("Creating session...")
    session = client.beta.sessions.create(
        agent=agent.id,
        environment_id=environment.id,
        title=f"Run {script_path.name} {script_args}",
    )
    print(f"Session: {session.id}\n")

    user_message = f"""Save the following Python script as `{script_path.name}` and run it with:
```
python3 {script_path.name} {script_args}
```

Show the complete stdout output.

```python
{script_source}
```"""

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
        print("\n\nInterrupted.")
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
