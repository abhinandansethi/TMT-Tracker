#!/usr/bin/env python3
"""TMT Radar — queryable connector API.

The partner's own system runs a pipeline: fetch instruments from here, match them to their
client list and context, draft a client alert email for the partner to review. This is the
boundary the pipeline calls. It is read-only, localhost by default, and serves the same
ledger the dashboard shows — one source of truth.

    engine/.venv/bin/python engine/radar_api.py            # serves http://127.0.0.1:8788
    engine/.venv/bin/python engine/radar_api.py --host 0.0.0.0 --port 8788   # LAN, deliberate

Endpoints (all GET, all JSON):
    /v1/health                         liveness + freshness (last sweep, staleness)
    /v1/sources                        the coverage list: every live source and its footing
    /v1/instruments?…                  filterable list of binding instruments
    /v1/judgments?…                    filterable list of tribunal/court decisions
    /v1/signals?…                      filterable list of non-binding signals (bulletins, leads)
    /v1/items/{id}                     one item, full draft-email-ready payload
    /v1/digest?since=YYYY-MM-DD        everything new since a date, grouped by regulator
    /v1/openapi.json                   the machine-readable contract

Filters on the list endpoints:
    regulator=TRAI   stratum=telecom   type=rules   lane=instruments
    since=2026-08-01 until=2026-08-27  has_deadline=1  q=spectrum   limit=200

Every item payload carries what a draft email needs: the instrument, a crisp summary of
what it pertains to, when it comes into force, both links, and a ready-made citation line.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
import ssl
import urllib.request
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys_path_added = str(HERE)
import sys  # noqa: E402
if sys_path_added not in sys.path:
    sys.path.insert(0, sys_path_added)
from radar import display  # noqa: E402
ITEMS = ROOT / "data" / "items.json"
HEALTH = HERE / "health.json"
REGISTRY = HERE / "registry_v2.json"
IST = timezone(timedelta(hours=5, minutes=30))
VERSION = "1.0"

_fmt = display.fmt_date  # shared date formatter


def _payload(it: dict) -> dict:
    """One instrument, everything a draft client email needs."""
    meta = it.get("meta") or {}
    doc = it.get("doc_url") or it.get("pdf_url")
    page = it.get("page_url")
    reg, title, d = it.get("regulator", ""), it.get("title", ""), it.get("date") or ""
    src = doc or page or meta.get("gazette_id", "")
    citation = f"Source: {reg}, {title}"
    if d:
        citation += f", {_fmt(d)}"
    citation += f". Retrieved from {src}." if src else "."
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return {
        "id": it.get("id"),
        "title": title,
        "short_title": it.get("short") or display.shorten(title),
        "summary": it.get("line") or display.descriptor(it, today),
        "regulator": reg,
        "type": it.get("type"),
        "lane": it.get("lane", "instruments"),
        "stratum": it.get("stratum"),
        "date": it.get("date"),
        "effective_date": meta.get("effective_date"),
        "deadline": it.get("deadline"),
        "impact": meta.get("impact"),
        "amends": meta.get("impacted_rule"),
        "gazette_id": meta.get("gazette_id"),
        "part_section": meta.get("part_section"),
        "parties": meta.get("parties"),
        "document_url": doc,
        "source_page_url": page,
        "citation": citation,
    }


class Store:
    """Reads the exported ledger. Reloaded on each request so the API always reflects the
    latest sweep without a restart."""

    def load(self) -> tuple:
        data = json.loads(ITEMS.read_text()) if ITEMS.exists() else {"items": [], "stats": {}}
        health = json.loads(HEALTH.read_text()) if HEALTH.exists() else {"sources": {}}
        return data, health

    def sources(self) -> List[dict]:
        reg = json.loads(REGISTRY.read_text())
        health = json.loads(HEALTH.read_text()) if HEALTH.exists() else {"sources": {}}
        out = []
        for s in reg["sources"]:
            if s.get("status") != "live":
                continue
            h = health.get("sources", {}).get(s["id"], {})
            out.append({
                "id": s["id"], "regulator": s["regulator"], "name": s["name"],
                "stratum": s["stratum"], "lane": s.get("lane", "instruments"),
                "landing_page": s["url"], "health": h.get("status"),
                "newest_visible": h.get("newest_visible"),
            })
        return out


STORE = Store()


def _filtered(items: List[dict], q: Dict[str, List[str]], lane: str) -> List[dict]:
    def one(k: str) -> Optional[str]:
        return q.get(k, [None])[0]
    items = [i for i in items if i.get("lane", "instruments") == lane]
    reg, strat, typ = one("regulator"), one("stratum"), one("type")
    since, until, text = one("since"), one("until"), one("q")
    if reg:
        items = [i for i in items if (i.get("regulator") or "").lower() == reg.lower()]
    if strat:
        items = [i for i in items if i.get("stratum") == strat]
    if typ:
        items = [i for i in items if (i.get("type") or "") == typ]
    if since:
        items = [i for i in items if (i.get("date") or "") >= since]
    if until:
        items = [i for i in items if (i.get("date") or "9999") <= until]
    if one("has_deadline") in ("1", "true", "yes"):
        items = [i for i in items if i.get("deadline")]
    if text:
        t = text.lower()
        items = [i for i in items if t in json.dumps(i, ensure_ascii=False).lower()]
    items.sort(key=lambda i: (i.get("date") or ""), reverse=True)
    try:
        lim = int(one("limit") or 500)
    except ValueError:
        lim = 500
    return items[:max(0, lim)]


OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "TMT Regulatory Radar API", "version": VERSION,
             "description": "Read-only feed of Indian TMT regulatory instruments and judgments "
                            "for a law firm's client-alert pipeline. Every item payload is "
                            "draft-email-ready."},
    "paths": {
        "/v1/health": {"get": {"summary": "Liveness and data freshness"}},
        "/v1/sources": {"get": {"summary": "The coverage list: every live source"}},
        "/v1/instruments": {"get": {"summary": "Binding instruments",
            "parameters": [{"name": n, "in": "query"} for n in
                           ("regulator", "stratum", "type", "since", "until", "has_deadline", "q", "limit")]}},
        "/v1/judgments": {"get": {"summary": "Tribunal and court decisions (same filters)"}},
        "/v1/signals": {"get": {"summary": "Non-binding signals: bulletins, diaries, unpublished-instrument leads (same filters)"}},
        "/v1/items/{id}": {"get": {"summary": "One item, full payload"}},
        "/v1/digest": {"get": {"summary": "Everything new since ?since=YYYY-MM-DD, grouped by regulator"}},
        "/v1/doc": {"get": {"summary": "Proxy a feed document, re-served inline (opens forced-download PDFs in-browser). ?u=<doc url from the feed>"}},
    },
}


_PROXY_UA = "Mozilla/5.0 (compatible; TMTRegulatoryRadar/2.0; Trilegal internal regulatory monitoring)"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: Any) -> None:
        payload = json.dumps(body, ensure_ascii=False, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")  # read-only feed, safe to embed
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path, q = u.path.rstrip("/"), parse_qs(u.query)
        data, health = STORE.load()
        items = data.get("items", [])

        if path in ("", "/v1", "/v1/openapi.json"):
            return self._send(200, OPENAPI)
        if path == "/v1/health":
            gen = health.get("generated") or data.get("generated")
            age_h = None
            if gen:
                try:
                    age_h = round((datetime.now(IST) - datetime.fromisoformat(gen)).total_seconds() / 3600, 1)
                except ValueError:
                    pass
            return self._send(200, {"ok": True, "version": VERSION, "last_sweep": gen,
                                    "age_hours": age_h, "stale": age_h is None or age_h > 26,
                                    "stats": data.get("stats", {})})
        if path == "/v1/sources":
            return self._send(200, {"sources": STORE.sources()})
        if path == "/v1/instruments":
            rows = _filtered(items, q, "instruments")
            return self._send(200, {"count": len(rows), "items": [_payload(i) for i in rows]})
        if path == "/v1/judgments":
            rows = _filtered(items, q, "judgments")
            return self._send(200, {"count": len(rows), "items": [_payload(i) for i in rows]})
        if path == "/v1/signals":
            # non-binding leads: security bulletins, court diaries, announcements, and
            # reported-but-unpublished instrument signals. Same filters as the other lists.
            rows = _filtered(items, q, "signals")
            return self._send(200, {"count": len(rows), "items": [_payload(i) for i in rows]})
        if path == "/v1/doc":
            # Document proxy: fetch a government PDF server-side and re-serve it INLINE, so
            # forced-download endpoints (e.g. MTCTE, served as application/octet-stream +
            # attachment, which no in-browser viewer can render) open in the tab instead of
            # downloading. SSRF guard: only URLs that already appear in the feed are proxied.
            target = (q.get("u") or [""])[0]
            allowed = {i.get(f) for i in items for f in ("doc_url", "page_url", "pdf_url") if i.get(f)}
            if target not in allowed:
                return self._send(403, {"error": "url not in feed",
                                        "hint": "only documents present in /v1/instruments|judgments|signals can be proxied"})
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # some gov TLS chains are flaky; this is a read-only GET
                req = urllib.request.Request(target, headers={"User-Agent": _PROXY_UA})
                with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:  # noqa: S310 (feed-gated)
                    body = resp.read()
                    upstream_ct = resp.headers.get("Content-Type", "")
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": "upstream fetch failed", "detail": str(e)[:200]})
            # normalise a PDF byte-stream to a renderable type; otherwise pass the real type through
            ctype = "application/pdf" if body[:4] == b"%PDF" else (upstream_ct or "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", "inline")          # override any attachment
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/v1/digest":
            since = q.get("since", [(date.today() - timedelta(days=7)).isoformat()])[0]
            rows = [i for i in items if i.get("lane", "instruments") == "instruments"
                    and (i.get("date") or "") >= since]
            grouped: Dict[str, list] = {}
            for i in sorted(rows, key=lambda x: (x.get("date") or ""), reverse=True):
                grouped.setdefault(i.get("regulator", "?"), []).append(_payload(i))
            return self._send(200, {"since": since, "count": len(rows), "by_regulator": grouped})
        if path.startswith("/v1/items/"):
            iid = path.rsplit("/", 1)[-1]
            hit = next((i for i in items if i.get("id") == iid), None)
            return self._send(200, _payload(hit)) if hit else self._send(404, {"error": "not found", "id": iid})
        return self._send(404, {"error": "unknown endpoint", "path": path, "see": "/v1/openapi.json"})

    def log_message(self, *_a) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    scope = "localhost only" if a.host == "127.0.0.1" else f"exposed on {a.host}"
    print(f"TMT Radar API v{VERSION}  ->  http://{a.host}:{a.port}/v1/openapi.json  ({scope})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
