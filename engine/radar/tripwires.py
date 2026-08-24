"""Tripwires: independent checks that catch the failure modes a naive scraper misses.

  sequence      numbered series (TRAI PR_NoNNofYYYY) — a numbering gap = a missed item
  monotonic     document shelves only grow — a shrink = parse regression or site change
  staleness     a venue that should move every N days but hasn't = source or parser stall
  floor         page 1 must carry at least row_floor items — fewer = broken parse, FAILED
  drift         generic fallback parser sees materially more rows than the adapter = WARN
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple

Row = Dict[str, object]


def check_floor(n_parsed: int, source: dict) -> Optional[str]:
    floor = source["parser"].get("row_floor", 1)
    if n_parsed < floor:
        return f"parsed {n_parsed} rows, floor is {floor} — treating as FAILED parse"
    return None


def check_sequence(rows: List[Row], source: dict, seq_state: dict, today: date,
                   known_seqs: Optional[set] = None) -> Tuple[List[str], dict]:
    """seq_state: {year: max_seen}. A number absent from the page AND from the ledger is
    a genuinely missed instrument; numbers the ledger already holds are just rows that
    scrolled off the visible window."""
    tw = source.get("tripwire")
    if not tw or tw.get("kind") != "sequence":
        return [], seq_state
    year_now = str(today.year)
    known = known_seqs or set()
    nums = sorted({r["extra"]["seq"] for r in rows
                   if isinstance(r.get("extra"), dict) and r["extra"].get("seq")})
    alerts: List[str] = []
    if not nums:
        return alerts, seq_state
    lo, hi = min(nums), max(nums)
    missing = sorted(set(range(lo, hi + 1)) - set(nums) - known)
    if missing and len(missing) <= 10:
        alerts.append(f"sequence gap: number(s) {missing} between {lo} and {hi} "
                      f"absent from both the page and the ledger — likely missed item(s)")
    prev_max = int(seq_state.get(year_now, 0))
    never_seen = sorted(set(range(prev_max + 1, lo)) - known) if prev_max else []
    if never_seen:
        alerts.append(f"sequence jump: last recorded {prev_max}, page now starts at {lo} "
                      f"— number(s) {never_seen} never seen")
    seq_state = dict(seq_state)
    seq_state[year_now] = max(prev_max, hi)
    return alerts, seq_state


def check_monotonic(n_parsed: int, source: dict, last_count: Optional[int]) -> Optional[str]:
    if not source.get("tripwire") or source["tripwire"].get("kind") != "monotonic":
        return None
    if last_count is not None and n_parsed < last_count:
        return f"shelf shrank: {last_count} -> {n_parsed} rows (shelves only grow)"
    return None


def check_staleness(rows: List[Row], source: dict, today: date) -> Optional[str]:
    max_age = source.get("stale_after_days")
    if not max_age:
        return None
    dates = sorted(str(r["date"]) for r in rows if r.get("date"))
    if not dates:
        return None
    newest = date.fromisoformat(dates[-1])
    age = (today - newest).days
    if age > max_age:
        return f"newest item is {age} days old (stale_after_days={max_age}) — venue or parser stalled"
    return None


def check_drift(n_parsed: int, n_generic: int) -> Optional[str]:
    if n_generic > max(n_parsed + 5, n_parsed * 2) and n_generic >= 10:
        return (f"drift: generic fallback sees {n_generic} date+link rows, adapter parsed "
                f"{n_parsed} — markup may have changed under the adapter")
    return None
