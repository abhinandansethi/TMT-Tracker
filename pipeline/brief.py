#!/usr/bin/env python3
"""LLM-brief generator for the TMT Radar pipeline.

The dashboard's deterministic brief is composed from an instrument's *metadata*
(type, amended section, effective date, action flag). It can say "amends s.56 of
the Telecommunications Act, 2023", but it cannot say what the new s.56 actually
*requires* — that lives in the PDF body, which the deterministic engine never reads.

This module reads that body. For each actionable instrument it fetches the
document, extracts the text, and asks Claude for a short, factual, lawyer-facing
brief of what the instrument does. Output is cached to ``pipeline/brief_cache.json``
keyed by item id, which the dashboard build embeds so the Clients tab can show the
substantive brief alongside the deterministic one.

Design constraints, in keeping with the rest of the project:
- **Server-side only.** The published dashboard is a static, sandboxed page that
  cannot fetch gov.in or call an LLM at runtime. Briefs are generated here, on the
  firm machine, and baked into the page.
- **Additive, never load-bearing.** If credentials are absent, the document can't
  be fetched, or Claude declines, the item simply gets no LLM brief and the
  deterministic brief stands. Nothing breaks.
- **Not legal advice.** The prompt forbids recommendations; every brief carries a
  confidence flag and rides under the dashboard's existing "verify before sending"
  banner. The feed is a monitoring signal, checked by a partner against the
  official text (docs/CONNECTOR.md, rule 2 of the boundary).

Credentials resolve the standard way (anthropic SDK): ANTHROPIC_API_KEY, or
ANTHROPIC_AUTH_TOKEN, or an ``ant auth login`` profile. No key is stored here.

Usage:
    python3 pipeline/brief.py --dry-run --limit 3     # extract + show the prompt, no API call, no creds needed
    python3 pipeline/brief.py                          # brief the actionable instruments (needs creds)
    python3 pipeline/brief.py --all --limit 20         # brief any instrument/judgment with a document
    python3 pipeline/brief.py --ids 09a96cbe62 ab12cd  # brief specific items
    TMT_BRIEF_MODEL=claude-sonnet-5 python3 pipeline/brief.py   # cheaper model for a large batch
"""
from __future__ import annotations
import argparse, datetime, hashlib, io, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ITEMS = ROOT / "data" / "items.json"
CACHE = ROOT / "pipeline" / "brief_cache.json"

# Same honest identifying UA the engine fetches with (Principle 2): not a browser
# spoof, but not a crawler-signature token either — passes the gov.in Akamai WAFs.
CONTACT = os.environ.get("TMT_RADAR_CONTACT", "compliance@trilegal.com")
UA = {"User-Agent": "Mozilla/5.0 (compatible; TMTRegulatoryRadar/2.0; Trilegal internal regulatory monitoring)",
      "From": CONTACT}

MODEL = os.environ.get("TMT_BRIEF_MODEL", "claude-opus-5")
EFFORT = os.environ.get("TMT_BRIEF_EFFORT", "medium")   # low | medium | high | xhigh | max
MAX_CHARS = 18000                                        # ~first dozen pages; enough for the operative part

SYSTEM = (
    "You brief a busy TMT (technology, media, telecom) lawyer at an Indian law firm. "
    "You are given the text of ONE regulatory instrument issued by an Indian regulator — a "
    "rule, notification, order, direction, press note, or advisory. Summarise what it "
    "actually does, precisely and factually, for a lawyer who will verify against the official "
    "text before advising a client.\n\n"
    "Rules:\n"
    "- Quote the specific section numbers, obligations, thresholds, entities covered, and dates "
    "the text states. Prefer the instrument's own operative language.\n"
    "- State only what the instrument says. Do not infer obligations it does not contain, and do "
    "not add background the text does not supply.\n"
    "- Never give advice or a recommendation. Describe the instrument, not what anyone should do.\n"
    "- If the extracted text looks truncated, garbled, or does not clearly contain the operative "
    "provisions, say so and set confidence to \"low\"."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "brief": {
            "type": "string",
            "description": "2-4 sentences: what the instrument is and what it does or changes, "
                           "with the specific sections, obligations, covered entities and dates "
                           "the text states.",
        },
        "so_what": {
            "type": "string",
            "description": "One sentence on the concrete compliance implication for a regulated "
                           "entity, grounded only in the text. No recommendation.",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "low if the extracted text was truncated, garbled, or did not clearly "
                           "contain the operative provisions.",
        },
    },
    "required": ["brief", "so_what", "confidence"],
    "additionalProperties": False,
}


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def doc_url_of(it):
    return it.get("document_url") or it.get("doc_url") or it.get("source_page_url") or ""


def doc_hash(it):
    key = doc_url_of(it) + "|" + (it.get("title") or "")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def is_actionable(it):
    if it.get("lane") != "instruments" or it.get("routine"):
        return False
    m = it.get("meta") or {}
    t = (it.get("type") or "").lower()
    binding = any(k in t for k in ("rule", "regulation", "notif", "direction", "amend"))
    flagged = (str(m.get("impact") or "").lower().find("action") >= 0
               or bool(it.get("deadline")) or bool(m.get("effective_date")))
    return binding or flagged


def extract_pdf_text(url):
    """Fetch a document and return its text. Raises on anything that isn't a usable PDF."""
    import requests
    from pypdf import PdfReader
    r = requests.get(url, headers=UA, timeout=45)
    r.raise_for_status()
    ct = (r.headers.get("content-type") or "").lower()
    body = r.content
    looks_pdf = body[:5] == b"%PDF-" or "pdf" in ct or url.lower().endswith(".pdf")
    if not looks_pdf:
        raise ValueError(f"not a PDF (content-type={ct or 'none'})")
    reader = PdfReader(io.BytesIO(body))
    pages = [(pg.extract_text() or "") for pg in reader.pages[:12]]
    text = "\n".join(pages).strip()
    if len(text) < 120:
        raise ValueError(f"no extractable text ({len(text)} chars — likely a scanned image)")
    return text[:MAX_CHARS]


def build_user_prompt(it, text):
    hdr = [f"Instrument: {it.get('title')}",
           f"Regulator: {it.get('regulator')}",
           f"Type: {it.get('type')}",
           f"Dated: {it.get('date')}"]
    if it.get("amends"):
        hdr.append(f"Stated to amend: {it['amends']}")
    if (it.get('meta') or {}).get('effective_date'):
        hdr.append(f"Effective date (metadata): {it['meta']['effective_date']}")
    return "\n".join(hdr) + "\n\n--- INSTRUMENT TEXT (may be truncated to the first pages) ---\n" + text


def call_claude(client, it, text):
    """Return {brief, so_what, confidence} or None on refusal/parse failure."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,   # room for adaptive thinking (on by default on Opus 5) + the short JSON
        system=SYSTEM,
        messages=[{"role": "user", "content": build_user_prompt(it, text)}],
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        cat = getattr(getattr(resp, "stop_details", None), "category", None)
        raise RuntimeError(f"model declined (category={cat})")
    txt = next((b.text for b in resp.content if b.type == "text"), None)
    if not txt:
        raise RuntimeError("no text block in response")
    data = json.loads(txt)
    return {k: data[k] for k in ("brief", "so_what", "confidence")}


def select(items, args):
    if args.ids:
        want = set(args.ids)
        chosen = [i for i in items if i.get("id") in want]
    elif args.all:
        chosen = [i for i in items if i.get("lane") in ("instruments", "judgments") and doc_url_of(i)]
    else:
        chosen = [i for i in items if is_actionable(i) and doc_url_of(i)]
    chosen.sort(key=lambda i: (i.get("date") or ""), reverse=True)
    if args.limit:
        chosen = chosen[: args.limit]
    return chosen


def main():
    ap = argparse.ArgumentParser(description="Generate LLM briefs for TMT Radar instruments.")
    ap.add_argument("--all", action="store_true", help="brief every instrument/judgment with a document (not just actionable)")
    ap.add_argument("--ids", nargs="*", help="brief only these item ids")
    ap.add_argument("--limit", type=int, default=0, help="cap how many items to brief")
    ap.add_argument("--force", action="store_true", help="re-brief even if a fresh cache entry exists")
    ap.add_argument("--dry-run", action="store_true", help="extract text and print the prompt; make NO API call (needs no credentials)")
    args = ap.parse_args()

    feed = load_json(ITEMS, {})
    items = feed.get("items", []) if isinstance(feed, dict) else feed
    if not items:
        sys.exit(f"no items in {ITEMS} — run a sweep + export first")

    cache = load_json(CACHE, {})
    chosen = select(items, args)
    print(f"[brief] {len(chosen)} item(s) selected · model={MODEL} effort={EFFORT} · dry_run={args.dry_run}")

    client = None
    if not args.dry_run:
        try:
            import anthropic
            client = anthropic.Anthropic()   # resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ant profile
        except Exception as e:
            sys.exit(f"[brief] cannot start the Anthropic client ({e}). Set ANTHROPIC_API_KEY or run `ant auth login`, "
                     f"or use --dry-run to test extraction without credentials.")

    done = skipped = failed = 0
    for it in chosen:
        iid, title = it.get("id"), (it.get("short") or it.get("title") or "")[:70]
        h = doc_hash(it)
        if not args.force and cache.get(iid, {}).get("doc_hash") == h:
            skipped += 1
            continue
        url = doc_url_of(it)
        try:
            text = extract_pdf_text(url)
        except Exception as e:
            print(f"  skip  {iid}  {title}  — {e}")
            failed += 1
            continue

        if args.dry_run:
            print(f"\n===== {iid}  {title} =====")
            print(f"  url: {url}")
            print(f"  extracted {len(text)} chars; prompt preview:\n")
            print("  " + build_user_prompt(it, text)[:700].replace("\n", "\n  ") + " …")
            done += 1
            continue

        try:
            brief = call_claude(client, it, text)
        except Exception as e:
            print(f"  fail  {iid}  {title}  — {e}")
            failed += 1
            continue
        cache[iid] = {**brief, "model": MODEL, "doc_hash": h,
                      "generated_at": datetime.datetime.now().strftime("%Y-%m-%d")}
        CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))  # checkpoint each success
        print(f"  ok    {iid}  {title}  [{brief['confidence']}]")
        done += 1

    print(f"\n[brief] done={done} skipped(cached)={skipped} failed/no-text={failed}")
    if not args.dry_run:
        print(f"[brief] cache: {CACHE}  ({len(cache)} briefs) — rebuild the dashboard to embed them")


if __name__ == "__main__":
    main()
