/**
 * Read the paper-trading forward test's current state.
 *
 * Same-origin proxy for the same reason /api/signals is one: a cross-origin
 * fetch to supabase.co fails inside the iOS Capacitor WKWebView while working
 * fine in mobile Safari.
 *
 * Reads the single row papertrader.py pushes at the end of every engine cycle.
 * A missing row is NOT an error -- it is the honest representation of "no
 * session has ever been armed", and the UI renders that state rather than an
 * error toast.
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
      `${SUPABASE_URL}/rest/v1/paper_state?id=eq.1&select=payload,requested,updated_at`,
      {
        headers: {
          apikey: SUPABASE_KEY,
          Authorization: `Bearer ${SUPABASE_KEY}`,
        },
      }
    );
    if (!r.ok) {
      const detail = await r.text();
      return res
        .status(r.status)
        .json({ error: `supabase ${r.status}`, detail: detail.slice(0, 500) });
    }
    const rows = await r.json();
    if (!rows.length) {
      return res.status(200).json({
        status: "DISARMED",
        never_run: true,
        note: "no paper session has been armed yet",
      });
    }
    const row = rows[0];
    return res.status(200).json({
      ...(row.payload || {}),
      pending_request: row.requested || null,
      updated_at: row.updated_at,
    });
  } catch (e) {
    return res.status(502).json({ error: "fetch failed", detail: String(e) });
  }
}
