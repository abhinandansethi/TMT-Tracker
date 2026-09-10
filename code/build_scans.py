#!/usr/bin/env python3
"""Static builder for the Scans page family: dist/scans.html and dist/scan/<id>.html.

Design: docs/horizon-design.md §2 and §7. Same shape as build_dashboard_v2.py — read the committed
data, build one JSON payload per page, write one HTML file with embedded CSS and vanilla JS. No
model, no network. The tracker's builder is untouched; the two families share the wordmark, the
design tokens and the auth gate, and nothing else, so this surface can be designed cleanly.

dist/scans.html is the product's front door: the list of scans, TMT India first as the built-in
vetted one. Each other card opens dist/scan/<id>.html, which is that scan's whole workspace — the
same seven sections the tracker has, over the scan's own data: Coverage · Instruments · Judgments ·
Signals · Miscellaneous · Clients · Audit, with the digest and its KPI tiles above them as the
scan's masthead. Developments route into the three ledger lanes by `type`, using the one rule in
lane_of() and nothing else, so every development is in exactly one lane and the tab counts cannot
disagree with the tables.

Inputs (all optional except the tracker's own registry, which the built-in card is computed from):
  scans/<id>.json                       the definition, written only by the scan workflow
  data/scans/<id>/developments.json     the ledger
  data/scans/<id>/digest.json           the week's narrative
  data/scans/<id>/health.json           per-source evidence from the last run
  data/scans/<id>/text/<dev>.txt        the text every citation points into
  data/scans/<id>/misc.json             the Miscellaneous lane: an open-web search OUTSIDE the
                                        gated coverage list. Absent on every older scan, and its
                                        absence renders as "nobody looked", never as "nothing found"

TMT_SCAN_ROOT moves scans/ and data/scans/ to another root (run.py honours the same variable), so
a sample or a fixture tree can be built without touching the repository. --out moves dist/.

Two honesty rules carried over from the tracker: a page's freshness is the last RUN's stamp, never
the build's; and every cap, skip or failure the pipeline recorded is rendered, never summarised
away. The builder adds one of its own: a citation whose quote cannot be found in the stored text
is shown as unverified rather than dropped, because a reader deciding whether to trust a sentence
needs to see exactly which sentence lost its evidence.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.scan import common  # noqa: E402  (load_json, atomic writes, IST clock, norm_ws)

# The page's second opinion on a citation must apply the enricher's own rule (typographic quotes
# and dashes folded, NFKC, a punctuation-bare last resort, a 20-character floor). Reviewed defect:
# the builder used a whitespace+case matcher of its own, so a quote the enricher had verified
# rendered as unverified when the text carried a ’ and the quote a ', and a two-word quote the
# enricher had rejected rendered as verified. The import is guarded so the builder still runs
# (with the simpler check) on a checkout where the enricher is absent.
try:
    from pipeline.scan.enrich import _quote_found, _norm, _bare, MIN_QUOTE_CHARS  # noqa: E402
    from pipeline.scan.enrich import TYPES as ENRICH_TYPES  # noqa: E402
except Exception:  # pragma: no cover — enrich.py missing or unimportable
    _quote_found = _norm = _bare = None  # type: ignore[assignment]
    MIN_QUOTE_CHARS = 20
    ENRICH_TYPES = None  # type: ignore[assignment]

# How many documents the FIRST run of a new scan reads. The dialog and the pending card both
# promise this number to the partner, so it must be the number the pipeline actually uses — a
# second literal here would keep promising "20" the day run.py changes the cap, and the page would
# be advertising a speed it no longer delivers. Guarded like the enricher import above so the
# builder still runs on a checkout whose pipeline is older or absent; 20 is the documented default.
try:
    from pipeline.scan.run import FIRST_RUN_MAX_NEW  # noqa: E402
    from pipeline.scan.common import Budget as _Budget  # noqa: E402
    MAX_SOURCES = int(_Budget.CEILINGS["max_sources"])
except Exception:  # pragma: no cover — run.py missing, or older than the first-run cap
    FIRST_RUN_MAX_NEW = 20
    MAX_SOURCES = 12

STATUS_ORDER = ("approved", "pending", "rejected")
LEVELS = ("high", "medium", "low")
# Health vocabulary shared with engine/health.json plus GATED, which only a scan can produce: an
# approved source the run did not fetch because max_sources was already reached. It is listed so
# the page can say "not fetched" rather than let a silent gap read as coverage.
HEALTH_STATUSES = ("OK", "QUIET", "EMPTY", "FAILED", "GATED")
# discover.KINDS -> the chip label Harvey's Sources column uses (Gov, Gazette, ...). "other" and
# an absent kind fall back to the host, which is always true even when nobody classified it.
KIND_LABELS = {"gazette": "Gazette", "regulator": "Regulator", "ministry": "Gov", "court": "Court",
               "parliament": "Parliament", "standards": "Standards"}
# /api/discover is the live source picker's endpoint: it PROPOSES venues and fetches nothing.
# Every candidate the partner ticks still goes through the Python gate (robots.txt, terms,
# extraction floor) when the scan is created — the dialog says so, and so does this comment.
# /api/subject is the subject filter's proposer: it turns {intent, topics} into {ok, regex, why}.
# Like /api/discover it PROPOSES and nothing more — the regex it returns lands in an input the
# partner can edit or clear before anything is created, and the run applies whatever is in that box.
API = {"scans": "/api/scans", "ask": "/api/ask", "draft": "/api/draft", "propose": "/api/propose",
       "discover": "/api/discover", "subject": "/api/subject"}

# ------------------------------------------------------------------------- the subject filter
# WHY THIS EXISTS. The first real scan asked for "new Indian AI regulatory requirements … relevant
# to advising OpenAI on its artificial-intelligence products" and ledgered 118 developments, eight
# of which mention AI at all: 103 TRAI telecom listings and 15 CERT-In vendor CVE bulletins
# ("Multiple Vulnerabilities in Oracle Products"). Every one the enricher managed to read came back
# relevance "low" — it knew, but by then the rows were in the ledger and the first run's whole
# reading budget had gone on them. A scan inherited the machinery for reading a listing and never
# the machinery for deciding what on that listing is the subject.
#
# The instrument is a REGEX, matched against a row's title before anything is read, exactly as
# engine/registry_v2.json gives every TMT India source a `row_filter` and engine/radar/core.py
# applies it. Deterministic, visible on the page, editable by the partner, free per row. The model
# proposes it once at create time; the partner confirms, edits or clears it.
#
# `source` records who decided: "proposed" (the model's, unedited), "partner" (typed or edited
# here), "none" (cleared — read everything). "none", or no regex, or no subject_filter at all, all
# mean the same thing and must behave exactly as the product did before this existed: every row
# every source publishes is ledgered. An existing scan cannot change behaviour by standing still.
SUBJECT_SOURCES = ("proposed", "partner", "none")


def subject_filter_of(defn: dict) -> tuple[dict, list[str]]:
    """The definition's subject_filter, normalised for the page, plus anything wrong with it.

    Returns (filter, problems). `on` is the only thing the page should branch on: a filter is on
    when it has a regex AND its source is not "none". A regex that does not compile is reported
    and shown, never silently treated as absent — the run refused it, and a partner reading
    "no subject filter" while a broken one sits in the definition would be told a lie about why
    the ledger is full."""
    raw = defn.get("subject_filter")
    problems: list[str] = []
    if raw is None:
        return {"on": False, "regex": "", "why": "", "source": "none", "valid": True}, problems
    if not isinstance(raw, dict):
        problems.append(f"subject_filter is not an object: {str(raw)[:80]!r} — treated as no filter, "
                        f"so every row every source publishes is ledgered")
        return {"on": False, "regex": "", "why": "", "source": "none", "valid": True}, problems
    regex = str(raw.get("regex") or "").strip()
    why = str(raw.get("why") or "").strip()
    src = raw.get("source")
    if src not in SUBJECT_SOURCES:
        if src is not None:
            problems.append(f"subject_filter.source is {str(src)[:40]!r}, which is not one of "
                            f"{', '.join(SUBJECT_SOURCES)} — shown as "
                            + ("proposed" if regex else "none"))
        src = "proposed" if regex else "none"
    valid = True
    if regex:
        try:
            re.compile(regex)
        except re.error as e:
            valid = False
            problems.append(f"the subject filter's regex does not compile ({e}) — the run cannot have "
                            f"applied it, so treat this scan's ledger as unfiltered until it is fixed")
    on = bool(regex) and src != "none"
    return {"on": on, "regex": regex, "why": why, "source": src, "valid": valid}, problems


# core.py's own wording, which the scan pipeline reuses, so a count can be read back off the health
# line even from a run that recorded no structured number.
_SUBJ_DROPPED_RE = re.compile(r"(\d+)\s+row\(s\)\s+outside\s+(?:this\s+source's|the)\s+subject\s+filter", re.I)
_SUBJ_TERSE_RE = re.compile(r"(\d+)\s+row\(s\)[^.]*?too\s+terse\s+to\s+judge", re.I)


def _as_count(v: Any) -> Optional[int]:
    """A count the pipeline may have written as a number or as the list it counted."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple)):
        return len(v)
    return None


def subject_counts(h: dict) -> Optional[dict]:
    """What the subject filter did at ONE source on the last run: rows dropped as not the subject,
    and rows kept because their title was too terse to judge.

    Absent is not zero. A run that predates the filter recorded nothing, and a page that renders
    that as "0 dropped" claims the filter looked and found nothing to drop. So this returns None
    when the run said nothing, and the page says "no counts recorded" in that case.

    Several shapes are accepted because the number matters more than the key it arrived under:
    a `subject` object, flat `subject_dropped`/`subject_kept_terse`, core.py's own ingest
    vocabulary (`filtered` / `unsure`), and — last — the health line's own text, which is the one
    thing every run writes because the partner reads it."""
    if not isinstance(h, dict):
        return None
    dropped = kept = None
    titles: list[str] = []
    sub = h.get("subject") if isinstance(h.get("subject"), dict) else h.get("subject_filter")
    if isinstance(sub, dict):
        dropped = _as_count(sub.get("dropped") if sub.get("dropped") is not None else sub.get("filtered"))
        kept = _as_count(sub.get("kept_terse") if sub.get("kept_terse") is not None else sub.get("unsure"))
        titles = [str(t)[:160] for t in (sub.get("titles") or sub.get("kept") or []) if t]
    if dropped is None:
        dropped = _as_count(h.get("subject_dropped"))
    if dropped is None:
        dropped = _as_count(h.get("filtered"))
    if kept is None:
        kept = _as_count(h.get("subject_kept_terse"))
    if kept is None:
        kept = _as_count(h.get("unsure"))
    if not titles:
        titles = [str(t)[:160] for t in (h.get("subject_kept_titles") or []) if t]
    if dropped is None or kept is None:
        for line in list(h.get("info") or []) + list(h.get("notes") or []):
            line = str(line)
            if dropped is None:
                m = _SUBJ_DROPPED_RE.search(line)
                if m:
                    dropped = int(m.group(1))
            if kept is None and "terse" in line.lower() and "kept" in line.lower():
                m = _SUBJ_TERSE_RE.search(line)
                if m:
                    kept = int(m.group(1))
    if dropped is None and kept is None:
        return None
    return {"dropped": dropped, "kept_terse": kept, "titles": titles[:5]}

# ----------------------------------------------------------------------------- lane routing
# ONE rule, shared by the whole product: a development's `type` decides its lane, and a development
# is in exactly one of the three. The tracker's own tabs mean the same three words, so a partner
# who learns Instruments/Judgments/Signals on TMT India reads any scan the same way.
INSTRUMENT_TYPES = ("Legislation", "Rules/Regulations", "Order/Decision", "Notice/Circular", "Guidance/Advisory")
JUDGMENT_TYPES = ("Judgment",)
SIGNAL_TYPES = ("Consultation/Draft", "Press release", "Other")
KNOWN_TYPES = INSTRUMENT_TYPES + JUDGMENT_TYPES + SIGNAL_TYPES
LANES = ("instruments", "judgments", "signals")

# Reviewed defect: the three tuples above were a second, hand-kept copy of enrich.TYPES, so the day
# the enricher gained a type this builder had never heard of, every row carrying it would have been
# routed to Signals AND stamped "untyped" — the page calling the pipeline's own vocabulary a
# mistake. The lanes cannot simply be `= enrich.TYPES` (the grouping is this file's editorial
# judgement, not the enricher's), so the vocabulary is asserted instead: a drift fails the build
# loudly, here, where the fix is one line, rather than quietly on a partner's page. The assert is
# skipped when enrich.py could not be imported at all — there is then nothing to disagree with.
if ENRICH_TYPES is not None and set(ENRICH_TYPES) != set(KNOWN_TYPES):
    _added = sorted(set(ENRICH_TYPES) - set(KNOWN_TYPES))
    _gone = sorted(set(KNOWN_TYPES) - set(ENRICH_TYPES))
    raise SystemExit(
        "build_scans: the lane routing table has drifted from pipeline.scan.enrich.TYPES — "
        + (f"the enricher now writes {_added} which no lane claims; " if _added else "")
        + (f"this file still routes {_gone} which the enricher no longer writes; " if _gone else "")
        + "put each type in INSTRUMENT_TYPES, JUDGMENT_TYPES or SIGNAL_TYPES and rebuild")


def lane_of(dev_type: Optional[str]) -> str:
    """Type -> lane. A row whose type is absent or unrecognised is NOT dropped: it lands in Signals,
    whose own catch-all is "Other", and the page marks it untyped. A development routed into no
    lane would be a development nobody ever reads again — the one outcome a ledger must not have.
    Queued and unread rows are exactly that case: run.py writes them before the enricher has said
    what they are."""
    t = (dev_type or "").strip()
    if t in INSTRUMENT_TYPES:
        return "instruments"
    if t in JUDGMENT_TYPES:
        return "judgments"
    return "signals"


# ----------------------------------------------------------------------------- Miscellaneous
# data/scans/<id>/misc.json, written by the pipeline's open-web pass. Nothing in it was fetched by
# us: it is what a hosted web-search tool returned, which is why it carries no gate evidence, no
# robots verdict and no ledger row. `official_venue` is the valuable case — a venue this scan does
# not cover, which a partner may promote into the coverage list, where the Python gate decides.
MISC_KINDS = ("official_venue", "secondary", "commentary")
MISC_STATUSES = ("new", "promoted", "dismissed")


# ----------------------------------------------------------------------------- roots
def scan_root() -> Path:
    return Path(os.environ["TMT_SCAN_ROOT"]).expanduser() if os.environ.get("TMT_SCAN_ROOT") else ROOT


def definitions_dir(root: Path) -> Path:
    return root / "scans"


def results_dir(root: Path, scan_id: str) -> Path:
    return root / "data" / "scans" / scan_id


# ----------------------------------------------------------------------------- small helpers
def flag(code: str) -> str:
    """ISO-2 -> regional-indicator pair. Anything that is not two letters gets no glyph rather than
    a wrong one; the code itself is always shown beside the flag."""
    c = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", c):
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in c)


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().replace("www.", "", 1) if url else ""
    except Exception:
        return ""


def _actions_url(workflow: str = "scan.yml") -> Optional[str]:
    """Same resolution order as build_dashboard_v2._actions_url (an explicit override, Vercel's
    git metadata, the local remote) — duplicated rather than imported because importing that
    module executes the whole tracker build. Points at scan.yml, the workflow api/scans.js
    dispatches, so the "open the Actions page" link lands on the run the partner is waiting for."""
    if os.environ.get("TMT_ACTIONS_URL"):
        # The tracker's override names sweep.yml; swap the file so one variable serves both pages.
        return re.sub(r"/workflows/[^/]+$", f"/workflows/{workflow}", os.environ["TMT_ACTIONS_URL"])
    owner, slug = os.environ.get("VERCEL_GIT_REPO_OWNER"), os.environ.get("VERCEL_GIT_REPO_SLUG")
    if owner and slug:
        return f"https://github.com/{owner}/{slug}/actions/workflows/{workflow}"
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/]+([^/]+/[^/.]+)", url)
    return f"https://github.com/{m.group(1)}/actions/workflows/{workflow}" if m else None


def quote_in_text(quote: str, text: Optional[str]) -> Optional[bool]:
    """None when there is no text to check against — an unknown, not a verdict. Otherwise the
    enricher's rule, verbatim (see the import above): one rule on both sides, so the page never
    contradicts the pipeline about the same quote. An ellipsis at either end is tolerated by the
    rule's punctuation-bare fallback, not by stripping it here."""
    if text is None:
        return None
    if _quote_found is not None:
        return _quote_found(quote or "", _norm(text), _bare(text))
    # Fallback without the enricher: whitespace+case only, but still the 20-character floor —
    # "the Act" is in every act and proves nothing.
    q = common.norm_ws(quote or "").lower()
    if len(q) < MIN_QUOTE_CHARS:
        return False
    return q in common.norm_ws(text).lower()


# ----------------------------------------------------------------------------- built-in card
def builtin_card() -> dict:
    """The TMT India tracker as the first, vetted scan. Every number is computed from the registry
    and the ledger at build time; a hardcoded '51 sources' would drift the first time a venue is
    retired and read as coverage that no longer exists."""
    reg = common.load_json(ROOT / "engine" / "registry_v2.json", {}) or {}
    live = [s for s in reg.get("sources", []) if s.get("status") == "live"]
    strata = sorted({s.get("stratum") for s in live if s.get("stratum")})
    regulators = sorted({s.get("regulator") for s in live if s.get("regulator")})
    items_doc = common.load_json(ROOT / "data" / "items.json", {}) or {}
    items = items_doc.get("items", []) if isinstance(items_doc, dict) else []
    health = common.load_json(ROOT / "engine" / "health.json", {}) or {}
    generated = health.get("generated") or items_doc.get("generated") or ""
    week_floor = ""
    if generated:
        try:
            week_floor = (datetime.fromisoformat(generated) - timedelta(days=7)).date().isoformat()
        except ValueError:
            week_floor = ""
    ledgered = [i for i in items if i.get("lane") in ("instruments", "judgments")]
    recent = [i for i in ledgered if (i.get("date") or "") >= week_floor] if week_floor else []
    return {
        "id": "tmt-india", "name": "TMT India", "href": "/tmt-radar-v2.html", "builtin": True,
        "tier": "vetted", "demo": False,
        "meta": f"{len(live)} vetted sources · {len(strata)} strata · {len(regulators)} regulators",
        "flags": ["IN"],
        "generated": generated,
        "kpi": [{"n": len(recent), "label": "this week"}, {"n": len(ledgered), "label": "ledgered"}],
        "problems": [] if live else ["engine/registry_v2.json has no live sources"],
    }


# ----------------------------------------------------------------------------- scan loading
def gate_evidence(gate: dict) -> str:
    """One line a partner can read as proof, in the order the gate checked: reachability, robots,
    terms, extraction. Absent evidence is stated as absent — 'terms not checked' is information."""
    if not gate:
        return "no gate evidence recorded"
    bits: list[str] = []
    robots = gate.get("robots")
    if robots == "disallowed":
        # Reviewed defect: a robots refusal was printed as "unreachable (HTTP —)". Nothing was
        # fetched because we declined to, which is a different fact from the site being down.
        bits.append("not fetched — robots.txt disallows our agent")
    elif gate.get("reachable") is False:
        bits.append(f"unreachable (HTTP {gate.get('http') or '—'})")
    if gate.get("final_url"):
        # The gate records a redirect target only when it differs from the URL asked for; a
        # partner should know the page actually read lives somewhere else.
        bits.append(f"redirected to {gate['final_url']}")
    if robots and robots != "disallowed":
        # robots_note is the engine's one-line account of WHY the path was allowed ("no
        # robots.txt", "403 on /robots.txt — treated as unrestricted"); it belongs beside the verdict.
        bits.append(f"robots {robots}" + (f" ({gate['robots_note']})" if gate.get("robots_note") else ""))
    tos = gate.get("tos") or {}
    checked = tos.get("checked") or []
    flags = tos.get("flags") or []
    errors = tos.get("errors") or []
    if checked:
        n = len(checked)
        bits.append(f"terms checked: {n} page{'s' if n != 1 else ''}, "
                    + (f"{len(flags)} flag{'s' if len(flags) != 1 else ''}" if flags else "no flags"))
    else:
        bits.append("terms not checked" + (f", {len(flags)} flag(s)" if flags else ""))
    if errors:
        # A policy page that could not be read is not "no flags"; the gate keeps the error and so
        # does this line, because "terms checked, no flags" would otherwise overstate the check.
        n = len(errors)
        first = errors[0] if isinstance(errors[0], dict) else {"url": "", "error": str(errors[0])}
        bits.append(f"{n} terms page{'s' if n != 1 else ''} unreadable"
                    + (f" ({first.get('error') or '?'}{' at ' + first['url'] if first.get('url') else ''})" if n == 1 else ""))
    # `extract` is null until the parse test actually ran (gate.py assigns it after step 4), so a
    # robots-refused or unreachable source prints no row count at all. Reviewed defect: a
    # pre-filled {rows: 0} made every never-fetched source claim "0 rows parsed (below floor 8)".
    ext = gate.get("extract") if isinstance(gate.get("extract"), dict) else {}
    if ext:
        rows, dated, floor = ext.get("rows"), ext.get("dated"), ext.get("floor")
        s = f"{rows if rows is not None else '?'} rows parsed"
        if dated is not None:
            s += f", {dated} dated"
        if floor is not None and rows is not None and rows < floor:
            s += f" (below floor {floor})"
        bits.append(s)
    return " · ".join(bits) if bits else "no gate evidence recorded"


def flagged_sentences(gate: dict) -> list[str]:
    """ToS flags may be plain strings or {sentence|text|quote, url}; quote what was found either way."""
    out: list[str] = []
    for f in ((gate or {}).get("tos") or {}).get("flags") or []:
        if isinstance(f, str):
            out.append(f)
        elif isinstance(f, dict):
            s = f.get("sentence") or f.get("text") or f.get("quote") or f.get("match") or ""
            if s:
                out.append(s + (f" — {f['url']}" if f.get("url") else ""))
    return out


def health_for(health: dict, src: dict) -> Optional[dict]:
    """health.json keys sources by URL in the contract, but a run may key by host; accept both,
    then fall back to a value that names the URL itself."""
    table = health.get("sources") if isinstance(health.get("sources"), dict) else health
    if not isinstance(table, dict):
        return None
    for key in (src.get("url"), src.get("host"), host_of(src.get("url", "")), src.get("id"), src.get("name")):
        if key and isinstance(table.get(key), dict):
            return table[key]
    for v in table.values():
        if isinstance(v, dict) and v.get("url") and v.get("url") == src.get("url"):
            return v
    return None


def coverage_for(defn: dict, health: dict) -> dict:  # noqa: C901 — one panel, one function
    """Sources by status with their gate evidence and last-run health, plus what the panel needs
    to say about the gaps: jurisdictions with no approved source, the discovery notes the run
    recorded (gaps, drops, a failed discovery), the approved sources the cap left unread."""
    groups: dict[str, list] = {k: [] for k in STATUS_ORDER}
    unknown: list[str] = []
    for src in defn.get("sources") or []:
        if not isinstance(src, dict):
            unknown.append(f"source entry is not an object: {str(src)[:80]!r} (skipped)")
            continue
        status = src.get("status") or "pending"
        if status not in groups:
            unknown.append(f"{src.get('url') or src.get('name') or '?'}: unknown status {status!r} (shown as pending)")
            status = "pending"
        gate = src.get("gate") or {}
        h = health_for(health, src) or {}
        hstatus = str(h.get("status") or "").upper()
        if h and hstatus not in HEALTH_STATUSES:
            unknown.append(f"{src.get('url') or '?'}: unknown health status {h.get('status')!r}")
        groups[status].append({
            "name": src.get("name") or host_of(src.get("url", "")) or src.get("url", ""),
            "url": src.get("url", ""), "host": src.get("host") or host_of(src.get("url", "")),
            "jurisdiction": src.get("jurisdiction") or "", "tier": src.get("tier") or "discovered",
            "kind": src.get("kind") or "", "confidence": src.get("confidence") or "",
            "proposed_by": src.get("proposed_by") or "", "rationale": src.get("rationale") or "",
            "reason": src.get("reason") or "", "evidence": gate_evidence(gate),
            "flags": flagged_sentences(gate), "checked": gate.get("checked") or "",
            "health": {"status": hstatus, "rows_seen": h.get("rows_seen"),
                       "new": h.get("new"), "newest_visible": h.get("newest_visible") or "",
                       "notes": list(h.get("notes") or []), "info": list(h.get("info") or []),
                       # What the subject filter did at THIS source: a partner auditing for a miss
                       # needs the number beside the venue that lost the rows, not only a total.
                       "subject": subject_counts(h),
                       "checked": h.get("checked") or ""} if h else None,
        })
    # Reviewed defect: the header said "4 jurisdictions" while coverage held sources for two and
    # nothing said the other two were searched and came up empty. Computed from the approved
    # list, so it is true whatever discovery reported.
    covered = {s["jurisdiction"].upper() for s in groups["approved"] if s["jurisdiction"]}
    uncovered = [str(j).upper() for j in defn.get("jurisdictions") or [] if str(j).upper() not in covered]
    # A partner-typed source arrives without a jurisdiction, so the line would call every
    # jurisdiction uncovered on a dialog-created scan (review finding). Only claim a gap when every
    # approved source says which jurisdiction it serves.
    if any(not s["jurisdiction"] for s in groups["approved"]):
        uncovered = []
    # Discovery's own account — "discovery gap DE: …", "discovery dropped …", "discovery failed —
    # …" — lives in health.notes; the panel groups those lines so a gap is read next to the list
    # it is a gap in. Reviewed defect: these notes were never read.
    # Grouped on the Coverage panel, all of them. But only the CAVEATS become page problems:
    # "discovery dropped <url>: <reason>" is the deny-list and the dedupe doing their job on every
    # healthy run, and filing that as a problem taught a partner to ignore the problems list.
    discovery = [n for n in (health.get("notes") or []) if isinstance(n, str) and n.lower().startswith("discovery")]
    discovery_caveats = [n for n in discovery if not n.lower().startswith("discovery dropped")]
    gated = sum(1 for s in groups["approved"] if s["health"] and s["health"]["status"] == "GATED")
    # The subject filter, and what it did across every source the last run touched. Totals are
    # summed only over sources that actually recorded a number, and `sources_counted` says how
    # many those were, so "0 dropped over 5 sources" and "nothing recorded" never look alike.
    subject, subject_problems = subject_filter_of(defn)
    unknown.extend(subject_problems)
    counted = [s["health"]["subject"] for grp in STATUS_ORDER for s in groups[grp]
               if s["health"] and s["health"]["subject"]]
    subject["dropped"] = sum(c["dropped"] or 0 for c in counted) if counted else None
    subject["kept_terse"] = sum(c["kept_terse"] or 0 for c in counted) if counted else None
    subject["sources_counted"] = len(counted)
    subject["sources_run"] = sum(1 for grp in STATUS_ORDER for s in groups[grp] if s["health"])
    return {"approved": groups["approved"], "pending": groups["pending"], "rejected": groups["rejected"],
            "uncovered": uncovered, "discovery": discovery, "discovery_caveats": discovery_caveats,
            "gated": gated, "subject": subject, "problems": unknown}


def load_misc(res: Path) -> dict:
    """The Miscellaneous lane's file, or the honest absence of one. An older scan has none and a
    scan whose run predates this lane has none; neither is an error, and neither may be rendered as
    "nothing was found" — "nobody looked" and "we looked and found nothing" are different facts, so
    `present` carries the difference to the page.

    A finding without a URL is dropped: this lane's entire product is a link out to a primary
    source, and a row nobody can open is a claim with nothing behind it. An unknown `kind` or
    `status` is shown in the safest group rather than hidden — commentary and new — with the raw
    value reported, because a value we do not understand must not silently become a Promote button.

    Every tolerance here appends a problem, so the page can show what it had to forgive. Reviewed
    defect: a duplicate finding id was the one exception — dropped in silence — so a file carrying
    two rows under one id lost one of them with nothing on the page to say a lead had gone.
    """
    path = res / "misc.json"
    if not path.exists():
        return {"present": False, "generated": "", "query": {}, "findings": [], "notes": [], "problems": []}
    doc = common.load_json(path, {}) or {}
    if not isinstance(doc, dict):
        raise RuntimeError(f"{path}: misc.json must be an object")
    problems: list[str] = []
    q = doc.get("query") if isinstance(doc.get("query"), dict) else {}
    query = {"intent": str(q.get("intent") or ""),
             "topics": [str(t) for t in (q.get("topics") or []) if t],
             "jurisdictions": [str(j).upper() for j in (q.get("jurisdictions") or []) if j],
             "excluded_hosts": [str(h) for h in (q.get("excluded_hosts") or []) if h]}
    findings: list[dict] = []
    seen: set = set()
    for f in doc.get("findings") or []:
        if not isinstance(f, dict):
            problems.append(f"misc: finding is not an object: {str(f)[:60]!r} (skipped)")
            continue
        url = str(f.get("url") or "").strip()
        if not url:
            problems.append(f"misc: {str(f.get('title') or '?')[:60]!r} has no URL — dropped; this lane is a link out, and a row with nowhere to go proves nothing")
            continue
        kind = f.get("kind") if f.get("kind") in MISC_KINDS else None
        if kind is None:
            problems.append(f"misc: unknown kind {f.get('kind')!r} on {host_of(url) or url} — shown as commentary, not promotable")
            kind = "commentary"
        status = f.get("status") if f.get("status") in MISC_STATUSES else None
        if status is None and f.get("status") not in (None, ""):
            problems.append(f"misc: unknown status {f.get('status')!r} on {host_of(url) or url} — shown as new")
        fid = str(f.get("id") or "") or hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        if fid in seen:
            # Two rows for one finding would read as two leads, so the second is still dropped —
            # but said, like every other tolerance in this function.
            problems.append(f"misc: two findings share the id {fid!r} — the second ({host_of(url) or url}) is dropped")
            continue
        seen.add(fid)
        # `host` is recomputed from the URL, never read from the file, exactly as misc.py does when
        # it writes one ("the model's `host` field is not trusted"). Reviewed defect: trusting the
        # stored value let a row show one host while its link went to another — and the host is the
        # single thing a partner reads to decide whether a lead is worth opening. host_of() folds
        # `www.` off, which is what misc.py's own discover.host_of does, so a file written by the
        # pipeline produces the identical string and nothing is reported.
        stored_host = str(f.get("host") or "").strip().lower()
        host = host_of(url)
        if stored_host and stored_host != host:
            problems.append(f"misc: finding {fid} says host {stored_host!r} but its URL is on {host or '(no host)'} — "
                            f"showing the URL's own host")
        findings.append({
            "id": fid, "title": str(f.get("title") or url), "url": url,
            "host": host,
            "date": str(f.get("date") or ""), "jurisdiction": str(f.get("jurisdiction") or "").upper(),
            "kind": kind, "why": str(f.get("why") or ""), "snippet": str(f.get("snippet") or ""),
            "first_seen": str(f.get("first_seen") or ""), "status": status or "new",
            # Written by misc.mark_stale for a finding the latest search no longer returned. It is
            # kept deliberately (design §2: "a lead does not cease to exist because a search engine
            # changed its mind"), and the page marks the row — a lead nobody can distinguish from a
            # fresh hit is a quietly ageing claim. Absent on every file written before the flag
            # existed, which reads as "not stale", the only safe default. misc.py's own docstring
            # says to READ this flag rather than re-derive it by comparing stamps, because two runs
            # in the same second would make a stale lead look fresh; `last_seen` is carried beside
            # it so the row can say how old the lead is, and never used to compute staleness.
            "stale": f.get("stale") is True,
            "last_seen": str(f.get("last_seen") or ""),
        })
    return {"present": True, "generated": str(doc.get("generated") or ""), "query": query,
            "findings": findings, "notes": [str(n) for n in (doc.get("notes") or []) if n],
            "problems": problems}


def prepare_item(it: dict, text_dir: Path, kinds: Optional[dict] = None) -> dict:
    """Ledger row -> page row. Adds the domain, verifies every citation against the stored text
    when that text is present, and normalises the enums the page filters on. `kinds` maps a
    source URL to its discovered kind (gazette, regulator, ...) so the Sources chip can name the
    venue's kind instead of its host."""
    text: Optional[str] = None
    tf = it.get("text_file")
    if tf:
        p = text_dir.parent / tf if not str(tf).startswith("text/") else text_dir / Path(tf).name
        try:
            text = p.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            text = None
    summary = []
    for s in it.get("summary") or []:
        if not isinstance(s, dict):
            continue
        cite = s.get("cite") or {}
        # The enricher's verdict is paragraph-level (`summary[i].verified`, enrich.verify_citations);
        # reviewed defect: the builder read `cite.verified`, which real ledgers never carry, so the
        # pipeline's verdict was ignored on every real scan. cite.verified is kept as a fallback
        # for hand-written fixtures only.
        explicit = s.get("verified") if isinstance(s.get("verified"), bool) else cite.get("verified")
        checked = quote_in_text(cite.get("quote", ""), text)
        # An explicit flag from the enricher wins; the text check is a second opinion that can only
        # demote (a quote that used to be there and is not any more is worth knowing about).
        verified = (explicit if isinstance(explicit, bool) else True) and (checked is not False)
        summary.append({"text": s.get("text", ""), "quote": cite.get("quote", ""),
                        "where": cite.get("where", ""), "verified": verified,
                        # the enricher's reason for an unverified paragraph ("quote is the title line")
                        "note": str(s.get("note") or "")})
    rel = it.get("relevance") or {}
    level = (rel.get("level") or "").lower()
    # A missing level is an unrated development, not a low one: the page sorts it with "low" so
    # it never crowds the top, but labels it "unrated" so nobody reads an absence as a verdict.
    # A metadata-only record carries level "" by contract, so it lands here too.
    unrated = level not in LEVELS
    if unrated:
        level = "low"
    title = it.get("title") or ""
    source_url = it.get("source_url") or ""
    kind = (kinds or {}).get(source_url) or ""
    # Read state, carried so the page can say WHY a development has no summary. Reviewed defect:
    # queued, read-failed, given-up and enrichment-failed rows all rendered as "No summary was
    # written." run.py always writes `enriched`; a ledger without the key (hand-written) is
    # taken as enriched when it has an enrichment stamp or a summary, since that is what it has.
    enriched = bool(it.get("enriched")) if "enriched" in it else bool(it.get("enriched_at") or it.get("summary"))
    try:
        attempts = int(it.get("read_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    ratio = it.get("verified_ratio")
    return {
        "id": it.get("id", ""), "title": title, "url": it.get("url", ""),
        "source_url": source_url, "domain": host_of(it.get("url", "")) or host_of(source_url),
        "kind": kind if kind in KIND_LABELS else "",
        "tier": it.get("tier") or "discovered", "jurisdiction": (it.get("jurisdiction") or "").upper(),
        "date": it.get("date") or "", "first_seen": it.get("first_seen") or "", "read_as": it.get("read_as") or "",
        "type": it.get("type") or "",
        # The contract's lane rule, applied once here so the table, the counts and the tab bar
        # cannot disagree about where a development belongs. `untyped` is carried separately so a
        # row that landed in Signals only because nothing has typed it yet says so on the page
        # rather than passing as a press release.
        "lane": lane_of(it.get("type")), "untyped": (it.get("type") or "").strip() not in KNOWN_TYPES,
        "topics": list(it.get("topics") or []),
        # The enricher may leave the headline empty on a document it could not summarise; the
        # table's two-line cell then shows the title rather than a blank.
        "headline": it.get("headline") or title, "summary": summary,
        "obligations": [{"who": str(o.get("who") or ""), "what": str(o.get("what") or ""), "when": str(o.get("when") or "")}
                        for o in (it.get("obligations") or []) if isinstance(o, dict)],
        "relevance": {"level": level, "unrated": unrated, "why": rel.get("why") or "", "action": rel.get("action") or "",
                      "clients": rel.get("clients") or {}},
        "confidence": it.get("confidence") or "", "has_text": text is not None,
        "enriched": enriched, "read_attempts": attempts,
        "read_error": str(it.get("read_error") or ""), "enrich_error": str(it.get("enrich_error") or ""),
        "truncated": it.get("truncated") is True, "note": str(it.get("note") or ""),
        "verified_ratio": ratio if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else None,
    }


def load_scan(root: Path, defn_path: Path) -> dict:
    """Definition plus everything its last run left behind. A missing results file is a state
    ('not run yet'), not an error; an unreadable one is an error and stops the build, because a
    page built over corrupt data would look exactly like a page built over good data."""
    defn = common.load_json(defn_path)
    if not isinstance(defn, dict) or not defn.get("id") or not defn.get("name"):
        raise RuntimeError(f"{defn_path}: a scan definition needs at least id and name")
    scan_id = defn["id"]
    if scan_id != defn_path.stem:
        raise RuntimeError(f"{defn_path}: id {scan_id!r} does not match the file name")
    res = results_dir(root, scan_id)
    developments = common.load_json(res / "developments.json", {}) or {}
    digest = common.load_json(res / "digest.json", {}) or {}
    health = common.load_json(res / "health.json", {}) or {}
    misc = load_misc(res)
    kinds = {s.get("url", ""): s.get("kind") or "" for s in defn.get("sources") or [] if isinstance(s, dict)}
    items = [prepare_item(it, res / "text", kinds) for it in developments.get("items", []) if isinstance(it, dict)]
    generated = health.get("generated") or developments.get("generated") or digest.get("generated") or ""
    problems: list[str] = []
    # create_scan writes the definition with partner sources marked "not yet gated" BEFORE it gates
    # them, so a create that died in between leaves a definition with nothing approved. Reviewed
    # defect: the page then said "no run yet — press Run scan", and Run would fetch nothing.
    ungated = [s for s in defn.get("sources") or [] if isinstance(s, dict) and s.get("reason") == "not yet gated"]
    if ungated:
        problems.append(f"create did not finish gating {len(ungated)} source(s) — Edit and save to re-run")
    elif not (res / "developments.json").exists():
        problems.append("no run yet — press Run scan")
    ids = {it["id"] for it in items}
    body = []
    for para in digest.get("body") or []:
        cites = [c for c in (para.get("cites") or []) if c in ids]
        missing = [c for c in (para.get("cites") or []) if c not in ids]
        if missing:
            problems.append(f"digest cites {len(missing)} development id(s) not in the ledger: {', '.join(missing)}")
        # `uncited` is the digest writer's own admission that a sentence rests on no development;
        # it is carried through so the page can mark it as opinion rather than hide it.
        body.append({"text": para.get("text", ""), "cites": cites, "uncited": para.get("uncited") is True or not cites})
    # digest.upcoming, when a run wrote one, is passed through only for ids the ledger has; the
    # page still filters it by the reader's today, because "ahead" is decided at reading time,
    # not at build time. Absent, the page computes the list from obligations[].when itself.
    upcoming = None
    if isinstance(digest.get("upcoming"), list):
        upcoming = [u for u in digest["upcoming"] if isinstance(u, dict) and u.get("dev") in ids and u.get("when")]
    cov = coverage_for(defn, health)
    problems.extend(cov.pop("problems"))
    problems.extend(misc.pop("problems"))
    # run.py writes every cap that dropped work to health.budget.dropped and the run-level notes
    # (given-up documents, discovery gaps, digest verifier notes) to health.notes. Reviewed
    # defect: the builder read a top-level health.dropped that only the old sample produced, so
    # no real run's cap or note ever reached the page. A top-level `dropped` is still accepted.
    drops = [str(d) for d in ((health.get("budget") or {}).get("dropped") or health.get("dropped") or [])]
    for d in drops:
        problems.append(f"budget: {d}")
    for n in (health.get("notes") or []):
        n = str(n)
        if n in drops or n in cov["discovery"] or n in (digest.get("notes") or []):
            continue       # drops are listed above; discovery lines and digest notes are shown where they belong
        problems.append(f"run: {n}")
    if cov.get("discovery_caveats"):
        problems.append(f"discovery recorded {len(cov['discovery_caveats'])} caveat(s) — open Coverage")
    run = health.get("run") if isinstance(health.get("run"), dict) else {}
    counts = digest.get("counts") or {}
    counts = {"new": counts.get("new", len([i for i in items if i["first_seen"] and i["first_seen"] >= generated[:10]]) if generated else counts.get("new", 0)),
              "high": counts.get("high", len([i for i in items if i["relevance"]["level"] == "high"])),
              "sources_ok": counts.get("sources_ok"), "sources_failed": counts.get("sources_failed"),
              "sources_empty": counts.get("sources_empty"),
              # "High relevance 1" beside "New 5" reads as 1 of 5 assessed; when 2 were never read
              # the tile must say so. The pipeline's counts carry assessed/queued when it wrote
              # them; health.run carries the same numbers for this run.
              "assessed": counts.get("assessed", run.get("enriched")),
              "queued": counts.get("queued", run.get("queued"))}
    # Header-level lines for what a collapsed coverage panel would otherwise hide. Reviewed
    # defect: a FAILED source, a queued backlog and failed reads were invisible outside the panel.
    if counts["sources_failed"]:
        problems.append(f"{counts['sources_failed']} source(s) FAILED this run — open Coverage")
    if counts["sources_empty"]:
        problems.append(f"{counts['sources_empty']} source(s) returned no rows this run — open Coverage")
    # No separate line for run.queued: run.py records that cap in budget.dropped, already listed
    # above; the KPI tile's subtitle carries the number.
    if run.get("read_failed"):
        problems.append(f"{run['read_failed']} development(s) could not be read this run")
    if run.get("enrich_failed"):
        problems.append(f"{run['enrich_failed']} development(s) read but not enriched this run (model failure) — retried next run")
    return {
        "definition": {
            "id": scan_id, "name": defn["name"], "intent": defn.get("intent", ""),
            "jurisdictions": [str(j).upper() for j in defn.get("jurisdictions") or []],
            "topics": list(defn.get("topics") or []), "industries": list(defn.get("industries") or []),
            "clients": list(defn.get("clients") or []),
            "sources": [{"url": s.get("url", ""), "status": s.get("status", ""), "proposed_by": s.get("proposed_by", ""),
                         "kind": s.get("kind", ""), "name": s.get("name", ""),
                         "jurisdiction": s.get("jurisdiction", ""), "rationale": s.get("rationale", "")}
                        for s in defn.get("sources") or [] if isinstance(s, dict)],
            # no_discover is stored on the definition by create_scan (contract), so Edit pre-ticks
            # the box as the scan was actually created rather than always "on".
            "no_discover": defn.get("no_discover") is True, "demo": bool(defn.get("demo", False)),
            # Reviewed defect: an Edit rebuilds the definition from THIS payload and submits it, so
            # any key missing here is silently erased. no_misc turned the Miscellaneous lane back
            # on, discovery_notes threw away discovery's account of what it searched and dropped,
            # and budget reset a partner's own caps to the defaults — all on a plain Save.
            "no_misc": defn.get("no_misc") is True,
            # An unattended daily run is the one thing a scan can do without a person; it must be
            # on the page and in the Edit dialog, never a hidden property of the definition.
            "schedule": dict(defn["schedule"]) if isinstance(defn.get("schedule"), dict) else None,
            # The subject filter is EDITED in that dialog rather than merely carried through it,
            # but the round-trip rule is the same: what Edit reads back is what Save re-submits,
            # so a partner who opens the dialog and changes a topic does not silently clear the
            # filter and turn the next run loose on every row every venue publishes.
            "subject_filter": {"regex": cov["subject"]["regex"], "why": cov["subject"]["why"],
                               "source": cov["subject"]["source"]},
            "discovery_notes": [str(n) for n in defn.get("discovery_notes") or []],
            "budget": dict(defn.get("budget") or {}),
            "created": defn.get("created", ""), "updated": defn.get("updated", ""),
        },
        "items": items, "digest": {"week": digest.get("week", ""), "headline": digest.get("headline", ""), "body": body,
                                   "upcoming": upcoming,
                                   # The selection rule and the writer's notes say whether the digest
                                   # is re-narrating old items or was not written at all and why.
                                   "selection": str(digest.get("selection") or ""),
                                   "notes": [str(n) for n in (digest.get("notes") or []) if n]},
        "counts": counts, "run": {k: run.get(k) for k in ("new", "enriched", "queued", "read_failed", "enrich_failed", "ledgered_total")},
        "coverage": cov, "misc": misc, "generated": generated, "problems": problems,
        "results_dir": res,
    }


def load_scans(root: Path) -> list[dict]:
    d = definitions_dir(root)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        if p.name == "schema.json" or p.name.startswith("_"):
            continue
        out.append(load_scan(root, p))
    return out


# ----------------------------------------------------------------------------- payloads
def client_names() -> list[str]:
    doc = common.load_json(ROOT / "pipeline" / "clients.json", {}) or {}
    return [c.get("name") for c in doc.get("clients", []) if c.get("name")]


def stamp_of(*parts: str) -> str:
    """What the page polls for. Built from the data's own stamps, not the build time, so a rebuild
    that changed nothing does not reload every open tab."""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def scan_meta(defn: dict, gated: int = 0) -> str:
    """The header's meta line. An approved source the cap left unread (GATED) is not coverage;
    reviewed defect: the line said "4 sources" when one produced nothing this run."""
    n_src = len([s for s in defn.get("sources") or [] if isinstance(s, dict) and s.get("status") == "approved"])
    parts = [f"{len(defn.get('topics') or [])} topic{'s' if len(defn.get('topics') or []) != 1 else ''}",
             f"{n_src} source{'s' if n_src != 1 else ''}" + (f" ({n_src - gated} read)" if gated else ""),
             f"{len(defn.get('jurisdictions') or [])} jurisdiction{'s' if len(defn.get('jurisdictions') or []) != 1 else ''}"]
    return " · ".join(parts)


def home_payload(scans: list[dict], built: str) -> dict:
    cards = [builtin_card()]
    for s in scans:
        d = s["definition"]
        cards.append({
            "id": d["id"], "name": d["name"], "href": f"/scan/{d['id']}.html", "builtin": False,
            "tier": "discovered", "demo": d["demo"], "meta": scan_meta(d, s["coverage"]["gated"]), "flags": d["jurisdictions"],
            "generated": s["generated"],
            # When the DEFINITION last changed, which a run does not always move. A promotion
            # changes only this: it adds a pending source and commits, without reading anything.
            # The home page's pending card watches it to know a promotion has landed, and the
            # stamp below includes it so an open tab notices the same commit.
            "updated": d.get("updated", ""),
            "kpi": [{"n": s["counts"]["new"], "label": "new"}, {"n": s["counts"]["high"], "label": "high"}],
            "problems": s["problems"],
        })
    return {"page": "home", "builtISO": built, "cards": cards, "clientNames": client_names(),
            "actionsUrl": _actions_url(), "api": API, "firstRunMax": FIRST_RUN_MAX_NEW,
            "maxSources": MAX_SOURCES,
            # Reviewed defect: the stamp was ids + last-run times only, so a promotion — which
            # commits a changed definition and no new developments — left it identical, and a home
            # page waiting for one would have polled for ever without noticing it had landed.
            "stamp": stamp_of(*[f"{c['id']}:{c['generated']}:{c.get('updated', '')}" for c in cards])}


def scan_payload(s: dict, built: str) -> dict:
    d = s["definition"]
    return {"page": "scan", "builtISO": built, "scan": d, "meta": scan_meta(d, s["coverage"]["gated"]), "items": s["items"],
            "digest": s["digest"], "counts": s["counts"], "run": s["run"], "coverage": s["coverage"], "misc": s["misc"],
            "generated": s["generated"],
            "problems": s["problems"], "clientNames": client_names(), "actionsUrl": _actions_url(), "api": API,
            "firstRunMax": FIRST_RUN_MAX_NEW, "maxSources": MAX_SOURCES,
            # misc.json has its own stamp: a Miscellaneous-only run changes nothing else on the
            # page, and an open tab must still notice that the lane refreshed.
            "stamp": stamp_of(d["id"], s["generated"], d.get("updated", ""), s["misc"].get("generated", ""))}


# ----------------------------------------------------------------------------- HTML
TEMPLATE = r"""<meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0C3A55">
<meta name="tmt-stamp" content="__STAMP__">
<link rel="icon" href="data:image/svg+xml;base64,__FAVICON_SVG__" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/site.webmanifest">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&family=Spectral:wght@300;400;500&display=swap">
<style>
:root{
  --navy:#003F62; --navy-d:#002B43; --navy-l:#1B6288; --navy-wash:#EAF0F4;
  --ochre:#AA8918; --ochre-wash:#F7F0DC; --alarm:#8A2B1C; --alarm-wash:#F7E9E6;
  --ok:#1B6B4A; --ok-wash:#E6F1EB;
  --ink:#111315; --mute:#4E555A; --faint:#7C848A; --ghost:#8F9396; --off:#A3A6A8;
  --paper:#FFFFFF; --ground:#E4E8EA; --panel:#F4F6F7; --panel2:#F4F6F7; --row3:#FAFBFB;
  --rule:#C9D1D6; --rule2:#DDE3E7; --rule3:#EBEFF1;
  --serif:Spectral,Georgia,'Times New Roman',serif;
  --sans:'IBM Plex Sans',system-ui,-apple-system,Segoe UI,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --grid:104px 78px minmax(0,1fr) 132px 24px;
  --shadow:0 12px 40px rgba(17,19,21,.12),0 2px 6px rgba(17,19,21,.06);
  --shadow-sm:0 6px 24px rgba(17,19,21,.10),0 1px 3px rgba(17,19,21,.06);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--ground)}
body{font-family:var(--sans);color:var(--ink);font-size:13.5px;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:var(--ink);text-decoration:none}
a:hover{color:var(--navy);text-decoration:underline;text-underline-offset:3px}
:focus-visible{outline:2px solid var(--navy);outline-offset:2px}
input,select,button,textarea{font-family:inherit;font-size:inherit;color:inherit}
input::placeholder,textarea::placeholder{color:var(--ghost)}
.sheet{max-width:1440px;margin:0 auto;background:var(--paper);min-height:100vh}

/* header — identical to the tracker's */
.head{background:var(--navy);color:#fff;padding:26px 64px 22px;display:flex;align-items:baseline;
  justify-content:space-between;gap:24px;flex-wrap:wrap}
.wordmark{font-family:var(--serif);font-weight:400;font-size:29px;letter-spacing:.005em;color:#fff}
.wordmark b{font-weight:600}
.wordmark a,.wordmark a:hover{color:#fff;text-decoration:none}
.updbar{display:flex;align-items:center;gap:16px}
.updated{font-family:var(--mono);font-size:11px;letter-spacing:.05em;color:#B9CEDC}
.updated span{color:#fff;font-weight:500}
.tabs{background:var(--navy-d);padding:0 64px;display:flex;gap:2px;overflow-x:auto}
.tabs a{display:inline-block;background:none;border:0;border-bottom:3px solid transparent;
  padding:13px 18px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;
  color:#93B2C6;text-decoration:none;white-space:nowrap}
.tabs a.on{color:#fff;border-bottom-color:var(--ochre);background:rgba(255,255,255,.06)}
.tabs a:hover{color:#fff;text-decoration:none}
.tabs a.tab-scans{margin-left:auto}

/* page */
.page{padding:36px 64px 110px}
.crumb{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-bottom:14px}
.crumb a{color:var(--faint)}
.crumb a:hover{color:var(--navy)}
.crumb span{margin:0 8px;color:var(--off)}
.titlerow{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;flex-wrap:wrap}
h1.title{font-family:var(--serif);font-weight:400;font-size:34px;line-height:1.15;margin:0;letter-spacing:-.005em;max-width:820px}
.metaline{margin-top:10px;color:var(--mute);font-size:13px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.metaline .dot{color:var(--off)}
.metaline .flags{letter-spacing:.06em;font-size:14px}
.intent{margin:14px 0 0;max-width:760px;color:var(--mute);font-size:13.5px;line-height:1.55}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}

/* buttons */
.btn{appearance:none;cursor:pointer;background:#fff;color:var(--ink);border:1px solid var(--rule);border-radius:6px;
  padding:7px 13px;font-size:12.5px;font-weight:500;line-height:1.3;white-space:nowrap;transition:border-color .12s,background .12s}
.btn:hover{border-color:var(--ink);text-decoration:none}
.btn:disabled{opacity:.5;cursor:progress}
.btn.primary{background:var(--ink);color:#fff;border-color:var(--ink)}
.btn.primary:hover{background:#000}
.btn.quiet{border-color:transparent;color:var(--mute)}
.btn.quiet:hover{border-color:var(--rule);color:var(--ink)}
.btn.on{background:var(--panel);border-color:var(--rule)}
.btn.sm{padding:5px 10px;font-size:12px}
.btn .k{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:6px}
.btn.on .k{color:var(--ink)}

/* notices */
.notice{display:none;margin:18px 0 0;padding:11px 14px;border:1px solid var(--rule2);border-radius:8px;background:var(--navy-wash);color:var(--navy);font-size:13px;line-height:1.5}
.notice.on{display:block}
.notice.warn{background:var(--ochre-wash);color:#5B4507;border-color:#E4D19A}
.notice.bad{background:var(--alarm-wash);color:var(--alarm);border-color:#E7C8C1}
.notice a{color:inherit;text-decoration:underline;text-underline-offset:2px}
.problems{margin:16px 0 0;padding:0;list-style:none;font-family:var(--mono);font-size:11px;color:#5B4507}
.problems li{padding:5px 10px;background:var(--ochre-wash);border-left:2px solid var(--ochre);margin-top:4px}

/* chips */
.chip{display:inline-flex;align-items:center;gap:5px;background:var(--panel);border:1px solid var(--rule3);border-radius:6px;
  padding:2px 8px;font-size:11.5px;line-height:1.5;color:var(--mute);white-space:nowrap;max-width:100%}
.chip.t{color:var(--ink)}
.chip .fl{font-size:13px;line-height:1}
.chip .mark{font-family:var(--mono);font-size:11px}
.chip .mark.v{color:var(--ok)}
.chip .mark.d{color:var(--faint)}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.badge{display:inline-block;font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.09em;padding:2px 8px;border-radius:20px;white-space:nowrap;border:1px solid transparent}
.badge.vetted{background:var(--ok-wash);color:var(--ok);border-color:#B9DCBD}
.badge.discovered{background:var(--panel);color:var(--faint);border-color:var(--rule2)}
.badge.demo{background:var(--ochre-wash);color:#7A5E0E;border-color:#E4D19A}
.badge.unverified{background:var(--ochre-wash);color:#7A5E0E;border-color:#E4D19A;font-size:9px}

/* home cards */
.lede{max-width:640px;color:var(--mute);font-size:13.5px;line-height:1.55;margin:8px 0 0}
.cards{margin-top:30px;display:flex;flex-direction:column;gap:12px}
.card{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;align-items:center;border:1px solid var(--rule2);border-radius:10px;
  padding:20px 24px;background:#fff;color:inherit;transition:border-color .12s,box-shadow .12s}
.card:hover{border-color:var(--rule);box-shadow:var(--shadow-sm);text-decoration:none;color:inherit}
.card .name{font-family:var(--serif);font-size:21px;line-height:1.25;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.card .meta{margin-top:6px;color:var(--mute);font-size:12.5px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.card .meta .flags{font-size:14px;letter-spacing:.04em}
.card .right{text-align:right;min-width:170px}
.card .kpi{font-family:var(--serif);font-size:22px;line-height:1.2}
.card .kpi small{font-family:var(--sans);font-size:12px;color:var(--mute);margin-left:4px}
.card .kpi .sep{color:var(--off);margin:0 8px;font-family:var(--sans);font-size:14px}
.card .last{font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--faint);margin-top:4px}
.card.builtin{background:linear-gradient(0deg,#fff,#fff) padding-box}
.empty{margin-top:36px;border:1px dashed var(--rule);border-radius:10px;padding:34px 36px;max-width:720px}
.empty h2{font-family:var(--serif);font-weight:400;font-size:24px;margin:0 0 8px}
.empty p{margin:0 0 6px;color:var(--mute);line-height:1.6}
.empty .btn{margin-top:14px}

/* digest */
.digest{margin-top:30px;border:1px solid var(--rule2);border-radius:12px;padding:28px 32px;display:grid;grid-template-columns:minmax(0,1fr) 230px;gap:32px;background:#fff}
.digest .label{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
.digest h2{font-family:var(--serif);font-weight:400;font-size:25px;line-height:1.3;margin:10px 0 14px;letter-spacing:-.003em}
.digest p{margin:0 0 8px;font-size:14px;line-height:1.65;max-width:720px}
.digest .none{color:var(--faint);font-style:italic}
.kpis{display:flex;flex-direction:column;gap:12px}
.kpi-tile{border:1px solid var(--rule2);border-radius:10px;padding:16px 18px;background:var(--row3)}
.kpi-tile .n{font-family:var(--serif);font-size:40px;line-height:1;font-weight:300}
.kpi-tile .l{margin-top:8px;font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint)}
.cite{appearance:none;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;padding:0 4px;margin-left:4px;
  border:1px solid var(--rule);border-radius:4px;background:#fff;font-family:var(--mono);font-size:10px;line-height:1;color:var(--mute);vertical-align:2px}
.cite:hover,.cite:focus-visible{border-color:var(--ink);color:var(--ink);background:var(--panel)}
.cite.x{border-style:dashed;color:#7A5E0E}

/* table */
.toolbar{margin-top:34px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;border-bottom:1px solid var(--rule2);padding-bottom:12px}
.ttabs{display:flex;gap:2px}
.ttabs button{appearance:none;cursor:pointer;background:none;border:0;border-radius:6px;padding:6px 12px;font-size:12.5px;color:var(--mute)}
.ttabs button .k{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:6px}
.ttabs button.on{background:var(--panel);color:var(--ink)}
.ttabs button.on .k{color:var(--ink)}
.ttabs button:hover{color:var(--ink)}
.tools{display:flex;gap:8px;align-items:center}
.tools input[type=search]{border:1px solid var(--rule);border-radius:6px;padding:6px 10px;width:240px;font-size:12.5px;background:#fff}
.tools input[type=search]:focus{border-color:var(--ink);outline:none}
.tools select{border:1px solid var(--rule);border-radius:6px;padding:6px 8px;font-size:12.5px;background:#fff}
table.dev{width:100%;border-collapse:collapse;table-layout:fixed;margin-top:4px}
table.dev th{text-align:left;font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);font-weight:500;padding:12px 10px 10px;border-bottom:1px solid var(--rule2)}
table.dev th:first-child,table.dev td:first-child{padding-left:4px}
table.dev td{padding:14px 10px;border-bottom:1px solid var(--rule3);vertical-align:top;font-size:12.5px}
table.dev tr.r{cursor:pointer}
table.dev tr.r:hover td{background:var(--row3)}
table.dev tr.r:focus-visible{outline:2px solid var(--navy);outline-offset:-2px}
table.dev tr.r.read .t{font-weight:500}
table.dev .t{font-weight:600;font-size:13.5px;line-height:1.4;color:var(--ink);display:flex;gap:8px;align-items:flex-start}
table.dev .t .un{flex:none;width:6px;height:6px;border-radius:50%;background:var(--navy);margin-top:7px}
table.dev tr.read .t .un{visibility:hidden}
table.dev .h{color:var(--mute);margin-top:3px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
table.dev .w{margin-top:5px;font-family:var(--mono);font-size:10.5px;color:var(--faint);letter-spacing:.02em}
table.dev .w b{font-weight:500;color:var(--faint)}
table.dev .star{color:var(--ochre);margin-left:6px;font-size:11px}
table.dev .chips{gap:3px}
.rel{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.bars{display:inline-flex;align-items:flex-end;gap:2px;height:12px}
.bars i{display:block;width:4px;background:var(--rule);border-radius:1px}
.bars i:nth-child(1){height:5px}.bars i:nth-child(2){height:8px}.bars i:nth-child(3){height:12px}
.rel.high .bars i{background:var(--ink)}
.rel.medium .bars i:nth-child(-n+2){background:var(--ink)}
.rel.low .bars i:nth-child(1){background:var(--ink)}
.rel .lv{text-transform:capitalize}
.nothing{padding:40px 0;color:var(--faint);text-align:center;font-style:italic}
.tablewrap{overflow-x:auto}
@media (max-width:1100px){table.dev{min-width:960px}}

/* coverage panel — a tab now, not a drawer: it shows whenever its view does, so no .on gate and
   no card chrome of its own (the tab is the frame). */
.coverage{margin-top:22px}
.coverage h3{font-family:var(--serif);font-weight:400;font-size:19px;margin:0 0 4px}
.coverage .sub{color:var(--mute);font-size:12.5px;margin:0 0 18px}
.coverage .grp{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin:20px 0 6px;padding-bottom:6px;border-bottom:1px solid var(--rule3)}
.src{display:grid;grid-template-columns:14px minmax(0,1fr);gap:12px;padding:12px 0;border-bottom:1px solid var(--rule3)}
.src:last-child{border-bottom:0}
.src .dotc{margin-top:6px;width:8px;height:8px;border-radius:50%;background:var(--off)}
.src .dotc.OK{background:var(--ok)}.src .dotc.QUIET{background:var(--ochre)}.src .dotc.WARN{background:var(--ochre)}.src .dotc.EMPTY{background:var(--off)}.src .dotc.FAILED{background:var(--alarm)}.src .dotc.GATED{background:var(--alarm)}
.src .n{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.src .n .host{font-family:var(--mono);font-size:11px;color:var(--faint)}
.src .n .st{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--mute)}
.src .ev{margin-top:4px;font-family:var(--mono);font-size:11px;color:var(--mute);line-height:1.6}
.src .ev .sep{color:var(--off);margin:0 6px}
.src .why{margin-top:3px;color:var(--mute);font-size:12.5px}
.src .quote{margin:6px 0 0;padding:8px 12px;border-left:2px solid var(--ochre);background:var(--ochre-wash);color:#5B4507;font-size:12.5px;font-style:italic;line-height:1.5}
.src .note{margin-top:4px;font-size:12px;color:#5B4507}
.src .reason{margin-top:3px;color:var(--alarm);font-size:12.5px}
.src .infos{margin:4px 0 0;padding-left:16px;color:var(--faint);font-size:11.5px}
.src .infos li.warn{color:#5B4507}
.coverage .foot{margin-top:20px;padding-top:14px;border-top:1px solid var(--rule3);font-family:var(--mono);font-size:11px;letter-spacing:.03em;color:var(--mute)}

/* slide-over */
.scrim{position:fixed;inset:0;background:rgba(17,19,21,.18);opacity:0;pointer-events:none;transition:opacity .18s;z-index:40}
.scrim.on{opacity:1;pointer-events:auto}
.panel{position:fixed;top:0;right:0;bottom:0;width:600px;max-width:100vw;background:#fff;box-shadow:var(--shadow);transform:translateX(102%);transition:transform .22s ease;z-index:50;overflow-y:auto;display:flex;flex-direction:column}
.panel.on{transform:none}
.panel .ph{padding:22px 32px 0;display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
.panel .pclose{appearance:none;cursor:pointer;border:1px solid transparent;background:none;border-radius:6px;width:30px;height:30px;font-size:18px;line-height:1;color:var(--mute);flex:none}
.panel .pclose:hover{border-color:var(--rule);color:var(--ink)}
.panel .pb{padding:0 32px 40px}
.panel h2{font-family:var(--serif);font-weight:400;font-size:23px;line-height:1.3;margin:14px 0 8px;letter-spacing:-.003em}
.panel .sub{font-family:var(--mono);font-size:11px;color:var(--faint);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.panel .sub .cite{margin-left:0}
.kv{margin-top:18px;border-top:1px solid var(--rule3)}
.kv .row{display:grid;grid-template-columns:120px minmax(0,1fr);gap:16px;padding:9px 0;border-bottom:1px solid var(--rule3);font-size:12.5px}
.kv .row .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);padding-top:3px}
.panel h4{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);font-weight:500;margin:26px 0 8px}
.panel p.s{margin:0 0 10px;font-size:13.5px;line-height:1.65}
.panel p.s.unv{color:var(--mute)}
.obl{width:100%;border-collapse:collapse;font-size:12.5px}
.obl th{text-align:left;font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);font-weight:500;padding:6px 10px 6px 0;border-bottom:1px solid var(--rule2)}
.obl td{padding:8px 10px 8px 0;border-bottom:1px solid var(--rule3);vertical-align:top;line-height:1.45}
.relbox{border:1px solid var(--rule2);border-radius:10px;padding:14px 16px;background:var(--row3)}
.relbox .lvl{display:flex;align-items:center;gap:8px;font-weight:600;font-size:13px}
.relbox .why{margin:8px 0 0;font-size:13px;line-height:1.55;color:var(--mute)}
.relbox .act{margin:10px 0 0;padding:9px 12px;border-left:2px solid var(--ochre);background:var(--ochre-wash);color:#3d2f05;font-size:13px;line-height:1.55;border-radius:0 6px 6px 0}
.relbox .cl{margin-top:8px;display:flex;gap:4px;flex-wrap:wrap}
.pactions{display:flex;gap:8px;flex-wrap:wrap;margin-top:26px;padding-top:18px;border-top:1px solid var(--rule3)}
.ask{margin-top:12px;display:none}
.ask.on{display:block}
.ask .in{display:flex;gap:8px}
.ask input{flex:1;border:1px solid var(--rule);border-radius:6px;padding:8px 10px;font-size:13px}
.ask input:focus{border-color:var(--ink);outline:none}
.ask .out{margin-top:12px;font-size:13.5px;line-height:1.6}
.ask .out .ng{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:#7A5E0E;background:var(--ochre-wash);border:1px solid #E4D19A;border-radius:20px;padding:2px 8px;display:inline-block;margin-bottom:8px}
.ask .out blockquote{margin:8px 0 0;padding:8px 12px;border-left:2px solid var(--rule);color:var(--mute);font-size:12.5px;line-height:1.5;font-style:italic}
.ask .out .nt{margin-top:8px;font-family:var(--mono);font-size:10.5px;color:var(--faint)}
.busy{color:var(--faint);font-style:italic}

/* hover card */
.hc{position:absolute;z-index:70;width:340px;max-width:calc(100vw - 24px);background:#fff;border:1px solid var(--rule2);border-radius:10px;box-shadow:var(--shadow);padding:14px 16px;font-size:12.5px;line-height:1.5;display:none}
.hc.on{display:block}
.hc .hh{font-weight:600;font-size:13px;line-height:1.4}
.hc .hm{margin-top:4px;font-family:var(--mono);font-size:10.5px;color:var(--faint)}
.hc .hq{margin:8px 0 0;padding:8px 12px;border-left:2px solid var(--rule);color:var(--mute);font-style:italic;font-size:12.5px;line-height:1.5;max-height:180px;overflow:auto}
.hc .hw{margin-top:6px;font-family:var(--mono);font-size:10.5px;color:var(--faint)}
.hc .btn{margin-top:10px}

/* dialogs */
dialog{border:0;border-radius:12px;padding:0;box-shadow:var(--shadow);width:640px;max-width:calc(100vw - 32px);max-height:calc(100vh - 48px);color:var(--ink);font-family:var(--sans)}
dialog::backdrop{background:rgba(17,19,21,.28)}
dialog .dh{padding:24px 30px 0;display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
dialog h2{font-family:var(--serif);font-weight:400;font-size:24px;margin:0;line-height:1.2}
dialog .db{padding:6px 30px 26px}
dialog .df{padding:16px 30px 22px;border-top:1px solid var(--rule3);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
dialog .df .err{color:var(--alarm);font-size:12.5px}
.field{margin-top:16px}
/* Direct children only: the source picker's candidate rows are labels too, and they are prose,
   not field captions (defect: every venue name and rationale rendered in shouting mono grey). */
.field>label{display:block;font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin-bottom:6px}
.field>label .req{color:var(--alarm);margin-left:2px}
.field .help{margin-top:5px;font-size:12px;color:var(--faint);line-height:1.45}
.field input[type=text],.field textarea{width:100%;border:1px solid var(--rule);border-radius:6px;padding:8px 10px;font-size:13px;background:#fff;line-height:1.45}
.field textarea{min-height:88px;resize:vertical}
.field input[type=text]:focus,.field textarea:focus{border-color:var(--ink);outline:none}
.field.check{display:flex;align-items:center;gap:8px}
.field.check>label{margin:0;font-family:var(--sans);text-transform:none;letter-spacing:0;font-size:13px;color:var(--ink)}
.cin{display:flex;flex-wrap:wrap;gap:4px;border:1px solid var(--rule);border-radius:6px;padding:4px 6px;min-height:36px;background:#fff;cursor:text}
.cin:focus-within{border-color:var(--ink)}
.cin .chip{padding:2px 4px 2px 8px}
.cin .chip button{appearance:none;border:0;background:none;cursor:pointer;color:var(--faint);font-size:13px;line-height:1;padding:0 3px;border-radius:3px}
.cin .chip button:hover{color:var(--alarm)}
.cin input{flex:1;min-width:140px;border:0;outline:none;padding:4px;font-size:13px;background:transparent}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:640px){.two{grid-template-columns:1fr}}
.modal pre{white-space:pre-wrap;font-family:var(--sans);font-size:13px;line-height:1.6;margin:12px 0 0;padding:14px 16px;border:1px solid var(--rule3);border-radius:8px;background:var(--row3);max-height:50vh;overflow:auto}
.modal .fb{margin-top:10px;font-family:var(--mono);font-size:10.5px;color:#7A5E0E;line-height:1.6}

/* create dialog, step one: describe it */
dialog[data-step=describe] .only-form{display:none}
dialog[data-step=form] .only-describe{display:none}
.describe .field textarea{min-height:120px}
.describe .paths{display:flex;gap:8px;align-items:center;margin-top:16px;flex-wrap:wrap}
.describe .paths .or{color:var(--faint);font-size:12.5px;margin:0 4px}
.pnote{display:none;margin-top:14px;padding:10px 12px;border-radius:8px;background:var(--navy-wash);color:var(--navy);font-size:12.5px;line-height:1.5}
.pnote.on{display:block}
.pnote.warn{background:var(--ochre-wash);color:#5B4507}

/* create dialog: the source picker. Candidates are proposals — the tick chooses what the gate
   will be asked about, never what gets fetched, and the standing line under the list says so. */
.find .findrow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.find .findwhy{font-size:12px;color:var(--faint);line-height:1.45;flex:1;min-width:180px}
.findcount{margin-left:auto;font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}
.findcount.over{color:var(--alarm);font-weight:600}
#dlg-find:disabled{cursor:not-allowed}
.cands{margin-top:10px}
.cands:empty{display:none}
.candlist{list-style:none;margin:0;padding:0;border:1px solid var(--rule3);border-radius:8px;max-height:280px;overflow:auto;background:#fff}
.candlist li+li{border-top:1px solid var(--rule3)}
.cand{display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px;padding:9px 12px;cursor:pointer;align-items:start;
  font-family:var(--sans);font-size:13px;letter-spacing:0;text-transform:none;color:var(--ink);margin:0}
.cand:hover{background:var(--row3)}
.cand>input{margin-top:4px}
.cand .ctop{display:flex;flex-wrap:wrap;gap:7px;align-items:baseline;font-size:13px;line-height:1.35}
.cand .nm{font-weight:500}
.cand .kind{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--mute);border:1px solid var(--rule3);border-radius:20px;padding:1px 7px}
.cand .jur{font-size:12px;color:var(--mute);white-space:nowrap}
.cand .conf{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
.cand .conf.c-high{color:var(--ok)}
.cand .conf.c-low{color:#7A5E0E}
.cand .chost{display:block;font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-top:3px}
.cand .crat{display:block;margin-top:3px;font-size:12px;color:var(--mute);line-height:1.45}
.gapline,.dropline{margin-top:6px;font-size:12.5px;line-height:1.5}
.gapline{color:#5B4507}
.dropline{color:var(--mute)}
.gapline .t,.dropline .t{font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin-right:7px}
.firstrun{margin-top:20px;padding:10px 12px;border:1px solid var(--rule3);border-radius:8px;background:var(--row3);font-size:12.5px;color:var(--mute);line-height:1.55}

/* home: scans dispatched but not yet built — dashed, because nothing is committed yet */
.pending{margin-top:14px;display:flex;flex-direction:column;gap:12px}
.pending:empty{display:none}
.pcard{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;align-items:center;border:1px dashed var(--rule);border-radius:10px;padding:18px 22px;background:var(--row3)}
.pcard .name{font-family:var(--serif);font-size:21px;line-height:1.25;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pcard.failed{border-style:solid;border-color:#E7C8C1;background:var(--alarm-wash)}
.pcard .pstate{margin:6px 0 0;color:var(--ink);font-size:13px;line-height:1.5}
/* The estimate is set apart from the observed line above it, so a reader can see at a glance
   which sentence is a fact about the run and which is the clock talking. */
.pcard .pest{margin:4px 0 0;color:var(--mute);font-size:12.5px;line-height:1.5;font-style:italic}
.pcard .right .btn.sm{margin-top:6px}
a.el.quiet{color:var(--faint);text-decoration:underline;text-underline-offset:2px;font-size:10px}
a.el.quiet:hover{color:var(--navy)}
.linkbtn{appearance:none;border:0;background:none;padding:0;font:inherit;color:inherit;
  text-decoration:underline;text-underline-offset:2px;cursor:pointer}
.pcard .fine{margin-top:5px;font-size:12px;color:var(--faint);line-height:1.5;max-width:640px}
.pcard .right{display:flex;flex-direction:column;align-items:flex-end;gap:3px;min-width:150px}
.pcard .el{font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;color:var(--mute)}
a.el{color:var(--navy)}
.pcard .pdismiss{appearance:none;border:1px solid transparent;background:none;cursor:pointer;color:var(--off);font-size:15px;line-height:1;padding:2px 7px;border-radius:6px;margin-top:2px}
.pcard .pdismiss:hover{color:var(--alarm);border-color:var(--rule)}
.badge.working{background:var(--navy-wash);color:var(--navy);border-color:#C7D8E2}
.badge.failed{background:var(--alarm-wash);color:var(--alarm);border-color:#E7C8C1}

/* home: tabs, sort, stars */
.htoolbar{margin-top:30px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;border-bottom:1px solid var(--rule2);padding-bottom:12px}
.htoolbar+.cards{margin-top:14px}
.card a.cardmain{display:block;color:inherit;min-width:0}
.card a.cardmain:hover{text-decoration:none;color:inherit}
.card .right{display:flex;flex-direction:column;align-items:flex-end;gap:2px}
.card .hstar{appearance:none;border:1px solid transparent;background:none;cursor:pointer;color:var(--off);font-size:17px;line-height:1;padding:3px 7px;border-radius:6px;margin-top:4px}
.card .hstar:hover{color:var(--ochre);border-color:var(--rule)}
.card .hstar.on{color:var(--ochre)}
.hnone{padding:22px 0 4px;color:var(--faint);font-style:italic}

/* scan header */
.plain{margin:10px 0 0;font-size:12.5px;color:var(--faint)}
.btn.primary.demo-off:disabled{cursor:not-allowed;opacity:.45}

/* digest: uncited mark, upcoming */
.digest p .unc{display:inline-block;font-family:var(--mono);font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);border:1px dashed var(--rule);border-radius:4px;padding:1px 5px;margin-left:6px;vertical-align:2px;line-height:1.3}
.upc{margin-top:20px;padding-top:16px;border-top:1px solid var(--rule3)}
.upc ul{list-style:none;margin:8px 0 0;padding:0;max-width:720px}
.upc li{border-bottom:1px solid var(--rule3)}
.upc li a{display:grid;grid-template-columns:96px minmax(0,1fr);gap:12px;padding:7px 0;font-size:13px;line-height:1.45;color:var(--ink)}
.upc li a:hover{text-decoration:none;color:var(--navy)}
.upc li .d{font-family:var(--mono);font-size:11px;color:var(--mute);padding-top:2px;white-space:nowrap}
.upc li .who{font-weight:500}
.upc li .sep{color:var(--off);margin:0 6px}
.upc .none{color:var(--faint);font-style:italic;margin-top:6px;font-size:13px}

/* the subject filter, on Coverage and again on Audit: a filter standing between a venue and the
   ledger is a fact about coverage AND a fact anyone auditing for a miss has to know. */
.subjbox{border:1px solid var(--rule2);border-radius:10px;background:var(--row3);padding:14px 16px;margin-top:8px}
.subjbox.off{border-color:var(--ochre);background:var(--ochre-wash)}
.subjbox .rxline{font-family:var(--mono);font-size:12px;color:var(--ink);background:var(--paper);border:1px solid var(--rule2);border-radius:6px;padding:8px 10px;overflow-x:auto;white-space:pre}
.subjbox .who{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-right:6px}
.subjbox p{margin:9px 0 0;font-size:12.5px;line-height:1.55;color:var(--mute)}
.subjbox p b{color:var(--ink);font-weight:600}
.subjbox .term{font-family:var(--mono);font-size:11.5px;background:var(--paper);border:1px solid var(--rule2);border-radius:4px;padding:1px 5px;margin:0 3px 3px 0;display:inline-block}
.subjbox .nums{margin-top:10px;display:flex;gap:18px;flex-wrap:wrap;font-family:var(--mono);font-size:11.5px;color:var(--mute)}
.subjbox .nums b{font-size:15px;color:var(--ink);font-family:var(--sans);margin-right:4px}
.subjbox .broken{color:var(--alarm)}
.coverage .ev.subj{color:var(--mute)}
.coverage .ev.subj b{color:var(--ink)}

/* obligations register */
.oblsec{margin-top:44px}
.oblsec h3{font-family:var(--serif);font-weight:400;font-size:19px;margin:0 0 4px}
.oblsec .sub{color:var(--mute);font-size:12.5px;margin:0 0 10px}
.oblsec table.obl td:last-child,.oblsec table.obl th:last-child{padding-right:0}
.oblsec .devlink{appearance:none;border:0;background:none;padding:0;cursor:pointer;color:var(--ink);text-align:left;font:inherit;line-height:1.45;text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--rule)}
.oblsec .devlink:hover{color:var(--navy);text-decoration-color:var(--navy)}
.oblsec .empty-o{color:var(--faint);font-style:italic;padding:14px 0}
.oblsec .w{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-top:3px}

/* unrated relevance: empty bars, quiet word */
.rel.unrated .bars i{background:var(--rule)}
.rel.unrated .lv{color:var(--faint);font-style:italic;text-transform:none}

/* read state of a development (queued / not read / metadata only / enrichment failed / truncated) */
.state{margin:22px 0 -10px;padding:9px 12px;border-radius:8px;background:var(--ochre-wash);color:#5B4507;font-size:12.5px;line-height:1.5;border:1px solid #E4D19A}
.state.quiet{background:var(--panel);color:var(--mute);border-color:var(--rule3)}
/* coverage: the last run's counts, one line each */
.lastrun{display:flex;gap:18px;flex-wrap:wrap;margin:0 0 6px;padding:10px 14px;border:1px solid var(--rule3);border-radius:8px;background:var(--row3);font-family:var(--mono);font-size:11px;color:var(--mute)}
.lastrun b{font-weight:500;color:var(--ink)}
.lastrun .bad b{color:var(--alarm)}
.coverage .gap{margin:4px 0 0;color:#5B4507;font-size:12.5px}
.coverage .disc{margin:0;padding-left:16px;font-size:12.5px;color:var(--mute);line-height:1.6}
/* digest: selection rule and verifier notes, in small type under the label */
.digest .fine{margin:4px 0 0;font-family:var(--mono);font-size:10.5px;color:var(--faint);line-height:1.6}
.kpi-tile .sub{margin-top:4px;font-family:var(--mono);font-size:10.5px;color:var(--faint)}

/* coverage: tier legend, GATED wording */
.legend{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 12px;align-items:start;margin:0 0 8px;font-size:12.5px;line-height:1.5;color:var(--mute);padding:10px 14px;border:1px solid var(--rule3);border-radius:8px;background:var(--row3)}
.legend .badge{margin-top:3px}
.src .ev .gated{color:var(--alarm)}

/* ---- the scan's own tab bar. Same vocabulary as the tracker's nav (uppercase mono labels, a
   3px ochre underline on the active one), on paper rather than navy, because the navy bar above
   it is the site's nav and two identical bars would read as one broken one. */
.vtabs{margin-top:30px;display:flex;gap:2px;overflow-x:auto;border-bottom:1px solid var(--rule2)}
.vtabs button{appearance:none;cursor:pointer;background:none;border:0;border-bottom:3px solid transparent;
  padding:12px 16px;font-family:var(--sans);font-size:11px;font-weight:600;text-transform:uppercase;
  letter-spacing:.14em;color:var(--faint);white-space:nowrap;margin-bottom:-1px}
.vtabs button:hover{color:var(--ink)}
.vtabs button.on{color:var(--ink);border-bottom-color:var(--ochre)}
.vtabs button .k{font-family:var(--mono);font-size:10px;letter-spacing:.04em;color:var(--off);margin-left:7px}
.vtabs button.on .k{color:var(--mute)}
.view{display:none}
.view.on{display:block}
.view .toolbar{margin-top:22px}
.viewhead{margin-top:26px}
.viewhead .eyebrow{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
.viewhead h3{font-family:var(--serif);font-weight:400;font-size:22px;margin:6px 0 0}
.viewhead .sub{margin:6px 0 0;color:var(--mute);font-size:13px;line-height:1.6;max-width:860px}
/* A lane that is empty says which types would land in it and why none did. */
.laneempty{margin-top:8px;border:1px dashed var(--rule);border-radius:10px;padding:26px 28px;max-width:760px}
.laneempty h4{font-family:var(--serif);font-weight:400;font-size:18px;margin:0 0 6px;letter-spacing:0;
  text-transform:none;color:var(--ink)}
.laneempty p{margin:0 0 6px;color:var(--mute);font-size:13px;line-height:1.6}
.laneempty p:last-child{margin-bottom:0}
.laneempty .why{color:#5B4507}
.chip.untyped{border-style:dashed;color:#7A5E0E;background:var(--ochre-wash);border-color:#E4D19A}

/* ---- Miscellaneous. The header copy is the most important text on this page: everything below
   it is unfetched, ungated, uncitable, and outside the coverage list. */
.miscwarn{margin-top:18px;border:1px solid #E4D19A;border-left:3px solid var(--ochre);border-radius:10px;
  padding:18px 22px;background:var(--ochre-wash);color:#4A3806;max-width:920px}
.miscwarn h4{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:#7A5E0E;margin:0 0 8px;font-weight:600}
.miscwarn p{margin:0 0 8px;font-size:13px;line-height:1.65}
.miscwarn p:last-child{margin-bottom:0}
.miscwarn b{font-weight:600}
.miscq{margin-top:16px;padding:10px 14px;border:1px solid var(--rule3);border-radius:8px;background:var(--row3);
  font-family:var(--mono);font-size:11px;color:var(--mute);line-height:1.7}
.miscq b{font-weight:500;color:var(--ink)}
.miscgrp{margin-top:26px}
.miscgrp .gh{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
  padding-bottom:6px;border-bottom:1px solid var(--rule3)}
.miscgrp .gsub{margin:6px 0 0;color:var(--mute);font-size:12.5px;line-height:1.55;max-width:800px}
.mrow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;padding:14px 0;border-bottom:1px solid var(--rule3);align-items:start}
.mrow .mt{font-size:13.5px;font-weight:600;line-height:1.4}
.mrow .mt a{text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--rule)}
.mrow .mm{margin-top:4px;font-family:var(--mono);font-size:10.5px;color:var(--faint);letter-spacing:.02em}
.mrow .mw{margin-top:5px;color:var(--mute);font-size:12.5px;line-height:1.55;max-width:720px}
.mrow .ms{margin:6px 0 0;padding:7px 11px;border-left:2px solid var(--rule2);color:var(--mute);font-size:12.5px;
  line-height:1.5;font-style:italic;background:var(--row3)}
.mrow .mact{display:flex;flex-direction:column;align-items:flex-end;gap:5px;min-width:132px}
.mrow .mst{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--faint)}
.mrow .mst.promoted{color:var(--ok)}
.mrow .mst.stale{color:#7A5E0E}
/* A lead the last search no longer returns is kept, never deleted — so it must LOOK different
   from one the search still stands behind, or the page ages silently. */
.mrow.stale{background:linear-gradient(90deg,var(--ochre-wash),transparent 70%)}
.mrow .mstale{margin-top:5px;font-size:12px;line-height:1.5;color:#5B4507;max-width:720px}
.miscgrp .gh .ghrest{margin-left:10px;letter-spacing:.04em;text-transform:none;font-size:10.5px;color:var(--off)}
.mnone{padding:16px 0;color:var(--faint);font-style:italic;font-size:13px}

/* ---- Clients: one section per client named on the scan */
.clsec{margin-top:26px;border-top:1px solid var(--rule2);padding-top:18px}
.clsec:first-of-type{border-top:0;padding-top:6px}
.clsec .cn{font-family:var(--serif);font-size:21px;line-height:1.25;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.clsec .cscope{margin-top:5px;font-size:12.5px;color:var(--mute)}
.clsec .cscope b{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--navy);margin-right:7px}
.clsec .ccount{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;color:var(--faint)}
.clitem{padding:14px 0;border-bottom:1px solid var(--rule3);display:grid;grid-template-columns:96px minmax(0,1fr) auto;gap:16px;align-items:start}
.clitem .cl-l{padding-top:2px}
.clitem .ct{font-size:13.5px;font-weight:600;line-height:1.4}
.clitem .ct button{appearance:none;border:0;background:none;padding:0;cursor:pointer;font:inherit;text-align:left;
  text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--rule);color:var(--ink)}
.clitem .ct button:hover{color:var(--navy);text-decoration-color:var(--navy)}
.clitem .cm{margin-top:4px;font-family:var(--mono);font-size:10.5px;color:var(--faint)}
.clitem .cw{margin-top:5px;color:var(--mute);font-size:12.5px;line-height:1.55}
.clitem .ca{margin-top:7px;padding:8px 12px;border-left:2px solid var(--ochre);background:var(--ochre-wash);
  color:#3d2f05;font-size:12.5px;line-height:1.5;border-radius:0 6px 6px 0}
.clitem .cbtn{min-width:120px;text-align:right}

/* ---- Audit: link-wise, the tracker's own framing */
.audsrc{border:1px solid var(--rule2);border-radius:10px;margin-top:12px;background:#fff}
.audsrc .ah{display:grid;grid-template-columns:12px minmax(0,1fr) auto;gap:12px;align-items:center;padding:13px 16px;cursor:pointer}
.audsrc .ah:hover{background:var(--row3)}
.audsrc .ah:focus-visible{outline:2px solid var(--navy);outline-offset:-2px}
/* the same health dot as the coverage panel, so one colour means one thing on both tabs */
.audsrc .dotc{width:8px;height:8px;border-radius:50%;background:var(--off);display:inline-block}
.audsrc .dotc.OK{background:var(--ok)}.audsrc .dotc.QUIET{background:var(--ochre)}
.audsrc .dotc.EMPTY{background:var(--off)}.audsrc .dotc.FAILED{background:var(--alarm)}.audsrc .dotc.GATED{background:var(--alarm)}
.audsrc .an b{font-weight:600}
.audsrc .an{font-size:13.5px;line-height:1.4;min-width:0}
.audsrc .ahost{display:block;font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-top:3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.audsrc .acount{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;color:var(--mute);white-space:nowrap}
.audsrc .abody{display:none;padding:0 16px 16px 40px;border-top:1px solid var(--rule3)}
.audsrc.open .abody{display:block}
.audsrc .ameta{margin-top:10px;font-family:var(--mono);font-size:11px;color:var(--mute);line-height:1.7}
.audsrc .alink{font-family:var(--mono);font-size:11px;color:var(--navy);text-decoration:underline;text-underline-offset:3px}
.aitem{display:grid;grid-template-columns:88px 42px minmax(0,1fr);gap:10px;padding:7px 0;border-bottom:1px solid var(--rule3);font-size:12.5px;line-height:1.45}
.aitem .ad{font-family:var(--mono);font-size:11px;color:var(--faint)}
.aitem .al{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--mute);
  border:1px solid var(--rule2);border-radius:20px;padding:1px 0;text-align:center;height:16px;line-height:14px}
.aitem button{appearance:none;border:0;background:none;padding:0;cursor:pointer;font:inherit;text-align:left;
  text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--rule);color:var(--ink)}
.aitem button:hover{color:var(--navy)}
.anone{margin-top:12px;padding:10px 13px;border-radius:8px;background:var(--panel);color:var(--mute);font-size:12.5px;line-height:1.55}
.anone.bad{background:var(--alarm-wash);color:var(--alarm)}
.averify{margin-top:12px;font-family:var(--mono);font-size:10.5px;letter-spacing:.03em;color:var(--faint);line-height:1.7}

/* ---- create dialog: the coverage preview that gates Create */
.prev{margin-top:20px;border:1px solid var(--rule2);border-radius:10px;background:var(--row3);padding:16px 18px}
.prev h4{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin:0 0 8px;font-weight:500}
.prev .pj{margin-top:12px}
.prev .pj:first-of-type{margin-top:0}
.prev .pjh{font-size:12.5px;font-weight:600;display:flex;gap:8px;align-items:baseline}
.prev ul{list-style:none;margin:5px 0 0;padding:0}
.prev li{padding:5px 0 5px 12px;border-left:2px solid var(--rule2);margin-top:4px}
.prev li .pn{font-size:12.5px;font-weight:500}
.prev li .ph{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:7px}
.prev li .pr{font-size:12px;color:var(--mute);line-height:1.45;margin-top:2px}
.prev .pdrop{float:right;margin-left:8px;border:1px solid var(--rule2);background:var(--paper);color:var(--faint);border-radius:4px;width:20px;height:20px;line-height:1;cursor:pointer;font-size:13px}.pdrop:hover{border-color:var(--alarm);color:var(--alarm)}.pfind{border:1px solid var(--rule2);background:var(--paper);color:var(--navy);border-radius:4px;padding:1px 7px;margin-left:4px;font-size:11px;font-family:var(--mono);cursor:pointer}.pfind:hover{border-color:var(--navy)}
.sched.on{color:var(--ochre);font-weight:600}
.pgap{margin-top:12px;font-size:12.5px;color:#5B4507;line-height:1.5}
.prev .pfine{margin-top:14px;padding-top:12px;border-top:1px solid var(--rule3);font-size:12px;color:var(--mute);line-height:1.6}
.prev .pfine b{color:var(--ink);font-weight:600}
.prevbar{margin-top:16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.prevbar .pwhy{font-size:12px;color:var(--faint);line-height:1.45;flex:1;min-width:200px}

/* ---- create dialog: the subject filter, the other half of "what will this scan collect?".
   It sits inside the same block as the coverage list, above it, because a partner choosing
   venues and a partner choosing the subject are answering one question, not two. */
.subj{margin-top:14px;border:1px solid var(--rule2);border-radius:10px;background:var(--row3);padding:16px 18px}
.subj h4{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin:0 0 8px;font-weight:500}
.subrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.subrow input.rx{flex:1;min-width:220px;font-family:var(--mono);font-size:12px;padding:8px 10px;border:1px solid var(--rule2);border-radius:6px;background:var(--paper);color:var(--ink)}
.subrow input.rx:focus{outline:2px solid var(--navy);outline-offset:-1px}
.subrow input.rx.bad{border-color:var(--alarm)}
.subj .btn.sm{padding:6px 10px;font-size:11.5px}
.subj .swhy{margin-top:9px;font-size:12.5px;color:var(--ink);line-height:1.5}
.subj .swhy .who{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-right:6px}
.subj .splain{margin-top:10px;font-size:12.5px;color:var(--mute);line-height:1.55}
.subj .splain b{color:var(--ink);font-weight:600}
.subj .splain .term{font-family:var(--mono);font-size:11.5px;background:var(--paper);border:1px solid var(--rule2);border-radius:4px;padding:1px 5px;margin:0 3px 3px 0;display:inline-block}
.subj .sillus{margin-top:10px;font-size:12.5px;color:var(--mute);line-height:1.55}
.subj .sbad{margin-top:10px;font-size:12.5px;color:var(--alarm);line-height:1.5}
.subj .sopen{margin-top:10px;font-size:12.5px;color:#5B4507;line-height:1.55}
.subj .sopen b{font-weight:600}

@media (max-width:900px){
  .head,.tabs{padding-left:22px;padding-right:22px}
  .clitem,.mrow{grid-template-columns:1fr}
  .clitem .cbtn{text-align:left}
  .mrow .mact{align-items:flex-start}
  .page{padding:26px 22px 90px}
  .digest{grid-template-columns:1fr}
  .kpis{flex-direction:row}
  .kpi-tile{flex:1}
  .card,.pcard{grid-template-columns:1fr}
  .card .right{text-align:left}
  .pcard .right{align-items:flex-start;min-width:0}
  .panel{width:100vw}
}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
</style>

<div class="sheet">
  <div class="head">
    <div class="wordmark"><a href="/">TMT <b>Regulatory Radar</b></a></div>
    <div class="updbar"><div class="updated" id="headstamp"></div></div>
  </div>
  <!-- DEFECT this closes: these were the TMT tracker's six tabs, rendered on every scan page and
       every one of them linking to /tmt-radar-v2.html. On the EU Pay Transparency scan, pressing
       "Instruments" left the scan and opened TMT India's ledger — and it shadowed the scan's OWN
       seven-tab bar, which sits with the content and is the real navigation here. A scan page
       gets a way home and nothing else; the lanes belong to the scan. -->
  <nav class="tabs" aria-label="Where you are">
    <a class="tab-scans on" href="/" aria-current="page">&larr; All scans</a>
  </nav>
  <main class="page" id="main"></main>
</div>

<div class="scrim" id="scrim"></div>
<aside class="panel" id="panel" aria-label="Development detail" aria-hidden="true"></aside>
<div class="hc" id="hc" role="tooltip"></div>

<dialog id="dlg" aria-labelledby="dlg-title" data-step="describe">
  <form id="dlg-form" method="dialog" novalidate>
    <div class="dh"><h2 id="dlg-title">Create scan</h2><button type="button" class="pclose" data-close aria-label="Close">×</button></div>
    <div class="db only-describe describe">
      <div class="field"><label for="f-desc">What do you want this scan to track?</label>
        <textarea id="f-desc" maxlength="2000" placeholder="Advise multinational-employer clients on national transposition of the Pay Transparency Directive; surface new obligations, thresholds and deadlines by country."></textarea>
        <div class="help">One box. The model proposes the name, intent, jurisdictions, topics and any official sources it knows, for you to confirm or edit. Nothing is created until you press Create scan, and every proposed source still goes through the gate.</div></div>
      <div class="paths"><button type="button" class="btn primary" id="dlg-build">Build the scan</button><span class="or">or</span><button type="button" class="btn" id="dlg-manual">Create manually</button></div>
      <div class="pnote" id="dlg-pnote-1" aria-live="polite"></div>
    </div>
    <div class="db only-form">
      <div class="pnote" id="dlg-pnote" aria-live="polite"></div>
      <div class="field"><label for="f-name">Name<span class="req">*</span></label><input type="text" id="f-name" maxlength="120" autocomplete="off"></div>
      <div class="field"><label for="f-intent">Intent<span class="req">*</span></label>
        <textarea id="f-intent" maxlength="1500"></textarea>
        <div class="help">Brief it the way you would brief an associate: who the clients are, what to advise on, and what to surface — obligations, thresholds, deadlines. Discovery and relevance both read this sentence.</div></div>
      <div class="field"><label for="f-jur">Jurisdictions<span class="req">*</span></label><div class="cin" id="c-jur"></div>
        <div class="help">Type a country or an ISO code and press Enter. EU works for Union-level venues. At least one is needed: the pipeline refuses a scan without a jurisdiction.</div></div>
      <div class="two">
        <div class="field"><label for="f-top">Topics</label><div class="cin" id="c-top"></div></div>
        <div class="field"><label for="f-ind">Industries</label><div class="cin" id="c-ind"></div></div>
      </div>
      <div class="field find" id="find-block">
        <label>Find sources</label>
        <div class="findrow"><button type="button" class="btn" id="dlg-find">Find sources</button>
          <span class="findwhy" id="find-why"></span>
          <span class="findcount" id="find-count" aria-live="polite"></span></div>
        <div class="pnote" id="find-note" aria-live="polite"></div>
        <div class="cands" id="cands"></div>
        <div class="help">These are proposals — nothing has been fetched to produce them. Every one you tick is checked against robots.txt and the site's own terms when the scan is created, and anything that fails is listed as rejected on the coverage panel and never fetched.</div>
      </div>
      <div class="field"><label for="f-src">Sources</label><div class="cin" id="c-src"></div>
        <div class="help">Optional. Add a listing page you already trust; it will still be checked — robots, terms and a parse test — before anything is read from it.</div></div>
      <div class="field"><label for="f-cl">Clients</label><div class="cin" id="c-cl"></div>
        <div class="help">Relevance is rated per named client; the model is asked to name them in the action line.</div></div>
      <div class="field check"><input type="checkbox" id="f-sched"><label for="f-sched">Run daily, unattended, at <input type="time" id="f-sched-at" value="06:30" step="60"> IST</label></div>
      <div class="help" id="f-sched-help">Off by default. Everything else here runs when a person presses a button; this one setting makes the scan read its approved sources once a day without anyone pressing anything. It is shown on the scan with your name, and the legal analysis of unattended collection is on record (engine/audit/scheduling_decision_2026-09-10.md).</div>
      <div class="field check"><input type="checkbox" id="f-disc" checked><label for="f-disc">Discover sources automatically</label></div>
      <div class="help" id="f-disc-help">Off, only the sources listed above are gated and read.</div>
      <div class="field" id="preview-block">
        <label>What this scan will cover</label>
        <div class="prevbar"><span class="pwhy" id="preview-why">Coverage is the whole product, so it is built here with you — tick a venue, add one, or search a gap, and this list follows.</span></div>
        <!-- The subject filter belongs HERE, above the venue list and inside the same block: the
             venues answer "where will this scan read?" and the filter answers "what on those pages
             is this scan's subject?". They are one question. A scan created without an answer to
             the second half reads a regulator's whole listing — the defect this closes ledgered 118
             developments of which eight mentioned AI, and spent the entire first reading budget on
             telecom quality-of-service notices and Oracle CVE bulletins. -->
        <div class="subj" id="subject-block">
          <h4>Subject — what counts, on the pages above</h4>
          <div class="subrow">
            <input type="text" id="f-subject" class="rx" spellcheck="false" autocomplete="off" autocapitalize="off"
                   aria-label="Subject filter, a regular expression matched against each row's title"
                   placeholder="\b(AI|artificial intelligence|machine learning)\b">
            <button type="button" class="btn sm" id="dlg-subject">Propose from the brief</button>
            <button type="button" class="btn sm quiet" id="subject-clear">Read everything</button>
          </div>
          <div class="swhy" id="subject-why"></div>
          <div class="pnote" id="subject-note" aria-live="polite"></div>
          <div class="splain" id="subject-plain"></div>
          <div class="sillus" id="subject-illus"></div>
        </div>
        <div class="prev" id="preview"></div>
      </div>
      <div class="firstrun">The first run reads the newest __FIRST_RUN_MAX__ documents, so the scan appears quickly rather than after every backlogged page. Anything older queues and is counted as queued on the scan. Press <b>Run scan</b> again to continue through the backlog.</div>
    </div>
    <div class="df"><div class="err" id="dlg-err" aria-live="polite"></div>
      <div class="actions"><button type="button" class="btn quiet only-form" id="dlg-back">Describe instead</button><button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn primary only-form" id="dlg-submit">Create scan</button></div></div>
  </form>
</dialog>

<dialog id="modal" class="modal" aria-labelledby="modal-title">
  <div class="dh"><h2 id="modal-title">Draft</h2><button type="button" class="pclose" data-close aria-label="Close">×</button></div>
  <div class="db" id="modal-body"></div>
  <div class="df"><div class="fb" id="modal-note"></div><div class="actions"><button type="button" class="btn" id="modal-copy">Copy</button><button type="button" class="btn primary" data-close>Done</button></div></div>
</dialog>

<script id="scan-data" type="application/json">__DATA__</script>
<script>
'use strict';
const D = JSON.parse(document.getElementById('scan-data').textContent);
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmt = iso => { if (!iso) return '—'; const p = String(iso).slice(0,10).split('-'); return p.length === 3 ? (+p[2]) + ' ' + MON[+p[1]-1] + ' ' + p[0] : iso; };
const pl = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');
const flag = code => { const c = String(code || '').toUpperCase(); if (!/^[A-Z]{2}$/.test(c)) return ''; return String.fromCodePoint(...[...c].map(ch => 0x1F1E6 + ch.charCodeAt(0) - 65)); };
const flagged = code => (flag(code) ? flag(code) + ' ' : '') + esc(code);
// Relative time is computed at open, never at build: a page says "3 days ago" because it is.
const rel = iso => {
  if (!iso) return 'never';
  const t = new Date(iso); if (isNaN(t)) return iso;
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  const m = Math.round(s / 60); if (m < 60) return m + ' min ago';
  const h = Math.round(m / 60); if (h < 36) return h + ' hr ago';
  const d = Math.round(h / 24); if (d < 14) return d + ' day' + (d === 1 ? '' : 's') + ' ago';
  const w = Math.round(d / 7); if (w < 9) return w + ' weeks ago';
  return fmt(iso);
};
// The run stamp is IST by contract (common.now_ist); read the clock out of the string rather than
// through the viewer's timezone, or a partner abroad sees a time the run never happened at.
const stampText = iso => { if (!iso) return ''; const m = String(iso).match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/); return m ? fmt(m[1]) + ', ' + m[2] : iso; };
const COUNTRIES = {'austria':'AT','belgium':'BE','bulgaria':'BG','croatia':'HR','cyprus':'CY','czechia':'CZ','czech republic':'CZ','denmark':'DK','estonia':'EE','finland':'FI','france':'FR','germany':'DE','greece':'GR','hungary':'HU','ireland':'IE','italy':'IT','latvia':'LV','lithuania':'LT','luxembourg':'LU','malta':'MT','netherlands':'NL','poland':'PL','portugal':'PT','romania':'RO','slovakia':'SK','slovenia':'SI','spain':'ES','sweden':'SE','european union':'EU','eu':'EU','united kingdom':'GB','uk':'GB','britain':'GB','switzerland':'CH','norway':'NO','iceland':'IS','india':'IN','united states':'US','usa':'US','canada':'CA','australia':'AU','new zealand':'NZ','singapore':'SG','japan':'JP','south korea':'KR','korea':'KR','china':'CN','hong kong':'HK','indonesia':'ID','malaysia':'MY','philippines':'PH','thailand':'TH','vietnam':'VN','uae':'AE','united arab emirates':'AE','saudi arabia':'SA','qatar':'QA','israel':'IL','turkey':'TR','south africa':'ZA','nigeria':'NG','kenya':'KE','egypt':'EG','brazil':'BR','mexico':'MX','argentina':'AR','chile':'CL','colombia':'CO','sri lanka':'LK','bangladesh':'BD','pakistan':'PK','nepal':'NP','mauritius':'MU'};
const NAMES = {}; Object.keys(COUNTRIES).forEach(k => { if (!NAMES[COUNTRIES[k]] || k.length > NAMES[COUNTRIES[k]].length) NAMES[COUNTRIES[k]] = k; });
const countryName = code => { const n = NAMES[code]; return n ? n.replace(/\b\w/g, c => c.toUpperCase()).replace('Uk', 'UK').replace('Usa', 'USA').replace('Uae', 'UAE') : code; };
// discover.KINDS -> the chip label Harvey's Sources column uses, embedded by the builder so the
// source picker in the dialog and the developments table cannot disagree about what a venue is.
const KINDL = __KIND_LABELS__;
const hostOf = u => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return ''; } };
const SOURCE_KINDS = Object.keys(KINDL).concat(['other']);   // = discover.KINDS / run.SOURCE_KINDS
// The number the pipeline actually caps the first run at (run.FIRST_RUN_MAX_NEW), carried in the
// payload rather than typed here: the dialog and the pending card must promise what the run does.
const FIRST_RUN_MAX = (typeof D.firstRunMax === 'number' && D.firstRunMax > 0) ? D.firstRunMax : 20;
// The pipeline's own max_sources ceiling (common.Budget.CEILINGS), so the picker can say when a
// partner has ticked more venues than a run will ever fetch.
const MAX_SOURCES = (typeof D.maxSources === 'number' && D.maxSources > 0) ? D.maxSources : 12;
const FIRST_RUN_LINE = 'The first run reads the newest ' + FIRST_RUN_MAX + ' documents so the scan appears quickly; anything older queues and is counted as queued. Press Run scan again to continue through the backlog.';

$('#headstamp').innerHTML = D.page === 'home'
  ? 'Human-run scans · <span>nothing scheduled</span>'
  : 'Last run <span>' + esc(D.generated ? stampText(D.generated) + ' IST' : 'never') + '</span>';

// ---- notice bar ------------------------------------------------------------------------------
let noticeEl = null;
function say(html, kind) { if (!noticeEl) return; noticeEl.className = 'notice on' + (kind ? ' ' + kind : ''); noticeEl.innerHTML = html; }
// The run log is the affordance of LAST resort, and it is worded that way. A partner should never
// have to open a CI page to know what is happening — that is what the pending cards are for — so
// this link appears only where something has gone wrong or cannot be seen from here, never as the
// main event, and it does not name the service it points at.
const runLog = (url, label) => {
  const u = url || D.actionsUrl || '';
  return u ? ' <a href="' + esc(u) + '" target="_blank" rel="noopener">' + esc(label || 'Open the run log') + '</a>.' : '';
};
async function postJSON(url, body, ms) {
  // `ms` is opt-in, and only the discovery call and the status poll ask for it. A fetch with no
  // deadline is indistinguishable from a slow model: the Find sources button would spin for ever
  // instead of falling back to the manual input, which is the whole point of the fallback.
  const ctl = (ms && typeof AbortController === 'function') ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), ms) : null;
  try {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: ctl ? ctl.signal : undefined });
    let data = null; try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, ok: res.ok, data: data || {} };
  } finally { if (timer) clearTimeout(timer); }
}
// Returns the endpoint's own answer on success (it carries scan_id and actionsUrl, which the
// pending card needs) and null on any failure, so callers can still write `if (ok)`.
async function dispatchScan(body, verb) {
  say('Asking the pipeline to ' + esc(verb) + '…');
  try {
    const r = await postJSON(D.api.scans, body);
    // The normal path names nothing but the work: the card below carries the progress from here,
    // and this page loads the result by itself, so there is nothing for the partner to go and do.
    //
    // The endpoint's own 202 message is deliberately NOT echoed. api/scans.js's queued message
    // names the CI service by name, tells the partner the page "refreshes itself" without saying
    // when, and on a promotion describes a gate that does not in fact run until the next run.
    // Those are that function's words about its own plumbing; this page's job is to say what is
    // happening to the partner's scan. Failures below DO carry its message verbatim: a
    // misconfiguration is exactly the case where the endpoint knows something this page cannot.
    if (r.ok) { say('Started. The progress is below, and this page shows the result as soon as it lands.'); return r.data || {}; }
    say(esc(r.data.message || ('The scans endpoint answered ' + r.status + '.'))
        + runLog(null, r.status === 501 ? 'Open the run log and start it by hand' : 'Open the run log'), 'bad');
  } catch (e) {
    say('This page could not reach the endpoint that starts a run.' + runLog(), 'bad');
  }
  return null;
}

// ---- pending runs: dispatched, and waited for HERE --------------------------------------------
// The dispatch is accepted in a second; the result does not exist until the workflow has gated,
// read, committed, and the site has rebuilt — about twelve minutes, of which the last two are the
// rebuild. The defect this closes: between those two moments the page said nothing, and a partner
// had to go and watch a CI page to know anything was happening, then guess when to reload. These
// records bridge the gap. They live in this browser only, like stars and triage, and each is
// dropped the moment the built page carries its result.
const PKEY = 'tmt_scans_pending_v1';
const PENDING_STALE_MS = 90 * 60 * 1000;
const STATUS_MS = 15000;   // how often the run's own status is asked for
const STAMP_MS = 20000;    // how often this page asks whether a newer version of itself exists
let redrawPending = null;  // set by whichever page mounted the cards
function pendingRead() {
  let v = null;
  try { v = JSON.parse(localStorage.getItem(PKEY) || '[]'); } catch (e) { v = null; }
  // An entry without an id can never be matched against a built card or polled for, so it could
  // only ever expire: refuse it on the way in rather than let it sit on the page for 90 minutes.
  return Array.isArray(v) ? v.filter(p => p && typeof p === 'object' && typeof p.id === 'string' && p.id) : [];
}
function pendingWrite(list) { try { localStorage.setItem(PKEY, JSON.stringify(list)); } catch (e) {} }
function addPending(rec) {
  if (!rec || !rec.id) return;
  pendingWrite(pendingRead().filter(p => p.id !== rec.id).concat([rec]));
  if (redrawPending) redrawPending();
}
// Older records (written before Try again existed) carry `action` and no `kind`.
const kindOf = p => String(p.kind || p.action || 'create');

// ---- what a run is probably doing -------------------------------------------------------------
// The status action gives three words — queued, in_progress, completed — and a conclusion. It
// never says which STEP is running. A spinner would be honest and useless; a named step would be
// useful and false. So the card states what it observes as fact and fills the long middle with an
// estimate derived from the clock against the shape we have measured: about two minutes before the
// runner has the code, then the gate, then the reading (which dominates), then the digest, then
// about two minutes committing and rebuilding this page. Every estimate is worded as one — "usually
// reading documents around now" — because a step that cannot be observed must never be asserted.
const RUN_PHASES = [{ to: 2, what: 'setting up' }, { to: 4, what: 'gating the venues' },
                    { to: 9.5, what: 'reading documents' }, { to: 11, what: 'writing the digest' },
                    { to: Infinity, what: 'publishing' }];
// A promotion is a different, much shorter errand: it edits the definition and commits. It reads
// nothing and gates nothing, so it must not borrow the reading run's phases.
const PROMOTE_PHASES = [{ to: 2, what: 'setting up' }, { to: 3, what: 'adding the venue to the sources list' },
                        { to: Infinity, what: 'publishing' }];
function phaseAt(mins, table) {
  const t = table || RUN_PHASES;
  for (let i = 0; i < t.length; i++) { if (mins < t[i].to) return t[i].what; }
  return t[t.length - 1].what;
}
const aboutMins = m => (m < 1 ? 'just started' : m < 1.5 ? 'about a minute in' : 'about ' + Math.round(m) + ' minutes in');
const QUEUED = ['queued', 'waiting', 'requested', 'pending'];

// ---- this page picking up its own result ------------------------------------------------------
// The workflow commits, the site rebuilds, and the page a partner is looking at knows none of it.
// So while anything is pending, the page fetches ITSELF with cache:'no-store' and compares the
// stamp the builder embedded — a hash of the DATA, not of the build, so a rebuild that changed
// nothing never disturbs a reader. When it differs, the result has landed.
let stampTimer = null, hasLanded = false, waitingForClear = false;
let currentTab = '';   // the open tab, set by whichever page is rendered; travels in the hash
function stampWatch(on) {
  if (!on || hasLanded) { if (stampTimer) { clearInterval(stampTimer); stampTimer = null; } return; }
  if (!stampTimer) stampTimer = setInterval(checkStamp, STAMP_MS);
}
async function checkStamp() {
  if (hasLanded) return;
  try {
    const res = await fetch(location.pathname + location.search, { cache: 'no-store' });
    if (!res.ok) return;                    // an auth challenge or a 5xx: try again next tick
    const m = (await res.text()).match(/name="tmt-stamp" content="([^"]*)"/);
    if (!m || m[1] === D.stamp) return;
  } catch (e) { return; }                   // offline: the next tick tries again
  hasLanded = true;
  stampWatch(false);
  showLanded();
}
// A reload while a dialog is open, or while the detail slide-over is showing, throws away what the
// partner is in the middle of reading or typing. So the bar goes up either way and the reload
// waits for the screen to be clear — with a button for a partner who would rather not wait.
function pageBusy() {
  if ($$('dialog[open]').length) return true;
  const panel = $('#panel');
  return !!(panel && panel.classList.contains('on'));
}
// The tab travels in the URL hash so the reload lands the partner back where they were. It is
// written with replaceState on every tab change rather than by assigning location.hash, which
// would push a history entry per click and turn Back into a tab-by-tab rewind.
function setTabHash(k) {
  currentTab = k || '';
  if (!currentTab) return;
  try { history.replaceState(null, '', location.pathname + location.search + '#tab=' + currentTab); } catch (e) {}
}
const tabFromHash = () => { const m = String(location.hash || '').match(/^#tab=([a-z-]{1,24})$/); return m ? m[1] : ''; };
function showLanded() {
  if (!pageBusy()) {
    say('This scan has finished — showing the new version.');
    // A beat, so the bar is read rather than flashed. The hash already holds the open tab.
    setTimeout(() => location.reload(), 800);
    return;
  }
  say('This scan has finished. The new version loads as soon as you close what is open — '
    + '<button type="button" class="linkbtn" id="shownow">show it now</button>.', 'warn');
  const b = $('#shownow'); if (b) b.addEventListener('click', () => location.reload());
  if (waitingForClear) return;
  waitingForClear = true;
  const t = setInterval(() => { if (!pageBusy()) { clearInterval(t); waitingForClear = false; showLanded(); } }, 1500);
}

// ---- the pending cards ------------------------------------------------------------------------
// One implementation, mounted by both pages. Reviewed gap: these cards existed only on the Scans
// home, so a partner who pressed Run scan on a scan's own page got a one-line notice and then
// nothing at all for twelve minutes — the exact wait this whole mechanism exists to fill.
//
// ctx.mine(p)      — is this record about the thing this page shows?
// ctx.builtNow(p)  — has the committed page this record was waiting for arrived?
function mountPending(el, ctx) {
  let runState = {};             // record id -> the last status answer, kept across redraws
  let ticker = null, statusTimer = null;

  const elapsed = iso => {
    const t = Date.parse(iso || '');
    if (!isFinite(t)) return '';
    const s = Math.max(0, Math.round((Date.now() - t) / 1000)), m = Math.floor(s / 60);
    if (m >= 60) return Math.floor(m / 60) + 'h ' + (m % 60) + 'm elapsed';
    return (m ? m + 'm ' + (s % 60) + 's' : s + 's') + ' elapsed';
  };
  function tickElapsed() { $$('.el[data-since]', el).forEach(x => { x.textContent = elapsed(x.dataset.since); }); }

  function prune() {
    const now = Date.now(), keep = [], mine = [];
    let changed = false;
    pendingRead().forEach(p => {
      if (!ctx.mine(p)) { keep.push(p); return; }          // another page's errand: leave it alone
      // The committed result is on the page now, so the card has nothing left to say. Some errands
      // still owe the partner a sentence about where to look for what changed.
      if (ctx.builtNow(p)) { changed = true; if (ctx.onLanded) ctx.onLanded(p); return; }
      const t = Date.parse(p.dispatched_at || '');
      // Ninety minutes is far past the longest run we have measured. Something went wrong that this
      // page cannot see, so stop pretending to watch it and point at the log.
      if (isFinite(t) && now - t > PENDING_STALE_MS) {
        changed = true;
        say('“' + esc(p.name || p.id) + '” was started more than 90 minutes ago and has still not landed here.'
          + runLog(p.actionsUrl, 'Open the run log'), 'warn');
        return;
      }
      keep.push(p); mine.push(p);
    });
    if (changed) pendingWrite(keep);
    return mine;
  }

  // What the card says, split so each line can be traced to what it rests on: `badge` and the
  // first sentence are OBSERVED (the status endpoint said so, or this page dispatched it itself);
  // `est` is the clock talking and always says so.
  function story(p, st) {
    st = st || {};
    const promo = kindOf(p) === 'promote';
    const status = String(st.status || ''), concl = String(st.conclusion || '');
    const known = !!status;                      // the endpoint has actually reported on this run
    // A run reports status 'completed' a moment before its conclusion is populated. Treating an
    // empty conclusion as success flashed "Finished" over a run that had in fact failed, so a
    // completed-but-unrated run is NOT yet done: the card keeps waiting for the verdict.
    const done = status === 'completed' && !!concl;
    const bad = done && concl !== 'success';
    // The phase estimate runs from when the RUN began, not from the button press: a run that sat
    // in a queue for four minutes is not four minutes into reading documents.
    const started = Date.parse(st.created_at || '') || Date.parse(p.dispatched_at || '');
    const mins = isFinite(started) ? Math.max(0, (Date.now() - started) / 60000) : 0;
    const out = { bad: bad, done: done, est: '', note: String(st.message || '') };
    if (bad) {
      out.badge = concl.replace(/_/g, ' ');
      out.badgeClass = 'failed';
      out.line = 'The run stopped — it finished as ' + concl.replace(/_/g, ' ') + '. Nothing was committed, so nothing on this page has changed.';
      return out;
    }
    if (done) {
      out.badge = 'Publishing'; out.badgeClass = 'working';
      out.line = promo
        // Reviewed defect: this card used to say the gate was running. run.py's promote does no
        // such thing — it appends the URL as a PENDING source and commits. The gate (robots.txt,
        // the site's terms, the parse test) runs at the start of the next run, so the card now
        // says what happened and what the partner has to press for the rest to happen.
        ? 'Added to this scan’s sources as pending. Nothing has been fetched from it and the gate has not judged it yet.'
        : 'Finished. Publishing the new version — this page loads it by itself, usually within a couple of minutes.';
      return out;
    }
    if (!known) {
      // The contract's degraded answer (runs:[] with a message) or no status endpoint at all. Say
      // only what is actually known: it was dispatched, this is how long ago, and the run's own
      // state cannot be seen from here. No phase is estimated, because a phase estimate on top of
      // an unknown run state would be a guess dressed as progress.
      out.badge = promo ? 'Adding' : 'Started'; out.badgeClass = 'working';
      out.line = 'Started from this page. Live run status is off on this deployment, so the timer here is this page’s own clock, not the run’s.';
      out.est = 'A run usually takes about twelve minutes end to end. This page still loads the result by itself when it lands.';
      return out;
    }
    if (QUEUED.indexOf(status) >= 0) {
      out.badge = 'Waiting for a runner'; out.badgeClass = 'working';
      out.line = 'Waiting for a runner. Nothing has been read yet.';
      return out;
    }
    out.badge = 'Running'; out.badgeClass = 'working';
    out.line = promo ? 'Running — this errand only edits the scan’s source list; it reads nothing.'
                     : 'Running.';
    out.est = aboutMins(mins) + '; usually ' + phaseAt(mins, promo ? PROMOTE_PHASES : RUN_PHASES)
            + ' around now. That is an estimate from the clock — the run reports that it is running, not which step it is on.';
    return out;
  }

  function pcard(p, st) {
    const s = story(p, st);
    const promo = kindOf(p) === 'promote';
    const url = (st && st.html_url) || p.actionsUrl || D.actionsUrl || '';
    // The fine print belongs to the errand, not to runs in general: the first-run cap is a promise
    // about a CREATE, and repeating it under a re-run of an established scan says something that
    // is not true of the run being waited for.
    const fine = promo
      ? (s.done && !s.bad
          ? 'The gate — robots.txt, the site’s own terms and a parse test — runs at the START of the next run, not now. Press <b>Run scan</b> on this scan when you want it judged. Until it passes, nothing is ever fetched from it, and a refusal appears on <b>Coverage</b> with its reason.'
          : 'Promoting is the only route from Miscellaneous into coverage, and it is a request, not a decision: the same gate that judged every other source will judge this URL at the start of the next run.')
      : kindOf(p) === 'run'
        ? 'This run reads the newest documents each approved source is showing, within the scan’s own caps. Anything beyond them queues and is counted as queued; press Run scan again to continue through the backlog.'
        : esc(FIRST_RUN_LINE);
    return '<div class="pcard' + (s.bad ? ' failed' : '') + '">'
      + '<div><div class="name">' + esc(p.name || p.id) + '<span class="badge ' + esc(s.badgeClass) + '">' + esc(s.badge) + '</span></div>'
      + '<p class="pstate">' + esc(s.line) + '</p>'
      + (s.est ? '<p class="pest">' + esc(s.est) + '</p>' : '')
      + '<div class="fine">' + fine + (s.note ? '<br>' + esc(s.note) : '') + '</div></div>'
      + '<div class="right"><div class="el" data-since="' + esc(p.dispatched_at || '') + '"></div>'
      + (s.bad ? '<button type="button" class="btn sm" data-retry="' + esc(p.id) + '">Try again</button>' : '')
      // Secondary by design, and only where it earns its place: a failure, or a run this page
      // cannot see the state of. In the normal path there is nothing here to click, because there
      // is nothing the partner needs to do.
      + ((s.bad || !st || !st.status) && url
          ? '<a class="el quiet" href="' + esc(url) + '" target="_blank" rel="noopener">Open the run log</a>' : '')
      + '<button type="button" class="pdismiss" data-dismiss="' + esc(p.id) + '" aria-label="Stop watching ' + esc(p.name || p.id) + '" title="Stop watching this run — the result still appears here when it lands">×</button></div></div>';
  }

  function draw() {
    const list = prune();
    el.innerHTML = list.map(p => pcard(p, runState[p.id])).join('');
    if (ctx.onDraw) ctx.onDraw(list);
    tickElapsed();
    // Stop polling a terminal state: a finished run has nothing more to report, and a failed one
    // will never land, so neither timer has anything left to do for it.
    const watching = list.filter(p => { const st = runState[p.id]; return !st || st.status !== 'completed'; });
    const canStillLand = list.some(p => { const st = runState[p.id]; return !st || st.status !== 'completed' || st.conclusion === 'success' || !st.conclusion; });
    // The clock runs only while something can still change. Reviewed defect: it was driven by
    // list.length, so a card left showing a FAILED run ticked its elapsed time upward for ever,
    // which reads as "still working" over a run that stopped.
    schedule(watching.length > 0, watching.length > 0, canStillLand);
  }
  redrawPending = draw;

  // Both timers stop while the tab is hidden and start again when it comes back: a partner who
  // leaves this open in a background tab must not keep the status endpoint busy for an hour to
  // animate a clock nobody is looking at.
  function schedule(wantStatus, wantClock, wantStamp) {
    const vis = !document.hidden;
    if (ticker && !(wantClock && vis)) { clearInterval(ticker); ticker = null; }
    if (statusTimer && !(wantStatus && vis)) { clearInterval(statusTimer); statusTimer = null; }
    // Reviewed defect: the stamp watch was owned by draw() alone, so hiding the tab stopped the
    // status poll and the clock but left this one re-fetching the whole page every 20 s for as
    // long as the tab stayed open — the exact behaviour the comment above says it avoids.
    stampWatch(!!wantStamp && vis);
    if (!vis) return;
    if (wantClock && !ticker) ticker = setInterval(tickElapsed, 1000);
    if (wantStatus && !statusTimer) { statusTimer = setInterval(pollStatus, STATUS_MS); pollStatus(); }
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) schedule(false, false, false); else draw(); });

  async function pollStatus() {
    const list = prune().filter(p => { const st = runState[p.id]; return !st || st.status !== 'completed'; });
    if (!list.length) { draw(); return; }
    for (const p of list) {
      if (document.hidden) return;               // stop mid-list rather than finish the round
      let r = null;
      // A promotion's record is keyed by finding, not by scan; the status action wants the scan the
      // workflow was dispatched for.
      try { r = await postJSON(D.api.scans, { action: 'status', scan_id: p.scan_id || p.id }, 20000); } catch (e) { r = null; }
      // No status endpoint, or it refused: the card keeps its own elapsed clock and says live
      // status is off. Nothing is shown as an error, because nothing about the RUN is known to be
      // wrong — only that this page cannot see it.
      if (!r || !r.ok) continue;
      const runs = Array.isArray(r.data.runs) ? r.data.runs : [];
      // The status action filters by scan id but not by time, so runs[0] can be LAST WEEK's run of
      // the same scan — which would flash "Finished" over a run that has not started. Only a run
      // created at or after this dispatch can be this dispatch. Two minutes of slack absorbs the
      // difference between the browser's clock and the runner's; a run older than that is somebody
      // else's history, and the card keeps saying live status is off rather than borrowing it.
      const since = Date.parse(p.dispatched_at || 0) - 120000;
      const mine = runs.filter(x => Date.parse(x.created_at || 0) >= since);
      if (mine.length) runState[p.id] = mine[0];
      else if (!runState[p.id]) runState[p.id] = { message: r.data.message || '' };
    }
    draw();
  }

  el.addEventListener('click', async e => {
    const d = e.target.closest('button[data-dismiss]');
    if (d) { pendingWrite(pendingRead().filter(p => p.id !== d.dataset.dismiss)); draw(); return; }
    const t = e.target.closest('button[data-retry]');
    if (!t) return;
    const p = pendingRead().filter(x => x.id === t.dataset.retry)[0];
    // The request is stored on the record precisely so a failed run can be re-dispatched exactly as
    // it was first sent — same definition, same sources, same discovery setting. A record written
    // before this existed has no request to replay, and says so rather than sending a guess.
    if (!p || !p.request) { say('This run was started before the page could remember its definition, so it cannot be repeated automatically. Press Create scan or Run scan again.', 'warn'); return; }
    t.disabled = true; t.textContent = 'Starting…';
    const res = await dispatchScan(p.request, p.verb || 'run this again');
    t.disabled = false; t.textContent = 'Try again';
    if (!res) return;
    delete runState[p.id];
    addPending(Object.assign({}, p, { dispatched_at: new Date().toISOString(),
      actionsUrl: res.actionsUrl || p.actionsUrl || D.actionsUrl || '' }));
  });

  draw();
}

// ---- chip input ----------------------------------------------------------------------------
function chipInput(root, opts) {
  opts = opts || {};
  let values = [];
  const listId = root.id + '-list';
  root.innerHTML = '<input type="text" id="' + esc(opts.inputId || root.id + '-in') + '" list="' + esc(listId) + '" placeholder="' + esc(opts.placeholder || '') + '" autocomplete="off">'
    + '<datalist id="' + esc(listId) + '">' + (opts.suggest || []).map(s => '<option value="' + esc(s) + '"></option>').join('') + '</datalist>';
  const input = $('input', root);
  const norm = opts.normalize || (s => s.trim());
  const draw = () => {
    $$('.chip', root).forEach(c => c.remove());
    values.slice().reverse().forEach((v, i) => {
      const idx = values.length - 1 - i;
      const c = document.createElement('span'); c.className = 'chip t';
      c.innerHTML = (opts.render ? opts.render(v) : esc(v)) + '<button type="button" aria-label="Remove ' + esc(v) + '" data-i="' + idx + '">×</button>';
      root.insertBefore(c, root.firstChild);
    });
    // draw() runs on every add, every removal and every programmatic set, so this one hook is
    // every change the coverage preview needs to hear about: a preview built from stale chips
    // would promise venues and jurisdictions the create no longer sends.
    if (opts.onchange) opts.onchange();
  };
  const add = raw => { const v = norm(raw); if (!v) return; if (opts.validate && !opts.validate(v)) { input.setCustomValidity('x'); input.reportValidity(); return; } if (!values.includes(v)) values.push(v); input.value = ''; draw(); };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(input.value); }
    else if (e.key === 'Backspace' && !input.value && values.length) { values.pop(); draw(); }
  });
  input.addEventListener('change', () => { if (input.value && (opts.suggest || []).includes(input.value)) add(input.value); });
  input.addEventListener('blur', () => { if (input.value.trim()) add(input.value); });
  root.addEventListener('click', e => { const b = e.target.closest('button[data-i]'); if (b) { values.splice(+b.dataset.i, 1); draw(); input.focus(); } else if (e.target === root) input.focus(); });
  return { get: () => values.slice(), set: v => { values = (v || []).map(norm).filter(Boolean); draw(); }, input };
}
const isUrl = s => /^https?:\/\/[^\s]+$/i.test(s);
const jurNorm = s => { s = s.trim(); if (!s) return ''; const c = COUNTRIES[s.toLowerCase()]; if (c) return c; return /^[a-z]{2}$/i.test(s) ? s.toUpperCase() : s; };
// A client in a definition is a string or {name, scope} (contract). Reviewed defect: the raw list
// was fed to the chip input (`s.trim is not a function` — Edit threw on the shipped demo), posted to
// Ask/Draft (400 'client must be a string') and printed in the template as '[object Object]'.
// Every consumer goes through this one helper.
const clientName = c => typeof c === 'string' ? c.trim() : (c && typeof c === 'object' && typeof c.name === 'string') ? c.name.trim() : '';
const TRAILER = '— DRAFT for partner review. Verify against the official text before sending. Not sent.';
const DEMO_NOTE = 'Demo scan: fixture data, not a real instrument.';

// ---- create / edit dialog -------------------------------------------------------------------
const dlg = $('#dlg'), form = $('#dlg-form');
// The coverage preview reads the jurisdictions and the typed sources, so both re-draw it.
const onCoverageInput = () => { if (typeof refreshPreview === 'function') refreshPreview(); };
const F = {
  jur: chipInput($('#c-jur'), { inputId: 'f-jur', placeholder: 'Germany, FR, EU…', normalize: jurNorm, onchange: onCoverageInput, render: v => flagged(v) + (NAMES[v] ? ' <span class="mark d">' + esc(countryName(v)) + '</span>' : '') }),
  top: chipInput($('#c-top'), { inputId: 'f-top', placeholder: 'Pay equity, Employment…' }),
  ind: chipInput($('#c-ind'), { inputId: 'f-ind', placeholder: 'Professional services…' }),
  src: chipInput($('#c-src'), { inputId: 'f-src', placeholder: 'https://…', validate: isUrl, onchange: onCoverageInput, render: v => esc(v.replace(/^https?:\/\/(www\.)?/, '').slice(0, 60)) }),
  cl: chipInput($('#c-cl'), { inputId: 'f-cl', placeholder: 'Client name', suggest: D.clientNames || [] }),
};
let editingId = null;
// The definition Edit read back, so Save can carry forward the keys this dialog does not edit.
let editingDefn = null;
// {name, scope} clients keep their scope across an edit: the chip shows the name, the object is
// re-attached on submit so saving a scan does not silently drop "employees in Germany only".
let clientObjs = {};
// The same discipline for sources. A source in a definition carries the venue's name, jurisdiction,
// kind and the rationale discovery gave for it; the chip input can only show a URL. Re-attach the
// descriptive fields on submit so an Edit does not quietly strip a venue back to a bare URL.
// The gate's own verdict (status, tier, gate evidence) is deliberately NOT sent back: the gate
// decides that again on every create, and echoing a stale "approved" would be us deciding for it.
let srcObjs = {};
// Candidates from /api/discover, and the partner's ticks. Proposals only: nothing here has been
// fetched, and every ticked URL still goes through the Python gate when the scan is created.
let cands = [], picked = {}, discTouched = false;
const DISC_HELP = 'Off, only the sources listed above are gated and read.';
function openDialog(scan) {
  editingDefn = scan || null;
  editingId = scan ? scan.id : null;
  clientObjs = {};
  (scan ? scan.clients : []).forEach(c => { const n = clientName(c); if (n && typeof c === 'object') clientObjs[n] = c; });
  $('#dlg-title').textContent = scan ? 'Edit scan' : 'Create scan';
  $('#dlg-submit').textContent = scan ? 'Save and re-run' : 'Create scan';
  $('#f-name').value = scan ? scan.name : '';
  $('#f-intent').value = scan ? scan.intent : '';
  F.jur.set(scan ? scan.jurisdictions : []); F.top.set(scan ? scan.topics : []); F.ind.set(scan ? scan.industries : []);
  srcObjs = {};
  (scan ? scan.sources : []).forEach(s => { if (s && typeof s === 'object' && s.url) srcObjs[s.url] = s; });
  F.src.set(scan ? scan.sources.map(s => typeof s === 'string' ? s : (s && s.url) || '').filter(Boolean) : []);
  F.cl.set(scan ? scan.clients.map(clientName).filter(Boolean) : []);
  cands = []; picked = {}; discTouched = false;
  $('#cands').innerHTML = ''; setNote('#find-note', ''); $('#f-disc-help').textContent = DISC_HELP;
  $('#f-disc').checked = scan ? !scan.no_discover : true;
  const sch = scan && scan.schedule;
  $('#f-sched').checked = !!(sch && sch.daily_at);
  $('#f-sched-at').value = (sch && sch.daily_at) || '06:30';
  // The subject filter round-trips like no_misc and budget: an Edit that opens, changes a topic
  // and saves must re-submit the very filter the scan already has. A definition that has never
  // had one opens empty and says, in the block itself, what creating it that way means.
  const sf = (scan && scan.subject_filter && typeof scan.subject_filter === 'object') ? scan.subject_filter : {};
  subject = { regex: String(sf.regex || '').trim(), why: String(sf.why || '').trim(),
              source: ['proposed', 'partner', 'none'].includes(sf.source) ? sf.source : (sf.regex ? 'proposed' : 'none') };
  if (!subject.regex) subject.source = 'none';
  // An Edit is not a fresh proposal: the sentence beside the box belongs to the regex in it until
  // the model is asked again, so the "you have changed it" caveat starts silent.
  proposedRegex = subject.source === 'proposed' ? subject.regex : '';
  proposedWhy = subject.source === 'proposed' ? subject.why : '';
  $('#f-subject').value = subject.regex;
  setNote('#subject-note', '');
  // Every dialog opening starts the coverage gate again: what the last scan was going to cover
  // says nothing about this one, and a Create left enabled from a previous open would be exactly
  // the "created blind" outcome this preview exists to stop.
  previewSeen = true; renderPreview();
  $('#dlg-err').textContent = '';
  setNote('#dlg-pnote', ''); setNote('#dlg-pnote-1', '');
  $('#f-desc').value = '';
  // Two paths, both from Harvey's launch material (design §2): describe it in one box and let
  // the model propose the structure, or fill the structured form directly. An edit of an existing
  // scan has nothing to describe, so it opens straight on the form.
  setStep(scan ? 'form' : 'describe');
  findWhy();          // the describe step too: it is the state the form will open in
  dlg.showModal();
  (scan ? $('#f-name') : $('#f-desc')).focus();
}
function setStep(step) {
  dlg.dataset.step = step;
  $('#dlg-back').hidden = step !== 'form' || !!editingId;
  // Every route into the form can have filled the intent without an input event (Create manually
  // carries the description over; Build the scan writes the proposal, or the description on its
  // fallback). Re-read it here so the Find sources button is never disabled beside a full intent.
  if (step === 'form') findWhy();
}
function setNote(sel, html, warn) { const el = $(sel); el.className = 'pnote' + (html ? ' on' : '') + (warn ? ' warn' : ''); el.innerHTML = html || ''; }
$('#dlg-manual').addEventListener('click', () => {
  // A description typed before choosing the manual path is the intent in the partner's own words;
  // carrying it over saves retyping and loses nothing.
  const desc = $('#f-desc').value.trim();
  if (desc && !$('#f-intent').value.trim()) $('#f-intent').value = desc;
  setStep('form'); $('#f-name').focus();
});
$('#dlg-back').addEventListener('click', () => { setStep('describe'); $('#f-desc').focus(); });

// ---- source picker: discovery moved out of the workflow and into the browser ------------------
// Discovery used to happen invisibly inside the run, so the partner never saw or chose the venues
// and waited five minutes while the workflow gated up to 25 model-proposed candidates. Here they
// see the evidence and pick, in about twenty seconds, and the run gates only what they ticked.
// The endpoint proposes; it fetches nothing and decides nothing.
function findWhy() {
  const intent = $('#f-intent').value.trim(), b = $('#dlg-find');
  const short = intent.length < 20;
  b.disabled = short;
  // A disabled button with no reason beside it is a dead end; say what is missing and how far off.
  $('#find-why').textContent = short
    ? 'Write the intent first — discovery reads that sentence, and it needs at least 20 characters (' + intent.length + ' so far).'
    : 'Proposes official venues for this brief. Nothing is fetched, and nothing is created.';
}
$('#f-intent').addEventListener('input', findWhy);
function discHelp() {
  const n = Object.keys(picked).length, disc = $('#f-disc');
  // The same de-duplicated list the submit handler sends and the preview shows. Defect the preview
  // exposed: a URL that was both ticked in the picker and typed into Sources was counted twice, so
  // the counter could read "13 of 12 — too many" over twelve venues.
  const total = previewSources().length;
  $('#find-count').textContent = total ? total + ' of ' + MAX_SOURCES + ' source' + (MAX_SOURCES === 1 ? '' : 's') + ' chosen' + (total > MAX_SOURCES ? ' — too many' : '') : '';
  $('#find-count').classList.toggle('over', total > MAX_SOURCES);
  $('#f-disc-help').textContent = (!disc.checked && n)
    ? 'Off — the workflow gates exactly the ' + pl(n, 'source') + ' you picked instead of proposing 25 of its own, which is about five minutes less before the scan appears. Tick it back on to have it look for more as well.'
    : DISC_HELP;
}
$('#f-disc').addEventListener('change', () => { discTouched = true; discHelp(); refreshPreview(); });

// ---- the subject filter: what counts as this scan's subject, BEFORE it exists ----------------
// The other half of the coverage question, built with the partner in the same block. A REGEX, not
// a model call per row: deterministic, visible here, editable here, free to apply, and the same
// instrument engine/registry_v2.json has given every TMT India source all along. The model
// proposes it once; whatever is in this box when Create is pressed is what the run applies.
let subject = { regex: '', why: '', source: 'none' };
// What the model last proposed, so an edited box can say the sentence beside it was written for
// something else. Attributing the partner's regex to the model would be a small lie in exactly the
// place this whole feature exists to be honest about.
let proposedRegex = '', proposedWhy = '';
// Python's re accepts a leading (?i); JavaScript's RegExp throws on it. The run is the authority
// on the pattern, so the page strips that one prefix before testing rather than calling a regex
// the pipeline would happily compile "invalid".
function compileSubject(rx) {
  const s = String(rx || '').trim();
  if (!s) return null;
  try { return new RegExp(s.replace(/^\(\?i\)/, ''), 'i'); } catch (e) { return null; }
}
// Split a pattern on its TOP-LEVEL alternation, ignoring | inside groups, character classes and
// escapes. Used only to read the filter back in English — never to decide anything.
function splitAlts(rx) {
  const out = []; let cur = '', depth = 0, cls = false;
  for (let i = 0; i < rx.length; i++) {
    const c = rx[i];
    if (c === '\\') { cur += c + (rx[i + 1] || ''); i++; continue; }
    if (cls) { cur += c; if (c === ']') cls = false; continue; }
    if (c === '[') { cls = true; cur += c; continue; }
    if (c === '(') { depth++; cur += c; continue; }
    if (c === ')') { depth = Math.max(0, depth - 1); cur += c; continue; }
    if (c === '|' && depth === 0) { out.push(cur); cur = ''; continue; }
    cur += c;
  }
  out.push(cur);
  return out;
}
function balanced(s) {
  let depth = 0, cls = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === '\\') { i++; continue; }
    if (cls) { if (c === ']') cls = false; continue; }
    if (c === '[') { cls = true; continue; }
    if (c === '(') depth++;
    if (c === ')') { depth--; if (depth < 0) return false; }
  }
  return depth === 0;
}
function subjectAlts(rx) {
  let s = String(rx || '').trim().replace(/^\(\?i\)/, '');
  for (let n = 0; n < 6; n++) {
    const before = s;
    s = s.replace(/^\^/, '').replace(/\$$/, '').replace(/^\\b/, '').replace(/\\b$/, '').trim();
    const m = s.match(/^\((\?:|\?i:)?([\s\S]*)\)$/);
    if (m && balanced(m[2])) s = m[2];
    if (s === before) break;
  }
  return splitAlts(s).map(a => a.trim()).filter(Boolean);
}
// An alternative is "plain" when it reads as the words a partner typed, once \b, \s and escaped
// punctuation are undone. Anything else is shown AS WRITTEN and counted, because a readback that
// quietly prettifies a pattern it does not understand is worse than no readback.
function plainWord(alt) {
  let w = String(alt).replace(/\\b/g, '').replace(/\\s[+*]?/g, ' ').replace(/\\([.\-\/&'+])/g, '$1').trim();
  return /^[A-Za-z0-9][A-Za-z0-9 .,&'’\/+-]*$/.test(w) ? w : null;
}
function subjectReadback(rx) {
  const alts = subjectAlts(rx), plain = [], raw = [];
  alts.forEach(a => { const w = plainWord(a); if (w) plain.push(w); else raw.push(a); });
  return { plain: plain, raw: raw, total: alts.length };
}
function setSubjectFromBox() {
  const v = $('#f-subject').value;
  subject.regex = v.trim();
  // Typing in the box makes the filter the partner's. Clearing it by hand is "read everything"
  // said the long way, and must record itself as such, not sit as an empty "proposed" filter.
  subject.source = subject.regex ? 'partner' : 'none';
  renderSubject();
}
function renderSubject() {
  const box = $('#f-subject'), rx = subject.regex, re = compileSubject(rx);
  if (box.value.trim() !== rx) box.value = rx;
  box.classList.toggle('bad', !!rx && !re);
  const on = !!rx && subject.source !== 'none';
  const who = subject.source === 'partner' ? 'yours' : subject.source === 'proposed' ? 'proposed by the model' : '';
  const edited = subject.source === 'partner' && proposedRegex && rx !== proposedRegex;
  $('#subject-why').innerHTML = !on ? ''
    : '<span class="who">' + esc(who) + '</span>'
      + (subject.why ? esc(subject.why) : 'No sentence was written for this filter — say in one line what it is meant to admit, so a partner can check it later.')
      + (edited ? ' <i>(that sentence was written for the pattern the model proposed; you have changed it.)</i>' : '');
  if (!on) {
    $('#subject-plain').innerHTML = '';
    $('#subject-illus').innerHTML = '<div class="sopen"><b>Read everything: this scan will have no subject filter.</b> '
      + 'Every row every source on the list below publishes is ledgered — for a busy regulator that is thousands of '
      + 'items that are not your subject, and the first run\'s whole reading budget is spent on whatever the venue '
      + 'happens to list first. The scan\'s Coverage and Audit tabs will say so in as many words.</div>';
    syncSubmit();
    return;
  }
  if (!re) {
    $('#subject-plain').innerHTML = '<div class="sbad">This is not a valid regular expression, so nothing can be read back from it '
      + 'and the run would refuse it. Fix it, or press <b>Read everything</b> to create the scan without a filter.</div>';
    $('#subject-illus').innerHTML = '';
    syncSubmit();
    return;
  }
  const rb = subjectReadback(rx);
  $('#subject-plain').innerHTML = (rb.plain.length
      ? '<b>Rows whose title mentions:</b> ' + rb.plain.slice(0, 24).map(w => '<span class="term">' + esc(w) + '</span>').join('')
        + (rb.plain.length > 24 ? ' and ' + (rb.plain.length - 24) + ' more' : '')
      : '<b>This pattern has no plain words to read back.</b>')
    + (rb.raw.length ? '<br>' + esc(pl(rb.raw.length, 'part')) + ' of it ' + (rb.raw.length === 1 ? 'is' : 'are')
        + ' not plain words and ' + (rb.raw.length === 1 ? 'is' : 'are') + ' shown as written: '
        + rb.raw.slice(0, 6).map(w => '<span class="term">' + esc(w) + '</span>').join('') : '')
    + '<br>Matched case-insensitively against <b>each row\'s title</b>, as the listing prints it, before anything is fetched or read. '
    + 'A row whose title is <b>too terse to judge</b> — a bare number, a docket reference — is <b>kept</b> and marked, never dropped: '
    + 'the tracker probes the document in that case, a scan cannot cheaply, and erring toward keeping is the only safe direction.';
  // WHAT IT WOULD DO, before the scan exists — but only what can honestly be shown. The filter is
  // applied per ROW at run time and there are no rows yet, so this counts the venues whose own name
  // or kind the filter's words plainly admit and says, in the same breath, that this is not the test.
  const list = previewSources();
  if (!list.length) {
    $('#subject-illus').innerHTML = '<div class="sillus">No venue is on the list yet, so there is nothing to illustrate this against. '
      + 'The filter is applied to each row\'s title at run time, not to the venue.</div>';
  } else {
    const hit = list.filter(s => re.test((s.name || '') + ' ' + (KINDL[s.kind] || '') + ' ' + (s.host || '')));
    $('#subject-illus').innerHTML = '<div class="sillus"><b>Illustration, not a promise.</b> Of the ' + esc(pl(list.length, 'venue'))
      + ' listed below, the filter\'s own words appear in the name or kind of <b>' + hit.length + '</b>'
      + (hit.length ? ': ' + hit.slice(0, 6).map(s => esc(s.name)).join(', ') + (hit.length > 6 ? ', and ' + (hit.length - 6) + ' more' : '') : '')
      + '. That is not the test. The test is <b>per row</b>, at run time, on the row\'s title: a venue whose name says nothing '
      + 'about your subject still contributes every row whose title matches, and a venue whose name matches contributes only '
      + 'the rows whose titles do.</div>';
  }
  syncSubmit();
}
$('#f-subject').addEventListener('input', setSubjectFromBox);
$('#subject-clear').addEventListener('click', () => {
  subject = { regex: '', why: '', source: 'none' };
  $('#f-subject').value = '';
  setNote('#subject-note', '');
  renderSubject();
  $('#f-subject').focus();
});
$('#dlg-subject').addEventListener('click', async () => {
  const intent = $('#f-intent').value.trim();
  if (intent.length < 20) {
    setNote('#subject-note', 'Write the intent first — the filter is proposed from that sentence and the topics beside it.', true);
    $('#f-intent').focus(); return;
  }
  const b = $('#dlg-subject'); b.disabled = true; b.textContent = 'Proposing…';
  setNote('#subject-note', '');
  let r = null, err = null;
  try { r = await postJSON(D.api.subject, { intent: intent, topics: F.top.get() }, 40000); } catch (e) { err = e; }
  b.disabled = false; b.textContent = 'Propose from the brief';
  if (r && r.ok && r.data && typeof r.data.regex === 'string' && r.data.regex.trim()) {
    proposedRegex = r.data.regex.trim(); proposedWhy = String(r.data.why || '').trim();
    subject = { regex: proposedRegex, why: proposedWhy, source: 'proposed' };
    renderSubject();
    setNote('#subject-note', compileSubject(subject.regex)
      ? '<b>Proposed from your brief.</b> Read it, edit it, or clear it — whatever is in the box when you press Create is what every run applies.'
      : '<b>The proposed pattern does not compile.</b> Edit it or press Read everything.', !compileSubject(subject.regex));
    return;
  }
  // No endpoint, no proposal: the box still works, and typing in it is the whole feature.
  const why = (err && err.name === 'AbortError') ? 'the request took longer than 40 seconds'
    : err ? 'no subject endpoint is reachable from this page'
    : (r && (r.status === 501 || r.status === 404)) ? 'the subject proposer is not configured on this deployment (HTTP ' + r.status + ')'
    : (r && !r.ok) ? 'the endpoint answered ' + r.status + (r.data && r.data.message ? ': ' + r.data.message : '')
    : 'the endpoint returned no pattern';
  setNote('#subject-note', 'Could not propose a filter — ' + esc(why) + '. Type one yourself: it is matched against each row\'s '
    + 'title, so the words your subject is called by are usually enough — <span class="term">\\b(AI|artificial intelligence|machine learning)\\b</span>. '
    + 'Or press <b>Read everything</b> and accept that every row every venue publishes is ledgered.', true);
});

// These three are top level on purpose. renderScan() builds its whole shell in one pass, and
// coverageHTML() runs inside that pass — a const declared further down its body is still in the
// temporal dead zone when it is read, which blanked the page. They depend on nothing but D.

// ---- the subject filter, shared by Coverage and Audit ---------------------------------------
// A scan's ledger is decided by two things: which venues it reads, and which rows on those
// venues count as its subject. Coverage has always shown the first. This shows the second, in
// both places a partner goes looking — Coverage, because a filter IS coverage, and Audit,
// because someone auditing for a miss has to know a filter stood between the venue and the
// ledger. When there is no filter, both say so in as many words rather than staying silent,
// since silence there reads as "nothing was dropped" when the truth is "nothing was judged".
const SUBJ = (D.coverage && D.coverage.subject) || { on: false, regex: '', why: '', source: 'none', valid: true };
function subjectCount(sub, on) {
  // Per source. Absent is NOT zero: a run that predates the filter recorded nothing, and
  // rendering that as "0 dropped" would claim the filter looked.
  if (!sub) return on ? '<div class="ev subj">subject filter: no counts recorded for this source on the last run</div>' : '';
  const d = sub.dropped, k = sub.kept_terse;
  return '<div class="ev subj">subject filter: <b>' + esc(d == null ? '—' : d) + '</b> row(s) dropped as not this scan\'s subject'
    + '<span class="sep">·</span><b>' + esc(k == null ? '—' : k) + '</b> row(s) kept because the title was too terse to judge'
    + (sub.titles && sub.titles.length ? '<span class="sep">·</span>kept: ' + sub.titles.map(esc).join('; ') : '') + '</div>';
}
function subjectPanelHTML() {
  if (!SUBJ.on) {
    return '<div class="subjbox off"><p><b>This scan has no subject filter, so every row every source publishes is ledgered.</b> '
      + 'Nothing stands between the venues below and this scan\'s ledger: whatever a venue lists in the window enters it, '
      + 'and the reading budget is spent in whatever order the venue happens to publish. For a busy regulator that is '
      + 'thousands of rows that are not this scan\'s subject. Press <b>Edit</b> and give it one — a pattern matched against '
      + 'each row\'s title, before anything is fetched.</p>'
      + (SUBJ.regex ? '<p class="broken">A pattern is stored on this scan but it is switched off (source &ldquo;none&rdquo;): '
          + '<span class="term">' + esc(SUBJ.regex) + '</span></p>' : '') + '</div>';
  }
  const rb = subjectReadback(SUBJ.regex), ok = !!compileSubject(SUBJ.regex) && SUBJ.valid !== false;
  const who = SUBJ.source === 'partner' ? 'set by the partner' : 'proposed by the model, accepted unedited';
  const counted = SUBJ.sources_counted || 0, run = SUBJ.sources_run || 0;
  return '<div class="subjbox"><div class="rxline">' + esc(SUBJ.regex) + '</div>'
    + '<p><span class="who">' + esc(who) + '</span>' + (SUBJ.why ? esc(SUBJ.why) : 'No sentence was recorded for this filter.') + '</p>'
    + (ok ? '<p>' + (rb.plain.length
          ? '<b>Rows whose title mentions:</b> ' + rb.plain.slice(0, 24).map(w => '<span class="term">' + esc(w) + '</span>').join('')
            + (rb.plain.length > 24 ? ' and ' + (rb.plain.length - 24) + ' more' : '')
          : '<b>This pattern has no plain words to read back.</b>')
        + (rb.raw.length ? ' &mdash; ' + esc(pl(rb.raw.length, 'part')) + ' shown as written: '
            + rb.raw.slice(0, 6).map(w => '<span class="term">' + esc(w) + '</span>').join('') : '')
        + '</p>'
      : '<p class="broken"><b>This pattern does not compile</b>, so the run cannot have applied it. '
        + 'Treat this scan\'s ledger as unfiltered until it is fixed in <b>Edit</b>.</p>')
    + '<p>Matched case-insensitively against <b>each row\'s title</b> at extraction, before anything is fetched, read or enriched &mdash; '
    + 'so the reading budget goes to the subject. A row whose title is <b>too terse to judge</b> is <b>kept</b> and counted, never dropped: '
    + 'erring toward keeping is the only safe direction for a tracker whose promise is that it does not miss things.</p>'
    + '<div class="nums"><span><b>' + esc(SUBJ.dropped == null ? '—' : SUBJ.dropped) + '</b>rows dropped as not the subject</span>'
    + '<span><b>' + esc(SUBJ.kept_terse == null ? '—' : SUBJ.kept_terse) + '</b>kept, title too terse to judge</span>'
    + '<span>' + (counted ? esc(counted) + ' of ' + esc(run) + ' source(s) recorded counts' : 'no source recorded counts on the last run') + '</span></div>'
    + '</div>';
}

// ---- the coverage preview: what this scan will read, BEFORE it exists -------------------------
// Coverage is the whole product — "a scan shows exactly which URLs it fetches" (design §1.1) — so
// a partner should see the venues before pressing Create, not discover them on the coverage panel
// twenty minutes later. Create stays disabled until this has been drawn from the current inputs.
// The old gate ("Create is disabled until you have pressed Show coverage") existed because the
// list was hidden behind a button. It is now always on screen and always current, so there is
// nothing left to make someone reveal — the line below just says what the list is.
let previewSeen = true;
let lastGaps = [], lastDropped = [];   // the last Find sources answer, for redraws
function syncSubmit() {
  const b = $('#dlg-submit'); if (!b) return;
  // The one thing that can hold Create back: a subject filter that does not compile. The run would
  // refuse it, and a scan created around a refused filter is a scan reading everything while its
  // page claims a subject.
  const broken = !!subject.regex && subject.source !== 'none' && !compileSubject(subject.regex);
  b.disabled = broken;
  b.title = broken ? 'The subject filter is not a valid regular expression.' : '';
  const why = $('#preview-why');
  if (why) why.textContent = 'Coverage is the whole product, so it is built here with you — tick a venue, '
    + 'add one, or search a gap, and this list follows. Everything on it is gated when the scan is created.';
}
// Exactly the list the submit handler will send, built by the same rule, so the preview can never
// promise a venue the dispatch drops or hide one it adds.
function previewSources() {
  const chosen = cands.filter(c => picked[c.url]);
  const chosenUrls = chosen.map(c => c.url);
  // Who put a venue on the list is part of what the partner is being asked to approve. An Edit
  // re-opens venues the definition already carries, and `proposed_by` on those is the truth —
  // calling a discovered venue "added by you" would misattribute the choice back to the reader.
  const from = (o, url, fallback) => ({ url: url, name: o.name || hostOf(url) || url, host: o.host || hostOf(url),
    jurisdiction: (o.jurisdiction || '').toUpperCase(), kind: o.kind || '', rationale: o.rationale || '',
    how: o.proposed_by ? 'proposed by ' + o.proposed_by : fallback });
  return chosen.map(c => from(c, c.url, 'proposed by discovery'))
    .concat(F.src.get().filter(u => !chosenUrls.includes(u)).map(u => from(srcObjs[u] || {}, u, 'added by you')));
}
function renderPreview() {
  const list = previewSources(), el = $('#preview');
  const order = [], groups = {};
  list.forEach(s => { const k = s.jurisdiction || '—'; if (!groups[k]) { groups[k] = []; order.push(k); } groups[k].push(s); });
  // The scan's own jurisdictions lead, in the order they were typed, so a jurisdiction with no
  // venue is read in place rather than found by its absence at the bottom of a list.
  const jurs = F.jur.get();
  const heads = jurs.filter(j => groups[j]).concat(order.filter(k => k !== '—' && !jurs.includes(k)), groups['—'] ? ['—'] : []);
  const venues = heads.map(k => '<div class="pj"><div class="pjh">' + (k === '—' ? 'Jurisdiction not stated' : flagged(k) + (NAMES[k] ? ' ' + esc(countryName(k)) : ''))
      + '<span class="ph">' + pl(groups[k].length, 'venue') + '</span></div><ul>'
    + groups[k].map(s => '<li><button type="button" class="pdrop" data-drop="' + esc(s.url) + '" title="Take this venue off the list" aria-label="Remove ' + esc(s.name) + '">&times;</button><span class="pn">' + esc(s.name) + '</span><span class="ph">' + esc(KINDL[s.kind] || 'kind not classified') + ' · ' + esc(s.host || s.url) + ' · ' + esc(s.how) + '</span>'
        + (s.rationale ? '<div class="pr">' + esc(String(s.rationale).slice(0, 300)) + '</div>' : '<div class="pr">No rationale was given for this venue.</div>') + '</li>').join('')
    + '</ul></div>').join('');
  // A venue that did not say which jurisdiction it serves makes a gap unprovable — the same rule
  // the built coverage panel uses, so the two never contradict each other about the same scan.
  const unstated = list.filter(s => !s.jurisdiction).length;
  const gaps = jurs.filter(j => !groups[j]);
  const gapLine = !jurs.length ? '<div class="pgap">No jurisdiction is listed yet — the pipeline refuses a scan without one.</div>'
    : unstated ? '<div class="pgap">' + esc(pl(unstated, 'venue')) + ' did not say which jurisdiction it serves, so this list cannot tell you which jurisdictions are uncovered. The scan\'s Coverage panel will, once the gate has read them.</div>'
    : gaps.length ? '<div class="pgap">No venue for ' + gaps.map(j => flagged(j)
          + ' <button type="button" class="pfind" data-findjur="' + esc(j) + '">Search ' + esc(j) + '</button>').join(', ') + '. '
        + ($('#f-disc').checked ? 'Discovery is on, so the workflow will look for one; if it finds none, the Coverage panel says so and keeps saying so.' : 'Discovery is off, so nothing will be read for ' + (gaps.length === 1 ? 'it' : 'them') + '. Add a listing page, or turn discovery back on.') + '</div>'
    : '';
  const named = heads.filter(k => k !== '—').length;
  el.innerHTML = (list.length
      ? '<h4>' + esc(pl(list.length, 'venue')) + (named ? ' across ' + esc(pl(named, 'jurisdiction')) : ', none stating a jurisdiction') + '</h4>' + venues
      : '<h4>No venue chosen yet</h4><div class="pgap">Nothing is listed, so this scan would read nothing of its own.'
        + ($('#f-disc').checked ? ' Discovery is on, so the workflow will propose venues and gate them; you will first see them on the scan\'s Coverage panel.' : ' Discovery is off too — press Find sources, or add a listing page.') + '</div>')
    + gapLine
    + '<div class="pfine"><b>Each venue above is gated when the scan is created</b> — reachability, robots.txt, the site\'s own terms and a parse test. Anything that fails is listed as <b>rejected</b> on the scan\'s Coverage panel, with the reason, and is never fetched. Nothing here is coverage until the gate has said so, and a scan reads at most ' + MAX_SOURCES + ' sources.'
    + ($('#f-disc').checked ? '<br>Discovery is on, so the workflow will also propose venues of its own and gate them the same way. Those are not on this list.' : '')
    + '<br><b>Miscellaneous will additionally search the open web outside this list.</b> That lane fetches nothing, gates nothing and cites nothing — it is leads to verify at their primary source. A lead that turns out to be an official venue can be promoted into this coverage list, where the same gate decides.</div>';
  el.hidden = false;
  // The subject illustration counts the venues on this list, so it redraws whenever the list does.
  renderSubject();
  syncSubmit();
}
// Re-draw only once it has been shown: opening the dialog must not silently satisfy its own gate.
// The coverage list is interactive: a venue can be taken off it, and a named gap can be searched
// on its own. Both act on the very state previewSources() reads, so what the list shows and what
// the dispatch sends cannot drift apart.
document.addEventListener('click', (e) => {
  const drop = e.target.closest('[data-drop]');
  if (drop) {
    const url = drop.getAttribute('data-drop');
    delete picked[url];                                   // if it came from Find sources
    F.src.set(F.src.get().filter(u => u !== url));        // if it was typed in
    drawCands(lastGaps, lastDropped); discHelp(); refreshPreview();
    return;
  }
  const find = e.target.closest('[data-findjur]');
  if (find) { findSources(find.getAttribute('data-findjur')); }
});

// Always current, never requested. The coverage list IS the create dialog's subject, so it
// redraws on every change rather than waiting behind a "show me" button.
function refreshPreview() { renderPreview(); }

function candRow(c, i) {
  const host = c.host || hostOf(c.url);
  const kind = KINDL[c.kind] || host || 'Source';
  const conf = ['high', 'medium', 'low'].includes(c.confidence) ? c.confidence : '';
  return '<li><label class="cand"><input type="checkbox" data-url="' + esc(c.url) + '"' + (picked[c.url] ? ' checked' : '') + '>'
    + '<span><span class="ctop"><span class="nm">' + esc(c.name || host || c.url) + '</span>'
    + '<span class="kind">' + esc(kind) + '</span>'
    + (c.jurisdiction ? '<span class="jur">' + flagged(c.jurisdiction) + '</span>' : '')
    + (conf ? '<span class="conf c-' + conf + '">' + conf + ' confidence</span>' : '<span class="conf">confidence not given</span>')
    + '</span>'
    + '<span class="chost">' + esc(host || c.url) + '</span>'
    + (c.rationale ? '<span class="crat">' + esc(String(c.rationale).slice(0, 300)) + '</span>' : '')
    + '</span></label></li>';
}
// gaps and dropped candidates are rendered, never swallowed: a jurisdiction discovery found no
// venue for, and a candidate it deny-listed or de-duplicated, are both facts about coverage.
function drawCands(gaps, dropped) {
  lastGaps = gaps || []; lastDropped = dropped || [];   // so a removal can redraw the same view
  const rows = cands.map(candRow).join('');
  $('#cands').innerHTML = (rows ? '<ul class="candlist">' + rows + '</ul>' : '')
    + (gaps || []).map(g => '<div class="gapline"><span class="t">gap</span>'
        + esc(g && g.note ? g.note : ('No official venue found for ' + ((g && g.jurisdiction) || 'one jurisdiction') + ' — add one by hand if you know it')) + '</div>').join('')
    + (dropped || []).map(t => '<div class="dropline"><span class="t">dropped</span>' + esc(t) + '</div>').join('');
}
$('#cands').addEventListener('change', e => {
  const cb = e.target.closest('input[type=checkbox][data-url]');
  if (!cb) return;
  if (cb.checked) picked[cb.dataset.url] = true; else delete picked[cb.dataset.url];
  // Picking venues here is the point: the create then dispatches no_discover:true and the workflow
  // gates the handful you chose. Turned off for you, once, and said out loud — unless you have
  // already set the box yourself, in which case your setting stands.
  if (Object.keys(picked).length && $('#f-disc').checked && !discTouched) $('#f-disc').checked = false;
  discHelp();
  refreshPreview();
});
$('#dlg-find').addEventListener('click', () => findSources(null));
// `only` searches ONE jurisdiction — the button beside a named gap on the coverage list, so a
// partner fills a hole without re-running every other search. null searches all of them.
async function findSources(only) {
  const intent = $('#f-intent').value.trim();
  if (intent.length < 20) { findWhy(); $('#f-intent').focus(); return; }
  const b = $('#dlg-find'); b.disabled = true;
  setNote('#find-note', '');
  // ONE JURISDICTION PER CALL. A hosted web search across four jurisdictions took longer than the
  // platform's 60 s function ceiling and answered 504 every time; /api/discover now refuses more
  // than one, and the fix is smaller searches run at the same time rather than a longer wait.
  // 55 s each: outside the endpoint's own 50 s model deadline, inside its 60 s budget.
  const jurs = only ? [only] : F.jur.get();
  const calls = jurs.length ? jurs : [null];
  let done = 0;
  const tick = () => { b.textContent = calls.length > 1 ? 'Looking… ' + done + '/' + calls.length : 'Looking…'; };
  tick();
  const results = await Promise.all(calls.map(j => postJSON(D.api.discover,
      Object.assign({ intent, topics: F.top.get(), industries: F.ind.get() }, j ? { jurisdiction: j } : {}), 55000)
    .then(x => ({ j: j, r: x }), e => ({ j: j, err: e }))
    .then(x => { done += 1; tick(); return x; })));
  b.disabled = false; b.textContent = 'Find sources'; findWhy();

  // Merge the answers. A jurisdiction whose own call failed is named rather than silently missing,
  // because an empty list and an unanswered search are not the same thing.
  const okCalls = results.filter(x => x.r && x.r.ok && Array.isArray(x.r.data.candidates));
  const failed = results.filter(x => !(x.r && x.r.ok && Array.isArray(x.r.data.candidates)));
  let r = null, err = null;
  if (okCalls.length) {
    const seen = {}, merged = [], gaps = [], dropped = [], notes = [];
    okCalls.forEach(x => {
      (x.r.data.candidates || []).forEach(c => {
        const k = c && c.url;
        if (!k || seen[k]) return;                      // the same venue can answer two searches
        seen[k] = 1; merged.push(c);
      });
      (x.r.data.gaps || []).forEach(g => gaps.push(g));
      (x.r.data.dropped || []).forEach(d => dropped.push(d));
      (x.r.data.notes || []).forEach(n => { if (notes.indexOf(n) < 0) notes.push(n); });
    });
    failed.forEach(x => notes.push('The search for ' + (x.j || 'this subject') + ' did not answer'
      + (x.err && x.err.name === 'AbortError' ? ' within 55 seconds' : '') + ' — nothing from it is listed below.'));
    r = { ok: true, status: 200, data: { candidates: merged, gaps: gaps, dropped: dropped,
      notes: notes, model: (okCalls[0].r.data || {}).model } };
  } else {
    const first = results[0] || {};
    err = first.err || null; r = first.r || null;
  }
  if (r && r.ok && Array.isArray(r.data.candidates)) {
    // A candidate without a usable URL cannot be gated or fetched; drop it and say how many, so a
    // short list is never mistaken for a thin one.
    const all = r.data.candidates.filter(c => c && typeof c === 'object');
    const fresh = all.filter(c => isUrl(c.url));
    // Searching one gap ADDS to the list; searching everything replaces it. Otherwise filling a
    // hole for ES would silently throw away every venue already ticked for DE.
    cands = only ? cands.filter(c => !fresh.some(f => f.url === c.url)).concat(fresh) : fresh;
    Object.keys(picked).forEach(u => { if (!cands.some(c => c.url === u)) delete picked[u]; });
    drawCands(r.data.gaps || [], r.data.dropped || []);
    discHelp();
    const notes = [].concat(Array.isArray(r.data.notes) ? r.data.notes : [],
      all.length > cands.length ? [pl(all.length - cands.length, 'candidate') + ' arrived without a usable URL and were left out.'] : []);
    setNote('#find-note', (cands.length
      ? '<b>' + esc(pl(cands.length, 'candidate')) + ' proposed' + (r.data.model ? ' by ' + esc(r.data.model) : '') + '.</b> Tick the ones this scan should read.'
      : '<b>No venue proposed for this brief.</b> Add the listing pages you know by hand below.')
      + (notes.length ? '<br>' + notes.map(esc).join('<br>') : ''), !cands.length);
    // The point of Find sources is to choose coverage, so the coverage preview opens with the
    // answer rather than waiting to be asked for.
    renderPreview();
    return;
  }
  // Same fallback discipline as the describe path: say why, and leave the manual input working.
  const why = (err && err.name === 'AbortError') ? 'discovery took longer than 55 seconds'
    : err ? 'no discover endpoint is reachable from this page'
    : (r.status === 501 || r.status === 404) ? 'source discovery is not configured on this deployment (HTTP ' + r.status + ')'
    : 'the discover endpoint answered ' + r.status + (r.data.message ? ': ' + r.data.message : '');
  setNote('#find-note', 'Could not propose sources — ' + esc(why) + '. Add the listing pages you know into <b>Sources</b> below; the scan is created the same way and each URL is gated the same way. Leaving <b>Discover sources automatically</b> ticked lets the workflow look for venues itself, as it did before.', true);
}

$('#dlg-build').addEventListener('click', async () => {
  const desc = $('#f-desc').value.trim();
  if (desc.length < 20) { setNote('#dlg-pnote-1', 'Say a little more — the subject, the countries, what to surface — or create the scan manually.', true); $('#f-desc').focus(); return; }
  const b = $('#dlg-build'); b.disabled = true; b.textContent = 'Proposing…';
  setNote('#dlg-pnote-1', '');
  let r = null, err = null;
  try { r = await postJSON(D.api.propose, { description: desc }); } catch (e) { err = e; }
  b.disabled = false; b.textContent = 'Build the scan';
  if (r && r.ok && r.data.proposal) {
    const p = r.data.proposal || {};
    $('#f-name').value = p.name || '';
    $('#f-intent').value = p.intent || desc;
    // Jurisdictions may arrive as codes or as {code, name} objects; the chip input normalises either.
    F.jur.set((p.jurisdictions || []).map(j => typeof j === 'string' ? j : (j && (j.code || j.name)) || '').filter(Boolean));
    F.top.set(p.topics || []); F.ind.set(p.industries || []);
    F.src.set((p.sources || []).map(s => typeof s === 'string' ? s : (s && s.url) || '').filter(isUrl));
    const notes = [].concat(p.notes ? [p.notes] : [], Array.isArray(r.data.notes) ? r.data.notes : []);
    setNote('#dlg-pnote', '<b>Proposed from your description.</b> Check every field before creating; the sources listed are suggestions until the gate has read them.' + (notes.length ? '<br>' + notes.map(esc).join('<br>') : ''));
    setStep('form'); renderPreview(); $('#f-name').focus();
    return;
  }
  // Fallback: the manual form, with one line saying why. The description becomes the intent so
  // the partner does not retype the sentence they just wrote.
  const why = err ? 'no propose endpoint is reachable from this page'
    : (r.status === 501 || r.status === 404) ? 'the describe path is not configured on this deployment (HTTP ' + r.status + ')'
    : 'the propose endpoint answered ' + r.status + (r.data.message ? ': ' + r.data.message : '');
  if (!$('#f-intent').value.trim()) $('#f-intent').value = desc;
  setNote('#dlg-pnote', 'Could not build the scan from the description — ' + esc(why) + '. Fill the form in by hand; your description is in the intent box.', true);
  setStep('form'); $('#f-name').focus();
});
form.addEventListener('submit', async e => {
  e.preventDefault();
  const name = $('#f-name').value.trim(), intent = $('#f-intent').value.trim();
  // Sources go as {url} objects: run.validate_definition requires objects (reviewed defect: URL
  // strings passed api/scans.js, then the workflow exited 2 with nothing committed). Clients go
  // back as the object they came in as, or the plain name.
  // Ticked candidates carry what discovery said about the venue; a URL typed into the chip input
  // is just a URL. Both are objects, because run.validate_definition requires objects (reviewed
  // defect: URL strings passed api/scans.js, then the workflow exited 2 with nothing committed).
  const chosen = cands.filter(c => picked[c.url]);
  const chosenUrls = chosen.map(c => c.url);
  // A kind the pipeline does not know would fail run.validate_definition and lose the whole
  // create over a decorative field, so an unrecognised one is dropped rather than forwarded.
  const describe = (from, into) => {
    ['name', 'jurisdiction', 'rationale'].forEach(k => { if (from[k]) into[k] = String(from[k]).slice(0, 300); });
    if (SOURCE_KINDS.includes(from.kind)) into.kind = from.kind;
    return into;
  };
  const sources = chosen.map(c => describe(c, { url: c.url }))
    .concat(F.src.get().filter(u => !chosenUrls.includes(u)).map(url => {
      const was = srcObjs[url];
      return was ? describe(was, { url }) : { url };
    }));
  const scan = { name, intent, jurisdictions: F.jur.get(), topics: F.top.get(), industries: F.ind.get(),
    sources, clients: F.cl.get().map(n => clientObjs[n] || n) };
  // The subject filter travels as the partner left it. "none" is sent explicitly rather than
  // omitted: on an Edit, an omitted key would let the previous filter stand, and pressing
  // "Read everything" must actually clear it — the consequence the block states is the one the
  // next run has to deliver.
  scan.subject_filter = (subject.regex && subject.source !== 'none')
    ? { regex: subject.regex, why: subject.why, source: subject.source === 'partner' ? 'partner' : 'proposed' }
    : { regex: '', why: '', source: 'none' };
  // Carry forward what this dialog does not edit. An Edit re-creates the scan from this object,
  // so a key left out here is erased: no_misc silently re-enabled the Miscellaneous lane, budget
  // reset the partner's own caps, and discovery_notes lost discovery's account of its search.
  // The schedule is written EXPLICITLY on every save — set, or null to clear — so a partner who
  // unticks it really turns it off (an absent key would let run.py carry the old one forward).
  const schedOn = $('#f-sched').checked, schedAt = ($('#f-sched-at').value || '').trim();
  if (schedOn && !/^([01]\d|2[0-3]):[0-5]\d$/.test(schedAt)) { err.textContent = 'Give the daily run a time (HH:MM).'; $('#f-sched-at').focus(); return; }
  scan.schedule = schedOn ? { daily_at: schedAt, tz: 'Asia/Kolkata', set_by: (D.user || 'a partner'), set_on: new Date().toISOString() } : null;
  if (editingId && editingDefn) {
    if (editingDefn.no_misc === true) scan.no_misc = true;
    if (editingDefn.budget && Object.keys(editingDefn.budget).length) scan.budget = editingDefn.budget;
    if ((editingDefn.discovery_notes || []).length) scan.discovery_notes = editingDefn.discovery_notes;
    if (editingDefn.demo === true) scan.demo = true;
  }
  if (editingId) scan.id = editingId;
  const err = $('#dlg-err');
  if (name.length < 3) { err.textContent = 'Give the scan a name (3 characters or more).'; $('#f-name').focus(); return; }
  if (intent.length < 20) { err.textContent = 'The intent is what discovery and relevance read — write at least a sentence.'; $('#f-intent').focus(); return; }
  // Reviewed defect: ticking a venue turns "Discover sources automatically" off, but unticking
  // the last one never turned it back on — so a partner who changed their mind could dispatch a
  // scan with no sources and no discovery, which reads nothing and burns a run to say so.
  if (!$('#f-disc').checked && !sources.length) {
    err.textContent = 'Discovery is off and no source is listed, so this scan would read nothing. '
      + 'Tick a venue, add a listing URL, or turn discovery back on.';
    $('#f-disc').focus(); return;
  }
  // The pipeline gates at most MAX_SOURCES; the picker used to let a partner tick 25 and say
  // nothing about the ones that would sit unfetched.
  if (sources.length > MAX_SOURCES) {
    err.textContent = 'A scan reads at most ' + MAX_SOURCES + ' sources; ' + sources.length
      + ' are listed. Untick ' + (sources.length - MAX_SOURCES) + ' — the rest would be approved but never fetched.';
    return;
  }
  // run.py refuses an empty jurisdictions list; asking here saves a queued create that fails two
  // minutes later on the Actions page.
  if (!scan.jurisdictions.length) { err.textContent = 'Add at least one jurisdiction — the pipeline refuses a scan without one.'; F.jur.input.focus(); return; }
  if (!scan.topics.length && !scan.industries.length && !scan.sources.length) { err.textContent = 'Add at least one topic, industry or source, or discovery has nothing to look for.'; F.top.input.focus(); return; }
  // A filter the run would refuse is worse than none: the page would claim a subject the ledger
  // does not have. Refuse it here, where the box is still in front of the person who wrote it.
  if (scan.subject_filter.regex && !compileSubject(scan.subject_filter.regex)) {
    err.textContent = 'The subject filter is not a valid regular expression, so the run would refuse it. '
      + 'Fix it, or press Read everything to create this scan without one.';
    $('#f-subject').focus(); return;
  }
  // The gate on Create: a scan is its coverage, and this is the last moment the partner can see
  // that coverage before twenty minutes of gating and reading happen on their behalf. The button
  // is disabled until the preview has been drawn; this is the belt to that braces, for a submit
  // that arrived by Enter rather than by the button.
  err.textContent = '';
  const btn = $('#dlg-submit'); btn.disabled = true;
  const verb = editingId ? 'update and re-run this scan' : 'create the scan';
  const req = { action: 'create', scan_id: editingId || undefined, scan, no_discover: !$('#f-disc').checked };
  const res = await dispatchScan(req, verb);
  btn.disabled = false;
  if (res) {
    // Remember it so the Scans home shows it immediately. api/scans.js answers 202 with the id it
    // derived from the name, which is the id the page will live at — take it from there rather
    // than deriving a second slug here that could disagree with the workflow's.
    // The request travels with it so a failed run can be repeated exactly as it was sent, without
    // making the partner retype a definition they have already confirmed.
    addPending({ id: res.scan_id || scan.id || '', name: name, dispatched_at: new Date().toISOString(),
      kind: 'create', action: 'create', request: req, verb: verb,
      actionsUrl: res.actionsUrl || D.actionsUrl || '' });
    dlg.close();
  }
});
$$('[data-close]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));

// ---- draft modal ------------------------------------------------------------------------------
const modal = $('#modal');
function showModal(title, text, note) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = '<pre id="modal-text">' + esc(text) + '</pre>';
  // `note` is a string or a list of them (the endpoint's notes[] follow the provenance line).
  $('#modal-note').innerHTML = [].concat(note || []).filter(Boolean).map(esc).join('<br>');
  modal.showModal();
}
$('#modal-copy').addEventListener('click', async () => {
  const t = ($('#modal-text') || {}).textContent || '';
  const b = $('#modal-copy');
  try { await navigator.clipboard.writeText(t); b.textContent = 'Copied'; }
  catch (e) { const r = document.createRange(); r.selectNodeContents($('#modal-text')); const s = getSelection(); s.removeAllRanges(); s.addRange(r); b.textContent = 'Selected — press ⌘C'; }
  setTimeout(() => { b.textContent = 'Copy'; }, 1800);
});

// ============================================================================================
if (D.page === 'home') renderHome(); else renderScan();

function tierBadge(t) { return t === 'vetted' ? '<span class="badge vetted">Vetted</span>' : '<span class="badge discovered">Discovered sources</span>'; }

function renderHome() {
  const main = $('#main');
  const scans = D.cards.filter(c => !c.builtin);
  // Stars, the chosen tab and the sort live in this browser only (design §2), under one key so
  // the home page's memory can be cleared in one go.
  const HKEY = 'tmt_scans_home';
  let hs = { star: {}, tab: 'all', sort: 'name' };
  try { hs = Object.assign(hs, JSON.parse(localStorage.getItem(HKEY) || '{}')); } catch (e) {}
  if (!hs.star || typeof hs.star !== 'object') hs.star = {};
  if (!['all', 'starred'].includes(hs.tab)) hs.tab = 'all';
  if (!['name', 'lastrun', 'new'].includes(hs.sort)) hs.sort = 'name';
  const hsave = () => { try { localStorage.setItem(HKEY, JSON.stringify(hs)); } catch (e) {} };
  main.innerHTML = '<div class="titlerow"><div><div class="crumb">TMT Regulatory Radar</div><h1 class="title">Scans</h1>'
    // This page is the product's front door, not an index behind the tracker: it opens with what a
    // scan is and who owns which one, because a partner arriving here for the first time has no
    // other page to learn it from.
    + '<p class="lede">A scan is one question read against a fixed list of official sources you can see. <b>TMT India</b> is the built-in, vetted one; anything else here you made, and its sources are labelled <b>discovered</b> wherever they appear. A scan runs when you press Run scan; one that someone has set to run daily says so on its page, with their name.</p></div>'
    + '<div class="actions"><button class="btn primary" id="create">+ Create scan</button></div></div>'
    + '<div class="notice" id="notice"></div>'
    + '<div class="htoolbar"><div class="ttabs" role="tablist" id="htabs"></div>'
    + '<div class="tools"><select id="hsort" aria-label="Sort scans"><option value="name">Sort: name</option><option value="lastrun">Sort: last run</option><option value="new">Sort: new developments</option></select></div></div>'
    + '<div class="pending" id="pending"></div>'
    + '<div class="cards" id="cards"></div>'
    + (scans.length ? '' : '<div class="empty" id="hempty"><h2>No scans yet.</h2><p>Describe a question the way you would brief an associate — the clients, the jurisdictions, what to surface — and the system finds candidate places to read, gates each one, reads them, and writes you a weekly digest in which every sentence is cited or marked as uncited.</p><p>Runs happen when you press <b>Run scan</b>, never on a schedule. Nothing enters a scan that did not come from a source you can see on its coverage panel.</p><button class="btn primary" id="create2">+ Create your first scan</button></div>');
  noticeEl = $('#notice');
  $('#create').addEventListener('click', () => openDialog(null));
  const c2 = $('#create2'); if (c2) c2.addEventListener('click', () => openDialog(null));
  $('#hsort').value = hs.sort;
  $('#hsort').addEventListener('change', e => { hs.sort = e.target.value; hsave(); drawCards(); });
  $('#htabs').addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) { hs.tab = b.dataset.tab; hsave(); setTabHash(hs.tab); drawCards(); } });
  $('#cards').addEventListener('click', e => {
    const b = e.target.closest('button[data-star]'); if (!b) return;
    e.preventDefault();
    const id = b.dataset.star;
    if (hs.star[id]) delete hs.star[id]; else hs.star[id] = true;
    hsave(); drawCards();
  });
  const kpiNew = c => (c.kpi && c.kpi[0] && typeof c.kpi[0].n === 'number') ? c.kpi[0].n : 0;
  function drawCards() {
    const starred = D.cards.filter(c => hs.star[c.id]).length;
    $('#htabs').innerHTML = [['starred', 'Starred', starred], ['all', 'All', D.cards.length]].map(([k, l, n]) => '<button type="button" role="tab" data-tab="' + k + '" class="' + (hs.tab === k ? 'on' : '') + '" aria-selected="' + (hs.tab === k) + '">' + l + '<span class="k">' + n + '</span></button>').join('');
    const list = D.cards.filter(c => !c.builtin && (hs.tab === 'all' || hs.star[c.id]));
    const byName = (a, b) => a.name.localeCompare(b.name);
    list.sort(hs.sort === 'lastrun' ? ((a, b) => (b.generated || '').localeCompare(a.generated || '') || byName(a, b))
      : hs.sort === 'new' ? ((a, b) => (kpiNew(b) - kpiNew(a)) || byName(a, b)) : byName);
    // The built-in tracker leads on every tab and under every sort: it is the vetted reference the
    // discovered scans are measured against, and a partner should never have to look for it.
    const builtin = D.cards.filter(c => c.builtin);
    $('#cards').innerHTML = builtin.concat(list).map(c => card(c, !!hs.star[c.id])).join('')
      + (hs.tab === 'starred' && !list.length && !builtin.some(c => hs.star[c.id]) ? '<div class="hnone">No starred scans yet — press ☆ on a card to keep it here.</div>' : '');
    $$('.card .last').forEach(el => { el.textContent = 'Last run ' + rel(el.dataset.iso); });
  }

  // ---- pending runs: dispatched, not yet on this page -----------------------------------------
  // The home page's job here is a CREATE: the scan has no card until the workflow has committed
  // and the site has rebuilt. A promotion dispatched from a scan page also lands here, keyed by
  // finding rather than by scan.
  const builtIds = {}, cardById = {};
  D.cards.forEach(c => { builtIds[c.id] = true; cardById[c.id] = c; });
  mountPending($('#pending'), {
    mine: () => true,                       // every errand in this browser is shown on the home page
    builtNow: p => {
      // A create is finished when the scan has a card: that card IS the committed result.
      if (builtIds[p.id]) return true;
      const c = cardById[p.scan_id || p.id];
      if (!c) return false;
      const since = Date.parse(p.dispatched_at || 0);
      // A promotion commits a changed DEFINITION and no new developments, so the definition's own
      // stamp is what moves. A run commits developments, so its stamp is the last-run time. Both
      // are on the card, and either one advancing past the dispatch means this errand has landed.
      return Date.parse(c.updated || 0) > since || Date.parse(c.generated || 0) > since;
    },
    onLanded: p => {
      // A promotion changes one thing and it is not on this page: the scan's source list. Say
      // where the verdict will be, and that the gate has not spoken yet.
      if (kindOf(p) === 'promote') {
        say('“' + esc(p.name || p.scan_id) + '” has the promoted venue in its sources now, as <b>pending</b>. '
          + 'The gate judges it at the start of the next run — open the scan and press <b>Run scan</b>.', 'warn');
      }
    },
    onDraw: list => { const em = $('#hempty'); if (em) em.hidden = list.length > 0; },
  });
  // The home page has tabs too (Starred / All); the hash keeps the partner on the one they chose
  // when this page reloads itself after a run lands.
  const hhash = tabFromHash();
  if (['all', 'starred'].includes(hhash)) hs.tab = hhash;
  setTabHash(hs.tab);
  drawCards();
}
function card(c, starred) {
  const flags = (c.flags || []).map(flag).filter(Boolean).join(' ');
  return '<div class="card' + (c.builtin ? ' builtin' : '') + '">'
    + '<a class="cardmain" href="' + esc(c.href) + '"><div class="name">' + esc(c.name) + tierBadge(c.tier) + (c.demo ? '<span class="badge demo">Demo</span>' : '') + '</div>'
    + '<div class="meta"><span>' + esc(c.meta) + '</span>' + (flags ? '<span class="flags" aria-label="' + esc((c.flags || []).join(', ')) + '">' + flags + '</span>' : '') + '</div>'
    + (c.problems && c.problems.length ? '<ul class="problems">' + c.problems.map(p => '<li>' + esc(p) + '</li>').join('') + '</ul>' : '') + '</a>'
    + '<div class="right"><div class="kpi">' + c.kpi.map(k => esc(k.n) + '<small>' + esc(k.label) + '</small>').join('<span class="sep">·</span>') + '</div>'
    + '<div class="last" data-iso="' + esc(c.generated) + '"></div>'
    + '<button type="button" class="hstar' + (starred ? ' on' : '') + '" data-star="' + esc(c.id) + '" aria-pressed="' + !!starred + '" aria-label="' + (starred ? 'Unstar ' : 'Star ') + esc(c.name) + '" title="' + (starred ? 'Starred in this browser' : 'Star this scan (this browser only)') + '">' + (starred ? '★' : '☆') + '</button></div></div>';
}

// ============================================================================================
function renderScan() {
  const S = D.scan, items = D.items;
  const MISC = D.misc || { present: false, generated: '', query: {}, findings: [], notes: [] };
  const byId = {}; items.forEach(it => { byId[it.id] = it; });
  const KL = KINDL;  // discover.KINDS -> chip label; one table, defined once above
  // Triage lives in this browser only, as clients do on the tracker. Nothing leaves the page.
  // `view` (the open tab) and `promoted` (findings this browser has already asked the gate about)
  // live beside it under the same key, so clearing one clears them all.
  const KEY = 'tmt_scan_' + S.id;
  let state = { read: {}, star: {}, arch: {}, promoted: {}, view: 'coverage' };
  try { state = Object.assign(state, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) {}
  ['read', 'star', 'arch', 'promoted'].forEach(k => { if (!state[k] || typeof state[k] !== 'object') state[k] = {}; });
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };
  let openId = null, lastFocus = null;

  // ---- lanes ------------------------------------------------------------------------------------
  // The builder stamped `lane` on every development using the contract's one routing rule; the page
  // groups by that stamp rather than re-deriving it, so the tab counts and the tables cannot
  // disagree about where a development lives — and every development is in exactly one of them.
  const LANE_DEFS = [
    { k: 'instruments', label: 'Instruments', types: ['Legislation', 'Rules/Regulations', 'Order/Decision', 'Notice/Circular', 'Guidance/Advisory'],
      sub: 'The binding texts and the official readings of them. Every row came from a source on the Coverage tab, and every summary sentence quotes a passage of the document itself.' },
    { k: 'judgments', label: 'Judgments', types: ['Judgment'],
      sub: 'Judgments of courts and tribunals, as the forum published them.' },
    { k: 'signals', label: 'Signals', types: ['Consultation/Draft', 'Press release', 'Other'],
      sub: 'Not a binding instrument, or not yet one. A development the enricher has not typed — because it is queued, unread or its enrichment failed — is parked here and marked untyped, rather than dropped out of every lane.' },
  ];
  const LANE_BY = {}; LANE_DEFS.forEach(d => { LANE_BY[d.k] = d; });
  const laneItems = { instruments: [], judgments: [], signals: [] };
  items.forEach(it => { (laneItems[it.lane] || laneItems.signals).push(it); });
  const lstate = {}; LANE_DEFS.forEach(d => { lstate[d.k] = { tab: 'all', q: '', sort: 'newest' }; });

  // Clients named on the scan, plus any client a development rates that the scan does not name: an
  // orphan rating is a fact about the ledger and gets its own section rather than vanishing.
  const scanClients = (S.clients || []).map(c => ({ name: clientName(c), scope: (c && typeof c === 'object' && c.scope) ? String(c.scope) : '' })).filter(c => c.name);
  const ratedNames = {};
  items.forEach(it => Object.keys(it.relevance.clients || {}).forEach(n => { ratedNames[n] = true; }));
  const orphanClients = Object.keys(ratedNames).filter(n => !scanClients.some(c => c.name.toLowerCase() === n.toLowerCase())).sort();

  const VIEWS = [
    { k: 'coverage', label: 'Coverage', n: () => D.coverage.approved.length },
    { k: 'instruments', label: 'Instruments', n: () => laneItems.instruments.filter(it => !state.arch[it.id]).length },
    { k: 'judgments', label: 'Judgments', n: () => laneItems.judgments.filter(it => !state.arch[it.id]).length },
    { k: 'signals', label: 'Signals', n: () => laneItems.signals.filter(it => !state.arch[it.id]).length },
    // No count when the file is absent: "0" would claim the open web was searched and held nothing.
    // Reviewed defect: the count was every row in the file, so it kept counting leads the partner
    // had dismissed, venues already promoted into coverage, and findings the last search no longer
    // returns — a tab reading "14" when the search had in fact surfaced four live leads. It counts
    // what the last search actually stands behind and nothing else; the groups below still SHOW the
    // rest, marked, because a lead is never deleted here.
    { k: 'misc', label: 'Miscellaneous', n: () => MISC.present ? MISC.findings.filter(isLiveFinding).length : null },
    { k: 'clients', label: 'Clients', n: () => scanClients.length + orphanClients.length },
    { k: 'audit', label: 'Audit', n: () => D.coverage.approved.length + D.coverage.pending.length + D.coverage.rejected.length },
  ];
  if (!VIEWS.some(v => v.k === state.view)) state.view = 'coverage';

  const main = $('#main');
  const flags = S.jurisdictions.map(j => flagged(j)).join(' ');
  main.innerHTML = '<div class="crumb"><a href="/scans.html">Scans</a><span>&rsaquo;</span>' + esc(S.name) + '</div>'
    + '<div class="titlerow"><div><h1 class="title">' + esc(S.name) + '</h1>'
    + '<div class="metaline"><span>' + esc(D.meta) + '</span><span class="dot">&middot;</span><span class="flags">' + flags + '</span><span class="dot">&middot;</span><span>Last run <b id="lastrun"></b></span>'
    + (S.demo ? '<span class="badge demo">Demo</span>' : '') + '<span class="badge discovered">Discovered sources</span></div>'
    + '<p class="plain">' + scheduleLine(S) + '</p>'
    + (S.intent ? '<p class="intent">' + esc(S.intent) + '</p>' : '') + '</div>'
    // run.py refuses a demo definition with exit 2 (its sources are reserved .test hosts), so the
    // button says so up front instead of letting a partner queue a run that can only fail.
    + '<div class="actions"><button class="btn primary' + (S.demo ? ' demo-off' : '') + '" id="run"' + (S.demo ? ' disabled title="Demo scans use fixture hosts and cannot be run live — create your own scan" aria-disabled="true"' : '') + '>Run scan</button><button class="btn" id="edit">Edit</button></div></div>'
    + '<div class="notice" id="notice"></div>'
    // Run scan and Promote are pressed HERE, so the wait has to be shown here too. Before this the
    // scan page dispatched a run, printed one line, and then looked identical for twelve minutes.
    + '<div class="pending" id="pending"></div>'
    + (D.problems.length ? '<ul class="problems">' + D.problems.map(p => '<li>' + esc(p) + '</li>').join('') + '</ul>' : '')
    // The digest and its tiles are the scan's masthead: true of every tab, so above all of them.
    + digestHTML()
    + '<nav class="vtabs" id="vtabs" role="tablist" aria-label="Scan sections"></nav>'
    + '<section class="view" id="v-coverage" role="tabpanel" aria-label="Coverage"><div class="coverage">' + coverageHTML() + '</div></section>'
    + LANE_DEFS.map(d => '<section class="view" id="v-' + d.k + '" role="tabpanel" aria-label="' + esc(d.label) + '">' + laneShell(d) + '</section>').join('')
    + '<section class="view" id="v-misc" role="tabpanel" aria-label="Miscellaneous"></section>'
    + '<section class="view" id="v-clients" role="tabpanel" aria-label="Clients"></section>'
    + '<section class="view" id="v-audit" role="tabpanel" aria-label="Audit"></section>'
    // The obligations register spans every lane, so it belongs to none of them: it stays below the
    // tabs, where it reads as a property of the scan rather than of whichever tab happens to be open.
    + '<section class="oblsec" id="oblsec" aria-label="Obligations register">' + obligationsHTML() + '</section>';
  noticeEl = $('#notice');
  $('#lastrun').textContent = rel(D.generated);
  $('#run').addEventListener('click', async () => {
    if (S.demo) return;
    const b = $('#run'); b.disabled = true;
    const req = { action: 'run', scan_id: S.id };
    const res = await dispatchScan(req, 'run this scan');
    b.disabled = false;
    if (!res) return;
    // The record is what turns a fired-and-forgotten dispatch into a wait the partner can watch:
    // it carries the clock, the derived phase, the failure state and the request to repeat.
    addPending({ id: S.id, scan_id: S.id, kind: 'run', name: S.name, request: req, verb: 'run this scan',
                 dispatched_at: new Date().toISOString(),
                 actionsUrl: res.actionsUrl || D.actionsUrl || '' });
  });
  $('#oblsec').addEventListener('click', e => { const b = e.target.closest('button[data-dev]'); if (b) openDetail(b.dataset.dev, b); });
  $('#main').addEventListener('click', e => { const a = e.target.closest('a[data-dev]'); if (a) { e.preventDefault(); openDetail(a.dataset.dev, a); } });
  $('#edit').addEventListener('click', () => openDialog(S));
  $('#vtabs').addEventListener('click', e => { const b = e.target.closest('button[data-view]'); if (b) setView(b.dataset.view); });
  LANE_DEFS.forEach(d => {
    const l = d.k;
    $('#q-' + l).addEventListener('input', e => { lstate[l].q = e.target.value.trim().toLowerCase(); drawLane(l); });
    $('#sort-' + l).addEventListener('change', e => { lstate[l].sort = e.target.value; drawLane(l); });
    $('#ttabs-' + l).addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) { lstate[l].tab = b.dataset.tab; drawLane(l); } });
    $('#tb-' + l).addEventListener('click', e => { const r = e.target.closest('tr.r'); if (r) openDetail(r.dataset.id, r); });
    $('#tb-' + l).addEventListener('keydown', e => { const r = e.target.closest('tr.r'); if (r && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openDetail(r.dataset.id, r); } });
  });

  // ---- tab bar ----------------------------------------------------------------------------------
  function drawTabs() {
    $('#vtabs').innerHTML = VIEWS.map(v => {
      const n = v.n();
      return '<button type="button" role="tab" data-view="' + v.k + '" class="' + (state.view === v.k ? 'on' : '') + '"'
        + ' aria-selected="' + (state.view === v.k) + '" aria-controls="v-' + v.k + '">' + esc(v.label)
        + (n == null ? '' : '<span class="k">' + esc(n) + '</span>') + '</button>';
    }).join('');
  }
  // Each tab draws when it is opened rather than on first paint: a scan with a long ledger would
  // otherwise build seven tables to show one.
  function setView(k) {
    if (!VIEWS.some(v => v.k === k)) k = 'coverage';
    state.view = k; save(); setTabHash(k);
    VIEWS.forEach(v => { $('#v-' + v.k).classList.toggle('on', v.k === k); });
    drawTabs();
    if (LANE_BY[k]) drawLane(k);
    else if (k === 'misc') drawMisc();
    else if (k === 'clients') drawClients();
    else if (k === 'audit') drawAudit();
  }

  // ---- digest --------------------------------------------------------------------------------
  function digestHTML() {
    const dg = D.digest, order = [];
    (dg.body || []).forEach(p => p.cites.forEach(id => { if (!order.includes(id)) order.push(id); }));
    const body = (dg.body || []).length
      ? dg.body.map(p => '<p>' + esc(p.text) + p.cites.map(id => citeChip(id, order.indexOf(id) + 1, 'dev')).join('')
          + (p.uncited ? '<span class="unc" title="This sentence cites no development in the ledger; it is the digest writer\'s own reading, not evidence.">no citation</span>' : '') + '</p>').join('')
      : '<p class="none">' + (D.generated ? 'No digest was written for this run.' : 'Nothing has been read yet — press Run scan.') + '</p>';
    // The selection rule and the writer's notes, in small type: "nothing dated within 14 days —
    // newest 25" tells the partner the digest is re-narrating old items; "model call failed"
    // says why the headline reads "Digest not written". Reviewed defect: neither was shown.
    const fine = [].concat(dg.selection ? ['Selection: ' + dg.selection] : [], dg.notes || []);
    // Demo fixtures are labelled wherever text can leave the page (reviewed defect: only the
    // home card and header said so).
    const assessed = typeof D.counts.assessed === 'number', queued = typeof D.counts.queued === 'number' && D.counts.queued > 0;
    return '<section class="digest" aria-label="Weekly digest"><div><div class="label">Weekly digest' + (S.demo ? ' · Demo — fixture data' : '') + (dg.week ? ' · ' + esc(dg.week) : '') + '</div>'
      + (fine.length ? '<div class="fine">' + fine.map(esc).join('<br>') + '</div>' : '')
      + (dg.headline ? '<h2>' + esc(dg.headline) + '</h2>' : '') + body + upcomingHTML() + '</div>'
      + '<div class="kpis"><div class="kpi-tile"><div class="n">' + esc(D.counts.new) + '</div><div class="l">New developments</div>'
      + (queued ? '<div class="sub">' + esc(D.counts.queued) + ' queued, not yet read</div>' : '') + '</div>'
      + '<div class="kpi-tile"><div class="n">' + esc(D.counts.high) + '</div><div class="l">High relevance</div>'
      + (assessed ? '<div class="sub">of ' + esc(D.counts.assessed) + ' assessed' + (queued ? ' · ' + esc(D.counts.queued) + ' queued' : '') + '</div>' : '') + '</div></div></section>';
  }
  // The Debrief's second face (design §2): every obligation whose date is still ahead, computed
  // from obligations[].when in code — never by the model, because a deadline list must not be a
  // guess. "Ahead" is judged against the reader's today, so the same page shortens as time passes.
  // Function declarations, not consts: the header render above calls into these before any
  // const below it would be initialised. `when` is free text from the enricher ("from 2027-01-01",
  // "by 1 March each year"); only an embedded, real calendar date counts — "2027" alone does not.
  function isoDate(s) {
    const m = String(s || '').match(/(\d{4})-(\d{2})-(\d{2})/); if (!m) return '';
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return (d.getUTCFullYear() === +m[1] && d.getUTCMonth() === +m[2] - 1 && d.getUTCDate() === +m[3]) ? m[0] : '';
  }
  function todayISO() { const t = new Date(); return t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-' + String(t.getDate()).padStart(2, '0'); }
  function upcomingRows() {
    const today = todayISO();
    let rows;
    if (Array.isArray(D.digest.upcoming)) {
      rows = D.digest.upcoming.map(u => ({ when: isoDate(u.when), who: u.who || '', what: u.what || '', dev: u.dev })).filter(u => u.when && byId[u.dev]);
    } else {
      rows = [];
      items.forEach(it => it.obligations.forEach(o => { const w = isoDate(o.when); if (w) rows.push({ when: w, who: o.who, what: o.what, dev: it.id }); }));
    }
    return rows.filter(u => u.when >= today).sort((a, b) => a.when.localeCompare(b.when) || a.who.localeCompare(b.who)).slice(0, 12);
  }
  function upcomingHTML() {
    const rows = upcomingRows();
    return '<div class="upc"><div class="label">Upcoming</div>' + (rows.length
      ? '<ul>' + rows.map(u => '<li><a href="#dev-' + esc(u.dev) + '" data-dev="' + esc(u.dev) + '" title="' + esc(byId[u.dev].title) + '"><span class="d">' + esc(fmt(u.when)) + '</span><span><span class="who">' + esc(u.who || '—') + '</span><span class="sep">·</span>' + esc(u.what || '—') + '</span></a></li>').join('') + '</ul>'
      : '<p class="none">No dated obligations ahead.</p>') + '</div>';
  }
  // The obligations register (design §10): one table of every obligations[] row across the
  // ledger. Rows with a parseable date come first in date order; the rest keep the ledger's
  // order, and their `when` is shown exactly as the document put it.
  function obligationsHTML() {
    const rows = [];
    items.forEach(it => it.obligations.forEach(o => rows.push({ o, it, iso: isoDate(o.when) })));
    rows.sort((a, b) => (a.iso && b.iso) ? a.iso.localeCompare(b.iso) : a.iso ? -1 : b.iso ? 1 : 0);
    const head = '<h3>Obligations</h3><p class="sub">Every obligation extracted across this scan\'s developments — ' + pl(rows.length, 'row') + ' from ' + pl(items.filter(it => it.obligations.length).length, 'development') + '. An empty list is an honest answer: the documents imposed nothing concrete.</p>';
    if (!rows.length) return head + '<div class="empty-o">No obligations extracted yet.</div>';
    return head + '<div class="tablewrap"><table class="obl"><thead><tr><th style="width:22%">Who</th><th>What</th><th style="width:18%">When</th><th style="width:30%">Development</th></tr></thead><tbody>'
      + rows.map(r => '<tr><td>' + esc(r.o.who || '—') + '</td><td>' + esc(r.o.what || '—') + '</td><td>' + esc(r.o.when || '—') + '</td>'
        // The tier chip travels with the development everywhere it is named (reviewed defect: the
        // register and the hover card showed one without its Vetted/Discovered mark).
        + '<td><button type="button" class="devlink" data-dev="' + esc(r.it.id) + '">' + esc(r.it.headline || r.it.title) + '</button><div class="w">' + flagged(r.it.jurisdiction) + ' · ' + esc(r.it.date ? fmt(r.it.date) : 'undated') + ' ' + srcChip(r.it) + '</div></td></tr>').join('')
      + '</tbody></table></div>';
  }
  function citeChip(ref, n, kind, extra) {
    return '<button type="button" class="cite' + (extra && extra.unverified ? ' x' : '') + '" data-kind="' + kind + '" data-ref="' + esc(ref) + '" aria-label="Citation ' + n + '">' + n + '</button>';
  }

  // ---- coverage ------------------------------------------------------------------------------
  function coverageHTML() {
    const C = D.coverage;
    const src = (s, extra) => '<div class="src"><div class="dotc ' + esc(s.health ? s.health.status : '') + '" title="' + esc(s.health ? s.health.status : 'not run') + '"></div><div>'
      + '<div class="n"><b>' + esc(s.name) + '</b><span class="host">' + esc(s.host) + '</span>' + (s.jurisdiction ? '<span>' + flagged(s.jurisdiction) + '</span>' : '')
      + kindChip(s.kind, s.tier, s.host)
      + (s.proposed_by ? '<span class="st">proposed by ' + esc(s.proposed_by) + (s.confidence ? ', ' + esc(s.confidence) + ' confidence' : '') + '</span>' : '') + '</div>'
      + (s.rationale ? '<div class="why">' + esc(s.rationale) + '</div>' : '') + extra + '</div></div>';
    const approved = C.approved.map(s => {
      const h = s.health;
      // GATED is not a fetch result: the source is approved but sat beyond max_sources on this run,
      // so nothing was read from it. Saying so here is what keeps the cap from reading as coverage.
      // A GATED source was never fetched, so its stamp is when the cap was RECORDED, not a check
      // (reviewed defect: "checked 3 min ago" on a source nothing touched).
      const line = !h ? '<span class="st">NOT RUN</span>'
        : h.status === 'GATED' ? '<span class="st gated">GATED</span><span class="sep">·</span><span class="gated">approved, but beyond max_sources this run — not fetched</span>' + (h.checked ? '<span class="sep">·</span>recorded ' + esc(rel(h.checked)) : '')
        : '<span class="st">' + esc(h.status) + '</span><span class="sep">·</span>' + esc(h.rows_seen == null ? '?' : h.rows_seen) + ' rows seen<span class="sep">·</span>' + esc(h.new == null ? 0 : h.new) + ' new<span class="sep">·</span>newest visible ' + esc(h.newest_visible ? fmt(h.newest_visible) : '—') + (h.checked ? '<span class="sep">·</span>checked ' + esc(rel(h.checked)) : '');
      const infos = h ? h.notes.map(n => '<li class="warn">' + esc(n) + '</li>').concat(h.info.map(n => '<li>' + esc(n) + '</li>')).join('') : '';
      return src(s, '<div class="ev">' + line + '</div><div class="ev">gate: ' + esc(s.evidence) + (s.checked ? '<span class="sep">·</span>' + esc(fmt(s.checked)) : '') + '</div>'
        + (h && h.status !== 'GATED' ? subjectCount(h.subject, SUBJ.on) : '')
        + (infos ? '<ul class="infos">' + infos + '</ul>' : ''));
    }).join('');
    // Pending has several causes and only one of them is a terms question. Reviewed defect: every
    // pending source told the partner to read the site's terms, including one parked for budget.
    const pendingNote = s => {
      const r = s.reason || '';
      if (/^budget:/i.test(r)) return 'Not gated: the scan already holds max_sources approved sources. Raise the budget or drop one.';
      if (/^not yet gated/i.test(r)) return 'Not gated: create did not finish. Edit and save to gate it.';
      if (s.flags.length || /^ToS language/i.test(r)) return 'A person must read this site\'s terms before it is approved. Nothing is fetched from it until then.';
      return 'The gate could not decide; a person must. Nothing is fetched from it until then.';
    };
    const pending = C.pending.map(s => src(s, '<div class="ev">gate: ' + esc(s.evidence) + '</div>' + (s.reason ? '<div class="why">' + esc(s.reason) + '</div>' : '')
      + s.flags.map(f => '<blockquote class="quote">“' + esc(f) + '”</blockquote>').join('')
      + '<div class="note">' + esc(pendingNote(s)) + '</div>')).join('');
    const rejected = C.rejected.map(s => src(s, '<div class="reason">' + esc(s.reason || 'rejected by the gate') + '</div><div class="ev">gate: ' + esc(s.evidence) + '</div>')).join('');
    const none = '<div class="src"><div></div><div class="why">None.</div></div>';
    // The last run's counts, in one line, so a FAILED source or a queued backlog is visible the
    // moment the panel opens (reviewed defect: health.run and sources_failed were never rendered).
    const R = D.run || {}, sf = D.counts.sources_failed || 0, se = D.counts.sources_empty || 0;
    const cell = (label, n, bad) => '<span' + (bad && n ? ' class="bad"' : '') + '><b>' + esc(n == null ? '—' : n) + '</b> ' + label + '</span>';
    const lastrun = D.generated ? '<div class="grp">Last run · ' + esc(stampText(D.generated)) + ' IST</div><div class="lastrun" id="lastrun-block">'
      + cell('new', R.new) + cell('enriched', R.enriched) + cell('queued', R.queued, true) + cell('not read', R.read_failed, true) + cell('enrichment failed', R.enrich_failed, true)
      + cell('sources failed', sf, true) + cell('sources empty', se, true) + (C.gated ? cell('sources not fetched (cap)', C.gated, true) : '') + '</div>' : '';
    const uncovered = C.uncovered && C.uncovered.length ? '<div class="gap">No approved source for: ' + C.uncovered.map(flagged).join(', ') + '</div>' : '';
    const discovery = C.discovery && C.discovery.length ? '<div class="grp">Discovery · ' + C.discovery.length + '</div><ul class="disc" id="discovery-notes">' + C.discovery.map(n => '<li>' + esc(n) + '</li>').join('') + '</ul>' : '';
    return '<h3>Coverage</h3><p class="sub">Every URL this scan reads, with the gate\'s evidence and the last run\'s health. A source not listed here cannot contribute a development.</p>'
      + '<div class="legend"><span class="badge vetted">Vetted</span><span>A TMT India registry source: a hand-built adapter, a fixture, a floor and a per-site legal analysis stand behind every row it produces.</span>'
      + '<span class="badge discovered">Discovered</span><span>A source this scan found or was given: it passed the automated gate — reachability, robots.txt, a terms scan and a parse test — and only that evidence, shown below, stands behind it.</span></div>'
      + lastrun
      // Before the venue list, not after it: what counts as the subject shapes the ledger as much
      // as which venues are read, and a reader who scrolls no further has still been told.
      + '<div class="grp">Subject filter</div>' + subjectPanelHTML()
      + '<div class="grp">Approved · ' + C.approved.length + (C.gated ? ' · ' + (C.approved.length - C.gated) + ' read this run' : '') + '</div>' + uncovered + (approved || none)
      + '<div class="grp">Pending a human decision · ' + C.pending.length + '</div>' + (pending || none)
      + '<div class="grp">Rejected · ' + C.rejected.length + '</div>' + (rejected || none)
      + discovery
      + '<div class="foot">Scans run when a person presses Run scan. Nothing here is scheduled.</div>';
  }

  // ---- lane tables -----------------------------------------------------------------------------
  const isUnread = it => !state.read[it.id] && !state.arch[it.id];
  const LV = { high: 0, medium: 1, low: 2 };
  // One shell per lane, built once; drawLane fills the triage tabs, the rows and the empty state.
  function laneShell(d) {
    const l = d.k, low = d.label.toLowerCase();
    return '<div class="viewhead"><div class="eyebrow">' + esc(d.label) + '</div>'
      + '<p class="sub">' + esc(d.sub) + '</p></div>'
      + '<div class="toolbar"><div class="ttabs" role="tablist" id="ttabs-' + l + '"></div>'
      + '<div class="tools"><input type="search" id="q-' + l + '" placeholder="Search ' + esc(low) + '" aria-label="Search ' + esc(low) + '">'
      + '<select id="sort-' + l + '" aria-label="Sort ' + esc(low) + '"><option value="newest">Newest</option><option value="relevance">Relevance</option></select></div></div>'
      + '<div class="tablewrap" id="wrap-' + l + '"><table class="dev"><colgroup><col><col style="width:112px"><col style="width:124px"><col style="width:180px"><col style="width:176px"><col style="width:112px"></colgroup>'
      + '<thead><tr><th>Development</th><th>Relevance</th><th>Type</th><th>Topics</th><th>Source</th><th>Jurisdiction</th></tr></thead>'
      + '<tbody id="tb-' + l + '"></tbody></table></div><div id="empty-' + l + '"></div>';
  }
  function visible(pool, st) {
    let list = pool.filter(it => st.tab === 'archived' ? state.arch[it.id] : st.tab === 'starred' ? state.star[it.id] && !state.arch[it.id] : st.tab === 'unread' ? isUnread(it) : !state.arch[it.id]);
    if (st.q) list = list.filter(it => [it.title, it.headline, it.type, it.domain, it.jurisdiction, it.topics.join(' '), it.summary.map(s => s.text).join(' ')].join(' ').toLowerCase().includes(st.q));
    const newest = (a, b) => (b.date || '').localeCompare(a.date || '') || (b.first_seen || '').localeCompare(a.first_seen || '');
    list.sort(st.sort === 'relevance' ? ((a, b) => (LV[a.relevance.level] - LV[b.relevance.level]) || newest(a, b)) : newest);
    return list;
  }
  function relHTML(r) {
    const level = typeof r === 'string' ? r : r.level, unrated = typeof r === 'object' && r.unrated;
    return '<span class="rel ' + esc(level) + (unrated ? ' unrated' : '') + '"' + (unrated ? ' title="The enricher recorded no relevance level for this development."' : '') + '><span class="bars"><i></i><i></i><i></i></span><span class="lv">' + (unrated ? 'unrated' : esc(level)) + '</span></span>';
  }
  // Sources chip = the venue's kind from discovery (Gov, Gazette, Court...), as Harvey's column
  // does; the host is the fallback when nothing classified the venue, and stays in the tooltip.
  function kindChip(kind, tier, host) {
    const label = KL[kind] || host || '?';
    const title = (tier === 'vetted' ? 'Vetted source: adapter, fixture, floor and legal analysis' : 'Discovered source: passed the automated gate; evidence on the coverage panel') + (host ? ' — ' + host : '');
    return '<span class="chip" title="' + esc(title) + '"><span class="mark ' + (tier === 'vetted' ? 'v' : 'd') + '">' + (tier === 'vetted' ? '&#10003;' : '&#9678;') + '</span>' + esc(label) + '</span>';
  }
  function srcChip(it) { return kindChip(it.kind, it.tier, it.domain); }
  function rowHTML(it) {
    return '<tr class="r' + (state.read[it.id] ? ' read' : '') + '" tabindex="0" data-id="' + esc(it.id) + '" aria-label="' + esc(it.title) + '">'
      + '<td><div class="t"><span class="un" aria-hidden="true"></span><span>' + esc(it.title) + (state.star[it.id] ? '<span class="star" aria-label="starred">&#9733;</span>' : '') + '</span></div>'
      + (it.headline ? '<div class="h">' + esc(it.headline) + '</div>' : '') + '<div class="w"><b>' + esc(rel(it.date || it.first_seen)) + '</b> &middot; ' + esc(it.domain) + '</div></td>'
      + '<td>' + relHTML(it.relevance) + '</td>'
      // An untyped row is in Signals because nothing has typed it yet, not because it is a signal.
      + '<td>' + (it.type ? '<span class="chip">' + esc(it.type) + '</span>'
          : '<span class="chip untyped" title="No type was recorded, so the lane rule parked this development in Signals. It moves when the enricher types it.">untyped</span>') + '</td>'
      + '<td><div class="chips">' + it.topics.map(t => '<span class="chip">' + esc(t) + '</span>').join('') + '</div></td>'
      + '<td>' + srcChip(it) + '</td>'
      + '<td>' + flagged(it.jurisdiction) + '</td></tr>';
  }
  // A lane with nothing in it must say what would be there and why nothing is: an empty table is
  // otherwise indistinguishable from a broken one, and from a scan that read nothing at all.
  function laneEmptyHTML(l, st, counts) {
    const d = LANE_BY[l], pool = laneItems[l];
    const would = '<p>What lands here: ' + d.types.map(t => '<b>' + esc(t) + '</b>').join(', ') + '.</p>';
    let why;
    if (st.q && pool.length) why = 'Nothing in this lane matches &ldquo;' + esc(st.q) + '&rdquo;. The lane holds ' + esc(pl(counts.all, 'development')) + '; clear the search to see them.';
    else if (pool.length && st.tab !== 'all') why = 'The lane holds ' + esc(pl(counts.all, 'development')) + ', but none is ' + (st.tab === 'unread' ? 'unread &mdash; you have opened them all' : st.tab === 'starred' ? 'starred in this browser' : 'archived') + '. Switch to <b>All</b>.';
    else if (!D.generated) why = 'This scan has not run yet. Press <b>Run scan</b>; nothing runs on a schedule.';
    else if (!items.length) why = 'The last run put nothing at all in the ledger. The <b>Coverage</b> tab says what each source returned, and the <b>Audit</b> tab shows the silence source by source so you can check it.';
    else why = 'The last run produced ' + esc(pl(items.length, 'development')) + ', and none of them is of these types. They are in ' + LANE_DEFS.filter(x => x.k !== l && laneItems[x.k].length).map(x => '<b>' + esc(x.label) + '</b>').join(' and ') + '.';
    return '<div class="laneempty"><h4>Nothing in ' + esc(d.label) + ' right now.</h4>' + would + '<p class="why">' + why + '</p>'
      + '<p>A development can only reach this lane from a source on the <b>Coverage</b> tab. The <b>Miscellaneous</b> tab is the one place this page shows anything from outside that list, and nothing there is a citable instrument.</p></div>';
  }
  function drawLane(l) {
    const st = lstate[l], pool = laneItems[l];
    const counts = { all: pool.filter(it => !state.arch[it.id]).length, unread: pool.filter(isUnread).length,
                     starred: pool.filter(it => state.star[it.id] && !state.arch[it.id]).length, archived: pool.filter(it => state.arch[it.id]).length };
    $('#ttabs-' + l).innerHTML = [['all', 'All'], ['unread', 'Unread'], ['starred', 'Starred'], ['archived', 'Archived']]
      .map(([k, lab]) => '<button type="button" role="tab" data-tab="' + k + '" class="' + (st.tab === k ? 'on' : '') + '" aria-selected="' + (st.tab === k) + '">' + lab + '<span class="k">' + counts[k] + '</span></button>').join('');
    const list = visible(pool, st);
    $('#tb-' + l).innerHTML = list.map(rowHTML).join('');
    $('#wrap-' + l).hidden = !list.length;
    $('#empty-' + l).innerHTML = list.length ? '' : laneEmptyHTML(l, st, counts);
  }
  // Every lane the reader is not looking at still needs its counts refreshed when triage changes.
  function drawAll() { drawTabs(); LANE_DEFS.forEach(d => { if (state.view === d.k) drawLane(d.k); }); if (state.view === 'clients') drawClients(); }

  // ---- Miscellaneous ---------------------------------------------------------------------------
  // The one lane that looks outside the gated coverage list, and therefore the one that has to be
  // hardest about what it is not. Nothing here was fetched by the tracker: these are a hosted
  // web-search tool's results, read and linked out. No robots.txt was consulted for these hosts
  // because we never asked them for anything; no terms were checked; nothing is citable; nothing
  // is in the ledger. Promote is the ONLY route from here into coverage, and it is a request to
  // the same Python gate that judges every other source, not a decision.
  const MISC_GROUPS = [
    { k: 'official_venue', label: 'Official venues this scan does not cover',
      sub: 'The valuable case: a regulator, gazette, court or ministry that publishes binding material on this brief and is not on the coverage list. Promote adds its URL to the scan\'s sources as <b>pending</b> &mdash; that is all it does. The gate &mdash; robots.txt, the site\'s own terms, a parse test &mdash; runs at the start of the <b>next run</b>, so press <b>Run scan</b> after promoting. Until it passes, nothing is ever fetched from it.', promote: true },
    { k: 'secondary', label: 'Secondary reports',
      sub: 'Press, trade bodies and law-firm notes reporting on something official. Useful as a pointer to the primary source; never a substitute for it, and never promotable &mdash; a report about an instrument is not a venue that publishes instruments.' },
    { k: 'commentary', label: 'Commentary',
      sub: 'Analysis and opinion. Context for a partner, evidence for nothing.' },
  ];
  // What the LAST search still stands behind: not dismissed, not already promoted into coverage,
  // and still returned by the search. The tab count is this set; the page shows every row.
  function isLiveFinding(f) {
    return f.status !== 'dismissed' && f.status !== 'promoted' && !state.promoted[f.id] && !f.stale;
  }
  function miscRow(f, canPromote) {
    const promoted = f.status === 'promoted' || !!state.promoted[f.id];
    const meta = [f.host, f.date ? fmt(f.date) : 'no date given', f.jurisdiction ? flagged(f.jurisdiction) : ''].filter(Boolean).join(' &middot; ');
    return '<div class="mrow' + (f.stale ? ' stale' : '') + '"><div>'
      // rel="nofollow noopener" and a new tab: we are pointing at an unvetted page, not endorsing it.
      + '<div class="mt"><a href="' + esc(f.url) + '" target="_blank" rel="noopener nofollow">' + esc(f.title) + ' &#8599;</a></div>'
      + '<div class="mm">' + meta + '</div>'
      // A stale finding is one the last search did not return. It is kept on purpose (design §2:
      // a lead does not cease to exist because a search engine changed its mind) — but a kept lead
      // that looks identical to a fresh hit is a claim quietly ageing on the page, so it is marked
      // on the row rather than only counted somewhere else.
      + (f.stale ? '<div class="mstale">Not in the last search'
          + (f.last_seen ? '; last returned ' + esc(stampText(f.last_seen)) : '')
          + '. Kept, not dropped &mdash; but the lead may have moved or been withdrawn, so verify it at the source before relying on it.</div>' : '')
      + (f.why ? '<div class="mw">' + esc(f.why) + '</div>' : '<div class="mw">No reason was recorded for surfacing this.</div>')
      + (f.snippet ? '<div class="ms">' + esc(f.snippet) + '</div>' : '')
      + '</div><div class="mact">'
      + (canPromote && !promoted && f.status !== 'dismissed'
          ? '<button type="button" class="btn sm" data-promote="' + esc(f.id) + '">Promote to coverage</button>'
          : '')
      + '<span class="mst' + (promoted ? ' promoted' : '') + (f.stale && !promoted ? ' stale' : '') + '">'
        + esc(promoted ? 'promotion requested' : f.status === 'dismissed' ? 'dismissed' : f.stale ? 'kept from an earlier search' : 'lead') + '</span>'
      // Reviewed defect: this said "the gate decides" the moment a promotion was requested, which
      // read as though something were being judged. Nothing is: promote appends the URL as a
      // pending source, and the gate runs at the start of the next run.
      + (promoted ? '<span class="mst">pending — gated on the next run</span>' : '')
      + '</div></div>';
  }
  function miscHTML() {
    const head = '<div class="viewhead"><div class="eyebrow">Miscellaneous</div>'
      + '<h3>An open-web search, outside this scan\'s gated coverage list</h3></div>'
      + '<div class="miscwarn"><h4>Read this before you use anything below</h4>'
      + '<p>Every other tab on this page is built from the sources on <b>Coverage</b>. This one is not. It is a search of the open web for things happening <b>outside</b> that list, and it exists because a coverage list you can see is also a coverage list with edges.</p>'
      + '<p><b>The tracker fetched none of these pages.</b> We read a hosted search tool\'s results and link out. Because we never requested these hosts, no robots.txt was consulted for them and no terms were checked &mdash; and none of that is a gap to be fixed here, because nothing on this tab is ever read, stored, quoted or ledgered.</p>'
      + '<p><b>Nothing here is a citable instrument.</b> There is no verified quote, no obligation, no relevance rating and no audit trail behind any row. Each one is a lead: open it, and verify it at its primary source.</p>'
      + '<p><b>Promote is the only route from this lane into coverage.</b> It adds an official venue\'s URL to this scan\'s sources as <b>pending</b>, and nothing more: the same gate that judged every other source judges it at the start of the <b>next run</b>, so promoting and then pressing <b>Run scan</b> are two steps, not one. If it fails, it appears on <b>Coverage</b> as rejected, with the reason. Nothing is fetched from a promoted venue until it has passed.</p></div>';
    if (!MISC.present) {
      return head + '<div class="laneempty"><h4>No open-web search has been run for this scan.</h4>'
        + '<p>This lane appears once a run has written one. A scan created before the lane existed has none, and that is a different fact from a search that found nothing &mdash; so this page will not pretend to have looked.</p>'
        + '<p>Press <b>Run scan</b> to have the next run search.</p></div>';
    }
    const q = MISC.query || {};
    const qline = '<div class="miscq">Searched: <b>' + esc(q.intent || S.intent || 'this scan\'s brief') + '</b>'
      + (q.jurisdictions && q.jurisdictions.length ? '<br>Jurisdictions: <b>' + q.jurisdictions.map(flagged).join(', ') + '</b>' : '')
      + (q.topics && q.topics.length ? '<br>Topics: <b>' + q.topics.map(esc).join(', ') + '</b>' : '')
      // The excluded hosts ARE the coverage list: saying so is what makes "outside the list" checkable.
      + (q.excluded_hosts && q.excluded_hosts.length ? '<br>Excluded (already covered): <b>' + q.excluded_hosts.map(esc).join(', ') + '</b>' : '')
      + (MISC.generated ? '<br>Last searched: <b>' + esc(stampText(MISC.generated)) + ' IST</b>' : '')
      + '</div>'
      + (MISC.notes.length ? '<ul class="problems">' + MISC.notes.map(n => '<li>' + esc(n) + '</li>').join('') + '</ul>' : '');
    if (!MISC.findings.length) {
      return head + qline + '<div class="laneempty"><h4>The search found nothing outside the coverage list.</h4>'
        + '<p>That is an answer, not a failure: the last open-web pass returned no official venue, no secondary report and no commentary this scan does not already cover.</p></div>';
    }
    const groups = MISC_GROUPS.map(g => {
      const rows = MISC.findings.filter(f => f.kind === g.k);
      // Same rule as the tab count: the headline number is what the last search stands behind, and
      // anything carried over or already dealt with is named beside it rather than folded in.
      const live = rows.filter(isLiveFinding).length, rest = rows.length - live;
      return '<div class="miscgrp"><div class="gh">' + esc(g.label) + ' &middot; ' + live
        + (rest ? ' live<span class="ghrest"> &middot; ' + rest + ' promoted, dismissed or kept from an earlier search</span>' : '') + '</div>'
        + '<p class="gsub">' + g.sub + '</p>'
        + (rows.length ? rows.map(f => miscRow(f, g.promote)).join('')
                       : '<div class="mnone">None in this search.</div>') + '</div>';
    }).join('');
    return head + qline + groups;
  }
  function drawMisc() {
    $('#v-misc').innerHTML = miscHTML();
  }
  $('#v-misc').addEventListener('click', async e => {
    const b = e.target.closest('button[data-promote]');
    if (!b) return;
    const f = MISC.findings.filter(x => x.id === b.dataset.promote)[0];
    if (!f) return;
    b.disabled = true; b.textContent = 'Asking…';
    // The whole finding travels, not just the URL: the workflow records what was promoted and why,
    // so the coverage panel can say where a source came from a year from now.
    // The id, never the record: /api/scans validates `finding` against ^[a-f0-9]{10}$ and the
    // workflow reads the rest out of misc.json by that id. Posting the object was a silent 400.
    const req = { action: 'promote', scan_id: S.id, finding: f.id };
    const res = await dispatchScan(req, 'add this venue to the scan’s sources');
    b.disabled = false; b.textContent = 'Promote to coverage';
    if (!res) return;
    state.promoted[f.id] = new Date().toISOString(); save();
    // Keyed by finding rather than by scan: a promotion is a separate errand from a run, and a
    // partner may promote two venues before either has landed.
    addPending({ id: S.id + '#' + f.id, scan_id: S.id, kind: 'promote', request: req,
                 verb: 'add this venue to the scan’s sources',
                 name: S.name + ' — ' + (f.host || hostOf(f.url)), dispatched_at: new Date().toISOString(),
                 action: 'promote', actionsUrl: res.actionsUrl || D.actionsUrl || '' });
    drawMisc();
  });

  // ---- Clients ----------------------------------------------------------------------------------
  // Per client, the developments that rate them, with the level, the why, the action line and the
  // Draft email that already exists on the detail panel. Relevance is rated per named client by the
  // enricher, so this view is a re-cut of the ledger, never a second judgement of it.
  function clientSection(name, scope, orphan) {
    // A per-client level the enricher wrote as something other than high/medium/low sorts last and
    // is shown unrated; NaN out of an unknown key would otherwise scramble the whole client's list.
    const clv = it => { const v = LV[String(it.relevance.clients[name] || '').toLowerCase()]; return v == null ? 3 : v; };
    const rows = items.filter(it => it.relevance.clients && it.relevance.clients[name])
      .sort((a, b) => (clv(a) - clv(b)) || (b.date || '').localeCompare(a.date || ''));
    const body = rows.length ? rows.map(it => {
      const lvl = String(it.relevance.clients[name] || '').toLowerCase();
      return '<div class="clitem"><div class="cl-l">' + relHTML(LV[lvl] == null ? { level: 'low', unrated: true } : lvl) + '</div>'
        + '<div><div class="ct"><button type="button" data-dev="' + esc(it.id) + '">' + esc(it.headline || it.title) + '</button></div>'
        + '<div class="cm">' + esc(it.date ? fmt(it.date) : 'undated') + ' &middot; ' + esc(it.type || 'untyped') + ' &middot; ' + esc(LANE_BY[it.lane].label) + ' &middot; ' + flagged(it.jurisdiction) + ' &middot; ' + esc(it.domain) + '</div>'
        + (it.relevance.why ? '<div class="cw">' + esc(it.relevance.why) + '</div>' : '')
        + (it.relevance.action ? '<div class="ca">' + esc(it.relevance.action) + '</div>' : '<div class="cw" style="color:var(--faint)">No action line was written for this development.</div>')
        + '</div><div class="cbtn"><button type="button" class="btn sm" data-draft="' + esc(it.id) + '" data-client="' + esc(name) + '">Draft email</button></div></div>';
    }).join('') : '<div class="mnone">No development in this scan rates ' + esc(name) + '. Relevance is rated per named client on every run; an absence here means nothing read so far touches them.</div>';
    return '<div class="clsec"><div class="cn">' + esc(name)
      + (orphan ? '<span class="badge demo">not named on this scan</span>' : '')
      + '<span class="ccount">' + esc(pl(rows.length, 'development')) + '</span></div>'
      + (scope ? '<div class="cscope"><b>Scope</b>' + esc(scope) + '</div>' : '')
      + (orphan ? '<div class="cscope">A development rates this client, but the scan definition does not name them. Add them with <b>Edit</b>, or treat the rating as stale.</div>' : '')
      + body + '</div>';
  }
  function drawClients() {
    const head = '<div class="viewhead"><div class="eyebrow">Clients</div>'
      + '<p class="sub">Each client named on this scan, and the developments the enricher rated against them &mdash; the level, why, and the action line, which names the client because the scan does. Draft email is grounded on the stored summary, obligations and text; nothing is ever sent from here.</p></div>';
    if (!scanClients.length && !orphanClients.length) {
      $('#v-clients').innerHTML = head + '<div class="laneempty"><h4>This scan names no clients.</h4>'
        + '<p>Add them with <b>Edit</b>. Every run then rates each development against each client by name, and the action line says what that client should do.</p></div>';
      return;
    }
    $('#v-clients').innerHTML = head
      + scanClients.map(c => clientSection(c.name, c.scope, false)).join('')
      + orphanClients.map(n => clientSection(n, '', true)).join('');
  }
  $('#v-clients').addEventListener('click', e => {
    const d = e.target.closest('button[data-draft]');
    if (d) { const it = byId[d.dataset.draft]; if (it) draftEmail(it, d.dataset.client, d); return; }
    const b = e.target.closest('button[data-dev]');
    if (b) openDetail(b.dataset.dev, b);
  });

  // ---- Audit ------------------------------------------------------------------------------------
  // The tracker's own framing, link by link: every approved source, what it yielded on the last
  // run, and every development traced back to the source it came from. Silence is a claim, so a
  // source that yielded nothing is listed with the invitation to go and check it.
  let auditOpen = null;
  function auditItems(url) { return items.filter(it => it.source_url === url).sort((a, b) => (b.date || '').localeCompare(a.date || '')); }
  function auditSrc(s, group) {
    const open = auditOpen === s.url;
    const rows = auditItems(s.url);
    const h = s.health;
    const laneTag = { instruments: 'ins', judgments: 'jdg', signals: 'sig' };
    return '<div class="audsrc' + (open ? ' open' : '') + '" data-url="' + esc(s.url) + '">'
      + '<div class="ah" tabindex="0" role="button" aria-expanded="' + open + '">'
      + '<span class="dotc ' + esc(h ? h.status : '') + '" title="' + esc(h ? h.status : 'not run') + '"></span>'
      + '<div class="an"><b>' + esc(s.name) + '</b>' + (s.jurisdiction ? ' ' + flagged(s.jurisdiction) : '')
      + '<span class="ahost">' + esc(s.url) + '</span></div>'
      + '<div class="acount">' + esc(group) + ' &middot; ' + (rows.length ? esc(pl(rows.length, 'development')) : 'nothing') + '</div>'
      + '</div><div class="abody">'
      + '<div class="ameta"><a class="alink" href="' + esc(s.url) + '" target="_blank" rel="noopener">Open the live listing we fetch &#8599;</a></div>'
      + '<div class="ameta">Gate: ' + esc(s.evidence) + (s.checked ? ' &middot; checked ' + esc(fmt(s.checked)) : '') + '</div>'
      + (h ? '<div class="ameta">Last run: ' + esc(h.status)
            + (h.rows_seen == null ? '' : ' &middot; ' + esc(h.rows_seen) + ' rows parsed')
            + (h.new == null ? '' : ' &middot; ' + esc(h.new) + ' new')
            + (h.newest_visible ? ' &middot; newest item the venue shows: ' + esc(fmt(h.newest_visible)) : '')
            + (h.checked ? ' &middot; ' + esc(rel(h.checked)) : '') + '</div>'
          // The filter's own account for THIS link. Someone auditing a suspected miss compares the
          // live listing with the rows below; if a filter dropped rows between the two, that is
          // the first thing they need to know, and it belongs here rather than only on Coverage.
          + (h.status !== 'GATED'
              ? (h.subject
                  ? '<div class="ameta">Subject filter: ' + esc(h.subject.dropped == null ? '—' : h.subject.dropped)
                    + ' row(s) dropped as not this scan’s subject &middot; ' + esc(h.subject.kept_terse == null ? '—' : h.subject.kept_terse)
                    + ' row(s) kept because the title was too terse to judge'
                    + (h.subject.titles && h.subject.titles.length ? '<br>kept: ' + h.subject.titles.map(esc).join('; ') : '') + '</div>'
                  : SUBJ.on ? '<div class="ameta">Subject filter: no counts recorded for this source on the last run &mdash; the number of rows it dropped here is unknown.</div>'
                  : '<div class="ameta">No subject filter: every row this link published in the window was ledgered.</div>')
              : '')
          + (h.notes.length ? '<div class="ameta">' + h.notes.map(esc).join('<br>') + '</div>' : '')
          + (h.info.length ? '<div class="ameta">' + h.info.map(esc).join('<br>') + '</div>' : '')
        : '<div class="ameta">No run has touched this source yet.</div>')
      // Nothing may enter the ledger from a source the gate did not approve (design §1.1). If rows
      // are listed under a pending or rejected link, the ledger and the definition have drifted —
      // say so above the rows rather than let the list read as ordinary coverage.
      + (rows.length && group !== 'approved'
          ? '<div class="anone bad">' + esc(pl(rows.length, 'development')) + ' in the ledger name this link, but the gate has it as ' + esc(group) + ' — nothing should ever have been read from it. The ledger and the coverage list have drifted apart; re-run the scan.</div>'
          : '')
      + (rows.length
          ? rows.map(it => '<div class="aitem"><span class="ad">' + esc(it.date ? fmt(it.date) : '\u2014') + '</span>'
              + '<span class="al" title="' + esc(LANE_BY[it.lane].label) + '">' + laneTag[it.lane] + '</span>'
              + '<span><button type="button" data-dev="' + esc(it.id) + '">' + esc(it.title) + '</button>'
              + (it.url ? ' <a href="' + esc(it.url) + '" target="_blank" rel="noopener" title="Open the document at the venue">&#8599;</a>' : '') + '</span></div>').join('')
          : (h && h.status === 'FAILED'
              ? '<div class="anone bad">This source FAILED on the last run &mdash; we could not read the venue, so this is blind, not silent. Anything it published since is unverified until it recovers.</div>'
              : group === 'approved'
                ? '<div class="anone">Nothing entered the ledger from this link. That is a claim, and it is checkable: open the live listing above and confirm the venue really published nothing new in the window. If it did, we missed it &mdash; say so.</div>'
                : '<div class="anone">Nothing has ever been fetched from this link, because the gate did not approve it. Its verdict and the reason are on the Coverage tab.</div>'))
      + '<div class="averify">Audit check: open the source link &rarr; list what the venue shows for the window &rarr; compare with the rows above.</div>'
      + '</div></div>';
  }
  function drawAudit() {
    const C = D.coverage;
    const known = {};
    [].concat(C.approved, C.pending, C.rejected).forEach(s => { known[s.url] = true; });
    // A development whose source_url matches no source on the definition is the one thing this
    // page must never round off: the ledger's own invariant says it cannot happen, so if it has,
    // it gets its own section rather than being quietly attributed to nobody.
    const orphans = items.filter(it => !known[it.source_url]);
    const head = '<div class="viewhead"><div class="eyebrow">Audit &mdash; verify us</div>'
      + '<p class="sub">Link by link from Coverage: every source this scan holds, what it yielded on the last run, and every development traced back to the link it came from. Sources that yielded nothing are listed too &mdash; silence must be checkable, not hidden. Open a source, open its live listing, and compare.</p>'
      // A partner auditing for a miss is comparing a venue's listing with this scan's rows. If a
      // filter stands between the two, the comparison is meaningless until they know it — and if
      // no filter stands there, the absence is just as load-bearing and is said just as plainly.
      + '<p class="sub">' + (SUBJ.on
          ? '<b>A subject filter stands between these venues and the ledger.</b> A row whose title does not match it was never fetched, read or ledgered, so a row you find on a venue and not below may have been dropped here rather than missed. The pattern, the sentence behind it and the per-source counts are on <b>Coverage</b>, and each source below carries its own count.'
          : '<b>This scan has no subject filter, so every row every source publishes is ledgered.</b> Nothing was dropped for being off-subject: anything a venue listed in the window and this scan does not show below is a miss, not a filter.')
        + '</p></div>';
    const sec = (label, list, group) => '<div class="miscgrp"><div class="gh">' + esc(label) + ' &middot; ' + list.length + '</div>'
      + (list.length ? list.map(s => auditSrc(s, group)).join('') : '<div class="mnone">None.</div>') + '</div>';
    $('#v-audit').innerHTML = head
      + sec('Approved \u2014 fetched on every run', C.approved, 'approved')
      + sec('Pending a human decision \u2014 never fetched', C.pending, 'pending')
      + sec('Rejected \u2014 never fetched', C.rejected, 'rejected')
      + (orphans.length
          ? '<div class="miscgrp"><div class="gh">Not traceable to a source &middot; ' + orphans.length + '</div>'
            + '<p class="gsub">These developments name a source URL this scan\'s definition does not list. Nothing should ever be in this group; if something is, the ledger and the definition have drifted apart and the scan needs re-running.</p>'
            + orphans.map(it => '<div class="aitem"><span class="ad">' + esc(it.date ? fmt(it.date) : '\u2014') + '</span><span class="al">?</span>'
                + '<span><button type="button" data-dev="' + esc(it.id) + '">' + esc(it.title) + '</button> <span style="font-family:var(--mono);font-size:10.5px;color:var(--faint)">' + esc(it.source_url || 'no source_url') + '</span></span></div>').join('')
            + '</div>'
          : '')
      + '<div class="averify" style="margin-top:22px">Every development on this page came from exactly one link above. A source not on this list cannot contribute anything &mdash; except the Miscellaneous tab, which contributes nothing to the ledger at all.</div>';
  }
  $('#v-audit').addEventListener('click', e => {
    const b = e.target.closest('button[data-dev]');
    if (b) { openDetail(b.dataset.dev, b); return; }
    const h = e.target.closest('.ah');
    if (h && !e.target.closest('a')) { const u = h.parentElement.dataset.url; auditOpen = auditOpen === u ? null : u; drawAudit(); }
  });
  $('#v-audit').addEventListener('keydown', e => {
    const h = e.target.closest('.ah');
    if (h && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); const u = h.parentElement.dataset.url; auditOpen = auditOpen === u ? null : u; drawAudit(); }
  });

  // ---- detail slide-over ---------------------------------------------------------------------
  const panel = $('#panel'), scrim = $('#scrim');
  function openDetail(id, from) {
    const it = byId[id]; if (!it) return;
    lastFocus = from || document.activeElement; openId = id;
    if (!state.read[id]) { state.read[id] = true; save(); drawAll(); }
    panel.innerHTML = detailHTML(it);
    panel.classList.add('on'); scrim.classList.add('on'); panel.setAttribute('aria-hidden', 'false');
    wireCites(panel);
    $('.pclose', panel).addEventListener('click', closeDetail);
    $('#p-star').addEventListener('click', () => { state.star[id] = !state.star[id]; if (!state.star[id]) delete state.star[id]; save(); $('#p-star').textContent = state.star[id] ? '★ Starred' : '☆ Star'; $('#p-star').classList.toggle('on', !!state.star[id]); drawAll(); });
    $('#p-arch').addEventListener('click', () => { state.arch[id] = !state.arch[id]; if (!state.arch[id]) delete state.arch[id]; save(); $('#p-arch').textContent = state.arch[id] ? 'Unarchive' : 'Archive'; drawAll(); });
    $('#p-draft').addEventListener('click', () => draftEmail(it, clientName(S.clients[0]), $('#p-draft')));
    $('#p-askb').addEventListener('click', () => { const a = $('#p-ask'); a.classList.toggle('on'); if (a.classList.contains('on')) $('#p-q').focus(); });
    $('#p-askf').addEventListener('submit', e => { e.preventDefault(); ask(it); });
    panel.scrollTop = 0;
    $('.pclose', panel).focus();
  }
  function closeDetail() {
    panel.classList.remove('on'); scrim.classList.remove('on'); panel.setAttribute('aria-hidden', 'true'); hideHC();
    openId = null;
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus(); else { const r = $('#v-' + state.view + ' tr.r'); if (r) r.focus(); }
  }
  scrim.addEventListener('click', closeDetail);
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if ($('#hc').classList.contains('on')) { hideHC(); e.preventDefault(); return; }
    if (openId && !dlg.open && !modal.open) { closeDetail(); e.preventDefault(); }
  });
  // What state the document is in, said above the summary so an absent summary is never mistaken
  // for an empty document. Reviewed defect: queued, unreadable, given-up, enrichment-failed and
  // truncated rows all read "No summary was written." Wording mirrors run.py's own notes.
  // the header's one sentence about when this scan runs — true for THIS scan, never a slogan
function scheduleLine(S) {
  const sch = S && S.definition && S.definition.schedule;
  if (sch && sch.daily_at) {
    const who = sch.set_by ? ' &mdash; set by ' + esc(sch.set_by) + (sch.set_on ? ' on ' + esc(String(sch.set_on).slice(0, 10)) : '') : '';
    return '<span class="sched on">Runs daily at ' + esc(sch.daily_at) + ' ' + esc(sch.tz || '') + who + ', and whenever a person presses Run scan.</span>';
  }
  return 'Runs when a person presses Run scan &mdash; this scan is not scheduled.';
}
  function stateLine(it) {
    const MAX = 3;
    if (!it.enriched) {
      if (it.enrich_error) return { text: 'Enrichment failed (attempt ' + it.read_attempts + ' of ' + MAX + '): ' + it.enrich_error + '. The text was read and stored; the model call did not succeed. ' + (it.read_attempts >= MAX ? 'Given up — no further retries; the stored text is still available to Ask.' : 'Retried on the next run.') };
      if (it.read_error && /^robots\.txt/i.test(it.read_error)) return { text: 'Not fetched — ' + it.read_error + '. The document is not read while the site\'s robots.txt says no; nothing below comes from it.' };
      if (it.read_error && it.read_attempts >= MAX) return { text: 'Metadata only — given up after ' + MAX + ' attempts: ' + it.read_error + '. Nothing below comes from the document itself.' };
      if (it.read_error) return { text: 'Document could not be read (attempt ' + it.read_attempts + ' of ' + MAX + '): ' + it.read_error + '. Retried on the next run.' };
      return { text: 'Not yet read — queued beyond max_new_per_run this run; the next run continues from the newest.', quiet: true };
    }
    if (it.truncated) return { text: 'Text truncated at 30,000 characters (max_doc_chars) — Ask and the citations cover only that part of the document.' };
    if (!it.has_text && it.summary.length) return { text: 'The stored text for this document is missing from the build, so the citations below could not be re-checked here.', quiet: true };
    return null;
  }
  function detailHTML(it) {
    const n = it.summary.length;
    const idx = it.summary.map((s, i) => citeChip(it.id + ':' + i, i + 1, 'quote', { unverified: !s.verified })).join('');
    const clients = Object.keys(it.relevance.clients || {});
    const st = stateLine(it);
    // Confidence carries the enricher's own verification ratio and note beside the level, so
    // "medium" next to "1 of 3 quotes verified · citations could not be verified" reads as one fact.
    const nv = it.summary.filter(s => s.verified).length;
    const conf = [it.confidence ? '<span class="chip">' + esc(it.confidence) + '</span>' : '—']
      .concat(n ? ['<span style="color:var(--mute)">' + nv + ' of ' + n + ' quote' + (n === 1 ? '' : 's') + ' verified' + (typeof it.verified_ratio === 'number' && Math.round(it.verified_ratio * n) !== nv ? ' here (enricher: ' + Math.round(it.verified_ratio * 100) + '%)' : '') + '</span>'] : [])
      .concat(it.note ? ['<span style="color:#7A5E0E">' + esc(it.note) + '</span>'] : []).join(' · ');
    return '<div class="ph"><div class="sub">' + (it.date ? esc(fmt(it.date)) : 'undated') + ' · ' + esc(it.domain) + (S.demo ? ' <span class="badge demo">Demo — fixture data</span>' : '') + (n ? ' ' + idx : '') + '</div><button type="button" class="pclose" aria-label="Close">×</button></div>'
      + '<div class="pb"><h2>' + esc(it.title) + '</h2>' + (it.headline ? '<p class="s" style="color:var(--mute);margin-top:0">' + esc(it.headline) + '</p>' : '')
      + '<div class="kv">'
      + kv('Type', it.type ? '<span class="chip">' + esc(it.type) + '</span>' : '—')
      + kv('Topics', it.topics.length ? '<div class="chips">' + it.topics.map(t => '<span class="chip">' + esc(t) + '</span>').join('') + '</div>' : '—')
      // extract.document_text labels a vision-transcribed PDF read_as "scan" (reviewed defect: the
      // page only knew the sample's "vision", so the chip never showed on a real scan).
      + kv('Source', srcChip(it) + (it.read_as === 'scan' || it.read_as === 'vision' ? ' <span class="chip" title="The document had no text layer; it was read from page images.">read by vision</span>' : ''))
      + kv('Jurisdiction', flagged(it.jurisdiction) + (NAMES[it.jurisdiction] ? ' <span style="color:var(--mute)">' + esc(countryName(it.jurisdiction)) + '</span>' : ''))
      + kv('Confidence', conf)
      + kv('First seen', it.first_seen ? esc(fmt(it.first_seen)) : '—')
      + '</div>'
      + (st ? '<div class="state' + (st.quiet ? ' quiet' : '') + '" id="p-state">' + esc(st.text) + '</div>' : '')
      + '<h4>Summary</h4>' + (n ? it.summary.map((s, i) => '<p class="s' + (s.verified ? '' : ' unv') + '">' + esc(s.text) + citeChip(it.id + ':' + i, i + 1, 'quote', { unverified: !s.verified }) + (s.verified ? '' : ' <span class="badge unverified" title="The cited quote could not be found in the stored text.">unverified</span>') + '</p>').join('') : '<p class="s" style="color:var(--faint)">No summary was written.</p>')
      + (it.obligations.length ? '<h4>Obligations</h4><table class="obl"><thead><tr><th>Who</th><th>What</th><th>When</th></tr></thead><tbody>' + it.obligations.map(o => '<tr><td>' + esc(o.who) + '</td><td>' + esc(o.what) + '</td><td>' + esc(o.when) + '</td></tr>').join('') + '</tbody></table>' : '<h4>Obligations</h4><p class="s" style="color:var(--faint)">None concrete in this document.</p>')
      + '<h4>Relevance</h4><div class="relbox"><div class="lvl">' + relHTML(it.relevance) + '</div>'
      + (it.relevance.unrated ? '<p class="why">No relevance level was recorded for this development; read it before relying on its position in the table.</p>' : '')
      + (it.relevance.why ? '<p class="why">' + esc(it.relevance.why) + '</p>' : '')
      + (it.relevance.action ? '<div class="act">' + esc(it.relevance.action) + '</div>' : '')
      + (clients.length ? '<div class="cl">' + clients.map(c => '<span class="chip t">' + esc(c) + ' <span class="mark d">' + esc(it.relevance.clients[c]) + '</span></span>').join('') + '</div>' : '') + '</div>'
      + '<div class="pactions"><button type="button" class="btn primary" id="p-draft">Draft email</button><button type="button" class="btn" id="p-askb" aria-expanded="false">Ask</button>'
      + '<button type="button" class="btn' + (state.star[it.id] ? ' on' : '') + '" id="p-star">' + (state.star[it.id] ? '★ Starred' : '☆ Star') + '</button>'
      + '<button type="button" class="btn" id="p-arch">' + (state.arch[it.id] ? 'Unarchive' : 'Archive') + '</button>'
      + (it.url ? '<a class="btn quiet" href="' + esc(it.url) + '" target="_blank" rel="noopener">Open source ↗</a>' : '') + '</div>'
      + '<div class="ask" id="p-ask"><form class="in" id="p-askf"><input type="text" id="p-q" placeholder="Ask this document a question" maxlength="1000" autocomplete="off"><button type="submit" class="btn">Ask</button></form><div class="out" id="p-out"></div></div>'
      + '</div>';
  }
  function kv(k, v) { return '<div class="row"><div class="k">' + k + '</div><div>' + v + '</div></div>'; }

  // ---- ask + draft ---------------------------------------------------------------------------
  async function ask(it) {
    const q = $('#p-q').value.trim(), out = $('#p-out'); if (!q) return;
    out.innerHTML = '<span class="busy">Reading the stored text…</span>';
    let r;
    try { r = await postJSON(D.api.ask, { scan: S.id, dev: it.id, question: q, client: clientName(S.clients[0]) || undefined }); }
    catch (e) { out.innerHTML = '<span class="ng">unavailable</span><div>No Ask endpoint is reachable from this page.</div>'; return; }
    if (!r.ok) {
      out.innerHTML = '<span class="ng">' + (r.status === 501 || r.status === 404 ? 'not configured' : 'error ' + r.status) + '</span><div>' + esc(r.data.message || 'The Ask endpoint did not answer.') + '</div>';
      return;
    }
    const d = r.data;
    out.innerHTML = (S.demo ? '<div class="nt">' + esc(DEMO_NOTE) + '</div>' : '')
      + (d.grounded ? '' : '<span class="ng">Not grounded — the document does not answer this</span>')
      + '<div>' + esc(d.answer || '') + '</div>'
      + (d.passages || []).map(p => '<blockquote>' + esc(typeof p === 'string' ? p : (p.quote || p.text || '')) + '</blockquote>').join('')
      + ((d.notes || []).length ? '<div class="nt">' + d.notes.map(esc).join('<br>') + '</div>' : '');
  }
  // `client` and `btn` are passed by the Clients tab, which drafts for the client whose section
  // the button sits in; the detail panel passes its own button and the scan's first client, as it
  // always did. Reviewed defect this closes by construction: a per-client draft that silently
  // addressed the first client on the scan would be worse than no draft at all.
  async function draftEmail(it, client, btn) {
    const b = btn || $('#p-draft'); b.disabled = true; b.textContent = 'Drafting…';
    let r = null, err = null;
    try { r = await postJSON(D.api.draft, { scan: S.id, dev: it.id, client: client || undefined, kind: 'email' }); } catch (e) { err = e; }
    b.disabled = false; b.textContent = 'Draft email';
    // The tier and the demo mark ride with the text itself, because the text is what gets copied
    // out of this modal and forwarded.
    const provenance = ['Source tier: ' + (it.tier || 'discovered') + ' — ' + it.domain];
    const demoPrefix = S.demo ? DEMO_NOTE + '\n\n' : '';
    // api/draft.js answers {ok, subject, body, model, kind, notes} (contract). Reviewed defect: the
    // page looked for r.data.draft || r.data.text, found neither, and threw every paid-for draft
    // away in favour of the template "because the draft endpoint answered 200".
    if (r && r.ok && typeof r.data.body === 'string' && r.data.body.trim()) {
      showModal('Draft email', demoPrefix + (r.data.subject ? 'Subject: ' + r.data.subject + '\n\n' : '') + r.data.body,
        ['Drafted by the model' + (r.data.model ? ' (' + r.data.model + ')' : '') + ' from the stored summary, obligations and text. Verify before sending.'].concat(provenance, Array.isArray(r.data.notes) ? r.data.notes : []));
      return;
    }
    // Only a non-2xx answer, an unreachable endpoint, or a 200 with no body lands here; the reason
    // shown is the real one.
    const why = err ? 'no draft endpoint is reachable from this page'
      : (r.status === 501 || r.status === 404) ? 'the draft endpoint is not configured on this deployment (HTTP ' + r.status + ')'
      : r.ok ? 'the draft endpoint answered without a draft body'
      : 'the draft endpoint answered ' + r.status + (r.data.message ? ': ' + r.data.message : '');
    showModal('Draft email', demoPrefix + template(it, client), ['Deterministic template built from the record, because ' + why + '. Nothing was sent.'].concat(provenance));
  }
  function template(it, forClient) {
    const client = forClient || clientName(S.clients[0]) || '[Client]';
    const lines = ['Subject: ' + (it.jurisdiction ? countryName(it.jurisdiction) + ' — ' : '') + (it.headline || it.title), '', 'Dear ' + client + ' team,', '',
      (it.headline ? it.headline + ' ' : '') + 'The instrument is: ' + it.title + (it.date ? ' (' + fmt(it.date) + ')' : '') + '.', ''];
    it.summary.forEach((s, i) => lines.push('• ' + s.text + (s.where ? ' [' + s.where + ']' : '') + (s.verified ? '' : ' [citation unverified]')));
    if (it.obligations.length) { lines.push('', 'Obligations:'); it.obligations.forEach(o => lines.push('• ' + o.who + ': ' + o.what + (o.when ? ' — ' + o.when : ''))); }
    if (it.relevance.action) lines.push('', 'Recommended next step: ' + it.relevance.action);
    // The same trailer api/draft.js enforces in code: the copied text must say it is a draft and
    // was not sent (reviewed defect: the template ended at "Kind regards").
    lines.push('', 'Source (' + it.tier + ' — ' + it.domain + '): ' + it.url, '', 'Kind regards', '', TRAILER);
    return lines.join('\n');
  }

  // ---- hover cards ---------------------------------------------------------------------------
  const hc = $('#hc'); let hcTimer = null, hcAnchor = null;
  function wireCites(root) {
    $$('.cite', root).forEach(c => {
      c.addEventListener('mouseenter', () => showHC(c));
      c.addEventListener('focus', () => showHC(c));
      c.addEventListener('mouseleave', scheduleHide);
      c.addEventListener('blur', scheduleHide);
      c.addEventListener('click', e => { e.stopPropagation(); if (c.dataset.kind === 'dev') openDetail(c.dataset.ref, c); else showHC(c); });
    });
  }
  hc.addEventListener('mouseenter', () => clearTimeout(hcTimer));
  hc.addEventListener('mouseleave', scheduleHide);
  hc.addEventListener('focusout', e => { if (!hc.contains(e.relatedTarget)) scheduleHide(); });
  document.addEventListener('click', e => { if (!hc.contains(e.target) && !e.target.closest('.cite')) hideHC(); });
  function scheduleHide() { clearTimeout(hcTimer); hcTimer = setTimeout(() => { if (!hc.contains(document.activeElement) && !hc.matches(':hover')) hideHC(); }, 160); }
  function hideHC() { hc.classList.remove('on'); hcAnchor = null; }
  function showHC(c) {
    clearTimeout(hcTimer);
    const kind = c.dataset.kind, ref = c.dataset.ref;
    let html = '';
    if (kind === 'dev') {
      const it = byId[ref]; if (!it) return;
      html = '<div class="hh">' + esc(it.headline || it.title) + '</div><div class="hm">' + esc(it.date ? fmt(it.date) : 'undated') + ' · ' + esc(it.domain) + ' · ' + flagged(it.jurisdiction) + ' ' + srcChip(it) + '</div>'
        + (it.headline ? '<div class="hq" style="font-style:normal">' + esc(it.title) + '</div>' : '') + '<button type="button" class="btn sm" data-view="' + esc(it.id) + '">View</button>';
    } else {
      const [id, i] = ref.split(':'); const it = byId[id]; const s = it && it.summary[+i]; if (!s) return;
      html = '<div class="hh">' + (s.verified ? 'Verbatim passage' : 'Passage not found in the stored text' + (s.note ? ' — ' + esc(s.note) : '')) + '</div>'
        + (s.quote ? '<div class="hq">“' + esc(s.quote) + '”</div>' : '<div class="hq">No quote was recorded.</div>')
        + (s.where ? '<div class="hw">' + esc(s.where) + '</div>' : '') + (s.verified ? '' : '<div class="hw" style="color:#7A5E0E">unverified — read the source before relying on this sentence</div>');
    }
    hc.innerHTML = html; hcAnchor = c;
    const v = $('[data-view]', hc); if (v) v.addEventListener('click', () => { hideHC(); openDetail(v.dataset.view, c); });
    hc.classList.add('on');
    // Position after render so the card's real size is known; clamp inside the viewport and flip
    // above the chip when there is no room below — a card off-screen is a card nobody reads.
    const r = c.getBoundingClientRect(), w = hc.offsetWidth, h = hc.offsetHeight, pad = 12;
    let left = r.left + window.scrollX, top = r.bottom + window.scrollY + 8;
    if (r.left + w > window.innerWidth - pad) left = window.scrollX + Math.max(pad, window.innerWidth - pad - w);
    if (r.bottom + 8 + h > window.innerHeight - pad && r.top - h - 8 > pad) top = r.top + window.scrollY - h - 8;
    hc.style.left = left + 'px'; hc.style.top = top + 'px';
  }

  // First paint last: everything above is declared by now (the const bindings the table reads
  // are not hoisted, and a draw before them throws on the very first render).
  wireCites(main);
  // The hash wins over the remembered tab: it is what the auto-reload writes on its way out, so a
  // partner reading Miscellaneous when a run lands comes back to Miscellaneous.
  const hv = tabFromHash();
  if (hv && VIEWS.some(v => v.k === hv)) state.view = hv;
  setView(state.view);

  // ---- pending runs on this scan ---------------------------------------------------------------
  mountPending($('#pending'), {
    // Only this scan's errands. Another scan's create belongs on the Scans home, not here.
    mine: p => (p.scan_id || p.id) === S.id,
    builtNow: p => {
      const since = Date.parse(p.dispatched_at || 0);
      // A run lands as new developments (D.generated moves); a promotion lands as a changed
      // definition (S.updated moves) and no developments at all. Watching only one of the two
      // would leave the other card spinning until the 90-minute timeout.
      return Date.parse(D.generated || 0) > since || Date.parse(S.updated || 0) > since;
    },
    onLanded: p => {
      if (kindOf(p) === 'promote') {
        say('The promoted venue is in this scan’s sources now, as <b>pending</b> on <b>Coverage</b>. '
          + 'Nothing has been fetched from it: the gate judges it at the start of the next run — press <b>Run scan</b>.', 'warn');
      }
    },
  });
}
</script>
"""


def render_page(payload: dict, title: str, favicon_b64: str) -> str:
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE.replace("__DATA__", data_json).replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;"))
            .replace("__STAMP__", payload["stamp"]).replace("__FAVICON_SVG__", favicon_b64)
            .replace("__KIND_LABELS__", json.dumps(KIND_LABELS))
            .replace("__FIRST_RUN_MAX__", str(FIRST_RUN_MAX_NEW)))


# ----------------------------------------------------------------------------- build
def publish_data(scan: dict, out: Path) -> dict:
    """dist/data/scans/<id>/ carries the ledger and the text files so api/ask.js (and draft.js)
    can fetch them from the deployment's own origin — the only origin they are allowed to read."""
    src = scan["results_dir"]
    dst = out / "data" / "scans" / scan["definition"]["id"]
    copied = {"developments": False, "texts": 0}
    if (src / "developments.json").exists():
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "developments.json", dst / "developments.json")
        copied["developments"] = True
    if (src / "text").is_dir():
        (dst / "text").mkdir(parents=True, exist_ok=True)
        for t in sorted((src / "text").glob("*.txt")):
            shutil.copyfile(t, dst / "text" / t.name)
            copied["texts"] += 1
    return copied


def build(root: Optional[Path] = None, out: Optional[Path] = None) -> dict:
    root = root or scan_root()
    out = out or (ROOT / "dist")
    out.mkdir(parents=True, exist_ok=True)
    built = common.now_ist()
    icons = ROOT / "assets" / "favicon"
    if icons.exists():
        for f in icons.iterdir():
            if f.is_file():
                shutil.copyfile(f, out / f.name)
    svg = icons / "favicon.svg"
    fav = base64.b64encode(svg.read_bytes()).decode() if svg.exists() else ""

    scans = load_scans(root)
    written: list[Path] = []
    home = render_page(home_payload(scans, built), "Scans · TMT Regulatory Radar", fav)
    p = out / "scans.html"
    common.atomic_write_text(p, home)
    written.append(p)
    published = {}
    for s in scans:
        sid = s["definition"]["id"]
        page = render_page(scan_payload(s, built), f"{s['definition']['name']} · Scans", fav)
        p = out / "scan" / f"{sid}.html"
        common.atomic_write_text(p, page)
        written.append(p)
        published[sid] = publish_data(s, out)
    for p in written:
        if not p.exists() or p.stat().st_size < 20_000:
            raise SystemExit(f"build produced no usable page at {p} — refusing to exit 0")
    summary = {"scans": [s["definition"]["id"] for s in scans], "written": [str(p) for p in written],
               "published": published, "problems": {s["definition"]["id"]: s["problems"] for s in scans if s["problems"]}}
    for w in written:
        print(f"wrote {w} ({w.stat().st_size:,} bytes)")
    for sid, probs in summary["problems"].items():
        for pr in probs:
            print(f"[{sid}] {pr}")
    return summary


# ----------------------------------------------------------------------------- sample
def write_sample(dest: Path) -> Path:
    """A realistic demo scan with the exact shapes of docs/horizon-design.md §4, for looking at
    the page. Marked demo:true so the card says so; the texts are written so every citation
    verifies except the one that is meant not to."""
    sid = "eu-pay-transparency"
    now = common.now_ist()
    week = common.iso_week()

    def src(url, name, jur, status, rationale, gate, **kw):
        # Same keys gate.py writes (kind, confidence, proposed_by) so the page is exercised against
        # the real shape, not a tidier one.
        d = {"url": url, "name": name, "host": host_of(url), "jurisdiction": jur, "kind": kw.get("kind", "other"),
             "status": status, "tier": "discovered", "proposed_by": kw.get("proposed_by", "discovery"),
             "rationale": rationale, "confidence": kw.get("confidence", "high"), "gate": gate}
        if kw.get("reason"):
            d["reason"] = kw["reason"]
        return d

    def ok_gate(rows, dated, pages=1, **extra):
        g = {"reachable": True, "http": 200, "robots": "allowed",
             "tos": {"checked": [f"https://example/legal-{i}" for i in range(pages)], "flags": []},
             "extract": {"rows": rows, "dated": dated, "floor": 8}, "checked": now}
        if extra.get("robots_note"):
            g["robots_note"] = extra["robots_note"]
        if extra.get("tos_errors"):
            g["tos"]["errors"] = extra["tos_errors"]
        return g

    boe_flag = "Queda prohibida la extracción sistemática o automatizada de contenidos sin autorización expresa."
    sources = [
        src("https://www.gazzettaufficiale.it/ricerca/serie_generale", "Gazzetta Ufficiale — Serie Generale", "IT", "approved",
            "Official gazette; transposition decrees are published here.", ok_gate(31, 29), kind="gazette"),
        src("https://www.bmfsfj.de/bmfsfj/service/gesetze", "BMFSFJ — Gesetze und Referentenentwürfe", "DE", "approved",
            "The ministry leading transposition publishes its draft bills on this page.",
            ok_gate(24, 24, 2, robots_note="no robots.txt — treated as unrestricted per RFC 9309"), kind="ministry"),
        src("https://www.legifrance.gouv.fr/liste/jorf", "Légifrance — Journal officiel", "FR", "approved",
            "Official journal; decrees and arrêtés on pay reporting appear here.",
            ok_gate(40, 40, tos_errors=[{"url": "https://www.legifrance.gouv.fr/mentions-legales", "error": "RuntimeError: HTTP 404"}]),
            proposed_by="partner", kind="gazette", confidence=""),
        # Approved at create time, FAILED on this run (the site answered 503): the header must say
        # so, not only the collapsed panel.
        src("https://travail-emploi.gouv.fr/actualites", "Ministère du Travail — actualités", "FR", "approved",
            "Ministry news page announcing decrees before they reach the Journal officiel.", ok_gate(18, 16), kind="ministry", confidence="medium"),
        # Approved, but the fifth approved source on a run whose budget allows four: health says
        # GATED and the page must say "not fetched" rather than let the approval read as coverage.
        src("https://www.senato.it/leggi-e-documenti/disegni-di-legge", "Senato della Repubblica — disegni di legge", "IT", "approved",
            "Parliamentary bills amending the transposition decree would be listed here.", ok_gate(52, 50), kind="parliament", confidence="medium"),
        src("https://www.boe.es/diario_boe/", "BOE — Boletín Oficial del Estado", "ES", "pending",
            "Official gazette for royal decrees on pay registers.",
            {"reachable": True, "http": 200, "robots": "allowed",
             "tos": {"checked": ["https://www.boe.es/aviso_legal"], "flags": [{"url": "https://www.boe.es/aviso_legal", "text": boe_flag}]},
             "extract": {"rows": 27, "dated": 27, "floor": 8}, "checked": now},
            reason=f"ToS language found: '{boe_flag[:120]}' at https://www.boe.es/aviso_legal — needs a human read", kind="gazette"),
        # The exact shape gate.py writes for a robots refusal: nothing was fetched, so reachable is
        # False, http is null and extract is null (the parse test never ran).
        src("https://www.mites.gob.es/buscador", "Ministerio de Trabajo — buscador", "ES", "rejected",
            "Ministry search page listing circulars.",
            {"reachable": False, "http": None, "content_type": None, "robots": "disallowed", "tos": {"checked": [], "flags": []}, "extract": None, "checked": now},
            reason="robots.txt disallows /buscador for our agent", kind="ministry", confidence="medium"),
    ]
    defn = {
        "id": sid, "name": "EU Pay Transparency Directive Scan",
        "intent": "Advise multinational-employer clients on national transposition of the Pay Transparency Directive; surface new obligations, thresholds and deadlines by country.",
        "jurisdictions": ["DE", "FR", "IT", "ES"], "topics": ["Pay equity", "Employment"], "industries": [],
        # One string client and one {name, scope} client: both shapes the contract allows, so the
        # Edit dialog, Ask and Draft are exercised against the object form.
        "clients": ["Accenture", {"name": "Annalise.ai", "scope": "employees in Germany and France only"}],
        "sources": sources, "budget": {"max_sources": 4, "max_new_per_run": 10, "delay_seconds": 1.5},
        # The subject filter in the shape the create dialog sends and the run applies: a gazette
        # publishes everything a state publishes, and only these rows are this scan's subject.
        # The second sample scan deliberately has NONE, so both pages are exercised: one that says
        # what its filter dropped, and one that says it has no filter and ledgers everything.
        "subject_filter": {
            "regex": r"\b(pay transparency|pay gap|equal pay|gender pay|Entgelttransparenz|transparence salariale|parit(a|à) retributiva|2023/970)\b",
            "why": "Admits rows whose title names pay transparency, the pay gap or the Directive itself, in each jurisdiction's own language.",
            "source": "proposed"},
        "no_discover": False, "created": now, "updated": now, "demo": True,
    }

    def dev(title, url, src_url, jur, date, typ, headline, summary, obligations, level, why, action, **kw):
        # The ledger row as run.py + enrich.py write it: `enriched`/`enriched_at` from the run,
        # `verified` per PARAGRAPH from enrich.verify_citations (not inside cite — the old sample
        # put it there and hid a builder defect), verified_ratio and note from the enricher.
        d = {"id": common.short_id(title, date), "title": title, "url": url, "source_url": src_url, "tier": "discovered",
             "jurisdiction": jur, "date": date, "first_seen": kw.get("first_seen", common.today_ist()),
             "last_seen": common.today_ist(), "snippet": "",
             "doc_hash": hashlib.sha1(title.encode()).hexdigest(), "read_as": kw.get("read_as", "text"), "enriched": True,
             "type": typ, "topics": kw.get("topics", ["Pay equity", "Employment"]), "headline": headline,
             "summary": [{"text": t, "cite": {"quote": q, "where": w}, "verified": True} for t, q, w in summary],
             "obligations": obligations,
             "relevance": {"level": level, "why": why, "action": action,
                           "clients": {"Accenture": kw.get("client_level", level), "Annalise.ai": kw.get("client_level", level)}},
             "confidence": kw.get("confidence", "high"), "verified_ratio": 1.0 if summary else 0.0, "note": kw.get("note", ""),
             "enriched_at": now}
        if kw.get("truncated"):
            d["truncated"] = True
        d["text_file"] = f"text/{d['id']}.txt"
        return d

    GU, BM, LF = sources[0]["url"], sources[1]["url"], sources[2]["url"]
    devs = [
        dev("Decreto legislativo 5 agosto 2026, n. 118 — attuazione della direttiva (UE) 2023/970",
            "https://www.gazzettaufficiale.it/eli/id/2026/08/12/26G00130/sg", GU, "IT", "2026-08-12", "Legislation",
            "Italy transposes the Pay Transparency Directive; reporting from 100 employees.",
            [("Employers with 100 or more employees must publish gender pay-gap data annually from 2027.",
              "I datori di lavoro con almeno cento dipendenti pubblicano annualmente, a decorrere dal 1° gennaio 2027, i dati sul divario retributivo di genere.", "Art. 4(1)"),
             ("Where an unjustified gap exceeds 5%, a joint pay assessment with worker representatives is mandatory.",
              "Ove il divario retributivo superiore al cinque per cento non sia giustificato da criteri oggettivi, il datore di lavoro procede a una valutazione congiunta delle retribuzioni con le rappresentanze dei lavoratori.", "Art. 6"),
             ("Job applicants gain a right to the pay range before the interview.",
              "I candidati hanno diritto a ricevere, prima del colloquio, informazioni sulla retribuzione iniziale o sulla relativa fascia.", "Art. 3(2)")],
            [{"who": "Employers ≥100 employees", "what": "first annual gender pay-gap report", "when": "from 2027-01-01"},
             {"who": "Employers with an unjustified gap >5%", "what": "joint pay assessment with worker representatives", "when": "within 6 months of the report"},
             {"who": "All employers", "what": "pay range disclosed to applicants before interview", "when": "from 2026-12-01"}],
            "high", "First major-economy transposition; sets the reporting threshold the intent asks about.",
            "Clients with ≥100 employees in Italy should run a pay-gap diagnostic before the first reporting year."),
        dev("Referentenentwurf eines Gesetzes zur Umsetzung der Richtlinie (EU) 2023/970 (Entgelttransparenzrichtlinie)",
            "https://www.bmfsfj.de/resource/blob/2026/entgelttransparenz-referentenentwurf.pdf", BM, "DE", "2026-08-28", "Consultation/Draft",
            "Germany publishes its draft bill: reporting from 100 employees, but a two-year transition.",
            [("The draft applies reporting duties to employers with at least 100 employees, with a two-year transition after entry into force.",
              "Arbeitgeber mit mindestens 100 Beschäftigten sind verpflichtet, zwei Jahre nach Inkrafttreten dieses Gesetzes erstmals zu berichten.", "§ 8(1)"),
             ("Consultation of the Länder and associations closes on 30 September 2026.",
              "Stellungnahmen der Länder und Verbände werden bis zum 30. September 2026 erbeten.", "Anschreiben")],
            [{"who": "Employers ≥100 employees (proposed)", "what": "first pay-gap report", "when": "two years after entry into force"}],
            "high", "Germany is the client's largest EU headcount; the draft fixes the threshold and the transition period.",
            "Consider a submission before 30 September 2026; the transition period is the point most likely to move.", confidence="medium"),
        dev("Décret n° 2026-812 du 20 août 2026 relatif à la publication des écarts de rémunération entre les femmes et les hommes",
            "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000052026812", LF, "FR", "2026-08-21", "Rules/Regulations",
            "France sets the reporting format: the existing Index is extended, not replaced.",
            [("The decree keeps the Index de l'égalité professionnelle and adds the Directive's pay-gap indicators to it.",
              "L'index de l'égalité professionnelle est complété par les indicateurs d'écart de rémunération prévus par la directive (UE) 2023/970.", "Art. 1"),
             ("Publication is due by 1 March each year on the employer's website.",
              "Les indicateurs sont publiés au plus tard le 1er mars de chaque année sur le site internet de l'employeur.", "Art. 3")],
            [{"who": "Employers ≥50 employees", "what": "publish the extended Index", "when": "by 1 March each year (first: 2027-03-01)"}],
            "medium", "Changes the format rather than the threshold; the client already reports under the Index.",
            "Map the new indicators onto the client's existing Index workflow before the March publication."),
        dev("Real Decreto 704/2026, de 4 de agosto, por el que se modifica el reglamento del registro retributivo",
            "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-15588", "https://www.boe.es/diario_boe/", "ES", "2026-08-05", "Rules/Regulations",
            "Spain aligns the registro retributivo with the Directive's pay-range and reporting rules.",
            [("The pay register must now record the pay range offered for each position.",
              "El registro retributivo incluirá la banda salarial ofrecida para cada puesto de trabajo.", "Art. único, apartado 2"),
             ("Employers with 250 or more workers report first, in 2027; the 100-worker tier follows in 2028.",
              "Las empresas de doscientas cincuenta o más personas trabajadoras cumplirán la obligación de información a partir de 2027 y las de cien o más a partir de 2028.", "Disposición transitoria")],
            [{"who": "Employers ≥250 workers", "what": "first Directive-format report", "when": "2027"},
             {"who": "Employers ≥100 workers", "what": "first Directive-format report", "when": "2028"}],
            "medium", "Staggered thresholds matter for the client's Spanish entities of different sizes.",
            "List the client's Spanish entities by headcount to see which report in 2027 and which in 2028.",
            first_seen="2026-08-27"),
        dev("Circolare del Ministero del Lavoro n. 14/2026 — metodologia per la valutazione congiunta delle retribuzioni",
            "https://www.gazzettaufficiale.it/eli/id/2026/08/26/26A04512/sg", GU, "IT", "2026-08-26", "Guidance/Advisory",
            "Italy explains how a joint pay assessment is to be run.",
            [("The assessment compares categories of workers performing work of equal value using the ministry's four criteria.",
              "La valutazione congiunta confronta le categorie di lavoratori che svolgono un lavoro di pari valore secondo i quattro criteri ministeriali: competenze, impegno, responsabilità e condizioni di lavoro.", "§ 2")],
            [], "medium", "Procedural detail for the obligation the decree created; useful once a gap is found.",
            "File with the Italian diagnostic; no action until a gap above 5% appears.", topics=["Pay equity"], confidence="medium"),
        dev("Ausschuss für Familie, Senioren, Frauen und Jugend — öffentliche Anhörung zum Entgelttransparenz-Umsetzungsgesetz",
            "https://www.bmfsfj.de/bmfsfj/aktuelles/anhoerung-entgelttransparenz-2026", BM, "DE", "2026-09-01", "Consultation/Draft",
            "The Bundestag committee schedules its hearing for 14 October 2026.",
            [("The hearing is set for 14 October 2026; written statements are due a week earlier.",
              "Die öffentliche Anhörung findet am 14. Oktober 2026 statt; schriftliche Stellungnahmen werden bis zum 7. Oktober 2026 erbeten.", "Terminhinweis")],
            [{"who": "Anyone wishing to be heard", "what": "written statement to the committee", "when": "by 2026-10-07"}],
            "low", "A date, not an obligation; relevant only if the client wants to be heard.",
            "Diary the 7 October deadline if a statement is planned.", topics=["Employment"]),
        dev("Communiqué — calendrier de transposition de la directive (UE) 2023/970",
            "https://travail-emploi.gouv.fr/communique-calendrier-transposition-2026.pdf", LF, "FR", "2026-08-19", "Press release",
            "The ministry announces a second decree on pay-range disclosure for November.",
            [("A second decree covering pre-hire pay-range disclosure is announced for November 2026.",
              "Un second décret relatif à la communication de la fourchette de rémunération avant l'embauche sera publié en novembre 2026.", "p. 1"),
             ("The ministry says the threshold will not go below 50 employees.",
              "le seuil ne descendra pas en dessous de 50 salariés", "p. 2")],
            [], "low", "Announces a future instrument; nothing binds yet.",
            # read_as "scan" is what extract.document_text writes for a vision-transcribed PDF; the
            # enricher's note and ratio say one of the two quotes was not found.
            "Monitor for the November decree; no client action now.", read_as="scan", confidence="low", topics=["Pay equity"],
            note="citations could not be verified against the document text"),
        dev("Plan de acción de la Inspección de Trabajo 2026-2027 — campaña sobre registro retributivo",
            "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-15901", "https://www.boe.es/diario_boe/", "ES", "2026-08-30", "Guidance/Advisory",
            "Spain's labour inspectorate announces an enforcement campaign on pay registers.",
            [("Inspections will prioritise employers above 250 workers whose registers lack pay ranges.",
              "Las actuaciones se dirigirán prioritariamente a las empresas de más de doscientas cincuenta personas trabajadoras cuyo registro retributivo carezca de bandas salariales.", "Eje 3")],
            [{"who": "Employers >250 workers", "what": "pay register with pay ranges, ready for inspection", "when": "from Q4 2026"}],
            "medium", "Enforcement priority named; the client's larger Spanish entity is in scope.",
            "Check the Spanish register carries pay ranges before Q4 2026.", topics=["Pay equity", "Employment"], first_seen="2026-09-01"),
        # The tolerances other lanes need, in one row: an empty headline (the page falls back to
        # the title) and no relevance level (shown as unrated, sorted with low).
        dev("Avviso di rettifica al decreto legislativo 5 agosto 2026, n. 118",
            "https://www.gazzettaufficiale.it/eli/id/2026/09/02/26A04701/sg", GU, "IT", "2026-09-02", "Notice/Circular",
            "", [("The corrigendum corrects a cross-reference in Article 6 and changes nothing of substance.",
                  "All'articolo 6, comma 2, le parole «articolo 4, comma 3» sono sostituite dalle seguenti: «articolo 4, comma 1».", "Avviso")],
            [], "", "", "", topics=["Pay equity"], confidence="medium"),
        # Read in full but cut at max_doc_chars: the page must say the citations and Ask cover only
        # the first 30,000 characters.
        dev("Relazione illustrativa al decreto legislativo n. 118/2026",
            "https://www.gazzettaufficiale.it/eli/id/2026/08/12/26G00130/relazione", GU, "IT", "2026-08-12", "Other",
            "The explanatory report sets out how the 100-employee threshold was chosen.",
            [("The report explains that the threshold follows the Directive's Article 9 tiers rather than national census bands.",
              "La soglia dei cento dipendenti riprende le fasce dell'articolo 9 della direttiva anziché le classi dimensionali nazionali.", "§ 1.2")],
            [], "low", "Background to the decree; no obligation of its own.", "", topics=["Pay equity"], confidence="medium", truncated=True),
    ]
    devs[-2]["relevance"] = {}
    # The four read states a real ledger carries besides "enriched" (run.py writes exactly these
    # fields). Queued: ledgered this run, beyond max_new_per_run, nothing read. Read failed: the
    # document 404ed, attempt 2 of 3. Enrichment failed: text read and stored, the model call did
    # not succeed (an "error" record, treated as a failed attempt by contract). None of them has
    # a summary, and the page must say why for each.
    def bare(title, url, src_url, jur, date, **kw):
        d = {"id": common.short_id(title, date), "title": title, "url": url, "source_url": src_url, "tier": "discovered",
             "jurisdiction": jur, "date": date, "first_seen": common.today_ist(), "last_seen": common.today_ist(),
             "snippet": kw.get("snippet", ""), "doc_hash": None, "read_as": None, "enriched": False}
        d.update(kw.get("extra", {}))
        return d
    queued = bare("Decreto del Ministro del Lavoro 1 settembre 2026 — modello di rapporto sul divario retributivo",
                  "https://www.gazzettaufficiale.it/eli/id/2026/09/02/26A04705/sg", GU, "IT", "2026-09-02",
                  snippet="Modello di rapporto e istruzioni per la compilazione.")
    read_failed = bare("Bekanntmachung — Fristverlängerung für Stellungnahmen zum Referentenentwurf",
                       "https://www.bmfsfj.de/resource/blob/2026/fristverlaengerung.pdf", BM, "DE", "2026-09-01",
                       extra={"read_attempts": 2, "read_error": "HTTPError: 404 Not Found"})
    enrich_failed = bare("Avviso — apertura del portale per la trasmissione del rapporto sul divario retributivo",
                         "https://www.gazzettaufficiale.it/eli/id/2026/09/01/26A04690/sg", GU, "IT", "2026-09-01",
                         extra={"read_attempts": 1, "enrich_error": "model declined: the response did not match the schema",
                                "doc_hash": hashlib.sha1(b"portale").hexdigest(), "read_as": "text"})
    enrich_failed["text_file"] = f"text/{enrich_failed['id']}.txt"
    devs.extend([queued, read_failed, enrich_failed])
    # The unverifiable citation: the sentence stays, the mark shows. The second French communiqué
    # sentence is written into the text with different wording, so the check fails honestly — in
    # the paragraph's own `verified`, where enrich.verify_citations puts it.
    texts = {}
    for d in devs:
        if not d.get("text_file"):
            continue
        paras = [f"{d['title']}\n"]
        for i, s in enumerate(d.get("summary") or []):
            q = s["cite"]["quote"]
            if d["read_as"] == "scan" and i == 1:
                s["verified"] = False
                d["verified_ratio"] = 0.5
                paras.append("Le ministère précise que le seuil applicable sera fixé par le second décret.")
            else:
                paras.append(f"({s['cite']['where']}) {q}")
        if d.get("truncated"):
            paras.append("(Il testo prosegue.) " + "La relazione illustra i criteri adottati. " * 40)
        else:
            paras.append("Fine del documento." if d["jurisdiction"] == "IT" else "—")
        texts[d["id"]] = "\n\n".join(paras)
    if not texts.get(enrich_failed["id"]):
        texts[enrich_failed["id"]] = f"{enrich_failed['title']}\n\nIl portale è aperto dal 1° marzo 2027.\n\nFine del documento."

    ids = [d["id"] for d in devs]
    # digest.upcoming as run.py computes it (contract): every obligations[].when that parses to a
    # date on or after today, ascending, capped at 12. The page re-filters by the reader's today.
    today = common.today_ist()
    upcoming = []
    for d in devs:
        for o in d.get("obligations") or []:
            m = re.search(r"\d{4}-\d{2}-\d{2}", o.get("when") or "")
            if m and m.group(0) >= today:
                upcoming.append({"dev": d["id"], "when": m.group(0), "who": o.get("who", ""), "what": o.get("what", "")})
    upcoming = sorted(upcoming, key=lambda u: (u["when"], u["who"]))[:12]
    digest = {
        "week": week, "generated": now,
        "headline": "The June 7 deadline has passed and most member states missed it. Your clients' obligations now differ by country.",
        "body": [
            {"text": "Italy has transposed the Directive with reporting from 100 employees; Germany has a draft with the same threshold but a two-year transition.", "cites": [ids[0], ids[1]]},
            {"text": "France and Spain are adapting existing instruments rather than legislating afresh, which keeps the client's current workflows largely intact.", "cites": [ids[2], ids[3]]},
            {"text": "Prioritise Italy, where the first reporting year is 2027, and diary the German consultation deadline of 30 September.", "cites": [ids[0], ids[1]]},
            {"text": "Spain's inspectorate has named pay ranges in the register as an enforcement priority from Q4 2026.", "cites": [ids[7]]},
            # The digest writer's own reading with no development behind it: kept, flagged uncited,
            # rendered as opinion — exactly what digest.py emits for such a sentence.
            {"text": "Expect the Bundestag to move before the year ends; the coalition has said as much.", "cites": [], "uncited": True},
        ],
        # counts as run.py hands them to digest.write; `assessed`/`queued` are the numbers the KPI
        # tile's subtitle reads ("of N assessed · M queued").
        "counts": {"new": 13, "high": 2, "sources_ok": 3, "sources_failed": 1, "assessed": 10, "queued": 1},
        "upcoming": upcoming,
        "notes": ["dropped 1 citation(s) to ids the model was not shown"],
        "shown": sorted(ids[:10]),
        "selection": "dated or first seen within the last 14 days",
    }
    # health.json in the shape run.py writes: per-source rows, this run's counts under `run`, the
    # caps and every drop under `budget`, and run-level notes (discovery's gaps and drops, the
    # digest verifier's notes) under `notes`. The old sample's top-level `dropped` was a shape no
    # pipeline ever wrote, which let a builder that read it pass its selftest while every real
    # run's notes vanished.
    dropped = ["1 approved source(s) not fetched — max_sources=4",
               "1 development(s) queued, not enriched — max_new_per_run=10; the next run continues from the newest"]
    health = {
        "generated": now, "scan": sid,
        "sources": {
            # Three shapes of subject-filter count, on purpose: the structured object, the flat
            # keys, and nothing but the health line's own words. The page must read all three,
            # because the number is what a partner needs and the key it arrives under is not.
            GU: {"status": "OK", "rows_seen": 31, "new": 6, "newest_visible": "2026-09-02", "notes": [],
                 "subject": {"dropped": 24, "kept_terse": 2,
                             "titles": ["Comunicato n. 214", "Avviso di rettifica"]},
                 "info": ["extractor saw 31 candidate row(s)",
                          "24 row(s) outside this source's subject filter",
                          "2 row(s) kept because the title was too terse to judge",
                          f"{enrich_failed['id']}: enrichment failed (attempt 1): {enrich_failed['enrich_error']}"],
                 "checked": now, "name": sources[0]["name"], "tier": "discovered"},
            BM: {"status": "OK", "rows_seen": 24, "new": 3, "newest_visible": "2026-09-01", "notes": [],
                 "subject_dropped": 19, "subject_kept_terse": 0,
                 "info": [f"{read_failed['id']}: document not read (attempt 2): {read_failed['read_error']}"],
                 "checked": now, "name": sources[1]["name"], "tier": "discovered"},
            LF: {"status": "QUIET", "rows_seen": 40, "new": 0, "newest_visible": "2026-08-21", "notes": [],
                 "info": ["nothing new; newest item this venue shows is 2026-08-21",
                          "38 row(s) outside this source's subject filter",
                          "1 row(s) kept because the title was too terse to judge"],
                 "checked": now, "name": sources[2]["name"], "tier": "discovered"},
            sources[3]["url"]: {"status": "FAILED", "rows_seen": 0, "new": 0, "newest_visible": None,
                                "notes": ["fetch failed: HTTPError: 503 Service Unavailable"], "info": [], "checked": now,
                                "name": sources[3]["name"], "tier": "discovered"},
            sources[4]["url"]: {"status": "GATED", "rows_seen": 0, "new": 0, "newest_visible": None,
                                "notes": ["not fetched: max_sources=4 reached"], "info": [], "checked": now},
        },
        "run": {"new": 13, "enriched": 10, "queued": 1, "read_failed": 1, "enrich_failed": 1, "ledgered_total": len(devs), "week": week},
        "budget": {"caps": {"max_sources": 4, "max_new_per_run": 10, "max_doc_chars": 30000, "delay_seconds": 1.5,
                            "max_candidates": 25, "max_pages_per_source": 1}, "dropped": dropped},
        "notes": ["discovery gap ES: no official listing found for pay-register rules; the BOE gazette is pending a human read",
                  "discovery dropped https://blog.example.com/pay-transparency: not an official venue"]
                 + dropped + ["dropped 1 citation(s) to ids the model was not shown"],
    }
    # The Miscellaneous lane: what a hosted web search returned from OUTSIDE the coverage list.
    # Nothing here was fetched, gated or ledgered — every field is what the search provider showed.
    # One of each kind and one of each status, so the grouping, the Promote action and the two
    # non-promotable groups are all exercised. The second sample scan deliberately has NO misc.json:
    # absence is a state the page must render as "nobody looked", not as "nothing was found".
    def finding(url, title, jur, kind, why, **kw):
        return {"id": hashlib.sha1(url.encode()).hexdigest()[:10], "title": title, "url": url,
                "host": host_of(url), "date": kw.get("date"), "jurisdiction": jur, "kind": kind,
                "why": why, "snippet": kw.get("snippet", ""), "first_seen": common.today_ist(),
                "status": kw.get("status", "new"),
                # misc.merge sets this on a finding the latest search no longer returned. The
                # sample carries one so the marked row, the live tab count and the group's
                # "+ n kept" line are all exercised by the selftest and visible on the page.
                "stale": bool(kw.get("stale", False)), "last_seen": kw.get("last_seen", "")}
    misc = {
        "generated": now,
        "query": {"intent": defn["intent"], "topics": defn["topics"], "jurisdictions": defn["jurisdictions"],
                  "excluded_hosts": sorted({s["host"] for s in sources})},
        "findings": [
            finding("https://www.ilo.org/global/standards/pay-transparency-2026", "ILO — guidance note on pay-gap reporting methodologies",
                    "EU", "commentary", "Referenced by two national consultations; methodology only, not binding anywhere."),
            finding("https://www.arbeidstilsynet.no/regelverk/lonnstransparens", "Arbeidstilsynet — lønnstransparens (regulatory portal)",
                    "NO", "official_venue", "An official labour-inspectorate portal publishing binding pay-transparency rules for a jurisdiction this scan does not cover.",
                    date="2026-08-31", snippet="Nye regler om lønnstransparens trer i kraft 1. januar 2027."),
            finding("https://curia.europa.eu/juris/liste.jsf?num=C-401/26", "CJEU — Case C-401/26, reference on Article 157 TFEU and pay ranges",
                    "EU", "official_venue", "The Court's own case register; a reference here would bind every member state's transposition.",
                    date="2026-09-01"),
            finding("https://www.reuters.com/legal/eu-pay-transparency-deadline-missed-2026-09-01/", "Most EU states miss the pay transparency deadline",
                    "EU", "secondary", "Press report naming four states said to have draft bills; each claim needs checking at its own gazette.",
                    date="2026-09-01", snippet="Only six of 27 member states had transposed the directive by the June deadline, according to Commission figures."),
            finding("https://www.paywatch-europe.example/blog/what-italy-got-wrong", "What Italy got wrong in its transposition",
                    "IT", "commentary", "Law-firm commentary on the Italian decree already in this scan's ledger.", date="2026-08-20"),
            finding("https://www.gazzettaufficiale.it/ricerca/serie_generale", "Gazzetta Ufficiale — Serie Generale",
                    "IT", "official_venue", "Already on this scan's coverage list; promoted from a previous search.",
                    date="2026-08-12", status="promoted"),
            finding("https://www.someministry.example/news", "Ministry newsroom (dismissed)",
                    "ES", "official_venue", "A newsroom, not a listing of instruments — dismissed by the partner.", status="dismissed"),
            finding("https://www.consilium.europa.eu/en/press/press-releases/2026/07/pay-transparency-council/", "Council press service — pay transparency progress note",
                    "EU", "secondary", "Surfaced by an earlier search and not by the last one; kept, and marked, because a lead does not vanish when a search changes its mind.",
                    date="2026-07-14", stale=True, last_seen="2026-08-28T09:00:00+05:30"),
        ],
        "notes": ["Search returned 19 results; 12 were on hosts this scan already covers and were dropped."],
    }
    common.atomic_write_json(dest / "scans" / f"{sid}.json", defn)
    res = dest / "data" / "scans" / sid
    common.atomic_write_json(res / "developments.json", {"generated": now, "items": devs})
    common.atomic_write_json(res / "digest.json", digest)
    common.atomic_write_json(res / "health.json", health)
    common.atomic_write_json(res / "misc.json", misc)
    for did, t in texts.items():
        common.atomic_write_text(res / "text" / f"{did}.txt", t)
    # A second definition, created with discovery off, whose create died between writing the
    # definition and gating its one partner source — the page must say "create did not finish
    # gating", not "no run yet", because Run would fetch nothing.
    common.atomic_write_json(dest / "scans" / "de-ai-liability.json", {
        "id": "de-ai-liability", "name": "German AI liability bill",
        "intent": "Track the Bundestag's AI liability bill for a software client; surface committee stages and amendments.",
        "jurisdictions": ["DE"], "topics": ["AI liability"], "industries": ["Software"], "clients": ["Annalise.ai"],
        "sources": [{"url": "https://www.bundestag.de/dokumente/drucksachen", "host": "bundestag.de", "status": "pending",
                     "reason": "not yet gated", "proposed_by": "partner", "tier": "discovered", "jurisdiction": "DE"}],
        "budget": {}, "no_discover": True, "created": now, "updated": now, "demo": True,
    })
    return dest


# ----------------------------------------------------------------------------- selftest
def selftest() -> None:
    # 1. helpers
    assert flag("de") == "🇩🇪" and flag("EU") == "🇪🇺" and flag("India") == "" and flag("") == ""
    assert host_of("https://www.boe.es/diario_boe/") == "boe.es" and host_of("") == ""
    # The page's second opinion is the enricher's rule: ellipses tolerated, typographic quotes
    # folded (reviewed defect: a ’ in the text and a ' in the quote read as unverified here),
    # and a quote under 20 characters proves nothing.
    assert quote_in_text("…almeno cento  dipendenti pubblicano…", "con almeno cento\ndipendenti pubblicano annualmente") is True
    assert quote_in_text("the employer's duty to report annually", "The employer’s duty to report annually is fixed.") is True
    assert quote_in_text("the Act", "the Act the Act the Act") is False
    assert quote_in_text("not there at all, honestly", "some text") is False and quote_in_text("x", None) is None
    # The contract's lane rule: every known type lands in exactly one lane, and a type nobody
    # recognises (or an absent one, which is what a queued row has) lands in Signals rather than
    # nowhere. A development in no lane is a development nobody reads again.
    assert [lane_of(t) for t in INSTRUMENT_TYPES] == ["instruments"] * 5
    assert lane_of("Judgment") == "judgments"
    assert [lane_of(t) for t in SIGNAL_TYPES] == ["signals"] * 3
    assert lane_of("") == lane_of(None) == lane_of("Gazette notice") == "signals"
    assert len(set(KNOWN_TYPES)) == 9 and set(LANES) == {lane_of(t) for t in KNOWN_TYPES}

    # The subject filter's normaliser. The three ways of having none — absent, empty, "none" —
    # must all read as off, because an existing scan cannot change behaviour by standing still.
    assert subject_filter_of({})[0] == {"on": False, "regex": "", "why": "", "source": "none", "valid": True}
    assert subject_filter_of({"subject_filter": {"regex": "", "source": "proposed"}})[0]["on"] is False
    assert subject_filter_of({"subject_filter": {"regex": r"\bAI\b", "source": "none"}})[0]["on"] is False
    on, probs = subject_filter_of({"subject_filter": {"regex": r"\bAI\b", "why": "w", "source": "partner"}})
    assert on == {"on": True, "regex": r"\bAI\b", "why": "w", "source": "partner", "valid": True} and not probs
    # a regex the run would refuse is SHOWN and reported, never silently read as "no filter"
    bad, probs = subject_filter_of({"subject_filter": {"regex": "(unclosed", "source": "partner"}})
    assert bad["on"] is True and bad["valid"] is False and any("does not compile" in p for p in probs), probs
    # an unknown `source` is tolerated, named, and shown as the safest true thing
    odd, probs = subject_filter_of({"subject_filter": {"regex": "x", "source": "magic"}})
    assert odd["source"] == "proposed" and any("not one of" in p for p in probs), probs
    assert subject_filter_of({"subject_filter": "no"})[0]["on"] is False and subject_filter_of({"subject_filter": "no"})[1]
    # Per-source counts: every shape a run might write, and — the point — absent is not zero.
    assert subject_counts({}) is None and subject_counts({"info": ["nothing new"]}) is None
    assert subject_counts({"subject": {"dropped": 3, "kept_terse": 1, "titles": ["a"]}}) == {"dropped": 3, "kept_terse": 1, "titles": ["a"]}
    assert subject_counts({"subject_dropped": 7, "subject_kept_terse": 0}) == {"dropped": 7, "kept_terse": 0, "titles": []}
    assert subject_counts({"filtered": 5, "unsure": ["t1", "t2"]}) == {"dropped": 5, "kept_terse": 2, "titles": []}
    parsed = subject_counts({"info": ["12 row(s) outside this source's subject filter",
                                      "2 row(s) kept because the title was too terse to judge"]})
    assert parsed == {"dropped": 12, "kept_terse": 2, "titles": []}, parsed
    assert subject_counts({"notes": ["9 row(s) outside the subject filter"]}) == {"dropped": 9, "kept_terse": None, "titles": []}

    ev = gate_evidence({"reachable": True, "http": 200, "robots": "allowed", "tos": {"checked": ["u"], "flags": []}, "extract": {"rows": 31, "dated": 29, "floor": 8}})
    assert ev == "robots allowed · terms checked: 1 page, no flags · 31 rows parsed, 29 dated", ev
    assert gate_evidence({}) == "no gate evidence recorded"
    assert "below floor" in gate_evidence({"extract": {"rows": 3, "dated": 3, "floor": 8}})
    # a robots refusal is "not fetched", never "unreachable", and a null extract prints no row count
    ev3 = gate_evidence({"reachable": False, "http": None, "robots": "disallowed", "tos": {"checked": [], "flags": []}, "extract": None})
    assert ev3 == "not fetched — robots.txt disallows our agent · terms not checked", ev3
    assert gate_evidence({"reachable": False, "http": None, "robots": None, "extract": None}).startswith("unreachable (HTTP —)")
    ev2 = gate_evidence({"reachable": True, "http": 200, "robots": "allowed", "robots_note": "no robots.txt", "final_url": "https://b/",
                         "tos": {"checked": ["u"], "flags": [], "errors": [{"url": "https://b/terms", "error": "RuntimeError: HTTP 404"}]}})
    assert ev2 == "redirected to https://b/ · robots allowed (no robots.txt) · terms checked: 1 page, no flags · 1 terms page unreadable (RuntimeError: HTTP 404 at https://b/terms)", ev2
    assert "2 terms pages unreadable" in gate_evidence({"tos": {"checked": [], "errors": [{"url": "a", "error": "x"}, {"url": "b", "error": "y"}]}})
    assert flagged_sentences({"tos": {"flags": ["a", {"sentence": "b", "url": "u"}, {"text": "c"}]}}) == ["a", "b — u", "c"]
    # health lookup by url, by host, by embedded url
    s = {"url": "https://www.x.org/list", "host": "x.org"}
    assert health_for({"sources": {"https://www.x.org/list": {"status": "OK"}}}, s)["status"] == "OK"
    assert health_for({"sources": {"x.org": {"status": "QUIET"}}}, s)["status"] == "QUIET"
    assert health_for({"k": {"url": "https://www.x.org/list", "status": "EMPTY"}}, s)["status"] == "EMPTY"
    assert health_for({"sources": {}}, s) is None
    # citation verification: the enricher's paragraph-level flag wins downward, the text check
    # demotes, absent text trusts the enricher; cite.verified is only a fixture fallback
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td) / "text"
        tdir.mkdir()
        (tdir / "abc.txt").write_text("The quick brown fox jumps over the lazy dog.", encoding="utf-8")
        it = prepare_item({"id": "abc", "text_file": "text/abc.txt", "url": "https://www.a.gov/x", "relevance": {"level": "HIGH"},
                           "enriched": True, "verified_ratio": 0.5, "note": "one quote short",
                           "summary": [{"text": "s1", "cite": {"quote": "quick brown fox jumps over"}, "verified": True},
                                       {"text": "s2", "cite": {"quote": "slow fox jumps over the lazy"}, "verified": True},
                                       {"text": "s3", "cite": {"quote": "quick brown fox jumps over"}, "verified": False},
                                       {"text": "s4", "cite": {"quote": "quick brown fox jumps over", "verified": False}}]}, tdir)
        assert [x["verified"] for x in it["summary"]] == [True, False, False, False]
        assert it["relevance"]["level"] == "high" and not it["relevance"]["unrated"] and it["domain"] == "a.gov" and it["has_text"]
        assert it["enriched"] and it["verified_ratio"] == 0.5 and it["note"] == "one quote short" and not it["truncated"]
        it2 = prepare_item({"id": "zzz", "text_file": "text/zzz.txt", "summary": [{"text": "s", "cite": {"quote": "q"}}], "relevance": {"level": "weird"}}, tdir)
        assert it2["summary"][0]["verified"] is True and it2["relevance"]["level"] == "low" and it2["relevance"]["unrated"] and not it2["has_text"]
        # read states carried as run.py writes them; a metadata-only level "" is unrated
        st = prepare_item({"id": "q1", "enriched": False, "read_attempts": 3, "read_error": "HTTPError: 404", "relevance": {"level": ""}}, tdir)
        assert not st["enriched"] and st["read_attempts"] == 3 and st["read_error"] == "HTTPError: 404" and st["relevance"]["unrated"]
        st2 = prepare_item({"id": "q2", "enriched": False, "read_attempts": 1, "enrich_error": "model declined", "truncated": True}, tdir)
        assert st2["enrich_error"] == "model declined" and st2["truncated"] and st2["verified_ratio"] is None
        assert prepare_item({"id": "q3", "enriched": False}, tdir)["read_attempts"] == 0
        # tolerances: empty headline falls back to the title, a missing level is unrated, the
        # source's kind rides along and an unknown kind is dropped rather than shown
        it3 = prepare_item({"id": "k", "title": "T", "headline": "", "source_url": "https://s/x", "obligations": [{"who": "w"}, "junk"]},
                           tdir, {"https://s/x": "gazette"})
        assert it3["headline"] == "T" and it3["relevance"] == {"level": "low", "unrated": True, "why": "", "action": "", "clients": {}}
        assert it3["kind"] == "gazette" and it3["obligations"] == [{"who": "w", "what": "", "when": ""}]
        assert prepare_item({"id": "k", "source_url": "https://s/x"}, tdir, {"https://s/x": "bogus"})["kind"] == ""
        # lane + untyped ride on every row, from the one rule above
        assert prepare_item({"id": "a", "type": "Judgment"}, tdir)["lane"] == "judgments"
        assert prepare_item({"id": "b", "type": "Notice/Circular"}, tdir) == dict(prepare_item({"id": "b", "type": "Notice/Circular"}, tdir), lane="instruments", untyped=False)
        assert prepare_item({"id": "c", "enriched": False}, tdir)["lane"] == "signals"
        assert prepare_item({"id": "c", "enriched": False}, tdir)["untyped"] is True
        assert prepare_item({"id": "d", "type": "Press release"}, tdir)["untyped"] is False

    # 1b. Miscellaneous: absence is a state, and every tolerance is stated rather than swallowed
    with tempfile.TemporaryDirectory() as td:
        res = Path(td)
        empty = load_misc(res)
        assert empty == {"present": False, "generated": "", "query": {}, "findings": [], "notes": [], "problems": []}
        common.atomic_write_json(res / "misc.json", {
            "generated": "2026-09-04T10:00:00+05:30",
            "query": {"intent": "i", "topics": ["t"], "jurisdictions": ["de"], "excluded_hosts": ["a.gov"]},
            "findings": [
                {"id": "aaaaaaaaaa", "title": "A", "url": "https://x.gov/list", "kind": "official_venue", "status": "new"},
                # host says one thing, URL says another: the URL wins, as misc.py's writer does
                {"title": "B", "url": "https://press.example/story", "kind": "secondary", "host": "somewhere.else"},
                {"title": "no url", "kind": "commentary"},
                {"title": "C", "url": "https://y.example/z", "kind": "sponsored", "status": "queued"},
                {"id": "aaaaaaaaaa", "title": "duplicate", "url": "https://x.gov/list", "kind": "official_venue"},
                # the last search did not return this one; it is kept and must be marked
                {"id": "bbbbbbbbbb", "title": "D", "url": "https://www.old.example/list", "kind": "official_venue", "stale": True},
                "junk",
            ],
            "notes": ["12 results dropped: already covered"]})
        m = load_misc(res)
        assert m["present"] and m["generated"].startswith("2026-09-04")
        assert m["query"]["jurisdictions"] == ["DE"] and m["query"]["excluded_hosts"] == ["a.gov"]
        # the URL-less row and the duplicate are gone; the unknown kind is shown as commentary
        assert [f["kind"] for f in m["findings"]] == ["official_venue", "secondary", "commentary", "official_venue"], m["findings"]
        assert m["findings"][1]["id"] == hashlib.sha1(b"https://press.example/story").hexdigest()[:10]
        assert m["findings"][1]["host"] == "press.example" and m["findings"][2]["status"] == "new"
        # host is recomputed from the URL, never read from the file (misc.py does the same), and
        # www. is folded off so a pipeline-written row produces the identical string
        assert m["findings"][3]["host"] == "old.example" and m["findings"][3]["stale"] is True
        assert [f["stale"] for f in m["findings"]] == [False, False, False, True]
        assert len(m["problems"]) == 6 and any("has no URL" in p for p in m["problems"])
        assert any("unknown kind 'sponsored'" in p and "not promotable" in p for p in m["problems"])
        assert any("unknown status 'queued'" in p for p in m["problems"])
        # every tolerance says so: the dropped duplicate and the disagreeing host are both reported
        assert any("two findings share the id 'aaaaaaaaaa'" in p for p in m["problems"]), m["problems"]
        assert any("says host 'somewhere.else'" in p and "press.example" in p for p in m["problems"])

    # 2. zero scans: only the built-in card, and a page that still stands on its own
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "empty", Path(td) / "dist"
        root.mkdir()
        summary = build(root, out)
        assert summary["scans"] == [] and (out / "scans.html").exists() and not (out / "scan").exists()
        html = (out / "scans.html").read_text(encoding="utf-8")
        assert '"builtin": true' in html and "vetted sources" in html and "Create scan" in html
        assert (out / "favicon.svg").exists()
        # The source picker: its button, its endpoint, and the standing line that must never be
        # edited away — a candidate is a proposal, and the gate is what decides.
        assert 'id="dlg-find">Find sources<' in html and '"discover": "/api/discover"' in html
        assert "These are proposals — nothing has been fetched to produce them." in html
        assert "checked against robots.txt and the site's own terms when the scan is created" in html
        assert 'id="cands"' in html and "at least 20 characters" in html
        # The first-run promise is the pipeline's number in both places it is made, never a literal
        assert f"the newest {FIRST_RUN_MAX_NEW} documents" in html, "dialog line lost the first-run cap"
        assert f'"firstRunMax": {FIRST_RUN_MAX_NEW}' in html
        assert "Press <b>Run scan</b> again to continue through the backlog." in html
        assert "'The first run reads the newest ' + FIRST_RUN_MAX + ' documents" in html
        # The pending store, under the contract's key, and the poll that must stand down
        assert "'tmt_scans_pending_v1'" in html and "action: 'status'" in html
        assert 'id="pending"' in html and "document.hidden" in html and "visibilitychange" in html
        # The wait, made a product rather than a CI job (§ "make the wait seamless"):
        # 1. the page picks up its own result — it fetches itself uncached, compares the DATA
        #    stamp, tells the partner, and reloads once with the open tab in the hash
        assert "cache: 'no-store'" in html and 'name="tmt-stamp" content="([^"]*)"' in html
        assert "This scan has finished — showing the new version." in html
        assert "const STAMP_MS = 20000;" in html and "function pageBusy()" in html
        assert "dialog[open]" in html and "history.replaceState" in html and "#tab=" in html
        # 2. honest phases: derived from the clock, and said to be derived
        assert "reading documents" in html and "writing the digest" in html and "gating the venues" in html
        assert "usually ' + phaseAt(mins" in html and "That is an estimate from the clock" in html
        assert "Waiting for a runner. Nothing has been read yet." in html
        # 3. failure keeps the clock, offers a repeat, and stops polling
        assert "data-retry=" in html and ">Try again<" in html and "The run stopped" in html
        assert "st.status !== 'completed'" in html
        # 4. status unavailable says only what is known
        assert "Live run status is off on this deployment" in html
        # 5. no CI service is named in the normal path; the run log is the fallback affordance
        assert "GitHub" not in html, "the page must not name the CI service"
        assert ">Open the run log<" in html or "'Open the run log'" in html
        # "No scans yet" is not true while one is being created, so the empty block is addressable
        assert 'id="hempty"' in html and "em.hidden = list.length > 0" in html
        # The Scans home is the product's front door now: it says what a scan is, and which one is
        # the built-in vetted scan, without typing a source count the registry owns.
        assert "A scan is one question" in html and "<b>TMT India</b> is the built-in, vetted one" in html
        # the built-in scan's source count comes from the card the registry computed, never typed
        # The lede is one line now, so it carries no counts to keep honest; the built-in card still
        # shows "N vetted sources · …" computed from the registry, which is where the number lives.
        assert "labelled <b>discovered</b>" in html, "the discovered/vetted distinction must survive the trim"
        assert "builtin" in html and "vetted sources" in html
        # The lane list was in the sentence the lede lost; the tabs themselves still name them.
        assert '"href": "/tmt-radar-v2.html"' in html, "the built-in card must still open the tracker"
        # Create is gated on the coverage preview, in the markup and in the submit handler
        assert 'id="preview-why"' in html and "built here with you" in html
        # The coverage list is now always on screen and always current, so there is no reveal to
        # gate on. What must stay true is that it redraws on every change.
        assert "function refreshPreview() { renderPreview(); }" in html
        assert 'class="prev" id="preview"></div>' in html, "the list must not be hidden"
        assert "built here with you" in html and "gated when the scan is created" in html
        assert "Each venue above is gated when the scan is created" in html
        assert "Miscellaneous will additionally search the open web outside this list." in html
        # The subject filter is built WITH the partner, in the same block as the coverage list —
        # not bolted on as an advanced option. Its box, its proposer, its endpoint, and the escape
        # hatch with the consequence stated plainly.
        assert 'id="subject-block"' in html and 'id="f-subject"' in html and 'class="rx"' in html
        assert 'id="dlg-subject">Propose from the brief<' in html and '"subject": "/api/subject"' in html
        assert 'id="subject-clear">Read everything<' in html
        assert "Subject — what counts, on the pages above" in html
        assert "Read everything: this scan will have no subject filter." in html
        assert "items that are not your subject" in html
        # What it would do, before the scan exists: the filter's own words back in English, and the
        # venue count marked as an illustration rather than a promise.
        assert "<b>Rows whose title mentions:</b>" in html and "each row\\'s title" in html
        assert "<b>Illustration, not a promise.</b>" in html and "The test is <b>per row</b>" in html
        # Editing the box makes the filter the partner's; clearing it by hand is "read everything"
        assert "subject.source = subject.regex ? 'partner' : 'none';" in html
        # A pattern the run would refuse never leaves this dialog
        assert "The subject filter is not a valid regular expression" in html
        # DEFECT this closes: the panel's helpers were declared inside renderScan(), BELOW the one
        # pass that builds the whole shell, so coverageHTML() read `SUBJ` in its temporal dead zone
        # and every scan page rendered blank. The selftest reads payloads and strings and never
        # executes this script, so the order is asserted here instead of being found on a page.
        assert (html.index("function subjectPanelHTML(") < html.index("if (D.page === 'home') renderHome();")
                and html.index("const SUBJ =") < html.index("if (D.page === 'home') renderHome();")), \
            "the subject panel's helpers must be declared before the render dispatch"
        # Python's (?i) is not a JavaScript flag; the page must not call a pattern the pipeline
        # compiles happily "invalid" (the run, not the browser, is the authority on the pattern)
        assert "replace(/^\\(\\?i\\)/, '')" in html, "the (?i) prefix must be stripped before the browser test"
        # The seven tabs, by name, and the lane rule the page routes by
        for label in ("Coverage", "Instruments", "Judgments", "Signals", "Miscellaneous", "Clients", "Audit"):
            assert "label: '" + label + "'" in html, label
        assert "id: 'v-'" not in html and "data-view=" in html
        # The Miscellaneous standing copy: the four sentences that must never be edited away
        assert "The tracker fetched none of these pages." in html
        assert "no robots.txt was consulted for them and no terms were checked" in html
        assert "Nothing here is a citable instrument." in html
        assert "Promote is the only route from this lane into coverage." in html
        assert "action: 'promote'" in html and "kind: 'promote'" in html
        # a lone unreadable definition must stop the build, not produce a page over corrupt data
        (root / "scans").mkdir()
        (root / "scans" / "bad.json").write_text("{not json", encoding="utf-8")
        try:
            build(root, out)
            raise AssertionError("unreadable definition did not fail the build")
        except RuntimeError:
            pass
        (root / "scans" / "bad.json").write_text(json.dumps({"id": "other", "name": "x"}), encoding="utf-8")
        try:
            build(root, out)
            raise AssertionError("id/file-name mismatch did not fail the build")
        except RuntimeError:
            pass

    # 3. the sample: every shape from the design, built through the same path the deployment uses
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "sample", Path(td) / "dist"
        write_sample(root)
        os.environ["TMT_SCAN_ROOT"] = str(root)
        try:
            summary = build(None, out)
        finally:
            del os.environ["TMT_SCAN_ROOT"]
        sid = "eu-pay-transparency"
        assert summary["scans"] == ["de-ai-liability", sid], summary["scans"]
        page = (out / "scan" / f"{sid}.html").read_text(encoding="utf-8")
        payload = json.loads(re.search(r'<script id="scan-data" type="application/json">(.*?)</script>', page, re.S).group(1).replace("<\\/", "</"))
        assert payload["page"] == "scan" and len(payload["items"]) == 13
        # Lane routing: every development lands in exactly one lane, and the three lanes together
        # are the whole ledger. This is the assertion the contract asks for.
        lanes = {}
        for it in payload["items"]:
            lanes.setdefault(it["lane"], []).append(it["id"])
        assert set(lanes) <= set(LANES) and sum(len(v) for v in lanes.values()) == len(payload["items"])
        assert len(set().union(*[set(v) for v in lanes.values()])) == len(payload["items"])   # no id in two lanes
        assert {k: len(v) for k, v in sorted(lanes.items())} == {"instruments": 6, "signals": 7}, lanes
        assert all(it["lane"] == lane_of(it["type"]) for it in payload["items"])
        # the three unread rows have no type and are parked in Signals, marked untyped
        assert sorted(it["lane"] for it in payload["items"] if it["untyped"]) == ["signals"] * 3
        # Miscellaneous, from the fixture: grouped by kind, statuses carried, coverage hosts excluded
        M = payload["misc"]
        assert M["present"] and M["generated"] and M["notes"]
        assert [f["kind"] for f in M["findings"]].count("official_venue") == 4
        assert [f["kind"] for f in M["findings"]].count("secondary") == 2
        assert [f["kind"] for f in M["findings"]].count("commentary") == 2
        assert {f["status"] for f in M["findings"]} == {"new", "promoted", "dismissed"}
        # One finding the last search no longer returned: kept, flagged, and therefore excluded
        # from the "live" count the tab shows (the page still renders it, marked).
        assert [f["id"] for f in M["findings"] if f["stale"]] and sum(1 for f in M["findings"] if f["stale"]) == 1
        assert "gazzettaufficiale.it" in M["query"]["excluded_hosts"] and M["query"]["jurisdictions"] == ["DE", "FR", "IT", "ES"]
        assert all(f["url"].startswith("http") and f["id"] for f in M["findings"])
        assert payload["counts"] == {"new": 13, "high": 2, "sources_ok": 3, "sources_failed": 1, "sources_empty": None, "assessed": 10, "queued": 1}, payload["counts"]
        assert payload["run"] == {"new": 13, "enriched": 10, "queued": 1, "read_failed": 1, "enrich_failed": 1, "ledgered_total": 13}, payload["run"]
        # the GATED source is not counted as read in the header (reviewed defect)
        assert payload["meta"] == "2 topics · 5 sources (4 read) · 4 jurisdictions", payload["meta"]
        cov = payload["coverage"]
        assert [len(cov[k]) for k in STATUS_ORDER] == [5, 1, 1]
        assert [s["health"]["status"] for s in cov["approved"]] == ["OK", "OK", "QUIET", "FAILED", "GATED"]
        assert cov["approved"][4]["health"]["notes"] == ["not fetched: max_sources=4 reached"] and cov["gated"] == 1
        assert cov["uncovered"] == ["ES"], cov["uncovered"]              # BOE pending, mites rejected: no approved ES source
        assert len(cov["discovery"]) == 2 and cov["discovery"][0].startswith("discovery gap ES")
        assert cov["approved"][0]["evidence"].startswith("robots allowed · terms checked: 1 page, no flags · 31 rows parsed")
        assert "robots allowed (no robots.txt" in cov["approved"][1]["evidence"] and "1 terms page unreadable" in cov["approved"][2]["evidence"]
        assert [s["kind"] for s in cov["approved"]] == ["gazette", "ministry", "gazette", "ministry", "parliament"]
        # Edit must be able to send a source back whole: the descriptive fields ride on the
        # definition the page holds, the gate's verdict does not (it is decided again on create).
        d0 = payload["scan"]["sources"][0]
        assert d0["name"] and d0["jurisdiction"] == "IT" and d0["rationale"] and d0["kind"] == "gazette", d0
        assert "gate" not in d0 and "tier" not in d0, d0
        # ---- the subject filter, on a scan that has one --------------------------------------
        # The count reaches the page under all three shapes a run might write it: the structured
        # object (Gazzetta), the flat keys (BMFSFJ), and nothing but the health line's own words
        # (Légifrance). Absent stays absent — a FAILED source recorded nothing and must read "—",
        # never "0 dropped", which would claim the filter looked.
        S = cov["subject"]
        assert S["on"] is True and S["source"] == "proposed" and S["valid"] is True, S
        assert S["regex"].startswith(r"\b(pay transparency") and S["why"].startswith("Admits rows")
        assert cov["approved"][0]["health"]["subject"] == {"dropped": 24, "kept_terse": 2,
                                                           "titles": ["Comunicato n. 214", "Avviso di rettifica"]}
        assert cov["approved"][1]["health"]["subject"] == {"dropped": 19, "kept_terse": 0, "titles": []}
        assert cov["approved"][2]["health"]["subject"] == {"dropped": 38, "kept_terse": 1, "titles": []}
        assert cov["approved"][3]["health"]["subject"] is None, "a FAILED source recorded no counts; absent is not zero"
        assert S["dropped"] == 81 and S["kept_terse"] == 3 and S["sources_counted"] == 3 and S["sources_run"] == 5, S
        # It round-trips through Edit exactly like no_misc and budget: what the page reads back is
        # what Save re-submits, so editing a topic cannot silently turn the next run loose.
        assert payload["scan"]["subject_filter"] == {"regex": S["regex"], "why": S["why"], "source": "proposed"}
        assert cov["pending"][0]["flags"] and "automatizada" in cov["pending"][0]["flags"][0] and cov["pending"][0]["reason"].startswith("ToS language found")
        assert cov["rejected"][0]["reason"].startswith("robots.txt disallows")
        assert cov["rejected"][0]["evidence"] == "not fetched — robots.txt disallows our agent · terms not checked", cov["rejected"][0]["evidence"]
        # the one unverified citation is the enricher's paragraph-level verdict, honoured
        unv = [(i["id"], j) for i in payload["items"] for j, s in enumerate(i["summary"]) if not s["verified"]]
        assert len(unv) == 1 and unv[0][1] == 1, unv
        scan_dev = [i for i in payload["items"] if i["read_as"] == "scan"]
        assert len(scan_dev) == 1 and scan_dev[0]["verified_ratio"] == 0.5 and "could not be verified" in scan_dev[0]["note"]
        assert all(s["verified"] for i in payload["items"] if i["read_as"] == "text" for s in i["summary"])
        assert sum(len(p["cites"]) for p in payload["digest"]["body"]) == 7
        assert [p["uncited"] for p in payload["digest"]["body"]] == [False, False, False, False, True]
        # digest.upcoming written by the run is passed through (ids in the ledger, dated), and the
        # selection rule and the verifier's notes ride along
        up = payload["digest"]["upcoming"]
        assert isinstance(up, list) and up and all(u["dev"] in {i["id"] for i in payload["items"]} for u in up) and up == sorted(up, key=lambda u: (u["when"], u["who"]))
        assert payload["digest"]["selection"].startswith("dated or first seen") and payload["digest"]["notes"] == ["dropped 1 citation(s) to ids the model was not shown"]
        # every cap and run-level note reaches the page: budget.dropped, health.notes, a FAILED
        # source, failed reads and failed enrichments (reviewed defects)
        probs = payload["problems"]
        assert probs[:2] == ["budget: 1 approved source(s) not fetched — max_sources=4",
                             "budget: 1 development(s) queued, not enriched — max_new_per_run=10; the next run continues from the newest"], probs
        # The digest verifier's notes are printed under the digest label, so they are NOT repeated as
        # header problems (review finding: the same sentence appeared twice on the page).
        assert not any("dropped 1 citation(s) to ids the model was not shown" in p for p in probs), probs
        # Only CAVEATS are problems now: a routine "discovery dropped <url>" is the deny-list working,
        # and filing that as a problem taught a partner to ignore the list (reviewed defect).
        assert any(re.match(r"discovery recorded \d+ caveat", p) for p in probs), probs
        assert not any("discovery dropped" in p for p in probs), probs
        assert "1 source(s) FAILED this run — open Coverage" in probs, probs
        assert any(p.startswith("1 development(s) could not be read") for p in probs) and any("read but not enriched" in p for p in probs)
        assert not any(p.startswith("run: discovery") for p in probs)   # discovery lines are grouped on the panel, not repeated
        # the read states, one of each, carried with their reasons
        by_state = {("queued" if not i["enriched"] and not i["read_error"] and not i["enrich_error"] else
                     "read_failed" if i["read_error"] else "enrich_failed" if i["enrich_error"] else
                     "truncated" if i["truncated"] else "ok"): i for i in payload["items"]}
        assert set(by_state) == {"queued", "read_failed", "enrich_failed", "truncated", "ok"}, set(by_state)
        assert by_state["queued"]["read_attempts"] == 0 and not by_state["queued"]["has_text"]
        assert by_state["read_failed"]["read_attempts"] == 2 and by_state["read_failed"]["read_error"] == "HTTPError: 404 Not Found"
        assert by_state["enrich_failed"]["read_attempts"] == 1 and by_state["enrich_failed"]["has_text"] and by_state["enrich_failed"]["relevance"]["unrated"]
        assert by_state["truncated"]["enriched"] and by_state["truncated"]["has_text"]
        # types are enrich.TYPES values (reviewed defect: the sample used labels the enricher never writes)
        assert {i["type"] for i in payload["items"] if i["type"]} <= {"Legislation", "Rules/Regulations", "Order/Decision", "Judgment", "Consultation/Draft", "Guidance/Advisory", "Notice/Circular", "Press release", "Other"}
        # the tolerance row: headline from title, unrated, sorted as low, kind from its source
        corr = [i for i in payload["items"] if i["title"].startswith("Avviso di rettifica")][0]
        assert corr["headline"] == corr["title"] and corr["relevance"]["unrated"] and corr["relevance"]["level"] == "low" and corr["kind"] == "gazette"
        assert sum(1 for i in payload["items"] if i["relevance"]["unrated"]) == 4   # corrigendum + the three unread states
        # future-dated obligations exist for the Upcoming list to find (the page filters by its own today)
        assert sum(1 for i in payload["items"] for o in i["obligations"] if re.search(r"202[7-9]-\d\d-\d\d", o["when"])) >= 2
        assert payload["scan"]["demo"] is True and payload["api"]["propose"] == "/api/propose"
        # Edit opens the same dialog, so the picker and the first-run promise are on the scan page
        # too, wired to the same number. (The pending card itself cannot be sampled: it exists only
        # after a live 202 from /api/scans.)
        assert payload["api"]["discover"] == "/api/discover" and payload["firstRunMax"] == FIRST_RUN_MAX_NEW
        assert 'id="dlg-find">Find sources<' in page and f"the newest {FIRST_RUN_MAX_NEW} documents" in page
        assert "These are proposals — nothing has been fetched to produce them." in page
        # clients keep both contract shapes; no_discover is read as stored
        assert payload["scan"]["clients"] == ["Accenture", {"name": "Annalise.ai", "scope": "employees in Germany and France only"}]
        assert payload["scan"]["no_discover"] is False
        assert '"__KIND_LABELS__"' not in page and '"gazette": "Gazette"' in page
        # published for the API from the deployment's own origin (queued and read-failed rows have no text)
        assert (out / "data" / "scans" / sid / "developments.json").exists()
        assert len(list((out / "data" / "scans" / sid / "text").glob("*.txt"))) == 11
        assert summary["published"][sid] == {"developments": True, "texts": 11}
        # the second definition: discovery off, create died before gating — say so, not "no run yet"
        page2 = (out / "scan" / "de-ai-liability.html").read_text(encoding="utf-8")
        p2 = json.loads(re.search(r'<script id="scan-data" type="application/json">(.*?)</script>', page2, re.S).group(1).replace("<\\/", "</"))
        assert p2["problems"] == ["create did not finish gating 1 source(s) — Edit and save to re-run"], p2["problems"]
        assert p2["scan"]["no_discover"] is True and p2["coverage"]["pending"][0]["reason"] == "not yet gated"
        # ... and it has NO subject filter. Existing scans must not change behaviour: the payload
        # says off, and both Coverage and Audit say it in as many words rather than staying silent,
        # because silence there reads as "nothing was dropped" when the truth is "nothing was judged".
        assert p2["coverage"]["subject"] == {"on": False, "regex": "", "why": "", "source": "none", "valid": True,
                                             "dropped": None, "kept_terse": None, "sources_counted": 0,
                                             "sources_run": 0}, p2["coverage"]["subject"]
        assert p2["scan"]["subject_filter"] == {"regex": "", "why": "", "source": "none"}
        assert "This scan has no subject filter, so every row every source publishes is ledgered." in page2
        # ... and it has no misc.json at all: the page must say nobody looked, not that nothing
        # was found, so `present` is false and the tab shows no count.
        assert p2["misc"] == {"present": False, "generated": "", "query": {}, "findings": [], "notes": []}, p2["misc"]
        assert "No open-web search has been run for this scan." in page2
        # a misc-only change moves the scan page's stamp, so an open tab notices the new lane
        mpath = root / "data" / "scans" / sid / "misc.json"
        orig_misc = mpath.read_text(encoding="utf-8")
        stamp_before = re.search(r'name="tmt-stamp" content="([0-9a-f]{12})"', page).group(1)
        mj = json.loads(orig_misc)
        mj["generated"] = "2027-01-01T00:00:00+05:30"
        mpath.write_text(json.dumps(mj), encoding="utf-8")
        build(root, out)
        assert re.search(r'name="tmt-stamp" content="([0-9a-f]{12})"', (out / "scan" / f"{sid}.html").read_text(encoding="utf-8")).group(1) != stamp_before
        # a misc.json the run wrote badly is reported on the page, never dropped in silence
        mpath.write_text(json.dumps({"generated": "x", "findings": [{"title": "no url", "kind": "commentary"}]}), encoding="utf-8")
        sm = build(root, out)
        assert any("has no URL" in pr for pr in sm["problems"][sid]), sm["problems"][sid]
        mpath.write_text(orig_misc, encoding="utf-8")
        # home
        home = (out / "scans.html").read_text(encoding="utf-8")
        hp = json.loads(re.search(r'<script id="scan-data" type="application/json">(.*?)</script>', home, re.S).group(1).replace("<\\/", "</"))
        assert [c["id"] for c in hp["cards"]] == ["tmt-india", "de-ai-liability", sid] and hp["cards"][2]["demo"] is True
        assert hp["cards"][2]["kpi"] == [{"n": 13, "label": "new"}, {"n": 2, "label": "high"}]
        assert hp["cards"][2]["flags"] == ["DE", "FR", "IT", "ES"] and "(4 read)" in hp["cards"][2]["meta"]
        assert re.search(r'name="tmt-stamp" content="[0-9a-f]{12}"', home) and re.search(r'name="tmt-stamp" content="[0-9a-f]{12}"', page)
        # embedded JSON cannot break out of its script tag
        assert "</script>" not in json.dumps(payload).replace("</", "<\\/")
        # a scan with no run yet renders as a state, never a crash
        (root / "scans" / "fresh.json").write_text(json.dumps({"id": "fresh", "name": "Fresh scan", "intent": "x" * 30, "jurisdictions": ["IN"], "topics": ["A"]}), encoding="utf-8")
        s2 = build(root, out)
        assert "fresh" in s2["scans"] and s2["problems"]["fresh"] == ["no run yet — press Run scan"]
        # a digest citing an id the ledger does not have is reported, and the chip is dropped
        res = root / "data" / "scans" / sid
        dg = json.loads((res / "digest.json").read_text(encoding="utf-8"))
        dg["body"][0]["cites"].append("deadbeef00")
        (res / "digest.json").write_text(json.dumps(dg), encoding="utf-8")
        s3 = build(root, out)
        assert any("deadbeef00" in p for p in s3["problems"][sid])
    print("PASS build_scans selftest: helpers (enricher's citation rule, gate wording), lane routing "
          "(9 types -> 3 lanes, an untyped row parked in Signals), Miscellaneous loading (absent, present, "
          "URL-less / duplicate / unknown-kind tolerances), zero-scan build, sample build "
          "(13 developments in exactly one lane each — 6 instruments, 0 judgments, 7 signals — incl. queued / "
          "read-failed / enrichment-failed / truncated, 5/1/1 sources incl. FAILED and GATED, 8 misc findings "
          "across 3 kinds and 3 statuses, budget drops + run notes + discovery on the page, 1 unverified "
          "citation, 1 uncited sentence, 4 unrated rows), a scan with no misc.json, an ungated definition, "
          "publish, failure modes; the subject filter (normalised, three count shapes read back, absent is "
          "not zero, round-tripped through Edit, proposed/edited/cleared in the dialog, and a scan without "
          "one saying so on Coverage and Audit); seven tabs, the Miscellaneous standing copy, the coverage preview gating "
          f"Create, source picker + pending store (first run = {FIRST_RUN_MAX_NEW} documents, from run.FIRST_RUN_MAX_NEW), "
          "and the wait: self pick-up on a changed data stamp, clock-derived phases said to be estimates, "
          "Try again on a failed run, an honest fallback when live status is off, and no CI service named")


# ----------------------------------------------------------------------------- main
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build dist/scans.html and dist/scan/<id>.html from scans/ and data/scans/.")
    ap.add_argument("--out", help="dist directory (default: <repo>/dist)")
    ap.add_argument("--sample-into", metavar="DIR", help="write a demo scan tree (scans/, data/scans/) under DIR and exit")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.sample_into:
        dest = write_sample(Path(a.sample_into).expanduser())
        print(f"sample scan written under {dest} — build it with TMT_SCAN_ROOT={dest} {sys.argv[0]} --out DIR")
        return 0
    build(None, Path(a.out).expanduser() if a.out else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
