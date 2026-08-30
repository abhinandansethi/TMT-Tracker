#!/usr/bin/env python3
"""Deterministic dashboard renderer, v2. Data files in -> single HTML out. No LLM, no network.

v2 over v1: three live strata (telecom / tech_data / media) plus the safety net, driven by
engine/registry_v2.json instead of registry/sources.json; per-source health from
engine/health.json; a stratum filter on the Instruments tab; Coverage grouped
stratum -> regulator -> venue. Signals is unchanged. Writes dist/tmt-radar-v2.html.
Visual system copied verbatim from build_dashboard.py (v1) — same type, same spacing.
"""
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)

ROOT = Path(__file__).resolve().parent.parent
DATA, DIST, ENGINE = ROOT / "data", ROOT / "dist", ROOT / "engine"
DIST.mkdir(exist_ok=True)

items = json.loads((DATA / "items.json").read_text())
signals = json.loads((DATA / "signals.json").read_text())
shelf = json.loads((DATA / "rules_shelf.json").read_text())
overrides = json.loads((DATA / "short_titles.json").read_text())
clients_path = ROOT / "pipeline" / "clients.json"
default_clients = json.loads(clients_path.read_text()).get("clients", []) if clients_path.exists() else []
# LLM briefs (optional): pipeline/brief.py reads each instrument's PDF and writes a substantive
# brief here, keyed by item id. Absent by default; when present, the Clients tab shows it in
# place of the deterministic metadata brief. Purely additive — a missing cache changes nothing.
brief_cache_path = ROOT / "pipeline" / "brief_cache.json"
brief_cache = json.loads(brief_cache_path.read_text()) if brief_cache_path.exists() else {}
lines_map = json.loads((DATA / "row_lines.json").read_text())
folds_map = json.loads((DATA / "folds.json").read_text())

registry_v1 = json.loads((ROOT / "registry" / "sources.json").read_text())   # v1: blind-spot ledger only
registry = json.loads((ENGINE / "registry_v2.json").read_text())             # v2: the live engine registry

# health.json is written by the sweep; a fresh checkout may not have one yet.
health_path = ENGINE / "health.json"
health_doc: dict[str, Any] = json.loads(health_path.read_text()) if health_path.exists() else {}
health: dict[str, Any] = health_doc.get("sources", {})

# The page's freshness claims are anchored to the LAST SWEEP, never to the build. Rebuilding
# without sweeping must not reset the staleness banner: "Last updated" is a claim about the
# data, and the data is only as fresh as the last time a source was actually checked.
try:
    SWEPT = datetime.fromisoformat(health_doc["generated"])
except Exception:
    SWEPT = None

STRATA = ("telecom", "tech_data", "media", "safety_net")
STRATUM_LABEL = {"telecom": "Telecom", "tech_data": "Technology and data",
                 "media": "Media", "safety_net": "Safety net"}
STRATUM_FILTER_LABEL = {"telecom": "Telecom", "tech_data": "Tech & data", "media": "Media"}

# ---- deterministic display-title shortener ----
# A crisp heading tells a partner at a glance what the instrument pertains to. That means
# stripping the bureaucratic lead-in ("Notification of the...", "Consultation Paper on...")
# and the statutory tail ("under sub-section (2) of section 56 of the ... Act, 2023") so the
# subject itself leads. Regex-driven rather than a hand list, so it generalises. Runtime stays
# deterministic; short_titles.json overrides still win for anything hand-curated.
_LEAD = re.compile(
    r"^(?:"
    r"notice for stakeholder consultation on (?:draft for comments\s*[–-]\s*)?(?:test cases for\s+)?"
    r"|draft generic test cases for "
    r"|(?:pre[\s-]?)?consultation paper on (?:draft amendments? (?:in|to) the\s+)?"
    r"|draft (?:amendments? (?:in|to) the\s+)?"
    r"|direction (?:on|regarding|to) (?:allocation and operationalization of\s+)?"
    r"|recommendations? on (?:issues (?:related to|relating to)\s+)?"
    r"|order (?:regarding|dated|on|in the matter of) "
    r"|publication of (?:revised |new )?"
    r"|notification (?:of the |of |regarding |for the |for )?(?:enforcement of\s+)?"
    r"|notification to be published[^,]*?(?:regarding|of|for) "
    r"|instructions? to be specified[^,]*?accordance with the\s+"
    r"|trai (?:releases?|issues?|initiates?|hosts?|assesses?) (?:an?\s+amended\s+|clarifications? regarding\s+)?"
    r"|nccs (?:designates?|has designated) "
    r"|press release (?:on|regarding) "
    r"|in the matter of "
    r"|clarification (?:regarding|on) "
    r")", re.I)
_TAIL = re.compile(
    r"(?:"
    r",?\s+under (?:sub-?section|section|rule|clause|the provisions)\b.*$"
    r"|,?\s+in accordance with\b.*$"
    r"|,?\s+pursuant to\b.*$"
    r"|,?\s+in pursuance of\b.*$"
    r"|\s+for (?:service and transactional|entities in sectors other)\b.*$"
    r"|\s+by (?:entities|access providers) in\b.*$"
    r")", re.I)


def shorten(t: str, cap: int = 66) -> str:
    s = re.sub(r"\s+", " ", t or "").strip().rstrip(".")
    s = _LEAD.sub("", s, count=1).strip()
    s = _TAIL.sub("", s).strip(" ,;:—-")
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    if not s:
        s = re.sub(r"\s+", " ", (t or "")).strip()
    if len(s) <= cap:
        return s
    return s[:cap].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def venue_label(reg: str, name: str) -> str:
    reg = reg.split("/")[-1].strip()
    n = name.replace(" — ", " ").replace(" – ", " ").split("/")[0].split(" (")[0].strip()
    if n.lower().startswith(reg.lower()):
        n = n[len(reg):].strip()
    lab = f"{reg} {n}".strip()
    return lab if len(lab) <= 34 else lab[:34].rsplit(" ", 1)[0]


# Venue labels: v1 ids first, then v2 overrides, so ids retired from the engine
# (dot_eservices_home, trai_standing_directions) still resolve for baseline rows.
VENUE = {s["id"]: venue_label(s["regulator"], s["name"]) for s in registry_v1["sources"]}
VENUE.update({s["id"]: venue_label(s["regulator"], s["name"]) for s in registry["sources"]})
VENUE["gazette_crosscheck"] = "e-Gazette cross-check"
VENUE["tdsat_orders"] = "TDSAT"
VENUE["gazette_communications"] = "e-Gazette · Communications"
VENUE["gazette_meity"] = "e-Gazette · MeitY"
VENUE["gazette_mib"] = "e-Gazette · MIB"

V2_BY_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in registry["sources"]}

# PIB and the e-Gazette are cross-cutting announcement lanes: they carry instruments from
# every stratum, so the lane's own stratum must not decide an item's. For those, the
# item's regulator does. REG_STRATUM is derived from the registry, never hardcoded.
LANE_REGULATORS = {"PIB", "e-Gazette"}
REG_STRATUM: dict[str, str] = {s["regulator"]: s["stratum"] for s in registry["sources"]
                               if s["regulator"] not in LANE_REGULATORS}
LANE_IDS = {s["id"] for s in registry["sources"] if s["regulator"] in LANE_REGULATORS}
LANE_IDS.add("gazette_crosscheck")  # v1 baseline lane id, retired from the engine registry

STRATUM_RULE = ("stratum = item.stratum if set, else the registry_v2 stratum of its source, "
                "except for cross-cutting lanes (PIB, e-Gazette) where the item's regulator "
                "decides via the registry regulator->stratum map; else telecom.")


def row_stratum(it: dict[str, Any]) -> str:
    s = it.get("stratum")
    if s in STRATA:
        return s
    src = V2_BY_ID.get(it["source_id"])
    if src and src["id"] not in LANE_IDS and src["stratum"] != "safety_net":
        return src["stratum"]
    return REG_STRATUM.get(it["regulator"], "telecom")


DASH = re.compile(r"\s+[—–]\s+")


def clean(t: Optional[str]) -> str:
    """Deterministic prose cleanup for display: dashes used as punctuation become commas."""
    return DASH.sub(", ", (t or "").strip())


def first_sentence(t: Optional[str], cap: int = 110) -> str:
    """Fallback description line: first sentence of the gist, capped on a word boundary."""
    t = (t or "").strip()
    if not t:
        return ""
    for i, ch in enumerate(t):
        if ch in ".;" and i > 30:
            t = t[:i + 1]
            break
    return t if len(t) <= cap else t[:cap].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(iso: Optional[str]) -> str:
    if not iso or len(str(iso)) < 10:
        return ""
    y, m, d = str(iso)[:10].split("-")
    return f"{int(d)} {_MON[int(m) - 1]} {y}"


def _short_rule(rule: str) -> str:
    """'Sub-section (2) of section 56 of the Telecommunications Act, 2023 (44 of 2023)'
    -> 's.56, Telecommunications Act 2023' — the citation a lawyer scans for."""
    r = re.sub(r"\s+", " ", rule or "")
    sec = re.search(r"section\s+(\d+[A-Z]?)", r, re.I)
    act = re.search(r"of the ([A-Z][^,]+? Act,? \d{4})", r)
    bits = []
    if sec:
        bits.append("s." + sec.group(1))
    if act:
        bits.append(re.sub(r",? (\d{4})$", r" \1", act.group(1).strip()))
    return ", ".join(bits) or (r[:48] + ("…" if len(r) > 48 else ""))


def descriptor(it: dict[str, Any]) -> str:
    """Deterministic one-line 'what it pertains to / what to do'. Built from the item's own
    metadata — an effective date, a comment deadline, the section it amends — never a model.
    The short title says what the instrument IS; this says why a partner should care now."""
    meta = it.get("meta") or {}
    typ = (it.get("type") or "").lower()
    dl = it.get("deadline")
    today = NOW.strftime("%Y-%m-%d")
    bits: list[str] = []
    # 1. an open comment window is the most actionable fact
    if dl and str(dl) >= today and ("consult" in typ or "draft" in typ):
        bits.append("Comments due " + fmt_date(dl))
    # 2. a gazette notification: when it bites, what it touches, whether action is flagged
    if meta.get("gazette_id"):
        if meta.get("effective_date") and not bits:
            bits.append("In force " + fmt_date(meta["effective_date"]))
        if meta.get("impacted_rule"):
            bits.append("amends " + _short_rule(meta["impacted_rule"]))
        if str(meta.get("impact", "")).lower().startswith("action"):
            bits.append("action required")
    # 3. a tribunal/court order: name the parties (the title is the case number)
    elif it.get("lane") == "judgments" and meta.get("parties"):
        p = clean(meta["parties"]).title()
        bits.append(p[:70] + ("…" if len(p) > 70 else ""))
    # 4. otherwise a plain type + effective/issue framing
    if not bits:
        lab = TYPE_LABEL.get(it.get("type", ""), (it.get("type", "") or "").replace("_", " ").title())
        when = fmt_date(it.get("date"))
        bits.append(f"{lab}{(' · ' + when) if when else ''}")
    return " · ".join(bits)


MEMOS = {"d90090c2ea": "2026-08-10_TRAI_1601-series_client-alert_SAMPLE.docx"}
TYPE_LABEL = {"consultation_notice": "Consultation", "consultation_paper": "Consultation",
  "draft_test_cases": "Draft", "draft_rules": "Draft rules", "exemption_circular": "Circular",
  "tstl_designation": "Designation", "court_notice": "Notice", "event_notice": "Notice",
  "press_release": "Release", "pib_release": "Release", "administrative": "Administrative",
  "clarification": "Clarification", "notification": "Notification", "circular": "Circular",
  "rules": "Rules", "direction": "Direction", "recommendation": "Recommendation", "manual": "Manual",
  "order": "Order", "do_letter": "Letter", "policy": "Policy", "other": "Other"}

def build_row(it: dict[str, Any]) -> dict[str, Any]:
    meta = it.get("meta") or {}
    # a gazette entry has no direct PDF (its citation is the permanent Gazette ID), so the
    # "document" link is the landing page and the id is shown as the reference
    doc = it.get("doc_url") or it.get("pdf_url")
    return {
        # date may be null: several media/tech_data listings publish undated rows. Kept in
        # the ledger, rendered as "—", and excluded from every date bucket.
        "id": it["id"], "date": it.get("date") or None, "reg": it["regulator"],
        "routine": it.get("routine", False), "lane": it.get("lane", "instruments"),
        "stratum": row_stratum(it),
        "type": TYPE_LABEL.get(it.get("type", ""), (it.get("type", "") or "").replace("_", " ").title()),
        # short + line are precomputed in the export by radar.display (one source of truth);
        # a hand-curated override still wins over the deterministic value
        "short": overrides.get(it["id"]) or it.get("short") or shorten(it["title"]),
        "official": it["title"], "gist": clean(it.get("gist") or ""),
        "line": lines_map.get(it["id"]) or first_sentence(clean(it.get("gist") or "")) or it.get("line") or descriptor(it),
        "venue": VENUE.get(it["source_id"], it["source_id"]),
        # dual links: the document itself and the official landing page
        "doc": doc, "page": it.get("page_url"),
        "gid": meta.get("gazette_id"), "impact": meta.get("impact"),
        "effective": meta.get("effective_date"), "rule": meta.get("impacted_rule"),
        "pr": it.get("announced_by_pr"),
        "deadline": it.get("deadline"), "flags": it.get("flags", []),
        "memo": MEMOS.get(it["id"]),
        # LLM brief of the document body, if pipeline/brief.py has produced one for this item
        "llm": brief_cache.get(it["id"]),
        # source_id: which coverage link this item was scraped from (the audit tab's join key)
        "src": it["source_id"],
    }


all_rows = [build_row(it) for it in items["items"]]
# route by lane: instruments and judgments render in separate tabs; signals stay out of the
# instruments ledger (they are leads and announcements, shown in the Signals tab)
rows = [r for r in all_rows if r["lane"] == "instruments"]
judgment_rows = [r for r in all_rows if r["lane"] == "judgments"]

# ---- fold announcement documents into the instrument they announce ----
# Same rule already used for TRAI press releases: a notice that merely announces a
# document is not a separate ledger row. Deterministic match on regulator + date +
# the parenthesised subject acronym. Nothing is lost: the notice PDF is linked from
# the surviving row.
ACRO = re.compile(r"\(([A-Z]{2,6})\)")


def fold_notices(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    by_id = {r["id"]: r for r in rows}
    # explicit folds first
    for ann, inst in folds_map.items():
        if ann.startswith("_"):
            continue
        a, i = by_id.get(ann), by_id.get(inst)
        if a and i:
            i["notice"] = a.get("pdf") or a.get("page")
            a["_folded_into"] = inst
    idx: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        m = ACRO.search(r["official"])
        # Undated rows are never auto-folded: regulator+acronym alone is too weak a key
        # without a date, and a wrong fold silently deletes a row from the ledger.
        if m and r["date"] and r["type"] in ("Consultation", "Draft"):
            idx.setdefault((r["reg"], r["date"], m.group(1)), {})[r["type"]] = r
    folded = 0
    for pair in idx.values():
        notice, draft = pair.get("Consultation"), pair.get("Draft")
        if notice and draft and "notice" in notice["official"].lower():
            draft["notice"] = notice.get("pdf") or notice.get("page")
            notice["_folded_into"] = draft["id"]
            folded += 1
    kept = [r for r in rows if "_folded_into" not in r]
    return kept, folded


rows, folded_n = fold_notices(rows)

# Newest instrument first: what changed this week is what a partner opens this page for.
# Undated shelf rows carry no timeline position, so they sit after every dated row rather
# than sorting to either end of it.
_dated = [r for r in rows if r["date"]]
_undated = [r for r in rows if not r["date"]]
_dated.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
rows = _dated + _undated
undated_n = len(_undated)

# ---- coverage: live sources grouped stratum -> regulator -> venue, with health ----
# Green is earned, never assumed. Only a source that has actually produced or confirmed an
# instrument shows OK; a venue we are getting nothing from must never look the same as one
# that is working, and "quiet" has to be provable from the newest date the venue displays.
HEALTH_UI = {
    "OK": ("OK", "ok"),
    "QUIET": ("Quiet", "quiet"),
    "FILTERED": ("Out of scope", "quiet"),
    "WARN": ("Warn", "warn"),
    "EMPTY": ("Nothing", "bad"),
    "FAILED": ("Failed", "bad"),
}


_EGZ_NOTE = ("The fetch target is the ministry-search endpoint behind this portal; it is "
             "session-based, so a cold deep-link lands on the portal's error page. This link "
             "opens the portal entry — search the ministry there to replicate the sweep.")


def display_url(u: str) -> str:
    return "https://egazette.gov.in/" if "egazette.gov.in/Search" in (u or "") else u


def display_note(u: str):
    return _EGZ_NOTE if "egazette.gov.in/Search" in (u or "") else None


def venue_name(s: dict[str, Any]) -> str:
    vn = clean(s["name"]).split(" (")[0]
    reg = s["regulator"]
    if vn.lower().startswith(reg.lower()):
        vn = vn[len(reg):].strip(" ,")
    return (vn[:1].upper() + vn[1:]) if vn else s["name"]


strata_groups: list[dict[str, Any]] = []
stratum_venue_counts: dict[str, int] = {}
health_tally: dict[str, int] = {"ok": 0, "quiet": 0, "warn": 0, "bad": 0, "pending": 0}

for st in STRATA:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for s in registry["sources"]:
        if s["stratum"] != st or s.get("status") != "live":
            continue
        reg = s["regulator"]
        if reg not in groups:
            groups[reg] = []; order.append(reg)
        h = health.get(s["id"])
        # No health entry means the sweep has not reached this source yet. Showing it as
        # OK would be a false freshness claim, so it renders as its own "pending" state.
        label, cls = HEALTH_UI.get((h or {}).get("status", ""), ("Pending", "pending"))
        if h is None:
            label, cls = "Pending", "pending"
        health_tally[cls] += 1
        notes = list((h or {}).get("notes") or [])
        info = list((h or {}).get("info") or [])
        groups[reg].append({
            "n": venue_name(s), "id": s["id"], "url": display_url(s.get("url", "")),
            "method": s.get("method", ""), "role": s.get("role", ""),
            "s": label, "st": cls,
            "rows": (h or {}).get("rows_seen"), "new": (h or {}).get("new"),
            "checked": ((h or {}).get("checked") or "")[:16].replace("T", " "),
            "fails": (h or {}).get("consecutive_failures", 0),
            # the proof that a quiet venue is alive rather than broken
            "newest": (h or {}).get("newest_visible"),
            "held": (h or {}).get("ledgered_total"),
            "lane": s.get("lane", "instruments"),
            "notes": notes, "info": info + ([display_note(s.get("url", ""))] if display_note(s.get("url", "")) else []),
        })
    if not order:
        continue
    n_ven = sum(len(groups[r]) for r in order)
    stratum_venue_counts[st] = n_ven
    strata_groups.append({"key": st, "label": STRATUM_LABEL[st], "venues": n_ven,
                          "regs": len(order),
                          "groups": [{"reg": r, "venues": groups[r]} for r in order]})

live_count = sum(stratum_venue_counts.values())
reg_count = len({s["regulator"] for s in registry["sources"] if s.get("status") == "live"})

# ---- blind spots: v1 sources still not covered by a live v2 adapter ----
V2_LIVE_IDS = {s["id"] for s in registry["sources"] if s.get("status") == "live"}


def blind_label(s: dict[str, Any]) -> str:
    n = clean(s["name"]).split(" (")[0]
    reg = s["regulator"].split("/")[-1].strip()
    acro = "".join(w[0] for w in s["regulator"].replace("/", " ").split() if w[:1].isupper())
    if n.lower().startswith(reg.lower()) or (len(acro) > 1 and acro in n):
        return n
    return f"{reg} {n}"


# Which live lane picks up each blind source. Encoded from the mitigation already written
# into each v1 source's "quirks" field — the registry states it in prose, not as a field.
# Only lanes that are LIVE may be named as covering. PIB sits at needs_decision and the DoT
# eServices adapter is planned — neither has ever swept, so neither earns a mention as cover.
COVERED_BY = {
    "dot_main": "e-Gazette MoC lane + curated signals (PIB planned, not yet covering)",
    "wpc_legacy": "No live lane (DoT eServices adapter planned)",
    "saralsanchar": "No live lane (DoT eServices adapter planned)",
    "tdsat_judgments": "No live lane (phase 2)",
    "dpb_watch": "MeitY + e-Gazette lanes (PIB planned, not yet covering)",
    "ogai_watch": "MeitY + curated signals",
    "sansad_bills": "MeitY/MIB consultation lanes (PIB planned, not yet covering)",
    "indiacode": "Reference only, not a net",
}

blind = [{"n": blind_label(s), "r": s.get("blind_reason", s["status"]),
          "c": COVERED_BY.get(s["id"], "Unassigned"), "q": clean(s.get("quirks", "")),
          "st": STRATUM_LABEL.get(s.get("stratum", ""), s.get("stratum", ""))}
         for s in registry_v1["sources"]
         if s["status"] in ("blocked", "watch", "supplement") and s["id"] not in V2_LIVE_IDS]

# v2 registry.excluded entries marked "planned" are cleared-but-not-yet-built venues (e.g.
# NCLAT: legally cleared, listing frozen at 2021, needs a date-filter driver). Name them on
# the coverage page so the "whole world" is complete rather than silently short.
for x in registry.get("excluded", []):
    if x.get("status") != "planned" or x.get("id") in V2_LIVE_IDS:
        continue
    reg_ = (x.get("regulator") or "").strip()
    nm = x.get("name") or (x.get("id") or "").replace("_", " ").title()
    nm = f"{reg_} {nm}".strip() if reg_ and not nm.lower().startswith(reg_.lower()) else nm
    blind.append({"n": nm, "r": "PLANNED — cleared, adapter pending",
                  "c": "No live lane (planned)", "q": clean(x.get("reason", "")),
                  "st": STRATUM_LABEL.get(x.get("stratum", ""), "Technology and data")})

# Not yet live: v1 planned venues the v2 engine has not shipped an adapter for.
# Counted from the live v2 registry, not the retired v1 one: every v2 source that exists
# but is not sweeping (planned / watch / needs_decision), grouped by stratum.
planned: dict[str, int] = {}
for s_ in registry["sources"]:
    if s_.get("status") != "live":
        planned[s_["stratum"]] = planned.get(s_["stratum"], 0) + 1
notlive = [f'{STRATUM_LABEL.get(k, k)}, {v} venue{"s" if v != 1 else ""}'
           for k, v in sorted(planned.items())]

stratum_counts = {st: {"rows": sum(1 for r in rows if r["stratum"] == st),
                       "venues": stratum_venue_counts.get(st, 0)}
                  for st in STRATA}

# ---- audit: the link-wise scraped-document ledger for human verification ----
# For every live source — including the ones that yielded nothing — the exact URL the engine
# fetches and every document scraped from it, across all three lanes. A human auditor opens
# the live listing next to this list and checks nothing was missed. Zero-yield sources are
# listed deliberately: silence has to be auditable, not hidden.
items_by_src: dict[str, list[dict[str, Any]]] = {}
for it in items["items"]:
    items_by_src.setdefault(it["source_id"], []).append(it)

audit_groups: list[dict[str, Any]] = []
_audit_seen_ids: set[str] = set()
for st in STRATA:
    src_entries = []
    for s in registry["sources"]:
        if s["stratum"] != st or s.get("status") != "live":
            continue
        h = health.get(s["id"]) or {}
        label, cls = HEALTH_UI.get(h.get("status", ""), ("Pending", "pending"))
        got = sorted(items_by_src.get(s["id"], []),
                     key=lambda i: (i.get("date") or ""), reverse=True)
        _audit_seen_ids.add(s["id"])
        src_entries.append({
            "id": s["id"], "n": venue_name(s), "reg": s["regulator"],
            "url": display_url(s.get("url", "")), "method": s.get("method", ""),
            "note": display_note(s.get("url", "")),
            "s": label, "st": cls,
            "checked": (h.get("checked") or "")[:16].replace("T", " "),
            "newest": h.get("newest_visible"), "rows": h.get("rows_seen"),
            "items": [{
                "d": i.get("date"),
                "t": i.get("short") or i.get("title"),
                # the venue lists official titles, so the auditor compares on this
                "o": i["title"],
                "doc": i.get("doc_url") or i.get("pdf_url"), "page": i.get("page_url"),
                "lane": i.get("lane", "instruments"),
            } for i in got],
        })
    if src_entries:
        audit_groups.append({"key": st, "label": STRATUM_LABEL[st], "sources": src_entries})

# Invariant check, not decoration: every ledgered item must belong to a live coverage source.
# An item from a source that is no longer on the coverage list is a boundary violation.
_orphaned = {sid for sid in items_by_src if sid not in _audit_seen_ids}
if _orphaned:
    raise SystemExit(f"audit invariant broken: items from non-live sources {sorted(_orphaned)}")

# The hosted-run URL is derived from the repo's own git remote, so a build made on the firm
# machine carries it too — otherwise a locally-built page ships with the button disarmed and
# silently loses the one-click refresh the hosted setup provides.
def _actions_url() -> Optional[str]:
    # Resolved for whichever environment is building: an explicit override, Vercel's own git
    # metadata (its build checkout may have no usable git remote), else the local remote.
    if os.environ.get("TMT_ACTIONS_URL"):
        return os.environ["TMT_ACTIONS_URL"]
    owner, slug = os.environ.get("VERCEL_GIT_REPO_OWNER"), os.environ.get("VERCEL_GIT_REPO_SLUG")
    if owner and slug:
        return f"https://github.com/{owner}/{slug}/actions/workflows/sweep.yml"
    try:
        import subprocess
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/]+([^/]+/[^/.]+)", url)
    return f"https://github.com/{m.group(1)}/actions/workflows/sweep.yml" if m else None


ACTIONS_URL = _actions_url()

payload: dict[str, Any] = {
    # Real build time. Never hardcode this: a stated sweep time that did not happen
    # is a false claim about how fresh the ledger is.
    "updated": (SWEPT or NOW).strftime("%d %b %Y, %H:%M IST"),
    # The page computes its own age from this at open time. A dashboard that has quietly
    # stopped being swept must announce it rather than look identical to a fresh one.
    "updatedISO": (SWEPT or NOW).isoformat(timespec="seconds"),
    "builtISO": NOW.isoformat(timespec="seconds"),
    "staleAfterHours": 26,
    "today": (SWEPT or NOW).strftime("%Y-%m-%d"),
    "clients": default_clients,
    "rows": rows,
    "judgments": sorted(judgment_rows, key=lambda r: (r.get("date") or ""), reverse=True),
    "signals": signals,
    # Update-now wiring. The published page cannot itself reach gov.in (sandbox), so the
    # button triggers the partner's pipeline endpoint if one is configured, else opens the
    # local operator console which runs the real sweep. Both are overridable at deploy time
    # via window.TMT_CONFIG = {pipelineEndpoint, consoleUrl}.
    "updateConfig": {"pipelineEndpoint": os.environ.get("TMT_PIPELINE_ENDPOINT", "/api/sweep"),
                     "consoleUrl": "http://127.0.0.1:8787",
                     # Fallback lane when the trigger endpoint is absent or unconfigured:
                     # the GitHub "Run workflow" page, one authenticated click.
                     "actionsUrl": ACTIONS_URL},
    "strataOrder": [{"key": st, "label": STRATUM_FILTER_LABEL[st]}
                    for st in ("telecom", "tech_data", "media")],
    "stratum_counts": stratum_counts,
    "coverage": {"strata": strata_groups, "live": live_count, "regs": reg_count,
                 "blind": blind, "notlive": notlive,
                 "healthAt": (health_doc.get("generated") or "")[:16].replace("T", " "),
                 "tally": health_tally},
    "audit": audit_groups,
    "shelfCount": len(shelf),
    # --- pipeline state: not rendered, read by the scheduled sweep so a fresh
    # session is fully self-contained (source list, adapter configs, gates, shelf). ---
    "pipeline": {
        "registry": registry["sources"],
        "registry_version": registry.get("schema_version"),
        "window_start": registry.get("window_start"),
        "classification": registry["classification"],
        "validation_gates": registry["validation_gates"],
        "health": health,
        "shelf": shelf,
        "short_titles": {k: v for k, v in overrides.items() if not k.startswith("_")},
        "row_lines": {k: v for k, v in lines_map.items() if not k.startswith("_")},
        "folds": {k: v for k, v in folds_map.items() if not k.startswith("_")},
        "stratum_rule": STRATUM_RULE,
        "shortener": {"lead_pattern": _LEAD.pattern, "tail_pattern": _TAIL.pattern, "cap": 66,
                      "note": "short = short_titles[id] if present, else strip a boilerplate lead-in (Notification of / Consultation Paper on / Direction on / ...) and a statutory tail (under section ... / in accordance with ...), cap 66 chars on a word boundary. line = row_lines[id] if present, else a deterministic descriptor from the item's own metadata. No runtime model judgment."},
    },
}
data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

TEMPLATE = r"""<meta charset="utf-8">
<title>TMT Regulatory Radar</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&family=Spectral:wght@300;400;500&display=swap">
<style>
:root{
  --navy:#003F62; --navy-d:#002B43; --navy-l:#1B6288; --navy-wash:#EAF0F4;
  --ochre:#AA8918; --ochre-wash:#F7F0DC; --alarm:#8A2B1C; --alarm-wash:#F7E9E6;
  --ok:#1B6B4A; --ok-wash:#E6F1EB;
  --ink:#111315; --mute:#4E555A; --faint:#7C848A; --ghost:#8F9396; --off:#A3A6A8;
  --paper:#FFFFFF; --ground:#E4E8EA; --panel:#F4F6F7; --panel2:#F4F6F7; --row3:#FAFBFB;
  --rule:#C9D1D6; --rule2:#DDE3E7; --rule3:#EBEFF1;
  --serif:Spectral,Georgia,'Times New Roman',serif;
  --sans:'IBM Plex Sans',system-ui,-apple-system,Segoe UI,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --grid:104px 78px minmax(0,1fr) 132px 24px;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--ground)}
body{font-family:var(--sans);color:var(--ink);-webkit-font-smoothing:antialiased}
a{color:var(--ink);text-decoration:none}
a:hover{color:var(--navy);text-decoration:underline;text-underline-offset:3px}
a:focus-visible,[tabindex]:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--navy);outline-offset:2px}
input,select,button{font-family:inherit}
input:focus,select:focus{outline:none}
input::placeholder{color:var(--ghost)}
.sheet{max-width:1440px;margin:0 auto;background:var(--paper);min-height:100vh}
.pad{padding:0 64px}

/* header */
.head{background:var(--navy);color:#fff;padding:26px 64px 22px;display:flex;align-items:baseline;
  justify-content:space-between;gap:24px;flex-wrap:wrap}
.wordmark{font-family:var(--serif);font-weight:400;font-size:29px;letter-spacing:.005em;color:#fff}
.wordmark b{font-weight:600}
.updbar{display:flex;align-items:center;gap:16px}
.updated{font-family:var(--mono);font-size:11px;letter-spacing:.05em;color:#B9CEDC}
.updated span{color:#fff;font-weight:500}
#updnow{appearance:none;cursor:pointer;font-family:var(--mono);font-size:10.5px;font-weight:600;
  letter-spacing:.1em;text-transform:uppercase;color:#fff;background:var(--ochre);
  border:0;padding:7px 14px;transition:opacity .12s}
#updnow:hover{opacity:.85}
#updnow:disabled{opacity:.5;cursor:progress}
#updnow:focus-visible{outline:2px solid #fff;outline-offset:2px}
.upd-note{display:none;background:var(--navy-wash);color:var(--navy);padding:9px 64px;
  font-family:var(--mono);font-size:11px;letter-spacing:.03em;border-bottom:1px solid var(--rule2)}
.upd-note.on{display:block}
.upd-note a{color:var(--navy);text-decoration:underline;text-underline-offset:2px}
.upd-note code{font-family:var(--mono);font-size:11px;background:#fff;border:1px solid var(--rule2);border-radius:3px;padding:1px 5px}
@media (max-width:760px){.upd-note{padding-left:22px;padding-right:22px}}
.thead .r.jr,.row .line.jr{grid-template-columns:104px 128px minmax(0,1fr) 120px 24px}
.tabs{background:var(--navy-d);padding:0 64px;display:flex;gap:2px}
.tabs button{appearance:none;background:none;border:0;border-bottom:3px solid transparent;
  padding:13px 18px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;
  color:#93B2C6;cursor:pointer}
.tabs button.on{color:#fff;border-bottom-color:var(--ochre);background:rgba(255,255,255,.06)}
.tabs button:hover{color:#fff}
.view{display:none;padding:0 64px 90px}
.view.on{display:block}
.cl-eyebrow{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--navy);margin-bottom:5px}
.cl-head{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin:2px 0 20px}
.cl-sub{font-size:13px;color:var(--mute);max-width:640px;line-height:1.5}
.cl-headbtns{white-space:nowrap}
.cl-add,.cl-reset{appearance:none;cursor:pointer;font-family:var(--mono);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.12em;border-radius:3px;padding:8px 14px}
.cl-add{background:var(--ochre);border:1px solid var(--ochre);color:#1a1206}
.cl-reset{margin-left:8px;border:1px solid var(--rule2);color:var(--mute);background:#fff}
.cl-card{border:1px solid var(--rule2);border-radius:9px;padding:16px 20px;margin:12px 0}
.cl-top{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.cl-name{font-family:var(--serif);font-size:19px;color:var(--ink)}
.cl-sec{font-size:12px;color:var(--mute);margin-left:6px}
.cl-act{display:flex;align-items:center;gap:8px;white-space:nowrap}
.cl-count{font-family:var(--mono);font-size:11px;color:var(--ochre);margin-right:4px}
.cl-btn{appearance:none;cursor:pointer;font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;border:1px solid var(--rule2);background:#fff;color:var(--navy);padding:4px 9px;border-radius:3px}
.cl-btn:hover{border-color:var(--navy)}
.cl-del{color:var(--alarm);border-color:transparent;font-size:15px;padding:0 6px;letter-spacing:0}
.cl-matches{display:none;margin-top:14px;border-top:1px solid var(--rule2);padding-top:12px}
.cl-matches.on{display:block}
.cl-matches ul{list-style:none;margin:0;padding:0}
.cl-matches li{padding:9px 0;border-top:1px solid #eef2f4}
.cl-matches li:first-child{border-top:none}
.cl-matches a{color:var(--navy);text-decoration:none;font-weight:600;font-size:14px}
.cl-matches a:hover{text-decoration:underline}
.cl-m{font-size:12px;color:#44555d;margin-top:2px}
.cl-why{font-size:11px;color:var(--faint);font-style:italic;margin-top:2px}
.cl-itop{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.cl-badge{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.09em;padding:2px 7px;border-radius:20px;white-space:nowrap}
.b-notify{background:#E9F3EA;color:#276B2E;border:1px solid #B9DCBD}
.b-review{background:var(--ochre-wash);color:#7A5E0E;border:1px solid #E4D19A}
.b-monitor{background:#EAF0F4;color:#1B6288;border:1px solid #BFD3E0}
.b-fyi{background:#F1F3F4;color:#7A868D;border:1px solid #E2E8EC}
.cl-brief{font-size:12.5px;color:#37474f;line-height:1.5;margin-top:4px;max-width:760px}
.cl-matwhy{color:var(--mute);font-style:normal}
.cl-idraft{margin-top:8px;appearance:none;cursor:pointer;font-family:var(--mono);font-size:9.5px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;background:#fff;color:var(--navy);border:1px solid var(--navy);padding:5px 12px;border-radius:3px}
.cl-idraft:hover{background:var(--navy);color:#fff}
li.mat-notify{border-left:2px solid #3E9C48;padding-left:12px;margin-left:-14px}
.cl-so{color:#37474f}
.cl-so::before{content:"→ ";color:var(--mute)}
.cl-ai{display:inline-block;font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:#7A5E0E;background:var(--ochre-wash);border:1px solid #E4D19A;border-radius:20px;padding:1px 7px;margin-left:4px;white-space:nowrap}
/* client card: advice scope + honest coverage-gap note */
.cl-scope{font-size:12px;color:#37474f;margin-top:6px;max-width:820px;line-height:1.5}
.cl-scope b{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--navy);font-weight:600;margin-right:6px}
.cl-gap{font-size:11.5px;color:#7A5E0E;background:var(--ochre-wash);border:1px solid #E4D19A;border-radius:3px;padding:6px 10px;margin-top:8px;max-width:820px;line-height:1.5}
/* coverage venue card: the exact fetched URL, always visible */
.ven .vlink{grid-column:1/-1;font-family:var(--mono);font-size:10px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ven .vlink a{color:var(--mute);text-decoration:none;border-bottom:1px dotted var(--rule2)}
.ven .vlink a:hover{color:var(--navy)}
/* instruments detail: What changed note */
.note.wc .body{max-width:900px}
.note.wc .ai{margin-top:6px;color:#37474f}
.aitag{display:inline-block;font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:#7A5E0E;background:var(--ochre-wash);border:1px solid #E4D19A;border-radius:20px;padding:1px 7px;margin-left:4px;white-space:nowrap}
/* audit tab: per-link scraped-document ledger */
.asrc{border:1px solid var(--rule2);border-radius:4px;background:#fff;margin-top:10px}
.asrc .ahead{display:grid;grid-template-columns:14px minmax(180px,1.1fr) minmax(0,1.4fr) 110px 24px;gap:12px;align-items:center;padding:10px 14px;cursor:pointer}
.asrc .ahead .dot{width:8px;height:8px;border-radius:50%}
.asrc .an{font-size:13px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.asrc .alink{font-family:var(--mono);font-size:10.5px;color:var(--mute);text-decoration:none;border-bottom:1px dotted var(--rule2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.asrc .alink:hover{color:var(--navy)}
.asrc .act{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--mute);text-align:right}
.asrc .abody{display:none;border-top:1px dashed var(--rule2);padding:10px 14px 14px}
.asrc.open .abody{display:block}
.asrc .ameta{font-family:var(--mono);font-size:10.5px;color:var(--mute);margin-bottom:8px}
.aitem{display:grid;grid-template-columns:78px 20px minmax(0,1fr);gap:10px;align-items:baseline;padding:4px 0;border-bottom:1px solid var(--wash)}
.aitem:last-child{border-bottom:none}
.aitem .ad{font-family:var(--mono);font-size:10.5px;color:var(--mute)}
.aitem a{color:var(--ink);text-decoration:none;font-size:12.5px;border-bottom:1px dotted var(--rule2)}
.aitem a:hover{color:var(--navy)}
.aitem span.noL{color:var(--ink);font-size:12.5px}
.alane{font-family:var(--mono);font-size:9px;text-transform:uppercase;border-radius:3px;text-align:center;padding:1px 0}
.alane.instruments{background:#E9F3EA;color:#276B2E}
.alane.judgments{background:#EAF0F4;color:#1B6288}
.alane.signals{background:var(--ochre-wash);color:#7A5E0E}
.anone{font-size:12px;color:#7A5E0E;background:var(--ochre-wash);border:1px solid #E4D19A;border-radius:3px;padding:8px 12px}
.averify{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--mute);margin-top:10px;line-height:1.7}
.cl-none{color:var(--faint);font-style:italic}
.cl-draft{margin-top:14px;appearance:none;cursor:pointer;font-family:var(--mono);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.12em;background:var(--navy);color:#fff;border:none;padding:8px 16px;border-radius:3px}
.cl-draft:disabled{opacity:.4;cursor:default}
.cl-empty{color:var(--mute);padding:24px 0}
#cl-modal{display:none;position:fixed;inset:0;background:rgba(0,20,35,.45);z-index:50;align-items:flex-start;justify-content:center;overflow:auto;padding:40px 16px}
#cl-modal.on{display:flex}
.cl-dialog{background:#fff;border-radius:10px;max-width:560px;width:100%;padding:24px 26px;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.cl-dialog h3{font-family:var(--serif);font-weight:500;margin:0 0 14px;font-size:21px}
.cl-lbl{display:block;font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--mute);margin:14px 0 5px}
.cl-hint{text-transform:none;letter-spacing:0;color:var(--faint);font-weight:400}
.cl-in,.cl-ta{width:100%;box-sizing:border-box;border:1px solid var(--rule2);border-radius:4px;padding:8px 10px;font-family:var(--sans);font-size:14px}
.cl-ta{font-family:var(--mono);font-size:12px}
.cl-boxes{display:flex;flex-wrap:wrap;gap:6px 14px}
.cl-chk{font-size:12.5px;color:var(--ink);display:flex;align-items:center;gap:4px}
.cl-formact{display:flex;gap:10px;margin-top:20px}
.cl-save{appearance:none;cursor:pointer;background:var(--ochre);border:none;color:#1a1206;font-family:var(--mono);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;padding:9px 18px;border-radius:3px}
.cl-cancel{appearance:none;cursor:pointer;background:#fff;border:1px solid var(--rule2);color:var(--mute);font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.1em;padding:9px 16px;border-radius:3px}
.cl-draftdlg{max-width:680px}
.cl-draftbox{width:100%;box-sizing:border-box;height:340px;border:1px solid var(--rule2);border-radius:4px;padding:12px;font-family:var(--mono);font-size:11.5px;line-height:1.5;white-space:pre;overflow:auto}
.cl-draftnote{font-size:11px;color:var(--alarm);margin-top:10px}

/* controls */
.controls{margin-top:30px;display:flex;align-items:flex-end;justify-content:space-between;gap:28px;flex-wrap:wrap}
.controls .left{display:flex;align-items:flex-end;gap:26px;flex-wrap:wrap}
#q{width:288px;max-width:100%;border:0;border-bottom:2px solid var(--navy);background:transparent;
  font-size:13px;color:var(--ink);padding:0 0 7px}
#reg,#dates,#stratum{border:0;border-bottom:2px solid var(--navy);background:transparent;font-size:11px;
  font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--navy);padding:0 0 7px;
  appearance:none;-webkit-appearance:none;cursor:pointer}
#stratum{width:150px}
#reg{width:184px}
#dates{width:152px}
.togs{display:flex;gap:8px}
.tog{cursor:pointer;display:flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid var(--rule2);
  background:transparent;color:var(--off);font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.06em}
.tog.on{border-color:var(--navy);background:var(--navy);color:#fff}

/* filter chip */
.tog{cursor:pointer;display:flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid var(--rule2);
  background:transparent;color:var(--off);font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.06em}
.tog.on{border-color:var(--navy);background:var(--navy);color:#fff}
.tog i{display:inline-block;width:10px;height:10px;border:1px solid var(--off);background:transparent}
.tog.on i{border-color:#fff;background:#fff}

/* table */
.tablewrap{overflow-x:auto}
.tbl{min-width:1080px}
.thead{margin-top:24px;border-bottom:2px solid var(--navy)}
.thead .r{display:grid;grid-template-columns:var(--grid);column-gap:24px;padding:11px 0 11px 4px;
  font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.16em;color:var(--navy);font-weight:600}
.row{border-bottom:1px solid var(--rule3);background:var(--paper)}
.row:hover{background:var(--navy-wash)}
.row.open{background:var(--navy-wash)}
.row.routine{background:var(--row3)}
.row .line{cursor:pointer;display:grid;grid-template-columns:var(--grid);column-gap:24px;align-items:baseline;
  padding:16px 0 17px 4px}
.c-date{font-family:var(--mono);font-size:12.5px;color:var(--mute);white-space:nowrap}
.c-date.none{color:var(--faint)}
.c-reg{font-size:12px;font-weight:600;letter-spacing:.06em;color:var(--navy);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row.routine .c-reg{color:var(--faint)}
.c-title{position:relative;min-width:0}
.c-title>.t{display:inline;font-family:var(--serif);font-size:17px;font-weight:500;color:var(--ink);
  border-bottom:1px dotted var(--rule);padding-bottom:2px;text-decoration:none}
a.t:hover{color:var(--navy);border-bottom-color:var(--navy);border-bottom-style:solid;text-decoration:none}
.c-title>.sub{display:block;margin-top:5px;font-size:12.5px;line-height:1.42;color:var(--mute);max-width:64ch}
.row.routine .c-title>.sub{color:var(--faint)}
.row.t2 .c-title>.t{font-size:16px;font-weight:400}
.row.t3 .c-title>.t{font-size:15px;font-weight:400;color:var(--mute)}
.c-type span{display:inline-block;font-family:var(--mono);font-size:9.5px;font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;padding:3px 8px;white-space:nowrap;
  color:var(--navy);background:var(--navy-wash);border:1px solid #C4D6E1}
.c-type span.draft{color:#7A6210;background:var(--ochre-wash);border-color:#E2D4A6}
.c-type span.quiet{color:var(--faint);background:transparent;border-color:var(--rule2)}
.c-dl{font-family:var(--mono);font-size:12.5px;color:var(--mute)}
.c-dl.hot{color:var(--alarm);font-weight:600}
.c-dl.verify{color:var(--alarm);font-weight:600;text-transform:uppercase;letter-spacing:.1em}
.c-dl.none{color:var(--faint)}
.c-mark{font-family:var(--mono);font-size:15px;color:var(--navy);text-align:right}

/* hover peek */
.peek{display:none;position:absolute;top:calc(100% + 10px);left:0;z-index:40;width:568px;max-width:80vw;
  background:var(--paper);border:1px solid var(--ink);padding:15px 18px 17px}
.c-title:hover .peek{display:block}
.row.open .peek{display:none!important}
.peek .lbl{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.18em;color:var(--faint);margin-bottom:9px}
.peek .full{font-family:var(--serif);font-size:14.5px;line-height:1.48;text-wrap:pretty}
.peek .foot{margin-top:13px;padding-top:11px;border-top:1px solid var(--rule2);display:flex;justify-content:space-between;
  gap:16px;font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;color:var(--mute)}

/* expanded */
.detail{display:none;border-top:1px solid var(--rule2);padding:22px 40px 24px 4px}
.row.open .detail{display:block}
.detail .lbl{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.18em;color:var(--faint);margin-bottom:11px}
.detail .full{font-family:var(--serif);font-size:19px;line-height:1.44;max-width:920px;text-wrap:pretty}
.meta{margin-top:24px;border-top:1px solid var(--rule2);display:grid;grid-template-columns:repeat(4,minmax(0,1fr));column-gap:24px}
.meta .k{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.16em;color:var(--navy);font-weight:600;margin-bottom:7px}
.meta .v{font-family:var(--mono);font-size:12.5px}
.meta>div{padding:15px 0 0}
.note{margin-top:22px;display:flex;gap:14px;align-items:baseline}
.note .lbl{flex:0 0 auto;padding-top:3px;margin:0;color:var(--navy);font-weight:600}
.note .body{font-family:var(--serif);font-size:15.5px;line-height:1.5;color:#3A3E42;max-width:760px}
.acts{margin-top:22px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.acts a{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.14em;font-weight:600;color:var(--navy)}
.acts .sep{display:inline-block;width:1px;height:11px;background:#C9C9C9}
.empty{padding:34px 0 0 4px;font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--ghost)}

/* coverage */
.sechead{margin-top:32px;border-top:2px solid var(--ink);padding-top:12px;display:flex;align-items:baseline;
  justify-content:space-between;gap:16px;flex-wrap:wrap}
.sechead .l{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.18em}
.sechead .r{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--faint)}
.covgrid{margin-top:24px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));column-gap:44px;row-gap:34px}
.grp{border-top:1px solid var(--rule);padding-top:11px}
.grp .h{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:9px}
.grp .h b{font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase}
.grp .h i{font-family:var(--mono);font-size:10px;color:var(--ghost);font-style:normal}
.ven{display:grid;grid-template-columns:minmax(0,1fr) 66px 74px;column-gap:12px;align-items:center;
  padding:8px 0;border-bottom:1px solid var(--rule3)}
.ven .n{font-family:var(--serif);font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ven .t{font-family:var(--mono);font-size:10.5px;color:var(--ghost)}
.ven .s{display:flex;align-items:center;justify-content:flex-end;gap:7px;font-family:var(--mono);font-size:9.5px;
  text-transform:uppercase;letter-spacing:.12em;color:var(--faint)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--navy);border:1px solid transparent}
.two{margin-top:48px;display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);column-gap:64px}
.subhead{border-top:2px solid var(--rule);padding-top:12px;font-family:var(--mono);font-size:10px;
  text-transform:uppercase;letter-spacing:.18em;color:var(--faint)}
.subhead.warn{border-top-color:var(--alarm);color:var(--alarm)}
.bl{display:grid;grid-template-columns:minmax(0,1fr) 188px 176px;column-gap:24px;align-items:baseline;padding:11px 0;
  border-bottom:1px solid var(--rule2)}
.bl .n{font-family:var(--serif);font-size:16px}
.bl .r{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--mute)}
.nl{padding:11px 0;border-bottom:1px solid var(--rule2);font-family:var(--serif);font-size:16px;color:var(--mute)}

/* signals */
.sig{background:var(--panel);border-left:3px dashed var(--alarm);border-bottom:1px solid var(--rule2);
  padding:16px 26px 18px 18px}
.sig .h{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.badge{font-family:var(--mono);font-size:9.5px;font-weight:600;text-transform:uppercase;letter-spacing:.18em;
  color:var(--alarm);border:1px solid var(--alarm);padding:3px 7px}
.sig .d{font-family:var(--mono);font-size:11.5px;color:var(--mute)}
.sig .b{font-size:11.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.sig .line{margin-top:11px;font-family:var(--serif);font-size:17px;line-height:1.42;max-width:740px}
.sig .src{margin-top:11px;display:flex;align-items:center;gap:10px}
.sig .src .lbl{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.16em;color:var(--faint)}
.sig .src a{font-family:var(--mono);font-size:11px;letter-spacing:.04em;color:var(--navy);
  text-decoration:underline;text-underline-offset:3px}
.vbar{display:inline-block;width:1px;height:12px;background:#C9C9C9}
.foot{margin-top:24px;font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:var(--ghost)}

/* coverage v2: stratum sections, health states, blind-spot lanes */
.stsec{margin-top:44px}
.stsec:first-of-type{margin-top:0}
.stsec .sechead{margin-top:0}
.dot.ok{background:var(--ok)}
.dot.quiet{background:transparent;border-color:var(--off)}
.dot.warn{background:var(--ochre)}
.dot.bad{background:var(--alarm)}
.dot.pending{background:transparent;border-color:var(--off)}
.ven .s.ok{color:var(--ok)}
.ven .s.quiet{color:var(--faint)}
.ven .s.warn{color:#7A6210}
.ven .s.bad{color:var(--alarm);font-weight:600}
.ven .s.pending{color:var(--ghost)}
/* the newest item a venue is actually showing: what makes "quiet" checkable */
.ven .ev{font-family:var(--mono);font-size:9.5px;color:var(--ghost);white-space:nowrap}
.ven.x{cursor:pointer}
.ven.x:hover{background:var(--navy-wash)}
.ven.open{background:var(--navy-wash)}
.vdet{display:none;grid-column:1/-1;margin-top:9px;padding:10px 0 2px;border-top:1px dashed var(--rule2)}
.ven.open .vdet{display:block}
.vdet .k{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.14em;color:var(--navy);
  font-weight:600;margin-bottom:5px}
.vdet .v{font-family:var(--mono);font-size:11px;line-height:1.55;color:var(--mute);word-break:break-word}
.vdet .v.warnnote{color:#7A6210}
.vdet .v.badnote{color:var(--alarm)}
.vdet .v+.k{margin-top:9px}
.bl .c{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--navy)}
.bl .c.gap{color:var(--alarm);font-weight:600}
.legend{margin-top:14px;display:flex;gap:20px;flex-wrap:wrap;font-family:var(--mono);font-size:9.5px;
  text-transform:uppercase;letter-spacing:.14em;color:var(--faint)}
.legend span{display:flex;align-items:center;gap:7px}

@media (max-width:1080px){
  .covgrid{grid-template-columns:repeat(2,minmax(0,1fr));column-gap:32px}
  .two{grid-template-columns:1fr;row-gap:36px}
}
@media (max-width:760px){
  .head,.tabs,.view{padding-left:22px;padding-right:22px}
  .covgrid{grid-template-columns:1fr}
  .bl{grid-template-columns:1fr;row-gap:4px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}

/* staleness banner: only rendered when the ledger has stopped being swept */
.stale-bar{display:none;background:var(--alarm);color:#fff;padding:11px 64px;
  font-size:13px;line-height:1.45}
.stale-bar.on{display:block}
.stale-bar b{font-weight:700}
.stale-bar span{font-family:var(--mono);font-size:11.5px;letter-spacing:.04em;opacity:.9}
@media (max-width:760px){.stale-bar{padding-left:22px;padding-right:22px}}
</style>

<div class="sheet">
  <div class="head">
    <div class="wordmark">TMT <b>Regulatory Radar</b></div>
    <div class="updbar">
      <div class="updated">Last updated <span id="upd"></span></div>
      <button id="updnow" type="button" title="Run a fresh sweep">Update now</button>
    </div>
  </div>
  <div class="stale-bar" id="stalebar"></div>
  <div class="upd-note" id="updnote"></div>

  <nav class="tabs">
    <button class="on" data-v="coverage">Coverage</button>
    <button data-v="instruments">Instruments</button>
    <button data-v="judgments">Judgments</button>
    <button data-v="signals">Signals</button>
    <button data-v="clients">Clients</button>
    <button data-v="audit">Audit</button>
  </nav>

  <section class="view" id="v-instruments">
    <div class="controls">
      <div class="left">
        <input type="text" id="q" placeholder="Search instruments">
        <select id="stratum" aria-label="Stratum"></select>
        <select id="reg"></select>
        <select id="dates" aria-label="Date range">
          <option value="all">All dates</option>
          <option value="today">Today</option>
          <option value="yesterday">Yesterday</option>
          <option value="week">This week</option>
          <option value="d30">Last 30 days</option>
        </select>
      </div>
      <div class="togs">
      </div>
    </div>
    <div class="tablewrap"><div class="tbl">
      <div class="thead"><div class="r">
        <div>Date</div><div>Regulator</div><div>Instrument</div><div>Type</div><div></div>
      </div></div>
      <div id="rows"></div>
    </div></div>
    <div class="empty" id="empty" style="display:none">No instruments match</div>
  </section>

  <section class="view" id="v-judgments">
    <div class="sechead"><div class="l">Judgments &amp; orders</div>
      <div class="r" id="judgct"></div></div>
    <div class="tablewrap"><div class="tbl">
      <div class="thead"><div class="r jr">
        <div>Date</div><div>Forum</div><div>Matter</div><div>Type</div><div></div>
      </div></div>
      <div id="jrows"></div>
    </div></div>
  </section>

  <section class="view on" id="v-coverage">
    <div class="cl-head" style="margin-bottom:6px">
      <div><div class="cl-eyebrow">Coverage</div>
        <div class="cl-sub">Every link this tracker fetches, by stratum and regulator. If a venue is not on this list, nothing from it can enter the Instruments, Judgments or Audit ledgers.</div></div>
    </div>
    <div id="strata"></div>
    <div class="two">
      <div>
        <div class="subhead warn">Blind spots</div>
        <div id="blind" style="margin-top:12px"></div>
      </div>
      <div>
        <div class="subhead">Not yet live</div>
        <div id="notlive" style="margin-top:12px"></div>
      </div>
    </div>
  </section>

  <section class="view" id="v-signals">
    <div class="sechead" style="border-top-color:#8A2B1C">
      <div class="l" style="color:#8A2B1C">Signals</div>
      <div class="r" style="text-transform:uppercase;letter-spacing:.12em;color:#5A5F63">Press reported, no official text</div>
    </div>
    <div style="margin-top:22px;max-width:1000px" id="sigs"></div>
    <div class="foot">Not memo eligible until gazetted</div>
  </section>
  <section class="view" id="v-clients">
    <div class="cl-head">
      <div><div class="cl-eyebrow">Clients</div>
        <div class="cl-sub">Match new instruments and judgments to each client's watch-list, then draft an alert email in a click. Clients and edits are stored in your browser only — nothing leaves this page.</div></div>
      <div class="cl-headbtns"><button class="cl-add" id="cl-add">+ Add client</button><button class="cl-reset" id="cl-reset" title="Restore the sample clients">Reset</button></div>
    </div>
    <div id="clientlist"></div>
  </section>
  <section class="view" id="v-audit">
    <div class="cl-head">
      <div><div class="cl-eyebrow">Audit — verify us</div>
        <div class="cl-sub">Link by link from Coverage: every document this tracker scraped from each source URL, all three lanes. Open the live listing beside each group and compare — anything the venue shows for the window that is missing below is a miss, and should be reported. Sources that yielded nothing are listed too: silence must be checkable, not hidden.</div></div>
    </div>
    <div id="auditlist"></div>
  </section>
  <div id="cl-modal"></div>
</div>

<script id="tracker-data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('tracker-data').textContent);
const $ = s => document.querySelector(s);
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const prettyUrl = u => { try { const x = new URL(u); return x.hostname.replace(/^www\./,'') + x.pathname.replace(/\/$/,''); } catch(e) { return u; } };
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmt = iso => { if (!iso) return '—'; const p = String(iso).split('-'); return p[2] + ' ' + MON[+p[1]-1] + ' ' + p[0]; };
const days = iso => Math.round((new Date(iso) - new Date(D.today)) / 86400000);
const pl = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');

$('#upd').textContent = D.updated;

// Update-now. The published page runs in a sandbox that cannot reach gov.in, so it cannot
// sweep itself. Behaviour is "both": if the partner has wired their pipeline endpoint
// (window.TMT_CONFIG.pipelineEndpoint, or the deploy-time updateConfig), the button triggers
// that pipeline; with actionsUrl it opens the hosted workflow page; with neither it explains how to refresh.
(function(){
  const cfg = Object.assign({}, D.updateConfig || {}, (window.TMT_CONFIG || {}));
  const btn = $('#updnow'), note = $('#updnote');
  if (!btn) return;
  // The tooltip must not promise a sweep the page cannot start.
  if (!cfg.pipelineEndpoint && !cfg.actionsUrl) btn.title = 'How to refresh this page';
  const say = (html) => { note.innerHTML = html; note.classList.add('on'); };
  const ghLink = () => cfg.actionsUrl
    ? ' You can still run it yourself: <a href="' + esc(cfg.actionsUrl) + '" target="_blank" rel="noopener">open the sweep workflow</a> and press <b>Run workflow</b>.'
    : ' On the firm machine, run <code>engine/run_sweep.sh</code>.';
  btn.addEventListener('click', async () => {
    if (cfg.pipelineEndpoint) {
      btn.disabled = true; btn.textContent = 'Updating…';
      say('Asking the pipeline to run a sweep…');
      try {
        const res = await fetch(cfg.pipelineEndpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'sweep', source: 'tmt-radar-dashboard' }) });
        let msg = '';
        try { msg = ((await res.json()) || {}).message || ''; } catch (e) { msg = ''; }
        // Report exactly what happened — never claim a sweep started unless the endpoint said so.
        say(res.ok ? esc(msg || 'Sweep requested. Reload this page once it finishes.')
                   : esc(msg || ('The trigger endpoint returned ' + res.status + '.')) + ghLink());
      } catch (e) {
        say('No sweep trigger is reachable from this page.' + ghLink());
      } finally { btn.disabled = false; btn.textContent = 'Update now'; }
      return;
    }
    if (cfg.actionsUrl) {
      window.open(cfg.actionsUrl, '_blank', 'noopener');
      say('Opened the hosted pipeline. Click <b>Run workflow</b> there — it sweeps every source, '
        + 'regenerates the briefs, and rebuilds this page (a few minutes). Then reload here.');
      return;
    }
    // Nothing wired: a sandboxed page cannot fetch government sites itself. Say so plainly.
    say('This is a published snapshot from <b>' + esc(D.updated || 'the last sweep') + '</b>. '
      + 'A shared page can\'t fetch government sites itself, so it doesn\'t refresh on click. To update it: '
      + 'on the firm machine run <code>engine/run_sweep.sh</code>, or wire a trigger — see docs/CONNECTOR.md.');
  });
})();

// Age is computed when the page opens, not when it was built: a tab left open for a
// week, or a link opened months later, must still tell the truth about freshness.
(function(){
  if (!D.updatedISO) return;
  const hrs = (Date.now() - new Date(D.updatedISO).getTime()) / 3.6e6;
  if (!(hrs > (D.staleAfterHours || 26))) return;
  const bar = $('#stalebar');
  const age = hrs < 48 ? Math.round(hrs) + ' hours' : Math.floor(hrs / 24) + ' days';
  // There is no schedule: this page only moves when someone runs a sweep. So the banner
  // is the whole safety net, and it must say plainly that nothing has been checked.
  bar.innerHTML = '<b>Nobody has run a check for ' + age + '.</b> '
    + 'Instruments published since then will not appear below, however current this page looks. '
    + '<span>Run a sweep before relying on it.</span>';
  bar.classList.add('on');
})();
const state = { q: '', reg: 'All', stratum: 'All', dates: 'all', open: null, ven: null };

/* Date buckets are anchored to the last sweep date, not the viewer's clock, so the
   filter always agrees with the deadline colouring. Week runs Monday to that date. */
const isoOf = d => d.toISOString().slice(0, 10);
function dateBounds(kind) {
  const t = new Date(D.today + 'T00:00:00Z');
  if (kind === 'today') return [isoOf(t), isoOf(t)];
  if (kind === 'yesterday') { const y = new Date(t); y.setUTCDate(y.getUTCDate() - 1); return [isoOf(y), isoOf(y)]; }
  if (kind === 'week') { const w = new Date(t); w.setUTCDate(w.getUTCDate() - ((w.getUTCDay() + 6) % 7)); return [isoOf(w), isoOf(t)]; }
  if (kind === 'd30') { const m = new Date(t); m.setUTCDate(m.getUTCDate() - 29); return [isoOf(m), isoOf(t)]; }
  return null;
}

document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('on', v.id === 'v-' + b.dataset.v));
}));

/* stratum filter */
$('#stratum').innerHTML = '<option value="All">All strata</option>' +
  D.strataOrder.map(s => '<option value="' + esc(s.key) + '">' + esc(s.label) + '</option>').join('');

/* Regulator options follow the chosen stratum, so the two selects can never be set to a
   combination that yields nothing. A regulator that survives the change stays selected. */
function fillRegs() {
  const pool = D.rows.filter(r => state.stratum === 'All' || r.stratum === state.stratum);
  const regs = [...new Set(pool.map(r => r.reg))].sort();
  if (state.reg !== 'All' && !regs.includes(state.reg)) state.reg = 'All';
  $('#reg').innerHTML = '<option value="All">All regulators</option>' +
    regs.map(r => '<option value="' + esc(r) + '">' + esc(r) + '</option>').join('');
  $('#reg').value = state.reg;
}
fillRegs();

function dlCell(r) {
  if (!r.deadline) return (r.flags || []).includes('needs_verification')
    ? '<div class="c-dl verify">verify</div>' : '<div class="c-dl none">—</div>';
  const n = days(r.deadline);
  return '<div class="c-dl' + (n >= 0 && n <= 30 ? ' hot' : '') + '">' + fmt(r.deadline) + '</div>';
}
function metaCells(r) {
  const m = [['Source', r.venue], ['Issued', r.date ? fmt(r.date) : 'Not dated on venue']];
  if (r.effective) m.push(['In force', fmt(r.effective)]);
  m.push(r.deadline && !r.effective ? [/consult|draft/i.test(r.type) ? 'Comments' : 'Lapses', fmt(r.deadline)] : null);
  if (r.rule) m.push(['Amends', r.rule]);
  if (r.gid) m.push(['Gazette ID', r.gid]);
  if (r.impact) m.push(['Impact', r.impact]);
  m.push(['Status', (r.flags || []).includes('needs_verification') ? 'Gazette pending' : 'On official venue']);
  return m.filter(Boolean).map(x => '<div><div class="k">' + esc(x[0]) + '</div><div class="v">' + esc(x[1]) + '</div></div>').join('');
}
/* "What changed": one deterministic sentence-set composed from the item's own metadata,
   plus the LLM brief of the document body when pipeline/brief.py has produced one. */
const ruleTextOf = r => { if (!r) return ''; return String(r)
  .replace(/\s*\(\d+ of \d{4}\)/g, '')
  .replace(/,\s*Amendment,\s*Change in Substance/i, ' (a change in substance)')
  .replace(/,\s*Amendment\b/i, '').replace(/\s+/g, ' ').trim(); };
function whatChanged(r) {
  const t = (r.type || '').toLowerCase();
  const verb = /amend/.test(t) ? 'has amended' : /order/.test(t) ? 'has passed an order regarding'
    : /(rule|regulation|notif)/.test(t) ? 'has notified' : /direction/.test(t) ? 'has issued a direction on'
    : /advisory/.test(t) ? 'has issued an advisory on' : /press.?note/.test(t) ? 'has issued'
    : /consult|draft/.test(t) ? 'has floated for consultation' : 'has published';
  const p = [(r.reg || 'The regulator') + ' ' + verb + ' ' + (r.short || r.official) + '.'];
  const rt = (r.rule && !/^\s*nil\s*$/i.test(r.rule)) ? ruleTextOf(r.rule) : '';
  if (rt) p.push('It amends ' + rt + '.');
  if (r.effective) p.push('In force from ' + fmt(r.effective) + '.');
  if (r.deadline && !r.effective) p.push((/consult|draft/i.test(r.type) ? 'Comments close ' : 'Deadline: ') + fmt(r.deadline) + '.');
  if (r.impact && /action/i.test(r.impact)) p.push('The Gazette marks it action-required.');
  // p[0] only rewrites the heading as a sentence. With nothing after it there is no fact to
  // report, and printing it anyway is padding that costs the reader a line and teaches them
  // the field is worthless. Say nothing instead.
  return p.length > 1 ? p.join(' ') : null;
}
function wcNote(r, label) {
  const det = whatChanged(r);
  const ai = r.llm && r.llm.brief;
  if (!det && !ai) return '';        // nothing to say beyond the heading — omit the block
  let h = '<div class="note wc"><div class="lbl">' + (label || 'What changed') + '</div>';
  if (ai) {
    h += '<div class="body ai">' + esc(r.llm.brief) + (r.llm.so_what ? ' ' + esc(r.llm.so_what) : '') +
         ' <span class="aitag">AI brief · ' + esc(r.llm.confidence || '') + ' · verify</span></div>';
  }
  if (det) h += '<div class="body">' + esc(det) + '</div>';
  return h + '</div>';
}
function acts(r) {
  const a = [];
  // dual links, always both where they exist: the document itself and the official page
  if (r.doc) a.push('<a href="' + esc(r.doc) + '" target="_blank" rel="noopener">Official text</a>');
  if (r.page && r.page !== r.doc) a.push('<a href="' + esc(r.page) + '" target="_blank" rel="noopener">Source page</a>');
  // the Gazette ID is the permanent citation — shown alongside the direct PDF
  if (r.gid) a.push('<span style="font-family:var(--mono);font-size:10.5px;color:var(--mute)">Gazette ID ' + esc(r.gid) + '</span>');
  if (r.pr) a.push('<a href="' + esc(r.pr) + '" target="_blank" rel="noopener">Announcement</a>');
  if (r.notice) a.push('<a href="' + esc(r.notice) + '" target="_blank" rel="noopener">Consultation notice</a>');
  if (r.memo) a.push('<a href="memos/' + esc(r.memo) + '" target="_blank" rel="noopener">Draft memo</a>');
  if (!a.length) a.push('<span style="font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.14em;color:#8F9396">No linkable copy yet</span>');
  return a.join('<span class="sep"></span>');
}

function render() {
  const q = state.q.trim().toLowerCase();
  const b = dateBounds(state.dates);
  const list = D.rows.filter(r =>
    (!b || (r.date && r.date >= b[0] && r.date <= b[1])) &&
    (state.stratum === 'All' || r.stratum === state.stratum) &&
    (state.reg === 'All' || r.reg === state.reg) &&
    (!q || (r.short + ' ' + r.line + ' ' + r.official + ' ' + r.reg + ' ' + r.type + ' ' + r.gist).toLowerCase().includes(q))
  );
  $('#rows').innerHTML = list.map(r => {
    const open = state.open === r.id;
    return '<div class="row' + (r.routine ? ' routine' : '') + (open ? ' open' : '') + '" data-id="' + r.id + '">' +
      '<div class="line" tabindex="0" role="button" aria-expanded="' + open + '">' +
        '<div class="c-date' + (r.date ? '' : ' none') + '">' + fmt(r.date) + '</div>' +
        '<div class="c-reg">' + esc(r.reg) + '</div>' +
        '<div class="c-title">' +
          ((r.doc || r.page)
            ? '<a class="t" href="' + esc(r.doc || r.page) + '" target="_blank" rel="noopener" title="Open the document">' + esc(r.short) + '</a>'
            : '<span class="t">' + esc(r.short) + '</span>') +
          (r.line ? '<span class="sub">' + esc(r.line) + '</span>' : '') +
          '<div class="peek"><div class="lbl">Official title</div><div class="full">' + esc(r.official) + '</div>' +
          '<div class="foot"><span>' + esc(r.venue) + '</span><span>' + (r.gid ? 'Gazette ' + esc(r.gid) : (r.doc ? 'Document on file' : 'No document linked')) + '</span></div></div>' +
        '</div>' +
        '<div class="c-type"><span class="' + (/draft|consult/i.test(r.type) ? 'draft' : (r.routine ? 'quiet' : '')) +
          '">' + esc((r.type || '').replace(/_/g, ' ')) + '</span></div>' +
        '<div class="c-mark">' + (open ? '−' : '+') + '</div>' +
      '</div>' +
      '<div class="detail"><div class="lbl">Official title</div><div class="full">' + esc(r.official) + '</div>' +
        '<div class="meta">' + metaCells(r) + '</div>' +
        wcNote(r) +
        (r.gist ? '<div class="note"><div class="lbl">Note</div><div class="body">' + esc(r.gist) + '</div></div>' : '') +
        '<div class="acts">' + acts(r) + '</div>' +
      '</div></div>';
  }).join('');
  $('#empty').style.display = list.length ? 'none' : 'block';
  if (!list.length) {
    const lbl = $('#dates').selectedOptions[0].textContent;
    $('#empty').textContent = state.dates === 'all'
      ? 'No instruments match'
      : 'No instruments in this range (' + lbl.toLowerCase() + ')';
  }

  $('#rows').querySelectorAll('.line').forEach(el => {
    const id = el.parentElement.dataset.id;
    const go = () => { state.open = state.open === id ? null : id; render(); };
    el.addEventListener('click', e => { if (e.target.closest('a')) return; go(); });
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  });
}

$('#q').addEventListener('input', e => { state.q = e.target.value; render(); });
$('#stratum').addEventListener('change', e => { state.stratum = e.target.value; fillRegs(); render(); });
$('#reg').addEventListener('change', e => { state.reg = e.target.value; render(); });
$('#dates').addEventListener('change', e => { state.dates = e.target.value; render(); });
render();

/* judgments: a separate lane, so tribunal/court orders never flood the instruments list */
(function renderJudgments(){
  const J = D.judgments || [];
  $('#judgct').textContent = pl(J.length, 'decision') + ' · last 7 days';
  $('#jrows').innerHTML = J.map(r => {
    const open = 'j:' + r.id === state.open;
    const forum = r.venue || r.reg;
    const matter = r.line || r.short;
    return '<div class="row' + (open ? ' open' : '') + '" data-id="j:' + r.id + '">' +
      '<div class="line jr" tabindex="0" role="button" aria-expanded="' + open + '">' +
        '<div class="c-date' + (r.date ? '' : ' none') + '">' + fmt(r.date) + '</div>' +
        '<div class="c-reg">' + esc(forum) + '</div>' +
        '<div class="c-title">' +
          (r.doc ? '<a class="t" href="' + esc(r.doc) + '" target="_blank" rel="noopener">' + esc(r.short) + '</a>'
                 : '<span class="t">' + esc(r.short) + '</span>') +
          (matter && matter !== r.short ? '<span class="sub">' + esc(matter) + '</span>' : '') +
        '</div>' +
        '<div class="c-type"><span class="quiet">' + esc((r.type || 'order').replace(/_/g,' ')) + '</span></div>' +
        '<div class="c-mark">' + (open ? '−' : '+') + '</div>' +
      '</div>' +
      '<div class="detail"><div class="lbl">Matter</div><div class="full">' + esc(r.official) + '</div>' +
        '<div class="meta">' + metaCells(r) + '</div>' +
        (r.llm && r.llm.brief ? '<div class="note wc"><div class="lbl">What it holds</div><div class="body ai">' + esc(r.llm.brief) + ' <span class="aitag">AI brief · ' + esc(r.llm.confidence || '') + ' · verify</span></div></div>' : '') +
        '<div class="acts">' + acts(r) + '</div>' +
      '</div></div>';
  }).join('') || '<div class="empty">No judgments in the current window</div>';
  $('#jrows').querySelectorAll('.line').forEach(el => {
    const id = el.parentElement.dataset.id;
    const go = () => { state.open = state.open === id ? null : id; renderJudgments(); };
    el.addEventListener('click', e => { if (e.target.closest('a')) return; go(); });
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  });
})();

/* coverage: stratum -> regulator -> venue, with per-source health */
function venDetail(v) {
  const p = [];
  if (v.checked) p.push(['Last checked', esc(v.checked) + ' IST']);
  if (v.rows != null) p.push(['Rows seen', esc(v.rows) + (v.new != null ? ' · ' + esc(v.new) + ' new' : '')]);
  // Evidence that a quiet venue is alive rather than silently broken.
  if (v.newest) p.push(['Newest item on the venue', esc(v.newest)]);
  else if (v.rows) p.push(['Newest item on the venue', 'venue publishes no dates']);
  if (v.held != null) p.push(['Instruments held from here', esc(v.held)]);
  if (v.lane && v.lane !== 'instruments') p.push(['Lane', esc(v.lane) + ' — leads, not citable instruments']);
  if (v.fails) p.push(['Consecutive failures', esc(v.fails)]);
  let h = p.map(x => '<div class="k">' + x[0] + '</div><div class="v">' + x[1] + '</div>').join('');
  if (v.notes && v.notes.length) {
    const c = v.st === 'bad' ? ' badnote' : (v.st === 'warn' ? ' warnnote' : '');
    h += '<div class="k">Notes</div>' + v.notes.map(n => '<div class="v' + c + '">' + esc(n) + '</div>').join('');
  }
  if (v.info && v.info.length) h += '<div class="k">Info</div>' + v.info.map(n => '<div class="v">' + esc(n) + '</div>').join('');
  if (v.st === 'pending' && !(v.notes || []).length) h += '<div class="k">Notes</div><div class="v">No sweep recorded for this source yet.</div>';
  h += '<div class="k">Venue</div><div class="v">' + esc(v.method) + (v.role ? ' · ' + esc(v.role) : '') + '</div>';
  if (v.url) h += '<div class="v"><a href="' + esc(v.url) + '" target="_blank" rel="noopener">' + esc(prettyUrl(v.url)) + '</a></div>';
  return '<div class="vdet">' + h + '</div>';
}

function renderCoverage() {
  $('#strata').innerHTML = D.coverage.strata.map(s =>
    '<div class="stsec">' +
      '<div class="sechead"><div class="l">' + esc(s.label) + '</div>' +
      '<div class="r">' + pl(s.regs, 'regulator') + ', ' + pl(s.venues, 'venue') + '</div></div>' +
      '<div class="covgrid">' + s.groups.map(g =>
        '<div class="grp"><div class="h"><b>' + esc(g.reg) + '</b><i>' + pl(g.venues.length, 'venue') + '</i></div>' +
        g.venues.map(v => {
          const tip = (v.notes || []).concat(v.info || []).join(' · ');
          const open = state.ven === v.id;
          return '<div class="ven x' + (open ? ' open' : '') + '" data-id="' + esc(v.id) + '" tabindex="0" role="button" aria-expanded="' + open + '">' +
            '<div class="n" title="' + esc(v.n) + '">' + esc(v.n) + '</div>' +
            '<div class="t">' + (v.newest ? esc(v.newest)
                 : (v.rows != null ? esc(v.rows) + ' rows' : '—')) + '</div>' +
            '<div class="s ' + esc(v.st) + '"' + (tip ? ' title="' + esc(tip) + '"' : '') + '>' +
              '<span>' + esc(v.s) + '</span><span class="dot ' + esc(v.st) + '"></span></div>' +
            (v.url ? '<div class="vlink"><a href="' + esc(v.url) + '" target="_blank" rel="noopener" title="The exact URL the engine fetches">' + esc(prettyUrl(v.url)) + '</a></div>' : '') +
            venDetail(v) + '</div>';
        }).join('') + '</div>').join('') +
      '</div>' +
    '</div>').join('') +
    '<div class="legend">' +
      '<span><i class="dot ok"></i>Producing instruments</span>' +
      '<span><i class="dot quiet"></i>Reachable, nothing new (date = newest item there)</span>' +
      '<span><i class="dot warn"></i>Warn</span>' +
      '<span><i class="dot bad"></i>Failed or yielding nothing</span>' +
      '<span>' + pl(D.coverage.regs, 'regulator') + ', ' + pl(D.coverage.live, 'venue') + ' live' +
      ((D.coverage.tally && D.coverage.tally.bad) ? ' · ' + D.coverage.tally.bad + ' currently down' : '') +
      (D.coverage.healthAt ? ' · health ' + esc(D.coverage.healthAt) + ' IST' : '') + '</span>' +
    '</div>';

  $('#strata').querySelectorAll('.ven.x').forEach(el => {
    const id = el.dataset.id;
    const go = () => { state.ven = state.ven === id ? null : id; renderCoverage(); };
    el.addEventListener('click', e => { if (e.target.closest('a')) return; go(); });
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  });
}
renderCoverage();

$('#blind').innerHTML = D.coverage.blind.map(b =>
  '<div class="bl"' + (b.q ? ' title="' + esc(b.q) + '"' : '') + '>' +
  '<div class="n">' + esc(b.n) + '</div><div class="r">' + esc(b.r) + '</div>' +
  '<div class="c' + (/no live lane|reference only/i.test(b.c) ? ' gap' : '') + '">' + esc(b.c) + '</div></div>').join('');
$('#notlive').innerHTML = D.coverage.notlive.map(n => '<div class="nl">' + esc(n) + '</div>').join('');

/* audit: link-wise scraped-document ledger for human verification */
(function renderAudit(){
  const A = D.audit || [];
  const wrap = document.querySelector('#auditlist'); if (!wrap || !A.length) return;
  let openSrc = null;
  const laneName = l => l === 'judgments' ? 'jdg' : l === 'signals' ? 'sig' : 'ins';
  function draw(){
    wrap.innerHTML = A.map(st =>
      '<div class="stsec"><div class="sechead"><div class="l">' + esc(st.label) + '</div>' +
      '<div class="r">' + pl(st.sources.length, 'source link') + ' · ' +
        pl(st.sources.reduce((n, x) => n + x.items.length, 0), 'document') + ' scraped</div></div>' +
      st.sources.map(sc => {
        const open = openSrc === sc.id;
        return '<div class="asrc' + (open ? ' open' : '') + '" data-id="' + esc(sc.id) + '">' +
          '<div class="ahead" tabindex="0" role="button" aria-expanded="' + open + '">' +
            '<span class="dot ' + esc(sc.st) + '"' + ' title="' + esc(sc.s) + '"></span>' +
            '<div class="an" title="' + esc(sc.reg + ' — ' + sc.n) + '"><b>' + esc(sc.reg) + '</b> — ' + esc(sc.n) + '</div>' +
            '<a class="alink" href="' + esc(sc.url) + '" target="_blank" rel="noopener" title="Open the live listing this tracker fetches">' + esc(prettyUrl(sc.url)) + '</a>' +
            '<div class="act">' + (sc.items.length ? pl(sc.items.length, 'doc') : 'nothing') + '</div>' +
            '<div class="c-mark">' + (open ? String.fromCharCode(8722) : '+') + '</div>' +
          '</div>' +
          '<div class="abody">' +
            (sc.note ? '<div class="ameta">' + esc(sc.note) + '</div>' : '') +
            '<div class="ameta">Health: ' + esc(sc.s) +
              (sc.checked ? ' · last checked ' + esc(sc.checked) + ' IST' : ' · no sweep recorded yet') +
              (sc.newest ? ' · newest item visible on the venue: ' + esc(sc.newest) : '') +
              (sc.rows != null ? ' · ' + esc(sc.rows) + ' rows parsed on the last sweep' : '') +
              (sc.method && sc.method !== 'GET' ? ' · fetched via ' + esc(sc.method) : '') + '</div>' +
            (sc.items.length
              ? '<div class="alist">' + sc.items.map(i =>
                  '<div class="aitem"><span class="ad">' + (i.d ? fmt(i.d) : String.fromCharCode(8212)) + '</span>' +
                  '<span class="alane ' + esc(i.lane) + '" title="' + esc(i.lane) + '">' + laneName(i.lane) + '</span>' +
                  ((i.doc || i.page)
                    ? '<a href="' + esc(i.doc || i.page) + '" target="_blank" rel="noopener" title="' + esc(i.o || '') + '">' + esc(i.t) + '</a>'
                    : '<span class="noL" title="' + esc(i.o || '') + '">' + esc(i.t) + '</span>') +
                  '</div>').join('') + '</div>'
              : (sc.st === 'bad'
                ? '<div class="anone">This source has been FAILING ' + String.fromCharCode(8212) + ' the tracker cannot currently read the venue, so this lane is blind, not silent. Anything the venue published' + (sc.checked ? ' since ' + esc(sc.checked) : '') + ' is unverified until the source recovers.</div>'
                : '<div class="anone">Nothing was scraped from this link in the window. That is a claim, and it is checkable: open the live listing and confirm the venue really published nothing new. If it did, this tracker missed it ' + String.fromCharCode(8212) + ' report it.</div>')) +
            '<div class="averify">Audit check: open the source link ' + String.fromCharCode(8594) + ' list what the venue shows for the window ' + String.fromCharCode(8594) + ' compare with the rows above. Hover a row for the official title as the venue prints it.</div>' +
          '</div></div>';
      }).join('') + '</div>').join('');
    wrap.querySelectorAll('.ahead').forEach(el => {
      const id = el.parentElement.dataset.id;
      const go = () => { openSrc = openSrc === id ? null : id; draw(); };
      el.addEventListener('click', e => { if (e.target.closest('a')) return; go(); });
      el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
  }
  draw();
})();

/* signals */
$('#sigs').innerHTML = D.signals.map(s =>
  '<div class="sig"><div class="h"><span class="badge">Unpublished</span><span class="vbar"></span>' +
  '<span class="d">' + esc(s.date_reported) + '</span><span class="vbar"></span>' +
  '<span class="b">' + esc(s.issuing_body) + '</span></div>' +
  '<div class="line">' + esc(s.title) + '</div>' +
  '<div class="src"><span class="lbl">Source</span><a href="' + esc(s.secondary_url) + '" target="_blank" rel="noopener">' +
  esc((s.secondary_url || '').replace(/^https?:\/\/(www\.)?/, '').split('/')[0]) + '</a></div></div>').join('');
/* ---- Clients tab: match items, judge materiality, brief + per-item draft email (client-side) ---- */
(function(){
  const CLKEY = 'tmt_clients_v3';
  const $c = s => document.querySelector(s);
  const escc = s => (s==null?'':String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const fmtD = iso => { if(!iso||String(iso).length<10) return String(iso||''); const [y,m,d]=String(iso).slice(0,10).split('-'); return (+d)+' '+MON[(+m)-1]+' '+y; };
  const ruleText = r => { if(!r || /^\s*nil\s*$/i.test(r)) return ''; return r
      .replace(/\s*\(\d+ of \d{4}\)/g,'')
      .replace(/,\s*Amendment,\s*Change in Substance/i,' (a change in substance)')
      .replace(/,\s*Amendment\b/i,'').replace(/\s+/g,' ').trim(); };
  const shortRule = r => { if(!r) return ''; const sec=/section\s+(\d+[A-Z]?)/i.exec(r); const act=/of the ([A-Z][^,]+? Act,? \d{4})/.exec(r);
    const bits=[]; if(sec) bits.push('s.'+sec[1]); if(act) bits.push(act[1].replace(/,?\s(\d{4})$/,' $1').trim()); return bits.join(', '); };

  function seed(){ return JSON.parse(JSON.stringify(D.clients || [])); }
  function load(){ try{ const s=localStorage.getItem(CLKEY); if(s) return JSON.parse(s); }catch(e){} return seed(); }
  function save(list){ try{ localStorage.setItem(CLKEY, JSON.stringify(list)); }catch(e){} }
  let clients = load();

  const POOL = () => (D.rows||[]).concat(D.judgments||[]);
  const REGS = () => [...new Set(POOL().map(x=>x.reg).filter(Boolean))].sort();

  // Deterministic materiality: does this change justify proactively emailing the client?
  // Rules-based and transparent — no black box, the partner sees the reason.
  function materiality(it){
    const t=(it.type||'').toLowerCase(), line=(it.line||'').toLowerCase();
    const actionable = (it.impact && /action/i.test(it.impact)) || /action required/.test(line) || !!it.deadline || !!it.effective;
    if(it.routine) return {level:'fyi', label:'FYI', why:'recurring or administrative output — not usually a client alert'};
    if(it.lane==='judgments') return {level:'monitor', label:'Monitor', why:'a decision/precedent — inform and verify if it touches the client, rather than alert'};
    if(/(rule|regulation|notif|direction|amend)/i.test(t) && actionable)
      return {level:'notify', label:'Worth an email', why:'a binding change carrying an obligation'+(it.deadline?' with a deadline':'')+' — worth proactively notifying the client'};
    if(/(rule|regulation|notif|direction|amend|order|press.?note|guideline)/i.test(t))
      return {level:'review', label:'Partner to judge', why:'a binding instrument but no clear deadline — a partner should decide whether it warrants a note'};
    return {level:'fyi', label:'FYI', why:'informational'};
  }
  const ORDER={notify:0, review:1, monitor:2, fyi:3};

  function brief(it){
    const reg=it.reg||'The regulator', t=(it.type||'').toLowerCase();
    const verb = /amend/.test(t)?'has amended':/order/.test(t)?'has passed an order regarding':/(rule|regulation|notif)/.test(t)?'has notified':/direction/.test(t)?'has issued a direction on':/advisory/.test(t)?'has issued an advisory on':/press.?note/.test(t)?'has issued':'has published';
    const p=[reg+' '+verb+' '+(it.short||it.official)+'.'];
    const rt=ruleText(it.rule); if(rt) p.push('It amends '+rt+'.');
    if(it.effective) p.push('In force from '+fmtD(it.effective)+'.');
    else if(it.date) p.push('Dated '+fmtD(it.date)+'.');
    if(it.deadline) p.push('Deadline: '+fmtD(it.deadline)+'.');
    if(it.impact && /action/i.test(it.impact)) p.push('The Gazette marks it action-required.');
    // p[0] merely restates the heading; with nothing after it there is no fact to report.
    return p.length>1 ? p.join(' ') : '';
  }
  // Prefer the LLM brief of the document body when the pipeline has produced one; else the
  // deterministic metadata brief. ai/conf drive the "verify" marker shown in the UI.
  function briefOf(it){
    if(it.llm && it.llm.brief) return {text:it.llm.brief, so:(it.llm.so_what||''), ai:true, conf:(it.llm.confidence||'')};
    return {text:brief(it), so:'', ai:false, conf:''};   // text may be '' — callers must tolerate it
  }

  function matchClient(cl){
    const w=cl.watch||{};
    const kws=(w.keywords||[]).map(k=>{try{return new RegExp(k,'i');}catch(e){return null;}}).filter(Boolean);
    const regs=new Set(w.regulators||[]);
    const out=[];
    for(const it of POOL()){
      const hay=[it.short,it.line,it.official,it.type].filter(Boolean).join(' ');
      const hits=[]; for(const rx of kws){const m=hay.match(rx); if(m) hits.push(m[0].toLowerCase());}
      if(!hits.length) continue;
      const reasons=[]; if(regs.has(it.reg)) reasons.push('from '+it.reg+', a regulator you follow');
      reasons.push('mentions '+[...new Set(hits)].slice(0,4).map(h=>"'"+h+"'").join(', '));
      out.push({it, reasons, mat:materiality(it)});
    }
    out.sort((a,b)=> (ORDER[a.mat.level]-ORDER[b.mat.level]) || (b.it.date||'').localeCompare(a.it.date||''));
    return out;
  }

  // --- draft emails ---
  function bulkDraft(cl, matched){
    const worth=matched.filter(m=>m.mat.level==='notify'); const use=worth.length?worth:matched;
    const n=use.length, pl=n!==1?'s':'';
    let s='Subject: TMT regulatory update — '+n+' item'+pl+' for '+cl.name+'\n\nDear [client contact],\n\n';
    s+='The following '+n+(worth.length?' priority':'')+' development'+pl+' may affect '+cl.name+(cl.scope?(' within our advice scope ('+cl.scope.replace(/\.$/,'')+')'):(' ('+cl.sector+')'))+':\n\n';
    use.forEach((m,i)=>{const it=m.it, doc=it.doc||it.page||'', cite=it.gid?('Gazette '+it.gid):'';
      s+=(i+1)+'. '+(it.short||it.official||'')+'  ['+m.mat.label+']\n   '+(it.reg||'')+' · '+((it.type||'').replace(/_/g,' '))+' · '+fmtD(it.date)+'\n   '+(function(){var b=briefOf(it);return b.text+(b.so?(' '+b.so):'')+(b.ai?(' [AI brief — '+(b.conf||'unrated')+' confidence — verify against the official text]'):'');})()+'\n   Why on your radar: '+m.reasons.join('; ')+'.\n   Official text: '+doc+(cite?('  ·  '+cite):'')+'\n\n';});
    s+='We flag these for your review and will follow with a considered note on any that warrant action.\n\nPrepared by [partner], Trilegal TMT.\n\n— DRAFT for partner review. Verify each item against the official text before advising the client. Not sent.';
    return s;
  }
  function itemDraft(cl, m){
    const it=m.it, doc=it.doc||it.page||'', cite=it.gid?('Gazette '+it.gid):'';
    let s='Subject: '+(it.reg||'Regulatory')+' update — '+(it.short||it.official)+' ('+cl.name+')\n\nDear [client contact],\n\n';
    s+='A quick note on a regulatory development relevant to '+cl.name+(cl.scope?(' '+String.fromCharCode(8212)+' within our advice scope: '+cl.scope.replace(/\.$/,'')):'')+'.\n\n';
    s+=(it.short||it.official||'')+'\n'+(it.reg||'')+' · '+((it.type||'').replace(/_/g,' '))+' · '+fmtD(it.date)+'\n\n';
    var bf=briefOf(it); s+=bf.text+(bf.so?(' '+bf.so):'')+(bf.ai?(' [AI brief — '+(bf.conf||'unrated')+' confidence — verify against the official text]'):'')+'\n\n';
    s+='Why it matters to you: '+m.reasons.join('; ')+'.\n\n';
    if(m.mat.level==='notify') s+='We think this warrants your attention'+(it.deadline?(', and note the deadline of '+fmtD(it.deadline)):'')+'. ';
    s+='The official text is here: '+doc+(cite?('  ·  '+cite):'')+'\n\n';
    s+='Happy to talk through how it applies to '+cl.name+'.\n\nBest regards,\n[partner], Trilegal TMT\n\n— DRAFT for partner review. Verify against the official text before sending. Not sent.';
    return s;
  }

  function renderList(){
    const wrap=$c('#clientlist'); if(!wrap) return;
    if(!clients.length){ wrap.innerHTML='<div class="cl-empty">No clients yet. Add one to start matching regulatory changes to it.</div>'; return; }
    wrap.innerHTML = clients.map((cl,idx)=>{
      const m=matchClient(cl), w=cl.watch||{}, worth=m.filter(x=>x.mat.level==='notify').length;
      const scope=[(w.regulators||[]).join(', '), ((w.keywords||[]).length)+' keywords'].filter(Boolean).join(' · ');
      const items=m.slice(0,80).map((x,j)=>{const it=x.it, bf=briefOf(it);
        return '<li class="mat-'+x.mat.level+'"><div class="cl-itop"><a href="'+escc(it.doc||it.page||'#')+'" target="_blank" rel="noopener">'+escc(it.short||it.official)+'</a>'
          +'<span class="cl-badge b-'+x.mat.level+'">'+escc(x.mat.label)+'</span></div>'
          +'<div class="cl-m">'+escc(it.reg||'')+' · '+escc((it.type||'').replace(/_/g,' '))+' · '+escc(fmtD(it.date))+'</div>'
          +(bf.text?('<div class="cl-brief">'+escc(bf.text)+(bf.so?(' <span class="cl-so">'+escc(bf.so)+'</span>'):'')+(bf.ai?(' <span class="cl-ai">AI brief · '+escc(bf.conf)+' · verify</span>'):'')+'</div>'):'')
          +'<div class="cl-why">On the radar — '+escc(x.reasons.join('; '))+'. <span class="cl-matwhy">'+escc(x.mat.why)+'.</span></div>'
          +'<button class="cl-idraft" data-act="idraft" data-i="'+idx+'" data-j="'+j+'">Draft email</button></li>';}).join('');
      return '<div class="cl-card" data-i="'+idx+'">'
        +'<div class="cl-top"><div><span class="cl-name">'+escc(cl.name)+'</span> <span class="cl-sec">'+escc(cl.sector||'')+'</span></div>'
        +'<div class="cl-act"><span class="cl-count">'+m.length+' match'+(m.length!==1?'es':'')+(worth?(' · <b>'+worth+' worth an email</b>'):'')+'</span>'
        +'<button class="cl-btn" data-act="toggle" data-i="'+idx+'">View</button>'
        +'<button class="cl-btn" data-act="edit" data-i="'+idx+'">Edit</button>'
        +'<button class="cl-btn cl-del" data-act="del" data-i="'+idx+'">×</button></div></div>'
        +(cl.scope?('<div class="cl-scope"><b>Advises on</b>'+escc(cl.scope)+'</div>'):'')+'<div class="cl-scope"><b>Watches</b>'+escc(scope)+'</div>'+(cl.gaps?('<div class="cl-gap">Coverage gap: '+escc(cl.gaps)+'</div>'):'')
        +'<div class="cl-matches" id="clm-'+idx+'"><ul>'+(items||'<li class="cl-none">No current items match this scope.</li>')+'</ul>'
        +(m.length?'<button class="cl-draft" data-act="draft" data-i="'+idx+'">Draft alert — '+(worth||m.length)+' item'+((worth||m.length)!==1?'s':'')+'</button>':'')+'</div></div>';
    }).join('');
  }

  function openForm(idx){
    const editing=idx!=null, cl=editing?clients[idx]:{name:'',sector:'',watch:{regulators:[],strata:[],keywords:[]}}, w=cl.watch||{regulators:[],strata:[],keywords:[]};
    const regBoxes=REGS().map(r=>'<label class="cl-chk"><input type="checkbox" value="'+escc(r)+'"'+((w.regulators||[]).includes(r)?' checked':'')+'> '+escc(r)+'</label>').join('');
    const strBoxes=[['telecom','Telecom'],['tech_data','Tech & data'],['media','Media']].map(s=>'<label class="cl-chk"><input type="checkbox" value="'+s[0]+'"'+((w.strata||[]).includes(s[0])?' checked':'')+'> '+s[1]+'</label>').join('');
    $c('#cl-modal').innerHTML='<div class="cl-dialog"><h3>'+(editing?'Edit client':'Add client')+'</h3>'
      +'<label class="cl-lbl">Name</label><input id="cf-name" class="cl-in" value="'+escc(cl.name)+'">'
      +'<label class="cl-lbl">Sector / description</label><input id="cf-sec" class="cl-in" value="'+escc(cl.sector||'')+'">'
      +'<label class="cl-lbl">Advice scope <span class="cl-hint">— what the firm advises this client on; quoted in draft emails.</span></label>'+'<textarea id="cf-scope" class="cl-ta" rows="2">'+escc(cl.scope||'')+'</textarea>'
      +'<label class="cl-lbl">Coverage gaps <span class="cl-hint">— regulators this client needs that are NOT in coverage; shown as an honest warning on the card.</span></label>'+'<textarea id="cf-gaps" class="cl-ta" rows="2">'+escc(cl.gaps||'')+'</textarea>'
      +'<label class="cl-lbl">Regulators to follow</label><div class="cl-boxes" id="cf-regs">'+regBoxes+'</div>'
      +'<label class="cl-lbl">Strata</label><div class="cl-boxes" id="cf-str">'+strBoxes+'</div>'
      +'<label class="cl-lbl">Keywords <span class="cl-hint">— one per line; a subject must match one. Regex ok (e.g. <code>dark.?pattern</code>, <code>\\bDPDP\\b</code>).</span></label>'
      +'<textarea id="cf-kw" class="cl-ta" rows="6">'+escc((w.keywords||[]).join('\n'))+'</textarea>'
      +'<div class="cl-formact"><button class="cl-save" id="cf-save">Save</button><button class="cl-cancel" id="cf-cancel">Cancel</button></div></div>';
    $c('#cl-modal').classList.add('on');
    $c('#cf-cancel').onclick=()=>$c('#cl-modal').classList.remove('on');
    $c('#cf-save').onclick=()=>{const name=$c('#cf-name').value.trim(); if(!name){$c('#cf-name').focus();return;}
      const regs=[...$c('#cf-regs').querySelectorAll('input:checked')].map(x=>x.value);
      const str=[...$c('#cf-str').querySelectorAll('input:checked')].map(x=>x.value);
      const kw=$c('#cf-kw').value.split('\n').map(x=>x.trim()).filter(Boolean);
      const obj={id:(editing?cl.id:('c'+Date.now())), name, sector:$c('#cf-sec').value.trim(), scope:$c('#cf-scope').value.trim(), gaps:$c('#cf-gaps').value.trim(), watch:{regulators:regs,strata:str,keywords:kw}};
      if(editing) clients[idx]=obj; else clients.push(obj);
      save(clients); $c('#cl-modal').classList.remove('on'); renderList();};
  }

  function showDraft(title, text){
    $c('#cl-modal').innerHTML='<div class="cl-dialog cl-draftdlg"><h3>'+escc(title)+'</h3>'
      +'<textarea class="cl-draftbox" id="cl-drafttext" readonly>'+escc(text)+'</textarea>'
      +'<div class="cl-formact"><button class="cl-save" id="cl-copy">Copy</button><button class="cl-cancel" id="cl-close">Close</button></div>'
      +'<div class="cl-draftnote">DRAFT for partner review — verify each item against the official text before sending. Nothing is sent from here.</div></div>';
    $c('#cl-modal').classList.add('on');
    $c('#cl-close').onclick=()=>$c('#cl-modal').classList.remove('on');
    $c('#cl-copy').onclick=()=>{const t=$c('#cl-drafttext'); t.select(); try{document.execCommand('copy');}catch(e){} if(navigator.clipboard){navigator.clipboard.writeText(t.value).catch(()=>{});} $c('#cl-copy').textContent='Copied'; setTimeout(()=>{$c('#cl-copy').textContent='Copy';},1200);};
  }

  document.addEventListener('click', e=>{
    const b=e.target.closest('[data-act]'); if(!b) return;
    const i=+b.dataset.i, act=b.dataset.act;
    if(act==='toggle'){const m=$c('#clm-'+i); m.classList.toggle('on'); b.textContent=m.classList.contains('on')?'Hide':'View';}
    else if(act==='edit') openForm(i);
    else if(act==='del'){ if(confirm('Remove '+clients[i].name+'?')){clients.splice(i,1); save(clients); renderList();} }
    else if(act==='draft'){const cl=clients[i]; showDraft('Draft alert — '+cl.name, bulkDraft(cl, matchClient(cl)));}
    else if(act==='idraft'){const cl=clients[i], m=matchClient(cl)[+b.dataset.j]; if(m) showDraft('Draft email — '+cl.name, itemDraft(cl, m));}
  });
  const addBtn=$c('#cl-add'); if(addBtn) addBtn.onclick=()=>openForm(null);
  const resetBtn=$c('#cl-reset'); if(resetBtn) resetBtn.onclick=()=>{ if(confirm('Reset to the sample clients?')){clients=seed(); save(clients); renderList();} };
  renderList();
})();

</script>
"""

# Ship the sample memo beside the page so its link resolves from any host.
_memo_src = ROOT / "memos"
if _memo_src.exists():
    _memo_dst = DIST / "memos"
    _memo_dst.mkdir(exist_ok=True)
    for _f in MEMOS.values():
        if (_memo_src / _f).exists():
            (_memo_dst / _f).write_bytes((_memo_src / _f).read_bytes())

html = TEMPLATE.replace("__DATA__", data_json)
_out = DIST / "tmt-radar-v2.html"
_out.write_text(html, encoding="utf-8")

# Vercel promotes any build that exits 0. dist/ also holds the legacy v1 page, so it is never
# empty and Vercel's own missing-output-directory guard can never fire — meaning a build that
# silently produced no page would deploy successfully and serve a 404 at "/". Assert the
# artifact exists and is a plausible size, so a broken build fails loudly and the previous
# deployment keeps serving instead.
if not _out.exists() or _out.stat().st_size < 100_000:
    raise SystemExit(f"build produced no usable page at {_out} "
                     f"({_out.stat().st_size if _out.exists() else 'missing'} bytes) — "
                     f"refusing to exit 0 and let a broken deployment be promoted")
print(f"wrote {DIST/'tmt-radar-v2.html'} ({len(html):,} bytes)")
print(f"rows={len(rows)} folded={folded_n} undated={undated_n} live_venues={live_count} "
      f"regs={reg_count} blind={len(blind)} notlive={notlive}")
print("stratum_counts=" + json.dumps(stratum_counts))
print("health=" + json.dumps(health_tally) + f" (registry={len(registry['sources'])}, health_entries={len(health)})")
