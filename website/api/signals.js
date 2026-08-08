/**
 * Same-origin proxy for the Supabase read.
 *
 * Why this exists: inside the iOS Capacitor WKWebView, a cross-origin fetch
 * from the page to supabase.co fails with "TypeError: Load failed", while the
 * identical page loads fine in mobile Safari. The webview can clearly reach
 * this Vercel deployment (it loaded the page from here), so serving the data
 * from the same origin sidesteps the whole cross-origin path.
 *
 * The key below is the Supabase *publishable* key. It is already public in the
 * browser bundle and is read-only via RLS, so exposing it here changes nothing.
 */

const SUPABASE_URL =
  process.env.SUPABASE_URL || "https://vjjnowmsvngijxvkvqhx.supabase.co";
const SUPABASE_KEY =
  process.env.SUPABASE_PUBLISHABLE_KEY ||
  "sb_publishable_FE879GUYSj-Pw1gp1-kZ2g_h9qbtHmP";

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  try {
    const r = await fetch(
      `${SUPABASE_URL}/rest/v1/signals_v2?select=*`,
      {
        headers: {
          apikey: SUPABASE_KEY,
          Authorization: `Bearer ${SUPABASE_KEY}`,
        },
      }
    );
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
