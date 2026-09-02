"""The scan orchestrator and CLI: create, run, delete, list.

Design: docs/horizon-design.md. This module owns the ledger semantics; the sibling modules
(discover, gate, extract, enrich, digest) each do one step and never touch a file.

    python -m pipeline.scan.run create --from-json scans/new.json [--no-discover] [--dry-run]
    python -m pipeline.scan.run run --id eu-pay-transparency [--dry-run] [--max-new N]
    python -m pipeline.scan.run delete --id eu-pay-transparency
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
  `max_new_per_run`, text beyond `max_doc_chars` — is written into health. A FAILED source makes
  the process exit 1 after everything is written, never before.
* `--dry-run` swaps the four network-touching functions for fixture readers and the model for
  FakeClient, so the whole path can be exercised without a key or a socket. The swap is at
  module level (`fetch_listing`, `listing_rows`, `document_text`, `enrich_dev`, `discover_propose`,
  `gate_assess`) precisely so a test can do the same.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from . import common

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
# Ids that match ID_RE but name something that is not a scan. Review finding: a scan named
# "Schema" slugged to `schema`, and ScanPaths then overwrote scans/schema.json (the contract)
# with the definition — and `delete --id schema` would have unlinked it. `tmt-india` is the
# built-in vetted tracker's slot on the Scans home. Checked everywhere an id enters: the
# validator, create (after slugging), run and delete; api/scans.js holds the same set.
RESERVED_IDS = frozenset({"schema", "tmt-india"})
SOURCE_STATUSES = ("approved", "pending", "rejected")
TIERS = ("vetted", "discovered")
TOP_KEYS = ("id", "name", "intent", "jurisdictions", "topics", "industries", "clients", "sources", "discovery_notes",
            "budget", "demo", "no_discover", "created", "updated")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAX_READ_ATTEMPTS = 3          # a document that will not read is given up on, loudly, not retried forever
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
    """A source may be given as a URL string or as {url, ...}: the edit dialog sends strings,
    a hand-typed definition may use either. Review finding: the validator demanded objects,
    so every dialog-created scan was queued (HTTP 202) and then died two minutes later with
    `sources[0] must be an object` in a log the partner never sees. Strings become {"url": s}
    in place, before validation, so both shapes are one shape from here on."""
    if isinstance(defn, dict) and isinstance(defn.get("sources"), list):
        defn["sources"] = [{"url": s.strip()} if isinstance(s, str) else s for s in defn["sources"]]


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
                    extra = set(c) - {"name", "scope"}
                    if extra:
                        problems.append(f"clients[{i}]: unknown field(s) {sorted(extra)}")
                    if not isinstance(c.get("name"), str) or not (1 <= len(c["name"].strip()) <= 120):
                        problems.append(f"clients[{i}].name must be 1..120 characters")
                    if "scope" in c and not (isinstance(c["scope"], str) and len(c["scope"]) <= 500):
                        problems.append(f"clients[{i}].scope must be a string of at most 500 characters")
                else:
                    problems.append(f"clients[{i}] must be a string or {{name, scope}}")
    if "sources" in defn:
        srcs = defn["sources"]
        if not isinstance(srcs, list):
            problems.append("sources must be an array")
        else:
            for i, s in enumerate(srcs):
                if not isinstance(s, dict):
                    problems.append(f"sources[{i}] must be an object")
                    continue
                if not _is_url(s.get("url")):
                    problems.append(f"sources[{i}].url must be an http(s) URL")
                if "status" in s and s["status"] not in SOURCE_STATUSES:
                    problems.append(f"sources[{i}].status must be one of {list(SOURCE_STATUSES)}")
                if "tier" in s and s["tier"] not in TIERS:
                    problems.append(f"sources[{i}].tier must be one of {list(TIERS)}")
                for k in ("name", "host", "jurisdiction", "rationale", "reason"):
                    if k in s and not isinstance(s[k], str):
                        problems.append(f"sources[{i}].{k} must be a string")
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
    for k in ("demo", "no_discover"):
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


fetch_listing = _default_fetch_listing
listing_rows = _default_listing_rows
document_text = _default_document_text
enrich_dev = _default_enrich
discover_propose = _default_discover
gate_assess = _default_gate


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
def run_scan(defn: dict, paths: ScanPaths, client, max_new: Optional[int] = None,
             notes: Optional[list[str]] = None) -> tuple[dict, int]:
    """Fetch every approved source, ledger what is new, enrich up to the budget, write the
    digest. Returns (summary, exit_code). Everything is written before the exit code is
    decided, so a FAILED source never costs the run's other results."""
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

    approved = [s for s in defn.get("sources") or [] if s.get("status") == "approved"]
    cap_sources = int(budget["max_sources"])
    if len(approved) > cap_sources:
        for s in approved[cap_sources:]:
            sources_health[s["url"]] = {"status": "GATED", "rows_seen": 0, "new": 0, "newest_visible": None,
                                        "notes": [f"not fetched: max_sources={cap_sources} reached"],
                                        "info": [], "checked": now}
        budget.note_drop(f"{len(approved) - cap_sources} approved source(s) not fetched — max_sources={cap_sources}")
        approved = approved[:cap_sources]

    for src in approved:
        url = src["url"]
        h = {"status": "FAILED", "rows_seen": 0, "new": 0, "newest_visible": None,
             "notes": [], "info": [], "checked": now,
             "name": src.get("name"), "tier": src.get("tier", "discovered")}
        sources_health[url] = h
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
        if not rows:
            h["status"] = "EMPTY"
            h["notes"].append("listing fetched but no rows extracted — page changed, or the extractor missed the list")
        elif h["new"]:
            h["status"] = "OK"
        else:
            h["status"] = "QUIET"
            h["info"].append("nothing new; newest item this venue shows is "
                             + (h["newest_visible"] or "undated"))
        common.log(f"{h['status']:6s} {url}  rows {h['rows_seen']}  new {h['new']}  newest {h['newest_visible'] or '—'}")

    # Enrichment: the never-enriched backlog, newest first, up to the cap. Failed reads are
    # retried on later runs until MAX_READ_ATTEMPTS, then left with their error on record.
    queue = [d for d in items if not d.get("enriched") and d.get("read_attempts", 0) < MAX_READ_ATTEMPTS]
    queue.sort(key=lambda d: (d.get("date") or "0000-00-00", d.get("first_seen") or ""), reverse=True)
    cap = int(budget["max_new_per_run"])
    if max_new is not None:
        cap = min(cap, max(0, int(max_new)))
    to_do, rest = queue[:cap], queue[cap:]
    if rest:
        budget.note_drop(f"{len(rest)} development(s) queued, not enriched — max_new_per_run={cap}; "
                         f"the next run continues from the newest")
    given_up = [d for d in items if not d.get("enriched") and d.get("read_attempts", 0) >= MAX_READ_ATTEMPTS]
    if given_up:
        scan_notes.append(f"{len(given_up)} development(s) could not be read after {MAX_READ_ATTEMPTS} attempts "
                          "and are no longer retried: " + ", ".join(d["id"] for d in given_up[:10]))
    enriched = read_failed = enrich_failed = 0
    enriched_now: list[dict] = []
    for dev in to_do:
        src_h = sources_health.get(dev.get("source_url") or "")
        sink = src_h["info"] if src_h else scan_notes
        # extract.document_text never raises for a bad document: it returns "" and puts the
        # reason (robots.txt, HTTP error, scan without a text layer, vision failure) in
        # `report`. Review finding: the report was never requested, so every unreadable
        # document was ledgered as "empty text" and re-fetched three times — even when
        # robots.txt had said no, which is the one answer that will not change.
        rep: dict = {}
        try:
            text, read_as = document_text(dev["url"], budget, client, report=rep)
        except Exception as e:
            dev["read_attempts"] = dev.get("read_attempts", 0) + 1
            dev["read_error"] = _err(e)
            read_failed += 1
            sink.append(f"{dev['id']}: document not read (attempt {dev['read_attempts']}): {dev['read_error']}")
            continue
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
            read_failed += 1
            sink.append(f"{dev['id']}: document not read (attempt {dev['read_attempts']}): {dev['read_error']}")
            continue
        h = _sha1(text)
        if dev.get("enriched") and dev.get("doc_hash") == h:
            continue
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
            enrich_failed += 1
            sink.append(f"{dev['id']}: enrichment failed (attempt {dev['read_attempts']}): {dev['enrich_error']}")
            continue
        for k in ENRICH_FIELDS:
            if k in e and e[k] not in (None, ""):
                dev[k] = e[k]
        dev["enriched"] = True
        dev["enriched_at"] = now
        dev.pop("read_error", None)
        dev.pop("enrich_error", None)
        enriched += 1
        enriched_now.append(dev)

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
        "run": {"new": len(new_devs), "enriched": enriched, "queued": len(rest),
                "read_failed": read_failed, "enrich_failed": enrich_failed,
                "ledgered_total": len(items), "week": week},
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
               "high": high, "ledgered_total": len(items), "week": week,
               "exit": EXIT_FAILED_SOURCE if any_failed else EXIT_OK}
    for n in health["notes"]:
        common.log(f"note: {n}")
    common.log(f"{defn.get('id')}: {len(new_devs)} new, {enriched} enriched, {len(rest)} queued, "
               f"{sources_ok} source(s) ok, {sources_failed} failed → {paths.dir}")
    return summary, summary["exit"]


# ----------------------------------------------------------------------------- create
def _candidate_from_partner(s: dict) -> dict:
    return {"url": s["url"], "name": s.get("name") or _host(s["url"]), "host": _host(s["url"]),
            "jurisdiction": s.get("jurisdiction", ""), "proposed_by": "partner",
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
                max_new: Optional[int] = None) -> tuple[dict, int]:
    now = common.now_ist()
    existing = common.load_json(paths.definition, None)
    notes: list[str] = []
    if existing:
        defn["created"] = existing.get("created") or now
        notes.append("definition replaced an existing one; the ledger under data/ was kept")
        common.log(f"replacing existing definition {paths.definition} (ledger kept)")
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
    coerce_sources(defn)
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
    summary, code = run_scan(defn, paths, client, max_new=max_new, notes=notes)
    summary["gated"] = {"approved": approved_n, "pending": sum(1 for s in results if s["status"] == "pending"),
                        "rejected": sum(1 for s in results if s["status"] == "rejected")}
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
    global fetch_listing, listing_rows, document_text, enrich_dev, discover_propose, gate_assess, _DRY
    fetch_listing = dry_fetch_listing
    listing_rows = dry_listing_rows
    document_text = dry_document_text
    enrich_dev = dry_enrich
    discover_propose = dry_discover
    gate_assess = dry_gate
    _DRY = True
    from . import digest as digest_mod
    return common.FakeClient(canned={digest_mod.NAME: _dry_digest})


def restore_live() -> None:
    global fetch_listing, listing_rows, document_text, enrich_dev, discover_propose, gate_assess, _DRY
    fetch_listing, listing_rows, document_text = _default_fetch_listing, _default_listing_rows, _default_document_text
    enrich_dev, discover_propose, gate_assess = _default_enrich, _default_discover, _default_gate
    _DRY = False


# ----------------------------------------------------------------------------- CLI
def _client_for(dry_run: bool):
    if dry_run:
        return enable_dry_run()
    return common.openai_client()


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
    summary, code = create_scan(defn, paths, client, no_discover=no_discover, max_new=getattr(args, "max_new", None))
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
    summary, code = run_scan(defn, paths, client, max_new=args.max_new)
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pipeline.scan.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--selftest", action="store_true", help="exercise the orchestrator offline")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("create", help="validate, write the definition, discover + gate, then run")
    c.add_argument("--json", help="the definition as a JSON string")
    c.add_argument("--from-json", help="path to a JSON definition")
    c.add_argument("--no-discover", action="store_true", help="gate only the partner's sources")
    c.add_argument("--dry-run", action="store_true", help="fixtures + FakeClient; no network, no key")
    c.add_argument("--max-new", type=int, default=None, help=argparse.SUPPRESS)
    r = sub.add_parser("run", help="fetch approved sources, enrich what is new, write the digest")
    r.add_argument("--id", required=True)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max-new", type=int, default=None, help="enrich at most N this run (below the budget)")
    d = sub.add_parser("delete", help="remove the definition and its data directory")
    d.add_argument("--id", required=True)
    sub.add_parser("list", help="every scan with its source and development counts")
    for p in (c, r, d):
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
    if args.cmd == "list":
        return cmd_list(args)
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
        assert set(inputs) == {"action", "scan_id", "scan", "no_discover"}, list(inputs)
        assert inputs["action"]["type"] == "choice" and inputs["action"]["required"] is True
        assert inputs["action"]["options"] == ["create", "run", "delete"]
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
        return "yaml"
    # Textual fallback: split the file at `run:` and make sure no input expression follows one.
    for chunk in re.split(r"\n\s+run:\s*\|", text)[1:]:
        body = chunk.split("\n      - ")[0]
        assert "${{ inputs." not in body and "${{ github.event.inputs" not in body
    for needle in ("workflow_dispatch:", "type: choice", "timeout-minutes: 60", "contents: write",
                   "tmt-scan-${{ inputs.scan_id }}", "cancel-in-progress: false", "::notice",
                   "--summary-out", "re.fullmatch(r\"[a-z0-9][a-z0-9-]{1,59}\""):
        assert needle in text, f"scan.yml lacks {needle}"
    assert "grep '^SUMMARY '" not in text
    return "text"


def selftest() -> None:
    import tempfile
    demo = json.loads((FIXTURES / "demo-definition.json").read_text(encoding="utf-8"))

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
    # sources given as URL strings (the edit dialog's shape) are coerced to {url} and accepted
    strs = dict(demo, sources=["https://gazette.example.test/serie-generale", {"url": "https://ministry.example.test/x"}, "ftp://no"])
    probs = validate_definition(strs)
    assert probs == ["sources[2].url must be an http(s) URL"], probs
    assert strs["sources"][0] == {"url": "https://gazette.example.test/serie-generale"}, strs["sources"]
    # reserved ids are refused by the validator; no_discover must be a boolean
    for rid in sorted(RESERVED_IDS):
        assert any("reserved" in p for p in validate_definition(dict(demo, id=rid))), rid
    assert any("no_discover must be a boolean" in p for p in validate_definition(dict(demo, no_discover="yes")))
    assert validate_definition(dict(demo, no_discover=True)) == []
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
            rc = main(["create", "--from-json", str(FIXTURES / "demo-definition.json"), "--no-discover", "--dry-run"])
            assert rc == 0, rc
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
        finally:
            os.environ.pop("TMT_SCAN_ROOT", None)
            restore_live()
    assert not (common.SCANS_DIR / "eu-pay-transparency-directive-scan.json").exists()
    assert not (common.SCANS_DIR / "schema.json").exists() or common.load_json(common.SCANS_DIR / "schema.json")["title"].startswith("Scan definition"), \
        "scans/schema.json must still be the contract"
    how = _check_workflow()
    print(f"PASS run: validator (string sources, reserved ids, budget ceilings), dry-run create+run (7 devs, "
          f"all quotes verified, digest cites real ids, upcoming computed), idempotent second run, enrichment cap + queue, "
          f"FAILED source exit 1 (fetch and extractor error), GATED over budget, delete guarded, undated pair kept, "
          f"enrich error retried then given up, robots read final, clamp noted, --summary-out; scan.yml checked via {how}")


if __name__ == "__main__":
    sys.exit(main())
