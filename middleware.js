// Vercel Edge Middleware — the access gate for the whole deployment.
//
// This runs on Vercel's edge BEFORE any file is served, so an unauthenticated request never
// receives the dashboard HTML at all. That distinction is the whole point: a login rendered
// inside the page could not protect the page, because the client roster and the password
// would both be sitting in the same downloadable file. Here, the password is only ever on
// the server, and the browser gets a 401 until it presents the right one.
//
// It also covers /api/sweep, so a stranger with the URL can no longer trigger sweeps.
//
// Credentials come from Vercel → Settings → Environment Variables (then redeploy):
//   AUTH_USER, AUTH_PASS
// There is deliberately NO fallback pair. An earlier version shipped starter credentials so the
// gate could never be accidentally open, but that made it weakly closed by default: a short
// password committed to git, guarding a client roster, with no rate limiting in front of it. If
// the variables are absent the deployment refuses every request and says why — a misconfigured
// deployment must be obviously broken, never quietly guessable.
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

const UNCONFIGURED = new Response(
  'This deployment has no credentials configured. Set AUTH_USER and AUTH_PASS in '
  + 'Vercel → Settings → Environment Variables, then redeploy.',
  { status: 503, headers: { 'Cache-Control': 'no-store' } },
);

const CHALLENGE = new Response('Authentication required.', {
  status: 401,
  headers: {
    'WWW-Authenticate': 'Basic realm="TMT Regulatory Radar", charset="UTF-8"',
    'Cache-Control': 'no-store',
  },
});

export default function middleware(request) {
  const user = process.env.AUTH_USER;
  const pass = process.env.AUTH_PASS;
  if (!user || !pass) return UNCONFIGURED.clone();

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

  const ok = safeEqual(decoded.slice(0, i), user) && safeEqual(decoded.slice(i + 1), pass);
  if (!ok) return CHALLENGE.clone();

  // Authenticated: returning nothing lets the request continue to the static file or function.
  return undefined;
}
