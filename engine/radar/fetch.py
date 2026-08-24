"""HTTP layer: browser UA, bounded retries, per-host politeness, TLS tolerance flags.
Every fetch either returns bytes or raises FetchError — no half-states."""
from __future__ import annotations

import time
from typing import Dict, Optional

import requests
import urllib3

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
TIMEOUT = 40
RETRIES = 2
BACKOFF = 3.0
POLITENESS = 1.2  # seconds between requests to the same host
MAX_BYTES = 15_000_000

_last_hit: Dict[str, float] = {}
_session = requests.Session()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class FetchError(Exception):
    pass


def _host(url: str) -> str:
    return url.split("/", 3)[2] if "://" in url else url


def get(url: str, tolerant_tls: bool = False) -> requests.Response:
    """GET with retries. Raises FetchError on final failure."""
    host = _host(url)
    last_err: Optional[Exception] = None
    for attempt in range(RETRIES + 1):
        wait = POLITENESS - (time.monotonic() - _last_hit.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        try:
            _last_hit[host] = time.monotonic()
            r = _session.get(url, headers=UA, timeout=TIMEOUT,
                             verify=not tolerant_tls, allow_redirects=True)
            if len(r.content) > MAX_BYTES:
                raise FetchError(f"response too large ({len(r.content)}b)")
            r.raise_for_status()
            return r
        except requests.exceptions.SSLError as e:
            # One automatic downgrade for known-flaky gov TLS chains, loudly recorded by caller
            if not tolerant_tls:
                tolerant_tls = True
                last_err = e
                continue
            last_err = e
        except Exception as e:  # noqa: BLE001 — every failure type ends as FetchError
            last_err = e
        if attempt < RETRIES:
            time.sleep(BACKOFF * (attempt + 1))
    raise FetchError(f"{type(last_err).__name__}: {str(last_err)[:200]}")
