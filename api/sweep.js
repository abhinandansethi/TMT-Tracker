// Vercel serverless function — the real "Update now" trigger.
//
// A static page cannot hold a GitHub token (every viewer could read it), so the token lives
// here, in Vercel's encrypted environment variables, and never reaches the browser. The
// dashboard POSTs to /api/sweep; this function asks GitHub to run the sweep workflow.
//
// Configure in Vercel → Settings → Environment Variables:
//   GITHUB_DISPATCH_TOKEN   a fine-grained PAT for this repo with Actions: read & write
//   GITHUB_REPO             owner/repo, e.g. abhinandansethi/TMT-Tracker
//
// Until both are set the endpoint answers 501 with a plain explanation, and the dashboard
// falls back to offering the GitHub "Run workflow" link. Nothing here ever claims a sweep
// ran that did not run.
//
// NOTE ON ACCESS: this endpoint inherits the deployment's protection. On an unprotected
// deployment anyone with the URL could trigger a sweep (spending Actions minutes and
// hitting government sites). Keep Vercel Deployment Protection on.

module.exports = async (req, res) => {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ ok: false, message: 'POST only.' });
  }

  const token = process.env.GITHUB_DISPATCH_TOKEN;
  const repo = process.env.GITHUB_REPO;
  if (!token || !repo) {
    return res.status(501).json({
      ok: false,
      message: 'This deployment has no sweep trigger configured yet. Add GITHUB_DISPATCH_TOKEN '
        + 'and GITHUB_REPO in Vercel → Settings → Environment Variables, then redeploy.',
    });
  }

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
      body: JSON.stringify({ ref: process.env.GITHUB_REF_NAME || 'main' }),
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
    return res.status(502).json({
      ok: false,
      message: `GitHub declined the request (${gh.status}${reason ? ': ' + reason : ''}). `
        + 'Check that the token has Actions: read & write on this repository and has not expired.',
    });
  } catch (e) {
    return res.status(502).json({ ok: false, message: 'Could not reach the GitHub API.' });
  }
};
