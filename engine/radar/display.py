"""Display logic: crisp title + one-line descriptor, computed once and shared.

The dashboard, the export feed and the connector API all need the same short title and the
same "what it pertains to" line. Keeping one implementation here means they can never drift.
Everything is deterministic — no model, at build time or runtime.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Boilerplate lead-ins to strip so the subject leads, and statutory tails to cut.
_LEAD = re.compile(
    r"^(?:"
    r"notice for stakeholder consultation on (?:draft for comments\s*[–-]\s*)?(?:test cases for\s+)?"
    r"|draft generic test cases for "
    r"|(?:pre[\s-]?)?consultation paper on (?:draft amendments? (?:in|to) the\s+)?"
    r"|draft (?:amendments? (?:in|to) the\s+)?"
    r"|direction (?:on|regarding|to) (?:allocation and operationalization of\s+)?"
    r"|recommendations? on (?:issues (?:related to|relating to)\s+)?"
    r"|order (?:regarding|dated|on|in the matter of) "
    r"|publication of (?:revised |new )?"
    r"|notification (?:of the |of |regarding |for the |for )?(?:enforcement of\s+)?"
    r"|notification to be published[^,]*?(?:regarding|of|for) "
    r"|instructions? to be specified[^,]*?accordance with the\s+"
    r"|trai (?:releases?|issues?|initiates?|hosts?|assesses?) (?:an?\s+amended\s+|clarifications? regarding\s+)?"
    r"|nccs (?:designates?|has designated) "
    r"|press release (?:on|regarding) "
    r"|in the matter of "
    r"|clarification (?:regarding|on) "
    r")", re.I)
_TAIL = re.compile(
    r"(?:"
    r",?\s+under (?:sub-?section|section|rule|clause|the provisions)\b.*$"
    r"|,?\s+in accordance with\b.*$"
    r"|,?\s+pursuant to\b.*$"
    r"|,?\s+in pursuance of\b.*$"
    r"|\s+for (?:service and transactional|entities in sectors other)\b.*$"
    r"|\s+by (?:entities|access providers) in\b.*$"
    r")", re.I)


def fmt_date(iso: Optional[str]) -> str:
    if not iso or len(str(iso)) < 10:
        return ""
    y, m, d = str(iso)[:10].split("-")
    return f"{int(d)} {_MON[int(m) - 1]} {y}"


def shorten(t: str, cap: int = 66) -> str:
    s = re.sub(r"\s+", " ", t or "").strip().rstrip(".")
    s = _LEAD.sub("", s, count=1).strip()
    s = _TAIL.sub("", s).strip(" ,;:—-")
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    if not s:
        s = re.sub(r"\s+", " ", (t or "")).strip()
    if len(s) <= cap:
        return s
    return s[:cap].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def short_rule(rule: str) -> str:
    """'Sub-section (2) of section 56 of the Telecommunications Act, 2023 (44 of 2023)'
    -> 's.56, Telecommunications Act 2023'."""
    r = re.sub(r"\s+", " ", rule or "")
    sec = re.search(r"section\s+(\d+[A-Z]?)", r, re.I)
    act = re.search(r"of the ([A-Z][^,]+? Act,? \d{4})", r)
    bits = []
    if sec:
        bits.append("s." + sec.group(1))
    if act:
        bits.append(re.sub(r",? (\d{4})$", r" \1", act.group(1).strip()))
    return ", ".join(bits) or (r[:48] + ("…" if len(r) > 48 else ""))


def descriptor(it: Dict[str, Any], today: str) -> str:
    """One-line 'what it pertains to / what to do', from the item's own metadata."""
    meta = it.get("meta") or {}
    typ = (it.get("type") or "").lower()
    dl = it.get("deadline")
    bits = []
    if dl and str(dl) >= today and ("consult" in typ or "draft" in typ):
        bits.append("Comments due " + fmt_date(dl))
    if meta.get("gazette_id"):
        if meta.get("effective_date") and not bits:
            bits.append("In force " + fmt_date(meta["effective_date"]))
        if meta.get("impacted_rule"):
            r = short_rule(meta["impacted_rule"])
            if r:
                bits.append("amends " + r)
        if str(meta.get("impact", "")).lower().startswith("action"):
            bits.append("action required")
    elif it.get("lane") == "judgments" and meta.get("parties"):
        p = re.sub(r"\s+[—–]\s+", ", ", meta["parties"]).title()
        bits.append(p[:70] + ("…" if len(p) > 70 else ""))
    if not bits:
        when = fmt_date(it.get("date"))
        lab = (it.get("type") or "instrument").replace("_", " ")
        bits.append(f"{lab[:1].upper()}{lab[1:]}{(' · ' + when) if when else ''}")
    return " · ".join(bits)
