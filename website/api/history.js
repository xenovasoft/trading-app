/**
 * Same-origin proxy for the last 24h of the `alerts` table — same reasoning
 * as api/signals.js: a cross-origin fetch to supabase.co fails inside the
 * iOS Capacitor WKWebView, so this serves it from the page's own origin.
 *
 * Only NEW_SIGNAL rows matter for the "trade history" view (each one is a
 * distinct LONG/SHORT call); everything else (BOS_CHOCH, LIQUIDITY_SWEEP,
 * data-gate notices) is noise for that specific view, so it's filtered here
 * rather than shipping it to the client and filtering there.
 */

const SUPABASE_URL =
  process.env.SUPABASE_URL || "https://vjjnowmsvngijxvkvqhx.supabase.co";
const SUPABASE_KEY =
  process.env.SUPABASE_PUBLISHABLE_KEY ||
  "sb_publishable_FE879GUYSj-Pw1gp1-kZ2g_h9qbtHmP";

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  try {
    const since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
    const q = new URLSearchParams({
      select: "id,asset,profile,type,title,message,created_at",
      type: "eq.NEW_SIGNAL",
      created_at: `gte.${since}`,
      order: "created_at.desc",
      limit: "300",
    });
    const r = await fetch(`${SUPABASE_URL}/rest/v1/alerts?${q}`, {
      headers: {
        apikey: SUPABASE_KEY,
        Authorization: `Bearer ${SUPABASE_KEY}`,
      },
    });
    const body = await r.text();
    if (!r.ok) {
      return res
        .status(r.status)
        .json({ error: `supabase ${r.status}`, detail: body.slice(0, 500) });
    }
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.status(200).send(body);
  } catch (e) {
    return res.status(502).json({ error: String(e && e.message) || "fetch failed" });
  }
}
