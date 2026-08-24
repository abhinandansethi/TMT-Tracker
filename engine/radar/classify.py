"""Deterministic classification: routine-vs-substantive split, instrument type from
per-source rules, deadline extraction. Zero judgment, zero LLM — the same regexes the
audit script asserts against."""
from __future__ import annotations

import re
from typing import Optional


def make_classifier(registry: dict):
    routine_rx = re.compile(registry["classification"]["routine_regex"])
    deadline_rx = re.compile(registry["classification"]["deadline_regex"])
    return routine_rx, deadline_rx


def is_routine(title: str, routine_rx) -> bool:
    return bool(routine_rx.search(title))


def extract_deadline(title: str, deadline_rx, date_formats: list) -> Optional[str]:
    from .dates import extract_date
    m = deadline_rx.search(title)
    if not m:
        return None
    return extract_date(m.group(0), date_formats or
                        ["DD/MM/YYYY", "DD-MM-YYYY", "DD.MM.YYYY", "DD Month YYYY"])


def derive_type(title: str, source: dict, src_type: Optional[str] = None) -> str:
    """What a document IS comes from its own label: first the venue's own type column
    (mapped through the source's type_map), then per-source ordered regex rules on the
    title, then the source default."""
    if src_type:
        for pattern, mapped in source.get("type_map", {}).items():
            if re.search(pattern, src_type, re.I):
                return mapped
    for rule in source.get("type_rules", []):
        if re.search(rule["match"], title):
            return rule["type"]
    return source.get("default_type", "notification")
