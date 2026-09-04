#!/usr/bin/env python3
"""Act-text reference lane — quote what a cited provision actually says.

An instrument tells you it was made under s.56(2), or that it commences s.3(7). It does not
tell you what those provisions say; that is in the parent Act. This module fetches the Act,
indexes it by section and sub-section, and returns the provision VERBATIM with a citation, so
the dashboard can show the substance instead of a bare cross-reference.

Two rules govern this, and they are the reason the module exists in this shape:

1. NEVER from model memory. Statutory text is quoted from a fetched document or not at all. A
   plausible-sounding provision recalled by a language model is exactly the failure a firm
   cannot absorb, and nothing here asks a model anything.

2. Only from venues already on the coverage list. India Code is the natural home for Act texts
   and is deliberately NOT used: its Terms of Use bar "any automated means to access the
   Portal" without written permission, and its Copyright Policy bars "systematic extraction,
   scraping, or harvesting" and says API data "must not be cached, stored, or redistributed".
   That is this use case, described exactly. UIDAI was excluded from this tracker on the same
   reasoning; consistency matters more than convenience. Instead each Act is taken from the
   regulator that administers it — all of them already covered sources with completed legal
   analyses — and, where checked, byte-identical to the e-Gazette original.

Reproduction footing: s.52(1)(q)(i) of the Copyright Act 1957 makes reproduction of the text of
any Act non-infringing, which is why quoting a provision is on firmer ground than the fetch is.

Usage:
    python3 pipeline/acts.py --refresh              # fetch and cache every registered Act
    python3 pipeline/acts.py --show telecom_2023 3 7   # print s.3(7) with its citation
"""
from __future__ import annotations
import argparse, io, json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "act_text"

CONTACT = os.environ.get("TMT_RADAR_CONTACT") or "compliance@trilegal.com"
UA = {"User-Agent": "Mozilla/5.0 (compatible; TMTRegulatoryRadar/2.0; Trilegal internal regulatory monitoring)",
      "From": CONTACT}

# Each Act is sourced from the regulator that administers it — every one of these hosts is
# already on the coverage list with a completed access/copyright analysis, so this lane adds no
# new venue. `verified` records a check performed at discovery, not an assumption.
ACTS = {
    "telecom_2023": {
        "title": "Telecommunications Act, 2023 (44 of 2023)",
        "url": "https://www.trai.gov.in/sites/default/files/2024-09/Telecommunications_01012024.pdf",
        "host": "trai.gov.in",
        "coverage_source": "trai_* (covered)",
        "verified": "byte-identical to the e-Gazette original at "
                    "egazette.gov.in/WriteReadData/2023/250880.pdf (checked 2026-08-31)",
        "aliases": ["telecommunications act, 2023", "telecommunications act 2023",
                    "telecommunication act, 2023", "telecommunication act 2023"],
    },
}

# In the gazette typesetting the section HEADING is a marginal side-note printed away from the
# body, so extracted text reads "56. (1) The Central Government may ...", with "Authorisation."
# floating separately among running heads and cross-reference margins. Anchor on the numbered
# section opener and treat everything else as noise rather than trying to recover headings from
# the margin — a mislabelled provision is worse than an unlabelled one.
_SECTION = re.compile(r"(?:^|\n)\s*(\d{1,3}[A-Z]?)\.\s+(?=\(\d+\)|[A-Z(])", re.M)
_SUBSEC = re.compile(r"(?:^|\s)\((\d+)\)\s+", re.M)

# Page furniture that the PDF interleaves into the body text.
_NOISE = [
    re.compile(r"\n?\s*\d*\s*THE GAZETTE OF INDIA[^\n]*", re.I),
    re.compile(r"\n?\s*\[?PART II[^\n]*", re.I),
    re.compile(r"\n?\s*SEC\.\s*\d+\([a-z]+\)\]?", re.I),
    re.compile(r"\n\s*\d+ of \d{4}\.\s*"),          # marginal statute cross-references
    re.compile(r"\n\s*\d{1,3}\s*\n"),                 # bare page numbers
]


def _strip_furniture(text: str) -> str:
    for rx in _NOISE:
        text = rx.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def fetch_act_text(url: str) -> str:
    """Full text of an Act PDF. Deliberately not sharing brief.py's 18k cap — an Act is long and
    the provision we need is often far past that."""
    import requests
    from pypdf import PdfReader
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    if r.content[:5] != b"%PDF-":
        raise ValueError(f"not a PDF (content-type={r.headers.get('content-type')})")
    reader = PdfReader(io.BytesIO(r.content))
    pages = [(p.extract_text() or "") for p in reader.pages]
    # Join hard-wrapped lines: PDF extraction breaks sentences mid-clause, which would otherwise
    # leave every quoted provision full of stray newlines.
    text = "\n".join(pages)
    text = re.sub(r"(?<![.\n])\n(?![\s(\d])", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def index_sections(text: str) -> dict[str, dict]:
    """Map section number -> {body}. Body runs to the next numbered section opener."""
    text = _strip_furniture(text)
    out, marks = {}, list(_SECTION.finditer(text))
    for i, m in enumerate(marks):
        num = m.group(1)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        # The arrangement-of-sections table at the front repeats every number with a one-line
        # entry. Keeping the longest body per number discards the table and keeps the real text.
        if num not in out or len(body) > len(out[num]["body"]):
            out[num] = {"body": body}
    return out


def subsection_text(body: str, sub: str) -> str | None:
    """Verbatim text of sub-section (n) within a section body."""
    marks = list(_SUBSEC.finditer(body))
    for i, m in enumerate(marks):
        if m.group(1) != str(sub):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        return re.sub(r"\s+", " ", body[m.end():end]).strip()
    return None


def cache_path(slug: str) -> Path:
    return CACHE_DIR / f"{slug}.json"


def load_act(slug: str, refresh: bool = False) -> dict:
    p = cache_path(slug)
    if p.exists() and not refresh:
        return json.loads(p.read_text())
    meta = ACTS[slug]
    text = fetch_act_text(meta["url"])
    doc = {"slug": slug, "title": meta["title"], "url": meta["url"], "host": meta["host"],
           "verified": meta["verified"], "chars": len(text),
           "sections": index_sections(text)}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    os.replace(tmp, p)
    return doc


def resolve_act(name: str) -> str | None:
    """Match a free-text Act reference from an instrument to a registered Act."""
    n = re.sub(r"\s+", " ", (name or "")).strip().lower()
    for slug, meta in ACTS.items():
        if any(a in n for a in meta["aliases"]):
            return slug
    return None


# How instruments cite a provision, in the two shapes the gazette actually uses:
#   "Sub-section (7) of section 3 of the Telecommunications Act, 2023 (44 of 2023)"
#   "section 3(7) of the Telecommunications Act, 2023"
# Rule references ("Rule 11 of the ... Rules, 2026") are deliberately NOT matched: those live in
# subordinate legislation this lane does not hold, and guessing at them would invent text.
_REF_LONG = re.compile(
    r"Sub\s*-?\s*section\s*\((\d+)\)\s+of\s+section\s+(\d+[A-Z]?)\s+of\s+the\s+([^,;]+?Act,?\s*\d{4})",
    re.I)
_REF_SHORT = re.compile(
    r"\bsections?\s+(\d+[A-Z]?)\s*\((\d+)\)\s+of\s+the\s+([^,;]+?Act,?\s*\d{4})", re.I)
_REF_BARE = re.compile(
    r"\bsection\s+(\d+[A-Z]?)\s+of\s+the\s+([^,;]+?Act,?\s*\d{4})", re.I)


def parse_ref(text: str):
    """(act_name, section, sub) for the first provision reference in `text`, else None."""
    if not text:
        return None
    m = _REF_LONG.search(text)
    if m:
        return (m.group(3).strip(), m.group(2), m.group(1))
    m = _REF_SHORT.search(text)
    if m:
        return (m.group(3).strip(), m.group(1), m.group(2))
    m = _REF_BARE.search(text)
    if m:
        return (m.group(2).strip(), m.group(1), None)
    return None


def provision_for(text: str) -> dict | None:
    """Resolve a free-text citation straight to the quoted provision."""
    ref = parse_ref(text)
    return provision(*ref) if ref else None


def provision(act_name: str, section: str, sub: str | None = None) -> dict | None:
    """The provision, verbatim, with its citation — or None if we cannot quote it."""
    slug = resolve_act(act_name)
    if not slug:
        return None
    try:
        doc = load_act(slug)
    except Exception:
        return None
    sec = doc["sections"].get(str(section))
    if not sec:
        return None
    text = subsection_text(sec["body"], sub) if sub else re.sub(r"\s+", " ", sec["body"]).strip()
    if not text or len(text) < 20:
        return None
    return {"act": doc["title"],
            "ref": f"s.{section}({sub})" if sub else f"s.{section}",
            "text": text, "url": doc["url"], "host": doc["host"]}


def main():
    ap = argparse.ArgumentParser(description="Act-text reference lane.")
    ap.add_argument("--refresh", action="store_true", help="re-fetch every registered Act")
    ap.add_argument("--show", nargs="+", metavar=("SLUG", "SECTION"),
                    help="print a provision: --show telecom_2023 3 7")
    args = ap.parse_args()

    if args.refresh:
        for slug in ACTS:
            doc = load_act(slug, refresh=True)
            print(f"  {slug}: {len(doc['sections'])} sections, {doc['chars']:,} chars  <- {doc['host']}")
        return
    if args.show:
        slug, sec = args.show[0], args.show[1]
        sub = args.show[2] if len(args.show) > 2 else None
        p = provision(ACTS[slug]["title"], sec, sub)
        if not p:
            sys.exit("provision not found")
        print(f"{p['ref']} of the {p['act']}\n\n{p['text']}\n\nSource: {p['url']}")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
