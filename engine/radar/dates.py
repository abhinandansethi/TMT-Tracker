"""Deterministic date extraction. Each source declares which formats its listings use;
only those formats are attempted, so a stray number can never be misread as a date."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
MONTHS.update({m.lower()[:3]: i for m, i in
               [(k.capitalize(), v) for k, v in list(MONTHS.items())]})

# format name -> (regex, groups->(y, m, d) builder)
_FMT = {
    "DD/MM/YYYY": (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
                   lambda g: (int(g[2]), int(g[1]), int(g[0]))),
    "DD-MM-YYYY": (re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b"),
                   lambda g: (int(g[2]), int(g[1]), int(g[0]))),
    "DD.MM.YYYY": (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"),
                   lambda g: (int(g[2]), int(g[1]), int(g[0]))),
    "YYYY-MM-DD": (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
                   lambda g: (int(g[0]), int(g[1]), int(g[2]))),
    "DD Month YYYY": (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})\b"),
                      lambda g: (int(g[2]), MONTHS.get(g[1].lower(), 0), int(g[0]))),
    "Month D, YYYY": (re.compile(r"\b([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\b"),
                      lambda g: (int(g[2]), MONTHS.get(g[0].lower(), 0), int(g[1]))),
    "DD-Mon-YYYY": (re.compile(r"\b(\d{1,2})-([A-Za-z]{3,9})-(\d{4})\b"),
                    lambda g: (int(g[2]), MONTHS.get(g[1].lower(), 0), int(g[0]))),
}


def extract_date(text: str, formats: list) -> Optional[str]:
    """First date in `text` matching one of the declared formats, as ISO YYYY-MM-DD.
    Returns None if nothing parses to a real calendar date."""
    for fmt in formats:
        spec = _FMT.get(fmt)
        if spec is None:
            continue
        rx, build = spec
        for m in rx.finditer(text):
            y, mo, d = build(m.groups())
            try:
                if 2000 <= y <= 2100:
                    return date(y, mo, d).isoformat()
            except ValueError:
                continue
    return None


def extract_all_dates(text: str, formats: list) -> list:
    out = []
    for fmt in formats:
        spec = _FMT.get(fmt)
        if spec is None:
            continue
        rx, build = spec
        for m in rx.finditer(text):
            y, mo, d = build(m.groups())
            try:
                if 2000 <= y <= 2100:
                    iso = date(y, mo, d).isoformat()
                    if iso not in out:
                        out.append(iso)
            except ValueError:
                continue
    return out


KNOWN_FORMATS = sorted(_FMT)
