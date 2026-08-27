"""Display logic: crisp title + one-line descriptor, computed once and shared.

The dashboard, the export feed and the connector API all need the same short title and the
same "what it pertains to" line. Keeping one implementation here means they can never drift.
Everything is deterministic — no model, at build time or runtime.

A heading has one job: tell a partner at a glance what the item is about. So the bureaucratic
lead-in is stripped ("Notification of…", "In the matter of…"), the subject is pulled to the
front, and for a judgment the parties lead while the case number and matter-type move to the
one-line descriptor beneath.
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
    r"|in (?:the )?matter of:?\s+(?:case against\s+)?"
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

# A statutory tail is only worth cutting if enough subject survives it; otherwise the tail
# WAS the subject (e.g. "services under section 3(7)…") and cutting leaves a bare word.
_TAIL_FLOOR = 14

# Connector phrases that separate an entity from what it did — compacted to an em-dash so the
# subject reads "Flipkart — sale of toys…" instead of trailing off after the party name.
_CONNECTOR = re.compile(
    r"\s+(?:with regard to|with respect to|in respect of|regarding|in the matter of|"
    r"with reference to|concerning)\s+", re.I)

# Pre-rewrites applied before the generic lead-strip, where a literal rewrite reads better.
_PRE = [
    (re.compile(r"^notification to be published in the gazette of india,?.*?\b(?:regarding|for the|of|for)\s+", re.I), ""),
    (re.compile(r"^notification of (?:the )?enforcement of\s+", re.I), "Commencement of "),
    (re.compile(r"^notification of (?:the )?commencement of\s+", re.I), "Commencement of "),
]

# ---- judgments: parties-first heading + a matter-type cue for the descriptor line ----
_VS = re.compile(r"\s+(?:v|vs|versus)\b\.?\s+", re.I)
# A leading case reference: "<TYPE>/<no>/<year> — <parties>" (TDSAT) or "<TYPE> - <no>/<year> - …" (HC).
# The optional leading run lets the case-type itself start the string (e.g. "W.P.(C) - …").
_CASE_HEAD = re.compile(
    r"^\s*(?P<ref>(?:[A-Z][A-Za-z0-9.\)\(&'/ ]*?)?"
    r"(?:PETITION|APPEAL|APPLICATION|SUIT|EXECUTION|CONTEMPT|MISC|REVIEW|"
    r"W\.?P\.?|L\.?P\.?A\.?|C\.?A\.?|O\.?M\.?P\.?|SLP|CRL|R\.?F\.?A\.?|F\.?A\.?O\.?|C\.?S\.?)"
    r"[^—–]*?)\s*[—–]\s*(?P<rest>.+)$", re.I)
# A trailing case reference (Supreme Court lists parties first, then "- SLP(C) No… - Diary Number…").
_CASE_TAIL = re.compile(
    r"\s*[-–—]\s*(?:SLP|C\.?A\.?|W\.?P\.?|O\.?M\.?P\.?|Crl|Diary\s+Number|Appeal|Petition|Application)\b.*$",
    re.I)
# Small words kept lowercase inside a title-cased party name (never the first token).
_LOWER = {"and", "of", "the", "for", "in", "on", "to", "&", "vs"}
_MATTER = re.compile(
    r"(broadcasting petition|telecom(?:munication)? petition|appeal(?:\s+no)?|application|"
    r"petition|suit|execution|contempt|review|writ|special leave)", re.I)
_HC_MATTER = {"LPA": "Letters Patent Appeal", "WP": "Writ petition", "CWP": "Writ petition",
              "CRL": "Criminal matter", "RFA": "Regular First Appeal", "FAO": "First Appeal",
              "RSA": "Second Appeal", "CS": "Civil suit", "OMP": "Arbitration petition",
              "CM": "Civil misc.", "CRP": "Civil revision"}
# Short tokens to keep upper-cased when title-casing an ALL-CAPS party name.
_KEEP_UP = {"TV", "DTH", "OTT", "IPTV", "BSNL", "MTNL", "MSO", "LCO", "HITS", "FM", "DD",
            "SEP", "CCI", "TRAI", "TDSAT", "DOT", "MIB", "GTPL", "RBB", "SITI", "DEN",
            "UFO", "PVR", "NCLAT", "NCLT", "SC", "HC", "USA", "UK", "II", "III", "IV",
            "LPA", "WP", "CWP", "CRL", "RFA", "FAO", "RSA", "CRP", "CS", "OMP", "CM"}
# Delhi/HC hyphen format: "LPA - 500/2025 - <petitioner> - <respondent>" (separate cells).
_HC_HYPHEN = re.compile(r"^\s*(?P<ctype>[A-Z][A-Z.()]{1,10})\s*[-–]\s*\d+\s*/\s*\d{2,4}\s*[-–]\s*(?P<rest>.+)$")


def _titlecase_party(s: str) -> str:
    """Title-case an ALL-CAPS or all-lower party string, leaving known acronyms upper, small
    joining words lower (except first), and mixed-case tokens (already deliberate) untouched."""
    out = []
    for i, w in enumerate(s.split()):
        core = re.sub(r"[^A-Za-z]", "", w)
        if core and core.upper() in _KEEP_UP:
            out.append(w.replace(core, core.upper()))
        elif i > 0 and core and core.lower() in _LOWER:
            out.append(w.replace(core, core.lower()))
        elif core and (w.isupper() or w.islower()):
            out.append(w.replace(core, core.capitalize()))
        else:
            out.append(w)
    return " ".join(out)


def fmt_date(iso: Optional[str]) -> str:
    if not iso or len(str(iso)) < 10:
        return ""
    y, m, d = str(iso)[:10].split("-")
    return f"{int(d)} {_MON[int(m) - 1]} {y}"


def shorten(t: str, cap: int = 66) -> str:
    s = re.sub(r"\s+", " ", t or "").strip().rstrip(".")
    for rx, repl in _PRE:
        s = rx.sub(repl, s, count=1)
    s = _LEAD.sub("", s, count=1).strip()
    s = _CONNECTOR.sub(" — ", s, count=1).strip()
    tail = _TAIL.sub("", s).strip(" ,;:—-")
    if len(tail) >= _TAIL_FLOOR:  # only cut the tail if enough subject survives it
        s = tail
    s = s.strip(" ,;:—-")
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    if not s:
        s = re.sub(r"\s+", " ", (t or "")).strip()
    if len(s) <= cap:
        return s
    return s[:cap].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def judgment_heading(it: Dict[str, Any]) -> str:
    """Parties lead; the case-number/type prefix is dropped (it rides the descriptor line)."""
    t = re.sub(r"\s+", " ", (it.get("title") or "")).strip()
    hc = _HC_HYPHEN.match(t)
    if hc:  # HC hyphen form: strip "LPA - 500/2025 - " and set "v" between the two party cells
        rest = hc.group("rest").strip()
        a, _, b = rest.partition(" - ")
        t = f"{a.strip()} v {b.strip()}" if b else a.strip()
    else:
        m = _CASE_HEAD.match(t)
        if m:
            t = m.group("rest").strip()
        t = _CASE_TAIL.sub("", t).strip(" .,-–—")  # drop a trailing case/diary reference (SC style)
    parts = _VS.split(t, maxsplit=1)
    if len(parts) == 2:
        t = _titlecase_party(parts[0].strip(" .,")) + " v " + _titlecase_party(parts[1].strip(" .,"))
    else:
        t = _titlecase_party(t)
    return shorten(t, cap=74)


def judgment_matter(it: Dict[str, Any]) -> Optional[str]:
    """A short subject cue for a judgment — the matter type from the case reference, or the
    forum's nature — so the line says 'Broadcasting petition · 24 Aug 2026', not just 'Order'."""
    t = re.sub(r"\s+", " ", (it.get("title") or ""))
    hc = _HC_HYPHEN.match(t)
    if hc:
        ct = re.sub(r"[^A-Za-z]", "", hc.group("ctype")).upper()
        return _HC_MATTER.get(ct, "High Court judgment")
    m = _CASE_HEAD.match(t)
    if m:
        kw = _MATTER.search(m.group("ref"))
        if kw:
            s = re.sub(r"\s+no$", "", kw.group(1).strip(), flags=re.I).lower()
            return s[:1].upper() + s[1:]
    reg = (it.get("regulator") or "")
    if reg == "CCI":
        return "Antitrust order"
    if "Supreme Court" in reg:
        return "Supreme Court order"
    if "High Court" in reg or reg.endswith("HC"):
        return "High Court judgment"
    return None


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


def heading(it: Dict[str, Any]) -> str:
    """Lane-aware crisp heading: parties-first for a judgment, subject-first otherwise."""
    if it.get("lane") == "judgments":
        return judgment_heading(it)
    return shorten(it.get("title") or "")


def _openable(url: str) -> Optional[str]:
    """Make a stored URL safe to click. Two fixes, both verified 2026-08-27:
    - DPIIT detail pages resolve on the public www host; the CMS origin
      (cms-dpiit.digifootprint.gov.in) is firewalled and times out.
    - Percent-encode raw spaces so NCCS/TEC filenames that contain spaces don't truncate the
      href at the first space. Already-encoded URLs (with %20) are untouched — only literal
      spaces are replaced, so there is no double-encoding."""
    if not url:
        return None
    url = url.replace("cms-dpiit.digifootprint.gov.in", "www.dpiit.gov.in")
    url = url.replace(" ", "%20")
    return url


def egazette_pdf(gazette_id: str) -> Optional[str]:
    """Direct, session-less URL for a gazette PDF, built from the Gazette ID.

    The e-Gazette search UI serves each PDF only via a session postback, but the file itself
    lives at a stable public path: egazette.gov.in/WriteReadData/<YEAR>/<N>.pdf, where <N> is
    the trailing number of the Gazette ID (CG-DL-E-<DDMMYYYY>-<N>) and <YEAR> is that date's
    year. Verified 2026-08-27: all 28 in-window gazette IDs resolve to 200 application/pdf."""
    parts = (gazette_id or "").split("-")
    if len(parts) < 2:
        return None
    num, datep = parts[-1], parts[-2]
    if not (num.isdigit() and len(datep) == 8 and datep.isdigit()):
        return None
    return "https://egazette.gov.in/WriteReadData/" + datep[4:8] + "/" + num + ".pdf"


def doc_link(it: Dict[str, Any]) -> Optional[str]:
    """Best OPENABLE document link. A gazette entry resolves to its stable WriteReadData PDF
    built from the Gazette ID; only if the ID cannot be parsed does it fall back to the portal
    home (which opens), with the Gazette ID shown as the citation."""
    meta = it.get("meta") or {}
    url = it.get("doc_url") or it.get("url") or ""
    if meta.get("gazette_id"):
        return egazette_pdf(meta["gazette_id"]) or "https://egazette.gov.in/"
    return _openable(url)


def page_link(it: Dict[str, Any]) -> Optional[str]:
    """The official landing page. A gazette entry has no per-item landing (the direct PDF is the
    document and the Gazette ID the citation), so it carries none rather than a redundant portal
    link."""
    meta = it.get("meta") or {}
    if meta.get("gazette_id"):
        return None
    return _openable(it.get("page_url") or "")


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
            if r and not re.match(r"(?i)^nil$", r):
                bits.append("amends " + r)
        if str(meta.get("impact", "")).lower().startswith("action"):
            bits.append("action required")
    elif it.get("lane") == "judgments":
        when = fmt_date(it.get("date"))
        matter = judgment_matter(it)
        if matter:
            bits.append(matter + (" · " + when if when else ""))
        elif meta.get("parties"):
            p = re.sub(r"\s+[—–]\s+", ", ", meta["parties"]).title()
            bits.append(p[:70] + ("…" if len(p) > 70 else ""))
    if not bits:
        when = fmt_date(it.get("date"))
        lab = (it.get("type") or "instrument").replace("_", " ")
        bits.append(f"{lab[:1].upper()}{lab[1:]}{(' · ' + when) if when else ''}")
    return " · ".join(bits)
