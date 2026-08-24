#!/usr/bin/env python3
"""Renders the source atlas + architecture page. Data in -> single HTML out."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
atlas = json.loads((ROOT / "registry" / "atlas.json").read_text())
DIST = ROOT / "dist"; DIST.mkdir(exist_ok=True)

KEEP = ("id","regulator","name","url","publishes","fetch_status","machine_readable",
        "date_format","pagination","cadence","tmt_relevance","priority","quirks","domain","in_tracker")
venues = [{k: v.get(k, "") for k in KEEP} for v in atlas["venues"]]
payload = {"venues": venues, "notes": atlas["engineering_notes"], "generated": atlas["generated"]}
data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

TEMPLATE = r"""<title>TMT Source Atlas</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&family=Spectral:wght@300;400;500;600&display=swap">
<style>
:root{
  --navy:#003F62; --navy-d:#002B43; --navy-l:#1B6288; --navy-wash:#EAF0F4;
  --ochre:#AA8918; --ochre-wash:#F7F0DC; --alarm:#8A2B1C; --alarm-wash:#F7E9E6;
  --ok:#1B6B4A; --ok-wash:#E6F1EB;
  --ink:#111315; --mute:#4E555A; --faint:#7C848A; --ghost:#8F9396; --off:#A3A6A8;
  --paper:#FFFFFF; --ground:#E4E8EA; --panel:#F4F6F7; --rule:#C9D1D6; --rule2:#DDE3E7; --rule3:#EBEFF1;
  --serif:Spectral,Georgia,'Times New Roman',serif;
  --sans:'IBM Plex Sans',system-ui,-apple-system,Segoe UI,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --grid:238px minmax(0,1fr);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--ground)}
body{font-family:var(--sans);color:var(--ink);-webkit-font-smoothing:antialiased}
a{color:var(--navy);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:3px}
a:focus-visible,[tabindex]:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--navy);outline-offset:2px}
input,select,button{font-family:inherit}
input:focus,select:focus{outline:none}
input::placeholder{color:var(--ghost)}
.sheet{max-width:1440px;margin:0 auto;background:var(--paper);min-height:100vh}

.band{background:var(--navy);color:#fff;padding:26px 64px 22px}
.band .in{display:flex;align-items:baseline;justify-content:space-between;gap:24px;flex-wrap:wrap}
.wordmark{font-family:var(--serif);font-weight:400;font-size:29px;letter-spacing:.005em;color:#fff}
.wordmark b{font-weight:600}
.band .meta{font-family:var(--mono);font-size:11px;letter-spacing:.05em;color:#B9CEDC;text-align:right;line-height:1.7}
.band .meta b{color:#fff;font-weight:500}
.tabs{background:var(--navy-d);padding:0 64px;display:flex;gap:2px}
.tabs button{appearance:none;background:none;border:0;border-bottom:3px solid transparent;
  padding:13px 18px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;
  color:#93B2C6;cursor:pointer}
.tabs button.on{color:#fff;border-bottom-color:var(--ochre);background:rgba(255,255,255,.06)}
.tabs button:hover{color:#fff}
.view{display:none;padding:0 64px 90px}
.view.on{display:block}

.controls{margin-top:28px;display:flex;align-items:flex-end;justify-content:space-between;gap:28px;flex-wrap:wrap}
.controls .left{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap}
#q{width:262px;max-width:100%;border:0;border-bottom:2px solid var(--navy);background:transparent;font-size:13px;color:var(--ink);padding:0 0 7px}
#dom,#reach{border:0;border-bottom:2px solid var(--navy);background:transparent;font-size:11px;font-weight:600;
  letter-spacing:.1em;text-transform:uppercase;color:var(--navy);padding:0 0 7px;appearance:none;-webkit-appearance:none;cursor:pointer}
#dom{width:236px}#reach{width:174px}
.tog{cursor:pointer;display:flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid var(--rule);
  background:transparent;color:var(--faint);font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.06em}
.tog.on{border-color:var(--navy);background:var(--navy);color:#fff}
.count{font-family:var(--mono);font-size:11px;letter-spacing:.06em;color:var(--faint)}

.tablewrap{overflow-x:auto}.tbl{min-width:1060px}
.thead{margin-top:22px;border-bottom:2px solid var(--navy)}
.thead .r{display:grid;grid-template-columns:var(--grid);column-gap:20px;padding:0 0 9px 4px;
  font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.16em;color:var(--navy);font-weight:600}
.grouphead{display:flex;align-items:baseline;gap:14px;padding:30px 4px 8px;border-bottom:2px solid var(--ochre);margin-top:10px}
.grouphead b{font-family:var(--serif);font-size:20px;font-weight:600;color:var(--navy)}
.grouphead span{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--ochre);font-weight:600}
.grouphead i{flex:1}
.row{border-bottom:1px solid var(--rule3);background:var(--paper)}
.row:hover{background:var(--navy-wash)}
.row .line{display:grid;grid-template-columns:var(--grid);column-gap:20px;align-items:baseline;padding:13px 0 14px 4px}
.c-reg{font-size:12px;font-weight:600;letter-spacing:.04em;color:var(--navy);line-height:1.4}
.c-name{min-width:0}
.c-name>.t{font-family:var(--serif);font-size:16.5px;font-weight:500;color:var(--ink);
  border-bottom:1px dotted var(--rule);padding-bottom:2px;text-decoration:none}
a.t:hover{color:var(--navy);border-bottom-color:var(--navy);border-bottom-style:solid;text-decoration:none}
.c-name>.sub{display:block;margin-top:4px;font-size:12.5px;line-height:1.44;color:var(--mute);max-width:70ch}
.empty{padding:34px 0 0 4px;font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--ghost)}

.arch{max-width:104ch;padding-top:34px}
.arch h2{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.18em;color:var(--navy);
  font-weight:600;margin:46px 0 14px;padding-top:12px;border-top:2px solid var(--navy)}
.arch h2:first-child{margin-top:0}
.arch h3{font-family:var(--serif);font-size:19.5px;font-weight:600;margin:26px 0 8px;color:var(--navy)}
.arch p{font-family:var(--serif);font-size:16.5px;line-height:1.62;color:#25292B;margin:0 0 14px;max-width:78ch}
.arch p.lead{font-size:18px;color:var(--ink)}
.arch ul{margin:0 0 16px;padding-left:0;list-style:none;max-width:80ch}
.arch li{font-family:var(--serif);font-size:16px;line-height:1.56;color:#25292B;padding:8px 0 8px 22px;
  border-bottom:1px solid var(--rule3);position:relative}
.arch li:before{content:"";position:absolute;left:2px;top:17px;width:8px;height:2px;background:var(--ochre)}
.arch b{font-weight:600}
figure{margin:26px 0 8px}
figure svg{width:100%;height:auto;max-width:100%;color:var(--navy)}
figcaption{font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;color:var(--faint);margin-top:12px;line-height:1.65}
.need{display:grid;grid-template-columns:180px minmax(0,1fr);column-gap:24px;padding:12px 0;border-bottom:1px solid var(--rule2);align-items:baseline}
.need .k{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.06em;color:var(--navy)}
.need .k.gap{color:var(--alarm)}
.need .v{font-family:var(--serif);font-size:15.5px;line-height:1.52;color:#25292B}
.pull{border-left:4px solid var(--alarm);background:var(--alarm-wash);padding:14px 20px;margin:22px 0;
  font-family:var(--serif);font-size:16px;line-height:1.55;max-width:80ch}
.notes li{font-family:var(--sans);font-size:13.5px;line-height:1.55;color:var(--mute)}
@media (max-width:760px){.band,.tabs,.view{padding-left:22px;padding-right:22px}
  .need{grid-template-columns:1fr;row-gap:4px}.dl{grid-template-columns:1fr;row-gap:4px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>

<div class="sheet">
  <div class="band"><div class="in">
    <div class="wordmark">TMT <b>Source Atlas</b></div>
    <div class="meta"><b id="n"></b> venues, every one verified by live fetch<br><span id="g"></span></div>
  </div></div>
  <nav class="tabs">
    <button class="on" data-v="sources">Sources</button>
    <button data-v="architecture">Architecture</button>
    <button data-v="notes">Engineering notes</button>
  </nav>

  <section class="view on" id="v-sources">
    <div class="controls">
      <div class="left">
        <input type="text" id="q" placeholder="Search regulator, page or purpose">
        <select id="dom"></select>
      </div>
      <div style="display:flex;align-items:center;gap:16px">
        <span class="count" id="count"></span>
        <div class="tog on" id="corechip" role="checkbox" aria-checked="true" tabindex="0"><span>Core only</span></div>
      </div>
    </div>
    <div class="tablewrap"><div class="tbl">
      <div class="thead"><div class="r">
        <div>Regulator</div><div>Page</div>
      </div></div>
      <div id="rows"></div>
    </div></div>
    <div class="empty" id="empty" style="display:none">Nothing matches</div>
  </section>

  <section class="view" id="v-architecture"><div class="arch" id="arch"></div></section>
  <section class="view" id="v-notes"><div class="arch"><h2>What breaks a scraper</h2>
    <p>Findings from fetching all of them. Each one is a defect a naive build walks into.</p>
    <ul class="notes" id="notelist"></ul></div></section>
</div>

<script id="atlas-data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('atlas-data').textContent);
const $ = s => document.querySelector(s);
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('#n').textContent = D.venues.length;
$('#g').textContent = D.generated;

document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('on', v.id === 'v-' + b.dataset.v));
}));

const DOMAINS = [...new Set(D.venues.map(v => v.domain))].sort();
$('#dom').innerHTML = '<option value="all">All domains</option>' +
  DOMAINS.map(d => '<option value="' + esc(d) + '">' + esc(d) + '</option>').join('');

const state = { q:'', dom:'all', core:true };

function render(){
  const q = state.q.trim().toLowerCase();
  const list = D.venues.filter(v =>
    (!state.core || v.priority === 'core') &&
    (state.dom === 'all' || v.domain === state.dom) &&
    (!q || (v.regulator+' '+v.name+' '+v.publishes+' '+v.tmt_relevance+' '+v.url).toLowerCase().includes(q))
  );
  let html = '', lastDom = null;
  for (const v of list) {
    if (v.domain !== lastDom) {
      lastDom = v.domain;
      const n = list.filter(x => x.domain === lastDom).length;
      html += '<div class="grouphead"><b>' + esc(lastDom) + '</b><span>' + n + ' venues</span><i></i></div>';
    }
    html += '<div class="row">' +
      '<div class="line">' +
        '<div class="c-reg">' + esc(v.regulator) + '</div>' +
        '<div class="c-name">' +
          '<a class="t" href="' + esc(v.url) + '" target="_blank" rel="noopener" title="' + esc(v.url) + '">' + esc(v.name) + '</a>' +
          (v.in_tracker === 'live' ? '<span class="wired">WATCHED</span>' : '') +
          (v.publishes ? '<span class="sub">' + esc(v.publishes) + '</span>' : '') +
        '</div>' +
      '</div></div>';
  }
  $('#rows').innerHTML = html;
  $('#empty').style.display = list.length ? 'none' : 'block';
  $('#count').textContent = list.length + ' of ' + D.venues.length;
}

$('#q').addEventListener('input', e => { state.q = e.target.value; render(); });
$('#dom').addEventListener('change', e => { state.dom = e.target.value; render(); });
const chip = $('#corechip');
const flip = () => { state.core = !state.core; chip.classList.toggle('on', state.core);
  chip.setAttribute('aria-checked', String(state.core)); render(); };
chip.addEventListener('click', flip);
chip.addEventListener('keydown', e => { if (e.key==='Enter'||e.key===' ') { e.preventDefault(); flip(); } });
render();

$('#notelist').innerHTML = D.notes.map(n => '<li>' + esc(n) + '</li>').join('');
$('#arch').innerHTML = ARCH;
</script>
"""

ARCH = r"""
<h2>What this is</h2>
<p class="lead">A regulatory tracker is not a scraper. It is six mechanisms stacked on each other, and
the tracker is only as trustworthy as the weakest one. This page describes each layer, what it must
guarantee, and where it is currently allowed to fail.</p>

<figure>
<svg viewBox="0 0 1200 460" role="img" aria-label="Pipeline: the registry drives fetch, extract, a validate gate, identity and shaping, and only then the ledger. Rejected rows go to quarantine. The gazette lane joins at the same validate gate and its items are flagged for verification. The signal lane terminates in a separate signals store that never joins the ledger. Memo drafting and alerting draw from the ledger only.">
  <defs>
    <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker>
    <marker id="ard" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#8A2B1C"/></marker>
  </defs>
  <g font-family="'IBM Plex Mono',monospace" font-size="11.5" fill="currentColor">

    <rect x="20" y="150" width="120" height="58" fill="none" stroke="currentColor" stroke-width="1.4"/>
    <text x="80" y="174" text-anchor="middle" font-weight="600">REGISTRY</text>
    <text x="80" y="192" text-anchor="middle" font-size="10.5" opacity="0.7">258 venues</text>
    <line x1="140" y1="179" x2="176" y2="179" stroke="currentColor" stroke-width="1.4" marker-end="url(#ar)"/>
    <text x="158" y="169" text-anchor="middle" font-size="9.5" opacity="0.65">reads</text>

    <rect x="180" y="150" width="110" height="58" fill="none" stroke="currentColor" stroke-width="1.4"/>
    <text x="235" y="174" text-anchor="middle" font-weight="600">FETCH</text>
    <text x="235" y="192" text-anchor="middle" font-size="10.5" opacity="0.7">http or browser</text>
    <line x1="290" y1="179" x2="326" y2="179" stroke="currentColor" stroke-width="1.4" marker-end="url(#ar)"/>
    <text x="308" y="169" text-anchor="middle" font-size="9.5" opacity="0.65">html</text>

    <rect x="330" y="150" width="110" height="58" fill="none" stroke="currentColor" stroke-width="1.4"/>
    <text x="385" y="174" text-anchor="middle" font-weight="600">EXTRACT</text>
    <text x="385" y="192" text-anchor="middle" font-size="10.5" opacity="0.7">date, title, link</text>
    <line x1="440" y1="179" x2="476" y2="179" stroke="currentColor" stroke-width="1.4" marker-end="url(#ar)"/>
    <text x="458" y="169" text-anchor="middle" font-size="9.5" opacity="0.65">rows</text>

    <rect x="480" y="150" width="120" height="58" fill="none" stroke="#8A2B1C" stroke-width="2"/>
    <text x="540" y="174" text-anchor="middle" font-weight="600" fill="#8A2B1C">VALIDATE</text>
    <text x="540" y="192" text-anchor="middle" font-size="10.5" fill="#8A2B1C" opacity="0.85">the gate</text>

    <rect x="420" y="40" width="240" height="46" fill="none" stroke="currentColor" stroke-width="1" stroke-dasharray="5 3"/>
    <text x="540" y="60" text-anchor="middle" font-size="11">GAZETTE LANE</text>
    <text x="540" y="76" text-anchor="middle" font-size="10" opacity="0.7">what regulator sites have not posted yet</text>
    <line x1="540" y1="86" x2="540" y2="146" stroke="currentColor" stroke-width="1.2" stroke-dasharray="5 3" marker-end="url(#ar)"/>
    <text x="552" y="120" font-size="9.5" opacity="0.7">same gate, flagged</text>

    <line x1="540" y1="208" x2="540" y2="254" stroke="#8A2B1C" stroke-width="1.4" marker-end="url(#ard)"/>
    <text x="552" y="236" font-size="9.5" fill="#8A2B1C">rejects</text>
    <rect x="480" y="256" width="120" height="34" fill="none" stroke="#8A2B1C" stroke-width="1" stroke-dasharray="4 3"/>
    <text x="540" y="278" text-anchor="middle" font-size="10.5" fill="#8A2B1C">quarantine</text>

    <line x1="600" y1="179" x2="636" y2="179" stroke="currentColor" stroke-width="1.4" marker-end="url(#ar)"/>
    <text x="618" y="169" text-anchor="middle" font-size="9.5" opacity="0.65">clean</text>

    <rect x="640" y="150" width="120" height="58" fill="none" stroke="currentColor" stroke-width="1.4"/>
    <text x="700" y="174" text-anchor="middle" font-weight="600">IDENTIFY</text>
    <text x="700" y="192" text-anchor="middle" font-size="10.5" opacity="0.7">hash, dedupe</text>
    <line x1="760" y1="179" x2="796" y2="179" stroke="currentColor" stroke-width="1.4" marker-end="url(#ar)"/>
    <text x="778" y="169" text-anchor="middle" font-size="9.5" opacity="0.65">new only</text>

    <rect x="800" y="150" width="120" height="58" fill="none" stroke="currentColor" stroke-width="1.4"/>
    <text x="860" y="174" text-anchor="middle" font-weight="600">SHAPE</text>
    <text x="860" y="192" text-anchor="middle" font-size="10.5" opacity="0.7">classify, fold</text>
    <line x1="920" y1="179" x2="996" y2="179" stroke="currentColor" stroke-width="1.4" marker-end="url(#ar)"/>
    <text x="958" y="169" text-anchor="middle" font-size="9.5" opacity="0.65">append</text>

    <rect x="1000" y="150" width="130" height="58" fill="none" stroke="currentColor" stroke-width="2"/>
    <text x="1065" y="174" text-anchor="middle" font-weight="600">LEDGER</text>
    <text x="1065" y="192" text-anchor="middle" font-size="10.5" opacity="0.7">append only</text>

    <rect x="420" y="330" width="240" height="46" fill="none" stroke="#8A2B1C" stroke-width="1" stroke-dasharray="5 3"/>
    <text x="540" y="350" text-anchor="middle" font-size="11" fill="#8A2B1C">SIGNAL LANE</text>
    <text x="540" y="366" text-anchor="middle" font-size="10" fill="#8A2B1C" opacity="0.8">instruments never published anywhere</text>
    <line x1="660" y1="353" x2="736" y2="353" stroke="#8A2B1C" stroke-width="1.2" stroke-dasharray="5 3" marker-end="url(#ard)"/>

    <rect x="740" y="330" width="140" height="46" fill="none" stroke="#8A2B1C" stroke-width="2" stroke-dasharray="5 3"/>
    <text x="810" y="350" text-anchor="middle" font-size="11" font-weight="600" fill="#8A2B1C">SIGNALS</text>
    <text x="810" y="366" text-anchor="middle" font-size="10" fill="#8A2B1C" opacity="0.8">separate store</text>
    <text x="810" y="396" text-anchor="middle" font-size="9.5" fill="#8A2B1C" opacity="0.85">no path into the ledger</text>

    <path d="M1030,208 L1030,382 L965,382 L965,406" fill="none" stroke="currentColor" stroke-width="1.2" marker-end="url(#ar)"/>
    <path d="M1100,208 L1100,382 L1120,382 L1120,406" fill="none" stroke="currentColor" stroke-width="1.2" marker-end="url(#ar)"/>
    <text x="1065" y="374" text-anchor="middle" font-size="9.5" opacity="0.7">new only</text>

    <rect x="895" y="408" width="140" height="32" fill="none" stroke="currentColor" stroke-width="1.4"/>
    <text x="965" y="428" text-anchor="middle" font-size="11">MEMO DRAFT</text>
    <rect x="1060" y="408" width="120" height="32" fill="none" stroke="currentColor" stroke-width="1.4"/>
    <text x="1120" y="428" text-anchor="middle" font-size="11">ALERT</text>
  </g>
</svg>
<figcaption>The main path runs left to right. The validate gate is the only place a row is refused,
and what it refuses goes to quarantine rather than disappearing. The gazette lane feeds through that
same gate, because an instrument found on the gazette still has to be a well-formed row; it carries a
verification flag onward. The signal lane is different: it ends in its own store with no path into the
ledger at all, because a press report of an unpublished letter is a lead, not an instrument. Memo
drafting and alerting read the ledger only.</figcaption>
</figure>

<h2>The six layers</h2>

<h3>1. Registry — what to watch</h3>
<p>A list of venues with, for each, the URL that actually shows dated rows, the date formats that
appear there, the domains a link is allowed to point at, how to page, and every known quirk.
Everything downstream is driven by this file, which is why adding a regulator is a config change
rather than a code change. It also has to record what <em>cannot</em> be reached, because an
unlisted blind spot is indistinguishable from a covered one.</p>

<h3>2. Fetch — reaching the page</h3>
<p>Roughly a quarter of Indian regulatory venues return an empty shell to a plain HTTP client. MeitY,
DPIIT, CBIC and the income-tax site are JavaScript applications; the Supreme Court's search sits
behind a CAPTCHA; several sites serve broken TLS chains or block non-browser user agents. A tracker
that only speaks HTTP silently covers three quarters of the landscape.</p>
<div class="pull">The dangerous failure is not a timeout. It is a page that returns HTTP 200 with an
empty body, or a listing whose rows load by AJAX after the HTML arrives. Both look like a healthy poll
that found nothing new. Any fetch that yields zero rows from a venue that normally has rows must be
treated as a failure, not as an absence of news.</div>

<h3>3. Extract — pulling rows out</h3>
<p>Per venue: find the dated rows, and for each one take the date, the title and the document link.
Dates arrive in at least six formats across these venues, and several listings carry no date column
at all, so those can only be watched by diffing snapshots or hashing the linked PDFs.</p>

<h3>4. Validate — the gate</h3>
<p>Before anything joins the ledger it must pass fixed checks: the date parses against the formats
that venue declares, the link points at a domain that venue is allowed to use, the title is a
plausible length. Rows that fail go to quarantine with a reason attached. This is the layer that
makes the difference between a tracker and a rumour mill.</p>

<h3>5. Identify — is this new?</h3>
<p>A stable identity per instrument, computed from the normalised title plus the date, so the same
document seen twice on two venues is one entry. Without this, every sweep re-reports everything.</p>

<h3>6. Shape — making it readable</h3>
<p>Three deterministic steps that turn a correct ledger into one a partner can scan: a short display
title, because official Indian instrument titles run past thirty words; a single factual line saying
what the instrument does, because four authorisation rules notified on the same day are otherwise
indistinguishable; and a fold, so a notice that merely announces a document does not become a second
row alongside it.</p>

<h2>Why two extra lanes exist</h2>
<p>The main path can only find what regulators publish. Two categories escape it, and both were
observed in the July to August test window.</p>
<ul>
<li><b>Published elsewhere first.</b> The gazette is the legal source of truth, and regulator sites
lag it. Two sets of telecom rules were gazetted and still absent from the department's own rules
shelf two weeks later. The gazette lane catches these and flags them as needing verification.</li>
<li><b>Never published at all.</b> A large share of Indian regulatory instruments are letters:
advisories served on named companies, directions to service providers, blocking orders, ministry
references. They reach the public through the press or civil society, if at all. The signal lane
watches those sources and records what it finds as clearly unofficial leads.</li>
</ul>

<h2>What it takes to run</h2>
<div class="need"><div class="k">Network path</div><div class="v">A machine that can reach gov.in hosts. Cloud sandboxes generally cannot, which is why the working system runs a local extractor and uses a cloud session only as a fetch-and-copy transport.</div></div>
<div class="need"><div class="k">Headless browser</div><div class="v">Required for the quarter of venues that render client-side. Without it, MeitY, DPIIT and the tax sites are permanently invisible.</div></div>
<div class="need"><div class="k">Scheduler</div><div class="v">Something that fires on a cadence and, critically, complains when a run fails. A silent scheduler is worse than none.</div></div>
<div class="need"><div class="k">State store</div><div class="v">An append-only ledger plus per-source health. History is never rewritten, so a missed item stays visible as a gap.</div></div>
<div class="need"><div class="k">PDF text extraction</div><div class="v">Instruments are PDFs. Memo drafting needs their operative text, and some scanned ones have no text layer at all.</div></div>
<div class="need"><div class="k">Document assembly</div><div class="v">A fixed template filled from extracted fields, so a memo is a mechanical fill rather than generated prose.</div></div>
<div class="need"><div class="k">Audit scripts</div><div class="v">Automated assertions that the ledger agrees with its own rules. This is what catches classification drift before a partner does.</div></div>
<div class="need"><div class="k gap">A human</div><div class="v">Every interpretive line in a memo carries an associate-review marker, and every instrument is verified against the gazette text before it reaches a client. The pipeline finds and drafts; it does not advise.</div></div>

<h2>Where automation stops</h2>
<ul>
<li><b>Court dockets.</b> High Court and Supreme Court listings sit behind CAPTCHAs and POST forms.
Judgments are reachable; comprehensive case tracking realistically needs a paid docket service.</li>
<li><b>Letter-form instruments.</b> Nothing can scrape a document that was never posted. The signal
lane surfaces leads; confirmation is a phone call.</li>
<li><b>Stale-but-healthy sites.</b> Two venues were serving content weeks or years out of date while
returning perfectly valid responses. No amount of scraping detects this; only cross-checking against
a second source does.</li>
<li><b>State instruments.</b> Thirty-odd state gazettes, most without usable listings. Worth watching
the handful of states that actually legislate on gaming and platforms, not all of them.</li>
</ul>
"""

html = TEMPLATE.replace("__DATA__", data_json).replace("$('#arch').innerHTML = ARCH;",
        "$('#arch').innerHTML = " + json.dumps(ARCH) + ";")
(DIST / "tmt-atlas.html").write_text(html)
print(f"wrote {DIST/'tmt-atlas.html'} ({len(html):,} bytes) | venues={len(venues)} notes={len(payload['notes'])}")
