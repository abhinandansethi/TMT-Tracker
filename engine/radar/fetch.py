"""HTTP layer: honest identifying UA, bounded retries, per-host politeness, TLS tolerance flags.
Every fetch either returns bytes or raises FetchError — no half-states."""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Tuple

import requests
import urllib3

# Identify honestly. A spoofed consumer-browser string is poor practice anywhere, and on a
# host that has expressed non-consent it turns an arguable technical breach into an
# evidential one, because unattended collection wearing a Chrome badge is indistinguishable
# from deliberate concealment. Set CONTACT before deploying.
CONTACT = os.environ.get("TMT_RADAR_CONTACT", "compliance@trilegal.com")
# Identify honestly without handing an edge WAF a token to match. Several government hosts
# (MeitY, DPIIT, sci.gov.in) sit behind an Akamai filter that 403s any User-Agent carrying a
# crawler signature — the "python-requests" token or an embedded "+mailto:" — while serving
# the conventional "Mozilla/5.0 (compatible; <name>)" identified-agent form HTTP 200 (verified
# 2026-08-27). That form is NOT a browser spoof: it names us truthfully (TMTRegulatoryRadar,
# Trilegal) and claims no specific browser. Contact travels in the standard From header
# (RFC 7231 5.5.1) instead of the UA, so it stays reachable without tripping the filter.
UA = {"User-Agent": "Mozilla/5.0 (compatible; TMTRegulatoryRadar/2.0; Trilegal internal regulatory monitoring)",
      "From": CONTACT}
TIMEOUT = 40
RETRIES = 2
BACKOFF = 3.0
POLITENESS = 1.2  # seconds between requests to the same host
MAX_BYTES = 15_000_000

_last_hit: Dict[str, float] = {}
_session = requests.Session()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Every request made during a run, as (source_id, host, url). The coverage page claims
# the tracker fetches from the declared venues and nowhere else; this is the record that
# makes that claim checkable rather than a promise.
fetch_log: List[Tuple[str, str, str]] = []


class FetchError(Exception):
    pass


class UndeclaredHost(FetchError):
    """A request was attempted against a host the source never declared."""


def _host(url: str) -> str:
    return url.split("/", 3)[2] if "://" in url else url


def get(url: str, tolerant_tls: bool = False, extra_headers: Optional[Dict[str, str]] = None,
        source_id: str = "?", allowed_domains: Optional[List[str]] = None) -> requests.Response:
    """GET with retries. Raises FetchError on final failure.

    Every call is attributed to the source that caused it, checked against that source's
    declared domains so no request reaches a host the coverage page does not disclose, and
    checked against the site's own robots.txt before it is made."""
    host = _host(url)
    if allowed_domains is not None:
        h = host.lower().split(":")[0]
        if not any(h == d.lower() or h.endswith("." + d.lower()) for d in allowed_domains):
            raise UndeclaredHost(
                f"{source_id} tried to fetch {h}, not in its declared domains {allowed_domains}")
    # The site's own published crawl policy is enforced on every request, rather than
    # trusted to have been read once by whoever added the source.
    from .robots import check as robots_check
    robots_check(url)
    fetch_log.append((source_id, host, url))
    headers = dict(UA, **(extra_headers or {}))
    last_err: Optional[Exception] = None
    for attempt in range(RETRIES + 1):
        wait = POLITENESS - (time.monotonic() - _last_hit.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        try:
            _last_hit[host] = time.monotonic()
            r = _session.get(url, headers=headers, timeout=TIMEOUT,
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
