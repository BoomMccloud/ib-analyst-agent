"""Unit tests for share-sheet internals (email regex + gws_share params).

Complements the integration coverage in tests/test_share_sheet.py with
focused guards on two atomic behaviours:
- _EMAIL_RE accepts/rejects the right shapes
- gws_share always sets sendNotificationEmail=False (silent share)
"""

import json
import os
import sys
from pathlib import Path

import pytest

SEC_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(SEC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(SEC_AGENT_ROOT))

os.environ.setdefault("SEC_CONTACT_EMAIL", "test@example.com")


def test_email_regex_accepts_valid_addresses():
    """_EMAIL_RE must accept typical valid addresses and reject malformed ones."""
    import web.app as webapp

    rx = webapp._EMAIL_RE

    # Valid forms
    assert rx.match("a@b.co")
    assert rx.match("first.last@sub.example.com")
    assert rx.match("user+tag@example.io")
    assert rx.match("x@y.z")

    # Invalid forms
    assert rx.match("") is None
    assert rx.match("foo") is None  # no @
    assert rx.match("a@b") is None  # no dot in domain
    assert rx.match("a @b.co") is None  # whitespace
    assert rx.match("a@b .co") is None  # whitespace
    assert rx.match("@b.co") is None  # empty local-part
    assert rx.match("a@.co") is None  # empty domain-label-before-dot


def test_gws_share_passes_sendNotificationEmail_false(monkeypatch):
    """gws_share must always pass sendNotificationEmail=False so shares are silent."""
    from sheets import gws as gws_mod
    from sheets.api import gws_share

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(argv, capture_output=True, text=True, timeout=30):
        captured["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr(gws_mod.subprocess, "run", fake_run)

    gws_share("any-sid", "user@example.com")

    argv = captured["argv"]
    p_idx = argv.index("--params")
    params = json.loads(argv[p_idx + 1])
    assert params["sendNotificationEmail"] is False
    assert params["fileId"] == "any-sid"
