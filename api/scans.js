// Vercel serverless function — Create / Run / Delete a scan.
//
// The scan layer (docs/horizon-design.md §3) does all its real work on GitHub Actions, because
// discovery, gating, fetching and enrichment take minutes and hit government sites, which is not
// something a serverless function should be doing under a 60-second clock. This endpoint only
// dispatches .github/workflows/scan.yml with the partner's request bound as workflow inputs; the
// workflow validates the definition again, runs, and commits the results, and Vercel rebuilds
// the page from what was committed. Nothing here claims a scan ran that did not run.
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

const TOKEN_NAMES = [
  'GITHUB_DISPATCH_TOKEN', 'TMT_TOKEN', 'TMT_DISPATCH_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
];
const WORKFLOW = 'scan.yml';
const ACTIONS = ['create', 'run', 'delete'];
// Same shape the workflow and pipeline/scan/common.py insist on, so an id minted here is one the
// pipeline will accept as a file name under scans/ and data/scans/ without any further cleaning.
const SCAN_ID = /^[a-z0-9][a-z0-9-]{1,59}$/;
const MAX_SCAN_JSON = 60000;
// Ids that match SCAN_ID but name files the layer already owns: scans/schema.json is the
// contract, tmt-india is the registry lane. REVIEWED DEFECT: a scan named "Schema" slugged to
// `schema`, and ScanPaths wrote the definition over scans/schema.json (and delete unlinked it).
// pipeline/scan/run.py holds the same set; keep the two identical.
const RESERVED_IDS = new Set(['schema', 'tmt-india']);
// Top-level fields run.validate_definition accepts (its TOP_KEYS plus no_discover, which travels
// inside the definition as well as as a dispatch input). An unknown key fails there with exit 2
// two minutes after we said "queued", so it fails here first.
const TOP_KEYS = ['id', 'name', 'intent', 'jurisdictions', 'topics', 'industries', 'clients',
  'sources', 'budget', 'demo', 'created', 'updated', 'no_discover'];
const SOURCE_STATUSES = ['approved', 'pending', 'rejected'];
const TIERS = ['vetted', 'discovered'];
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
      if (!isUrl(s.url)) return `scan.sources[${i}].url must be an http(s) URL.`;
      if (s.status !== undefined && !SOURCE_STATUSES.includes(s.status)) {
        return `scan.sources[${i}].status must be one of: ${SOURCE_STATUSES.join(', ')}.`;
      }
      if (s.tier !== undefined && !TIERS.includes(s.tier)) {
        return `scan.sources[${i}].tier must be one of: ${TIERS.join(', ')}.`;
      }
      for (const k of ['name', 'host', 'jurisdiction', 'rationale', 'reason']) {
        if (s[k] !== undefined && typeof s[k] !== 'string') return `scan.sources[${i}].${k} must be a string.`;
      }
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
  if (scan.no_discover !== undefined && typeof scan.no_discover !== 'boolean') {
    return 'scan.no_discover must be true or false when given.';
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
  return { action, scan_id: scanId, scan, no_discover: noDiscover };
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
  const actionsUrl = `https://github.com/${repo}/actions/workflows/${WORKFLOW}`;
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

  let gh;
  try {
    gh = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'tmt-radar-dashboard',
      },
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
      : 'Scan queued on GitHub Actions — results land here in a few minutes; the page refreshes itself.';
    return res.status(202).json({ ok: true, message, scan_id: v.scan_id, actionsUrl });
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
      + ' match {action, scan_id, scan, no_discover}, or the inputs exceed 65,535 characters.'
    : '';
  return res.status(502).json({
    ok: false,
    message: `GitHub declined the request (${gh.status}${reason ? ': ' + reason : ''}).${hint}`,
    scan_id: v.scan_id,
    actionsUrl,
  });
};
