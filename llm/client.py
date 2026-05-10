"""
LLM Response Parsing Utilities
===============================
Shared code-fence stripping, truncated JSON recovery, and LLM call wrapper.

Backend is any OpenAI-compatible Chat Completions endpoint, selected via env:
    LLM_BASE_URL  e.g. https://api.groq.com/openai/v1  (default)
    LLM_API_KEY   provider key
    LLM_MODEL     e.g. llama-3.1-70b-versatile         (default)
"""

import json
import os
import re
import sys

from openai import OpenAI


DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.1-70b-versatile"


def get_llm_client() -> OpenAI:
    """Build an OpenAI-compatible client from env."""
    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL)
    api_key = os.environ.get("LLM_API_KEY", "")
    return OpenAI(base_url=base_url, api_key=api_key or "EMPTY")


def get_llm_model() -> str:
    return os.environ.get("LLM_MODEL", DEFAULT_MODEL)


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
        stop_reason: Provider stop reason. Repairs only if "length" (OpenAI)
            or "max_tokens" (Anthropic-style) — both treated equivalently.
    """
    if stop_reason not in ("length", "max_tokens"):
        return text
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")
    text = text.rstrip(", \n")
    text += "]" * max(0, open_brackets)
    text += "}" * max(0, open_braces)
    return text


def parse_json_response(text: str, stop_reason: str | None = None) -> dict:
    """Parse a JSON response from an LLM, handling code fences and truncation."""
    text = strip_code_fences(text)
    text = recover_truncated_json(text, stop_reason)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse JSON from LLM response:\n{text[:500]}")


def call_llm(client: OpenAI, model: str, prompt: str, max_tokens: int = 8192) -> dict:
    """Call the LLM (OpenAI-compatible Chat Completions) and parse JSON. Retries once on parse failure."""
    for attempt in range(2):
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        stop_reason = choice.finish_reason

        try:
            return parse_json_response(text, stop_reason)
        except ValueError:
            if attempt == 0:
                print(f"    JSON parse failed, retrying...", file=sys.stderr)
                continue
            raise
