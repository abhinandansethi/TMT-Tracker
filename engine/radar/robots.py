"""robots.txt enforcement.

robots.txt is not itself law in India. It matters for two other reasons, and both are
reasons a law firm should honour it strictly. It is the clearest published evidence of
whether a site operator consents to automated collection, which is exactly the question
that decides whether access is "without permission" for s.43 of the Information Technology
Act 2000. And a firm that advises on compliance cannot run a scraper that ignores a
published exclusion.

Fetching and interpretation follow RFC 9309 rather than Python's legacy RobotFileParser,
which gets two cases wrong in ways that matter here:

  * A robots.txt that returns 401 or 403 is treated by RobotFileParser as a blanket
    disallow. RFC 9309 s.2.3.1.3 treats any 4xx as "unavailable", meaning no restrictions.
    mib.gov.in serves 403 on /robots.txt to every client while serving its content pages
    normally, so the legacy reading would have silently dropped an entire ministry.
  * Rules that appear before any "User-agent:" line belong to no group and are not
    enforceable against anyone. ascionline.in carries two dozen such orphan Disallow lines
    above its only real group, which is "User-agent: * / Disallow:" — that is, allow all.

Both were caught because the guard was tested against live sites rather than trusted.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests

from .fetch import UA

TTL = 3600.0
TIMEOUT = 15

# origin -> (rules, note, fetched_at); rules is None when nothing is enforceable
_cache: Dict[str, Tuple[Optional[List[Tuple[bool, str]]], str, float]] = {}


class RobotsDisallowed(Exception):
    """The site's own robots.txt excludes this path for our agent."""


def _agent_token() -> str:
    # Our real identifier now rides inside the conventional "Mozilla/5.0 (compatible; NAME; ...)"
    # form, so a robots group targeting us by name is still honoured rather than shadowed by the
    # vestigial "Mozilla" prefix.
    ua = UA["User-Agent"]
    m = re.search(r"\(compatible;\s*([^;/)\s]+)", ua)
    return m.group(1) if m else re.split(r"[/ ]", ua.lstrip())[0]


def _parse(text: str, agent: str) -> List[Tuple[bool, str]]:
    """Return [(allowed, path_prefix)] for the group matching `agent`, else the '*' group.
    Lines before the first User-agent belong to no group and are ignored, per RFC 9309."""
    groups: Dict[str, List[Tuple[bool, str]]] = {}
    current: List[str] = []
    starting = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not starting:
                current = []
                starting = True
            current.append(value.lower())
            groups.setdefault(value.lower(), [])
        elif field in ("allow", "disallow"):
            if not current:
                continue  # orphan rule: belongs to no group, unenforceable
            starting = False
            for a in current:
                groups.setdefault(a, []).append((field == "allow", value))
    low = agent.lower()
    for key in list(groups):
        if key and key != "*" and key in low:
            return groups[key]
    return groups.get("*", [])


def _load(origin: str) -> Tuple[Optional[List[Tuple[bool, str]]], str]:
    hit = _cache.get(origin)
    if hit and (time.monotonic() - hit[2]) < TTL:
        return hit[0], hit[1]
    rules: Optional[List[Tuple[bool, str]]] = None
    try:
        r = requests.get(origin + "/robots.txt", headers=UA, timeout=TIMEOUT)
        if r.status_code == 200 and "html" not in r.headers.get("content-type", "").lower():
            rules = _parse(r.text, _agent_token())
            note = f"robots.txt 200, {len(rules)} rule(s) for us"
        elif 400 <= r.status_code < 500:
            note = f"robots.txt {r.status_code} — unavailable, no restrictions (RFC 9309)"
        else:
            note = f"robots.txt {r.status_code} — not enforceable, no restrictions"
    except Exception as e:  # noqa: BLE001
        note = f"robots.txt unreachable ({type(e).__name__}) — no restrictions"
    _cache[origin] = (rules, note, time.monotonic())
    return rules, note


def _matches(pattern: str, path: str) -> bool:
    """RFC 9309 path matching: '*' is a wildcard, a trailing '$' anchors the end."""
    if pattern == "":
        return False
    p = unquote(pattern)
    anchored = p.endswith("$")
    if anchored:
        p = p[:-1]
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in p)
    return re.match(rx + ("$" if anchored else ""), unquote(path)) is not None


def check(url: str) -> None:
    """Raise RobotsDisallowed if the site excludes this path. The only interesting
    outcome is the refusal, so there is no return value."""
    parts = urlparse(url)
    if not parts.scheme.startswith("http"):
        return
    rules, _ = _load(f"{parts.scheme}://{parts.netloc}")
    if not rules:
        return
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    # longest matching rule wins; Allow beats Disallow at equal length
    best: Optional[Tuple[int, bool, str]] = None
    for allowed, pattern in rules:
        if _matches(pattern, path):
            key = (len(pattern), allowed)
            if best is None or key > (best[0], best[1]):
                best = (len(pattern), allowed, pattern)
    if best and not best[1]:
        raise RobotsDisallowed(
            f"{parts.netloc}/robots.txt disallows '{best[2]}' covering {path[:70]} — not fetched")


def describe(origin: str) -> str:
    return _load(origin)[1]
