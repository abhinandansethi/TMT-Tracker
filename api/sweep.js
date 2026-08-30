// Vercel serverless function — the real "Update now" trigger.
//
// A static page cannot hold a GitHub token (every viewer could read it), so the token lives
// here, in Vercel's encrypted environment variables, and never reaches the browser. The
// dashboard POSTs to /api/sweep; this function asks GitHub to run the sweep workflow.
//
// Configure in Vercel → Settings → Environment Variables:
//   a GitHub token with Actions: read & write on this repo, named any of TOKEN_NAMES below
//   (GITHUB_DISPATCH_TOKEN is canonical; TMT_TOKEN etc. are accepted, case-insensitively).
//
// The repository is derived automatically from Vercel's built-in VERCEL_GIT_REPO_OWNER /
// VERCEL_GIT_REPO_SLUG on a git-connected project; set GITHUB_REPO ("owner/repo") only to
// override that.
//
// Until a token is present the endpoint answers 501 naming exactly what it looked for, and the
// dashboard falls back to offering the GitHub "Run workflow" link. Nothing here ever claims a
// sweep ran that did not run, and no token value is ever echoed, logged, or returned.
//
// NOTE ON ACCESS: this endpoint inherits the deployment's protection. On an unprotected
// deployment anyone with the URL could trigger a sweep (spending Actions minutes and hitting
// government sites in the firm's name). Keep Vercel Deployment Protection on.

const TOKEN_NAMES = [
  'GITHUB_DISPATCH_TOKEN', 'TMT_TOKEN', 'TMT_DISPATCH_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
];

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

module.exports = async (req, res) => {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ ok: false, message: 'POST only.' });
  }

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
  const url = `https://api.github.com/repos/${repo}/actions/workflows/sweep.yml/dispatches`;
  try {
    const gh = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'tmt-radar-dashboard',
      },
      body: JSON.stringify({ ref: branch }),
    });

    // 204 No Content is GitHub's success for a workflow dispatch.
    if (gh.status === 204) {
      return res.status(202).json({
        ok: true,
        message: 'Sweep started on GitHub Actions. It checks every source, regenerates the '
          + 'briefs and rebuilds this page — a few minutes. Reload when it finishes.',
      });
    }

    // Report GitHub's own reason rather than a generic failure.
    const detail = await gh.text();
    let reason = '';
    try { reason = JSON.parse(detail).message || ''; } catch (e) { reason = ''; }
    const hint = gh.status === 401 ? ' The token is invalid or has expired.'
      : gh.status === 403 ? ' The token likely lacks Actions: read & write on this repository.'
      : gh.status === 404 ? ` Checked ${repo} for .github/workflows/sweep.yml on branch ${branch} —`
        + ' a 404 here usually means the token cannot see this repository.'
      : '';
    return res.status(502).json({
      ok: false,
      message: `GitHub declined the request (${gh.status}${reason ? ': ' + reason : ''}).${hint}`,
    });
  } catch (e) {
    return res.status(502).json({ ok: false, message: 'Could not reach the GitHub API.' });
  }
};
