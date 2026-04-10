"""
LLM Response Parsing Utilities
===============================
Shared code-fence stripping, truncated JSON recovery, and LLM call wrapper.
"""

import json
import re
import sys

from anthropic import Anthropic


def strip_code_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


def recover_truncated_json(text: str, stop_reason: str | None = None) -> str:
    """Attempt to close unmatched braces/brackets in truncated JSON.

    Args:
        text: The JSON string, possibly truncated.
        stop_reason: The LLM stop reason. Only repairs if "max_tokens".

    Returns:
        The repaired JSON string.
    """
    if stop_reason != "max_tokens":
        return text
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")
    text = text.rstrip(", \n")
    text += "]" * max(0, open_brackets)
    text += "}" * max(0, open_braces)
    return text


def parse_json_response(text: str, stop_reason: str | None = None) -> dict:
    """Parse a JSON response from an LLM, handling code fences and truncation.

    Args:
        text: Raw LLM output text.
        stop_reason: The LLM stop reason (e.g., "max_tokens", "end_turn").

    Returns:
        Parsed JSON as a dict.

    Raises:
        ValueError: If JSON cannot be parsed after all recovery attempts.
    """
    text = strip_code_fences(text)
    text = recover_truncated_json(text, stop_reason)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find the outermost JSON object
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse JSON from LLM response:\n{text[:500]}")


def call_llm(client: Anthropic, model: str, prompt: str, max_tokens: int = 8192) -> dict:
    """Call the LLM and parse the JSON response. Retries once on parse failure.

    Args:
        client: Anthropic client instance.
        model: Model ID to use.
        prompt: The prompt to send.
        max_tokens: Maximum tokens in the response.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        ValueError: If JSON cannot be parsed after retry.
    """
    for attempt in range(2):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        try:
            return parse_json_response(text, response.stop_reason)
        except ValueError:
            if attempt == 0:
                print(f"    JSON parse failed, retrying...", file=sys.stderr)
                continue
            raise
