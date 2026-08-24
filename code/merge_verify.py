#!/usr/bin/env python3
"""Deterministic merge + validation + ground-truth verification for the TMT tracker baseline.
No LLM anywhere in this path."""
import json, hashlib, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime.now(IST).strftime('%Y-%m-%d')

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

ALLOWED_DOMAINS = ("trai.gov.in","eservices.dot.gov.in","dot.gov.in","nccs.gov.in","tec.gov.in",
                   "www.tec.gov.in","tdsat.gov.in","pib.gov.in","www.pib.gov.in","egazette.gov.in",
                   "medianama.com","www.medianama.com")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Baseline window: fixed start, end is always the real run date. Never hardcode "now".
WINDOW = ("2026-06-01", TODAY)

def norm_title(t):
    return re.sub(r"\s+", " ", t.strip().lower())

def make_id(item):
    return hashlib.sha1((norm_title(item["title"]) + item["date"]).encode()).hexdigest()[:10]

def validate(items):
    errors, quarantine = [], []
    seen = {}
    out = []
    for i, it in enumerate(items):
        probs = []
        if not DATE_RE.match(it.get("date","")):
            probs.append(f"bad date {it.get('date')}")
        else:
            if not (WINDOW[0] <= it["date"] <= WINDOW[1]):
                probs.append(f"date outside window {it['date']}")
        t = it.get("title","").strip()
        if not (8 <= len(t) <= 300):
            probs.append(f"title length {len(t)}")
        for k in ("pdf_url","page_url"):
            u = it.get(k)
            if u and not any(d in u for d in ALLOWED_DOMAINS):
                probs.append(f"{k} off-domain: {u}")
        if not isinstance(it.get("routine"), bool):
            probs.append("missing routine flag")
        if probs:
            quarantine.append({"idx": i, "title": t[:60], "problems": probs})
            continue
        iid = make_id(it)
        if iid in seen:
            errors.append(f"DUPLICATE: '{t[:60]}' ({it['date']}) collides with '{seen[iid][:60]}'")
            continue
        seen[iid] = t
        it["id"] = iid
        it["status"] = "baseline"
        out.append(it)
    return out, quarantine, errors

def crosscheck(items, signals):
    """Ground truth: telecom-stratum developments found INDEPENDENTLY via secondary-source sweep."""
    ledger = [norm_title(i["title"]) for i in items]
    sig = [norm_title(s["title"]) for s in signals]
    def in_ledger(*kws):
        return any(all(k in t for k in kws) for t in ledger)
    def in_signals(*kws):
        return any(all(k in t for k in kws) for t in sig)
    checks = [
        ("Radio Equipment Possession Rules 08-07",       in_ledger("radio equipment possession")),
        ("Network Authorisation Rules 20-07 (gazette)",  in_ledger("authorisation for telecommunication network")),
        ("User Identification Rules 09-08 (gazette)",    in_ledger("user identification) rules")),
        ("TRAI QoS consultation paper 05-08",            in_ledger("quality of service", "consultation")),
        ("TRAI 1601-series direction 10-08",             in_ledger("1601")),
        ("TRAI 1600/140 clarification 10-07",            in_ledger("1600 series")),
        ("Principal Telecom Services Rules 23-06",       in_ledger("principal telecommunication services")),
        ("Captive Telecom Services Rules 23-06",         in_ledger("captive telecommunication services")),
        ("Miscellaneous Telecom Services Rules 23-06",   in_ledger("miscellaneous telecommunication services")),
        ("Migration Rules 23-06",                        in_ledger("migration) rules")),
        ("eServices Portal notification 23-06",          in_ledger("notification of telecom eservices portal")),
        ("Draft TV/Radio rules under Telecom Act 12-06", in_ledger("television, radio and associated services")),
        ("Draft Spectrum Assignment rules 18-06",        in_ledger("spectrum assignment by administrative process")),
        ("V2X 5.9GHz exemption G.S.R. 466(E) 10-06",     in_ledger("5.875")),
        ("NCCS cloud exemption extension 13-08",         in_ledger("cloud implemented ip routers")),
        ("SIGNAL: net-neutrality reference to TRAI",     in_signals("net neutrality")),
        ("SIGNAL: 9-SIM cap circular (unpublished)",     in_signals("nine connections")),
        ("SIGNAL: Delhi shutdown, no published order",   in_signals("shutdown")),
    ]
    return checks

def main():
    items = json.loads((DATA/"raw_items.json").read_text())
    shelf = json.loads((DATA/"rules_shelf.json").read_text())
    signals = json.loads((DATA/"signals.json").read_text())
    registry = json.loads((ROOT/"registry"/"sources.json").read_text())

    out, quarantine, errors = validate(items)
    out.sort(key=lambda x: (x["date"], x.get("regulator","")), reverse=True)

    sub = [i for i in out if not i.get("routine")]
    rout = [i for i in out if i.get("routine")]
    open_deadlines = [i for i in out if i.get("deadline") and i["deadline"] >= TODAY]

    checks = crosscheck(out, signals)
    passed = sum(1 for _,ok in checks if ok)

    final = {
        "schema_version": "1.0",
        "generated": TODAY,
        "window": {"from": WINDOW[0], "to": WINDOW[1]},
        "stats": {"total": len(out), "substantive": len(sub), "routine": len(rout),
                  "quarantined": len(quarantine), "open_deadlines": len(open_deadlines)},
        "items": out
    }
    (DATA/"items.json").write_text(json.dumps(final, indent=1, ensure_ascii=False))

    print(f"ITEMS: {len(out)} valid | substantive={len(sub)} routine={len(rout)}")
    print(f"SHELF: {len(shelf)} | SIGNALS: {len(signals)} | REGISTRY: {len(registry['sources'])} sources "
          f"({sum(1 for s in registry['sources'] if s['status']=='live')} live)")
    print(f"QUARANTINE: {len(quarantine)}")
    for q in quarantine: print("  !", q)
    for e in errors: print("  DUP!", e)
    print(f"\nGROUND-TRUTH CROSS-CHECK: {passed}/{len(checks)}")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'MISS'}] {name}")
    print("\nSUBSTANTIVE LEDGER (newest first):")
    for i in sub: print(f"  {i['date']}  {i['regulator']:<6} {i.get('type',''):<18} {i['title'][:78]}")
    print("\nOPEN DEADLINES:")
    for i in open_deadlines: print(f"  due {i['deadline']}  {i['title'][:88]}")
    if quarantine or errors or passed < len(checks):
        print("\nRESULT: ATTENTION NEEDED"); sys.exit(1)
    print("\nRESULT: ALL CHECKS PASS")

if __name__ == "__main__":
    main()
