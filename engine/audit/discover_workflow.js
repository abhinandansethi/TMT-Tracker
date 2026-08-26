export const meta = {
  name: 'tmt-new-source-discovery',
  description: 'Live-verify each legally-cleared new source, capture a fixture, and report an exact adapter spec',
  phases: [{ title: 'Discover', detail: 'one agent per new source' }],
}
const ROOT = args.root, TODAY = args.today
const UA = 'TMT-Regulatory-Radar/2.0 (Trilegal regulatory monitoring; +mailto:compliance@trilegal.com)'
const BROWSER = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

const SPEC = {
  type: 'object', required: ['id', 'reachable', 'strategy', 'endpoint', 'row_structure', 'date_format', 'row_floor', 'allowed_domains', 'sample_rows', 'fixture_saved', 'notes'],
  properties: {
    id: { type: 'string' },
    reachable: { type: 'string', enum: ['OK', 'BLOCKED', 'JS_ONLY', 'MOVED', 'NOT_FOUND', 'TIMEOUT'] },
    strategy: { type: 'string', description: 'which existing engine strategy fits: html_table / link_shelf / json_api (meity-style) / wp_json / regex_rows / rss / needs_new_strategy' },
    endpoint: { type: 'string', description: 'the exact URL to fetch (the working one, after any redirect/correction)' },
    landing_page: { type: 'string', description: 'the human-facing listing page for page_url' },
    row_structure: { type: 'string', description: 'precise: CSS selector + which cell is title/date/link, OR the JSON path to the array and the keys for title/date/doc-url' },
    date_format: { type: 'array', items: { type: 'string' } },
    row_floor: { type: 'integer' },
    pagination: { type: 'string' },
    allowed_domains: { type: 'array', items: { type: 'string' } },
    pdf_pattern: { type: 'string' },
    lane: { type: 'string', enum: ['instruments', 'judgments', 'signals'] },
    default_type: { type: 'string' },
    tmt_filter: { type: 'string', description: 'if the source carries non-TMT matter, the include/exclude regex to keep it TMT-relevant' },
    sample_rows: { type: 'array', items: { type: 'string' }, description: 'first 3 rows verbatim as DATE | TITLE | DOC-URL, copied from the fetched bytes' },
    fixture_saved: { type: 'string', description: 'path under engine/fixtures/ where you saved the raw bytes' },
    hard_rules: { type: 'string', description: 'legal/technical must-nots (e.g. never fetch the robots-Disallowed /images/ dirs; CAPTCHA-free feed only)' },
    notes: { type: 'string' },
  },
}

phase('Discover')

const SOURCES = [
  { id: 'dpiit_press_notes', brief: `DPIIT FDI Press Notes. Cleared: robots 'Allow: /', s.52(1)(q)(i). Fetch the public WordPress CMS API at https://www.dpiit.gov.in/cms/wp-json/wp/v2/documents (try ?per_page=30, and look for a document-type/category filter for "Press Note" / FDI). The SPA shell is empty — use the wp-json API. Landing page https://www.dpiit.gov.in/policies/foreign-direct-investment-policy . Lane instruments, default_type press_note.` },
  { id: 'nclat_orders', brief: `NCLAT orders (competition appellate — hears CCI appeals; TMT-relevant when the appeal is a tech/telecom/media matter). Cleared: express GoI licence + s.52(1)(q)(iv). Fetch nclat.nic.in listings: try https://nclat.nic.in/judgement-data and https://nclat.nic.in/daily-order-data and https://nclat.nic.in/display-board/orders . Find the one that lists recent orders as a table/list with dates and PDF links. Lane judgments, default_type order. Note the PDF may need a token-POST (report the mechanism, don't need to solve).` },
  { id: 'sc_judgments', brief: `Supreme Court of India — CAPTCHA-FREE Latest Orders listing ONLY. Cleared: s.52(1)(q)(iv), include_with_conditions. Fetch https://www.sci.gov.in/latest-orders/ (repoint from the dead main.sci.gov.in). Find the recent-orders table/list with dates and judgment PDF links. HARD RULE: never touch the siwp_captcha case-number search. Lane judgments, default_type judgment. TMT filter: this is a firehose of all SC matters — propose a keyword filter to keep TMT-relevant ones (telecom, broadcasting, intermediary, IT Act, data protection, internet, OTT, copyright, spectrum) OR report that filtering must happen downstream.` },
  { id: 'delhihc_judgments', brief: `Delhi High Court — CAPTCHA-FREE Latest Judgments feed ONLY. Cleared: s.52(1)(q)(iv). Fetch delhihighcourt.nic.in — find the "Latest Judgments" / "Judgments" feed and the direct /files/ PDF pattern. HARD RULE: never the case-status/party-name search. Lane judgments. Same TMT-keyword-filter consideration as SC.` },
  { id: 'cci_orders', brief: `Competition Commission of India orders. Cleared: s.52(1)(q)(iv), include_with_conditions. Consume the DataTables JSON at https://www.cci.gov.in/antitrust/orders/list (and the combination equivalent /combination/orders-section31 or /combination/orders/list). PDFs at /images/antitrustorder/en/. HARD RULE: robots.txt Disallows three /images/ consultation directories — verify robots.txt live and report exactly which /images/ paths are Disallowed; never fetch those. Lane instruments, default_type order.` },
  { id: 'ccpa_orders', brief: `Central Consumer Protection Authority orders/advisories. Cleared include_with_conditions but the host MIGRATED: try ccpa.doca.gov.in (NOT doca.gov.in, which is under maintenance / cert expired 26-Aug-2026 / 403). Require valid TLS. If ccpa.doca.gov.in is not cleanly reachable with a valid cert today, report reachable=BLOCKED/MOVED and recommend deferring (register as planned). Lane instruments, default_type order/advisory.` },
]

const results = await parallel(SOURCES.map(s => () => agent(`You are building an adapter for a deterministic regulatory-tracker engine. Today is ${TODAY}. You are on a Mac with direct internet. Your job: live-verify ONE source, capture its raw bytes as a fixture, and report an exact adapter spec another engineer can wire without re-discovering anything.

Source ${s.id}: ${s.brief}

The engine already has these parser strategies (reuse one if it fits): html_table (CSS row selector + title_cell/date_cell/link_selector), link_shelf (a page of document links), json_api / wp_json (a JSON endpoint with an array of records — like MeitY's wp-json), regex_rows (raw-bytes regex for malformed HTML), rss. Only say needs_new_strategy if none fits.

Do this:
1. Fetch with curl. Prefer an identifying UA "${UA}"; if a WAF blocks it (403), note that and retry with the browser UA "${BROWSER}" and record which worked. curl -sL --max-time 30 -k allowed for TLS-broken gov hosts (note if -k was needed).
2. Save the raw bytes to ${ROOT}/engine/fixtures/${s.id}.html (or .json if JSON). This fixture is required.
3. Fetch the host's robots.txt and confirm the exact path you fetch is not Disallowed (RFC 9309: a 4xx robots = no restriction). Quote any Disallow that matters. Honour every HARD RULE in the brief — do not fetch a Disallowed or CAPTCHA-gated path.
4. Determine the strategy and the precise row structure: for HTML, the CSS selector for one row and which cell holds the title, the date, and the document link; for JSON, the path to the records array and the keys for title / date / document-url. Extract the first 3 real rows BY HAND and report them verbatim as DATE | TITLE | DOC-URL. If you cannot produce 3 real rows, reachable is not OK.
5. Record date formats seen, a realistic row_floor (count real rows on page 1), pagination, allowed_domains (hosts the doc links point to), the PDF URL pattern, and — for the court/CCI firehoses — a TMT include/exclude filter or an honest note that filtering is downstream.

Python3 here is 3.9 stdlib only (no bs4/lxml/requests in the system python); for parsing use the venv at ${ROOT}/engine/.venv/bin/python (has bs4/lxml) or plain regex.

Never invent a row, date, URL or robots rule. Quote what you fetch. If the source is not cleanly reachable today, say so honestly (reachable=BLOCKED/MOVED/JS_ONLY) and recommend deferring. Your final text is data for a pipeline.`, { label: `disc:${s.id}`, phase: 'Discover', schema: SPEC })))

const specs = results.filter(Boolean)
log(`Discovered ${specs.length}/6; reachable OK: ${specs.filter(s => s.reachable === 'OK').map(s => s.id).join(', ')}`)
return { specs }
