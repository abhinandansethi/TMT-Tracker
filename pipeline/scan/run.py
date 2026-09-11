"""The scan orchestrator and CLI: create, run, delete, promote, list.

Design: docs/horizon-design.md. This module owns the ledger semantics; the sibling modules
(discover, gate, extract, enrich, digest) each do one step and never touch a file.

    python -m pipeline.scan.run create --from-json scans/new.json [--no-discover] [--max-new N] [--dry-run]
    python -m pipeline.scan.run run --id eu-pay-transparency [--dry-run] [--max-new N]
    python -m pipeline.scan.run delete --id eu-pay-transparency
    python -m pipeline.scan.run promote --id eu-pay-transparency --finding 3f9c1a7e2b
    python -m pipeline.scan.run dismiss --id eu-pay-transparency --finding 3f9c1a7e2b
    python -m pipeline.scan.run list
    python -m pipeline.scan.run --selftest

Rules this file enforces, each learned from the engine the hard way:

* The definition is written before anything network-shaped happens, so a failure mid-discovery
  still leaves a definition a human can inspect and re-run.
* Nothing enters the ledger from a source that is not `approved`, and nothing is approved
  except by the gate. A partner-typed URL and a model-proposed one get the same gate.
* A development is enriched once. It is re-read only while it has never been enriched (or
  the read failed, up to three tries); a URL seen again just updates `last_seen`. Re-enriching
  the backlog every run would cost the whole budget and change summaries under a partner's feet.
* Every cap that drops work — sources beyond `max_sources`, developments beyond
  `max_new_per_run` (or beyond FIRST_RUN_MAX_NEW on a create), text beyond `max_doc_chars` — is
  written into health. A FAILED source makes the process exit 1 after everything is written,
  never before.
* A create reads less than a run: the partner is waiting on a page that does not exist yet, so
  the first run enriches FIRST_RUN_MAX_NEW documents and says how many it queued. See the
  constant for the measurement behind the number.
* The Miscellaneous lane (`misc.py`) runs beside all of this and is held apart from it: it never
  fetches, nothing it returns enters the ledger, and it can never fail the run — a web-search
  failure is a note, because a lane that is not coverage cannot make the scan wrong. Its one door
  into coverage is `promote`, which adds an official venue's URL as a *pending* source; the next
  run gates that URL like any other candidate before a single row of it is read. `dismiss` is the
  other half of triage: it sets a lead aside so later runs stop offering it, and touches nothing
  else — the finding is kept, and coverage is not changed.
* A note in health is a claim that something needs attention: the page renders health.notes as
  the scan's problems. Counts, approvals and other routine outcomes go to health's own count
  fields, to the source's `info`, or to the log — never to notes.
* `--dry-run` swaps the four network-touching functions for fixture readers and the model for
  FakeClient, so the whole path can be exercised without a key or a socket. The swap is at
  module level (`fetch_listing`, `listing_rows`, `document_text`, `enrich_dev`, `discover_propose`,
  `gate_assess`) precisely so a test can do the same.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from . import common

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
FINDING_ID_RE = re.compile(r"^[0-9a-f]{10}$")   # misc.finding_id: 10 hex characters of the URL hash
# Ids that match ID_RE but name something that is not a scan. Review finding: a scan named
# "Schema" slugged to `schema`, and ScanPaths then overwrote scans/schema.json (the contract)
# with the definition — and `delete --id schema` would have unlinked it. `tmt-india` is the
# built-in vetted tracker's slot on the Scans home. Checked everywhere an id enters: the
# validator, create (after slugging), run and delete; api/scans.js holds the same set.
RESERVED_IDS = frozenset({"schema", "tmt-india"})
SOURCE_STATUSES = ("approved", "pending", "rejected")
TIERS = ("vetted", "discovered")
SOURCE_KINDS = ("gazette", "regulator", "ministry", "court", "parliament", "standards", "other")
# Who put a source in front of the gate. "miscellany" is a finding a partner promoted out of the
# Miscellaneous lane (pipeline/scan/misc.py): that lane never fetches, so a promoted URL has had
# nothing done to it yet — it enters as `pending` and is gated at the start of the next run, like
# every other candidate. Promotion is the ONLY route from that lane into coverage.
SOURCE_PROPOSERS = ("partner", "discovery", "miscellany")
MISC_PROPOSED_BY = "miscellany"   # mirrors misc.PROPOSED_BY; kept local so this file loads without it
# What a partner may write on a source (the create dialog's shape, and what /api/discover
# returns for each candidate), and what the gate adds on top of it. The validator accepts both
# groups because it re-checks the *committed* definition on every `run` — but only the first
# group survives `_candidate_from_partner` into the gate, so a hand-typed definition can never
# award itself `tier: "vetted"`, an `approved` status or forged gate evidence.
SOURCE_KEYS_PARTNER = ("url", "name", "jurisdiction", "kind", "rationale")
SOURCE_KEYS = SOURCE_KEYS_PARTNER + ("host", "status", "tier", "proposed_by", "confidence", "reason", "gate")
TOP_KEYS = ("id", "name", "intent", "jurisdictions", "topics", "industries", "clients", "sources", "discovery_notes",
            "subject_filter", "budget", "demo", "no_discover", "no_misc", "schedule", "group", "layer", "legal",
            "created", "updated")
# group / layer — a radar is a GROUP of scans, one per layer: "OpenAI" with layers "Regulation",
# "Company updates", "Competitors". Each layer is a whole scan (its own coverage, ledger, digest,
# Miscellaneous lane, schedule); the group is only how the pages arrange them. Both optional.
# legal — the partner's per-source decision on whether it is acceptable to fetch, keyed by URL:
# {decision: fetch | do_not_fetch | undecided, by, on, note}. The gate collects the evidence
# (robots.txt, the site's terms wording); this records the human call on it. `do_not_fetch`
# keeps the source on the coverage list — approved, visible — and makes every run skip it,
# recorded in health as WITHHELD with who decided and when.
LEGAL_DECISIONS = ("fetch", "do_not_fetch", "undecided")
LEGAL_KEYS = ("decision", "by", "on", "note")
# A client on a scan: the name the enricher rates against, the advice scope it reads, and — for
# the page's own deterministic matching, the way the TMT India tracker does it — a sector line
# and keywords. The enricher never sees the keywords; they are the partner's watch-list.
CLIENT_KEYS = ("name", "scope", "sector", "keywords")
# schedule — {daily_at: "HH:MM", tz: "Asia/Kolkata", set_by, set_on}: the ONE place a scan can be
# told to run unattended. Off unless a partner set it, visible on the page, and recorded with who
# set it and when, because a decision to collect without a person pressing the button is the
# decision the per-source legal analysis was written to avoid (docs/horizon-design.md §1.3).
SCHEDULE_KEYS = ("daily_at", "tz", "set_by", "set_on")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# The subject filter's vocabulary, mirrored from pipeline/scan/subject.py and kept local for the
# same reason MISC_PROPOSED_BY is: this file and its selftest must load even when a sibling module
# is broken or not yet written. The selftest asserts the two agree, so they cannot drift.
SUBJECT_KEYS = ("regex", "why", "source")
FILTER_SOURCES = ("proposed", "partner", "none")
MAX_REGEX, MAX_WHY = 400, 300
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAX_READ_ATTEMPTS = 3          # a document that will not read is given up on, loudly, not retried forever
# How many developments a scan's FIRST run enriches, under the budget's own max_new_per_run.
# The defect this closes, measured on a real create: enriching up to 60 documents took 10-15
# minutes of the ~20 the whole create took, and the partner saw nothing at all — no page, no
# digest, no coverage panel — until the run had committed and Vercel had rebuilt. Nothing is
# lost by reading fewer first: the remainder queues exactly like any other over-cap backlog and
# is reported through the paths that already exist (budget.note_drop -> health.budget.dropped ->
# the page's problems list, and digest counts.queued -> the KPI subtitle), and pressing Run scan
# again continues from the newest. Only the first run is capped this way; a later plain `run`
# uses the full max_new_per_run.
FIRST_RUN_MAX_NEW = 20
ENRICH_FIELDS = ("headline", "summary", "obligations", "type", "topics", "jurisdiction",
                 "relevance", "confidence", "verified_ratio", "note")
EXIT_OK, EXIT_FAILED_SOURCE, EXIT_USAGE = 0, 1, 2


# ----------------------------------------------------------------------------- paths
class ScanPaths:
    """Every file a scan owns. Same shape as common.Paths, but rooted explicitly so tests and
    the selftest can point the whole layer at a temp directory via TMT_SCAN_ROOT instead of
    leaving demo output under scans/ and data/scans/."""

    def __init__(self, scan_id: str, scans_dir: Path, data_dir: Path):
        self.id = scan_id
        self.scans_dir = scans_dir
        self.definition = scans_dir / f"{scan_id}.json"
        self.dir = data_dir / scan_id
        self.developments = self.dir / "developments.json"
        self.digest = self.dir / "digest.json"
        self.health = self.dir / "health.json"
        self.misc = self.dir / "misc.json"
        self.text_dir = self.dir / "text"

    def text_file(self, dev_id: str) -> Path:
        return self.text_dir / f"{dev_id}.txt"


def roots() -> tuple[Path, Path]:
    root = os.environ.get("TMT_SCAN_ROOT")
    if root:
        base = Path(root)
        return base / "scans", base / "data" / "scans"
    return common.SCANS_DIR, common.DATA_DIR


def paths_for(scan_id: str) -> ScanPaths:
    scans_dir, data_dir = roots()
    return ScanPaths(scan_id, scans_dir, data_dir)


# ----------------------------------------------------------------------------- validation
def _is_url(u: Any) -> bool:
    if not isinstance(u, str) or len(u) > 2000:
        return False
    p = urlparse(u)
    return p.scheme in ("http", "https") and bool(p.netloc)


def _str_list(v: Any, what: str, max_len: int, problems: list[str]) -> None:
    if not isinstance(v, list):
        problems.append(f"{what} must be an array of strings")
        return
    for i, s in enumerate(v):
        if not isinstance(s, str) or not (1 <= len(s.strip()) <= max_len):
            problems.append(f"{what}[{i}] must be a string of 1..{max_len} characters")


def coerce_sources(defn: Any) -> None:
    """A source may be given as a URL string or as an object carrying any of
    SOURCE_KEYS_PARTNER — {url (required), name, jurisdiction, kind, rationale}. That object is
    what the live discovery step hands the create dialog for each venue the partner picked, and
    the partner's own words for a venue are worth keeping: they ride through the gate into the
    committed source and are what the coverage panel shows. Review finding: the validator
    demanded objects, so every dialog-created scan was queued (HTTP 202) and then died two
    minutes later with `sources[0] must be an object` in a log the partner never sees. Strings
    become {"url": s} in place, before validation, so both shapes are one shape from here on."""
    if isinstance(defn, dict) and isinstance(defn.get("sources"), list):
        defn["sources"] = [{"url": s.strip()} if isinstance(s, str) else s for s in defn["sources"]]


def coerce_subject_filter(defn: Any) -> None:
    """A subject filter may be typed as `{"regex": "..."}` alone — that is all the create dialog
    and /api/propose-filter have to say — and the committed file always holds all three keys.
    Filled in place, before validation, exactly like coerce_sources, so the validator can mirror
    scans/schema.json's required-equals-properties discipline without refusing a partner's
    shorthand. A filter with no regex means no filter, so `source` defaults to "none": the safe
    reading, and the one that leaves an existing scan's behaviour untouched."""
    if not isinstance(defn, dict):
        return
    f = defn.get("subject_filter")
    if isinstance(f, dict):
        f.setdefault("regex", "")
        f.setdefault("why", "")
        f.setdefault("source", "partner" if str(f.get("regex") or "").strip() else "none")


def budget_notes(defn: Any) -> list[str]:
    """What the budget ceilings will do to this definition's overrides, in the words health
    will carry. Not a validation problem — a partner asking for more than the ceiling gets the
    ceiling, and is told."""
    if not isinstance(defn, dict) or not isinstance(defn.get("budget"), dict):
        return []
    return list(common.Budget(defn["budget"], dry_run=_DRY).dropped)


def validate_definition(defn: Any) -> list[str]:
    """Every problem, not just the first — a partner fixing a definition should see the whole
    list once. Mirrors scans/schema.json by hand; keep the two in step."""
    problems: list[str] = []
    if not isinstance(defn, dict):
        return ["definition must be a JSON object"]
    coerce_sources(defn)
    coerce_subject_filter(defn)
    for k in defn:
        if k not in TOP_KEYS:
            problems.append(f"unknown field '{k}' (allowed: {', '.join(TOP_KEYS)})")
    if "id" in defn and not (isinstance(defn["id"], str) and ID_RE.match(defn["id"])):
        problems.append("id must match ^[a-z0-9][a-z0-9-]{1,59}$")
    elif "id" in defn and defn["id"] in RESERVED_IDS:
        problems.append(f"id '{defn['id']}' is reserved (reserved: {', '.join(sorted(RESERVED_IDS))})")
    name = defn.get("name")
    if not isinstance(name, str) or not (3 <= len(name.strip()) <= 120):
        problems.append("name must be a string of 3..120 characters")
    intent = defn.get("intent")
    if not isinstance(intent, str) or not (20 <= len(intent.strip()) <= 1500):
        problems.append("intent must be a string of 20..1500 characters")
    j = defn.get("jurisdictions")
    if not isinstance(j, list) or not j:
        problems.append("jurisdictions must be a non-empty array of strings")
    else:
        if len(j) > 60:
            problems.append("jurisdictions: at most 60")
        _str_list(j, "jurisdictions", 40, problems)
    for key in ("topics", "industries"):
        if key in defn:
            _str_list(defn[key], key, 80, problems)
    if "clients" in defn:
        cl = defn["clients"]
        if not isinstance(cl, list):
            problems.append("clients must be an array")
        else:
            for i, c in enumerate(cl):
                if isinstance(c, str):
                    if not (1 <= len(c.strip()) <= 120):
                        problems.append(f"clients[{i}] must be 1..120 characters")
                elif isinstance(c, dict):
                    extra = set(c) - set(CLIENT_KEYS)
                    if extra:
                        problems.append(f"clients[{i}]: unknown field(s) {sorted(extra)} (allowed: {', '.join(CLIENT_KEYS)})")
                    if not isinstance(c.get("name"), str) or not (1 <= len(c["name"].strip()) <= 120):
                        problems.append(f"clients[{i}].name must be 1..120 characters")
                    for k in ("scope", "sector"):
                        if k in c and not (isinstance(c[k], str) and len(c[k]) <= 500):
                            problems.append(f"clients[{i}].{k} must be a string of at most 500 characters")
                    if "keywords" in c and not (isinstance(c["keywords"], list) and len(c["keywords"]) <= 60
                                                and all(isinstance(k, str) and 1 <= len(k) <= 120 for k in c["keywords"])):
                        problems.append(f"clients[{i}].keywords must be a list of at most 60 short strings")
                else:
                    problems.append(f"clients[{i}] must be a string or {{name, scope, sector, keywords}}")
    if "sources" in defn:
        srcs = defn["sources"]
        if not isinstance(srcs, list):
            problems.append("sources must be an array")
        else:
            for i, s in enumerate(srcs):
                if not isinstance(s, dict):
                    problems.append(f"sources[{i}] must be an object")
                    continue
                # Named, not silently ignored. Defect this closes: the loop checked only the
                # keys it knew, so a source carrying `sources[0].urls` or `tier_` was accepted,
                # gated as {url: None} and rejected two minutes later inside the workflow.
                extra = [k for k in s if k not in SOURCE_KEYS]
                if extra:
                    problems.append(f"sources[{i}]: unknown field(s) {sorted(extra)} "
                                    f"(allowed: {', '.join(SOURCE_KEYS)})")
                if not _is_url(s.get("url")):
                    problems.append(f"sources[{i}].url must be an http(s) URL")
                if "status" in s and s["status"] not in SOURCE_STATUSES:
                    problems.append(f"sources[{i}].status must be one of {list(SOURCE_STATUSES)}")
                if "tier" in s and s["tier"] not in TIERS:
                    problems.append(f"sources[{i}].tier must be one of {list(TIERS)}")
                if "kind" in s and s["kind"] not in SOURCE_KINDS:
                    problems.append(f"sources[{i}].kind must be one of {list(SOURCE_KINDS)}")
                if "proposed_by" in s and s["proposed_by"] not in SOURCE_PROPOSERS:
                    problems.append(f"sources[{i}].proposed_by must be one of {list(SOURCE_PROPOSERS)}")
                # "" is a real value: a source whose confidence was never assessed. An absence
                # is not a low score — the same rule the page applies to relevance.
                if "confidence" in s and s["confidence"] not in ("high", "medium", "low", ""):
                    problems.append(f"sources[{i}].confidence must be one of ['high', 'medium', 'low']")
                if "gate" in s and not isinstance(s["gate"], dict):
                    problems.append(f"sources[{i}].gate must be an object")
                for k in ("name", "host", "jurisdiction", "rationale", "reason"):
                    if k in s and not isinstance(s[k], str):
                        problems.append(f"sources[{i}].{k} must be a string")
                if isinstance(s.get("name"), str) and len(s["name"]) > 200:
                    problems.append(f"sources[{i}].name must be at most 200 characters")
                if isinstance(s.get("rationale"), str) and len(s["rationale"]) > 1000:
                    problems.append(f"sources[{i}].rationale must be at most 1000 characters")
                if isinstance(s.get("jurisdiction"), str) and len(s["jurisdiction"]) > 40:
                    problems.append(f"sources[{i}].jurisdiction must be at most 40 characters")
    if "subject_filter" in defn:
        # SHAPE ONLY. Whether the regex compiles is deliberately NOT a validation problem: a
        # definition that fails validation does not run at all, and refusing to run a scan over a
        # broken filter would be a worse outcome than reading a few extra rows. subject.prepare()
        # catches it at run time, reports it as a note and keeps every row.
        f = defn["subject_filter"]
        if not isinstance(f, dict):
            problems.append("subject_filter must be an object {regex, why, source}")
        else:
            extra = [k for k in f if k not in SUBJECT_KEYS]
            if extra:
                problems.append(f"subject_filter: unknown field(s) {sorted(extra)} "
                                f"(allowed: {', '.join(SUBJECT_KEYS)})")
            if not isinstance(f.get("regex"), str) or len(f.get("regex") or "") > MAX_REGEX:
                problems.append(f"subject_filter.regex must be a string of at most {MAX_REGEX} characters")
            if not isinstance(f.get("why"), str) or len(f.get("why") or "") > MAX_WHY:
                problems.append(f"subject_filter.why must be a string of at most {MAX_WHY} characters")
            if f.get("source") not in FILTER_SOURCES:
                problems.append(f"subject_filter.source must be one of {list(FILTER_SOURCES)}")
    if "budget" in defn:
        b = defn["budget"]
        if not isinstance(b, dict):
            problems.append("budget must be an object")
        else:
            for k, v in b.items():
                if k not in common.Budget.DEFAULTS:
                    problems.append(f"budget.{k} is not a budget key (allowed: {', '.join(common.Budget.DEFAULTS)})")
                elif (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0
                      or (isinstance(v, float) and not math.isfinite(v))):
                    problems.append(f"budget.{k} must be a finite, non-negative number")
    sch = defn.get("schedule")
    if sch is not None:
        if not isinstance(sch, dict):
            problems.append("schedule must be an object {daily_at, tz, set_by, set_on} or null")
        else:
            extra = set(sch) - set(SCHEDULE_KEYS)
            if extra:
                problems.append(f"schedule: unknown field(s) {sorted(extra)} (allowed: {', '.join(SCHEDULE_KEYS)})")
            if not isinstance(sch.get("daily_at"), str) or not _HHMM.match(sch["daily_at"]):
                problems.append("schedule.daily_at must be HH:MM (24-hour)")
            if not isinstance(sch.get("tz"), str) or not 1 <= len(sch["tz"]) <= 64:
                problems.append("schedule.tz must be an IANA zone name such as Asia/Kolkata")
            for k in ("set_by", "set_on"):
                if k in sch and not isinstance(sch[k], str):
                    problems.append(f"schedule.{k} must be a string")
    for k in ("group", "layer"):
        if k in defn and (not isinstance(defn[k], str) or len(defn[k]) > 120):
            problems.append(f"{k} must be a string of at most 120 characters")
    legal = defn.get("legal")
    if legal is not None:
        if not isinstance(legal, dict):
            problems.append("legal must be an object keyed by source URL")
        else:
            for url, rec in legal.items():
                if not _is_url(url):
                    problems.append(f"legal: '{url}' is not an http(s) URL")
                if not isinstance(rec, dict):
                    problems.append(f"legal[{url}] must be an object {{decision, by, on, note}}")
                    continue
                extra = set(rec) - set(LEGAL_KEYS)
                if extra:
                    problems.append(f"legal[{url}]: unknown field(s) {sorted(extra)} (allowed: {', '.join(LEGAL_KEYS)})")
                if rec.get("decision") not in LEGAL_DECISIONS:
                    problems.append(f"legal[{url}].decision must be one of: {', '.join(LEGAL_DECISIONS)}")
                for k in ("by", "on", "note"):
                    if k in rec and not isinstance(rec[k], str):
                        problems.append(f"legal[{url}].{k} must be a string")
    for k in ("demo", "no_discover", "no_misc"):
        if k in defn and not isinstance(defn[k], bool):
            problems.append(f"{k} must be a boolean")
    for k in ("created", "updated"):
        if k in defn and not isinstance(defn[k], str):
            problems.append(f"{k} must be a string")
    return problems


# ----------------------------------------------------------------------------- injectable steps
# Each of these is looked up by name at call time, so --dry-run (and a test) can replace it.
# The sibling modules are imported lazily inside so this file — and its selftest — load even
# when one of them is broken or not yet written.

_DRY = False   # True while enable_dry_run() is in force; Budget reads it to allow delay_seconds=0


def _budget(defn: dict) -> common.Budget:
    return common.Budget(defn.get("budget"), dry_run=_DRY)


def _default_fetch_listing(url: str, delay: float, allowed_hosts: list[str]) -> tuple[bytes, dict]:
    r = common.polite_get(url, delay=delay, allowed_hosts=allowed_hosts)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    info = {"http": r.status_code, "final_url": r.url, "content_type": r.headers.get("content-type", "")}
    if getattr(r, "hops", None):
        info["hops"] = list(r.hops)
    return r.content, info


def _default_listing_rows(body: bytes, url: str, client, defn: dict, budget: common.Budget):
    from . import extract
    return extract.listing_rows(body, url, client, defn, budget)


def _default_document_text(url: str, budget: common.Budget, client, report: Optional[dict] = None) -> tuple[str, str]:
    from . import extract
    return extract.document_text(url, budget, client=client, report=report)


def _default_enrich(defn: dict, dev: dict, text: str, client, budget: common.Budget) -> dict:
    from . import enrich
    return enrich.enrich(defn, dev, text, client, budget)


def _default_discover(defn: dict, client, budget: common.Budget) -> list[dict]:
    from . import discover
    return discover.propose(defn, client, budget)


def _default_gate(candidate: dict, budget: common.Budget, extractor: Callable) -> dict:
    from . import gate
    return gate.assess(candidate, budget, extractor)


def _default_misc(defn: dict, client, previous: Optional[dict]) -> tuple[dict, dict]:
    from . import misc
    return misc.scan(defn, client, previous=previous)


fetch_listing = _default_fetch_listing
listing_rows = _default_listing_rows
document_text = _default_document_text
enrich_dev = _default_enrich
discover_propose = _default_discover
gate_assess = _default_gate
misc_scan = _default_misc


def subject_module():
    """pipeline/scan/subject.py, or None when it cannot be imported.

    Lazy for the reason every sibling here is lazy — run.py and its selftest must load even when
    one module is broken — and tolerant for a reason of its own: a scan whose filter module will
    not import must read EVERYTHING, loudly, rather than not run. A filter that is missing costs
    a wasted reading budget; a run that refuses to start costs the coverage."""
    try:
        from . import subject
        return subject
    except Exception as e:                              # pragma: no cover - import-time breakage
        common.log(f"subject filter unavailable: {_err(e)}")
        return None


def subject_filter_for(defn: dict) -> tuple[Optional[dict], list[str]]:
    """(the filter this run will apply, or None to keep every row; health notes).

    Three ways to end up keeping everything, and health tells them apart. A BROKEN filter is a
    problem and says so. NO filter at all is also a problem — an unfiltered scan reading a telecom
    listing for an AI question is exactly what produced 118 developments with three mentions of
    the subject — so it is named too, and nobody mistakes it for good targeting. A filter
    deliberately switched off (`source: "none"`) is a partner's decision and gets no note."""
    raw = defn.get("subject_filter")
    sub = subject_module()
    if sub is None:
        return None, ([f"subject filter ignored, every row kept: pipeline/scan/subject.py could "
                       f"not be imported"] if raw else [])
    filt, note = sub.prepare(raw)
    if note:
        return None, [note]
    if filt is None and not (isinstance(raw, dict) and raw.get("source") == "none"):
        return None, [sub.NO_FILTER_NOTE]
    return filt, []


def propose_subject_filter(intent: str, topics: Any, client, model: Optional[str] = None) -> tuple[dict, str]:
    """Ask the model ONCE for a subject filter from an intent and topics: ({regex, why, source},
    note). Exposed here so /api/propose-filter and the create dialog reach it the same way every
    other step is reached, and validated in code inside subject.propose before it is returned.

    It is a PROPOSAL. Nothing in this file ever stores it: `create` writes the definition it is
    given, so a filter only ever takes effect after a partner has seen it."""
    sub = subject_module()
    if sub is None:
        return {"regex": "", "why": "", "source": "none"}, "pipeline/scan/subject.py could not be imported"
    return sub.propose(intent, topics, client, model=model)


def rows_and_report(result: Any) -> tuple[list[dict], dict]:
    """extract.listing_rows returns (rows, report); tolerate a bare list, because the design
    left that open and a shape mismatch here would fail every source at once."""
    if isinstance(result, tuple) and len(result) == 2:
        rows, report = result
    else:
        rows, report = result, {}
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    report = report if isinstance(report, dict) else {}
    return rows, report


def bind_extractor(client, defn: dict, budget: common.Budget) -> Callable:
    """The adapter the gate calls: extractor(body_bytes, url, candidate) -> list[rows]."""
    def extractor(body: bytes, url: str, candidate: dict) -> list[dict]:
        rows, _ = rows_and_report(listing_rows(body, url, client, defn, budget))
        return rows
    return extractor


# ----------------------------------------------------------------------------- small helpers
def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _declared_hosts(url: str) -> list[str]:
    """The host list polite_get holds every hop to: the source's own host with www. folded, so
    an apex/www redirect passes and anything off-host is refused with its reason."""
    h = _host(url)
    return [h[4:] if h.startswith("www.") else h]


def canon_url(url: str) -> str:
    """Identity for dedupe: scheme and host case-folded, fragment dropped, trailing slash
    stripped. Query kept — on many gazettes the id lives there."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "/").rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), host, path, p.params, p.query, ""))


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def valid_date(s: Any, today: str) -> Optional[str]:
    """ISO date, not before 2000, not more than 45 days ahead — the engine's gate. Anything
    else is treated as undated rather than trusted."""
    if not isinstance(s, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", s.strip()):
        return None
    try:
        d = _dt.date.fromisoformat(s.strip())
    except ValueError:
        return None
    if d.year < 2000 or d > _dt.date.fromisoformat(today) + _dt.timedelta(days=45):
        return None
    return d.isoformat()


def _client_names(defn: dict) -> list[str]:
    out = []
    for c in defn.get("clients") or []:
        n = c if isinstance(c, str) else (c or {}).get("name")
        if n:
            out.append(str(n))
    return out


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:240]}"


# ----------------------------------------------------------------------------- the run
def gate_promoted(defn: dict, client, budget: common.Budget, notes: list[str]) -> dict:
    """Gate the sources promoted out of the Miscellaneous lane since the last run.

    Returns {url: line} for the ones the gate APPROVED, so the caller can put that line on the
    source's own health row. Defect this closes: every outcome, approval included, was appended
    to the run's notes — which the page renders as the scan's problems — so a promotion that
    worked perfectly reported itself in the ochre list. A rejection or a parked source is still
    a note: that is news a partner has to act on.

    Only those sources: `proposed_by` miscellany, still pending, and carrying no gate evidence at
    all. That is exactly the state `promote` leaves a URL in — the lane never fetches, so nothing
    has been done to it yet — and it is what promote means when it tells the partner the next run
    will gate it: the URL is fetched with the honest UA, robots.txt and the terms scan are read,
    the extraction floor is applied, and an approved venue is read in this same run.

    A source pending for any other reason is left alone. The gate already looked at it and could
    not decide (terms language, usually); that is a human's call, and a later run must not
    quietly flip it."""
    fresh = [s for s in defn.get("sources") or []
             if s.get("proposed_by") == MISC_PROPOSED_BY and s.get("status") == "pending" and not s.get("gate")]
    if not fresh:
        return {}
    extractor = bind_extractor(client, defn, budget)
    cap_s = int(budget["max_sources"])
    approved_n = sum(1 for s in defn.get("sources") or [] if s.get("status") == "approved")
    approved_info: dict = {}
    for src in fresh:
        cand = {"url": src["url"], "name": src.get("name") or _host(src["url"]), "host": _host(src["url"]),
                "jurisdiction": src.get("jurisdiction", ""),
                "kind": src["kind"] if src.get("kind") in SOURCE_KINDS else "other",
                "proposed_by": MISC_PROPOSED_BY, "tier": "discovered",
                "rationale": src.get("rationale") or "promoted from the Miscellaneous lane"}
        if approved_n >= cap_s:
            src["reason"] = f"budget: max_sources={cap_s} already approved — not gated"
            notes.append(f"promoted source not gated: {src['url']} — max_sources={cap_s} already approved")
            continue
        try:
            decided = _normalise_source(gate_assess(cand, budget, extractor), cand, "gate returned no decision")
        except Exception as e:
            decided = _normalise_source({"status": "pending", "reason": f"gate unavailable: {_err(e)}"}, cand, "")
        src.clear()
        src.update(decided)
        line = (f"promoted source gated {src['status']}: {src['url']}"
                + (f" — {src['reason']}" if src.get("reason") else ""))
        if src["status"] == "approved":
            approved_n += 1
            # Routine, and good: the venue was promoted, gated and is read in this same run.
            # It belongs on that source's health row, where a reader looking at the venue sees
            # how it got there — not in the problems list.
            approved_info[src["url"]] = line
        else:
            notes.append(line)
        common.log(f"gate {src['status']:8s} {src['url']} (promoted from miscellany)")
    return approved_info


def run_misc(defn: dict, paths: ScanPaths, client, notes: list[str]) -> dict:
    """Run the Miscellaneous lane and write data/scans/<id>/misc.json. Never raises.

    Nothing this lane produces is coverage — it is a list of leads read out of a web-search
    provider's results, fetched from nobody — so its absence cannot make the scan wrong, and a
    web-search failure must never cost the partner the lanes that are coverage. Every way this
    can go wrong therefore ends in a note.

    The counts keep one shape whatever happened — a skipped or failed lane says `skipped` and
    carries its reason, rather than reporting zero findings, which a page would read as "we
    looked and found nothing".

    A note here is a claim that something went wrong: health.notes is what the page renders as
    the scan's problems. So only the abnormal outcomes get one — the lane failed, the previous
    file was unreadable, the findings could not be written, or the search itself errored. The
    routine numbers travel as `health.misc` and in the SUMMARY, and the leads themselves are a
    whole tab on the page. Defect this closes: "miscellany: N finding(s) outside coverage this
    run …" was appended on every successful run, so a scan that worked perfectly reported a
    problem — on every healthy run it ever had."""
    none = {"found": 0, "new": 0, "promoted": 0, "dismissed": 0, "kept": 0, "stale": 0,
            "dropped": 0, "total": 0, "skipped": True, "error": ""}
    previous = None
    try:
        previous = common.load_json(paths.misc, None)
    except Exception as e:
        notes.append(f"miscellany: the previous misc.json is unreadable and was not merged: {_err(e)}")
    try:
        doc, counts = misc_scan(defn, client, previous)
    except Exception as e:
        notes.append(f"miscellany: lane failed and was skipped this run: {_err(e)}")
        return dict(none, error=_err(e))
    try:
        common.atomic_write_json(paths.misc, doc)
    except Exception as e:
        notes.append(f"miscellany: findings could not be written to {paths.misc.name}: {_err(e)}")
        return dict(none, error=_err(e))
    if counts.get("error"):
        notes.append(f"miscellany: {counts['error']}")
    out = {"found": int(counts.get("found", 0)), "new": int(counts.get("new", 0)),
           "promoted": int(counts.get("promoted", 0)), "dismissed": int(counts.get("dismissed", 0)),
           "kept": int(counts.get("kept", 0)), "stale": int(counts.get("stale", 0)),
           "dropped": int(counts.get("dropped", 0)), "total": len(doc.get("findings") or []),
           "skipped": False, "error": str(counts.get("error") or "")}
    common.log(f"miscellany: {out['found']} finding(s) outside coverage this run "
               f"({out['new']} new, {out['kept']} kept from earlier runs and marked stale, "
               f"{out['promoted']} promoted, {out['dismissed']} dismissed, {out['dropped']} dropped) — "
               f"leads only; nothing here is fetched, cited or ledgered")
    return out


def run_scan(defn: dict, paths: ScanPaths, client, max_new: Optional[int] = None,
             notes: Optional[list[str]] = None, first_run: bool = False,
             no_misc: bool = False) -> tuple[dict, int]:
    """Fetch every approved source, ledger what is new, enrich up to the budget, write the
    digest. Returns (summary, exit_code). Everything is written before the exit code is
    decided, so a FAILED source never costs the run's other results.

    `max_new` lowers this run's enrichment cap below the budget's max_new_per_run; `first_run`
    only changes how the resulting queue note reads, because a partner watching their brand-new
    scan appear needs to be told it is the *first* run that reads fewer, not the scan."""
    from . import digest as digest_mod
    budget = _budget(defn)
    today, now = common.today_ist(), common.now_ist()
    scan_notes: list[str] = list(dict.fromkeys(list(defn.get("discovery_notes") or []) + list(notes or [])))
    ledger = common.load_json(paths.developments, None) or {"generated": now, "items": []}
    items: list[dict] = ledger.setdefault("items", [])
    by_url = {canon_url(d["url"]): d for d in items if d.get("url")}
    # The title+date key exists for venues that move a document to a new URL; it is only an
    # identity when there IS a date. Review finding: keyed on `date or ""`, every undated row
    # sharing a title ("Corrigendum", "Weekly bulletin", "Notification") — including rows whose
    # printed date failed valid_date — collapsed onto one development, silently.
    by_key = {(norm_title(d.get("title", "")), d["date"]): d for d in items if d.get("date")}
    ids = {d.get("id") for d in items}
    sources_health: dict[str, dict] = {}
    any_failed = False
    new_devs: list[dict] = []

    # Before anything is read: a URL promoted out of the Miscellaneous lane has never been
    # fetched, so this is where the gate decides on it. Approved here means read in this run,
    # and the approval is recorded on that source's own health row rather than in the run's
    # notes, which the page reads as problems.
    promoted_ok = gate_promoted(defn, client, budget, scan_notes)

    # The subject filter, decided once before any source is fetched and applied to every listing
    # below. It is the cheapest instrument in the pipeline and the earliest: a row it drops costs
    # no ledger entry, no fetch, no document read and no enrichment call, so the reading budget
    # goes to the subject instead of to whatever else the venue happened to publish.
    sub = subject_module()
    subject_filter, sf_notes = subject_filter_for(defn)
    scan_notes.extend(sf_notes)
    filtered_total, unjudged_total = 0, 0

    approved = [s for s in defn.get("sources") or [] if s.get("status") == "approved"]
    cap_sources = int(budget["max_sources"])
    if len(approved) > cap_sources:
        for s in approved[cap_sources:]:
            sources_health[s["url"]] = {"status": "GATED", "rows_seen": 0, "new": 0, "newest_visible": None,
                                        "notes": [f"not fetched: max_sources={cap_sources} reached"],
                                        "info": [], "checked": now}
        budget.note_drop(f"{len(approved) - cap_sources} approved source(s) not fetched — max_sources={cap_sources}")
        approved = approved[:cap_sources]

    legal = defn.get("legal") if isinstance(defn.get("legal"), dict) else {}
    for src in approved:
        url = src["url"]
        h = {"status": "FAILED", "rows_seen": 0, "new": 0, "newest_visible": None,
             "notes": [], "info": [], "checked": now,
             "name": src.get("name"), "tier": src.get("tier", "discovered")}
        if url in promoted_ok:
            h["info"].append(promoted_ok[url])
        sources_health[url] = h
        # A partner's "do not fetch" is final for this run: the source stays on the list, approved
        # and visible, and health says who withheld it rather than pretending it was read.
        dec = legal.get(url) or legal.get(canon_url(url)) or {}
        if isinstance(dec, dict) and dec.get("decision") == "do_not_fetch":
            h["status"] = "WITHHELD"
            h["notes"].append(f"not fetched: {dec.get('by') or 'a partner'} decided on the Legal tab"
                              + (f" ({dec['on'][:10]})" if isinstance(dec.get("on"), str) and dec.get("on") else "")
                              + (f" — {dec['note'][:200]}" if dec.get("note") else ""))
            common.log(f"WITHHELD {url} — do_not_fetch, decided by {dec.get('by') or 'a partner'}")
            continue
        try:
            body, info = fetch_listing(url, budget["delay_seconds"], _declared_hosts(url))
        except Exception as e:
            h["notes"].append(f"fetch failed: {_err(e)}")
            any_failed = True
            common.log(f"FAILED {url} — {_err(e)}")
            continue
        try:
            rows, report = rows_and_report(listing_rows(body, url, client, defn, budget))
        except Exception as e:
            h["notes"].append(f"extraction failed: {_err(e)}")
            any_failed = True
            common.log(f"FAILED {url} — extraction: {_err(e)}")
            continue
        if report.get("error"):
            # extract.listing_rows never raises for a model failure; it returns no rows and the
            # error in the report. Review finding: only the exception path above was treated as
            # FAILED, so a model outage read as EMPTY ("page changed, or the extractor missed the
            # list"), exit 0, and the workflow's last step passed.
            h["notes"].append(f"extraction failed: {str(report['error'])[:300]}")
            any_failed = True
            common.log(f"FAILED {url} — {report['error']}")
            continue
        if info.get("hops"):
            h["info"].append("redirected via " + " → ".join(str(x) for x in info["hops"]))
        for k, v in (report.get("dropped") or {}).items():
            if v:
                h["info"].append(f"extractor dropped {v} row(s): {k}")
        if report.get("seen") is not None:
            h["info"].append(f"extractor saw {report['seen']} candidate row(s)")
        # Here, per source, before dedupe and before anything is read: what on this listing is
        # this scan's subject. A row whose title cannot be judged is KEPT and marked — a scan has
        # no document probe to settle it with, and erring toward keeping is the only safe
        # direction for a tracker whose promise is that it does not miss things.
        rows_before = len(rows)
        if sub and subject_filter:
            rows, srep = sub.filter_rows(rows, subject_filter)
            h["info"].extend(sub.source_lines(srep))
            # The same two numbers again, structured, on the source's own health row. The page
            # (code/build_scans.py::subject_counts) sums these per venue so a partner auditing for
            # a miss reads the count beside the venue that lost the rows, not only a run total —
            # and it treats ABSENT as "this run recorded nothing", not as zero, so the key is
            # written only when the filter actually ran against this source.
            filtered_total += int(srep.get("filtered") or 0)
            unjudged_total += len(srep.get("unsure") or [])
            h["subject"] = {"dropped": int(srep.get("filtered") or 0),
                            "kept_terse": len(srep.get("unsure") or []),
                            "titles": list(srep.get("unsure") or [])[:5]}
        h["rows_seen"] = len(rows)
        dates: list[str] = []
        skipped_bad, undated = 0, 0
        for row in rows:
            title = common.norm_ws(str(row.get("title") or ""))
            raw_url = str(row.get("url") or "").strip()
            row_url = urljoin(url, raw_url) if raw_url else ""
            if not title or not _is_url(row_url):
                skipped_bad += 1
                continue
            date = valid_date(row.get("date"), today)
            if date:
                dates.append(date)
            elif row.get("date"):
                undated += 1
            cu = canon_url(row_url)
            existing = by_url.get(cu) or (by_key.get((norm_title(title), date)) if date else None)
            if existing:
                existing["last_seen"] = today
                if existing.get("source_url") != url and url not in existing.setdefault("sightings", []):
                    existing["sightings"].append(url)
                continue
            dev_id = common.short_id(cu)
            if dev_id in ids:                           # two distinct rows hashing alike: keep both, honestly
                dev_id = common.short_id(cu, title, date or "")
            dev = {
                "id": dev_id, "title": title, "url": row_url, "source_url": url,
                "tier": src.get("tier", "discovered"),
                "jurisdiction": str(row.get("jurisdiction") or src.get("jurisdiction") or ""),
                "date": date, "first_seen": today, "last_seen": today,
                "snippet": common.norm_ws(str(row.get("snippet") or ""))[:400],
                "doc_hash": None, "read_as": None, "enriched": False,
            }
            # Kept only because its title could not be judged against the subject filter. The
            # development says so on its own record, so nobody reads it as "this is in scope".
            if row.get("subject_unjudged"):
                dev["subject_unjudged"] = True
            items.append(dev)
            ids.add(dev_id)
            by_url[cu] = dev
            if date:
                by_key[(norm_title(title), date)] = dev
            new_devs.append(dev)
            h["new"] += 1
        if skipped_bad:
            h["info"].append(f"{skipped_bad} row(s) skipped: no title or no usable link")
        if undated:
            h["info"].append(f"{undated} row(s) carried a date that did not validate — kept undated")
        h["newest_visible"] = max(dates) if dates else None
        if not rows_before:
            h["status"] = "EMPTY"
            h["notes"].append("listing fetched but no rows extracted — page changed, or the extractor missed the list")
        elif h["new"]:
            h["status"] = "OK"
        elif not rows:
            # The listing parsed perfectly; every item on it belongs to another subject. That is
            # the filter working, not a broken venue, so it must not read EMPTY ("page changed")
            # and must not go in the problems list. QUIET, and the info line says why.
            h["status"] = "QUIET"
            h["info"].append(f"all {rows_before} row(s) on this listing are outside this scan's subject filter")
        else:
            h["status"] = "QUIET"
            h["info"].append("nothing new; newest item this venue shows is "
                             + (h["newest_visible"] or "undated"))
        common.log(f"{h['status']:6s} {url}  rows {h['rows_seen']}  new {h['new']}  newest {h['newest_visible'] or '—'}")

    # The Miscellaneous lane, after the sources have been read: what is happening on this
    # subject OUTSIDE the coverage list above. It is a search, never a fetch, and nothing it
    # returns is a citable instrument or enters the ledger — so it runs here, where the set of
    # approved hosts it must exclude is exactly the set this run just read, and it is allowed to
    # fail without touching the exit code.
    misc_counts: dict = {"found": 0, "new": 0, "promoted": 0, "kept": 0, "dropped": 0, "total": 0,
                         "skipped": True, "error": ""}
    if no_misc:
        scan_notes.append("miscellany: skipped on this run (--no-misc); the existing misc.json was left as it was")
    elif defn.get("no_misc"):
        scan_notes.append("miscellany: the lane is switched off for this scan (no_misc)")
    else:
        misc_counts = run_misc(defn, paths, client, scan_notes)

    # Enrichment: the never-enriched backlog, newest first, up to the cap. Failed reads are
    # retried on later runs until MAX_READ_ATTEMPTS, then left with their error on record.
    queue = [d for d in items if not d.get("enriched") and d.get("read_attempts", 0) < MAX_READ_ATTEMPTS]
    queue.sort(key=lambda d: (d.get("date") or "0000-00-00", d.get("first_seen") or ""), reverse=True)
    cap = int(budget["max_new_per_run"])
    # Which cap actually bit decides what the note says, because the two are different promises:
    # max_new_per_run is the scan's standing budget, while a smaller `max_new` is this one run
    # holding back — on a create, so the page appears in a couple of minutes instead of fifteen.
    capped_by_run = max_new is not None and max(0, int(max_new)) < cap
    if capped_by_run:
        cap = max(0, int(max_new))
    to_do, rest = queue[:cap], queue[cap:]
    if rest:
        if capped_by_run and first_run:
            why = f"first run reads the newest {cap}"
        elif capped_by_run:
            why = f"this run was limited to {cap} (--max-new)"
        else:
            why = f"max_new_per_run={cap}"
        budget.note_drop(f"{len(rest)} development(s) queued, not enriched — {why}; "
                         f"press Run scan again to continue through the backlog")
    given_up = [d for d in items if not d.get("enriched") and d.get("read_attempts", 0) >= MAX_READ_ATTEMPTS]
    if given_up:
        scan_notes.append(f"{len(given_up)} development(s) could not be read after {MAX_READ_ATTEMPTS} attempts "
                          "and are no longer retried: " + ", ".join(d["id"] for d in given_up[:10]))
    enriched = read_failed = enrich_failed = 0
    enriched_now: list[dict] = []

    # Reading is where a run spends its time: fetch the document, then a model call to summarise
    # it. The model calls overlap across a small pool of threads; the FETCHES do not — they take
    # a lock and go one at a time, so the politeness the legal basis rests on (one request at a
    # time, the delay between them) is exactly what it was when this loop was sequential. Each
    # worker touches only its own `dev`; the counters are tallied here, in order, from what it
    # returns. Under a dry run there is one worker, so the fake client is never shared.
    fetch_lock = threading.Lock()

    def _read_one(dev: dict) -> str:
        src_h = sources_health.get(dev.get("source_url") or "")
        sink = src_h["info"] if src_h else scan_notes
        # extract.document_text never raises for a bad document: it returns "" and puts the
        # reason (robots.txt, HTTP error, scan without a text layer, vision failure) in
        # `report`. Review finding: the report was never requested, so every unreadable
        # document was ledgered as "empty text" and re-fetched three times — even when
        # robots.txt had said no, which is the one answer that will not change.
        rep: dict = {}
        with fetch_lock:
            try:
                text, read_as = document_text(dev["url"], budget, client, report=rep)
            except Exception as e:
                dev["read_attempts"] = dev.get("read_attempts", 0) + 1
                dev["read_error"] = _err(e)
                sink.append(f"{dev['id']}: document not read (attempt {dev['read_attempts']}): {dev['read_error']}")
                return "read_failed"
        text = text or ""
        limit = int(budget["max_doc_chars"])
        if len(text) > limit:
            text = text[:limit]
            dev["truncated"] = True
            sink.append(f"{dev['id']}: text truncated to {limit:,} characters (max_doc_chars)")
        if not text.strip():
            reason = str(rep.get("reason") or "").strip() or f"document read as {read_as} but yielded no text"
            if reason.lower().startswith("robots.txt"):
                dev["read_attempts"] = MAX_READ_ATTEMPTS      # a published exclusion is final; do not knock again
            else:
                dev["read_attempts"] = dev.get("read_attempts", 0) + 1
            dev["read_error"] = reason[:300]
            sink.append(f"{dev['id']}: document not read (attempt {dev['read_attempts']}): {dev['read_error']}")
            return "read_failed"
        h = _sha1(text)
        if dev.get("enriched") and dev.get("doc_hash") == h:
            return "unchanged"
        common.atomic_write_text(paths.text_file(dev["id"]), text)
        dev.update({"doc_hash": h, "read_as": read_as, "text_file": f"text/{dev['id']}.txt"})
        try:
            e = enrich_dev(defn, dev, text, client, budget)
        except Exception as ex:
            e = {"error": _err(ex)}
        if not isinstance(e, dict) or e.get("error"):
            # enrich.enrich never raises for a model failure — it returns a metadata-only record
            # with `error` set. Review finding: that record was merged as if it were a reading
            # (enriched=True, never retried, enrich_failed 0, exit 0), with a relevance level the
            # page rendered as a verdict. An error record is a failed attempt, nothing more.
            dev["read_attempts"] = dev.get("read_attempts", 0) + 1
            dev["enrich_error"] = str(e.get("error") if isinstance(e, dict) else "enricher returned no record")[:240]
            sink.append(f"{dev['id']}: enrichment failed (attempt {dev['read_attempts']}): {dev['enrich_error']}")
            return "enrich_failed"
        for k in ENRICH_FIELDS:
            if k in e and e[k] not in (None, ""):
                dev[k] = e[k]
        dev["enriched"] = True
        dev["enriched_at"] = now
        dev.pop("read_error", None)
        dev.pop("enrich_error", None)
        return "ok"

    workers = 1 if common.DRY_RUN else max(1, min(6, int(os.environ.get("TMT_READ_WORKERS") or 4)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="read") as pool:
        futures = [(dev, pool.submit(_read_one, dev)) for dev in to_do]
        for dev, fut in futures:
            try:
                outcome = fut.result()
            except Exception as ex:  # a worker died outside the paths above: count it as a failed reading
                dev["read_attempts"] = dev.get("read_attempts", 0) + 1
                dev["enrich_error"] = _err(ex)[:240]
                outcome = "enrich_failed"
            if outcome == "read_failed":
                read_failed += 1
            elif outcome == "enrich_failed":
                enrich_failed += 1
            elif outcome == "ok":
                enriched += 1
                enriched_now.append(dev)
                # One line per document, so the page can show the run as it happens.
                common.log(f"read {enriched}/{len(to_do)}: {(dev.get('title') or dev.get('url') or '')[:80]}")

    ok_statuses = ("OK", "QUIET")
    sources_ok = sum(1 for h in sources_health.values() if h["status"] in ok_statuses)
    sources_failed = sum(1 for h in sources_health.values() if h["status"] == "FAILED")
    sources_empty = sum(1 for h in sources_health.values() if h["status"] == "EMPTY")
    # `high` is over what THIS run assessed, so the tile reads "N high of M assessed · K queued".
    # Review finding: it counted every development first seen today, so a second run the same
    # day reported new=0, high=2, and developments queued past the cap — never read — were
    # silently inside the denominator the tile implied.
    high = sum(1 for d in enriched_now
               if isinstance(d.get("relevance"), dict) and d["relevance"].get("level") == "high")
    counts = {"new": len(new_devs), "high": high, "assessed": enriched, "queued": len(rest),
              "sources_ok": sources_ok, "sources_failed": sources_failed}
    if sources_empty:
        counts["sources_empty"] = sources_empty

    week = common.iso_week()
    prev = common.load_json(paths.digest, None)
    dg = digest_mod.write(defn, items, prev, client, week, counts=counts)
    # The deadline list is computed in code from obligations[].when, never by the model
    # (design §4). Review finding: digest.json never carried it; the page recomputed it
    # client-side and the committed digest a reader inspects in git lacked the list the
    # contract promises. The page re-filters by the reader's own today.
    dg["upcoming"] = digest_mod.upcoming(items, today)

    health = {
        "generated": now,
        "scan": defn.get("id"),
        "sources": sources_health,
        # `filtered` is how many rows the subject filter kept out of the ledger this run, and
        # `unjudged` how many it let in because their titles were too terse to judge. Both are 0
        # for a scan with no filter, which is what every scan was before the filter existed.
        "run": {"new": len(new_devs), "enriched": enriched, "queued": len(rest),
                "read_failed": read_failed, "enrich_failed": enrich_failed,
                "filtered": filtered_total, "unjudged": unjudged_total,
                "ledgered_total": len(items), "week": week},
        # The Miscellaneous lane's own counts, kept apart from `run` because they are not
        # coverage: found is what the search surfaced outside the coverage list this run, new is
        # what the lane had never seen, promoted is how many findings a partner has moved into
        # sources. A skipped or failed lane says so rather than reading as zero findings.
        "misc": misc_counts,
        "budget": {"caps": dict(budget.v), "dropped": list(budget.dropped)},
        "notes": list(dict.fromkeys(scan_notes + list(budget.dropped) + [n for n in dg.get("notes", []) if n])),
    }
    ledger["generated"] = now
    common.atomic_write_json(paths.developments, ledger)
    common.atomic_write_json(paths.health, health)
    common.atomic_write_json(paths.digest, dg)
    defn["updated"] = now
    common.atomic_write_json(paths.definition, defn)

    summary = {"id": defn.get("id"), "sources": len(approved), "sources_ok": sources_ok,
               "sources_failed": sources_failed, "new": len(new_devs), "enriched": enriched,
               "queued": len(rest), "read_failed": read_failed, "enrich_failed": enrich_failed,
               "filtered": filtered_total, "unjudged": unjudged_total,
               "high": high, "ledgered_total": len(items), "week": week,
               "misc": {k: misc_counts.get(k, 0) for k in ("found", "new", "promoted")},
               "exit": EXIT_FAILED_SOURCE if any_failed else EXIT_OK}
    for n in health["notes"]:
        common.log(f"note: {n}")
    common.log(f"{defn.get('id')}: {len(new_devs)} new, {enriched} enriched, {len(rest)} queued, "
               f"{filtered_total} outside the subject filter, {unjudged_total} kept unjudged, "
               f"{sources_ok} source(s) ok, {sources_failed} failed → {paths.dir}")
    return summary, summary["exit"]


# ----------------------------------------------------------------------------- create
def _candidate_from_partner(s: dict) -> dict:
    """A partner's source object as a gate candidate. Everything the partner wrote about the
    venue — name, jurisdiction, kind, rationale — is carried through, because gate._source_shell
    keeps exactly these fields on the committed source and the coverage panel then shows the
    partner's own reason for picking it rather than a bare hostname. Defect this closes: `kind`
    was dropped here, so a venue the partner chose as a gazette in the create dialog rendered on
    the coverage panel as kind "other".

    `status`, `tier` and `gate` are deliberately NOT copied from the partner's object even
    though the validator accepts them on a committed definition: the gate decides all three, and
    tier is forced to "discovered" — a scan reads every source through the generic extractor, so
    a definition may never label itself vetted. (Mirrors discover._partner_candidate; kept local
    because create must still write a definition when discover cannot even be imported.)"""
    return {"url": s["url"], "name": s.get("name") or _host(s["url"]), "host": _host(s["url"]),
            "jurisdiction": s.get("jurisdiction", ""),
            "kind": s["kind"] if s.get("kind") in SOURCE_KINDS else "other",
            "proposed_by": "partner",
            "tier": "discovered", "rationale": s.get("rationale") or "added by the partner"}


def _normalise_source(src: Any, cand: dict, reason_if_bad: str) -> dict:
    """Whatever the gate returned, the committed source has a url, a status from the enum and
    its provenance. A gate that returns garbage yields a pending source, not an approved one."""
    out = dict(cand)
    if isinstance(src, dict):
        out.update(src)
    out["url"] = out.get("url") or cand["url"]
    out["host"] = out.get("host") or _host(out["url"])
    out.setdefault("tier", "discovered")
    out.setdefault("proposed_by", cand.get("proposed_by", "discovery"))
    if out.get("status") not in SOURCE_STATUSES:
        out["status"] = "pending"
        out["reason"] = reason_if_bad
    if out["status"] == "approved":
        out.pop("reason", None)
    return out


def create_scan(defn: dict, paths: ScanPaths, client, no_discover: bool,
                max_new: Optional[int] = None, no_misc: bool = False) -> tuple[dict, int]:
    now = common.now_ist()
    existing = common.load_json(paths.definition, None)
    notes: list[str] = []
    if existing:
        defn["created"] = existing.get("created") or now
        # The log, not `notes`: an Edit and Save is a partner's own routine action with a healthy
        # outcome, and health.notes is the page's problems list. Same defect as the miscellany
        # count — every Save reported itself as a problem on the page it had just rebuilt.
        common.log(f"replacing existing definition {paths.definition} (ledger kept; developments keep their ids)")
    else:
        defn["created"] = defn.get("created") or now
    defn["updated"] = now
    defn.setdefault("topics", [])
    defn.setdefault("industries", [])
    defn.setdefault("clients", [])
    defn.setdefault("budget", {})
    # Recorded so the edit dialog can show how the scan was created. Review finding: the flag
    # was a dispatch input only, never stored, so Edit always pre-ticked "discover" and a
    # "Save and re-run" silently re-enabled discovery on a scan created without it.
    defn["no_discover"] = bool(no_discover)
    # Recorded the same way and for the same reason: the edit dialog must be able to show that
    # this scan's Miscellaneous lane is off, and a later plain `run` must keep it off.
    # Unlike no_discover, no_misc is NOT a dispatch input — it can only travel inside the
    # definition — so an Edit that re-submits a definition the page read back without the key
    # used to switch the lane back on for a scan whose partner had turned it off. Absent now
    # means "unchanged", the same way `created` is preserved above: this create's own flag wins,
    # then what the incoming definition says, then what the definition on disk already said.
    if not isinstance(defn.get("no_misc"), bool) and isinstance(existing, dict):
        defn["no_misc"] = bool(existing.get("no_misc"))
        # A schedule survives an Edit the same way: the dialog may not mention it, and losing it
        # silently would turn an unattended scan back into a manual one — or vice versa — unnoticed.
        if "schedule" not in defn and isinstance(existing, dict) and existing.get("schedule"):
            defn["schedule"] = existing["schedule"]
    # The group a layer belongs to, and the partner's per-source legal decisions, survive an Edit
    # the same way: neither is something a re-submitted form should be able to erase by omission.
    if isinstance(existing, dict):
        for k in ("group", "layer", "legal"):
            if k not in defn and existing.get(k):
                defn[k] = existing[k]
    defn["no_misc"] = bool(no_misc) or bool(defn.get("no_misc"))
    coerce_sources(defn)
    # The subject filter is STORED AS GIVEN and never invented here. propose_subject_filter is
    # what puts a suggestion in front of a partner (the create dialog, /api/propose-filter); a
    # definition that arrives without one gets no filter at all, and the run says so in health.
    # A filter nobody has seen is worse than no filter: it drops rows in the partner's name.
    coerce_subject_filter(defn)
    partner = [_candidate_from_partner(s) for s in defn.get("sources") or []]
    # Written first, with partner sources visibly ungated, so a crash in discovery leaves a
    # definition that says exactly what has and has not happened.
    defn["sources"] = [dict(c, status="pending", reason="not yet gated") for c in partner]
    common.atomic_write_json(paths.definition, defn)
    common.log(f"wrote {paths.definition}")

    budget = _budget(defn)
    candidates: list[dict] = list(partner)
    if not no_discover:
        try:
            found = discover_propose(defn, client, budget) or []
            for c in found:
                if isinstance(c, dict) and _is_url(c.get("url")):
                    c = dict(c)
                    c.setdefault("proposed_by", "discovery")
                    c.setdefault("tier", "discovered")
                    c["host"] = _host(c["url"])
                    candidates.append(c)
                else:
                    notes.append(f"discovery returned an unusable candidate: {str(c)[:120]}")
            common.log(f"discovery proposed {len(found)} candidate(s)")
        except Exception as e:
            notes.append(f"discovery failed — only partner sources were gated: {_err(e)}")
            common.log(notes[-1])
    seen: set = set()
    uniq: list[dict] = []
    for c in candidates:
        k = canon_url(c["url"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    cap_c = int(budget["max_candidates"])
    if len(uniq) > cap_c:
        budget.note_drop(f"{len(uniq) - cap_c} candidate(s) beyond max_candidates={cap_c} were not gated")
        uniq = uniq[:cap_c]

    extractor = bind_extractor(client, defn, budget)
    cap_s = int(budget["max_sources"])
    approved_n = 0
    results: list[dict] = []
    for cand in uniq:
        if approved_n >= cap_s:
            results.append(_normalise_source({"status": "pending",
                                              "reason": f"budget: max_sources={cap_s} already approved — not gated"},
                                             cand, ""))
            continue
        try:
            src = gate_assess(cand, budget, extractor)
            src = _normalise_source(src, cand, "gate returned no decision")
        except Exception as e:
            src = _normalise_source({"status": "pending", "reason": f"gate unavailable: {_err(e)}"}, cand, "")
        if src["status"] == "approved":
            approved_n += 1
        results.append(src)
        common.log(f"gate {src['status']:8s} {src['url']}" + (f" — {src.get('reason')}" if src.get("reason") else ""))
    defn["sources"] = results
    defn["updated"] = common.now_ist()
    common.atomic_write_json(paths.definition, defn)
    notes.extend(budget.dropped)
    # What discovery searched for and did not find, or found and dropped, is part of the coverage
    # claim and must outlive this create: a plain `run` rebuilds health from scratch (review
    # finding), so the account lives in the definition and every run re-seeds its notes from it.
    defn["discovery_notes"] = [n for n in dict.fromkeys(notes) if n.lower().startswith("discovery")]
    summary, code = run_scan(defn, paths, client, max_new=max_new, notes=notes, first_run=True)
    summary["gated"] = {"approved": approved_n, "pending": sum(1 for s in results if s["status"] == "pending"),
                        "rejected": sum(1 for s in results if s["status"] == "rejected")}
    # How many candidates were gated at all — the number a `no_discover` create is judged by:
    # six partner-chosen venues must cost six gates, not the twenty-five max_candidates allows.
    summary["candidates"] = len(uniq)
    return summary, code


# ----------------------------------------------------------------------------- dry-run fixtures
def _fixture_path(kind: str, url: str, ext: str) -> Path:
    return FIXTURES / f"{kind}.{_host(url)}.{ext}"


def dry_fetch_listing(url: str, delay: float, allowed_hosts: list[str]) -> tuple[bytes, dict]:
    p = _fixture_path("listing", url, "html")
    if not p.exists():
        raise common.FetchRefused(f"dry-run: no listing fixture for {_host(url)} (expected {p.name})")
    return p.read_bytes(), {"http": 200, "fixture": p.name}


def dry_listing_rows(body: bytes, url: str, client, defn: dict, budget: common.Budget):
    """Deterministic rows from a fixture listing. The fixtures mark rows as `li.row`, which is
    a shape we control — the real extractor reads arbitrary pages with the model."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "lxml")
    rows, seen = [], 0
    for li in soup.select("li.row"):
        seen += 1
        a = li.select_one("a.title")
        t = li.select_one("time")
        snip = li.select_one(".snippet")
        if not a or not a.get("href"):
            continue
        rows.append({"title": a.get_text(" ", strip=True), "date": t.get("datetime") if t else None,
                     "url": a["href"], "snippet": snip.get_text(" ", strip=True) if snip else ""})
    return rows, {"seen": seen, "dropped": {"no link": seen - len(rows)} if seen - len(rows) else {}}


def _fixture_documents(url: str) -> dict:
    p = _fixture_path("documents", url, "txt")
    if not p.exists():
        raise common.FetchRefused(f"dry-run: no document fixture for {_host(url)} (expected {p.name})")
    docs, cur, buf = {}, None, []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^==== (\S+) ====$", line)
        if m:
            if cur:
                docs[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), []
        else:
            buf.append(line)
    if cur:
        docs[cur] = "\n".join(buf).strip()
    return docs


def dry_document_text(url: str, budget: common.Budget, client, report: Optional[dict] = None) -> tuple[str, str]:
    docs = _fixture_documents(url)
    path = urlparse(url).path
    if path not in docs:
        raise ValueError(f"dry-run: no fixture document for {path}")
    if report is not None:
        report.update({"reason": "", "chars": len(docs[path]), "truncated": False})
    return docs[path], "text"


# Canned enrichment for the demo, keyed by document path. Quotes are checked against the text
# below before they are returned — the stub must obey the same rule the real enricher does, or
# the demo would show a citation the UI cannot verify.
DRY_ENRICH = {
    "/eli/id/2026/08/12/26G00130/sg": {
        "headline": "Italy transposes the Pay Transparency Directive; annual reporting from 100 employees, first report due 30 June 2027.",
        "summary": [
            {"text": "Employers with at least 100 employees must report their gender pay gap by 30 June each year from 2027.",
             "cite": {"quote": "Employers with at least one hundred employees shall report, by 30 June of each year starting from 2027, the gender pay gap", "where": "Art. 4(1)"}},
            {"text": "An unjustified gap of 5% or more in any category, not remedied within six months, triggers a mandatory joint pay assessment with worker representatives.",
             "cite": {"quote": "of at least five per cent in any category of workers", "where": "Art. 6(1)"}},
            {"text": "Applicants gain a right to the pay range before interview, and employers may no longer ask about pay history.",
             "cite": {"quote": "Employers shall not ask applicants about their pay history", "where": "Art. 5(2)"}},
        ],
        "obligations": [
            {"who": "Employers with ≥100 employees in Italy", "what": "annual gender pay-gap report", "when": "by 2027-06-30, then yearly"},
            {"who": "Employers with 100–149 employees", "what": "report every three years", "when": "first report 2027"},
        ],
        "type": "Legislation", "topics": ["Pay equity", "Employment"], "jurisdiction": "IT",
        "relevance": {"level": "high",
                      "why": "First transposition among the scan's jurisdictions; fixes the reporting threshold and first deadline the intent asks about.",
                      "action": "Run a pay-gap diagnostic for clients with 100+ employees in Italy before the 2027 reporting window."},
        "confidence": "high",
    },
    "/eli/id/2026/08/28/26A04512/sg": {
        "headline": "Italy fixes the filing channel and window for the pay-gap report: Servizi Lavoro portal, 1 March to 30 June 2027.",
        "summary": [
            {"text": "The report can only be filed through the ministry's Servizi Lavoro portal, using the annexed electronic form.",
             "cite": {"quote": "shall be transmitted exclusively through the Servizi Lavoro portal", "where": "Art. 1(1)"}},
            {"text": "The first filing window runs from 1 March to 30 June 2027; a report in any other format is treated as not submitted.",
             "cite": {"quote": "The first transmission window opens on 1 March 2027 and closes on 30 June 2027.", "where": "Art. 2(1)"}},
        ],
        "obligations": [{"who": "Employers subject to Art. 4 reporting", "what": "file via Servizi Lavoro portal, annexed form only", "when": "2027-03-01 to 2027-06-30"}],
        "type": "Rules/Regulations", "topics": ["Pay equity", "Employment"], "jurisdiction": "IT",
        "relevance": {"level": "medium", "why": "Procedural, but sets the concrete filing window and the format that counts as compliance.",
                      "action": "Diarise the 2027 window; confirm portal credentials for Italian entities."},
        "confidence": "high",
    },
    "/eli/id/2026/07/30/26A04210/sg": {
        "headline": "Italian labour inspectorate: headcount is the prior-year average, per legal entity; inspections start only after the first deadline.",
        "summary": [
            {"text": "The 100-employee threshold is the average headcount of the preceding calendar year, with part-time staff counted pro rata.",
             "cite": {"quote": "calculated as the average number of employees in the calendar year preceding the report, counting part-time workers pro rata", "where": "para 1"}},
            {"text": "Group headcount is not aggregated; each entity is tested alone.",
             "cite": {"quote": "the headcount of a corporate group is not aggregated", "where": "para 2"}},
        ],
        "obligations": [],
        "type": "Guidance/Advisory", "topics": ["Pay equity", "Employment"], "jurisdiction": "IT",
        "relevance": {"level": "medium", "why": "Answers the threshold-computation question multinational groups with several Italian entities will ask first.",
                      "action": "Map Italian entities against the prior-year average headcount rule."},
        "confidence": "high",
    },
    "/avvisi/rettifica-26G00130": {
        "headline": "Corrigendum to the Italian transposition decree: 100–149 employee reporters owe a first report in 2027.",
        "summary": [
            {"text": "The three-yearly cycle for employers with 100 to 149 employees now expressly starts with a 2027 report.",
             "cite": {"quote": "the first report being due in 2027", "where": "corrigendum"}},
        ],
        "obligations": [{"who": "Employers with 100–149 employees", "what": "first pay-gap report", "when": "2027"}],
        "type": "Notice/Circular", "topics": ["Pay equity"], "jurisdiction": "IT",
        "relevance": {"level": "medium", "why": "Removes the ambiguity over whether smaller reporters could wait until 2030.",
                      "action": "Treat the 2027 deadline as applying to all Italian reporters above 100 employees."},
        "confidence": "medium",
    },
    "/eli/id/2026/06/19/26A03900/sg": {
        "headline": "Italy sets the statutory interest rate for H2 2026 — unrelated to pay transparency.",
        "summary": [{"text": "The statutory interest rate for the second half of 2026 is 2.0% per annum.",
                     "cite": {"quote": "is set at 2.0 per cent per annum", "where": "communication"}}],
        "obligations": [], "type": "Notice/Circular", "topics": [], "jurisdiction": "IT",
        "relevance": {"level": "low", "why": "Routine finance-ministry notice with no bearing on the intent.", "action": "None."},
        "confidence": "high",
    },
    "/labour/pay-transparency/referentenentwurf-2026": {
        "headline": "Germany publishes its draft transposition bill: reporting from 100 employees, fines up to €100,000, consultation to 30 September.",
        "summary": [
            {"text": "The draft extends the individual right to pay information to every employer, dropping the 200-employee floor.",
             "cite": {"quote": "extends the individual right to information to all employers regardless of size", "where": "B"}},
            {"text": "Employers with 250 or more employees would report first for calendar 2026; those with 100 or more report annually.",
             "cite": {"quote": "employers with 250 or more employees shall report for the first time in respect of the 2026 calendar year", "where": "B"}},
            {"text": "Failure to report would carry a fine of up to €100,000, and the association consultation closes on 30 September 2026.",
             "cite": {"quote": "The consultation period for the associations ends on 30 September 2026.", "where": "C"}},
        ],
        "obligations": [{"who": "Employers with ≥250 employees in Germany (draft)", "what": "first gender pay-gap report", "when": "in respect of calendar 2026"}],
        "type": "Consultation/Draft", "topics": ["Pay equity", "Employment"], "jurisdiction": "DE",
        "relevance": {"level": "high", "why": "Germany is the scan's largest jurisdiction and the draft proposes reporting on 2026 data — earlier than the Directive requires.",
                      "action": "Clients with 250+ German employees should treat 2026 as a reporting year now; consider a consultation response before 30 September."},
        "confidence": "medium",
    },
    "/labour/pay-transparency/faq-thresholds": {
        "headline": "German ministry FAQ: no new obligations for 100–249 employee employers until the amending Act is in force.",
        "summary": [
            {"text": "Until the amending Act takes effect the existing Entgelttransparenzgesetz applies unchanged.",
             "cite": {"quote": "the existing Entgelttransparenzgesetz continues to apply and no new obligations arise", "where": "FAQ 2"}},
            {"text": "Mid-sized employers report every three years, with the Directive's own first deadline of 7 June 2031 unless the bill brings it forward.",
             "cite": {"quote": "the first report is due by 7 June 2031 under the Directive's timetable", "where": "FAQ 1"}},
        ],
        "obligations": [],
        "type": "Guidance/Advisory", "topics": ["Pay equity", "Employment"], "jurisdiction": "DE",
        "relevance": {"level": "low", "why": "Confirms the status quo; useful only as a holding position for mid-sized clients.", "action": "Monitor; nothing to do until the bill passes."},
        "confidence": "high",
    },
}


def dry_enrich(defn: dict, dev: dict, text: str, client, budget: common.Budget) -> dict:
    from . import enrich as enrich_mod
    path = urlparse(dev["url"]).path
    canned = DRY_ENRICH.get(path)
    if not canned:
        # Generic but honest: the first substantive sentence of the document, quoted verbatim.
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", common.norm_ws(text)) if len(s.strip()) > 40]
        first = sents[0] if sents else common.norm_ws(text)[:200]
        canned = {"headline": dev["title"][:160],
                  "summary": [{"text": first, "cite": {"quote": first, "where": "opening"}}],
                  "obligations": [], "type": "Other", "topics": [], "jurisdiction": dev.get("jurisdiction", ""),
                  "relevance": {"level": "low", "why": "No canned reading for this fixture.", "action": "Read the document."},
                  "confidence": "low"}
    out = json.loads(json.dumps(canned))
    # The demo must speak the enricher's vocabulary. Review finding: the canned records wrote
    # "Secondary legislation", "Corrigendum", "Document" — none in enrich.TYPES — so the demo
    # page showed a type filter no real scan could ever produce.
    if out.get("type") not in enrich_mod.TYPES:
        out["type"] = "Other"
    hay = common.norm_ws(text)
    # Same contract as the live enricher: every paragraph carries its own verdict and an
    # unverifiable one is kept and marked, never dropped (review finding — the dry ledger had no
    # summary[i].verified at all, so the page's "unverified" mark was never exercised).
    for s in out["summary"]:
        s["verified"] = common.norm_ws(s["cite"]["quote"]) in hay
        if not s["verified"]:
            s["note"] = "quote not found in the document text"
    kept = [s for s in out["summary"] if s["verified"]]
    out["verified_ratio"] = round(len(kept) / len(out["summary"]), 2) if out["summary"] else 0.0
    if len(kept) != len(out["summary"]):
        out["note"] = f"{len(out['summary']) - len(kept)} sentence(s) could not be verified against the text"
    clients = _client_names(defn)
    if clients:
        lvl = out["relevance"]["level"]
        out["relevance"]["clients"] = {c: lvl for c in clients}
    return out


def dry_gate(candidate: dict, budget: common.Budget, extractor: Callable) -> dict:
    """The gate's decision shape, from the fixtures: approved on a dated listing above a small
    floor, pending when the listing exists but reads thin, rejected when the fixture is missing
    — mirroring reachable / unreadable / unfetchable."""
    src = dict(candidate)
    floor = 2
    # extract stays None until the extractor has actually run (same rule as gate.py): a
    # pre-filled {rows: 0} read on the page as "0 rows parsed" for a source never fetched.
    gate = {"reachable": False, "http": None, "robots": "dry-run: not checked",
            "tos": {"checked": [], "flags": []}, "extract": None,
            "checked": common.now_ist()}
    try:
        body, info = fetch_listing(src["url"], budget["delay_seconds"], _declared_hosts(src["url"]))
        gate["reachable"], gate["http"] = True, info.get("http", 200)
        rows = extractor(body, src["url"], candidate)
    except Exception as e:
        src.update({"status": "rejected", "reason": f"not fetchable: {_err(e)}", "gate": gate})
        return src
    dated = sum(1 for r in rows if r.get("date"))
    gate["extract"] = {"rows": len(rows), "dated": dated, "floor": floor}
    if dated >= floor:
        src.update({"status": "approved", "gate": gate})
    else:
        src.update({"status": "pending", "reason": f"listing test produced {dated} dated row(s), floor {floor}", "gate": gate})
    return src


def dry_discover(defn: dict, client, budget: common.Budget) -> list[dict]:
    common.log("dry-run: discovery skipped (no web search without a network)")
    return []


# Canned search results for the Miscellaneous lane's dry run. Unlike discovery, the lane is NOT
# skipped here: it runs its real code — normalisation, the coverage-host drop, dedupe, the cap and
# the merge — over a canned answer, so the demo exercises the whole lane and produces a misc.json
# a partner (and the promote CLI) can be shown. The hosts are reserved .test names; the one on
# gazette.example.test is there to be dropped, because that host is coverage.
DRY_MISC_FINDINGS = [
    {"url": "https://boe.example.test/diario/2026/09/01/rd-812-2026", "host": "boe.example.test",
     "title": "Real Decreto 812/2026 transposing Directive (EU) 2023/970",
     "date": "2026-09-01", "jurisdiction": "ES", "kind": "official_venue",
     "why": "Spain's official gazette publishes the transposition decrees this scan is about, and it is not on the coverage list.",
     "snippet": "Real Decreto 812/2026, de 28 de agosto, por el que se transpone la Directiva (UE) 2023/970."},
    {"url": "https://parlement.example.test/dossiers/transparence-salariale", "host": "parlement.example.test",
     "title": "Dossier législatif — transparence salariale", "date": "", "jurisdiction": "FR",
     "kind": "official_venue",
     "why": "The French parliament's dossier page tracks the transposition bill; no French venue is covered yet.",
     "snippet": "Projet de loi portant transposition de la directive (UE) 2023/970."},
    {"url": "https://press.example.test/story/spain-pay-gap-deadline", "host": "press.example.test",
     "title": "Spain sets first pay-gap reporting deadline for 2027", "date": "2026-09-02",
     "jurisdiction": "ES", "kind": "secondary",
     "why": "Reports the decree above and names the first reporting year.",
     "snippet": "Employers with 100 or more staff must report by June 2027, the ministry said."},
    {"url": "https://gazette.example.test/serie-generale/2026/118", "host": "gazette.example.test",
     "title": "Decreto legislativo 118/2026", "date": "2026-08-12", "jurisdiction": "IT",
     "kind": "official_venue", "why": "Already covered — dropped, because that is coverage, not miscellany.",
     "snippet": ""},
]


def dry_misc(defn: dict, client, previous: Optional[dict]) -> tuple[dict, dict]:
    from . import misc
    canned = common.FakeClient(canned={misc.SCHEMA_NAME: {"findings": DRY_MISC_FINDINGS,
                                                          "notes": ["Dry run: canned search results."]}})
    return misc.scan(defn, canned, previous=previous)


def _dry_digest(kw: dict) -> dict:
    """Canned digest that cites the ids it was actually shown, parsed back out of the prompt —
    so the demo digest exercises the verifier on real ids rather than on a hard-coded list."""
    shown = []
    for line in (kw.get("user") or "").splitlines():
        if not line.startswith("- "):
            continue
        parts = line[2:].split(" · ")
        if len(parts) < 5:
            continue
        shown.append({"id": parts[0], "date": parts[1], "jur": parts[2],
                      "headline": " · ".join(parts[3:-1]), "level": parts[-1]})
    high = [s for s in shown if s["level"] == "high"] or shown[:1]
    others = [s for s in shown if s not in high]
    body = []
    if high:
        body.append({"text": "New this period: " + " ".join(s["headline"] for s in high[:2]),
                     "cites": [s["id"] for s in high[:2]]})
        body.append({"text": f"Prioritise {high[0]['jur']}: {high[0]['headline']}", "cites": [high[0]["id"]]})
    if others:
        body.append({"text": "Also on the radar: " + "; ".join(s["headline"].rstrip(".") for s in others[:3]) + ".",
                     "cites": [s["id"] for s in others[:3]]})
    return {"headline": (f"{len(high)} high-relevance development(s) across "
                         f"{len({s['jur'] for s in shown})} jurisdiction(s); {high[0]['jur'] if high else '—'} leads."),
            "body": body}


def enable_dry_run() -> common.FakeClient:
    """Route every network-touching step to the fixtures and return the fake model. The
    digest's schema name is ours; the sibling modules' names are not, which is why their steps
    are replaced wholesale rather than fed canned responses through the client."""
    global fetch_listing, listing_rows, document_text, enrich_dev, discover_propose, gate_assess, misc_scan, _DRY
    fetch_listing = dry_fetch_listing
    listing_rows = dry_listing_rows
    document_text = dry_document_text
    enrich_dev = dry_enrich
    discover_propose = dry_discover
    gate_assess = dry_gate
    misc_scan = dry_misc
    _DRY = True
    from . import digest as digest_mod
    return common.FakeClient(canned={digest_mod.NAME: _dry_digest})


def restore_live() -> None:
    global fetch_listing, listing_rows, document_text, enrich_dev, discover_propose, gate_assess, misc_scan, _DRY
    fetch_listing, listing_rows, document_text = _default_fetch_listing, _default_listing_rows, _default_document_text
    enrich_dev, discover_propose, gate_assess = _default_enrich, _default_discover, _default_gate
    misc_scan = _default_misc
    _DRY = False


# ----------------------------------------------------------------------------- CLI
def _client_for(dry_run: bool):
    if dry_run:
        return enable_dry_run()
    client = common.openai_client()
    # Ask before working. A wrong model name used to surface as "discovery failed" twelve minutes
    # in, with the results already written and then discarded; now it stops the run in seconds and
    # names the models the key actually has.
    common.preflight(client, [common.MODEL, common.MODEL_STRONG])
    return client


def _print_summary(summary: dict, out: Optional[str] = None) -> None:
    """The SUMMARY line on stdout for a human reading the log, and — when --summary-out is
    given — the same JSON in a file the workflow reads. Review finding: the workflow grepped
    the merged log for `^SUMMARY `, so any future print of fetched page text on stdout could
    have forged the line and, through it, the commit message."""
    line = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    print("SUMMARY " + line, flush=True)
    if out:
        common.atomic_write_text(Path(out), line + "\n")


def _reserved(cmd: str, scan_id: str) -> bool:
    if scan_id in RESERVED_IDS:
        print(f"{cmd}: id '{scan_id}' is reserved (reserved: {', '.join(sorted(RESERVED_IDS))})", file=sys.stderr)
        return True
    return False


def cmd_create(args) -> int:
    if bool(args.json) == bool(args.from_json):
        print("create: give exactly one of --json STRING or --from-json FILE", file=sys.stderr)
        return EXIT_USAGE
    try:
        raw = args.json if args.json else Path(args.from_json).read_text(encoding="utf-8")
        defn = json.loads(raw)
    except Exception as e:
        print(f"create: definition is not valid JSON: {_err(e)}", file=sys.stderr)
        return EXIT_USAGE
    problems = validate_definition(defn)
    if problems:
        for p in problems:
            print(f"create: {p}", file=sys.stderr)
        return EXIT_USAGE
    defn.setdefault("id", common.slug(defn["name"]))
    if not ID_RE.match(defn["id"]):
        print(f"create: derived id '{defn['id']}' is not a valid id — set one explicitly", file=sys.stderr)
        return EXIT_USAGE
    if _reserved("create", defn["id"]):
        return EXIT_USAGE
    client = _client_for(args.dry_run)
    # Only after the dry-run flag is set: budget_notes() reads it, and printing first reported a
    # clamp of delay_seconds=0 that a dry run never applies (review finding).
    for n in budget_notes(defn):
        print(f"create: {n}", file=sys.stderr)
    paths = paths_for(defn["id"])
    # The flag may arrive as a dispatch input (--no-discover) or inside the definition
    # (api/scans.js passes both); either is the partner's choice.
    no_discover = bool(args.no_discover) or bool(defn.get("no_discover"))
    # A create is the only run nobody has ever seen results from, so it reads FIRST_RUN_MAX_NEW
    # documents rather than the budget's full max_new_per_run and the page appears in minutes.
    # An explicit --max-new wins: a caller who named a number meant that number.
    max_new = args.max_new if getattr(args, "max_new", None) is not None else FIRST_RUN_MAX_NEW
    summary, code = create_scan(defn, paths, client, no_discover=no_discover, max_new=max_new,
                                no_misc=bool(getattr(args, "no_misc", False)))
    summary["action"] = "create"
    _print_summary(summary, getattr(args, "summary_out", None))
    return code


def cmd_run(args) -> int:
    if not ID_RE.match(args.id or ""):
        print("run: --id must match ^[a-z0-9][a-z0-9-]{1,59}$", file=sys.stderr)
        return EXIT_USAGE
    if _reserved("run", args.id):
        return EXIT_USAGE
    paths = paths_for(args.id)
    defn = common.load_json(paths.definition, None)
    if not defn:
        print(f"run: no definition at {paths.definition}", file=sys.stderr)
        return EXIT_USAGE
    problems = validate_definition(defn)
    if problems:
        for p in problems:
            print(f"run: definition invalid: {p}", file=sys.stderr)
        return EXIT_USAGE
    if defn.get("demo") and not args.dry_run:
        # The demo's hosts are reserved .test names: a live run would only produce FAILED
        # sources. Say so rather than let the workflow burn a run on it.
        print("run: this is the demo scan; run it with --dry-run (its sources are fixtures)", file=sys.stderr)
        return EXIT_USAGE
    client = _client_for(args.dry_run)
    # Only after the dry-run flag is set: budget_notes() reads it, and printing first reported a
    # clamp of delay_seconds=0 that a dry run never applies (review finding).
    for n in budget_notes(defn):
        print(f"run: {n}", file=sys.stderr)
    summary, code = run_scan(defn, paths, client, max_new=args.max_new,
                             no_misc=bool(getattr(args, "no_misc", False)))
    summary["action"] = "run"
    _print_summary(summary, getattr(args, "summary_out", None))
    return code


def cmd_delete(args) -> int:
    if not ID_RE.match(args.id or ""):
        print("delete: --id must match ^[a-z0-9][a-z0-9-]{1,59}$", file=sys.stderr)
        return EXIT_USAGE
    if _reserved("delete", args.id):
        return EXIT_USAGE
    paths = paths_for(args.id)
    removed = []
    if paths.definition.exists():
        # Only a scan definition is unlinked: the file must load as an object whose `id` is the
        # id asked for. Anything else under scans/ with that stem is not ours to remove.
        try:
            existing = common.load_json(paths.definition, None)
        except Exception:
            existing = None
        if not isinstance(existing, dict) or existing.get("id") != args.id:
            print(f"delete: {paths.definition} is not a scan definition with id '{args.id}' — not removed", file=sys.stderr)
            _print_summary({"id": args.id, "action": "delete", "removed": [], "exit": EXIT_USAGE},
                           getattr(args, "summary_out", None))
            return EXIT_USAGE
        paths.definition.unlink()
        removed.append(str(paths.definition))
    if paths.dir.exists():
        shutil.rmtree(paths.dir)
        removed.append(str(paths.dir) + "/")
    for r in removed:
        common.log(f"removed {r}")
    if not removed:
        common.log(f"nothing to remove for '{args.id}' (no definition, no data)")
    _print_summary({"id": args.id, "action": "delete", "removed": removed, "exit": 0 if removed else EXIT_USAGE},
                   getattr(args, "summary_out", None))
    return EXIT_OK if removed else EXIT_USAGE


def _finding_for(cmd: str, args) -> tuple:
    """(paths, definition, misc document, finding) for the two commands that act on one finding,
    or (None, None, None, None) after printing why not.

    One function because `promote` and `dismiss` must refuse in exactly the same way and for the
    same reasons: a mistyped scan id, a finding id that is not ten hex characters, a definition
    that is missing or invalid, a lane that has never run, an id that is not in the file. Two
    copies of this would drift, and the difference would show up as one door being stricter than
    the other for no reason a partner could see."""
    if not ID_RE.match(args.id or ""):
        print(f"{cmd}: --id must match ^[a-z0-9][a-z0-9-]{{1,59}}$", file=sys.stderr)
        return None, None, None, None
    if _reserved(cmd, args.id):
        return None, None, None, None
    fid = (args.finding or "").strip().lower()
    if not FINDING_ID_RE.match(fid):
        print(f"{cmd}: --finding must be a 10-character finding id from misc.json (hex)", file=sys.stderr)
        return None, None, None, None
    paths = paths_for(args.id)
    defn = common.load_json(paths.definition, None)
    if not isinstance(defn, dict) or defn.get("id") != args.id:
        print(f"{cmd}: no scan definition with id '{args.id}' at {paths.definition}", file=sys.stderr)
        return None, None, None, None
    problems = validate_definition(defn)
    if problems:
        for p in problems:          # every problem, not just the first: one pass to fix them all
            print(f"{cmd}: definition invalid: {p}", file=sys.stderr)
        return None, None, None, None
    doc = common.load_json(paths.misc, None)
    findings = (doc or {}).get("findings") if isinstance(doc, dict) else None
    if not isinstance(findings, list) or not findings:
        print(f"{cmd}: no Miscellaneous findings at {paths.misc} — run the scan first", file=sys.stderr)
        return None, None, None, None
    finding = next((f for f in findings if isinstance(f, dict) and f.get("id") == fid), None)
    if finding is None:
        print(f"{cmd}: no finding '{fid}' in {paths.misc.name} ({len(findings)} finding(s) there)", file=sys.stderr)
        return None, None, None, None
    return paths, defn, doc, finding


def cmd_dismiss(args) -> int:
    """Set one Miscellaneous finding aside: status `dismissed`, so later runs stop offering it.

    The contract has had this status since the lane was designed, `misc.merge` has always
    preserved it, and nothing could ever set it — so a lead a partner had judged irrelevant came
    back, in the same place, on every run for ever. That is the defect this closes.

    Any kind may be dismissed: judging a lead irrelevant is a reading of the subject, not of the
    publisher, and the whole point of the lane is that a partner triages it. Nothing is deleted —
    the finding stays in misc.json with its id, its first sighting and its link, so the judgement
    is auditable and reversible by hand. What it does NOT do is touch coverage: a finding already
    promoted has become a source, and this command refuses it rather than leave the lane and the
    coverage list disagreeing about the same URL."""
    paths, defn, doc, finding = _finding_for("dismiss", args)
    if finding is None:
        return EXIT_USAGE
    fid = finding["id"]
    url = finding.get("url") or ""
    if finding.get("status") == "promoted":
        print(f"dismiss: finding '{fid}' was promoted into this scan's coverage — dismissing the lead would "
              f"not remove the source, and a lane that says 'set aside' about a URL the scan still fetches is "
              f"worse than either answer. Remove it from the coverage list in Edit if it should not be read: "
              f"{url}", file=sys.stderr)
        return EXIT_USAGE
    if finding.get("status") == "dismissed":
        # Idempotent on purpose: two clicks, or a retried dispatch, must not read as a failure.
        common.log(f"{fid} is already dismissed — nothing changed")
        _print_summary({"id": args.id, "action": "dismiss", "finding": fid, "url": url,
                        "status": "dismissed", "changed": False, "exit": EXIT_OK},
                       getattr(args, "summary_out", None))
        return EXIT_OK
    finding["status"] = "dismissed"
    common.atomic_write_json(paths.misc, doc)
    common.log(f"dismissed {fid} → {url}")
    common.log("the finding stays in misc.json with its id and first sighting; later runs keep the status, so "
               "this lead is not offered again. Nothing was fetched, and the coverage list is unchanged.")
    _print_summary({"id": args.id, "action": "dismiss", "finding": fid, "url": url,
                    "status": "dismissed", "changed": True, "exit": EXIT_OK},
                   getattr(args, "summary_out", None))
    return EXIT_OK


def cmd_clients(args) -> int:
    """Replace the scan's client roster with the one in --from-json ({"clients": [...]}).

    Clients are named on the definition so every run rates each development against each of them
    by name; the page's Clients tab is where a partner adds one. This edits the roster and nothing
    else — no run, no re-gate — and the next run picks the new names up."""
    if not ID_RE.match(args.id or ""):
        print("clients: --id must match ^[a-z0-9][a-z0-9-]{1,59}$", file=sys.stderr)
        return EXIT_USAGE
    paths = paths_for(args.id)
    defn = common.load_json(paths.definition, None) if paths.definition.exists() else None
    if not isinstance(defn, dict) or defn.get("id") != args.id:
        print(f"clients: no scan definition with id '{args.id}'", file=sys.stderr)
        return EXIT_USAGE
    try:
        doc = common.load_json(Path(args.from_json), None)
    except Exception as e:
        print(f"clients: could not read {args.from_json}: {e}", file=sys.stderr)
        return EXIT_USAGE
    roster = doc.get("clients") if isinstance(doc, dict) else None
    if not isinstance(roster, list):
        print("clients: --from-json must hold {\"clients\": [...]}", file=sys.stderr)
        return EXIT_USAGE
    cleaned = []
    for c in roster:
        if isinstance(c, str):
            cleaned.append(common.norm_ws(c)[:120])
        elif isinstance(c, dict):
            o = {"name": common.norm_ws(str(c.get("name") or ""))[:120]}
            for k in ("scope", "sector"):
                if c.get(k):
                    o[k] = common.norm_ws(str(c[k]))[:500]
            kws = [common.norm_ws(str(k))[:120] for k in (c.get("keywords") or []) if isinstance(k, str) and common.norm_ws(str(k))]
            if kws:
                o["keywords"] = kws[:60]
            cleaned.append(o)
        else:
            cleaned.append(c)   # let the validator name it
    before = defn.get("clients")
    defn["clients"] = cleaned
    problems = validate_definition(defn)
    if problems:
        for p in problems:
            print(f"clients: {p}", file=sys.stderr)
        return EXIT_USAGE
    defn["updated"] = common.now_ist()
    common.atomic_write_json(paths.definition, defn)
    names = [c if isinstance(c, str) else c["name"] for c in cleaned]
    common.log(f"clients: {len(cleaned)} on the scan now ({', '.join(names) or 'none'}); was {len(before) if isinstance(before, list) else 0}")
    common.log("the next run rates every development against each of them by name")
    _print_summary({"id": args.id, "action": "clients", "clients": names, "exit": EXIT_OK},
                   getattr(args, "summary_out", None))
    return EXIT_OK


def cmd_legal(args) -> int:
    """Record a partner's decision on whether one source may be fetched.

    The gate gathers evidence — robots.txt, the wording of the site's own terms — and parks a
    doubtful source as pending. It never rules on legality, because that is a grey area and the
    call belongs to a person. This is where that call is written down: keyed by URL, with who
    made it and when, and `do_not_fetch` makes every later run skip the source and say so in
    health (WITHHELD). `undecided` clears a previous decision."""
    if not ID_RE.match(args.id or ""):
        print("legal: --id must match ^[a-z0-9][a-z0-9-]{1,59}$", file=sys.stderr)
        return EXIT_USAGE
    if args.decision not in LEGAL_DECISIONS:
        print(f"legal: --decision must be one of: {', '.join(LEGAL_DECISIONS)}", file=sys.stderr)
        return EXIT_USAGE
    if not _is_url(args.url):
        print("legal: --url must be an http(s) URL", file=sys.stderr)
        return EXIT_USAGE
    paths = paths_for(args.id)
    defn = common.load_json(paths.definition, None) if paths.definition.exists() else None
    if not isinstance(defn, dict) or defn.get("id") != args.id:
        print(f"legal: no scan definition with id '{args.id}'", file=sys.stderr)
        return EXIT_USAGE
    match = next((s for s in defn.get("sources") or []
                  if isinstance(s, dict) and s.get("url") and canon_url(s["url"]) == canon_url(args.url)), None)
    if match is None:
        print(f"legal: {args.url} is not on this scan's coverage list — a decision needs a source to be about", file=sys.stderr)
        return EXIT_USAGE
    url = match["url"]
    legal = defn.get("legal") if isinstance(defn.get("legal"), dict) else {}
    if args.decision == "undecided":
        legal.pop(url, None)
    else:
        legal[url] = {"decision": args.decision, "by": common.norm_ws(args.by or "")[:80] or "a partner",
                      "on": common.now_ist(), "note": common.norm_ws(args.note or "")[:500]}
    if legal:
        defn["legal"] = legal
    else:
        defn.pop("legal", None)
    problems = validate_definition(defn)
    if problems:
        for p in problems:
            print(f"legal: the decision would make the definition invalid: {p}", file=sys.stderr)
        return EXIT_USAGE
    defn["updated"] = common.now_ist()
    common.atomic_write_json(paths.definition, defn)
    common.log(f"legal {args.decision} {url} — by {legal.get(url, {}).get('by', '') or args.by or 'a partner'}")
    if args.decision == "do_not_fetch":
        common.log("every later run skips this source and records it as WITHHELD; it stays on the coverage list")
    _print_summary({"id": args.id, "action": "legal", "url": url, "decision": args.decision, "exit": EXIT_OK},
                   getattr(args, "summary_out", None))
    return EXIT_OK


def cmd_promote(args) -> int:
    """Move one Miscellaneous finding into the scan's coverage list, where the gate decides.

    This is the ONLY route from that lane into coverage (docs/horizon-design.md; the lane itself
    never fetches anything). It is one finding at a time, by hand, and it does not approve
    anything: the URL is added as `pending` with `proposed_by: "miscellany"` and the finding's own
    reason as the rationale, and the next run fetches it with the honest UA, reads robots.txt and
    the terms, and applies the extraction floor before a single row of it can reach the ledger.

    Only a finding of kind `official_venue` may be promoted. A press report or a commentary piece
    is not a venue: putting a newspaper on a coverage list would put an unofficial retelling into
    a ledger the whole product promises is made of primary instruments."""
    paths, defn, doc, finding = _finding_for("promote", args)
    if finding is None:
        return EXIT_USAGE
    fid = finding["id"]
    kind = finding.get("kind")
    if kind != "official_venue":
        # Named, with the reason, because this refusal is the boundary the lane exists behind.
        print(f"promote: finding '{fid}' is kind '{kind}', not 'official_venue' — only an official venue "
              f"can be promoted into coverage. A press report, a trade-body item or a commentary piece is "
              f"not where an instrument is published, and a ledger built on primary sources cannot cite one. "
              f"Promote the official page it reports on instead: {finding.get('url', '')}", file=sys.stderr)
        return EXIT_USAGE
    url = finding.get("url") or ""
    if not _is_url(url):
        print(f"promote: finding '{fid}' has no usable http(s) URL", file=sys.stderr)
        return EXIT_USAGE
    existing = next((s for s in defn.get("sources") or []
                     if isinstance(s, dict) and s.get("url") and canon_url(s["url"]) == canon_url(url)), None)
    if existing:
        # Nothing to do, and saying so is better than adding the URL twice: a second copy would be
        # gated again and would double-count the venue on the coverage panel.
        common.log(f"{url} is already on this scan's coverage list with status "
                   f"'{existing.get('status', 'pending')}' — nothing added")
        if finding.get("status") != "promoted":
            finding["status"] = "promoted"
            common.atomic_write_json(paths.misc, doc)
        _print_summary({"id": args.id, "action": "promote", "finding": fid, "url": url,
                        "status": existing.get("status", "pending"), "added": False, "exit": EXIT_OK},
                       getattr(args, "summary_out", None))
        return EXIT_OK
    source = {
        "url": url,
        "name": common.norm_ws(finding.get("title") or "")[:200] or _host(url),
        "host": _host(url),
        "jurisdiction": common.norm_ws(finding.get("jurisdiction") or "")[:40],
        # The lane's kinds (official_venue / secondary / commentary) are not the coverage list's
        # kinds (gazette / regulator / court / …), and guessing between them would put a label on
        # the coverage panel that nobody checked. "other" until a human or the gate says better.
        "kind": "other",
        "status": "pending",
        "tier": "discovered",
        "proposed_by": MISC_PROPOSED_BY,
        "rationale": common.norm_ws(finding.get("why") or "")[:1000] or "Promoted from the Miscellaneous lane.",
        "reason": "promoted from the Miscellaneous lane; not yet gated",
    }
    defn.setdefault("sources", []).append(source)
    problems = validate_definition(defn)
    if problems:
        for p in problems:
            print(f"promote: the promoted source would make the definition invalid: {p}", file=sys.stderr)
        return EXIT_USAGE
    defn["updated"] = common.now_ist()
    common.atomic_write_json(paths.definition, defn)
    finding["status"] = "promoted"
    common.atomic_write_json(paths.misc, doc)
    common.log(f"promoted {fid} → {url}")
    common.log("added as a pending source. Nothing has been fetched from it: the Miscellaneous lane only "
               "reads search results. The next run of this scan gates it — robots.txt, terms, extraction "
               "floor — and only an approved source is ever read into the ledger.")
    _print_summary({"id": args.id, "action": "promote", "finding": fid, "url": url,
                    "status": "pending", "added": True, "exit": EXIT_OK},
                   getattr(args, "summary_out", None))
    return EXIT_OK


def cmd_list(args) -> int:
    scans_dir, data_dir = roots()
    rows = []
    for p in sorted(scans_dir.glob("*.json")):
        if p.name == "schema.json":
            continue
        d = common.load_json(p, None)
        if not isinstance(d, dict) or not d.get("id"):
            continue
        sp = ScanPaths(d["id"], scans_dir, data_dir)
        ledger = common.load_json(sp.developments, None) or {}
        health = common.load_json(sp.health, None) or {}
        srcs = d.get("sources") or []
        rows.append({"id": d["id"], "name": d.get("name", ""), "demo": bool(d.get("demo")),
                     "sources": {s: sum(1 for x in srcs if x.get("status") == s) for s in SOURCE_STATUSES},
                     "developments": len(ledger.get("items") or []),
                     "last_run": health.get("generated"), "updated": d.get("updated")})
    for r in rows:
        s = r["sources"]
        print(f"{r['id']:32s} {r['developments']:4d} dev  sources {s['approved']} ok/{s['pending']} pending/"
              f"{s['rejected']} rejected  last run {r['last_run'] or '—'}  {r['name']}" + ("  [demo]" if r["demo"] else ""))
    if not rows:
        print(f"no scans under {scans_dir}")
    _print_summary({"action": "list", "scans": rows, "exit": 0})
    return EXIT_OK


def cmd_propose_filter(args) -> int:
    """Print a proposed subject filter and stop. Nothing is written, no scan is touched: this
    command exists so the create dialog and /api/propose-filter can show a partner a filter to
    accept, edit or clear before it has any effect on anything.

    A refused or unavailable proposal is EXIT_OK with `source: "none"` and the reason in `note` —
    "we could not propose one" is a perfectly good answer, and the scan then reads everything and
    says so, which is the state every scan was in before subject filters existed."""
    intent = str(getattr(args, "intent", "") or "")
    if len(intent.strip()) < 20:
        print("propose-filter: --intent must be at least 20 characters (the partner's own question)",
              file=sys.stderr)
        return EXIT_USAGE
    client = _client_for(bool(getattr(args, "dry_run", False)))
    filt, note = propose_subject_filter(intent, list(getattr(args, "topic", []) or []), client)
    out = {"action": "propose-filter", "subject_filter": filt, "note": note, "exit": EXIT_OK}
    _print_summary(out, getattr(args, "summary_out", None))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pipeline.scan.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--selftest", action="store_true", help="exercise the orchestrator offline")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("create", help="validate, write the definition, discover + gate, then run")
    c.add_argument("--json", help="the definition as a JSON string")
    c.add_argument("--from-json", help="path to a JSON definition")
    c.add_argument("--no-discover", action="store_true", help="gate only the partner's sources")
    c.add_argument("--dry-run", action="store_true", help="fixtures + FakeClient; no network, no key")
    c.add_argument("--max-new", type=int, default=None,
                   help=f"enrich at most N on this first run (default {FIRST_RUN_MAX_NEW}; the rest queue)")
    r = sub.add_parser("run", help="fetch approved sources, enrich what is new, write the digest")
    r.add_argument("--id", required=True)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max-new", type=int, default=None, help="enrich at most N this run (below the budget)")
    d = sub.add_parser("delete", help="remove the definition and its data directory")
    d.add_argument("--id", required=True)
    m = sub.add_parser("promote", help="move one Miscellaneous finding's URL into the scan's sources, to be gated")
    m.add_argument("--id", required=True)
    m.add_argument("--finding", required=True, metavar="FINDING_ID",
                   help="a 10-character finding id from data/scans/<id>/misc.json (kind must be official_venue)")
    x = sub.add_parser("dismiss", help="set one Miscellaneous finding aside so later runs stop offering it")
    x.add_argument("--id", required=True)
    x.add_argument("--finding", required=True, metavar="FINDING_ID",
                   help="a 10-character finding id from data/scans/<id>/misc.json (any kind; not one already promoted)")
    cs = sub.add_parser("clients", help="replace the scan's client roster from a JSON file {clients: [...]}")
    cs.add_argument("--id", required=True)
    cs.add_argument("--from-json", required=True, metavar="FILE")
    lg = sub.add_parser("legal", help="record a partner's decision on whether one source may be fetched")
    lg.add_argument("--id", required=True)
    lg.add_argument("--url", required=True, help="a URL on the scan's coverage list")
    lg.add_argument("--decision", required=True, choices=LEGAL_DECISIONS)
    lg.add_argument("--by", default="", help="who decided (the service passes the signed-in user)")
    lg.add_argument("--note", default="", help="why, in the partner's words")
    sub.add_parser("list", help="every scan with its source and development counts")
    # Proposes a subject filter and PRINTS it. It writes no definition and starts no run: the
    # partner is the one who decides whether the scan is aimed this way. /api/propose-filter and
    # the create dialog are the callers this exists for.
    f = sub.add_parser("propose-filter", help="propose a subject filter from an intent and topics (prints JSON; stores nothing)")
    f.add_argument("--intent", required=True, help="the partner's question, in their own words")
    f.add_argument("--topic", action="append", default=[], metavar="TOPIC", help="a topic (repeatable)")
    f.add_argument("--dry-run", action="store_true", help="FakeClient; no key, no call")
    for p in (c, r):
        p.add_argument("--no-misc", action="store_true",
                       help="skip the Miscellaneous lane (the open-web search for things outside coverage)")
    for p in (c, r, d, m, x, cs, lg, f):
        p.add_argument("--summary-out", metavar="FILE", default=None,
                       help="also write the SUMMARY JSON to FILE (the workflow reads this, not the log)")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        selftest()
        return EXIT_OK
    if args.cmd == "create":
        return cmd_create(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "delete":
        return cmd_delete(args)
    if args.cmd == "promote":
        return cmd_promote(args)
    if args.cmd == "dismiss":
        return cmd_dismiss(args)
    if args.cmd == "legal":
        return cmd_legal(args)
    if args.cmd == "clients":
        return cmd_clients(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "propose-filter":
        return cmd_propose_filter(args)
    ap.print_help()
    return EXIT_USAGE


# ----------------------------------------------------------------------------- selftest
def _check_workflow() -> str:
    """scan.yml must never interpolate a dispatch input into a shell body. Checked structurally
    when PyYAML is present, textually otherwise — the textual check is deliberately stricter
    than necessary (any `${{ inputs.` inside a run block fails)."""
    wf_path = common.ROOT / ".github" / "workflows" / "scan.yml"
    text = wf_path.read_text(encoding="utf-8")
    # An actual trigger key, not the comment that explains why there is none.
    assert not re.search(r"^\s*schedule:", text, re.M), "scan.yml must be dispatch-only"
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml:
        wf = yaml.safe_load(text)
        on = wf.get("on") if "on" in wf else wf.get(True)
        assert set(on) == {"workflow_dispatch"}, f"triggers: {list(on)}"
        inputs = on["workflow_dispatch"]["inputs"]
        assert set(inputs) == {"action", "scan_id", "scan", "no_discover", "finding_id"}, list(inputs)
        assert inputs["action"]["type"] == "choice" and inputs["action"]["required"] is True
        # `dismiss` (run.py dismiss --id … --finding …) is wired into scan.yml separately — that
        # file is not this module's to edit — so the four below are required and in order, and
        # `dismiss` is the only addition this check will accept. When it IS there, it must be
        # built exactly like promote: a quoted array element, never a shell interpolation.
        opts = inputs["action"]["options"]
        assert opts[:4] == ["create", "run", "delete", "promote"], opts
        assert set(opts) <= {"create", "run", "delete", "promote", "dismiss"}, opts
        assert inputs["finding_id"]["required"] is False and inputs["finding_id"]["default"] == ""
        assert inputs["no_discover"]["type"] == "boolean" and inputs["no_discover"]["default"] is False
        assert wf["permissions"] == {"contents": "write"}
        assert wf["concurrency"]["group"] == "tmt-scan-${{ inputs.scan_id }}"
        assert wf["concurrency"]["cancel-in-progress"] is False
        job = list(wf["jobs"].values())[0]
        assert job["timeout-minutes"] == 60
        steps = job["steps"]
        assert any("checkout" in str(s.get("uses", "")) for s in steps)
        py = [s for s in steps if "setup-python" in str(s.get("uses", ""))][0]
        assert str(py["with"]["python-version"]) == "3.11" and py["with"]["cache"] == "pip"
        for s in steps:
            body = s.get("run") or ""
            assert "${{ inputs." not in body and "${{ github.event.inputs" not in body, \
                f"step {s.get('name')} interpolates an input into its shell body"
        assert any("requirements.txt" in (s.get("run") or "") for s in steps)
        assert any("git push" in (s.get("run") or "") for s in steps)
        assert any("::notice" in (s.get("run") or "") for s in steps)
        run_step = [s for s in steps if s.get("id") == "run"][0]["run"]
        # The summary comes from a file run.py writes (--summary-out), never grepped out of the
        # log, and the id that reaches the commit message is re-validated in the step itself.
        assert "--summary-out" in run_step and "grep '^SUMMARY '" not in run_step, "summary must come from --summary-out"
        assert "re.fullmatch(r\"[a-z0-9][a-z0-9-]{1,59}\"" in run_step, "the summary id must be re-validated"
        assert "\"schema\", \"tmt-india\"" in run_step, "reserved ids must be refused as a commit-message id"
        # promote: the finding id is shape-checked in the workflow before it reaches any command,
        # exactly like scan_id, and is passed as a quoted array element.
        validate = [x for x in steps if x.get("name") == "Validate inputs"][0]["run"]
        assert "'^[0-9a-f]{10}$'" in validate and "finding_id is required for" in validate, validate
        assert 'promote) ARGS=(promote --id "$SCAN_ID" --finding "$FINDING_ID"' in run_step, run_step
        if "dismiss" in opts:
            assert 'dismiss) ARGS=(dismiss --id "$SCAN_ID" --finding "$FINDING_ID"' in run_step, run_step
            assert "dismiss" in validate, "dismiss must require finding_id like promote does"
        return "yaml"
    # Textual fallback: split the file at `run:` and make sure no input expression follows one.
    for chunk in re.split(r"\n\s+run:\s*\|", text)[1:]:
        body = chunk.split("\n      - ")[0]
        assert "${{ inputs." not in body and "${{ github.event.inputs" not in body
    for needle in ("workflow_dispatch:", "type: choice", "timeout-minutes: 60", "contents: write",
                   "tmt-scan-${{ inputs.scan_id }}", "cancel-in-progress: false", "::notice",
                   "--summary-out", "re.fullmatch(r\"[a-z0-9][a-z0-9-]{1,59}\"",
                   "options: [create, run, delete, promote", "'^[0-9a-f]{10}$'"):
        assert needle in text, f"scan.yml lacks {needle}"
    assert "grep '^SUMMARY '" not in text
    return "text"


def selftest() -> None:
    import tempfile
    demo = json.loads((FIXTURES / "demo-definition.json").read_text(encoding="utf-8"))

    # Everything below runs under TMT_SCAN_ROOT, so the repo's own scans/ must come out byte
    # for byte as it went in. Defect this closes: the guard at the end asserted that
    # scans/eu-pay-transparency-directive-scan.json did not exist — which stopped being true
    # the day the shipped demo scan was committed under exactly that name, so the selftest
    # failed on the very file it was meant to be protecting. A before/after snapshot says the
    # real thing ("nothing here was written"), and keeps saying it whatever scans exist.
    def _scans_dir_state() -> dict:
        if not common.SCANS_DIR.exists():
            return {}
        return {p.name: p.read_bytes() for p in sorted(common.SCANS_DIR.glob("*.json"))}

    scans_before = _scans_dir_state()

    # 1. The validator: the demo passes; a broken definition reports every problem.
    assert validate_definition(demo) == [], validate_definition(demo)
    bad = {"id": "Bad_ID", "name": "ab", "intent": "too short", "jurisdictions": [],
           "clients": [{"nam": "x"}], "sources": [{"url": "ftp://x"}, {"url": "https://ok.test", "status": "maybe"}],
           "budget": {"max_sources": -1, "bogus": 3}, "demo": "yes", "jurisdiction": ["IT"]}
    probs = validate_definition(bad)
    for needle in ("id must match", "name must be", "intent must be", "jurisdictions must be",
                   "clients[0]", "sources[0].url", "sources[1].status", "budget.max_sources",
                   "budget.bogus", "demo must be", "unknown field 'jurisdiction'"):
        assert any(needle in p for p in probs), (needle, probs)
    assert validate_definition("nope") == ["definition must be a JSON object"]
    # validate_definition mirrors scans/schema.json by hand (no jsonschema dependency in the
    # pipeline), so the two are checked against each other here rather than left to drift.
    schema = common.load_json(common.SCANS_DIR / "schema.json", None)
    if schema:
        # The top-level key set too, not only the source's. Defect this closes: `discovery_notes`
        # was in TOP_KEYS and in every definition run.py has ever written, but absent from a
        # schema with additionalProperties false — so the committed contract rejected every file
        # the pipeline produced, and nobody noticed because nothing validated against it.
        assert set(schema["properties"]) == set(TOP_KEYS), sorted(set(schema["properties"]) ^ set(TOP_KEYS))
        src_schema = schema["$defs"]["source"]
        assert src_schema["additionalProperties"] is False, "the schema still lets any key onto a source"
        assert set(src_schema["properties"]) == set(SOURCE_KEYS), \
            (sorted(set(src_schema["properties"]) ^ set(SOURCE_KEYS)), "schema and SOURCE_KEYS disagree")
        assert tuple(src_schema["properties"]["kind"]["enum"]) == SOURCE_KINDS, src_schema["properties"]["kind"]
        assert tuple(src_schema["properties"]["status"]["enum"]) == SOURCE_STATUSES
        assert tuple(src_schema["properties"]["tier"]["enum"]) == TIERS
        assert tuple(src_schema["properties"]["proposed_by"]["enum"]) == SOURCE_PROPOSERS
        # misc.json's contract lives in the same file; the lane's own vocabulary is checked
        # against the module so the two cannot drift.
        from . import misc as misc_mod
        mf = schema["$defs"]["misc_finding"]
        assert tuple(mf["properties"]["kind"]["enum"]) == tuple(misc_mod.KINDS), mf["properties"]["kind"]
        assert tuple(mf["properties"]["status"]["enum"]) == misc_mod.STATUSES
        assert set(mf["required"]) == set(mf["properties"]) and mf["additionalProperties"] is False
        assert set(schema["$defs"]["misc"]["required"]) == {"generated", "query", "findings", "notes"}
        assert misc_mod.PROPOSED_BY == MISC_PROPOSED_BY and MISC_PROPOSED_BY in SOURCE_PROPOSERS
        # The subject filter's contract, in the same three places: the schema, this file's local
        # copy of the vocabulary, and the module that applies it.
        sf = schema["properties"]["subject_filter"]
        assert set(sf["required"]) == set(sf["properties"]) == set(SUBJECT_KEYS), sf["properties"]
        assert sf["additionalProperties"] is False
        assert tuple(sf["properties"]["source"]["enum"]) == FILTER_SOURCES, sf["properties"]["source"]
        assert sf["properties"]["regex"]["maxLength"] == MAX_REGEX and sf["properties"]["why"]["maxLength"] == MAX_WHY
        sub_mod = subject_module()
        assert sub_mod and sub_mod.SUBJECT_KEYS == SUBJECT_KEYS and sub_mod.FILTER_SOURCES == FILTER_SOURCES
        assert (sub_mod.MAX_REGEX, sub_mod.MAX_WHY) == (MAX_REGEX, MAX_WHY)
    # sources given as URL strings (the edit dialog's shape) are coerced to {url} and accepted
    strs = dict(demo, sources=["https://gazette.example.test/serie-generale", {"url": "https://ministry.example.test/x"}, "ftp://no"])
    probs = validate_definition(strs)
    assert probs == ["sources[2].url must be an http(s) URL"], probs
    assert strs["sources"][0] == {"url": "https://gazette.example.test/serie-generale"}, strs["sources"]
    # A rich source object — what the create dialog sends for a venue the partner picked out of
    # the live discovery step — is accepted whole, and every field the gate later fills in is
    # accepted too, because `run` re-validates the committed definition.
    rich = dict(demo, sources=[{"url": "https://gazette.example.test/serie-generale",
                                "name": "Gazzetta Ufficiale — Serie Generale",
                                "jurisdiction": "IT", "kind": "gazette",
                                "rationale": "Official gazette; transposition decrees appear here."}])
    assert validate_definition(rich) == [], validate_definition(rich)
    gated_shape = dict(demo, sources=[dict(rich["sources"][0], host="gazette.example.test", status="approved",
                                           tier="discovered", proposed_by="partner", confidence="high",
                                           gate={"reachable": True})])
    assert validate_definition(gated_shape) == [], validate_definition(gated_shape)
    # An unknown key is named, with the whole allowed set, rather than silently ignored and
    # then dropped on the floor by the gate.
    junk = dict(demo, sources=[{"url": "https://ok.test/x", "kind": "blog", "confidence": "certain",
                                "gate": "yes", "why": "because", "rationale": 5}])
    jp = validate_definition(junk)
    assert any("unknown field(s) ['why']" in p and "allowed: url, name, jurisdiction, kind, rationale, host" in p
               for p in jp), jp
    assert any("sources[0].kind must be one of" in p for p in jp), jp
    assert any("sources[0].confidence must be one of" in p for p in jp), jp
    assert any("sources[0].gate must be an object" in p for p in jp), jp
    assert any("sources[0].rationale must be a string" in p for p in jp), jp
    assert any("sources[0].rationale must be at most 1000" in p for p in validate_definition(
        dict(demo, sources=[{"url": "https://ok.test/x", "rationale": "x" * 1001}])))
    # reserved ids are refused by the validator; no_discover must be a boolean
    for rid in sorted(RESERVED_IDS):
        assert any("reserved" in p for p in validate_definition(dict(demo, id=rid))), rid
    assert any("no_discover must be a boolean" in p for p in validate_definition(dict(demo, no_discover="yes")))
    assert validate_definition(dict(demo, no_discover=True)) == []
    # subject_filter: shape is validated, the regex itself is NOT. A definition whose regex will
    # not compile must still RUN — the run reports it and reads everything — because a scan that
    # refuses to start has lost its coverage, while one that reads a few extra rows has not.
    ai_filter = {"regex": r"artificial\s+intelligence|\bAI\b", "why": "Keeps items that name AI.",
                 "source": "proposed"}
    assert validate_definition(dict(demo, subject_filter=ai_filter)) == []
    assert validate_definition(dict(demo, subject_filter={"regex": "(unclosed", "why": "", "source": "partner"})) == [], \
        "a regex that will not compile must not stop the scan from running"
    sfp = validate_definition(dict(demo, subject_filter={"regex": 5, "why": "x" * (MAX_WHY + 1),
                                                        "source": "guessed", "field": "title"}))
    for needle in ("subject_filter: unknown field(s) ['field']", "subject_filter.regex must be a string",
                   f"subject_filter.why must be a string of at most {MAX_WHY}", "subject_filter.source must be one of"):
        assert any(needle in p for p in sfp), (needle, sfp)
    assert any("subject_filter must be an object" in p for p in validate_definition(dict(demo, subject_filter="ai")))
    # the create dialog's shorthand — a bare regex — is filled in place, like a string source
    short = dict(demo, subject_filter={"regex": r"\bAI\b"})
    assert validate_definition(short) == [] and short["subject_filter"] == {
        "regex": r"\bAI\b", "why": "", "source": "partner"}, short["subject_filter"]
    empty = dict(demo, subject_filter={})
    assert validate_definition(empty) == [] and empty["subject_filter"]["source"] == "none", empty["subject_filter"]
    # an over-ceiling budget is not a validation problem: it is clamped and said, in health's words
    big = dict(demo, budget={"max_new_per_run": 1000000, "max_sources": 100, "delay_seconds": 0, "max_candidates": 3})
    assert validate_definition(big) == []
    live_b = common.Budget(big["budget"], dry_run=False)
    assert live_b["max_new_per_run"] == 60 and live_b["max_sources"] == 12 and live_b["delay_seconds"] == 1.0 \
        and live_b["max_candidates"] == 3, live_b.v
    assert len(live_b.dropped) == 3 and all("clamped" in n for n in live_b.dropped), live_b.dropped
    assert any("max_new_per_run=1000000 is above the ceiling 60" in n for n in live_b.dropped), live_b.dropped
    assert any("delay_seconds=0 is below the floor 1s" in n for n in live_b.dropped), live_b.dropped
    dry_b = common.Budget(big["budget"], dry_run=True)
    assert dry_b["delay_seconds"] == 0 and not any("delay" in n for n in dry_b.dropped), dry_b.dropped
    assert common.Budget({"max_sources": True}).v == common.Budget.DEFAULTS, "a bool is not a number"

    # 2. Adapter tolerance: the extractor closure handles both return shapes.
    global listing_rows
    saved = listing_rows
    try:
        listing_rows = lambda body, url, client, defn, budget: [{"title": "a", "url": "/a", "date": "2026-01-01"}]
        assert bind_extractor(None, {}, common.Budget())(b"", "https://x.test/", {}) == [{"title": "a", "url": "/a", "date": "2026-01-01"}]
        listing_rows = lambda body, url, client, defn, budget: ([{"title": "b"}, "junk"], {"seen": 2, "dropped": {"x": 1}})
        rows, rep = rows_and_report(listing_rows(b"", "", None, {}, None))
        assert rows == [{"title": "b"}] and rep["seen"] == 2
    finally:
        listing_rows = saved

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TMT_SCAN_ROOT"] = tmp
        try:
            # 3. Full dry-run create (no discovery) against the fixtures.
            create_summary = Path(tmp) / "create-summary.json"
            rc = main(["create", "--from-json", str(FIXTURES / "demo-definition.json"), "--no-discover",
                       "--dry-run", "--summary-out", str(create_summary)])
            assert rc == 0, rc
            summary_of_create = json.loads(create_summary.read_text(encoding="utf-8"))
            paths = paths_for("eu-pay-transparency-directive-scan")
            for p in (paths.definition, paths.developments, paths.health, paths.digest):
                assert p.exists() and not p.with_suffix(".json.tmp").exists(), p
            defn = common.load_json(paths.definition)
            assert defn["id"] == "eu-pay-transparency-directive-scan" and defn["created"] and defn["updated"]
            assert defn["demo"] is True and defn["no_discover"] is True, (defn.get("demo"), defn.get("no_discover"))
            assert [s["status"] for s in defn["sources"]] == ["approved", "approved"], defn["sources"]
            assert all(s["tier"] == "discovered" and s["proposed_by"] == "partner" and s["gate"]["extract"]["dated"] >= 2
                       for s in defn["sources"])
            ledger = common.load_json(paths.developments)
            devs = ledger["items"]
            assert len(devs) == 7, len(devs)
            assert all(d["enriched"] and d["doc_hash"] and d["read_as"] == "text" for d in devs), devs
            from . import enrich as enrich_mod
            assert all(d["type"] in enrich_mod.TYPES for d in devs), sorted({d["type"] for d in devs})
            for d in devs:
                tf = paths.dir / d["text_file"]
                assert tf.exists(), tf
                hay = common.norm_ws(tf.read_text(encoding="utf-8"))
                assert d["summary"], d["id"]
                for s in d["summary"]:
                    assert common.norm_ws(s["cite"]["quote"]) in hay, (d["id"], s["cite"]["quote"])
                assert d["verified_ratio"] == 1.0, (d["id"], d.get("note"))
                assert set(d["relevance"]["clients"]) == {"Accenture", "Annalise.ai"}
            undated = [d for d in devs if d["date"] is None]
            assert len(undated) == 1 and undated[0]["url"].endswith("/avvisi/rettifica-26G00130")
            assert len({d["id"] for d in devs}) == 7
            health = common.load_json(paths.health)
            assert set(health["sources"]) == {s["url"] for s in defn["sources"]}
            assert all(h["status"] == "OK" for h in health["sources"].values()), health["sources"]
            g = health["sources"]["https://gazette.example.test/serie-generale"]
            assert g["rows_seen"] == 5 and g["new"] == 5 and g["newest_visible"] == "2026-08-28", g
            assert health["run"]["enriched"] == 7 and health["run"]["queued"] == 0
            # The Miscellaneous lane ran as part of the create: misc.json is written beside the
            # ledger, the finding published on a covered host is dropped (that is coverage, not
            # miscellany) and the counts reach health and the summary.
            assert paths.misc.exists() and not paths.misc.with_suffix(".json.tmp").exists(), paths.misc
            mdoc = common.load_json(paths.misc)
            assert [f["kind"] for f in mdoc["findings"]] == ["official_venue", "official_venue", "secondary"], mdoc["findings"]
            assert not any(f["host"] == "gazette.example.test" for f in mdoc["findings"]), mdoc["findings"]
            assert any("gazette.example.test is on this scan's coverage list" in n for n in mdoc["notes"]), mdoc["notes"]
            assert mdoc["query"]["excluded_hosts"] == ["gazette.example.test", "ministry.example.test"], mdoc["query"]
            assert all(f["status"] == "new" and f["first_seen"] == common.today_ist() for f in mdoc["findings"])
            # Every finding this run's search returned carries the run's own stamp and is not
            # stale; the page needs no rule of its own to tell a fresh lead from a kept one.
            assert all(f["last_seen"] == mdoc["generated"] and f["stale"] is False for f in mdoc["findings"]), \
                mdoc["findings"]
            assert health["misc"] == {"found": 3, "new": 3, "promoted": 0, "dismissed": 0, "kept": 0,
                                      "stale": 0, "dropped": 1, "total": 3, "skipped": False,
                                      "error": ""}, health["misc"]
            # A COUNT IS NOT A PROBLEM. health.notes is what the page renders in its problems
            # list, so a healthy lane must put nothing there: the numbers are health.misc and the
            # SUMMARY, and the leads are their own tab. Defect this closes: every successful run
            # reported "miscellany: N finding(s) outside coverage this run …" as a problem.
            assert not any(n.startswith("miscellany") for n in health["notes"]), health["notes"]
            assert not any("outside coverage this run" in n for n in health["notes"]), health["notes"]
            assert summary_of_create["misc"] == {"found": 3, "new": 3, "promoted": 0}, summary_of_create["misc"]
            dg = common.load_json(paths.digest)
            known = {d["id"] for d in devs}
            assert dg["week"] == common.iso_week() and dg["headline"] and len(dg["body"]) >= 2
            assert all(c in known for s in dg["body"] for c in s["cites"]) and all(s["cites"] for s in dg["body"])
            assert dg["counts"]["new"] == 7 and dg["counts"]["sources_ok"] == 2 and dg["counts"]["sources_failed"] == 0
            assert dg["counts"]["high"] == 2 and dg["counts"]["assessed"] == 7 and dg["counts"]["queued"] == 0, dg["counts"]
            # upcoming: computed from obligations[].when in code — only ISO dates, ascending
            up = dg["upcoming"]
            assert [u["when"] for u in up] == ["2027-03-01", "2027-06-30"], up
            assert all(u["dev"] in known and u["who"] and u["what"] for u in up), up
            # a definition-level budget clamp shows up in health (dry run: delay 0 is allowed, so none here)
            assert not any("clamped" in n for n in health["notes"]), health["notes"]

            # 4. A second run is QUIET, ledgers nothing new, re-enriches nothing; the KPI is this
            #    run's (0 assessed, 0 high), not "everything first seen today".
            stamps = {d["id"]: d["enriched_at"] for d in devs}
            rc = main(["run", "--id", defn["id"], "--dry-run"])
            assert rc == 0
            ledger2 = common.load_json(paths.developments)
            assert len(ledger2["items"]) == 7
            assert {d["id"]: d["enriched_at"] for d in ledger2["items"]} == stamps, "re-enriched an unchanged doc"
            assert all(d["last_seen"] == common.today_ist() for d in ledger2["items"])
            h2 = common.load_json(paths.health)
            assert all(h["status"] == "QUIET" and h["new"] == 0 for h in h2["sources"].values()), h2["sources"]
            assert h2["run"]["enriched"] == 0
            c2 = common.load_json(paths.digest)["counts"]
            assert c2["new"] == 0 and c2["high"] == 0 and c2["assessed"] == 0, c2
            # the demo refuses a live run rather than fail every fixture host
            assert main(["run", "--id", defn["id"]]) == EXIT_USAGE

            # 5. The enrichment cap queues the rest, says so, and the next run continues.
            capped = dict(demo, name="Capped demo", budget=dict(demo["budget"], max_new_per_run=2))
            capped_path = Path(tmp) / "capped.json"
            capped_path.write_text(json.dumps(capped), encoding="utf-8")
            rc = main(["create", "--from-json", str(capped_path), "--no-discover", "--dry-run"])
            assert rc == 0
            cp = paths_for("capped-demo")
            ch = common.load_json(cp.health)
            assert ch["run"]["enriched"] == 2 and ch["run"]["queued"] == 5, ch["run"]
            assert any("queued" in n and "max_new_per_run=2" in n for n in ch["notes"]), ch["notes"]
            cl = common.load_json(cp.developments)["items"]
            done = sorted(d["date"] or "" for d in cl if d["enriched"])
            assert done == ["2026-08-25", "2026-08-28"], done          # newest first
            assert main(["run", "--id", "capped-demo", "--dry-run", "--max-new", "1"]) == 0
            ch = common.load_json(cp.health)
            assert ch["run"]["enriched"] == 1 and ch["run"]["queued"] == 4, ch["run"]
            # a --max-new below the budget names itself, so the note never blames a cap the
            # partner set: this run held back, the scan's standing budget did not change
            assert any("this run was limited to 1 (--max-new)" in n for n in ch["notes"]), ch["notes"]

            # 5b. FIRST_RUN_MAX_NEW. A create reads only the newest FIRST_RUN_MAX_NEW documents
            #     even when the budget allows 60, queues the rest through the cap's existing
            #     report path, and says how to continue; a later plain `run` uses the full cap.
            #     25 rows from one listing, so the first-run cap is what bites, not the budget.
            fc0 = enable_dry_run()
            saved_rows, saved_client_for = listing_rows, _client_for
            try:
                globals()["listing_rows"] = lambda body, url, client, defn_, budget: ([
                    {"title": f"Decreto {n}", "url": f"/eli/{n}", "date": f"2026-08-{n:02d}"}
                    for n in range(1, 26)], {"seen": 25, "kept": 25, "dropped": {}})
                # main(--dry-run) would call enable_dry_run() again and put the fixture
                # extractor back over the one above; the client is already the fake one.
                globals()["_client_for"] = lambda dry_run: fc0
                first = dict(demo, name="First run demo", sources=[demo["sources"][0]],
                             budget=dict(demo["budget"], max_new_per_run=60))
                first_path = Path(tmp) / "first.json"
                first_path.write_text(json.dumps(first), encoding="utf-8")
                assert main(["create", "--from-json", str(first_path), "--no-discover", "--dry-run"]) == 0
                fpp = paths_for("first-run-demo")
                fh = common.load_json(fpp.health)
                assert fh["budget"]["caps"]["max_new_per_run"] == 60, fh["budget"]["caps"]
                assert fh["run"]["queued"] == 25 - FIRST_RUN_MAX_NEW, fh["run"]
                note = f"first run reads the newest {FIRST_RUN_MAX_NEW}"
                assert any(note in n and "press Run scan again" in n for n in fh["budget"]["dropped"]), fh["budget"]
                assert any(note in n for n in fh["notes"]), fh["notes"]      # and in the page's problems list
                assert common.load_json(fpp.digest)["counts"]["queued"] == 25 - FIRST_RUN_MAX_NEW
                fl = common.load_json(fpp.developments)["items"]
                assert len(fl) == 25, len(fl)
                tried = sorted(d["date"] for d in fl if d.get("read_attempts"))
                untouched = sorted(d["date"] for d in fl if not d.get("read_attempts"))
                assert len(tried) == FIRST_RUN_MAX_NEW and untouched == ["2026-08-0%d" % n for n in range(1, 6)], untouched
                # the backlog is picked up by a plain run, which is not capped to the first-run number
                assert main(["run", "--id", "first-run-demo", "--dry-run"]) == 0
                fh2 = common.load_json(fpp.health)
                assert fh2["run"]["queued"] == 0 and not any("queued" in n for n in fh2["budget"]["dropped"]), fh2
                assert all(d.get("read_attempts") for d in common.load_json(fpp.developments)["items"])
            finally:
                globals()["listing_rows"], globals()["_client_for"] = saved_rows, saved_client_for
                restore_live()

            # 5c. A partner's source object arrives whole: name, jurisdiction, kind and
            #     rationale survive the gate onto the committed source, so the coverage panel
            #     shows the partner's own reason for picking the venue. `tier` and `status` in
            #     the partner's object are ignored — the gate decides both, and tier is always
            #     "discovered" (a scan reads every venue through the generic extractor).
            picked = dict(demo, name="Picked demo", sources=[
                {"url": "https://gazette.example.test/serie-generale",
                 "name": "Gazzetta Ufficiale — Serie Generale",
                 "jurisdiction": "IT", "kind": "gazette",
                 "rationale": "Official gazette; transposition decrees are published here."},
                {"url": "https://ministry.example.test/labour/pay-transparency",
                 "name": "Bundesministerium für Arbeit — Entgelttransparenz",
                 "jurisdiction": "DE", "kind": "ministry",
                 "rationale": "Publishes the transposition bill and its drafts.",
                 "tier": "vetted", "status": "approved"},
            ])
            picked_path = Path(tmp) / "picked.json"
            picked_path.write_text(json.dumps(picked), encoding="utf-8")
            assert main(["create", "--from-json", str(picked_path), "--no-discover", "--dry-run"]) == 0
            pd = common.load_json(paths_for("picked-demo").definition)["sources"]
            assert [s["kind"] for s in pd] == ["gazette", "ministry"], pd
            assert [s["name"] for s in pd] == [picked["sources"][0]["name"], picked["sources"][1]["name"]], pd
            assert [s["rationale"] for s in pd] == [s["rationale"] for s in picked["sources"]], pd
            assert [s["jurisdiction"] for s in pd] == ["IT", "DE"], pd
            assert all(s["tier"] == "discovered" and s["proposed_by"] == "partner" for s in pd), pd
            assert all(s["status"] == "approved" and s["gate"]["extract"]["dated"] >= 2 for s in pd), pd

            # 5d. no_discover: six partner-chosen venues cost six gate calls and no discovery
            #     call at all. This is what the live create dialog buys — the workflow gates the
            #     partner's ~6 picks instead of up to max_candidates=25 model proposals.
            fc1 = enable_dry_run()
            saved_gate, saved_disc = gate_assess, discover_propose
            calls = {"gate": 0, "discover": 0}
            try:
                def counting_gate(cand, budget, extractor):
                    calls["gate"] += 1
                    return saved_gate(cand, budget, extractor)

                def counting_discover(defn_, client, budget):
                    calls["discover"] += 1
                    return saved_disc(defn_, client, budget)
                globals()["gate_assess"], globals()["discover_propose"] = counting_gate, counting_discover
                six = dict(demo, name="Six sources demo", id="six-sources-demo",
                           budget=dict(demo["budget"], max_sources=6),
                           sources=[{"url": f"https://gazette.example.test/serie-generale?part={n}",
                                     "name": f"Gazzetta Ufficiale — part {n}", "jurisdiction": "IT",
                                     "kind": "gazette", "rationale": "Picked by the partner from discovery."}
                                    for n in range(6)])
                assert validate_definition(six) == [], validate_definition(six)
                summ, code6 = create_scan(six, paths_for(six["id"]), fc1, no_discover=True)
                assert calls == {"gate": 6, "discover": 0}, calls
                assert summ["candidates"] == 6 and summ["gated"]["approved"] == 6 and code6 == 0, summ
            finally:
                globals()["gate_assess"], globals()["discover_propose"] = saved_gate, saved_disc
                restore_live()

            # 5e. THE SUBJECT FILTER, end to end, on the rows that produced the defect. Twelve
            #     titles lifted from data/scans/india-ai-regulation/developments.json: six CERT-In
            #     vendor bulletins, three TRAI telecom rows (two of them substring traps — TRAI
            #     and IRDAI both contain "AI") and the three rows out of that scan's 118 that are
            #     actually about artificial intelligence.
            fc3 = enable_dry_run()
            saved_rows, saved_client_for = listing_rows, _client_for
            try:
                REAL = [
                    "Multiple Vulnerabilities in Oracle Products",
                    "A End of Mainstream for Windows Server 2022",
                    "Multiple Vulnerabilities in Apple Products",
                    "Multiple Vulnerabilities in SAP Products",
                    "Multiple Vulnerabilities in Microsoft Products",
                    "Multiple Vulnerabilities in Adobe Products",
                    "Consultation Paper on Cloud Services",
                    "Consultation Paper on Net Neutrality",
                    "Direction regarding mandatory adoption of 1600-series numbers by IRDAI regulated entities.",
                    "Consultation Paper on Leveraging Artificial Intelligence and Big Data in Telecommunication Sector",
                    "Recommendations on Leveraging Artificial Intelligence and Big Data in Telecommunication Sector",
                    "Direction regarding institutionalization of AI/ML-based UCC_Detect intelligence "
                    "for inter-operator sharing and regulatory action against UCC senders.",
                ]

                def _stub(titles):
                    globals()["listing_rows"] = lambda body, url, client, defn_, budget: (
                        [{"title": t, "url": f"/doc/{n}", "date": "2026-08-%02d" % (n + 1)}
                         for n, t in enumerate(titles)],
                        {"seen": len(titles), "kept": len(titles), "dropped": {}})
                globals()["_client_for"] = lambda dry_run: fc3
                _stub(REAL)
                src0 = demo["sources"][0]
                base = dict(demo, sources=[src0], budget=dict(demo["budget"], max_new_per_run=0))

                def _filtered_create(name: str, filt, out: Path) -> tuple:
                    d = dict(base, name=name)
                    if filt is not None:
                        d["subject_filter"] = filt
                    p = Path(tmp) / f"{common.slug(name)}.json"
                    p.write_text(json.dumps(d), encoding="utf-8")
                    assert main(["create", "--from-json", str(p), "--no-discover", "--dry-run",
                                 "--summary-out", str(out)]) == 0, name
                    pp = paths_for(common.slug(name))
                    return (common.load_json(pp.health), common.load_json(pp.developments)["items"],
                            json.loads(out.read_text(encoding="utf-8")), pp)

                # BEFORE — no subject filter at all: every one of the twelve is ledgered, exactly
                # as it was before this key existed, and health SAYS the scan is unfiltered so
                # nobody mistakes it for a well-targeted one.
                so = Path(tmp) / "subject-summary.json"
                nh, nl, nsum, _ = _filtered_create("Unfiltered subject demo", None, so)
                assert len(nl) == 12, len(nl)
                assert nh["run"]["filtered"] == 0 and nh["run"]["unjudged"] == 0, nh["run"]
                assert nsum["filtered"] == 0 and nsum["unjudged"] == 0, nsum
                assert any("no subject filter" in n for n in nh["notes"]), nh["notes"]
                assert not any("outside this scan's subject filter" in i
                               for h in nh["sources"].values() for i in h["info"]), nh["sources"]

                # AFTER — the same twelve rows through a filter a partner can read. The three AI
                # items survive; the six CERT-In bulletins and the TRAI/IRDAI substring traps do
                # not, and none of them costs a fetch, a ledger row or a reading-budget slot.
                ai_f = {"regex": r"artificial\s+intelligence|\bA\.?I\.?\b|\bAI/ML\b|machine\s+learning|"
                                 r"generative|deepfake|algorithmic",
                        "why": "Keeps items whose title names artificial intelligence or its usual variants.",
                        "source": "proposed"}
                fh_, fl, fsum, fpp = _filtered_create("Filtered subject demo", ai_f, so)
                kept_titles = sorted(d["title"] for d in fl)
                assert len(fl) == 3, [d["title"] for d in fl]
                assert all("Artificial Intelligence" in t or "AI/ML" in t for t in kept_titles), kept_titles
                assert not any("Vulnerabilities" in t or "IRDAI" in t or "Neutrality" in t for t in kept_titles), kept_titles
                assert fh_["run"]["filtered"] == 9 and fh_["run"]["unjudged"] == 0, fh_["run"]
                assert fsum["filtered"] == 9 and fsum["unjudged"] == 0, fsum
                assert not any(d.get("subject_unjudged") for d in fl), fl
                sh_ = fh_["sources"][src0["url"]]
                assert sh_["status"] == "OK" and sh_["rows_seen"] == 3, sh_
                assert "9 row(s) outside this scan's subject filter" in sh_["info"], sh_["info"]
                # the structured per-source block the page sums (absent is not zero, so an
                # unfiltered run must not carry the key at all)
                assert sh_["subject"] == {"dropped": 9, "kept_terse": 0, "titles": []}, sh_["subject"]
                assert "subject" not in nh["sources"][src0["url"]], nh["sources"][src0["url"]]
                assert not any("no subject filter" in n for n in fh_["notes"]), fh_["notes"]
                # The definition keeps the partner's own words, and a re-run re-validates it.
                assert common.load_json(fpp.definition)["subject_filter"] == ai_f
                assert main(["run", "--id", fpp.id, "--dry-run"]) == 0

                # A title too terse to judge is KEPT, marked on its own development, and counted —
                # the one direction a filter with no document probe is allowed to err in.
                _stub(REAL + ["Corrigendum"])
                th_, tl, tsum, _ = _filtered_create("Terse subject demo", ai_f, so)
                assert len(tl) == 4 and th_["run"]["filtered"] == 9 and th_["run"]["unjudged"] == 1, th_["run"]
                terse = [d for d in tl if d.get("subject_unjudged")]
                assert [d["title"] for d in terse] == ["Corrigendum"], tl
                assert tsum["unjudged"] == 1, tsum
                sh_ = th_["sources"][src0["url"]]
                assert any(i.startswith("1 row(s) kept: title too terse to judge against the filter")
                           and "Corrigendum" in i for i in sh_["info"]), sh_["info"]
                assert sh_["subject"] == {"dropped": 9, "kept_terse": 1, "titles": ["Corrigendum"]}, sh_["subject"]
                # Kept-because-unjudgeable is not a problem: the row is in the ledger, not lost.
                assert not any("too terse" in n for n in th_["notes"]), th_["notes"]

                # A MALFORMED regex is reported and IGNORED — every row kept — because a broken
                # filter that silently drops the subject is the worst outcome available.
                _stub(REAL)
                bh_, bl, bsum, _ = _filtered_create(
                    "Broken filter demo", {"regex": r"artificial intelligence|(unclosed",
                                           "why": "", "source": "partner"}, so)
                assert len(bl) == 12 and bh_["run"]["filtered"] == 0, (len(bl), bh_["run"])
                assert any("does not compile" in n and "every row kept" in n for n in bh_["notes"]), bh_["notes"]
                assert bsum["filtered"] == 0, bsum

                # source "none" is a partner's decision to read everything: no filter, no note.
                oh_, ol, _, _ = _filtered_create("Filter off demo",
                                                 dict(ai_f, source="none"), so)
                assert len(ol) == 12 and oh_["run"]["filtered"] == 0, (len(ol), oh_["run"])
                assert not any("subject filter" in n for n in oh_["notes"]), oh_["notes"]

                # A listing on which NOTHING is the subject is QUIET, not EMPTY: the venue is
                # fine, its business is simply someone else's. "page changed" would be a lie, and
                # it is a note — the page's problems list — which this must never reach.
                _stub(REAL[:6])
                ah_, al, _, _ = _filtered_create("All filtered demo", ai_f, so)
                assert al == [] and ah_["run"]["filtered"] == 6, (al, ah_["run"])
                sh_ = ah_["sources"][src0["url"]]
                assert sh_["status"] == "QUIET" and sh_["notes"] == [], sh_
                assert "all 6 row(s) on this listing are outside this scan's subject filter" in sh_["info"], sh_["info"]

                # propose-filter proposes and stores NOTHING: no definition is written, no scan
                # runs, and a refused proposal is a clean "none" the partner can act on.
                fc3.canned["subject_filter"] = {"regex": ai_f["regex"], "why": ai_f["why"]}
                assert main(["propose-filter", "--intent", demo["intent"], "--topic", "artificial intelligence",
                             "--dry-run", "--summary-out", str(so)]) == 0
                prop = json.loads(so.read_text(encoding="utf-8"))
                assert prop["subject_filter"]["source"] == "proposed" and prop["note"] == "", prop
                assert prop["subject_filter"]["regex"] == ai_f["regex"], prop
                fc3.canned["subject_filter"] = {"regex": ".*", "why": "everything"}
                assert main(["propose-filter", "--intent", demo["intent"], "--dry-run",
                             "--summary-out", str(so)]) == 0
                prop = json.loads(so.read_text(encoding="utf-8"))
                assert prop["subject_filter"] == {"regex": "", "why": "", "source": "none"}, prop
                assert "refused" in prop["note"] and "empty string" in prop["note"], prop
                fc3.canned.pop("subject_filter", None)
                assert main(["propose-filter", "--intent", "too short", "--dry-run"]) == EXIT_USAGE
            finally:
                globals()["listing_rows"], globals()["_client_for"] = saved_rows, saved_client_for
                restore_live()

            # 6. A source whose host has no fixture FAILS: everything is still written, exit 1.
            broken = dict(demo, name="Broken demo",
                          sources=demo["sources"] + [{"url": "https://nowhere.example.test/list", "jurisdiction": "FR"}])
            bp = Path(tmp) / "broken.json"
            bp.write_text(json.dumps(broken), encoding="utf-8")
            rc = main(["create", "--from-json", str(bp), "--no-discover", "--dry-run"])
            assert rc == 0, "the gate rejects an unfetchable candidate, so create itself succeeds"
            bpaths = paths_for("broken-demo")
            bd = common.load_json(bpaths.definition)
            rej = [s for s in bd["sources"] if s["status"] == "rejected"]
            assert len(rej) == 1 and "no listing fixture" in rej[0]["reason"], bd["sources"]
            # force the rejected source to approved (as a hand edit would) and run: FAILED, exit 1
            for s in bd["sources"]:
                s["status"] = "approved"
            common.atomic_write_json(bpaths.definition, bd)
            rc = main(["run", "--id", "broken-demo", "--dry-run"])
            assert rc == EXIT_FAILED_SOURCE, rc
            bh = common.load_json(bpaths.health)
            fh = bh["sources"]["https://nowhere.example.test/list"]
            assert fh["status"] == "FAILED" and any("fetch failed" in n for n in fh["notes"]), fh
            assert common.load_json(bpaths.digest)["counts"]["sources_failed"] == 1
            assert bpaths.developments.exists()

            # 7. Over-budget approved sources are GATED, not fetched, and reported.
            bd["budget"]["max_sources"] = 1
            common.atomic_write_json(bpaths.definition, bd)
            rc = main(["run", "--id", "broken-demo", "--dry-run"])
            bh = common.load_json(bpaths.health)
            assert sum(1 for h in bh["sources"].values() if h["status"] == "GATED") == 2
            assert any("max_sources=1" in n for n in bh["budget"]["dropped"])

            # 8. list and delete. Delete unlinks only a definition whose `id` is the one asked
            #    for, and never a reserved id.
            assert main(["list"]) == 0
            stray = Path(tmp) / "scans" / "stray.json"
            stray.write_text(json.dumps({"not": "a scan"}), encoding="utf-8")
            assert main(["delete", "--id", "stray"]) == EXIT_USAGE and stray.exists(), "a non-definition was unlinked"
            stray.unlink()
            for rid in sorted(RESERVED_IDS):
                assert main(["delete", "--id", rid]) == EXIT_USAGE, rid
                assert main(["run", "--id", rid, "--dry-run"]) == EXIT_USAGE, rid
            assert main(["create", "--json", json.dumps(dict(demo, name="Schema")), "--no-discover", "--dry-run"]) == EXIT_USAGE
            assert main(["create", "--json", json.dumps(dict(demo, id="tmt-india")), "--no-discover", "--dry-run"]) == EXIT_USAGE
            assert not (Path(tmp) / "scans" / "schema.json").exists() and not (Path(tmp) / "scans" / "tmt-india.json").exists()
            out_file = Path(tmp) / "summary.json"
            rc = main(["delete", "--id", "broken-demo", "--summary-out", str(out_file)])
            assert rc == 0 and not bpaths.definition.exists() and not bpaths.dir.exists()
            written = json.loads(out_file.read_text(encoding="utf-8"))
            assert written["id"] == "broken-demo" and written["action"] == "delete" and len(written["removed"]) == 2, written
            assert main(["delete", "--id", "broken-demo"]) == EXIT_USAGE

            # Sections 10-12 replace an injectable step and drive create_scan/run_scan directly:
            # main(--dry-run) calls enable_dry_run(), which would put the fixture step back.
            fc = enable_dry_run()

            def _create(name: str, sources: list) -> ScanPaths:
                d = dict(demo, name=name, id=common.slug(name), sources=sources)
                assert validate_definition(d) == [], validate_definition(d)
                pp = paths_for(d["id"])
                create_scan(d, pp, fc, no_discover=True)
                return pp

            def _run(pp: ScanPaths) -> int:
                return run_scan(common.load_json(pp.definition), pp, fc)[1]

            # 10. Rows that share a title but carry no date are distinct developments; a dated
            #     pair sharing title+date is one. Read failures for the fake links are per-dev,
            #     not source failures.
            saved_rows = listing_rows
            try:
                globals()["listing_rows"] = lambda body, url, client, defn_, budget: ([
                    {"title": "Corrigendum", "url": "/c/1", "date": None},
                    {"title": "Corrigendum", "url": "/c/2", "date": None},
                    {"title": "Corrigendum", "url": "/c/3", "date": "12/08/2026"},      # invalid date: undated, still distinct
                    {"title": "Corrigendum", "url": "/c/4", "date": "2026-08-01"},
                    {"title": "Corrigendum", "url": "/c/5", "date": "2026-08-01"},       # same title+date: merged into /c/4
                ], {"seen": 5, "kept": 5, "dropped": {}})
                up_ = _create("Undated demo", [demo["sources"][0]])
                ul = common.load_json(up_.developments)["items"]
                assert sorted(d["url"].rsplit("/", 1)[1] for d in ul) == ["1", "2", "3", "4"], [d["url"] for d in ul]
                assert sum(1 for d in ul if d["date"] is None) == 3
                uh = common.load_json(up_.health)["sources"][demo["sources"][0]["url"]]
                assert uh["status"] == "OK" and uh["new"] == 4 and any("did not validate" in i for i in uh["info"]), uh
                assert all(d["read_error"].startswith("ValueError: dry-run: no fixture document") for d in ul), ul

                # 11. A listing extraction that reports `error` is a FAILED source (exit 1), not EMPTY.
                globals()["listing_rows"] = lambda body, url, client, defn_, budget: ([], {"error": "listing extraction failed: model returned no text"})
                assert _run(up_) == EXIT_FAILED_SOURCE
                uh = common.load_json(up_.health)["sources"][demo["sources"][0]["url"]]
                assert uh["status"] == "FAILED" and any("extraction failed: listing extraction failed" in n for n in uh["notes"]), uh
                assert common.load_json(up_.digest)["counts"]["sources_failed"] == 1
            finally:
                globals()["listing_rows"] = saved_rows

            # 12. An enricher that returns an error record is a failed attempt: not enriched,
            #     retried on the next run, given up after MAX_READ_ATTEMPTS with a note; the
            #     document's read reason (robots.txt) is kept and is final on the first try.
            saved_enrich, saved_doc = enrich_dev, document_text
            try:
                globals()["enrich_dev"] = lambda defn_, dev, text, client, budget: {"error": "model declined: policy"}
                ep = _create("Enrich fail demo", [demo["sources"][1]])
                el = common.load_json(ep.developments)["items"]
                assert el and all(not d["enriched"] and d["read_attempts"] == 1 and d["enrich_error"].startswith("model declined")
                                  and "relevance" not in d for d in el), el
                eh = common.load_json(ep.health)
                assert eh["run"]["enrich_failed"] == len(el) and eh["run"]["enriched"] == 0, eh["run"]
                ec = common.load_json(ep.digest)["counts"]
                assert ec["high"] == 0 and ec["assessed"] == 0 and ec["new"] == len(el), ec
                assert any("enrichment failed (attempt 1)" in i for i in eh["sources"][demo["sources"][1]["url"]]["info"])
                assert _run(ep) == 0
                assert all(d["read_attempts"] == 2 for d in common.load_json(ep.developments)["items"])
                assert _run(ep) == 0 and _run(ep) == 0
                eh = common.load_json(ep.health)
                assert eh["run"]["enrich_failed"] == 0 and any("no longer retried" in n for n in eh["notes"]), eh
                # now the enricher works again: the given-up developments stay given up (no retry)
                globals()["enrich_dev"] = saved_enrich
                assert _run(ep) == 0
                assert common.load_json(ep.health)["run"]["enriched"] == 0

                def robots_doc(url, budget, client, report=None):
                    if report is not None:
                        report.update({"reason": "robots.txt disallows: ministry.example.test/robots.txt disallows '/labour'", "chars": 0})
                    return "", "unreadable"
                globals()["document_text"] = robots_doc
                rpp = _create("Robots demo", [demo["sources"][1]])
                rl = common.load_json(rpp.developments)["items"]
                assert rl and all(d["read_error"].startswith("robots.txt disallows") and d["read_attempts"] == MAX_READ_ATTEMPTS
                                  and not d["enriched"] for d in rl), rl
                rh = common.load_json(rpp.health)
                assert rh["run"]["read_failed"] == len(rl) and any("robots.txt disallows" in i for i in rh["sources"][demo["sources"][1]["url"]]["info"])
            finally:
                globals()["enrich_dev"], globals()["document_text"] = saved_enrich, saved_doc

            # 13. A budget above the ceiling is clamped and the clamp is in health, in both places
            #     the page reads (budget.dropped and notes); the definition keeps what was typed.
            over = dict(demo, name="Over budget demo", budget={"max_new_per_run": 5000, "max_sources": 99, "delay_seconds": 0})
            (Path(tmp) / "over.json").write_text(json.dumps(over), encoding="utf-8")
            assert main(["create", "--from-json", str(Path(tmp) / "over.json"), "--no-discover", "--dry-run"]) == 0
            oh = common.load_json(paths_for("over-budget-demo").health)
            assert oh["budget"]["caps"]["max_new_per_run"] == 60 and oh["budget"]["caps"]["max_sources"] == 12, oh["budget"]
            clamps = [n for n in oh["budget"]["dropped"] if "clamped" in n]
            assert len(clamps) == 2 and all(n in oh["notes"] for n in clamps), (oh["budget"]["dropped"], oh["notes"])
            assert oh["notes"].count(clamps[0]) == 1, "clamp notes must not be duplicated across create and run"
            assert common.load_json(paths_for("over-budget-demo").definition)["budget"]["max_new_per_run"] == 5000

            # 9. A partner source with no gate module available stays pending, never approved.
            restore_live()
            saved_gate, saved_disc = gate_assess, discover_propose
            try:
                def no_gate(cand, budget, extractor):
                    raise ImportError("No module named 'pipeline.scan.gate'")
                globals()["gate_assess"] = no_gate
                globals()["discover_propose"] = lambda defn, client, budget: [{"url": "https://gazette.example.test/x", "name": "dup-host"}]
                fc = common.FakeClient()
                np = paths_for("nogate")
                summary, code = create_scan(dict(demo, id="nogate", sources=[demo["sources"][0]]), np, fc, no_discover=False)
                nd = common.load_json(np.definition)
                assert [s["status"] for s in nd["sources"]] == ["pending", "pending"], nd["sources"]
                assert all("gate unavailable" in s["reason"] for s in nd["sources"])
                assert nd["sources"][1]["proposed_by"] == "discovery"
                assert code == 0 and summary["sources"] == 0
                assert common.load_json(np.digest)["headline"] == "Nothing new this week"
            finally:
                globals()["gate_assess"], globals()["discover_propose"] = saved_gate, saved_disc

            # 14. The Miscellaneous lane end to end. A lead outside coverage is promoted into the
            #     definition, gated on the next run exactly like any other candidate, and once it
            #     IS coverage the lane stops surfacing it — while keeping it, with its status.
            import contextlib
            import io
            fc2 = enable_dry_run()
            mp = paths_for("misc-demo")
            create_scan(json.loads(json.dumps(dict(demo, name="Misc demo", id="misc-demo"))),
                        mp, fc2, no_discover=True)
            md = common.load_json(mp.misc)
            by_url = {f["url"]: f for f in md["findings"]}
            boe = by_url["https://boe.example.test/diario/2026/09/01/rd-812-2026"]
            parl = by_url["https://parlement.example.test/dossiers/transparence-salariale"]
            press = by_url["https://press.example.test/story/spain-pay-gap-deadline"]
            assert all(f["status"] == "new" and len(f["id"]) == 10 for f in md["findings"]), md["findings"]

            def _promote(fid: str) -> tuple:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = main(["promote", "--id", "misc-demo", "--finding", fid])
                return rc, err.getvalue()

            # Only an official venue can be promoted, and the refusal names why rather than
            # printing "invalid": this is the boundary the whole lane exists behind.
            rc, err = _promote(press["id"])
            assert rc == EXIT_USAGE and "not 'official_venue'" in err and "not where an instrument is published" in err, err
            assert _promote("0123456789")[0] == EXIT_USAGE, "an unknown finding id was accepted"
            assert _promote("NOTHEXNOT")[0] == EXIT_USAGE, "a malformed finding id was accepted"
            assert len(common.load_json(mp.definition)["sources"]) == 2, "a refused promote changed the definition"
            assert common.load_json(mp.misc)["findings"] == md["findings"], "a refused promote changed misc.json"

            # The two official venues go in as pending, carrying the finding's own words.
            assert _promote(boe["id"])[0] == EXIT_OK and _promote(parl["id"])[0] == EXIT_OK
            pdefn = common.load_json(mp.definition)
            promoted = [s for s in pdefn["sources"] if s.get("proposed_by") == MISC_PROPOSED_BY]
            assert len(promoted) == 2 and all(s["status"] == "pending" and "gate" not in s for s in promoted), promoted
            assert promoted[0]["url"] == boe["url"] and promoted[0]["rationale"] == boe["why"], promoted[0]
            assert promoted[0]["name"] == boe["title"] and promoted[0]["jurisdiction"] == "ES"
            assert all(s["tier"] == "discovered" and s["kind"] == "other" for s in promoted), promoted
            assert validate_definition(pdefn) == [], validate_definition(pdefn)
            statuses = {f["id"]: f["status"] for f in common.load_json(mp.misc)["findings"]}
            assert statuses[boe["id"]] == "promoted" and statuses[press["id"]] == "new", statuses
            # Promoting the same finding twice adds nothing: one venue, one row on the coverage panel.
            assert _promote(boe["id"])[0] == EXIT_OK
            assert len([s for s in common.load_json(mp.definition)["sources"]
                        if s.get("proposed_by") == MISC_PROPOSED_BY]) == 2

            # 14b. dismiss: the other half of triage, and the status nothing could set before —
            #      so a lead a partner had judged irrelevant came back every run for ever.
            def _dismiss(fid: str, scan_id: str = "misc-demo") -> tuple:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = main(["dismiss", "--id", scan_id, "--finding", fid])
                return rc, err.getvalue()

            misc_before, defn_before = mp.misc.read_bytes(), mp.definition.read_bytes()
            # The same refusals as promote, from the same code: a malformed or unknown finding
            # id, a reserved scan id, a scan with no lane to triage.
            assert _dismiss("0123456789")[0] == EXIT_USAGE, "an unknown finding id was accepted"
            assert _dismiss("NOTHEXNOT")[0] == EXIT_USAGE, "a malformed finding id was accepted"
            assert _dismiss(press["id"], "no-such-scan")[0] == EXIT_USAGE, "an unknown scan was accepted"
            for rid in sorted(RESERVED_IDS):
                assert _dismiss(press["id"], rid)[0] == EXIT_USAGE, rid
            # A promoted finding is coverage now: refusing is the honest answer, because
            # dismissing the lead would not remove the source.
            rc, err = _dismiss(boe["id"])
            assert rc == EXIT_USAGE and "was promoted into this scan's coverage" in err, err
            assert mp.misc.read_bytes() == misc_before, "a refused dismiss changed misc.json"
            # A secondary finding CAN be dismissed — judging a lead irrelevant is a reading of
            # the subject, not of the publisher — and the definition is not touched by it.
            assert _dismiss(press["id"])[0] == EXIT_OK
            st = {f["id"]: f["status"] for f in common.load_json(mp.misc)["findings"]}
            assert st[press["id"]] == "dismissed" and st[boe["id"]] == "promoted", st
            assert mp.definition.read_bytes() == defn_before, "dismiss changed the coverage list"
            # Idempotent: a second click, or a retried dispatch, is not a failure.
            settled = mp.misc.read_bytes()
            assert _dismiss(press["id"])[0] == EXIT_OK and mp.misc.read_bytes() == settled

            saved_fetch = fetch_listing
            try:
                def stub_fetch(url, delay, allowed_hosts):
                    """A listing for the promoted Spanish venue; nothing for the French one — so
                    one promotion is approved and read in the same run and the other is rejected,
                    which is the gate deciding, exactly as promote promised it would."""
                    if _host(url) == "boe.example.test":
                        return (FIXTURES / "listing.gazette.example.test.html").read_bytes(), {"http": 200, "fixture": "stub"}
                    return dry_fetch_listing(url, delay, allowed_hosts)
                globals()["fetch_listing"] = stub_fetch
                assert run_scan(common.load_json(mp.definition), mp, fc2)[1] == EXIT_OK
                gdefn = common.load_json(mp.definition)
                states = {s["url"]: s["status"] for s in gdefn["sources"] if s.get("proposed_by") == MISC_PROPOSED_BY}
                assert states[boe["url"]] == "approved" and states[parl["url"]] == "rejected", states
                gh = common.load_json(mp.health)
                # A promotion that worked is not a problem: the approval is on the venue's own
                # health row, and only the rejection — which a partner must act on — is a note.
                assert not any("promoted source gated approved" in n for n in gh["notes"]), gh["notes"]
                assert any("promoted source gated approved" in i for i in gh["sources"][boe["url"]]["info"]), \
                    gh["sources"][boe["url"]]
                assert any("promoted source gated rejected" in n and "no listing fixture" in n for n in gh["notes"]), gh["notes"]
                # Approved means read in this same run, through the ordinary source loop.
                assert gh["sources"][boe["url"]]["status"] == "OK" and gh["sources"][boe["url"]]["new"] > 0, gh["sources"]
                # And now that the venue is coverage, the lane drops it — while keeping the
                # finding, with the status the partner gave it.
                md2 = common.load_json(mp.misc)
                boe2 = next(f for f in md2["findings"] if f["id"] == boe["id"])
                assert boe2["status"] == "promoted" and boe2["first_seen"] == boe["first_seen"], boe2
                assert any("boe.example.test is on this scan's coverage list" in n for n in md2["notes"]), md2["notes"]
                assert any("no longer surface in search" in n and boe["id"] in n for n in md2["notes"]), md2["notes"]
                # A kept lead is visible AS a kept lead: its last_seen is the earlier run's stamp,
                # older than this file's own, so `stale` is true and the page can mark the row.
                # `stale` comes from the set of ids the search returned, not from comparing the
                # two stamps: these two runs land within a second of each other and now_ist()
                # reads to the second, so a stamp comparison would have called this lead fresh.
                assert boe2["stale"] is True and boe2["last_seen"] <= md2["generated"], boe2
                assert all(f["stale"] is False and f["last_seen"] == md2["generated"]
                           for f in md2["findings"] if f["id"] != boe["id"]), md2["findings"]
                # A dismissed lead stays dismissed and is not re-listed as new by the next run.
                press2 = next(f for f in md2["findings"] if f["id"] == press["id"])
                assert press2["status"] == "dismissed" and press2["stale"] is False, press2
                # Nothing new: the lane had seen both remaining leads before, and "new" means the
                # lane had never seen the URL — not "first seen today".
                assert gh["misc"] == {"found": 2, "new": 0, "promoted": 2, "dismissed": 1, "kept": 1,
                                      "stale": 1, "dropped": 2, "total": 3, "skipped": False,
                                      "error": ""}, gh["misc"]
                # And still no routine note: a healthy lane says its numbers in health.misc.
                assert not any(n.startswith("miscellany") for n in gh["notes"]), gh["notes"]

                # Skippable, and skipping says so and leaves the findings alone.
                before = mp.misc.read_bytes()
                assert run_scan(common.load_json(mp.definition), mp, fc2, no_misc=True)[1] == EXIT_OK
                sh = common.load_json(mp.health)
                assert mp.misc.read_bytes() == before, "--no-misc rewrote misc.json"
                assert sh["misc"]["skipped"] is True and any("skipped on this run (--no-misc)" in n for n in sh["notes"]), sh["misc"]

                # A lane failure is a note, never an exit code: nothing here is coverage, so its
                # absence cannot make the scan wrong.
                saved_misc = misc_scan

                def boom_misc(defn_, client_, previous):
                    raise RuntimeError("web search unavailable")
                try:
                    globals()["misc_scan"] = boom_misc
                    assert run_scan(common.load_json(mp.definition), mp, fc2)[1] == EXIT_OK
                finally:
                    globals()["misc_scan"] = saved_misc
                bh = common.load_json(mp.health)
                assert bh["misc"]["skipped"] is True and bh["misc"]["found"] == 0 \
                    and "web search unavailable" in bh["misc"]["error"], bh["misc"]
                assert any("lane failed and was skipped this run" in n for n in bh["notes"]), bh["notes"]
                assert mp.misc.read_bytes() == before, "a failed lane rewrote misc.json"
            finally:
                globals()["fetch_listing"] = saved_fetch

            # A scan created with the lane off never writes misc.json at all, and says so.
            np2 = paths_for("no-misc-demo")
            create_scan(json.loads(json.dumps(dict(demo, name="No misc demo", id="no-misc-demo"))),
                        np2, fc2, no_discover=True, no_misc=True)
            assert not np2.misc.exists(), "no_misc still wrote misc.json"
            assert common.load_json(np2.definition)["no_misc"] is True
            assert any("switched off for this scan" in n for n in common.load_json(np2.health)["notes"])
            assert main(["promote", "--id", "no-misc-demo", "--finding", boe["id"]]) == EXIT_USAGE, \
                "promote must refuse when there are no findings to promote from"
            assert _dismiss(boe["id"], "no-misc-demo")[0] == EXIT_USAGE, \
                "dismiss must refuse when there are no findings to triage"

            # 14d. no_misc survives an Edit. A Save re-creates the scan from a definition the page
            #      read back, and that definition may simply not carry the key — no_misc is not a
            #      dispatch input, so there is no second channel for it. Absent must therefore
            #      mean unchanged, exactly like `created`. Defect this closes: a Save silently
            #      switched the open-web lane back on for a scan whose partner had turned it off.
            edited = json.loads(json.dumps(dict(demo, name="No misc demo", id="no-misc-demo")))
            assert "no_misc" not in edited
            create_scan(edited, np2, fc2, no_discover=True)          # and no --no-misc flag either
            assert common.load_json(np2.definition)["no_misc"] is True, "an Edit re-enabled the lane"
            assert not np2.misc.exists(), "the lane ran on a scan whose partner had switched it off"
            nh = common.load_json(np2.health)["notes"]
            # The lane being off is why the tab is empty, so it is said. That a Save replaced the
            # definition is not a problem — it is what Save does — so it is only in the log.
            assert any("switched off for this scan" in n for n in nh), nh
            assert not any("replaced an existing one" in n for n in nh), nh
            # Turning it back on is still one Save away: a definition that says False means False.
            back_on = json.loads(json.dumps(dict(demo, name="No misc demo", id="no-misc-demo", no_misc=False)))
            create_scan(back_on, np2, fc2, no_discover=True)
            assert common.load_json(np2.definition)["no_misc"] is False, "the lane could not be turned back on"
            assert np2.misc.exists() and common.load_json(np2.misc)["findings"], "the lane did not run"
            restore_live()
        finally:
            os.environ.pop("TMT_SCAN_ROOT", None)
            restore_live()
    after = _scans_dir_state()
    assert after == scans_before, \
        f"the selftest wrote into the repo's scans/: {sorted(set(after) ^ set(scans_before)) or 'contents changed'}"
    assert not (common.SCANS_DIR / "schema.json").exists() or common.load_json(common.SCANS_DIR / "schema.json")["title"].startswith("Scan definition"), \
        "scans/schema.json must still be the contract"
    how = _check_workflow()
    print(f"PASS run: validator (string sources, rich {{url,name,jurisdiction,kind,rationale}} sources, unknown source "
          f"key named, reserved ids, budget ceilings), dry-run create+run (7 devs, "
          f"all quotes verified, digest cites real ids, upcoming computed), idempotent second run, enrichment cap + queue, "
          f"first run reads {FIRST_RUN_MAX_NEW} of 25 and the backlog is picked up by the next run, partner kind/name/"
          f"rationale survive the gate (tier still discovered), no_discover gates 6 partner sources with 0 discovery calls, "
          f"subject filter on the real india-ai-regulation rows: 12 -> 3 ledgered (9 outside the filter, "
          f"reported per source and in health.run/SUMMARY), a terse title kept and marked, a malformed "
          f"regex noted and ignored (12 kept), source='none' and no filter keep everything (and an "
          f"unfiltered scan says so), an all-filtered listing is QUIET not EMPTY, propose-filter stores "
          f"nothing and refuses a broad regex, "
          f"FAILED source exit 1 (fetch and extractor error), GATED over budget, delete guarded, undated pair kept, "
          f"enrich error retried then given up, robots read final, clamp noted, --summary-out; "
          f"miscellany lane written beside the ledger (covered host dropped) with NO routine note in health "
          f"(a healthy lane reports counts, not problems), promote refuses a non-official finding and moves an "
          f"official one into sources, the next run gates it (one approved — noted on the venue's own health "
          f"row, not in problems — and one rejected), a promoted venue stops being miscellany but keeps its "
          f"status and is marked stale, dismiss refuses a bad id/reserved scan/promoted finding and is "
          f"idempotent, a dismissed lead survives the next run and is not re-listed as new, --no-misc and "
          f"no_misc skip the lane, no_misc survives an Edit that omits the key, a lane failure is a note not "
          f"an exit code; scan.yml checked via {how}")


if __name__ == "__main__":
    sys.exit(main())
