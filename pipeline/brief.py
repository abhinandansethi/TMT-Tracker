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

Model provider — either works, pick by which key you have. Nothing is stored here;
keys come from the environment (locally: export the variable; in CI: a repository secret):
  OPENAI_API_KEY     -> OpenAI backend (default model gpt-5-mini)
  ANTHROPIC_API_KEY  -> Anthropic backend (default model claude-opus-5)
Override with TMT_BRIEF_PROVIDER=openai|anthropic and TMT_BRIEF_MODEL=<model id>.

Usage:
    python3 pipeline/brief.py --dry-run --limit 3     # extract + show the prompt, no API call, no creds needed
    python3 pipeline/brief.py                          # brief the actionable instruments (needs creds)
    python3 pipeline/brief.py --all --limit 20         # brief any instrument/judgment with a document
    python3 pipeline/brief.py --ids 09a96cbe62 ab12cd  # brief specific items
    TMT_BRIEF_MODEL=claude-sonnet-5 python3 pipeline/brief.py   # cheaper model for a large batch
"""
from __future__ import annotations
import argparse, datetime, hashlib, io, json, os, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ITEMS = ROOT / "data" / "items.json"
CACHE = ROOT / "pipeline" / "brief_cache.json"

# Same honest identifying UA the engine fetches with (Principle 2): not a browser
# spoof, but not a crawler-signature token either — passes the gov.in Akamai WAFs.
CONTACT = os.environ.get("TMT_RADAR_CONTACT", "compliance@trilegal.com")
UA = {"User-Agent": "Mozilla/5.0 (compatible; TMTRegulatoryRadar/2.0; Trilegal internal regulatory monitoring)",
      "From": CONTACT}

def _resolve_provider():
    p = (os.environ.get("TMT_BRIEF_PROVIDER") or "").lower()
    if p and p not in ("openai", "anthropic"):
        sys.exit(f"unknown TMT_BRIEF_PROVIDER={p!r} (use openai|anthropic)")
    if p:
        return p
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "anthropic"   # key, auth token, or an `ant auth login` profile

PROVIDER = _resolve_provider()
MODEL = os.environ.get("TMT_BRIEF_MODEL") or ("gpt-5-mini" if PROVIDER == "openai" else "claude-opus-5")
EFFORT = os.environ.get("TMT_BRIEF_EFFORT", "medium")   # low | medium | high | xhigh | max
MAX_CHARS = 18000                                        # ~first dozen pages; enough for the operative part

# Bumped whenever SYSTEM/SCHEMA change: cached briefs written under an older prompt are
# regenerated rather than left in place, so a prompt fix actually reaches the page.
PROMPT_VERSION = 2

SYSTEM = (
    "You brief a busy TMT (technology, media, telecom) lawyer at an Indian law firm on ONE "
    "regulatory instrument or decision from an Indian regulator, tribunal or court.\n\n"
    "Say what it DOES, as briefly as possible.\n\n"
    "Rules:\n"
    "- 'brief': ONE sentence, 30 words or fewer. Lead with the substantive action — what is now "
    "required, permitted, prohibited, extended, exempted, amended, or decided — and name the "
    "thing it applies to plus any date, threshold or duration that defines it.\n"
    "- Do NOT restate the title, the file/reference number, or the citation. A reader who has "
    "already read the heading must learn something new from your sentence.\n"
    "- Do not quote long passages. Do not give advice or recommendations.\n"
    "- 'so_what': at most 20 words on the concrete obligation and who it binds. Use an empty "
    "string if the instrument imposes nothing concrete.\n"
    "- If the extracted text is truncated, garbled, or does not show the operative substance, "
    "set confidence to \"low\" and say plainly in 'brief' that the substance was not readable "
    "rather than guessing.\n"
    "- For a judgment: say what was held and who prevailed, not the procedural history."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "brief": {
            "type": "string",
            "description": "ONE sentence, 30 words or fewer, saying what the instrument does — "
                           "the substantive change, not the title restated.",
        },
        "so_what": {
            "type": "string",
            "description": "At most 20 words on the concrete obligation and who it binds. "
                           "Empty string if it imposes nothing concrete.",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "low if the extracted text was truncated, garbled, or did not show "
                           "the operative substance.",
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


def _pdf_text(body):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(body))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages[:12]).strip()


def _html_text(body):
    """Text of an HTML document. Most TDSAT/court decisions and several regulator pages are
    served as HTML, not PDF — treating those as unreadable silently discarded the majority of
    the corpus (24 of 25 in a sample), which is why briefs covered so few items."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form"]):
        tag.decompose()
    # Prefer the main content region when the page marks one; fall back to the whole body.
    node = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    text = node.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_text(url):
    """Fetch a document and return its text, whether it is a PDF or an HTML page.
    Raises with a stated reason on anything unusable, so the caller can log why."""
    import requests
    r = requests.get(url, headers=UA, timeout=45)
    r.raise_for_status()
    ct = (r.headers.get("content-type") or "").lower()
    body = r.content

    if body[:5] == b"%PDF-" or "pdf" in ct or url.lower().endswith(".pdf"):
        text, kind = _pdf_text(body), "pdf"
    elif "html" in ct or body[:200].lstrip()[:1] == b"<":
        text, kind = _html_text(body), "html"
    else:
        raise ValueError(f"unsupported document type (content-type={ct or 'none'})")

    if len(text) < 120:
        raise ValueError(f"no extractable text from {kind} "
                         f"({len(text)} chars — scanned image, or the body is script-rendered)")
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


def call_openai(client, it, text):
    """Same brief via the OpenAI API (strict JSON-schema output)."""
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": build_user_prompt(it, text)}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "instrument_brief", "strict": True,
                                         "schema": SCHEMA}},
    )
    choice = resp.choices[0]
    if getattr(choice.message, "refusal", None):
        raise RuntimeError(f"model declined: {choice.message.refusal[:120]}")
    data = json.loads(choice.message.content)
    return {k: data[k] for k in ("brief", "so_what", "confidence")}


def call_llm(client, it, text):
    return call_openai(client, it, text) if PROVIDER == "openai" else call_claude(client, it, text)


def select(items, args):
    if args.ids:
        want = set(args.ids)
        chosen = [i for i in items if i.get("id") in want]
    elif args.all:
        chosen = [i for i in items if i.get("lane") in ("instruments", "judgments") and doc_url_of(i)]
    elif args.actionable:
        chosen = [i for i in items if is_actionable(i) and doc_url_of(i)]
    else:
        # Default is every instrument and judgment with a document. The cache makes this cheap:
        # each run only briefs what is new or stale, so coverage fills in across runs.
        chosen = [i for i in items if i.get("lane") in ("instruments", "judgments") and doc_url_of(i)]
    chosen.sort(key=lambda i: (i.get("date") or ""), reverse=True)
    if args.limit:
        chosen = chosen[: args.limit]
    return chosen


def main():
    ap = argparse.ArgumentParser(description="Generate LLM briefs for TMT Radar instruments.")
    ap.add_argument("--all", action="store_true", help="every instrument/judgment with a document (the default)")
    ap.add_argument("--actionable", action="store_true", help="restrict to actionable instruments only")
    ap.add_argument("--ids", nargs="*", help="brief only these item ids")
    ap.add_argument("--limit", type=int, default=0, help="cap how many items to brief")
    ap.add_argument("--force", action="store_true", help="re-brief even if a fresh cache entry exists")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds to pause between document fetches (default 1.0). Keeps bulk "
                         "runs at the de-minimis, non-disruptive load the legal analysis records.")
    ap.add_argument("--dry-run", action="store_true", help="extract text and print the prompt; make NO API call (needs no credentials)")
    args = ap.parse_args()

    feed = load_json(ITEMS, {})
    items = feed.get("items", []) if isinstance(feed, dict) else feed
    if not items:
        sys.exit(f"no items in {ITEMS} — run a sweep + export first")

    cache = load_json(CACHE, {})
    chosen = select(items, args)
    print(f"[brief] {len(chosen)} item(s) selected · provider={PROVIDER} model={MODEL} · dry_run={args.dry_run}")

    client = None
    if not args.dry_run:
        try:
            if PROVIDER == "openai":
                from openai import OpenAI
                client = OpenAI()            # resolves OPENAI_API_KEY from the environment
            else:
                import anthropic
                client = anthropic.Anthropic()   # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ant profile
        except Exception as e:
            sys.exit(f"[brief] cannot start the {PROVIDER} client ({e}). Set "
                     f"{'OPENAI_API_KEY' if PROVIDER == 'openai' else 'ANTHROPIC_API_KEY'} "
                     f"(or use --dry-run to test extraction without credentials).")

    done = skipped = failed = fetched = 0
    for it in chosen:
        iid, title = it.get("id"), (it.get("short") or it.get("title") or "")[:70]
        h = doc_hash(it)
        prev = cache.get(iid, {})
        if (not args.force and prev.get("doc_hash") == h
                and prev.get("prompt_version") == PROMPT_VERSION):
            skipped += 1
            continue
        url = doc_url_of(it)
        if fetched and args.delay:
            time.sleep(args.delay)
        fetched += 1
        try:
            text = extract_text(url)
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
            brief = call_llm(client, it, text)
        except Exception as e:
            print(f"  fail  {iid}  {title}  — {e}")
            failed += 1
            continue
        cache[iid] = {**brief, "model": MODEL, "provider": PROVIDER, "doc_hash": h,
                      "prompt_version": PROMPT_VERSION,
                      "generated_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")}
        CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))  # checkpoint each success
        print(f"  ok   [{done + 1:>3}/{len(chosen)}] {iid}  {title}  [{brief['confidence']}]", flush=True)
        done += 1

    print(f"\n[brief] done={done} skipped(cached)={skipped} failed/no-text={failed}")
    if not args.dry_run:
        print(f"[brief] cache: {CACHE}  ({len(cache)} briefs) — rebuild the dashboard to embed them")


if __name__ == "__main__":
    main()
