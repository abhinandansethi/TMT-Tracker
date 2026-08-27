export const meta = {
  name: 'tmt-integration-verify',
  description: 'Adversarially verify each newly-wired TMT source (fetch/parse/filter/dual-link/lane/legal), then a completeness critic',
  phases: [{ title: 'Verify' }, { title: 'Critic' }],
}
const ROOT = args.root
const UA = 'Mozilla/5.0 (compatible; TMTRegulatoryRadar/2.0; Trilegal internal regulatory monitoring)'

const VERDICT = {
  type: 'object',
  required: ['source','reachable_200','parse_ok','filter_sound','dual_link_ok','lane_correct','legal_consistent','verdict','issues','evidence'],
  properties: {
    source: { type: 'string' },
    reachable_200: { type: 'boolean', description: 'live GET with the honest UA returns 200 usable content' },
    parse_ok: { type: 'boolean', description: 'the engine parser extracts real rows with correct date/title/doc-url' },
    filter_sound: { type: 'boolean', description: 'the TMT row_filter keeps genuinely-TMT rows and drops off-topic ones without egregious false positives/negatives' },
    dual_link_ok: { type: 'boolean', description: 'ledgered items carry BOTH a document url and a landing page_url' },
    lane_correct: { type: 'boolean', description: 'instruments vs judgments assignment is defensible' },
    legal_consistent: { type: 'boolean', description: 'access(scraping)+copyright posture in docs/TMT-Radar-legal-basis.pdf / legal_analysis json matches how the engine actually fetches (esp. the honest non-triggering UA, no browser spoof)' },
    verdict: { type: 'string', enum: ['SOUND','FLAWED'] },
    issues: { type: 'array', items: { type: 'string' }, description: 'concrete defects found; empty if none' },
    evidence: { type: 'string', description: 'commands run and what they returned, verbatim where possible' },
  },
}

phase('Verify')
const SOURCES = [
  { id: 'cci_orders', endpoint: 'https://www.cci.gov.in/antitrust/orders/list', lane: 'judgments', hdr: '-H "X-Requested-With: XMLHttpRequest"' },
  { id: 'ccpa_orders', endpoint: 'https://ccpa.doca.gov.in/ccpa-orders.php?page_no=1', lane: 'instruments', hdr: '' },
  { id: 'dpiit_press_notes', endpoint: 'https://www.dpiit.gov.in/cms/wp-json/wp/v2/documents?search=press%20note&per_page=100&orderby=date&order=desc', lane: 'instruments', hdr: '' },
  { id: 'sc_judgments', endpoint: 'https://www.sci.gov.in/latest-orders/', lane: 'judgments', hdr: '' },
  { id: 'delhihc_judgments', endpoint: 'https://delhihighcourt.nic.in/web/judgement/fetch-data', lane: 'judgments', hdr: '' },
]

const verdicts = await parallel(SOURCES.map(s => () => agent(
`You are an ADVERSARIAL verifier for a deterministic regulatory tracker. Your job is to REFUTE the claim that source "${s.id}" is correctly wired. Assume it is broken until the bytes prove otherwise. Work in ${ROOT}.

The engine is at ${ROOT}/engine. Registry: engine/registry_v2.json (find the "${s.id}" entry). Parser code: engine/radar/parse.py. Gate: engine/radar/validate.py. Venv python (has bs4/lxml): engine/.venv/bin/python. Fixtures: engine/fixtures/.

The engine's HONEST default User-Agent is exactly: ${UA}  (contact travels in a From header). It is NOT a browser spoof — it names the firm. Several hosts (MeitY/DPIIT/sci.gov.in) 403 crawler-signature UAs but serve this form 200.

Do ALL of this and report the schema:
1. LIVE FETCH: curl -s -o /tmp/v_${s.id} -w "%{http_code}" --max-time 30 -A "${UA}" ${s.hdr} "${s.endpoint}"  — confirm 200 and non-trivial bytes. If it 403s or is empty, reachable_200=false with the code.
2. PARSE: run the engine's real parser against the live bytes AND the saved fixture, e.g.:
   cd ${ROOT}/engine && .venv/bin/python -c "import sys;sys.path.insert(0,'.');from radar import parse,validate as vl;import json;from datetime import date;reg=json.load(open('registry_v2.json'));s=[x for x in reg['sources'] if x['id']=='${s.id}'][0];rows=parse.parse(s, open('/tmp/v_${s.id}','rb').read(), s['url']);print('rows',len(rows));[print(r.get('date'),'|',r['title'][:70],'|',(r.get('url') or '')[:60]) for r in rows[:5]]"
   Confirm real dates/titles/doc-urls (not invented). Cross-check 2-3 rows by eye against the raw bytes.
3. FILTER: the registry entry has a row_filter regex (TMT). Apply it to the parsed rows. Does it keep genuinely-TMT rows (telecom/tech/media/e-commerce/named TMT companies) and drop off-topic ones? Look hard for FALSE POSITIVES (e.g. a school or institute matching a company token) and FALSE NEGATIVES. Report specific examples.
4. DUAL-LINK: in the engine sqlite ledger (engine/ledger.db, table items) do this source's rows carry BOTH url (document) and page_url (landing)? Run: sqlite3 ${ROOT}/engine/ledger.db "select url,page_url from items where source_id='${s.id}' limit 5;"  (if sqlite3 missing, use the venv python sqlite3 module). Both columns must be populated and distinct-ish.
5. LANE: expected lane is "${s.lane}". Is that defensible (instruments=binds a client; judgments=tribunal/court decision)? Argue the other side.
6. LEGAL: read the "${s.id}" entry in engine/audit/legal_analysis_2026-08-27.json (per_source + signoff.approved). Does its access+copyright reasoning match how the engine actually fetches — especially the honest-UA claim (no browser spoof)? Flag any contradiction.

Never invent a row, date, or result. Quote what you actually ran. If you cannot refute a dimension, mark it true; if you find a real defect, mark it false and put the concrete failure in issues[]. verdict=FLAWED if any material defect.`,
  { label: `verify:${s.id}`, phase: 'Verify', schema: VERDICT })))

phase('Critic')
const critic = await agent(
`You are a COMPLETENESS + CONSISTENCY critic for the TMT tracker integration. The per-source verifiers returned these verdicts (JSON):

${JSON.stringify(verdicts.filter(Boolean), null, 1)}

Now audit the WHOLE integration in ${ROOT}. Read: engine/registry_v2.json, README.md, docs/CONNECTOR.md, and the dashboard payload embedded in dist/tmt-radar-v2.html (the <script id="tracker-data"> JSON). Check and report findings as prose:
1. COVERAGE ⊇ LEDGER: is every source that produced a ledgered item present as a LIVE venue on the coverage page? Is anything in the ledger from a source NOT on the coverage list? (Run the venv python to compare engine/ledger.db source_ids vs the coverage payload.)
2. DUAL-LINK: across ALL items in dist payload rows+judgments, is any missing a doc url or page_url?
3. HONEST-UA CONSISTENCY: fetch.py UA vs Principle 2 in the legal PDF/json — consistent? Any live source still carrying a browser-UA spoof in http_headers?
4. NEEDS_DECISION / PLANNED honesty: pib_moc (needs_decision) and nclat_orders (planned) — are they correctly kept OUT of live coverage and honestly surfaced?
5. README ACCURACY: does README.md state the correct live count (52), manual-trigger (no schedule), the three lanes, and the new sources? Flag any remaining stale claim.
6. MISSING SOURCES: for a TMT partner with a broad practice, is any obviously-important venue still absent that we could lawfully add? Name at most 3, concretely.
Return a prose report: what is SOUND, what is BROKEN (with file:line or exact query), and a final GO / NO-GO for committing this to git.`,
  { label: 'critic:completeness', phase: 'Critic' })

return { verdicts: verdicts.filter(Boolean), critic }
