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
// If they are unset it falls back to the starter credentials below so the gate is never
// accidentally open. CHANGE THESE: "1234" is a four-digit password guarding client-
// confidential information, and this file lives in git.
//
// Edge Middleware is available on Hobby (free) as well as paid plans — no upgrade needed.

const FALLBACK_USER = 'abhi';
const FALLBACK_PASS = '1234';

// Protect everything, including /api. Vercel's own internal paths are excluded so the
// deployment can still serve its infrastructure requests.
export const config = {
  matcher: ['/((?!_vercel|_next/static|_next/image).*)'],
};

// Constant-time-ish comparison: avoids leaking the password length/prefix through timing.
// (Marginal for a short password, but free to do correctly.)
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const CHALLENGE = new Response('Authentication required.', {
  status: 401,
  headers: {
    'WWW-Authenticate': 'Basic realm="TMT Regulatory Radar", charset="UTF-8"',
    'Cache-Control': 'no-store',
  },
});

export default function middleware(request) {
  const user = process.env.AUTH_USER || FALLBACK_USER;
  const pass = process.env.AUTH_PASS || FALLBACK_PASS;

  const header = request.headers.get('authorization') || '';
  if (!header.startsWith('Basic ')) return CHALLENGE.clone();

  let decoded;
  try {
    decoded = atob(header.slice(6));
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
