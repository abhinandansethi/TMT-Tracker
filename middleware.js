// Vercel Edge Middleware — the access gate for the whole deployment.
//
// This runs on Vercel's edge BEFORE any file is served, so an unauthenticated request never
// receives the dashboard HTML at all. That distinction is the whole point: a login rendered
// inside the page could not protect the page, because the client roster and the password
// would both be sitting in the same downloadable file. Here, the password is only ever on
// the server, and the browser gets a 401 until it presents the right one.
//
// It also covers /api/sweep and /api/scans, so a stranger with the URL can no longer trigger
// sweeps or spend workflow runs.
//
// The challenge below is OUR challenge — `WWW-Authenticate: Basic realm="TMT Regulatory Radar"`.
// A partner therefore needs nothing but the URL and a username and password: no Vercel account,
// no SSO, no invitation, any browser or phone. That is deliberate and must stay true.
//
// ---------------------------------------------------------------- ONE LOGIN PER PARTNER
// Credentials come from Vercel → Settings → Environment Variables. Environment changes only
// take effect on a NEW deployment, so after editing any of these, redeploy.
//
// There are three ways to configure them, and they all work at once. Paste the variable NAMES
// exactly as written here:
//
//   1. AUTH_USER and AUTH_PASS
//      The original single pair. Unchanged, still supported, still the simplest thing that
//      works. Its value is used byte for byte — a password with a leading or trailing space
//      authenticates exactly as typed.
//
//   2. AUTH_USERS
//      One variable holding one `user:password` pair PER LINE, which is how you add a partner
//      with one edit and one redeploy:
//
//          abhi:correct-horse-battery
//          priya:another-long-passphrase
//          rahul:a-third-one
//
//      Blank lines are skipped and whitespace around a pair is ignored, so pasting from a
//      password manager or an email does not break it. A single-line list may separate pairs
//      with commas instead (`abhi:one,priya:two`) — but a comma is then a separator, so if any
//      password contains a comma, put every pair on its own line. Only the FIRST colon splits,
//      so a password may contain colons: `ana:pa:ss` is user `ana`, password `pa:ss`. The whole entry is
//      trimmed and so is the username, but the password keeps every character after that first
//      colon — including leading and trailing spaces — so `ana: pw ` is password ' pw '. Only a
//      password that begins or ends with a NEWLINE cannot be expressed here; use AUTH_PASS or a
//      numbered variable for that one.
//
//   3. AUTH_USER_2 / AUTH_PASS_2, AUTH_USER_3 / AUTH_PASS_3, … up to AUTH_USER_9 / AUTH_PASS_9
//      Separate variables per partner, for anyone who would rather see nine rows in the Vercel
//      UI than one multi-line box. Values are used byte for byte, like the original pair.
//
// There is deliberately NO fallback pair. An earlier version shipped starter credentials so the
// gate could never be accidentally open, but that made it weakly closed by default: a short
// password committed to git, guarding a client roster, with no rate limiting in front of it. If
// NOTHING at all is configured the deployment refuses every request with 503 and says why — a
// misconfigured deployment must be obviously broken, never quietly guessable.
//
// No credential — configured or submitted — is ever logged, echoed, or named in a response.
//
// Edge Middleware is available on Hobby (free) as well as paid plans — no upgrade needed.

// Protect everything, including /api. Vercel's own internal paths are excluded so the
// deployment can still serve its infrastructure requests.
export const config = {
  matcher: ['/((?!_vercel|_next/static|_next/image).*)'],
};

// Compares without leaking the password *prefix* through timing. It does still leak the
// password *length* — the early length check returns measurably faster — which is a deliberate
// trade for simplicity, not an oversight. Length alone is a weak signal; prefix would not be.
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// Splits one entry of the AUTH_USERS list into `user` and `password` on the FIRST colon only —
// a password may legitimately contain colons, a username may not. The caller has already trimmed
// the entry as a whole (so indentation, a trailing \r from a Windows paste and a stray space
// around the pair are tolerated); the username is trimmed once more, because a space around a
// name typed into a browser dialog is a typo rather than part of the credential, while the
// password keeps every character the entry trim left. Returns null when there is no colon or
// either half is empty: a pair with no password is a configuration mistake, and accepting it
// would turn an empty password into a working one.
function splitPair(entry) {
  const i = entry.indexOf(':');
  if (i < 0) return null;
  const user = entry.slice(0, i).trim();
  const pass = entry.slice(i + 1);
  if (!user || !pass) return null;
  return [user, pass];
}

// Reads the environment ONCE per request, and every read is a literal `process.env.NAME`.
// That shape is not stylistic. Edge builders may replace `process.env.NAME` textually at build
// time, and a computed `process.env[name]` would then read nothing at all: the numbered partners
// would be silently locked out, and an inlined-away AUTH_USER would put the whole deployment into
// the 503. Nine numbered slots is not a technical limit — it is the point past which AUTH_USERS
// is plainly the better way to hold a roster. Add a tenth by adding a line here and a line to the
// file header, not by building the name from a counter.
function readEnv() {
  return {
    AUTH_USER: process.env.AUTH_USER,
    AUTH_PASS: process.env.AUTH_PASS,
    AUTH_USERS: process.env.AUTH_USERS,
    numbered: [
      [process.env.AUTH_USER_2, process.env.AUTH_PASS_2],
      [process.env.AUTH_USER_3, process.env.AUTH_PASS_3],
      [process.env.AUTH_USER_4, process.env.AUTH_PASS_4],
      [process.env.AUTH_USER_5, process.env.AUTH_PASS_5],
      [process.env.AUTH_USER_6, process.env.AUTH_PASS_6],
      [process.env.AUTH_USER_7, process.env.AUTH_PASS_7],
      [process.env.AUTH_USER_8, process.env.AUTH_PASS_8],
      [process.env.AUTH_USER_9, process.env.AUTH_PASS_9],
    ],
  };
}

// Every credential this deployment accepts, from all three configuration styles. Built fresh on
// each request rather than cached at module load: the list is at most a handful of pairs, and a
// cache would only matter if the environment could change under a warm edge instance — in which
// case a stale cache is a partner locked out or a removed partner still admitted.
function configuredPairs(env) {
  const pairs = [];
  const seen = new Set();
  const add = (pair) => {
    if (!pair) return;
    // Deduplicate so the same partner listed twice does not double the work; the key cannot
    // collide across pairs because NUL cannot appear in an environment variable value.
    const key = `${pair[0]}\u0000${pair[1]}`;
    if (seen.has(key)) return;
    seen.add(key);
    pairs.push(pair);
  };

  // 1. the original pair, byte for byte as before.
  if (env.AUTH_USER && env.AUTH_PASS) add([env.AUTH_USER, env.AUTH_PASS]);

  // 2. the list. Newlines separate; commas separate only when there is no newline at all, so a
  //    password containing a comma keeps working in the one-pair-per-line form.
  const list = String(env.AUTH_USERS || '');
  if (list.trim()) {
    const entries = /[\r\n]/.test(list) ? list.split(/[\r\n]+/) : list.split(',');
    for (const entry of entries) {
      const e = entry.trim();
      if (!e) continue;              // blank lines, and the trailing newline of a pasted block
      add(splitPair(e));
    }
  }

  // 3. the numbered variables, byte for byte like the original pair. A slot with only one half
  //    filled in is skipped, exactly as a half-configured AUTH_USER/AUTH_PASS is.
  for (const [u, p] of env.numbered) {
    if (u && p) add([u, p]);
  }

  return pairs;
}

const UNCONFIGURED = new Response(
  'This deployment has no credentials configured. Set AUTH_USER and AUTH_PASS — or AUTH_USERS, '
  + 'one "user:password" pair per line — in Vercel → Settings → Environment Variables, then redeploy.',
  { status: 503, headers: { 'Cache-Control': 'no-store' } },
);

// One body for every failure: no header, an unparseable header, an unknown username, a known
// username with the wrong password. A partner who mistyped needs to know who to ask; anyone
// else must not learn from the answer which half was wrong or whether a username exists.
const CHALLENGE = new Response(
  'Authentication required. Ask Abhi for your username and password.',
  {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="TMT Regulatory Radar", charset="UTF-8"',
      'Cache-Control': 'no-store',
    },
  },
);

export default function middleware(request) {
  const pairs = configuredPairs(readEnv());
  if (!pairs.length) return UNCONFIGURED.clone();

  const header = request.headers.get('authorization') || '';
  if (!header.startsWith('Basic ')) return CHALLENGE.clone();

  let decoded;
  try {
    // atob gives bytes, not text. Decode as UTF-8 to match the charset the challenge advertises,
    // so a password containing non-ASCII characters authenticates instead of silently failing.
    decoded = new TextDecoder().decode(Uint8Array.from(atob(header.slice(6)), (c) => c.charCodeAt(0)));
  } catch (e) {
    return CHALLENGE.clone();
  }

  // Split on the FIRST colon only — a password may legitimately contain colons.
  const i = decoded.indexOf(':');
  if (i < 0) return CHALLENGE.clone();
  const user = decoded.slice(0, i);
  const pass = decoded.slice(i + 1);

  // EVERY configured pair is compared on EVERY request — no early exit on the first match, and
  // both halves compared even when the username already failed. Short-circuiting would make the
  // response measurably faster for a username nobody has than for one that exists, which is the
  // same prefix leak safeEqual exists to prevent, one level up. (The length leak safeEqual
  // documents is still here, and now covers the configured usernames too.)
  let matched = 0;
  for (const [u, p] of pairs) {
    const userOk = safeEqual(user, u);
    const passOk = safeEqual(pass, p);
    matched |= (userOk && passOk) ? 1 : 0;
  }
  if (!matched) return CHALLENGE.clone();

  // Authenticated: returning nothing lets the request continue to the static file or function.
  return undefined;
}
