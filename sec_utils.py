"""
SEC EDGAR Fetch Utilities
=========================
Shared rate-limiting and HTTP fetch logic for SEC EDGAR requests.
"""

import os
import sys
import time
import json
import urllib.error
import urllib.request
import hashlib
from pathlib import Path

_contact = os.environ.get("SEC_CONTACT_EMAIL", "boom.mccloud@gmail.com")
HEADERS = {"User-Agent": f"SecFilingsAgent {_contact}"}
REQUEST_INTERVAL = 1.0 / 8  # SEC rate limit

_last_request_time = 0.0

def _throttle():
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()

def fetch_url(url: str, headers: dict | None = None, retries: int = 5) -> bytes:
    """Fetch a URL with retry logic, caching, fixture lookup, and SEC-compliant rate limiting.

    Args:
        url: The URL to fetch.
        headers: Optional custom headers. Defaults to SEC EDGAR headers.
        retries: Number of retry attempts.

    Returns:
        The response body as bytes.

    Raises:
        ValueError: If offline mode is on and URL is not cached.
        urllib.error.HTTPError: On non-retryable HTTP errors.
    """
    offline_mode = os.environ.get("SEC_OFFLINE_MODE") == "1"
    record_mode = os.environ.get("SEC_RECORD_FIXTURES") == "1"
    url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()
    
    # 1. Check local cache
    cache_dir = Path(".cache")
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / f"{url_hash}.bin"
    
    # 2. Check offline fixtures
    fixtures_dir = Path("tests/fixtures/sec_filings")
    url_map_file = fixtures_dir / "url_map.json"
    
    def _record_fixture(data_bytes):
        if record_mode:
            fixtures_dir.mkdir(parents=True, exist_ok=True)
            if url_map_file.exists():
                with open(url_map_file, 'r') as f:
                    url_map = json.load(f)
            else:
                url_map = {}
            if url not in url_map:
                url_map[url] = f"{url_hash}.bin"
                with open(url_map_file, 'w') as f:
                    json.dump(url_map, f, indent=2)
            fixture_file = fixtures_dir / f"{url_hash}.bin"
            fixture_file.write_bytes(data_bytes)

    if cache_file.exists() and not record_mode:
        if not offline_mode:
            print(f"CACHE HIT: {url}", file=sys.stderr)
        return cache_file.read_bytes()
        
    if url_map_file.exists():
        with open(url_map_file, 'r') as f:
            url_map = json.load(f)
            if url in url_map:
                fixture_path = fixtures_dir / url_map[url]
                if fixture_path.exists():
                    if not offline_mode:
                        print(f"FIXTURE HIT: {url}", file=sys.stderr)
                    return fixture_path.read_bytes()
                    
    if offline_mode:
        raise ValueError(f"Offline mode enabled, but {url} not found in cache or fixtures.")

    # 3. Fetch from network
    hdrs = headers or HEADERS
    for attempt in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                cache_file.write_bytes(data)
                _record_fixture(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print(f"Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
                
    raise Exception(f"Failed to fetch {url} after {retries} attempts")
