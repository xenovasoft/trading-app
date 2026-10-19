/**
 * Arm or stop the paper-trading forward test from the website.
 *
 * This writes an INTENT, it does not act. The engine lives in a 15-minute
 * GitHub Actions cron and this is a Vercel serverless function; they share no
 * process. The next engine cycle calls papertrader.apply_remote_intent(),
 * honours the request and clears it.
 *
 * So Stop is not instantaneous -- it lands within one cycle. The UI says so
 * rather than spinning as if something happened immediately. The limits that
 * actually protect the session (drawdown, sample size, staleness, feed
 * freshness) are enforced engine-side in check_auto_stop() precisely because
 * they must not depend on a web request being delivered.
 *
 * Writes need a key that can write: the publishable key is read-only under
 * RLS. Set SUPABASE_SECRET_KEY in the Vercel project. Without it this endpoint
 * returns a clear 501 instead of silently appearing to work.
 *
 * PAPER_CONTROL_TOKEN, if set, is required as an x-paper-token header. The
 * blast radius here is a simulated session with no money attached, but an
 * unauthenticated endpoint that resets someone's running sample is still worth
 * a token.
 */

const SUPABASE_URL =
  process.env.SUPABASE_URL || "https://vjjnowmsvngijxvkvqhx.supabase.co";

const VALID = new Set(["arm", "disarm"]);

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");

  if (req.method !== "POST") {
    return res.status(405).json({ error: "POST only" });
  }

  const gate = process.env.PAPER_CONTROL_TOKEN;
  if (gate && req.headers["x-paper-token"] !== gate) {
    return res.status(401).json({ error: "bad or missing x-paper-token" });
  }

  const key = process.env.SUPABASE_SECRET_KEY;
  if (!key) {
    return res.status(501).json({
      error: "SUPABASE_SECRET_KEY not configured",
      detail:
        "paper control unavailable -- add the Supabase service key as " +
        "SUPABASE_SECRET_KEY in Vercel project settings. The publishable " +
        "key is read-only under RLS and cannot write the request.",
    });
  }

  let action = null;
  let balance = 1000;
  let leverage = 1;
  try {
    const body =
      typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body || {};
    action = body.action;
    if (body.balance != null) balance = Number(body.balance);
    if (body.leverage != null) leverage = Number(body.leverage);
  } catch {
    return res.status(400).json({ error: "body must be JSON" });
  }
  // Same bounds papertrader.parse_arm() enforces. Rejected here so the user
  // sees the error now, instead of the engine silently substituting a
  // default 15 minutes later.
  if (action === "arm") {
    if (!Number.isFinite(balance) || balance < 1 || balance > 1e9) {
      return res.status(400).json({ error: "balance must be between 1 and 1,000,000,000" });
    }
    if (!Number.isFinite(leverage) || leverage < 1 || leverage > 10) {
      return res.status(400).json({ error: "leverage must be between 1 and 10" });
    }
  }

  if (!VALID.has(action)) {
    return res
      .status(400)
      .json({ error: `action must be one of: ${[...VALID].join(", ")}` });
  }

  const headers = {
    apikey: key,
    Authorization: `Bearer ${key}`,
    "Content-Type": "application/json",
    Prefer: "resolution=merge-duplicates",
  };

  try {
    const r = await fetch(`${SUPABASE_URL}/rest/v1/paper_state`, {
      method: "POST",
      headers,
      body: JSON.stringify([
        {
          id: 1,
          // The amount rides inside the intent string: this column is the only
          // channel between the page and the engine, which share no process.
          requested: action === "arm" ? `arm:${balance}:${leverage}` : action,
          updated_at: new Date().toISOString(),
        },
      ]),
    });
    if (!r.ok) {
      const detail = await r.text();
      return res
        .status(r.status)
        .json({ error: `supabase ${r.status}`, detail: detail.slice(0, 500) });
    }
    return res.status(200).json({
      ok: true,
      requested: action,
      detail:
        action === "arm"
          ? `a fresh paper account with $${balance.toLocaleString()} at ${leverage}x will start on the next engine cycle (within 15 minutes)`
          : "the session will stop on the next engine cycle (within 15 minutes)",
    });
  } catch (e) {
    return res.status(502).json({ error: "write failed", detail: String(e) });
  }
}
