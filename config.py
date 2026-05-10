"""
Centralized configuration and startup validation.

All environment variables consumed by the pipeline are read here, with named
accessors and defaults. Call ``validate_environment()`` at process startup
(``run_pipeline.py``, ``web/app.py``) to fail fast on missing or invalid values
instead of crashing deep in a pipeline run.
"""

import os
import re
import shutil


# ---------------------------------------------------------------------------
# LLM (OpenAI-compatible Chat Completions)
# ---------------------------------------------------------------------------
LLM_BASE_URL_DEFAULT = "https://api.groq.com/openai/v1"
LLM_MODEL_DEFAULT = "llama-3.1-70b-versatile"


def llm_base_url() -> str:
    return os.environ.get("LLM_BASE_URL", LLM_BASE_URL_DEFAULT)


def llm_api_key() -> str:
    return os.environ.get("LLM_API_KEY", "")


def llm_model() -> str:
    return os.environ.get("LLM_MODEL", LLM_MODEL_DEFAULT)


# ---------------------------------------------------------------------------
# SEC EDGAR
# ---------------------------------------------------------------------------
SEC_RATE_LIMIT_REQS_PER_SEC = 8
SEC_REQUEST_INTERVAL = 1.0 / SEC_RATE_LIMIT_REQS_PER_SEC

# Placeholder addresses we refuse to use as the SEC User-Agent. SEC EDGAR 403s
# obviously-fake UAs, and shipping a default that's a real personal email is
# worse than failing fast.
_PLACEHOLDER_EMAILS = frozenset({
    "you@example.com",
    "demo@example.com",
    "admin@example.com",
    "test@example.com",
    "boom.mccloud@gmail.com",
    "boommccloud@gmail.com",
})
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def sec_contact_email() -> str:
    return os.environ.get("SEC_CONTACT_EMAIL", "").strip()


def sec_user_agent() -> str:
    """SEC EDGAR User-Agent header. Falls back to a marker that EDGAR will 403,
    so a missing email surfaces immediately rather than silently using a stale
    default."""
    email = sec_contact_email() or "unset@example.invalid"
    return f"SecFilingsAgent {email}"


def sec_offline_mode() -> bool:
    return os.environ.get("SEC_OFFLINE_MODE") == "1"


def sec_record_fixtures() -> bool:
    return os.environ.get("SEC_RECORD_FIXTURES") == "1"


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------
class ConfigError(RuntimeError):
    """Raised when required environment is missing or invalid."""


def validate_environment(*, require_gws: bool = True, require_llm: bool = False) -> list[str]:
    """Validate environment for production use.

    Returns a list of non-fatal warnings. Raises ``ConfigError`` (with a single
    aggregated message) if any required value is missing or invalid.

    Offline mode (``SEC_OFFLINE_MODE=1``) skips the email + gws checks so test
    fixtures can run without external setup.
    """
    errors: list[str] = []
    warnings: list[str] = []
    offline = sec_offline_mode()

    if not offline:
        email = sec_contact_email()
        if not email:
            errors.append(
                "SEC_CONTACT_EMAIL is not set. SEC EDGAR requires a real "
                "contact email in the User-Agent (otherwise requests get 403'd). "
                "Add it to .env or export it: SEC_CONTACT_EMAIL=you@example.com"
            )
        elif not _EMAIL_RE.match(email):
            errors.append(f"SEC_CONTACT_EMAIL={email!r} is not a valid email address.")
        elif email.lower() in _PLACEHOLDER_EMAILS:
            errors.append(
                f"SEC_CONTACT_EMAIL={email!r} is a placeholder. Set it to a real address."
            )

    if require_gws and not offline and shutil.which("gws") is None:
        errors.append(
            "gws CLI not found on PATH. Sheet generation requires the gws "
            "(Google Workspace) CLI. Install it and run `gws auth login`."
        )

    if not llm_api_key():
        msg = (
            "LLM_API_KEY is not set. Invariant repair will be unavailable; "
            "the pipeline will still run but cannot self-heal semantic mismatches."
        )
        if require_llm:
            errors.append(msg)
        else:
            warnings.append(msg)

    if errors:
        bullet = "\n  - "
        raise ConfigError("Environment validation failed:" + bullet + bullet.join(errors))
    return warnings
