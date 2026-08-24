"""Validation gates. A row either passes every gate into the ledger pipeline or goes to
quarantine with a reason — it is never silently dropped."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

Row = Dict[str, object]

TITLE_MIN, TITLE_MAX = 8, 300


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
