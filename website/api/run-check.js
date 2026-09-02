/**
 * Triggers a fresh engine run on demand, from the Refresh button.
 *
 * The engine (run_check_v2.py) is Python and does several sequential Yahoo/
 * Kraken calls per asset across multiple timeframes -- it cannot run inside
 * this Vercel project's Node serverless functions, and would very likely
 * blow the Hobby plan's 10s execution limit even as a Python function here.
 * Instead this dispatches the SAME GitHub Actions workflow the 15-minute
 * schedule cron already runs (.github/workflows/signal-check.yml), reusing the real
 * engine unmodified. The repo is public, so Actions minutes are free and
 * unlimited -- no quota risk from users tapping Refresh repeatedly.
 *
 * Requires a GH_DISPATCH_TOKEN env var in Vercel: a GitHub fine-grained
 * personal access token scoped to ONLY this repo, with "Actions:
 * Read and write" permission and nothing else. Without it this endpoint
 * degrades to a clear error rather than silently doing nothing -- the
 * Refresh button already falls back to a plain data re-read either way.
 */

const OWNER = "xenovasoft";
const REPO = "trading-app";
const WORKFLOW = "signal-check.yml";

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    return res.status(405).json({ error: "POST only" });
  }
  const token = process.env.GH_DISPATCH_TOKEN;
  if (!token) {
    return res.status(501).json({
      error: "GH_DISPATCH_TOKEN not configured",
      detail: "engine trigger unavailable -- add a GitHub token scoped to " +
        "this repo (Actions: read/write) as GH_DISPATCH_TOKEN in Vercel " +
        "project settings",
    });
  }
  try {
    const r = await fetch(
      `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );
    if (r.status === 204) {
      return res.status(200).json({ triggered: true });
    }
    const body = await r.text();
    return res.status(r.status).json({
      error: `github ${r.status}`,
      detail: body.slice(0, 500),
    });
  } catch (e) {
    return res.status(502).json({ error: String(e && e.message) || "dispatch failed" });
  }
}
