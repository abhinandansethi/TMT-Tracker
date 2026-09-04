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
import argparse, collections, base64, datetime, hashlib, io, json, os, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ITEMS = ROOT / "data" / "items.json"
CACHE = ROOT / "pipeline" / "brief_cache.json"
CLIENTS = ROOT / "pipeline" / "clients.json"

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
MODEL = os.environ.get("TMT_BRIEF_MODEL") or ("gpt-5.6-luna" if PROVIDER == "openai" else "claude-opus-5")
# Vision model for scanned documents. Only the OpenAI path is wired today; the Anthropic path
# would need image content blocks, so a scan simply stays unbriefed there rather than pretending.
VISION_MODEL = os.environ.get("TMT_VISION_MODEL", "gpt-4o-mini")
EFFORT = os.environ.get("TMT_BRIEF_EFFORT", "medium")   # low | medium | high | xhigh | max
MAX_CHARS = 18000
MAX_FETCH_SECONDS = 90          # hard wall-clock cap per document
VISION_PAGES = 4                # pages rendered for a scanned document (the operative part)
VISION_DPI = 150                # legible for a model without ballooning the payload
MAX_BYTES = 40_000_000          # refuse absurd payloads rather than buffer them                                        # ~first dozen pages; enough for the operative part

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


def write_cache(cache):
    """Write the cache atomically. Path.write_text truncates before writing, so an interrupt in
    that window leaves an empty file — after which this script reloads {} and re-bills every
    brief, and the dashboard build dies on unparseable JSON. Write a sibling, then rename."""
    tmp = CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    os.replace(tmp, CACHE)


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


def extract_text(url, max_chars=None):
    """Fetch a document and return its text, whether it is a PDF or an HTML page.
    Raises with a stated reason on anything unusable, so the caller can log why."""
    import requests
    # requests' timeout= is per socket read, not a total budget: a server trickling one byte at a
    # time holds the fetch open indefinitely (measured: 12.1s against a 1s timeout). Stream with a
    # wall-clock deadline and a size cap so one slow host cannot consume the whole run.
    deadline = time.monotonic() + MAX_FETCH_SECONDS
    r = requests.get(url, headers=UA, timeout=20, stream=True)
    r.raise_for_status()
    ct = (r.headers.get("content-type") or "").lower()
    chunks, total = [], 0
    for chunk in r.iter_content(65536):
        chunks.append(chunk); total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError(f"document exceeds {MAX_BYTES // 1_000_000}MB — abandoning")
        if time.monotonic() > deadline:
            raise ValueError(f"fetch exceeded {MAX_FETCH_SECONDS}s wall clock (server trickling)")
    body = b"".join(chunks)

    # Sniff the body before trusting the URL suffix: gov.in servers routinely answer a dead .pdf
    # path with a 200 HTML error page, which a suffix-first rule parses as a broken PDF and
    # reports as "no extractable text" instead of reading it.
    if body[:5] == b"%PDF-":
        text, kind = _pdf_text(body), "pdf"
    elif body[:200].lstrip()[:1] == b"<" or "html" in ct:
        text, kind = _html_text(body), "html"
    elif "pdf" in ct or url.lower().endswith(".pdf"):
        text, kind = _pdf_text(body), "pdf"
    else:
        raise ValueError(f"unsupported document type (content-type={ct or 'none'})")

    if len(text) < 120:
        raise ValueError(f"no extractable text from {kind} "
                         f"({len(text)} chars — scanned image, or the body is script-rendered)")
    # brief.py wants ~12 pages for a one-sentence brief; the scan layer wants more so that a
    # citation can point past the recitals. Callers choose; the default is unchanged.
    return text[:(max_chars or MAX_CHARS)]


def render_pages(body: bytes, pages: int = VISION_PAGES, dpi: int = VISION_DPI):
    """PNG bytes for the first pages of a PDF that has no text layer.

    Many regulators (CCPA, IN-SPACe, CBFC) publish scans, where the PDF carries images and no
    extractable characters. Rasterising and letting a vision model read the page is still
    reading the fetched document — the same act as parsing its text layer — not recall from
    memory. It is marked as such in the cache so the provenance is never ambiguous."""
    import fitz
    doc = fitz.open(stream=body, filetype="pdf")
    out = []
    for i in range(min(pages, doc.page_count)):
        out.append(doc[i].get_pixmap(dpi=dpi).tobytes("png"))
    return out


def fetch_raw(url):
    """The document bytes, under the same wall-clock and size bounds as extract_text."""
    import requests
    deadline = time.monotonic() + MAX_FETCH_SECONDS
    r = requests.get(url, headers=UA, timeout=20, stream=True)
    r.raise_for_status()
    chunks, total = [], 0
    for chunk in r.iter_content(65536):
        chunks.append(chunk); total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError("document too large")
        if time.monotonic() > deadline:
            raise ValueError("fetch exceeded wall clock")
    return b"".join(chunks)


def call_openai_vision(client, it, images):
    """Brief a scanned document by reading rendered page images."""
    content = [{"type": "text", "text": build_user_prompt(it, "(scanned document — read the page images below)")}]
    for png in images:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}})
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{"role": "system", "content": SYSTEM + "\n\nThe document is a SCAN with no text "
                   "layer; you are reading page images. If the scan is too poor to read the "
                   "operative part, say so and set confidence low rather than guessing."},
                  {"role": "user", "content": content}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "instrument_brief", "strict": True, "schema": SCHEMA}},
    )
    choice = resp.choices[0]
    if getattr(choice.message, "refusal", None):
        raise RuntimeError(f"model declined: {choice.message.refusal[:120]}")
    data = json.loads(choice.message.content)
    return {k: data[k] for k in ("brief", "so_what", "confidence")}


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


def needs_brief(it, cache, force=False):
    """True when this item has no current brief — the same test the run loop applies."""
    if force:
        return True
    prev = cache.get(it["id"], {})
    return not (prev.get("doc_hash") == doc_hash(it)
                and prev.get("prompt_version") == PROMPT_VERSION)


def client_matched(items) -> set:
    """Ids of items that match a client's watch keywords, by the SAME rule the dashboard uses:
    each keyword is a regex tested against the item's short title, one-line descriptor, official
    title and type. Kept identical on purpose — an item the Clients tab shows the partner and an
    item this pipeline briefs must be the same set, or the tab fills with metadata boilerplate
    while the briefs land on rows nobody opened.

    That is not hypothetical. Before this existed, the twelve client matches without a brief were
    the Telecommunications (User Identification) Rules 2026, the Dark Patterns Guidelines, two
    CCPA dark-pattern orders, Press Note 3 and CERT-In CISG-2026-03 — the six most client-facing
    documents in the tracker, shown to the partner as "DPIIT has issued Press Note 3 (2026)."
    """
    try:
        roster = json.loads(CLIENTS.read_text()).get("clients", [])
    except Exception:
        return set()
    rxs = []
    for cl in roster:
        for k in (cl.get("watch") or {}).get("keywords") or []:
            try:
                rxs.append(re.compile(k, re.I))
            except re.error:
                pass                      # a bad pattern is the roster's problem, not a crash here
    if not rxs:
        return set()
    out = set()
    for it in items:
        hay = " ".join(str(it.get(f) or "") for f in ("short", "line", "title", "type"))
        if any(rx.search(hay) for rx in rxs):
            out.add(it.get("id"))
    return out


def select(items, args, cache=None):
    if args.ids:
        want = set(args.ids)
        chosen = [i for i in items if i.get("id") in want]
    elif args.actionable:
        chosen = [i for i in items if is_actionable(i) and doc_url_of(i)]
    else:
        # Default (and --all) is every instrument and judgment with a document. The cache makes
        # this cheap: a run only briefs what is new or stale, so coverage fills in across runs.
        chosen = [i for i in items if i.get("lane") in ("instruments", "judgments") and doc_url_of(i)]
    # Client-matched items first, then newest. Date order alone is the wrong priority: it ranks
    # by when a venue published, not by whether anyone is waiting to read it, so a run that does
    # not finish leaves exactly the Clients tab bare. Undated rows still sort below dated ones
    # within each group — 105 were unreachable under any limit short of the whole corpus — but a
    # client match now outranks recency, so the partner-facing view fills first.
    matched = client_matched(items)
    if args.clients:
        chosen = [i for i in chosen if i.get("id") in matched]
    chosen.sort(key=lambda i: (i.get("id") in matched, i.get("date") or "0000-00-00"), reverse=True)
    # The cap must limit WORK, not selection. Truncating before the freshness test spent the
    # whole budget on already-briefed items, which made a capped run a silent no-op: the sweep's
    # --limit 40 currently briefs nothing while hundreds remain outstanding.
    if args.limit:
        if cache is not None:
            chosen = [i for i in chosen if needs_brief(i, cache, args.force)]
        chosen = chosen[: args.limit]
    return chosen


def main():
    ap = argparse.ArgumentParser(description="Generate LLM briefs for TMT Radar instruments.")
    ap.add_argument("--all", action="store_true", help="every instrument/judgment with a document (the default)")
    ap.add_argument("--actionable", action="store_true", help="restrict to actionable instruments only")
    ap.add_argument("--ids", nargs="*", help="brief only these item ids")
    ap.add_argument("--clients", action="store_true",
                    help="brief only what the client roster matches — the Clients tab, filled "
                         "in one short run instead of waiting out the whole backlog")
    ap.add_argument("--limit", type=int, default=0, help="cap how many items to brief")
    ap.add_argument("--force", action="store_true", help="re-brief even if a fresh cache entry exists")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds to pause between document fetches (default 1.0). Keeps bulk "
                         "runs at the de-minimis, non-disruptive load the legal analysis records.")
    ap.add_argument("--dry-run", action="store_true", help="extract text and print the prompt; make NO API call (needs no credentials)")
    args = ap.parse_args()
    args.delay = max(0.0, args.delay)   # a negative sleep would kill a run hours in

    feed = load_json(ITEMS, {})
    items = feed.get("items", []) if isinstance(feed, dict) else feed
    if not items:
        sys.exit(f"no items in {ITEMS} — run a sweep + export first")

    cache = load_json(CACHE, {})
    chosen = select(items, args, cache)
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
    # Why a run left work behind is the question every post-mortem asks, and the log could not
    # answer it: a bare "failed=113" says nothing about whether one venue broke or the provider
    # cut us off. Tallied by source and by cause so the next log names the culprit itself.
    trouble: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
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
        text, images = None, None
        try:
            text = extract_text(url)
        except Exception as e:
            # A scan is not an unreadable document, it is a document with no text layer. Render
            # the pages and read them, rather than reporting a gap we can actually close.
            scanned = "no extractable text" in str(e)
            if scanned and PROVIDER == "openai" and not args.dry_run:
                try:
                    images = render_pages(fetch_raw(url))
                except Exception as e2:
                    print(f"  skip  {iid}  {title}  — scan, and rendering failed: {e2}", flush=True)
                    failed += 1
                    trouble[it.get("source_id", "?")]["scan, render failed"] += 1
                    continue
            else:
                print(f"  skip  {iid}  {title}  — {e}"
                      + (" (scan; vision needs the openai provider)" if scanned else ""), flush=True)
                failed += 1
                trouble[it.get("source_id", "?")]["scan/extract"] += 1
                continue

        if args.dry_run and text is None:
            print(f"  scan  {iid}  {title}  — no text layer; would be read as page images", flush=True)
            done += 1
            continue
        if args.dry_run:
            print(f"\n===== {iid}  {title} =====")
            print(f"  url: {url}")
            print(f"  extracted {len(text)} chars; prompt preview:\n")
            print("  " + build_user_prompt(it, text)[:700].replace("\n", "\n  ") + " …")
            done += 1
            continue

        try:
            if images:
                brief = call_openai_vision(client, it, images)
                brief["read_as"] = "scan"     # provenance: read from page images, not a text layer
            else:
                brief = call_llm(client, it, text)
        except Exception as e:
            print(f"  fail  {iid}  {title}  — {e}", flush=True)
            failed += 1
            trouble[it.get("source_id", "?")]["model call"] += 1
            continue
        cache[iid] = {**brief, "model": MODEL, "provider": PROVIDER, "doc_hash": h,
                      "prompt_version": PROMPT_VERSION,
                      "generated_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")}
        write_cache(cache)   # checkpoint each success, atomically
        print(f"  ok   [{done + 1:>3}/{len(chosen)}] {iid}  {title}  [{brief['confidence']}]", flush=True)
        done += 1

    print(f"\n[brief] done={done} skipped(cached)={skipped} failed/no-text={failed}")
    if trouble:
        print("[brief] what did not get briefed, by source and cause:")
        for src, causes in sorted(trouble.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"         {sum(causes.values()):4d}  {src:26s} "
                  + ", ".join(f"{n} {c}" for c, n in causes.most_common()))
    if not args.dry_run:
        print(f"[brief] cache: {CACHE}  ({len(cache)} briefs) — rebuild the dashboard to embed them")


if __name__ == "__main__":
    main()
