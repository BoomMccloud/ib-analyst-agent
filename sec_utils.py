"""
SEC EDGAR Fetch Utilities
=========================
Shared rate-limiting and HTTP fetch logic for SEC EDGAR requests.
"""

import os
import sys
import time
import urllib.error
import urllib.request

_contact = os.environ.get("SEC_CONTACT_EMAIL", "boommccloud@gmail.com")
HEADERS = {"User-Agent": f"SecFilingsAgent {_contact}"}
REQUEST_INTERVAL = 0.15  # seconds between requests (SEC rate limit)


def fetch_url(url: str, headers: dict | None = None) -> bytes:
    """Fetch a URL with retry logic and SEC-compliant rate limiting.

    Args:
        url: The URL to fetch.
        headers: Optional custom headers. Defaults to SEC EDGAR headers.

    Returns:
        The response body as bytes.

    Raises:
        urllib.error.HTTPError: On non-retryable HTTP errors.
    """
    hdrs = headers or HEADERS
    for attempt in range(3):
        try:
            time.sleep(REQUEST_INTERVAL)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(10 * (attempt + 1))
            else:
                raise
