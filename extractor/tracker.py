#!/usr/bin/env python3
"""
TMT Regulatory Radar — local extractor (Lane A: 0% LLM).

Registry-driven, deterministic pipeline:
    fetch (requests) -> parse (fixed selectors/regex) -> validate -> dedupe -> classify (routine regex)
    -> append ledger -> write health -> optionally download instrument PDFs.

Run this on a firm machine with normal internet access (the cloud sandbox that hosts the
assistant cannot reach gov.in domains, which is why this lane exists).

Usage:
    python3 tracker.py sweep                 # check all live sources, append new items
    python3 tracker.py sweep --source trai_directions
    python3 tracker.py fetch-pdfs            # download PDFs for ledger items into instruments/
    python3 tracker.py health                # print last health snapshot

Schedule (macOS):  crontab -e
    0 8-20/2 * * 1-5  cd /path/to/TMT\ Tracker/extractor && /usr/bin/python3 tracker.py sweep >> sweep.log 2>&1
Schedule (Windows): Task Scheduler -> repeat every 2h, 08:00-20:00, action: python3 tracker.py sweep
"""
import argparse, hashlib, json, re, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("pip3 install -r requirements.txt first")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REGISTRY = json.loads((ROOT / "registry" / "sources.json").read_text())
LEDGER = HERE / "ledger.jsonl"
HEALTH = HERE / "health.json"
INSTRUMENTS = ROOT / "instruments"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}

DATE_PATTERNS = [
    (re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"), lambda g: f"{g[2]}-{g[1]}-{g[0]}"),  # DD/MM/YYYY
    (re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b"), lambda g: f"{g[2]}-{g[1]}-{g[0]}"),  # DD-MM-YYYY
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})\b"),                          # DD Month YYYY
     lambda g: f"{g[2]}-{MONTHS[g[1].lower()]:02d}-{int(g[0]):02d}" if g[1].lower() in MONTHS else None),
]

def extract_date(text):
    for rx, fmt in DATE_PATTERNS:
        m = rx.search(text)
        if m:
            iso = fmt(m.groups())
            if iso and re.match(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$", iso):
                return iso
    return None

ROUTINE_RX = REGISTRY["classification"]["routine_regex"]

def is_routine(title):
    """No tiers. One deterministic split: recurring/administrative noise, or substantive."""
    return bool(re.search(ROUTINE_RX, title))

def norm(t):
    return re.sub(r"\s+", " ", t.strip().lower())

def item_id(title, date):
    return hashlib.sha1((norm(title) + date).encode()).hexdigest()[:10]

def load_ledger_ids():
    ids = set()
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            if line.strip():
                ids.add(json.loads(line)["id"])
    return ids

def fetch(url, tolerant_tls=False):
    return requests.get(url, headers=UA, timeout=30, verify=not tolerant_tls)

def parse_listing(source, html, base):
    """Generic deterministic row extraction: for each table row / list item, pair the first
    in-row date with the first in-row document link. Fixed logic — tune per source in the
    registry if a site changes markup."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr")
    if len(rows) < 2:
        rows = soup.select("li, .views-row, .item, article")
    out = []
    for row in rows:
        text = row.get_text(" ", strip=True)
        date = extract_date(text)
        if not date:
            continue
        link = None
        for a in row.select("a[href]"):
            href = urljoin(base, a["href"])
            if any(d in href for d in source.get("allowed_domains", [])):
                link = (href, a.get_text(" ", strip=True))
                if href.lower().endswith(".pdf"):
                    break
        if not link:
            continue
        title = link[1] if len(link[1]) >= 8 else text[:200]
        title = re.sub(r"\s+", " ", title)[:300]
        if len(title) < 8:
            continue
        out.append({"date": date, "title": title, "url": link[0]})
    return out

def sweep(only=None):
    known = load_ledger_ids()
    health, new_items = {}, []
    live = [s for s in REGISTRY["sources"] if s["status"] == "live"
            and s.get("method", "").startswith(("static_html", "rss"))]
    for s in live:
        if only and s["id"] != only:
            continue
        try:
            r = fetch(s["url"], tolerant_tls="tls_tolerant" in s.get("method", ""))
            r.raise_for_status()
            found = parse_listing(s, r.text, s["url"])
            fresh = 0
            for it in found:
                iid = item_id(it["title"], it["date"])
                if iid in known:
                    continue
                routine = is_routine(it["title"])
                rec = {"id": iid, "date": it["date"], "source_id": s["id"], "regulator": s["regulator"],
                       "routine": routine, "title": it["title"], "url": it["url"],
                       "first_seen": datetime.now().isoformat(timespec="seconds"), "status": "new"}
                with LEDGER.open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                known.add(iid); new_items.append(rec); fresh += 1
            health[s["id"]] = {"status": "OK", "rows_seen": len(found), "new": fresh,
                               "checked": datetime.now().isoformat(timespec="seconds")}
        except Exception as e:
            health[s["id"]] = {"status": "FAILED", "error": str(e)[:200],
                               "checked": datetime.now().isoformat(timespec="seconds")}
        time.sleep(1.0)  # be polite to government servers
    HEALTH.write_text(json.dumps(health, indent=1))
    sub = [i for i in new_items if not i["routine"]]
    print(f"sweep done: {len(new_items)} new ({len(sub)} substantive); "
          f"{sum(1 for h in health.values() if h['status']!='OK')} source failures")
    for i in sub:
        print(f"  NEW  {i['date']}  {i['regulator']}  {i['title'][:90]}")
    for sid, h in health.items():
        if h["status"] != "OK":
            print(f"  FAILED  {sid}: {h['error']}")
    # Exit non-zero when a source failed so cron mails/alerts fire. Zero silent failures.
    sys.exit(1 if any(h["status"] != "OK" for h in health.values()) else 0)

def fetch_pdfs():
    INSTRUMENTS.mkdir(exist_ok=True)
    n = 0
    for line in LEDGER.read_text().splitlines() if LEDGER.exists() else []:
        rec = json.loads(line)
        url = rec.get("url", "")
        if not url.lower().endswith(".pdf"):
            continue
        name = f"{rec['date']}_{rec['regulator']}_{rec['id']}.pdf".replace("/", "-")
        dest = INSTRUMENTS / name
        if dest.exists():
            continue
        try:
            r = fetch(url); r.raise_for_status()
            dest.write_bytes(r.content); n += 1
            print("saved", name)
        except Exception as e:
            print("FAILED", url, str(e)[:120])
    print(f"{n} instruments downloaded to {INSTRUMENTS}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sweep", "fetch-pdfs", "health"])
    ap.add_argument("--source")
    a = ap.parse_args()
    if a.cmd == "sweep":
        sweep(a.source)
    elif a.cmd == "fetch-pdfs":
        fetch_pdfs()
    else:
        print(HEALTH.read_text() if HEALTH.exists() else "no health snapshot yet")
