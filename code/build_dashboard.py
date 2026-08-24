#!/usr/bin/env python3
"""Deterministic dashboard renderer. Data files in -> single HTML out. No LLM in this path.
Visual system implemented from the Claude Design canvas "TMT Regulatory Radar"."""
import json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)

ROOT = Path(__file__).resolve().parent.parent
DATA, DIST = ROOT / "data", ROOT / "dist"
DIST.mkdir(exist_ok=True)

items = json.loads((DATA / "items.json").read_text())
signals = json.loads((DATA / "signals.json").read_text())
shelf = json.loads((DATA / "rules_shelf.json").read_text())
registry = json.loads((ROOT / "registry" / "sources.json").read_text())
overrides = json.loads((DATA / "short_titles.json").read_text())
lines_map = json.loads((DATA / "row_lines.json").read_text())
folds_map = json.loads((DATA / "folds.json").read_text())

# ---- deterministic display-title shortener (fallback for items with no override) ----
STRIP = ["Notice for stakeholder consultation on Draft for Comments – Test Cases for ",
         "Notice for stakeholder consultation on ", "Draft Generic Test Cases for ",
         "Consultation Paper on Draft Amendments in The ", "Consultation Paper on ",
         "Instructions to be specified on the portal in accordance with the ",
         "Direction on Allocation and operationalization of ", "Direction on ", "Order regarding ",
         "TRAI releases clarifications regarding ", "TRAI releases ", "TRAI issues an amended ",
         "TRAI issues ", "Press Release on ", "Notification for ", "Notification of ",
         "NCCS designates ", "Extension of last date for "]
CUT = [" for Service and Transactional", " for entities in sectors", " under TCCCPR",
       " in accordance with", " for assessment of", " notified under", " pursuant to",
       " (Wireline and Wireless)", " Service Regulations, 2024"]

def shorten(t, cap=54):
    s = t.strip()
    for p in STRIP:
        if s.lower().startswith(p.lower()):
            s = s[len(p):]; break
    for c in CUT:
        i = s.find(c)
        if i > 12:
            s = s[:i]; break
    s = s.strip(" ,;:—-")
    if len(s) <= cap:
        return s
    return s[:cap].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"

def venue_label(reg, name):
    reg = reg.split("/")[-1].strip()
    n = name.replace(" \u2014 ", " ").replace(" \u2013 ", " ").split("/")[0].split(" (")[0].strip()
    if n.lower().startswith(reg.lower()):
        n = n[len(reg):].strip()
    lab = f"{reg} {n}".strip()
    return lab if len(lab) <= 34 else lab[:34].rsplit(" ", 1)[0]

VENUE = {s["id"]: venue_label(s["regulator"], s["name"]) for s in registry["sources"]}
VENUE["gazette_crosscheck"] = "e-Gazette cross-check"

DASH = __import__("re").compile(r"\s+[\u2014\u2013]\s+")
def clean(t):
    """Deterministic prose cleanup for display: dashes used as punctuation become commas."""
    return DASH.sub(", ", (t or "").strip())
def first_sentence(t, cap=110):
    """Fallback description line: first sentence of the gist, capped on a word boundary."""
    t = (t or "").strip()
    if not t:
        return ""
    for i, ch in enumerate(t):
        if ch in ".;" and i > 30:
            t = t[:i + 1]
            break
    return t if len(t) <= cap else t[:cap].rsplit(" ", 1)[0].rstrip(" ,;:") + "\u2026"

MEMOS = {"d90090c2ea": "2026-08-10_TRAI_1601-series_client-alert_SAMPLE.docx"}
TYPE_LABEL = {"consultation_notice":"Consultation","consultation_paper":"Consultation",
  "draft_test_cases":"Draft","draft_rules":"Draft rules","exemption_circular":"Circular",
  "tstl_designation":"Designation","court_notice":"Notice","event_notice":"Notice",
  "press_release":"Release","pib_release":"Release","administrative":"Administrative",
  "clarification":"Clarification","notification":"Notification","circular":"Circular",
  "rules":"Rules","direction":"Direction","recommendation":"Recommendation","manual":"Manual",
  "order":"Order","do_letter":"Letter","policy":"Policy","other":"Other"}

rows = []
for it in items["items"]:
    rows.append({
        "id": it["id"], "date": it["date"], "reg": it["regulator"],
        "routine": it.get("routine", False),
        "type": TYPE_LABEL.get(it.get("type",""), (it.get("type","") or "").replace("_"," ").title()),
        "short": overrides.get(it["id"]) or shorten(it["title"]),
        "official": it["title"], "gist": clean(it.get("gist") or ""),
        "line": lines_map.get(it["id"]) or first_sentence(clean(it.get("gist") or "")),
        "venue": VENUE.get(it["source_id"], it["source_id"]),
        "pdf": it.get("pdf_url"), "page": it.get("page_url"), "pr": it.get("announced_by_pr"),
        "deadline": it.get("deadline"), "flags": it.get("flags", []),
        "memo": MEMOS.get(it["id"]),
    })

# ---- fold announcement documents into the instrument they announce ----
# Same rule already used for TRAI press releases: a notice that merely announces a
# document is not a separate ledger row. Deterministic match on regulator + date +
# the parenthesised subject acronym. Nothing is lost: the notice PDF is linked from
# the surviving row.
ACRO = re.compile(r"\(([A-Z]{2,6})\)")

def fold_notices(rows):
    by_id = {r["id"]: r for r in rows}
    # explicit folds first
    for ann, inst in folds_map.items():
        if ann.startswith("_"):
            continue
        a, i = by_id.get(ann), by_id.get(inst)
        if a and i:
            i["notice"] = a.get("pdf") or a.get("page")
            a["_folded_into"] = inst
    idx = {}
    for r in rows:
        m = ACRO.search(r["official"])
        if m and r["type"] in ("Consultation", "Draft"):
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

# ---- coverage: group live sources by regulator ----
CAD = {"every_run": "2 h", "daily": "daily", "weekly": "weekly", "monthly": "monthly",
       "weekly_mon_fri": "Mon, Fri"}
groups, order = {}, []
for s in registry["sources"]:
    if s["status"] != "live":
        continue
    reg = s["regulator"]
    if reg not in groups:
        groups[reg] = []; order.append(reg)
    vn = clean(s["name"]).split(" (")[0]
    if vn.lower().startswith(reg.lower()):
        vn = vn[len(reg):].strip(" ,")
    vn = (vn[:1].upper() + vn[1:]) if vn else s["name"]
    groups[reg].append({"n": vn, "t": CAD.get(s.get("check", ""), s.get("check", "")),
                        "s": "Live", "st": "ok"})
cov_groups = [{"reg": r, "venues": groups[r]} for r in order]
live_count = sum(len(g["venues"]) for g in cov_groups)

def blind_label(s):
    n = clean(s["name"]).split(" (")[0]
    reg = s["regulator"].split("/")[-1].strip()
    acro = "".join(w[0] for w in s["regulator"].replace("/", " ").split() if w[:1].isupper())
    if n.lower().startswith(reg.lower()) or (len(acro) > 1 and acro in n):
        return n
    return f"{reg} {n}"

blind = [{"n": blind_label(s), "r": s.get("blind_reason", s["status"])}
         for s in registry["sources"] if s["status"] in ("blocked", "watch", "supplement")]
planned = {}
for s in registry["sources"]:
    if s["status"] == "planned":
        planned[s["stratum"]] = planned.get(s["stratum"], 0) + 1
NAMES = {"tech_data": "Technology and data", "media": "Media"}
notlive = [f'{NAMES.get(k, k)}, {v} venues' for k, v in planned.items()]

payload = {
    # Real build time. Never hardcode this: a stated sweep time that did not happen
    # is a false claim about how fresh the ledger is.
    "updated": NOW.strftime("%d %b %Y, %H:%M IST"),
    "today": NOW.strftime("%Y-%m-%d"),
    "rows": rows,
    "signals": signals,
    "coverage": {"groups": cov_groups, "live": live_count, "regs": len(cov_groups),
                 "blind": blind, "notlive": notlive},
    "shelfCount": len(shelf),
    # --- pipeline state: not rendered, read by the scheduled sweep so a fresh
    # session is fully self-contained (source list, tier rules, gates, shelf). ---
    "pipeline": {
        "registry": registry["sources"],
        "classification": registry["classification"],
        "validation_gates": registry["validation_gates"],
        "shelf": shelf,
        "short_titles": {k: v for k, v in overrides.items() if not k.startswith("_")},
        "row_lines": {k: v for k, v in lines_map.items() if not k.startswith("_")},
        "folds": {k: v for k, v in folds_map.items() if not k.startswith("_")},
        "shortener": {"strip_prefixes": STRIP, "cut_at": CUT, "cap": 54,
                      "note": "short = short_titles[id] if present, else strip a known prefix, cut at a known qualifier, cap at 54 chars on a word boundary. line = row_lines[id] if present, else the first sentence of gist capped at 110 chars. No model judgment."},
    },
}
data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

TEMPLATE = r"""<title>TMT Regulatory Radar</title>
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
  --grid:104px 78px minmax(0,1fr) 132px 116px 24px;
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
.updated{font-family:var(--mono);font-size:11px;letter-spacing:.05em;color:#B9CEDC}
.updated span{color:#fff;font-weight:500}
.tabs{background:var(--navy-d);padding:0 64px;display:flex;gap:2px}
.tabs button{appearance:none;background:none;border:0;border-bottom:3px solid transparent;
  padding:13px 18px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;
  color:#93B2C6;cursor:pointer}
.tabs button.on{color:#fff;border-bottom-color:var(--ochre);background:rgba(255,255,255,.06)}
.tabs button:hover{color:#fff}
.view{display:none;padding:0 64px 90px}
.view.on{display:block}

/* controls */
.controls{margin-top:30px;display:flex;align-items:flex-end;justify-content:space-between;gap:28px;flex-wrap:wrap}
.controls .left{display:flex;align-items:flex-end;gap:26px;flex-wrap:wrap}
#q{width:288px;max-width:100%;border:0;border-bottom:2px solid var(--navy);background:transparent;
  font-size:13px;color:var(--ink);padding:0 0 7px}
#reg,#dates{border:0;border-bottom:2px solid var(--navy);background:transparent;font-size:11px;
  font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--navy);padding:0 0 7px;
  appearance:none;-webkit-appearance:none;cursor:pointer}
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
.ven{display:grid;grid-template-columns:minmax(0,1fr) 60px 74px;column-gap:12px;align-items:center;
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
.bl{display:grid;grid-template-columns:minmax(0,1fr) 232px;column-gap:24px;align-items:baseline;padding:11px 0;
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
</style>

<div class="sheet">
  <div class="head">
    <div class="wordmark">TMT <b>Regulatory Radar</b></div>
    <div class="updated">Last updated <span id="upd"></span></div>
  </div>

  <nav class="tabs">
    <button class="on" data-v="instruments">Instruments</button>
    <button data-v="coverage">Coverage</button>
    <button data-v="signals">Signals</button>
  </nav>

  <section class="view on" id="v-instruments">
    <div class="controls">
      <div class="left">
        <input type="text" id="q" placeholder="Search instruments">
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
        <div class="tog" id="routine" role="checkbox" aria-checked="false" tabindex="0"><i></i><span>Routine</span></div>
      </div>
    </div>
    <div class="tablewrap"><div class="tbl">
      <div class="thead"><div class="r">
        <div>Date</div><div>Regulator</div><div>Instrument</div><div>Type</div><div>Deadline</div><div></div>
      </div></div>
      <div id="rows"></div>
    </div></div>
    <div class="empty" id="empty" style="display:none">No instruments match</div>
  </section>

  <section class="view" id="v-coverage">
    <div class="sechead"><div class="l">Live sources</div><div class="r" id="covcount"></div></div>
    <div class="covgrid" id="covgrid"></div>
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
</div>

<script id="tracker-data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('tracker-data').textContent);
const $ = s => document.querySelector(s);
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const prettyUrl = u => { try { const x = new URL(u); return x.hostname.replace(/^www\./,'') + x.pathname.replace(/\/$/,''); } catch(e) { return u; } };
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmt = iso => { const p = iso.split('-'); return p[2] + ' ' + MON[+p[1]-1] + ' ' + p[0]; };
const days = iso => Math.round((new Date(iso) - new Date(D.today)) / 86400000);

$('#upd').textContent = D.updated;
const state = { q: '', reg: 'All', dates: 'all', routine: false, open: null };

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

/* regulator filter */
const regs = [...new Set(D.rows.map(r => r.reg))].sort();
$('#reg').innerHTML = '<option value="All">All regulators</option>' +
  regs.map(r => '<option value="' + esc(r) + '">' + esc(r) + '</option>').join('');

function dlCell(r) {
  if (!r.deadline) return (r.flags || []).includes('needs_verification')
    ? '<div class="c-dl verify">verify</div>' : '<div class="c-dl none">—</div>';
  const n = days(r.deadline);
  return '<div class="c-dl' + (n >= 0 && n <= 30 ? ' hot' : '') + '">' + fmt(r.deadline) + '</div>';
}
function metaCells(r) {
  const m = [['Source', r.venue], ['Issued', fmt(r.date)]];
  m.push(r.deadline ? [/consult|draft/i.test(r.type) ? 'Comments' : 'Lapses', fmt(r.deadline)] : ['Deadline', 'None stated']);
  m.push(['Status', (r.flags || []).includes('needs_verification') ? 'Gazette pending' : 'On official venue']);
  return m.map(x => '<div><div class="k">' + esc(x[0]) + '</div><div class="v">' + esc(x[1]) + '</div></div>').join('');
}
function acts(r) {
  const a = [];
  if (r.pdf) a.push('<a href="' + esc(r.pdf) + '" target="_blank" rel="noopener">Official text</a>');
  if (r.page && r.page !== r.pdf) a.push('<a href="' + esc(r.page) + '" target="_blank" rel="noopener">Source page</a>');
  if (r.pr) a.push('<a href="' + esc(r.pr) + '" target="_blank" rel="noopener">Announcement</a>');
  if (r.notice) a.push('<a href="' + esc(r.notice) + '" target="_blank" rel="noopener">Consultation notice</a>');
  if (r.memo) a.push('<a href="' + esc(r.memo) + '">Draft memo</a>');
  if (!a.length) a.push('<span style="font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.14em;color:#8F9396">No linkable copy yet</span>');
  return a.join('<span class="sep"></span>');
}

function render() {
  const q = state.q.trim().toLowerCase();
  const b = dateBounds(state.dates);
  const list = D.rows.filter(r =>
    (!b || (r.date >= b[0] && r.date <= b[1])) &&
    (state.routine || !r.routine) &&
    (state.reg === 'All' || r.reg === state.reg) &&
    (!q || (r.short + ' ' + r.line + ' ' + r.official + ' ' + r.reg + ' ' + r.type + ' ' + r.gist).toLowerCase().includes(q))
  );
  $('#rows').innerHTML = list.map(r => {
    const open = state.open === r.id;
    return '<div class="row' + (r.routine ? ' routine' : '') + (open ? ' open' : '') + '" data-id="' + r.id + '">' +
      '<div class="line" tabindex="0" role="button" aria-expanded="' + open + '">' +
        '<div class="c-date">' + fmt(r.date) + '</div>' +
        '<div class="c-reg">' + esc(r.reg) + '</div>' +
        '<div class="c-title">' +
          ((r.pdf || r.page)
            ? '<a class="t" href="' + esc(r.pdf || r.page) + '" target="_blank" rel="noopener" title="Open the document">' + esc(r.short) + '</a>'
            : '<span class="t">' + esc(r.short) + '</span>') +
          (r.line ? '<span class="sub">' + esc(r.line) + '</span>' : '') +
          '<div class="peek"><div class="lbl">Official title</div><div class="full">' + esc(r.official) + '</div>' +
          '<div class="foot"><span>' + esc(r.venue) + '</span><span>' + (r.pdf ? 'PDF on file' : 'No PDF linked') + '</span></div></div>' +
        '</div>' +
        '<div class="c-type"><span class="' + (/draft|consult/i.test(r.type) ? 'draft' : (r.routine ? 'quiet' : '')) +
          '">' + esc((r.type || '').replace(/_/g, ' ')) + '</span></div>' +
        dlCell(r) +
        '<div class="c-mark">' + (open ? '−' : '+') + '</div>' +
      '</div>' +
      '<div class="detail"><div class="lbl">Official title</div><div class="full">' + esc(r.official) + '</div>' +
        '<div class="meta">' + metaCells(r) + '</div>' +
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
$('#reg').addEventListener('change', e => { state.reg = e.target.value; render(); });
$('#dates').addEventListener('change', e => { state.dates = e.target.value; render(); });
const rtog = $('#routine');
const flipRoutine = () => {
  state.routine = !state.routine;
  rtog.classList.toggle('on', state.routine);
  rtog.setAttribute('aria-checked', String(state.routine));
  render();
};
rtog.addEventListener('click', flipRoutine);
rtog.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flipRoutine(); } });
render();

/* coverage */
$('#covcount').textContent = D.coverage.regs + ' regulators, ' + D.coverage.live + ' venues';
$('#covgrid').innerHTML = D.coverage.groups.map(g =>
  '<div class="grp"><div class="h"><b>' + esc(g.reg) + '</b><i>' + g.venues.length + ' venues</i></div>' +
  g.venues.map(v => '<div class="ven"><div class="n" title="' + esc(v.n) + '">' + esc(v.n) + '</div>' +
    '<div class="t">' + esc(v.t) + '</div><div class="s"><span>' + esc(v.s) + '</span><span class="dot"></span></div></div>').join('') +
  '</div>').join('');
$('#blind').innerHTML = D.coverage.blind.map(b =>
  '<div class="bl"><div class="n">' + esc(b.n) + '</div><div class="r">' + esc(b.r) + '</div></div>').join('');
$('#notlive').innerHTML = D.coverage.notlive.map(n => '<div class="nl">' + esc(n) + '</div>').join('');

/* signals */
$('#sigs').innerHTML = D.signals.map(s =>
  '<div class="sig"><div class="h"><span class="badge">Unpublished</span><span class="vbar"></span>' +
  '<span class="d">' + esc(s.date_reported) + '</span><span class="vbar"></span>' +
  '<span class="b">' + esc(s.issuing_body) + '</span></div>' +
  '<div class="line">' + esc(s.title) + '</div>' +
  '<div class="src"><span class="lbl">Source</span><a href="' + esc(s.secondary_url) + '" target="_blank" rel="noopener">' +
  esc((s.secondary_url || '').replace(/^https?:\/\/(www\.)?/, '').split('/')[0]) + '</a></div></div>').join('');
</script>
"""

html = TEMPLATE.replace("__DATA__", data_json)
(DIST / "tmt-radar.html").write_text(html)
print(f"wrote {DIST/'tmt-radar.html'} ({len(html):,} bytes)")
print(f"rows={len(rows)} folded={folded_n} live_venues={live_count} regs={len(cov_groups)} blind={len(blind)} notlive={notlive}")
