"""Validation gates. A row either passes every gate into the ledger pipeline or goes to
quarantine with a reason — it is never silently dropped."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

Row = Dict[str, object]

TITLE_MIN, TITLE_MAX = 8, 300


def _canon(url: str) -> str:
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.rstrip("/")


def _same_page(url: str, listing_url: str) -> bool:
    """Is this link just the page we scraped it from? Compared whole, query string
    included: on legacy servlet sites the query IS the document's identity
    (s2cMainServlet?VLCODE=CIAD-2026-0042), so stripping it would reject every real row."""
    return _canon(url) == _canon(listing_url)


def _domain_ok(url: str, allowed: List[str]) -> bool:
    if not url:
        return False
    try:
        host = url.split("/", 3)[2].lower()
    except IndexError:
        return False
    return any(host == d or host.endswith("." + d) for d in (x.lower() for x in allowed))


def gate(row: Row, source: dict, today: date) -> Tuple[Optional[Row], Optional[str]]:
    """Returns (clean_row, None) or (None, quarantine_reason)."""
    title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
    if not (TITLE_MIN <= len(title) <= TITLE_MAX):
        return None, f"title length {len(title)} outside [{TITLE_MIN},{TITLE_MAX}]: {title[:80]!r}"

    url = str(row.get("url") or "")
    if not _domain_ok(url, source.get("allowed_domains", [])):
        return None, f"link off allowed domains: {url[:120]!r}"

    # Self-citation gate. A row whose only link is the listing page it was scraped from
    # carries no citation of its own, so it cannot be shown to a partner as an instrument
    # or cited to a client. This is what let a register of penalties enter the ledger
    # typed as orders; the listing page is a place, not a document.
    #
    # Signals-lane sources are exempt by design: a signal is openly a LEAD, not a citation
    # (the e-Gazette homepage panel lists gazettes with no per-item link). They pay for the
    # exemption by having to carry a verifiable identifier instead.
    # Every row must be traceable to something other than the page it was scraped from.
    # Two things qualify: a link of its own, or an official identifier. The identifier is
    # often the better citation — "CG-DL-E-21082026-275657" is permanent and is what a
    # lawyer actually cites, while the Gazette's own URLs are session-scoped and expire.
    extra = row.get("extra") or {}
    ident = extra.get("gazette_id") or extra.get("ref") or extra.get("filing_token")
    if _same_page(url, source["url"]) and not ident:
        kind = "signal" if source.get("lane") == "signals" else "row"
        return None, f"{kind} has no citation of its own, only the listing page: {title[:80]!r}"

    d = row.get("date")
    undated_ok = source["parser"].get("undated_ok", False)
    if d is None and not undated_ok:
        return None, f"no parseable date in row: {title[:80]!r}"
    if d is not None:
        try:
            dt = date.fromisoformat(str(d))
        except ValueError:
            return None, f"unparseable date {d!r}: {title[:80]!r}"
        if dt > today + timedelta(days=45):
            return None, f"date {d} implausibly in the future: {title[:80]!r}"
        if dt.year < 2000:
            return None, f"date {d} implausibly old: {title[:80]!r}"

    clean = dict(row)
    clean["title"] = title
    return clean, None
