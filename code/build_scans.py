#!/usr/bin/env python3
"""Static builder for the Scans page family: dist/scans.html and dist/scan/<id>.html.

Design: docs/horizon-design.md §2 and §7. Same shape as build_dashboard_v2.py — read the committed
data, build one JSON payload per page, write one HTML file with embedded CSS and vanilla JS. No
model, no network. The tracker's builder is untouched; the two families share the wordmark, the
design tokens and the auth gate, and nothing else, so this surface can be designed cleanly.

Inputs (all optional except the tracker's own registry, which the built-in card is computed from):
  scans/<id>.json                       the definition, written only by the scan workflow
  data/scans/<id>/developments.json     the ledger
  data/scans/<id>/digest.json           the week's narrative
  data/scans/<id>/health.json           per-source evidence from the last run
  data/scans/<id>/text/<dev>.txt        the text every citation points into

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
except Exception:  # pragma: no cover — enrich.py missing or unimportable
    _quote_found = _norm = _bare = None  # type: ignore[assignment]
    MIN_QUOTE_CHARS = 20

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
API = {"scans": "/api/scans", "ask": "/api/ask", "draft": "/api/draft", "propose": "/api/propose"}


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


def coverage_for(defn: dict, health: dict) -> dict:
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
    discovery = [n for n in (health.get("notes") or []) if isinstance(n, str) and n.lower().startswith("discovery")]
    gated = sum(1 for s in groups["approved"] if s["health"] and s["health"]["status"] == "GATED")
    return {"approved": groups["approved"], "pending": groups["pending"], "rejected": groups["rejected"],
            "uncovered": uncovered, "discovery": discovery, "gated": gated, "problems": unknown}


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
        "type": it.get("type") or "", "topics": list(it.get("topics") or []),
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
    if cov["discovery"]:
        problems.append(f"discovery recorded {len(cov['discovery'])} note(s) — open Coverage")
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
                         "kind": s.get("kind", "")}
                        for s in defn.get("sources") or [] if isinstance(s, dict)],
            # no_discover is stored on the definition by create_scan (contract), so Edit pre-ticks
            # the box as the scan was actually created rather than always "on".
            "no_discover": defn.get("no_discover") is True, "demo": bool(defn.get("demo", False)),
            "created": defn.get("created", ""), "updated": defn.get("updated", ""),
        },
        "items": items, "digest": {"week": digest.get("week", ""), "headline": digest.get("headline", ""), "body": body,
                                   "upcoming": upcoming,
                                   # The selection rule and the writer's notes say whether the digest
                                   # is re-narrating old items or was not written at all and why.
                                   "selection": str(digest.get("selection") or ""),
                                   "notes": [str(n) for n in (digest.get("notes") or []) if n]},
        "counts": counts, "run": {k: run.get(k) for k in ("new", "enriched", "queued", "read_failed", "enrich_failed", "ledgered_total")},
        "coverage": cov, "generated": generated, "problems": problems,
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
            "kpi": [{"n": s["counts"]["new"], "label": "new"}, {"n": s["counts"]["high"], "label": "high"}],
            "problems": s["problems"],
        })
    return {"page": "home", "builtISO": built, "cards": cards, "clientNames": client_names(),
            "actionsUrl": _actions_url(), "api": API,
            "stamp": stamp_of(*[f"{c['id']}:{c['generated']}" for c in cards])}


def scan_payload(s: dict, built: str) -> dict:
    d = s["definition"]
    return {"page": "scan", "builtISO": built, "scan": d, "meta": scan_meta(d, s["coverage"]["gated"]), "items": s["items"],
            "digest": s["digest"], "counts": s["counts"], "run": s["run"], "coverage": s["coverage"], "generated": s["generated"],
            "problems": s["problems"], "clientNames": client_names(), "actionsUrl": _actions_url(), "api": API,
            "stamp": stamp_of(d["id"], s["generated"], d.get("updated", ""))}


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

/* coverage panel */
.coverage{display:none;margin-top:26px;border:1px solid var(--rule2);border-radius:12px;padding:24px 28px;background:#fff}
.coverage.on{display:block}
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
.field label{display:block;font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin-bottom:6px}
.field label .req{color:var(--alarm);margin-left:2px}
.field .help{margin-top:5px;font-size:12px;color:var(--faint);line-height:1.45}
.field input[type=text],.field textarea{width:100%;border:1px solid var(--rule);border-radius:6px;padding:8px 10px;font-size:13px;background:#fff;line-height:1.45}
.field textarea{min-height:88px;resize:vertical}
.field input[type=text]:focus,.field textarea:focus{border-color:var(--ink);outline:none}
.field.check{display:flex;align-items:center;gap:8px}
.field.check label{margin:0;font-family:var(--sans);text-transform:none;letter-spacing:0;font-size:13px;color:var(--ink)}
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

@media (max-width:900px){
  .head,.tabs{padding-left:22px;padding-right:22px}
  .page{padding:26px 22px 90px}
  .digest{grid-template-columns:1fr}
  .kpis{flex-direction:row}
  .kpi-tile{flex:1}
  .card{grid-template-columns:1fr}
  .card .right{text-align:left}
  .panel{width:100vw}
}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
</style>

<div class="sheet">
  <div class="head">
    <div class="wordmark"><a href="/tmt-radar-v2.html">TMT <b>Regulatory Radar</b></a></div>
    <div class="updbar"><div class="updated" id="headstamp"></div></div>
  </div>
  <nav class="tabs" aria-label="Sections">
    <a href="/tmt-radar-v2.html">Coverage</a>
    <a href="/tmt-radar-v2.html">Instruments</a>
    <a href="/tmt-radar-v2.html">Judgments</a>
    <a href="/tmt-radar-v2.html">Signals</a>
    <a href="/tmt-radar-v2.html">Clients</a>
    <a href="/tmt-radar-v2.html">Audit</a>
    <a class="tab-scans on" href="/scans.html" aria-current="page">Scans</a>
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
      <div class="field"><label for="f-src">Sources</label><div class="cin" id="c-src"></div>
        <div class="help">Optional. Add a listing page you already trust; it will still be checked — robots, terms and a parse test — before anything is read from it.</div></div>
      <div class="field"><label for="f-cl">Clients</label><div class="cin" id="c-cl"></div>
        <div class="help">Relevance is rated per named client; the model is asked to name them in the action line.</div></div>
      <div class="field check"><input type="checkbox" id="f-disc" checked><label for="f-disc">Discover sources automatically</label></div>
      <div class="help" id="f-disc-help">Off, only the sources listed above are gated and read.</div>
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

$('#headstamp').innerHTML = D.page === 'home'
  ? 'Human-run scans · <span>nothing scheduled</span>'
  : 'Last run <span>' + esc(D.generated ? stampText(D.generated) + ' IST' : 'never') + '</span>';

// ---- notice bar + polling ----------------------------------------------------------------
let noticeEl = null;
function say(html, kind) { if (!noticeEl) return; noticeEl.className = 'notice on' + (kind ? ' ' + kind : ''); noticeEl.innerHTML = html; }
const actionsLink = () => D.actionsUrl ? ' <a href="' + esc(D.actionsUrl) + '" target="_blank" rel="noopener">Open the Actions page</a> to watch it.' : '';
let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  // Fetch this very page and compare the stamp the builder embedded. A reload happens only when
  // the data behind the page changed, so a rebuild that changed nothing leaves the reader alone.
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(location.pathname, { cache: 'no-store' });
      if (!res.ok) return;
      const m = (await res.text()).match(/name="tmt-stamp" content="([^"]*)"/);
      if (m && m[1] !== D.stamp) location.reload();
    } catch (e) { /* offline or auth lapsed: keep waiting, the next tick tries again */ }
  }, 60000);
}
async function postJSON(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let data = null; try { data = await res.json(); } catch (e) { data = null; }
  return { status: res.status, ok: res.ok, data: data || {} };
}
async function dispatchScan(body, verb) {
  say('Asking GitHub Actions to ' + esc(verb) + '…');
  try {
    const r = await postJSON(D.api.scans, body);
    if (r.ok) { say(esc(r.data.message || 'Queued. This page refreshes itself when results land.') + actionsLink()); startPolling(); return true; }
    say(esc(r.data.message || ('The scans endpoint answered ' + r.status + '.')) + (r.status === 501 ? actionsLink().replace('to watch it', 'and run <b>scan.yml</b> by hand') : ''), 'bad');
  } catch (e) {
    say('No scans endpoint is reachable from this page.' + actionsLink(), 'bad');
  }
  return false;
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
const F = {
  jur: chipInput($('#c-jur'), { inputId: 'f-jur', placeholder: 'Germany, FR, EU…', normalize: jurNorm, render: v => flagged(v) + (NAMES[v] ? ' <span class="mark d">' + esc(countryName(v)) + '</span>' : '') }),
  top: chipInput($('#c-top'), { inputId: 'f-top', placeholder: 'Pay equity, Employment…' }),
  ind: chipInput($('#c-ind'), { inputId: 'f-ind', placeholder: 'Professional services…' }),
  src: chipInput($('#c-src'), { inputId: 'f-src', placeholder: 'https://…', validate: isUrl, render: v => esc(v.replace(/^https?:\/\/(www\.)?/, '').slice(0, 60)) }),
  cl: chipInput($('#c-cl'), { inputId: 'f-cl', placeholder: 'Client name', suggest: D.clientNames || [] }),
};
let editingId = null;
// {name, scope} clients keep their scope across an edit: the chip shows the name, the object is
// re-attached on submit so saving a scan does not silently drop "employees in Germany only".
let clientObjs = {};
function openDialog(scan) {
  editingId = scan ? scan.id : null;
  clientObjs = {};
  (scan ? scan.clients : []).forEach(c => { const n = clientName(c); if (n && typeof c === 'object') clientObjs[n] = c; });
  $('#dlg-title').textContent = scan ? 'Edit scan' : 'Create scan';
  $('#dlg-submit').textContent = scan ? 'Save and re-run' : 'Create scan';
  $('#f-name').value = scan ? scan.name : '';
  $('#f-intent').value = scan ? scan.intent : '';
  F.jur.set(scan ? scan.jurisdictions : []); F.top.set(scan ? scan.topics : []); F.ind.set(scan ? scan.industries : []);
  F.src.set(scan ? scan.sources.map(s => typeof s === 'string' ? s : (s && s.url) || '').filter(Boolean) : []);
  F.cl.set(scan ? scan.clients.map(clientName).filter(Boolean) : []);
  $('#f-disc').checked = scan ? !scan.no_discover : true;
  $('#dlg-err').textContent = '';
  setNote('#dlg-pnote', ''); setNote('#dlg-pnote-1', '');
  $('#f-desc').value = '';
  // Two paths, both from Harvey's launch material (design §2): describe it in one box and let
  // the model propose the structure, or fill the structured form directly. An edit of an existing
  // scan has nothing to describe, so it opens straight on the form.
  setStep(scan ? 'form' : 'describe');
  dlg.showModal();
  (scan ? $('#f-name') : $('#f-desc')).focus();
}
function setStep(step) { dlg.dataset.step = step; $('#dlg-back').hidden = step !== 'form' || !!editingId; }
function setNote(sel, html, warn) { const el = $(sel); el.className = 'pnote' + (html ? ' on' : '') + (warn ? ' warn' : ''); el.innerHTML = html || ''; }
$('#dlg-manual').addEventListener('click', () => {
  // A description typed before choosing the manual path is the intent in the partner's own words;
  // carrying it over saves retyping and loses nothing.
  const desc = $('#f-desc').value.trim();
  if (desc && !$('#f-intent').value.trim()) $('#f-intent').value = desc;
  setStep('form'); $('#f-name').focus();
});
$('#dlg-back').addEventListener('click', () => { setStep('describe'); $('#f-desc').focus(); });
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
    setStep('form'); $('#f-name').focus();
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
  const scan = { name, intent, jurisdictions: F.jur.get(), topics: F.top.get(), industries: F.ind.get(),
    sources: F.src.get().map(url => ({ url })), clients: F.cl.get().map(n => clientObjs[n] || n) };
  if (editingId) scan.id = editingId;
  const err = $('#dlg-err');
  if (name.length < 3) { err.textContent = 'Give the scan a name (3 characters or more).'; $('#f-name').focus(); return; }
  if (intent.length < 20) { err.textContent = 'The intent is what discovery and relevance read — write at least a sentence.'; $('#f-intent').focus(); return; }
  // run.py refuses an empty jurisdictions list; asking here saves a queued create that fails two
  // minutes later on the Actions page.
  if (!scan.jurisdictions.length) { err.textContent = 'Add at least one jurisdiction — the pipeline refuses a scan without one.'; F.jur.input.focus(); return; }
  if (!scan.topics.length && !scan.industries.length && !scan.sources.length) { err.textContent = 'Add at least one topic, industry or source, or discovery has nothing to look for.'; F.top.input.focus(); return; }
  err.textContent = '';
  const btn = $('#dlg-submit'); btn.disabled = true;
  const ok = await dispatchScan({ action: 'create', scan_id: editingId || undefined, scan, no_discover: !$('#f-disc').checked }, editingId ? 'update and re-run this scan' : 'create the scan');
  btn.disabled = false;
  if (ok) dlg.close();
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
  main.innerHTML = '<div class="titlerow"><div><div class="crumb">Scans</div><h1 class="title">Scans</h1>'
    + '<p class="lede">Each scan reads a fixed, gated list of sources for one question. The TMT India tracker is the built-in, vetted one; the rest are yours, with every source discovered, gated and labelled as such.</p></div>'
    + '<div class="actions"><button class="btn primary" id="create">+ Create scan</button></div></div>'
    + '<div class="notice" id="notice"></div>'
    + '<div class="htoolbar"><div class="ttabs" role="tablist" id="htabs"></div>'
    + '<div class="tools"><select id="hsort" aria-label="Sort scans"><option value="name">Sort: name</option><option value="lastrun">Sort: last run</option><option value="new">Sort: new developments</option></select></div></div>'
    + '<div class="cards" id="cards"></div>'
    + (scans.length ? '' : '<div class="empty"><h2>No scans yet.</h2><p>Describe a question the way you would brief an associate — the clients, the jurisdictions, what to surface — and the system finds candidate places to read, gates each one, reads them, and writes you a weekly digest in which every sentence is cited or marked as uncited.</p><p>Runs happen when you press <b>Run scan</b>, never on a schedule. Nothing enters a scan that did not come from a source you can see on its coverage panel.</p><button class="btn primary" id="create2">+ Create your first scan</button></div>');
  noticeEl = $('#notice');
  $('#create').addEventListener('click', () => openDialog(null));
  const c2 = $('#create2'); if (c2) c2.addEventListener('click', () => openDialog(null));
  $('#hsort').value = hs.sort;
  $('#hsort').addEventListener('change', e => { hs.sort = e.target.value; hsave(); drawCards(); });
  $('#htabs').addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) { hs.tab = b.dataset.tab; hsave(); drawCards(); } });
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
  const byId = {}; items.forEach(it => { byId[it.id] = it; });
  const KL = __KIND_LABELS__;  // discover.KINDS -> chip label, embedded by the builder so one table serves both
  // Triage lives in this browser only, as clients do on the tracker. Nothing leaves the page.
  const KEY = 'tmt_scan_' + S.id;
  let state = { read: {}, star: {}, arch: {} };
  try { state = Object.assign(state, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) {}
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };
  let tab = 'all', q = '', sort = 'newest', openId = null, lastFocus = null;

  const main = $('#main');
  const flags = S.jurisdictions.map(j => flagged(j)).join(' ');
  main.innerHTML = '<div class="crumb"><a href="/scans.html">Scans</a><span>›</span>' + esc(S.name) + '</div>'
    + '<div class="titlerow"><div><h1 class="title">' + esc(S.name) + '</h1>'
    + '<div class="metaline"><span>' + esc(D.meta) + '</span><span class="dot">·</span><span class="flags">' + flags + '</span><span class="dot">·</span><span>Last run <b id="lastrun"></b></span>'
    + (S.demo ? '<span class="badge demo">Demo</span>' : '') + '<span class="badge discovered">Discovered sources</span></div>'
    + '<p class="plain">Runs when a person presses Run scan — nothing here is scheduled.</p>'
    + (S.intent ? '<p class="intent">' + esc(S.intent) + '</p>' : '') + '</div>'
    // run.py refuses a demo definition with exit 2 (its sources are reserved .test hosts), so the
    // button says so up front instead of letting a partner queue a run that can only fail.
    + '<div class="actions"><button class="btn primary' + (S.demo ? ' demo-off' : '') + '" id="run"' + (S.demo ? ' disabled title="Demo scans use fixture hosts and cannot be run live — create your own scan" aria-disabled="true"' : '') + '>Run scan</button><button class="btn" id="edit">Edit</button><button class="btn" id="cov" aria-expanded="false" aria-controls="coverage">Coverage<span class="k">' + D.coverage.approved.length + (D.coverage.gated ? ' · ' + (D.coverage.approved.length - D.coverage.gated) + ' read' : '') + '</span></button></div></div>'
    + '<div class="notice" id="notice"></div>'
    + (D.problems.length ? '<ul class="problems">' + D.problems.map(p => '<li>' + esc(p) + '</li>').join('') + '</ul>' : '')
    + '<section class="coverage" id="coverage">' + coverageHTML() + '</section>'
    + digestHTML()
    + '<div class="toolbar"><div class="ttabs" role="tablist" id="ttabs"></div>'
    + '<div class="tools"><input type="search" id="q" placeholder="Search developments" aria-label="Search developments"><select id="sort" aria-label="Sort"><option value="newest">Newest</option><option value="relevance">Relevance</option></select></div></div>'
    + '<div class="tablewrap"><table class="dev" id="tbl"><colgroup><col><col style="width:112px"><col style="width:124px"><col style="width:180px"><col style="width:176px"><col style="width:112px"></colgroup>'
    + '<thead><tr><th>Development</th><th>Relevance</th><th>Type</th><th>Topics</th><th>Source</th><th>Jurisdiction</th></tr></thead><tbody id="tb"></tbody></table></div>'
    + '<section class="oblsec" id="oblsec" aria-label="Obligations register">' + obligationsHTML() + '</section>';
  noticeEl = $('#notice');
  $('#lastrun').textContent = rel(D.generated);
  $('#run').addEventListener('click', async () => { if (S.demo) return; const b = $('#run'); b.disabled = true; await dispatchScan({ action: 'run', scan_id: S.id }, 'run this scan'); b.disabled = false; });
  $('#oblsec').addEventListener('click', e => { const b = e.target.closest('button[data-dev]'); if (b) openDetail(b.dataset.dev, b); });
  $('#main').addEventListener('click', e => { const a = e.target.closest('a[data-dev]'); if (a) { e.preventDefault(); openDetail(a.dataset.dev, a); } });
  $('#edit').addEventListener('click', () => openDialog(S));
  $('#cov').addEventListener('click', () => { const c = $('#coverage'), on = !c.classList.contains('on'); c.classList.toggle('on', on); $('#cov').classList.toggle('on', on); $('#cov').setAttribute('aria-expanded', on); if (on) c.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); });
  $('#q').addEventListener('input', e => { q = e.target.value.trim().toLowerCase(); drawTable(); });
  $('#sort').addEventListener('change', e => { sort = e.target.value; drawTable(); });
  $('#ttabs').addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) { tab = b.dataset.tab; drawTable(); } });
  $('#tb').addEventListener('click', e => { const r = e.target.closest('tr.r'); if (r) openDetail(r.dataset.id, r); });
  $('#tb').addEventListener('keydown', e => { const r = e.target.closest('tr.r'); if (r && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openDetail(r.dataset.id, r); } });

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
      return src(s, '<div class="ev">' + line + '</div><div class="ev">gate: ' + esc(s.evidence) + (s.checked ? '<span class="sep">·</span>' + esc(fmt(s.checked)) : '') + '</div>' + (infos ? '<ul class="infos">' + infos + '</ul>' : ''));
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
      + '<div class="grp">Approved · ' + C.approved.length + (C.gated ? ' · ' + (C.approved.length - C.gated) + ' read this run' : '') + '</div>' + uncovered + (approved || none)
      + '<div class="grp">Pending a human decision · ' + C.pending.length + '</div>' + (pending || none)
      + '<div class="grp">Rejected · ' + C.rejected.length + '</div>' + (rejected || none)
      + discovery
      + '<div class="foot">Scans run when a person presses Run scan. Nothing here is scheduled.</div>';
  }

  // ---- table ---------------------------------------------------------------------------------
  const isUnread = it => !state.read[it.id] && !state.arch[it.id];
  const LV = { high: 0, medium: 1, low: 2 };
  function visible() {
    let list = items.filter(it => tab === 'archived' ? state.arch[it.id] : tab === 'starred' ? state.star[it.id] && !state.arch[it.id] : tab === 'unread' ? isUnread(it) : !state.arch[it.id]);
    if (q) list = list.filter(it => [it.title, it.headline, it.type, it.domain, it.jurisdiction, it.topics.join(' '), it.summary.map(s => s.text).join(' ')].join(' ').toLowerCase().includes(q));
    const newest = (a, b) => (b.date || '').localeCompare(a.date || '') || (b.first_seen || '').localeCompare(a.first_seen || '');
    list.sort(sort === 'relevance' ? ((a, b) => (LV[a.relevance.level] - LV[b.relevance.level]) || newest(a, b)) : newest);
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
    return '<span class="chip" title="' + esc(title) + '"><span class="mark ' + (tier === 'vetted' ? 'v' : 'd') + '">' + (tier === 'vetted' ? '✓' : '◌') + '</span>' + esc(label) + '</span>';
  }
  function srcChip(it) { return kindChip(it.kind, it.tier, it.domain); }
  function drawTable() {
    const counts = { all: items.filter(it => !state.arch[it.id]).length, unread: items.filter(isUnread).length, starred: items.filter(it => state.star[it.id] && !state.arch[it.id]).length, archived: items.filter(it => state.arch[it.id]).length };
    $('#ttabs').innerHTML = [['all', 'All'], ['unread', 'Unread'], ['starred', 'Starred'], ['archived', 'Archived']].map(([k, l]) => '<button type="button" role="tab" data-tab="' + k + '" class="' + (tab === k ? 'on' : '') + '" aria-selected="' + (tab === k) + '">' + l + '<span class="k">' + counts[k] + '</span></button>').join('');
    const list = visible();
    $('#tb').innerHTML = list.length ? list.map(it => '<tr class="r' + (state.read[it.id] ? ' read' : '') + '" tabindex="0" data-id="' + esc(it.id) + '" aria-label="' + esc(it.title) + '">'
      + '<td><div class="t"><span class="un" aria-hidden="true"></span><span>' + esc(it.title) + (state.star[it.id] ? '<span class="star" aria-label="starred">★</span>' : '') + '</span></div>'
      + (it.headline ? '<div class="h">' + esc(it.headline) + '</div>' : '') + '<div class="w"><b>' + esc(rel(it.date || it.first_seen)) + '</b> · ' + esc(it.domain) + '</div></td>'
      + '<td>' + relHTML(it.relevance) + '</td>'
      + '<td>' + (it.type ? '<span class="chip">' + esc(it.type) + '</span>' : '') + '</td>'
      + '<td><div class="chips">' + it.topics.map(t => '<span class="chip">' + esc(t) + '</span>').join('') + '</div></td>'
      + '<td>' + srcChip(it) + '</td>'
      + '<td>' + flagged(it.jurisdiction) + '</td></tr>').join('')
      : '<tr><td colspan="6" class="nothing">' + (items.length ? 'Nothing matches.' : (D.generated ? 'This run read nothing new.' : 'No run yet.')) + '</td></tr>';
  }

  // ---- detail slide-over ---------------------------------------------------------------------
  const panel = $('#panel'), scrim = $('#scrim');
  function openDetail(id, from) {
    const it = byId[id]; if (!it) return;
    lastFocus = from || document.activeElement; openId = id;
    if (!state.read[id]) { state.read[id] = true; save(); drawTable(); }
    panel.innerHTML = detailHTML(it);
    panel.classList.add('on'); scrim.classList.add('on'); panel.setAttribute('aria-hidden', 'false');
    wireCites(panel);
    $('.pclose', panel).addEventListener('click', closeDetail);
    $('#p-star').addEventListener('click', () => { state.star[id] = !state.star[id]; if (!state.star[id]) delete state.star[id]; save(); $('#p-star').textContent = state.star[id] ? '★ Starred' : '☆ Star'; $('#p-star').classList.toggle('on', !!state.star[id]); drawTable(); });
    $('#p-arch').addEventListener('click', () => { state.arch[id] = !state.arch[id]; if (!state.arch[id]) delete state.arch[id]; save(); $('#p-arch').textContent = state.arch[id] ? 'Unarchive' : 'Archive'; drawTable(); });
    $('#p-draft').addEventListener('click', () => draftEmail(it));
    $('#p-askb').addEventListener('click', () => { const a = $('#p-ask'); a.classList.toggle('on'); if (a.classList.contains('on')) $('#p-q').focus(); });
    $('#p-askf').addEventListener('submit', e => { e.preventDefault(); ask(it); });
    panel.scrollTop = 0;
    $('.pclose', panel).focus();
  }
  function closeDetail() {
    panel.classList.remove('on'); scrim.classList.remove('on'); panel.setAttribute('aria-hidden', 'true'); hideHC();
    openId = null;
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus(); else { const r = $('#tb tr.r'); if (r) r.focus(); }
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
  async function draftEmail(it) {
    const b = $('#p-draft'); b.disabled = true; b.textContent = 'Drafting…';
    let r = null, err = null;
    try { r = await postJSON(D.api.draft, { scan: S.id, dev: it.id, client: clientName(S.clients[0]) || undefined, kind: 'email' }); } catch (e) { err = e; }
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
    showModal('Draft email', demoPrefix + template(it), ['Deterministic template built from the record, because ' + why + '. Nothing was sent.'].concat(provenance));
  }
  function template(it) {
    const client = clientName(S.clients[0]) || '[Client]';
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
  drawTable();
}
</script>
"""


def render_page(payload: dict, title: str, favicon_b64: str) -> str:
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE.replace("__DATA__", data_json).replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;"))
            .replace("__STAMP__", payload["stamp"]).replace("__FAVICON_SVG__", favicon_b64)
            .replace("__KIND_LABELS__", json.dumps(KIND_LABELS)))


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
            GU: {"status": "OK", "rows_seen": 31, "new": 6, "newest_visible": "2026-09-02", "notes": [],
                 "info": ["extractor saw 31 candidate row(s)", f"{enrich_failed['id']}: enrichment failed (attempt 1): {enrich_failed['enrich_error']}"],
                 "checked": now, "name": sources[0]["name"], "tier": "discovered"},
            BM: {"status": "OK", "rows_seen": 24, "new": 3, "newest_visible": "2026-09-01", "notes": [],
                 "info": [f"{read_failed['id']}: document not read (attempt 2): {read_failed['read_error']}"],
                 "checked": now, "name": sources[1]["name"], "tier": "discovered"},
            LF: {"status": "QUIET", "rows_seen": 40, "new": 0, "newest_visible": "2026-08-21", "notes": [],
                 "info": ["nothing new; newest item this venue shows is 2026-08-21"], "checked": now, "name": sources[2]["name"], "tier": "discovered"},
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
    common.atomic_write_json(dest / "scans" / f"{sid}.json", defn)
    res = dest / "data" / "scans" / sid
    common.atomic_write_json(res / "developments.json", {"generated": now, "items": devs})
    common.atomic_write_json(res / "digest.json", digest)
    common.atomic_write_json(res / "health.json", health)
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

    # 2. zero scans: only the built-in card, and a page that still stands on its own
    with tempfile.TemporaryDirectory() as td:
        root, out = Path(td) / "empty", Path(td) / "dist"
        root.mkdir()
        summary = build(root, out)
        assert summary["scans"] == [] and (out / "scans.html").exists() and not (out / "scan").exists()
        html = (out / "scans.html").read_text(encoding="utf-8")
        assert '"builtin": true' in html and "vetted sources" in html and "Create scan" in html
        assert (out / "favicon.svg").exists()
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
        assert "discovery recorded 2 note(s) — open Coverage" in probs and "1 source(s) FAILED this run — open Coverage" in probs
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
    print("PASS build_scans selftest: helpers (enricher's citation rule, gate wording), zero-scan build, sample build "
          "(13 developments incl. queued / read-failed / enrichment-failed / truncated, 5/1/1 sources incl. FAILED and GATED, "
          "budget drops + run notes + discovery on the page, 1 unverified citation, 1 uncited sentence, 4 unrated rows), "
          "an ungated definition, publish, failure modes")


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
