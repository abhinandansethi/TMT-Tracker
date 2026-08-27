#!/usr/bin/env python3
"""TMT Regulatory Radar — extractor engine v2 CLI. Zero LLM anywhere.

    tracker.py selftest                       parse every live source's fixture; floors must hold
    tracker.py sweep [--source ID] [--stratum S]
    tracker.py backfill --since YYYY-MM-DD [--source ID] [--stratum S]
    tracker.py import-baseline                seed the ledger from ../data/items.json (one-off)
    tracker.py export                         write ../data/items.json from the ledger (curated fields preserved)
    tracker.py health                         print the last health snapshot
    tracker.py fetch-pdfs                     archive instrument PDFs into ../instruments/

Exit codes: 0 all OK · 1 source FAILED or floor breached (cron alert fires on non-zero).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from radar.core import Sweeper  # noqa: E402
from radar.ledger import IST, Ledger, now_ist  # noqa: E402
from radar import display  # noqa: E402

REGISTRY_PATH = HERE / "registry_v2.json"
DB = HERE / "ledger.db"
JSONL = HERE / "ledger.jsonl"
HEALTH = HERE / "health.json"
FIXTURES = HERE / "fixtures"


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text())


def open_ledger() -> Ledger:
    return Ledger(DB, JSONL)


# ---------------------------------------------------------------------- selftest
def selftest() -> int:
    """Parse every live source's saved fixture. Floors must hold and ≥70% of parsed rows
    must pass the gates. This is the regression harness — run it before trusting a deploy
    and after any site adds new markup (re-capture the fixture first)."""
    from radar import parse as ps
    from radar import validate as vl

    reg = load_registry()
    today = date.today()
    failures = []
    print(f"{'source':<28} {'rows':>5} {'gated':>5}  status")
    drivers = []
    for s in [x for x in reg["sources"] if x.get("status") == "live"]:
        # Driver sources are session-and-postback flows, not a single fetch, so a saved page
        # cannot stand in for them. They are exercised against the live site instead; say so
        # rather than let the harness imply a coverage it does not have.
        if s["parser"].get("driver"):
            drivers.append(s["id"])
            print(f"{s['id']:<28} {'-':>5} {'-':>5}  DRIVER (live-tested, no fixture)")
            continue
        # fixture matching the strategy wins: API sources test their .json capture even
        # when an .html shell of the same venue sits alongside as evidence
        json_first = s["parser"]["strategy"] in ("meity_api", "tec_er_api", "inspace_api", "wp_json", "cci_datatables")
        exts = (".json", ".html", ".xml") if json_first else (".html", ".json", ".xml")
        fx = None
        for ext in exts:
            cand = FIXTURES / f"{s['id']}{ext}"
            if cand.exists():
                fx = cand
                break
        if fx is None:
            fx = FIXTURES / f"{s['id']}.html"
        if not fx.exists():
            print(f"{s['id']:<28} {'-':>5} {'-':>5}  NO FIXTURE")
            failures.append((s["id"], "no fixture captured"))
            continue
        try:
            rows = ps.parse(s, fx.read_bytes(), s["url"])
        except Exception as e:  # noqa: BLE001
            print(f"{s['id']:<28} {'-':>5} {'-':>5}  PARSE ERROR {str(e)[:60]}")
            failures.append((s["id"], f"parse error: {e}"))
            continue
        gated = sum(1 for r in rows if vl.gate(r, s, today)[0] is not None)
        floor = s["parser"].get("row_floor", 1)
        ok = len(rows) >= floor and (gated >= max(1, int(0.7 * len(rows))) or s["parser"].get("undated_ok"))
        # row_filter sources legitimately gate down to a subset; floor applies pre-filter
        status = "OK" if ok else "FLOOR/GATE BREACH"
        if not ok:
            failures.append((s["id"], f"rows={len(rows)} floor={floor} gated={gated}"))
        print(f"{s['id']:<28} {len(rows):>5} {gated:>5}  {status}")
    if failures:
        print(f"\nSELFTEST FAILED: {len(failures)} source(s)")
        for sid, why in failures:
            print(f"  {sid}: {why}")
        return 1
    print("\nselftest passed: every live source parses its fixture above floor")
    if drivers:
        print(f"note: {len(drivers)} driver source(s) not fixture-covered "
              f"({', '.join(drivers)}) — verify with: tracker.py sweep --source <id>")
    return 0


# ------------------------------------------------------------------------ audit
def audit() -> int:
    """No classification drift: every engine-classified row's routine flag must equal
    what the current regex produces. Curated v1 baseline rows are exempt (their flags
    were hand-verified against the v1 regex); engine rows must agree exactly."""
    import re as _re
    reg = load_registry()
    rx = _re.compile(reg["classification"]["routine_regex"])
    led = open_ledger()
    bad = []
    for it in led.all_items():
        if it["status"] in ("baseline", "duplicate"):
            continue
        expect = bool(rx.search(it["title"]))
        if bool(it["routine"]) != expect:
            bad.append((it["id"], it["routine"], expect, it["title"][:80]))
    if bad:
        print(f"AUDIT FAILED: {len(bad)} row(s) disagree with the routine regex")
        for iid, got, want, t in bad:
            print(f"  {iid} stored={got} regex={want}  {t}")
        return 1
    print("audit passed: every engine row agrees with the classification regex")
    return 0


# ------------------------------------------------------------------- compliance
def compliance() -> int:
    """Snapshot every host's robots.txt as dated evidence, and report the verdict for the
    exact paths we fetch. Exits non-zero if any host now refuses a path we are live on."""
    from radar.compliance import snapshot
    res = snapshot(load_registry(), HERE / "audit")
    rep = res["report"]
    refused = []
    print(f"robots.txt snapshot  {rep['taken']}\n")
    print(f"{'host':<30} {'HTTP':<6} {'bytes':>7}  paths we fetch")
    print("-" * 78)
    for origin, e in rep["hosts"].items():
        host = origin.split("//")[1]
        bad = [k for k, v in e["paths"].items() if v != "allowed"]
        refused += bad
        verdict = "all allowed" if not bad else f"REFUSED: {', '.join(bad)}"
        print(f"{host:<30} {str(e.get('status') or e.get('error','?'))[:6]:<6} "
              f"{e.get('bytes', 0):>7}  {verdict}")
    print(f"\nsaved: {res['json']}")
    print(f"raw files: {res['raw']}")
    if refused:
        print(f"\n{len(refused)} source path(s) refused by their site's own robots.txt")
    return 1 if refused else 0


# ---------------------------------------------------------------- import-baseline
def import_baseline() -> int:
    """One-off: seed the ledger with the verified v1 baseline so the first sweep does not
    re-announce history. Existing ids are skipped (idempotent)."""
    import re as _re
    led = open_ledger()
    data = json.loads((ROOT / "data" / "items.json").read_text())
    reg = load_registry()
    strata = {s["id"]: s.get("stratum") for s in reg["sources"]}
    seq_sources = {s["id"] for s in reg["sources"]
                   if s.get("tripwire", {}).get("kind") == "sequence"}
    n = 0
    for it in data["items"]:
        if led.known(it["id"]):
            continue
        seq = None
        if it["source_id"] in seq_sources:
            m = _re.search(r"No\.?_?\s*(\d+)\s*of\s*(\d{4})", it.get("pdf_url") or "", _re.I)
            if m:
                seq = f"{m.group(2)}:{int(m.group(1))}"
        led.insert({
            "id": it["id"], "date": it.get("date"), "title": it["title"],
            "url": it.get("pdf_url") or "", "page_url": it.get("page_url"),
            "source_id": it["source_id"], "regulator": it.get("regulator"),
            "stratum": strata.get(it["source_id"], "telecom"),
            "type": it.get("type"), "routine": bool(it.get("routine")),
            "deadline": it.get("deadline"), "flags": it.get("flags", []),
            "seq": seq, "first_seen": now_ist(), "status": "baseline",
        })
        n += 1
    print(f"baseline import: {n} items added, ledger now {led.count()}")
    return 0


# ------------------------------------------------------------------------ export
def export() -> int:
    """Write ../data/items.json in the dashboard contract. Items already curated there
    (gist, folds, hand-set types) are preserved verbatim; engine items are appended."""
    led = open_ledger()
    path = ROOT / "data" / "items.json"
    existing = json.loads(path.read_text()) if path.exists() else {"items": []}
    curated = {i["id"]: i for i in existing.get("items", [])}
    out_items = []
    for it in led.all_items():
        if it["status"] == "duplicate":
            continue
        url = str(it["url"] or "")
        is_pdf = url.lower().split("?")[0].endswith((".pdf", ".doc", ".docx"))
        meta = it.get("meta") or {}
        rec = {
            "date": it["date"], "source_id": it["source_id"], "regulator": it["regulator"],
            "type": it["type"], "routine": it["routine"], "title": it["title"],
            "lane": it.get("lane", "instruments"),
            # dual links, every item: the document itself and the official landing page it
            # was published on. A gazette entry has no direct PDF (postback), so its citation
            # is the permanent Gazette ID carried in meta.
            "doc_url": url or None,
            "page_url": it.get("page_url") or None,
            "pdf_url": url if is_pdf else None,   # kept for backward compatibility
            "deadline": it["deadline"], "id": it["id"], "status": it["status"],
            "meta": meta,
            # preserve a hand-written gist from the v1 baseline (curated prose), but never let
            # a stale exported entry freeze fresh engine metadata like the impacted rule
            "gist": (curated.get(it["id"], {}) or {}).get("gist", "") or "",
            # crisp display fields, computed once here and consumed identically by the
            # dashboard and the connector API (radar.display is the single source of truth)
            "short": display.heading(it),
            "line": display.descriptor(it, date.today().isoformat()),
        }
        if it.get("flags"):
            rec["flags"] = it["flags"]
        if it.get("stratum"):
            rec["stratum"] = it["stratum"]
        out_items.append(rec)
    dates = sorted(d for d in (i.get("date") for i in out_items) if d)

    def lane_of(i: dict) -> str:
        return i.get("lane", "instruments")
    inst = [i for i in out_items if lane_of(i) == "instruments"]
    payload = {
        "schema_version": "3.2",
        "generated": now_ist(),
        "window": {"from": dates[0] if dates else None, "to": dates[-1] if dates else None},
        "stats": {
            "total": len(out_items),
            "instruments": len(inst),
            "judgments": sum(1 for i in out_items if lane_of(i) == "judgments"),
            "signals": sum(1 for i in out_items if lane_of(i) == "signals"),
            "substantive": sum(1 for i in inst if not i.get("routine")),
            "routine": sum(1 for i in out_items if i.get("routine")),
            "quarantined": led.db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0],
            "open_deadlines": sum(1 for i in inst
                                  if i.get("deadline") and str(i["deadline"]) >= date.today().isoformat()),
        },
        # newest first; undated rows (document shelves) fall to the end
        "items": sorted(out_items, key=lambda i: (i.get("date") or "", i["id"]), reverse=True),
    }
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"exported {len(out_items)} items -> {path}")
    return 0


# --------------------------------------------------------------------- fetch-pdfs
def fetch_pdfs() -> int:
    from radar.fetch import FetchError, get
    led = open_ledger()
    dest_dir = ROOT / "instruments"
    dest_dir.mkdir(exist_ok=True)
    n, failed = 0, 0
    for it in led.all_items():
        url = str(it["url"] or "")
        if not url.lower().split("?")[0].endswith(".pdf"):
            continue
        name = f"{it['date'] or 'undated'}_{it['regulator']}_{it['id']}.pdf".replace("/", "-").replace(" ", "_")
        dest = dest_dir / name
        if dest.exists():
            continue
        try:
            r = get(url.replace(" ", "%20"))
            dest.write_bytes(r.content)
            n += 1
        except FetchError as e:
            failed += 1
            print(f"FAILED {url[:90]} {e}")
    print(f"{n} instruments archived, {failed} failed, dir={dest_dir}")
    return 1 if failed else 0


# ------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["sweep", "backfill", "selftest", "import-baseline",
                                    "export", "health", "fetch-pdfs", "audit", "compliance"])
    ap.add_argument("--source")
    ap.add_argument("--stratum")
    ap.add_argument("--since")
    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "audit":
        return audit()
    if a.cmd == "compliance":
        return compliance()
    if a.cmd == "import-baseline":
        return import_baseline()
    if a.cmd == "export":
        return export()
    if a.cmd == "fetch-pdfs":
        return fetch_pdfs()
    if a.cmd == "health":
        print(HEALTH.read_text() if HEALTH.exists() else "no health snapshot yet")
        return 0

    if a.cmd == "backfill" and not a.since:
        ap.error("backfill requires --since YYYY-MM-DD")
    sw = Sweeper(load_registry(), open_ledger())
    code = sw.run(only=a.source, stratum=a.stratum,
                  backfill_since=a.since if a.cmd == "backfill" else None)
    sw.write_health(HEALTH)
    return code


if __name__ == "__main__":
    sys.exit(main())
