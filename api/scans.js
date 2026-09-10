// Vercel serverless function — Create / Run / Delete a scan, promote a Miscellaneous finding into
// its coverage, and report what its runs are doing.
//
// The scan layer (docs/horizon-design.md §3) does all its real work on GitHub Actions, because
// discovery, gating, fetching and enrichment take minutes and hit government sites, which is not
// something a serverless function should be doing under a 60-second clock. This endpoint only
// dispatches .github/workflows/scan.yml with the partner's request bound as workflow inputs; the
// workflow validates the definition again, runs, and commits the results, and Vercel rebuilds
// the page from what was committed. Nothing here claims a scan ran that did not run.
//
// The one exception to "dispatch only" is action:"status", which READS the Actions runs of
// scan.yml so the page can show a dispatched scan as queued/running/finished instead of leaving
// the partner to watch GitHub for twenty minutes. It is still a report of what GitHub says, and
// when GitHub will not say (a token without actions:read), it answers with an empty list and the
// reason rather than an error — the page then falls back to its own elapsed-time card.
//
// Token and repository discovery, CSRF guards and error mapping mirror api/sweep.js exactly —
// same Vercel settings, same 501 when a secret is missing, and no token value ever echoed.
//
// Why the definition travels as a dispatch input rather than a commit from here: a function
// that writes to git needs a token with contents:write and a merge strategy for the race with
// the sweep lane. A dispatch input needs neither — the workflow is the only writer, so every
// definition change is a commit made by the same actor, in order. GitHub caps the whole inputs
// payload at 65,535 characters, which is where the 60,000-character limit on `scan` comes from.
//
// NOTE ON ACCESS: like /api/sweep this inherits the deployment's Edge auth (middleware.js). On an
// unprotected deployment anyone with the URL could create scans that fetch arbitrary official
// sites in the firm's name. Keep Vercel Deployment Protection on.
//
// WHY maxDuration IS 20 SECONDS HERE (vercel.json → functions). The rationale lives in this
// comment because vercel.json cannot hold one: its schema sets additionalProperties:false at the
// top level and inside every function entry, so there is no key to put a note in, and JSON has no
// comments. The numbers there are:
//   api/scans.js  20 — this function makes exactly ONE GitHub API call per request (a dispatch
//                      POST, or the status GET) and does no model work. A second or two is
//                      normal; 20 is generous headroom for a slow GitHub, and it is deliberately
//                      short because these fetches carry no AbortSignal, so maxDuration is the
//                      only thing that ends a hung call — and a partner pressing Run scan should
//                      get an answer or an error, never a minute of spinner.
//   api/sweep.js  20 — the same single dispatch POST to the same API, so the same number for the
//                      same reason. Both were on the platform default (10s on Hobby) before, and
//                      an undeclared default is a number nobody chose.
//   api/ask.js, api/draft.js, api/propose.js, api/discover.js  60 — those call a model (and, for
//                      ask/draft, fetch a source document first) and hold their own inner
//                      timeouts; 60 is the ceiling those timeouts are set to fit inside.
//   api/propose-filter.js  30 — one short model call, no fetching and no search tool: it turns an
//                      intent into the scan's subject_filter regex. Its own MODEL_TIMEOUT_MS is
//                      25 s, so 30 leaves room to answer with a sentence rather than be killed.

const TOKEN_NAMES = [
  'GITHUB_DISPATCH_TOKEN', 'TMT_TOKEN', 'TMT_DISPATCH_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
];
const WORKFLOW = 'scan.yml';
// The status action can report on any workflow this deployment already dispatches, so the
// dashboard's Update now can show the same live phases as a scan. Allow-listed by name, never
// taken from the caller verbatim: a free-text workflow file would be a path the caller chose.
const STATUS_WORKFLOWS = { scan: 'scan.yml', sweep: 'sweep.yml', briefs: 'briefs.yml' };
// "status" is the odd one out: it dispatches nothing, it reads. A scan takes minutes on Actions,
// and until this existed the partner had to watch GitHub to know whether their scan was running,
// finished or had failed. It is on this endpoint rather than a new file because it needs exactly
// the same token and repository discovery.
// "promote" is the Miscellaneous lane's one door into coverage. That lane never fetches anything:
// it reads the hosted web-search tool's results and links out, so nothing in it is a citable
// instrument and nothing in it enters the ledger. When a finding turns out to be an official venue
// the scan does not cover, promoting it adds that URL to the scan's sources — and then the same
// deterministic Python gate that judges every other source decides (fetch with the honest
// User-Agent, robots.txt, terms scan, extraction floor). This endpoint approves nothing; it
// dispatches. Promotion is the ONLY route from Miscellaneous into coverage.
const ACTIONS = ['create', 'run', 'delete', 'status', 'promote'];
// How many runs "status" reports, and how many it asks GitHub for when filtering by scan_id
// (the matching runs may sit behind other scans' runs).
const MAX_RUNS = 10;
// Same shape the workflow and pipeline/scan/common.py insist on, so an id minted here is one the
// pipeline will accept as a file name under scans/ and data/scans/ without any further cleaning.
const SCAN_ID = /^[a-z0-9][a-z0-9-]{1,59}$/;
const MAX_SCAN_JSON = 60000;
// A misc finding's id, as data/scans/<id>/misc.json writes it: a 10-character hash of the URL.
// The id is all a promote carries — the URL, host and kind are read from misc.json by the
// pipeline, so a caller cannot smuggle a different URL in behind an id the partner clicked.
const FINDING_ID = /^[a-f0-9]{10}$/;
// Ids that match SCAN_ID but name files the layer already owns: scans/schema.json is the
// contract, tmt-india is the registry lane. REVIEWED DEFECT: a scan named "Schema" slugged to
// `schema`, and ScanPaths wrote the definition over scans/schema.json (and delete unlinked it).
// pipeline/scan/run.py holds the same set; keep the two identical.
const RESERVED_IDS = new Set(['schema', 'tmt-india']);
// Top-level fields run.validate_definition accepts (its TOP_KEYS; no_discover travels inside the
// definition as well as as a dispatch input). An unknown key fails there with exit 2 two minutes
// after we said "queued", so it fails here first.
// REVIEWED DEFECT: `no_misc` and `discovery_notes` were missing from this list, and an Edit
// re-submits the definition the page read back out of scans/<id>.json. Both are keys the
// pipeline itself writes into every definition, so an Edit of a scan that had the Miscellaneous
// lane switched off was refused outright — or, once the page dropped unknown keys, silently
// re-enabled the lane and threw away discovery's account of what it searched for and did not
// find, which the coverage panel is supposed to keep showing for as long as it is true.
const TOP_KEYS = ['id', 'name', 'intent', 'jurisdictions', 'topics', 'industries', 'clients',
  'sources', 'subject_filter', 'budget', 'demo', 'created', 'updated', 'no_discover', 'no_misc',
  'discovery_notes'];
// subject_filter — what this scan considers ITS SUBJECT, decided at extraction, before anything
// is read. THE DEFECT IT ANSWERS: the first real scan asked for Indian AI regulation and ledgered
// 118 developments of which eight mentioned AI — 103 TRAI telecom listings and 15 CERT-In vendor
// CVE bulletins — because a scan had inherited the machinery for reading a listing but never the
// machinery for deciding what on it is the subject. A regex, not a model call per row, for the
// same reasons engine/registry_v2.json has given every TMT India source a row_filter since the
// beginning: deterministic, visible on the page, editable by the partner, free per row.
// /api/propose-filter proposes one at create time; the partner confirms, edits, or clears it.
// This endpoint only checks the shape — the pipeline compiles it with Python's `re` and applies
// it, and a row whose title is too terse to judge is KEPT and marked, never dropped.
const SUBJECT_FILTER_KEYS = ['regex', 'why', 'source'];
// Who the filter came from. "none" means the partner deliberately turned it off, which the
// definition records rather than forgetting: an absent subject_filter and an explicit "none" both
// keep every row, and only the second says a human decided that.
const SUBJECT_FILTER_SOURCES = ['proposed', 'partner', 'none'];
const MAX_SUBJECT_REGEX = 400;
const MAX_SUBJECT_WHY = 300;
// discovery_notes is written by the pipeline, not typed by a partner, so the cap is only there
// to stop an absurd payload: one note per venue considered is the natural size.
const MAX_DISCOVERY_NOTES = 200;
const MAX_DISCOVERY_NOTE_CHARS = 1000;
const SOURCE_STATUSES = ['approved', 'pending', 'rejected'];
const TIERS = ['vetted', 'discovered'];
// A source may arrive as a bare URL string or as an object. The object shape widened when
// discovery moved into the browser (/api/discover): the Create dialog now sends the venue the
// partner picked *with its evidence* — name, jurisdiction, kind and the one-line rationale — so
// the coverage panel can show why that venue is on the list without re-asking a model. The rest
// are the gate's own fields, accepted because an Edit re-submits a definition that was read back
// out of scans/<id>.json (run._candidate_from_partner discards them; a definition can never
// approve its own source or label itself vetted). This list is run.py's SOURCE_KEYS and
// scans/schema.json's $defs/source properties, in the same order — keep the three identical, and
// refuse anything else here so a typo is a message in the dialog rather than exit 2 in a
// workflow log the partner never opens.
const SOURCE_KEYS = ['url', 'name', 'jurisdiction', 'kind', 'rationale', 'host', 'status', 'tier',
  'proposed_by', 'confidence', 'reason', 'gate'];
// run.SOURCE_KINDS / discover.py's KINDS: the venue kinds the coverage panel has chips for.
const KINDS = ['gazette', 'regulator', 'ministry', 'court', 'parliament', 'standards', 'other'];
const PROPOSED_BY = ['partner', 'discovery'];
// '' is a real value: a source whose confidence was never assessed. An absence is not a low score.
const CONFIDENCE = ['high', 'medium', 'low', ''];
// The maxLengths in scans/schema.json's $defs/source. Refusing beats a silent trim: a rationale
// cut mid-sentence in the coverage panel reads as evidence when it is half of one.
const SOURCE_STR_MAX = { name: 200, host: 253, jurisdiction: 40, rationale: 1000, reason: 1000 };
// REVIEWED DEFECT: budget overrides used to pass through untouched, so a definition could set
// max_new_per_run to a million and delay_seconds to 0 and remove every cost and politeness
// bound the pipeline has. common.Budget.DEFAULTS are ceilings, not defaults-to-override; the
// pipeline clamps and records a note, this endpoint refuses and names the ceiling, so the
// partner learns the limit before a workflow run is spent. Numbers mirror common.py exactly.
const BUDGET_CEILINGS = {
  max_sources: 12, max_new_per_run: 60, max_candidates: 25, max_doc_chars: 30000, max_pages_per_source: 1,
};
// The one budget key that is a floor: politeness towards official sites is not the partner's to
// waive. Dry-run fixtures may use 0, but nothing dispatched from here is a dry run.
const MIN_DELAY_SECONDS = 1.0;
const BUDGET_KEYS = Object.keys(BUDGET_CEILINGS).concat(['delay_seconds']);

// Vercel variable names are case-sensitive and easy to mistype, so match case-insensitively
// across the accepted names rather than failing on a capitalisation difference.
function findToken(env) {
  const want = new Set(TOKEN_NAMES.map((n) => n.toLowerCase()));
  for (const [k, v] of Object.entries(env)) {
    if (want.has(k.toLowerCase()) && v) return v;
  }
  return null;
}

function findRepo(env) {
  if (env.GITHUB_REPO) return env.GITHUB_REPO;
  const owner = env.VERCEL_GIT_REPO_OWNER;
  const slug = env.VERCEL_GIT_REPO_SLUG;
  return owner && slug ? `${owner}/${slug}` : null;
}

// Mirrors common.slug() in pipeline/scan/common.py character for character: the id the partner
// sees in the queued message must be the id the workflow writes, or the page they wait for never
// appears.
function slug(s) {
  const out = String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return out.slice(0, 60) || 'scan';
}

// ---- request plumbing (kept inline: Vercel bundles each function alone, so there is no shared
// module to import without adding a build step) ---------------------------------------------

// Method, content-type and same-site guards, identical to api/sweep.js. Returns true when the
// request was refused (and the response already sent).
function refuse(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    res.status(405).json({ ok: false, message: 'POST only.' });
    return true;
  }
  // CSRF guard. The browser re-sends Basic Auth on cross-site requests, so authentication alone
  // does not prove the partner intended this. A cross-site HTML form can only send
  // urlencoded/multipart/text-plain, and a cross-site fetch setting a JSON content-type is
  // preflighted — so insisting on JSON keeps drive-by dispatches out.
  const ctype = String(req.headers['content-type'] || '').toLowerCase();
  if (!ctype.startsWith('application/json')) {
    res.status(415).json({
      ok: false,
      message: 'Send Content-Type: application/json. This endpoint changes state, so it does not '
        + 'accept form-style submissions.',
    });
    return true;
  }
  // Same-origin only, where the browser tells us. Absent header = non-browser caller (curl), allowed.
  const site = String(req.headers['sec-fetch-site'] || '');
  if (site && site !== 'same-origin' && site !== 'none') {
    res.status(403).json({ ok: false, message: `Cross-site request refused (${site}).` });
    return true;
  }
  return false;
}

// Vercel hands us a parsed object for application/json bodies, a string for anything it did not
// parse, and nothing at all for an empty body. Returns {body} or {error}.
function parseBody(req) {
  let body;
  try {
    body = typeof req.body === 'string' ? (req.body.trim() ? JSON.parse(req.body) : {}) : (req.body || {});
  } catch (e) {
    return { error: 'Request body is not valid JSON.' };
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    return { error: 'Request body must be a JSON object.' };
  }
  return { body };
}

function isStr(v, min, max) {
  return typeof v === 'string' && v.length >= min && v.length <= max;
}

// run._is_url: http(s) with a host, at most 2000 characters. Cheap parse via the URL class so a
// string like "https://" or "http:///path" fails the same way it does in Python's urlparse.
function isUrl(u) {
  if (typeof u !== 'string' || u.length > 2000) return false;
  let p;
  try { p = new URL(u); } catch (e) { return false; }
  return (p.protocol === 'http:' || p.protocol === 'https:') && Boolean(p.hostname);
}

// A plain object — not null, not an array. What "object" means in every check below.
function isObj(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

// run._str_list: an array whose every entry is a string of 1..max trimmed characters. Returns
// an error message or null.
function strList(v, what, max) {
  if (!Array.isArray(v)) return `scan.${what} must be an array of strings.`;
  for (let i = 0; i < v.length; i += 1) {
    const s = v[i];
    if (typeof s !== 'string' || s.trim().length < 1 || s.trim().length > max) {
      return `scan.${what}[${i}] must be a string of 1–${max} characters.`;
    }
  }
  return null;
}

// The id checks every id must pass, wherever it entered (scan_id, scan.id, or the slug derived
// from the name). `what` names the field in the message.
function idError(id, what) {
  if (!isStr(id, 2, 60) || !SCAN_ID.test(id)) {
    return `${what} must be 2–60 characters of lowercase letters, digits and hyphens, starting with a letter or digit.`;
  }
  if (RESERVED_IDS.has(id)) {
    return `${what} ${JSON.stringify(id)} is reserved (${Array.from(RESERVED_IDS).join(', ')} name files the `
      + 'tracker itself owns). Pick another name or set scan.id.';
  }
  return null;
}

// The definition checks that mirror run.validate_definition, so what we queue is what the
// workflow will accept. REVIEWED DEFECT: this endpoint used to check only name, intent, id and
// size; a definition with no jurisdictions, URL-string sources, object clients or an unknown
// budget key got a 202 "Scan queued" and then exit 2 in the workflow, visible only in the
// Actions log. Returns an error message or null. Mutates nothing.
function definitionError(scan) {
  for (const k of Object.keys(scan)) {
    if (!TOP_KEYS.includes(k)) {
      return `scan.${k} is not a definition field (allowed: ${TOP_KEYS.join(', ')}).`;
    }
  }
  if (!isStr(scan.name, 3, 120) || scan.name.trim().length < 3) {
    return 'scan.name must be 3–120 characters.';
  }
  if (!isStr(scan.intent, 20, 1500) || scan.intent.trim().length < 20) {
    return 'scan.intent must be 20–1500 characters — say what to advise on and what to '
      + 'surface, the way you would brief an associate.';
  }
  if (scan.id !== undefined) {
    const e = idError(scan.id, 'scan.id');
    if (e) return e;
  }
  const jur = scan.jurisdictions;
  if (!Array.isArray(jur) || jur.length < 1) {
    return 'scan.jurisdictions must name at least one jurisdiction (e.g. "IN", "EU", "US-CA").';
  }
  if (jur.length > 60) return 'scan.jurisdictions: at most 60.';
  const jurErr = strList(jur, 'jurisdictions', 40);
  if (jurErr) return jurErr;
  for (const key of ['topics', 'industries']) {
    if (scan[key] !== undefined) {
      const e = strList(scan[key], key, 80);
      if (e) return e;
    }
  }
  if (scan.clients !== undefined) {
    const cl = scan.clients;
    if (!Array.isArray(cl)) return 'scan.clients must be an array of client names, or {name, scope} objects.';
    for (let i = 0; i < cl.length; i += 1) {
      const c = cl[i];
      if (typeof c === 'string') {
        if (c.trim().length < 1 || c.trim().length > 120) return `scan.clients[${i}] must be 1–120 characters.`;
      } else if (isObj(c)) {
        const extra = Object.keys(c).filter((k) => k !== 'name' && k !== 'scope');
        if (extra.length) return `scan.clients[${i}] has unknown field(s) ${extra.join(', ')}; only name and scope are allowed.`;
        if (typeof c.name !== 'string' || c.name.trim().length < 1 || c.name.trim().length > 120) {
          return `scan.clients[${i}].name must be 1–120 characters.`;
        }
        if (c.scope !== undefined && (typeof c.scope !== 'string' || c.scope.length > 500)) {
          return `scan.clients[${i}].scope must be a string of at most 500 characters.`;
        }
      } else {
        return `scan.clients[${i}] must be a client name (string) or {name, scope}.`;
      }
    }
  }
  if (scan.sources !== undefined) {
    const srcs = scan.sources;
    if (!Array.isArray(srcs)) return 'scan.sources must be an array of URLs or {url, ...} objects.';
    for (let i = 0; i < srcs.length; i += 1) {
      const s = srcs[i];
      // A bare URL string is what the Create dialog sends; the workflow coerces it to {url}.
      if (typeof s === 'string') {
        if (!isUrl(s)) return `scan.sources[${i}] must be an http(s) URL.`;
        continue;
      }
      if (!isObj(s)) return `scan.sources[${i}] must be an http(s) URL or an object with a url field.`;
      for (const k of Object.keys(s)) {
        if (!SOURCE_KEYS.includes(k)) {
          return `scan.sources[${i}].${k} is not a source field (allowed: ${SOURCE_KEYS.join(', ')}).`;
        }
      }
      if (!isUrl(s.url)) return `scan.sources[${i}].url must be an http(s) URL.`;
      if (s.status !== undefined && !SOURCE_STATUSES.includes(s.status)) {
        return `scan.sources[${i}].status must be one of: ${SOURCE_STATUSES.join(', ')}.`;
      }
      if (s.tier !== undefined && !TIERS.includes(s.tier)) {
        return `scan.sources[${i}].tier must be one of: ${TIERS.join(', ')}.`;
      }
      if (s.kind !== undefined && !KINDS.includes(s.kind)) {
        return `scan.sources[${i}].kind must be one of: ${KINDS.join(', ')}.`;
      }
      if (s.proposed_by !== undefined && !PROPOSED_BY.includes(s.proposed_by)) {
        return `scan.sources[${i}].proposed_by must be one of: ${PROPOSED_BY.join(', ')}.`;
      }
      if (s.confidence !== undefined && !CONFIDENCE.includes(s.confidence)) {
        return `scan.sources[${i}].confidence must be one of: high, medium, low (or "" when unassessed).`;
      }
      // The gate's evidence block is re-submitted verbatim by Edit. Its shape is gate.py's, so
      // check only that it is an object and let the pipeline own the rest.
      if (s.gate !== undefined && !isObj(s.gate)) return `scan.sources[${i}].gate must be an object.`;
      for (const [k, max] of Object.entries(SOURCE_STR_MAX)) {
        if (s[k] === undefined) continue;
        if (typeof s[k] !== 'string') return `scan.sources[${i}].${k} must be a string.`;
        if (s[k].length > max) return `scan.sources[${i}].${k} must be at most ${max} characters.`;
      }
    }
  }
  // subject_filter is optional, and a definition without one behaves exactly as it does today:
  // every row its sources list is kept. What is refused here is a MALFORMED one, because a
  // subject filter that does not survive validation would be dropped or half-applied by the
  // pipeline, and a filter nobody can read is one nobody can check.
  if (scan.subject_filter !== undefined) {
    const f = scan.subject_filter;
    if (!isObj(f)) {
      return `scan.subject_filter must be an object {${SUBJECT_FILTER_KEYS.join(', ')}} (or be left out, in which `
        + 'case every row is kept).';
    }
    for (const k of Object.keys(f)) {
      if (!SUBJECT_FILTER_KEYS.includes(k)) {
        return `scan.subject_filter.${k} is not a subject-filter field (allowed: ${SUBJECT_FILTER_KEYS.join(', ')}).`;
      }
    }
    if (typeof f.regex !== 'string' || f.regex.trim().length < 1 || f.regex.length > MAX_SUBJECT_REGEX) {
      // Empty is refused rather than read as "no filter": there are two ways to say that, and both
      // are explicit. Leave subject_filter out, or keep the regex and set source to "none" so the
      // scan page can show what was turned off and by whom.
      return `scan.subject_filter.regex must be 1–${MAX_SUBJECT_REGEX} characters of regular expression. To run this `
        + 'scan without a subject filter, leave subject_filter out entirely, or set '
        + 'subject_filter.source to "none".';
    }
    if (f.why !== undefined && (typeof f.why !== 'string' || f.why.length > MAX_SUBJECT_WHY)) {
      return `scan.subject_filter.why must be a string of at most ${MAX_SUBJECT_WHY} characters — one sentence a `
        + 'partner can check against the regex.';
    }
    if (typeof f.source !== 'string' || !SUBJECT_FILTER_SOURCES.includes(f.source)) {
      return `scan.subject_filter.source must be one of: ${SUBJECT_FILTER_SOURCES.join(', ')} — who this filter came `
        + 'from ("none" means it is deliberately not applied).';
    }
  }
  if (scan.budget !== undefined) {
    const b = scan.budget;
    if (!isObj(b)) return `scan.budget must be an object with keys from: ${BUDGET_KEYS.join(', ')}.`;
    for (const [k, v] of Object.entries(b)) {
      if (!BUDGET_KEYS.includes(k)) {
        return `scan.budget.${k} is not a budget key (allowed: ${BUDGET_KEYS.join(', ')}).`;
      }
      if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) {
        return `scan.budget.${k} must be a non-negative number.`;
      }
      if (k === 'delay_seconds') {
        if (v < MIN_DELAY_SECONDS) {
          return `scan.budget.delay_seconds must be at least ${MIN_DELAY_SECONDS} (the floor on how fast we hit an `
            + 'official site; it cannot be lowered per scan).';
        }
      } else if (v > BUDGET_CEILINGS[k]) {
        return `scan.budget.${k} is above the ceiling of ${BUDGET_CEILINGS[k]} (the pipeline would clamp it there; `
          + 'set it to the ceiling or leave it out).';
      }
    }
  }
  if (scan.demo !== undefined && typeof scan.demo !== 'boolean') return 'scan.demo must be true or false.';
  for (const k of ['no_discover', 'no_misc']) {
    if (scan[k] !== undefined && typeof scan[k] !== 'boolean') {
      return `scan.${k} must be true or false when given.`;
    }
  }
  if (scan.discovery_notes !== undefined) {
    const e = strList(scan.discovery_notes, 'discovery_notes', MAX_DISCOVERY_NOTE_CHARS);
    if (e) return e;
    if (scan.discovery_notes.length > MAX_DISCOVERY_NOTES) {
      return `scan.discovery_notes: at most ${MAX_DISCOVERY_NOTES}.`;
    }
  }
  for (const k of ['created', 'updated']) {
    if (scan[k] !== undefined && typeof scan[k] !== 'string') return `scan.${k} must be a string.`;
  }
  return null;
}

// Everything the request must satisfy before we spend a workflow run on it. Returns
// {error} or {action, scan_id, scan, no_discover}. Validation is deliberately strict and
// specific: a partner who typed a one-line intent should be told the minimum, not shown a
// workflow that failed twenty seconds later with a Python traceback.
function validate(body) {
  const action = body.action;
  if (typeof action !== 'string' || !ACTIONS.includes(action)) {
    return { error: `Unknown action ${JSON.stringify(action)}. Allowed: ${ACTIONS.join(', ')}.` };
  }

  // status reads GitHub; it dispatches nothing, so it takes an optional scan_id and nothing else.
  // Refusing the extra keys keeps a caller from believing a definition sent alongside was acted on.
  if (action === 'status') {
    const extra = Object.keys(body).filter((k) => k !== 'action' && k !== 'scan_id' && k !== 'workflow');
    if (extra.length) {
      return { error: `status takes only scan_id and workflow (got ${extra.join(', ')}).` };
    }
    let statusId = null;
    if (body.scan_id !== undefined) {
      const e = idError(body.scan_id, 'scan_id');
      if (e) return { error: e };
      statusId = body.scan_id;
    }
    // `workflow` lets the dashboard's Update now watch its own sweep with the same machinery.
    // Matched against the allow-list, never used as a path: the caller names a key, not a file.
    let wf = null;
    if (body.workflow !== undefined) {
      if (typeof body.workflow !== 'string' || !Object.prototype.hasOwnProperty.call(STATUS_WORKFLOWS, body.workflow)) {
        return { error: `workflow must be one of: ${Object.keys(STATUS_WORKFLOWS).join(', ')}.` };
      }
      wf = body.workflow;
    }
    return { action, scan_id: statusId, scan: null, no_discover: false, finding: null, workflow: wf };
  }

  // promote takes a scan and one finding id and nothing else: no definition, no no_discover. A
  // definition sent alongside would look acted upon and would not be, so it is refused, exactly
  // as status refuses its extras.
  if (action === 'promote') {
    const extra = Object.keys(body).filter((k) => k !== 'action' && k !== 'scan_id' && k !== 'finding');
    if (extra.length) {
      return { error: `promote takes only scan_id and finding (got also: ${extra.join(', ')}). To change a `
        + "scan's definition, use create." };
    }
    if (body.scan_id === undefined) {
      return { error: 'promote needs scan_id — the scan whose Miscellaneous finding is being promoted.' };
    }
    const e = idError(body.scan_id, 'scan_id');
    if (e) return { error: e };
    if (typeof body.finding !== 'string' || !FINDING_ID.test(body.finding)) {
      return { error: 'promote needs "finding": the 10-character id of the miscellaneous finding to promote '
        + '(lowercase hex, exactly as it appears in that scan\'s misc.json).' };
    }
    return { action, scan_id: body.scan_id, scan: null, no_discover: false, finding: body.finding };
  }

  if (body.no_discover !== undefined && typeof body.no_discover !== 'boolean') {
    return { error: 'no_discover must be true or false when given.' };
  }

  let scanId = null;
  if (body.scan_id !== undefined) {
    const e = idError(body.scan_id, 'scan_id');
    if (e) return { error: e };
    scanId = body.scan_id;
  }

  let scan = null;
  if (action === 'create' || body.scan !== undefined) {
    scan = body.scan;
    if (!isObj(scan)) {
      return { error: action === 'create'
        ? 'create needs a scan object: {name, intent, jurisdictions, topics, ...}.'
        : 'scan must be an object when given.' };
    }
    const e = definitionError(scan);
    if (e) return { error: e };
    // The id the workflow writes is the id the page will live at; derive it once, here, and pin
    // it into the definition so the two can never disagree. A derived slug goes through the same
    // checks as a typed id — that is where a name like "Schema" is caught.
    if (!scanId) {
      scanId = scan.id || slug(scan.name);
      const idErr = idError(scanId, `The id derived from the name (${JSON.stringify(scanId)})`);
      if (idErr) return { error: `${idErr} Give the scan a longer or different name, or set scan.id.` };
    }
  }

  // The dispatch input is the authority; the same value is pinned inside the definition so the
  // workflow records how the scan was created and Edit can show it. A definition that carries
  // its own no_discover is honoured only when the input is silent.
  // no_misc has no dispatch input on purpose: scan.yml does not declare one, and GitHub answers
  // 422 for an input a workflow has not declared. It rides inside the definition, where
  // run.py create reads it (`defn["no_misc"] = bool(no_misc) or bool(defn.get("no_misc"))`), so
  // an Edit that keeps the lane switched off keeps it switched off.
  const noDiscover = body.no_discover !== undefined ? body.no_discover : Boolean(scan && scan.no_discover === true);
  if (scan) {
    scan = Object.assign({}, scan, { id: scanId, no_discover: noDiscover });
    // REVIEWED DEFECT: the size check ran on the caller's object, before `id` was pinned, so a
    // definition within ~70 characters of the limit passed here and failed the workflow's own
    // check. Measure the exact string that will be dispatched.
    if (JSON.stringify(scan).length > MAX_SCAN_JSON) {
      return { error: `The scan definition is too large (over ${MAX_SCAN_JSON} characters as JSON, measured `
        + 'with the id pinned in). Trim the source list or the intent.' };
    }
  }

  if (!scanId) {
    return { error: `${action} needs scan_id.` };
  }
  return { action, scan_id: scanId, scan, no_discover: noDiscover, finding: null };
}

function ghHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'Content-Type': 'application/json',
    'User-Agent': 'tmt-radar-dashboard',
  };
}

// run-name in .github/workflows/scan.yml is "Scan <action> <scan_id>", so the id is a whole
// whitespace-separated word of the title. Matching on words rather than a substring keeps the
// runs of "eu-pay" out of the status of "eu-pay-transparency".
function titleNames(title, scanId) {
  return String(title || '').split(/\s+/).includes(scanId);
}

// What the runs of scan.yml are doing, newest first. THIS NEVER FAILS THE PAGE: every problem —
// a token without actions:read, GitHub unreachable, a body that is not JSON — comes back as an
// empty list plus the reason, and the caller answers 200. The Scans page keeps its own
// elapsed-time card for a scan it dispatched itself, and a red banner over a scan that is in fact
// running would be a lie told by the status widget about the work, not about itself.
async function runStatus(token, repo, scanId, wf) {
  wf = STATUS_WORKFLOWS[wf] || WORKFLOW;   // allow-listed above; an unknown name falls back to scan.yml
  const perPage = scanId ? 30 : MAX_RUNS;
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${wf}/runs?per_page=${perPage}`;
  let gh;
  try {
    gh = await fetch(url, { headers: ghHeaders(token) });
  } catch (e) {
    return { runs: [], message: 'Could not reach the GitHub API for run status; the timer here is this page\'s own.' };
  }
  if (gh.status !== 200) {
    let reason = '';
    try { reason = JSON.parse(await gh.text()).message || ''; } catch (e) { reason = ''; }
    const detail = `${gh.status}${reason ? ': ' + reason : ''}`;
    return { runs: [], message: gh.status === 403 || gh.status === 404
      ? `Live run status is off: the GitHub token cannot read this repository's Actions runs (${detail}). `
        + 'Give it Actions: read — dispatching works without it, so the scan is still running.'
      : `GitHub could not report run status (${detail}). The scan itself is unaffected.` };
  }
  let data;
  try { data = JSON.parse(await gh.text()); } catch (e) {
    return { runs: [], message: 'GitHub returned something that was not JSON when asked for run status.' };
  }
  const runs = (Array.isArray(data.workflow_runs) ? data.workflow_runs : [])
    .filter((r) => isObj(r))
    .filter((r) => !scanId || titleNames(r.display_title || r.name, scanId))
    // GitHub already sorts newest first; sorting again means a change there cannot quietly put
    // last week's run at the top of a partner's status card.
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
    .slice(0, MAX_RUNS)
    .map((r) => ({
      id: r.id,
      status: r.status || '',            // queued | in_progress | completed
      conclusion: r.conclusion || null,  // null until it completes, then success | failure | cancelled | ...
      created_at: r.created_at || '',
      updated_at: r.updated_at || '',
      html_url: r.html_url || '',
      display_title: r.display_title || r.name || '',
    }));
  if (!runs.length) {
    return { runs: [], message: scanId
      ? 'No run for this scan has reached GitHub yet — a dispatch takes a few seconds to appear.'
      : 'No scan runs yet.' };
  }
  return { runs };
}

module.exports = async (req, res) => {
  if (refuse(req, res)) return undefined;

  const parsed = parseBody(req);
  if (parsed.error) return res.status(400).json({ ok: false, message: parsed.error });
  const v = validate(parsed.body);
  if (v.error) return res.status(400).json({ ok: false, message: v.error });

  const token = findToken(process.env);
  const repo = findRepo(process.env);
  if (!token) {
    return res.status(501).json({
      ok: false,
      message: 'No GitHub token is configured on this deployment. Add one in Vercel → Settings → '
        + `Environment Variables named any of: ${TOKEN_NAMES.join(', ')} — then redeploy `
        + '(environment changes only take effect on a new deployment).',
    });
  }
  if (!repo) {
    return res.status(501).json({
      ok: false,
      message: 'The repository could not be determined. Set GITHUB_REPO to "owner/repo" in '
        + 'Vercel → Settings → Environment Variables, then redeploy.',
    });
  }

  const branch = process.env.VERCEL_GIT_COMMIT_REF || 'main';
  const actionsUrl = `https://github.com/${repo}/actions/workflows/${STATUS_WORKFLOWS[v.workflow] || WORKFLOW}`;

  if (v.action === 'status') {
    const s = await runStatus(token, repo, v.scan_id, v.workflow);
    const out = { ok: true, runs: s.runs, actionsUrl };
    if (v.scan_id) out.scan_id = v.scan_id;
    if (v.workflow) out.workflow = v.workflow;
    if (s.message) out.message = s.message;
    return res.status(200).json(out);
  }

  const url = `https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`;
  // workflow_dispatch inputs are strings only — booleans and objects arrive as text and the
  // workflow parses them back. An absent scan is "" rather than "null" so a shell test on the
  // input stays a plain -z.
  const inputs = {
    action: v.action,
    scan_id: v.scan_id,
    scan: v.scan ? JSON.stringify(v.scan) : '',
    no_discover: v.no_discover ? 'true' : 'false',
  };
  // `finding` is sent only for promote. GitHub answers 422 for an input the workflow does not
  // declare, so a deployment whose scan.yml predates the promote action keeps working for
  // create/run/delete/status; only promote itself needs the newer workflow.
  // The request field is `finding`; the WORKFLOW INPUT is `finding_id` — scan.yml declares that
  // name and validates it. Sending `finding` made GitHub 422 the dispatch on an undeclared input,
  // so promote failed after passing every check on this side.
  if (v.action === 'promote') inputs.finding_id = v.finding;

  let gh;
  try {
    gh = await fetch(url, {
      method: 'POST',
      headers: ghHeaders(token),
      body: JSON.stringify({ ref: branch, inputs }),
    });
  } catch (e) {
    return res.status(502).json({ ok: false, message: 'Could not reach the GitHub API.' });
  }

  // 204 No Content is GitHub's success for a workflow dispatch.
  if (gh.status === 204) {
    const message = v.action === 'delete'
      ? 'Scan removal queued on GitHub Actions — it disappears from this page in a few minutes; '
        + 'the page refreshes itself.'
      : v.action === 'promote'
        ? 'Promotion queued on GitHub Actions — the venue is added to this scan\'s sources and then goes '
          + 'through the same gate as every other source (fetched with our identifying User-Agent, '
          + 'robots.txt enforced, terms scanned, extraction floor applied). It joins coverage only if the '
          + 'gate approves it, and the coverage panel shows the evidence either way.'
        : 'Scan queued on GitHub Actions — results land here in a few minutes; the page refreshes itself.';
    const out = { ok: true, message, scan_id: v.scan_id, actionsUrl };
    if (v.finding) out.finding = v.finding;
    return res.status(202).json(out);
  }

  // Report GitHub's own reason rather than a generic failure, plus what it usually means here.
  let reason = '';
  try { reason = JSON.parse(await gh.text()).message || ''; } catch (e) { reason = ''; }
  const hint = gh.status === 401 ? ' The token is invalid or has expired.'
    : gh.status === 403 ? ' The token likely lacks Actions: read & write on this repository.'
    : gh.status === 404 ? ` Checked ${repo} for .github/workflows/${WORKFLOW} on branch ${branch} —`
      + ' a 404 here usually means the token cannot see this repository, or the workflow file is'
      + ' not on that branch.'
    : gh.status === 422 ? ` GitHub only accepts a dispatch once .github/workflows/${WORKFLOW} exists on`
      + ' the DEFAULT branch with a workflow_dispatch trigger — a workflow that is only on a feature'
      + ' branch, or not yet merged, answers 422. It also answers 422 when the declared inputs do not'
      + ' match {action, scan_id, scan, no_discover} (plus finding, which only a promote sends and'
      + ' only the newer workflow declares), or the inputs exceed 65,535 characters.'
    : '';
  return res.status(502).json({
    ok: false,
    message: `GitHub declined the request (${gh.status}${reason ? ': ' + reason : ''}).${hint}`,
    scan_id: v.scan_id,
    actionsUrl,
  });
};
