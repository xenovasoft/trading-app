/**
 * Log in, create an account, log out, or ask who is logged in.
 *
 *   GET  /api/auth                                  -> { user | null }
 *   POST /api/auth { action: "login",    username, password }
 *   POST /api/auth { action: "register", username, password }
 *   POST /api/auth { action: "logout" }
 *
 * The session is an HttpOnly cookie holding a signed token (see _lib.js), so
 * page scripts never see it and it works the same in the iOS app, which
 * loads this site in a WKWebView.
 */
import {
  checkPassword,
  checkUsername,
  config,
  db,
  fail,
  hashPassword,
  parseBody,
  SESSION_MAX_AGE,
  sessionCookie,
  sessionUser,
  signSession,
  verifyPassword,
} from "./_lib.js";

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  const cfg = config();
  if (cfg.error) return res.status(501).json(cfg);

  if (req.method === "GET") {
    return res.status(200).json({ user: sessionUser(req, cfg.secret) });
  }
  if (req.method !== "POST") return res.status(405).json({ error: "GET or POST only" });

  let b;
  try {
    b = parseBody(req);
  } catch {
    return res.status(400).json({ error: "body must be JSON" });
  }

  if (b.action === "logout") {
    res.setHeader("Set-Cookie", sessionCookie(req, "", 0));
    return res.status(200).json({ ok: true });
  }

  const [username, nameErr] = checkUsername(b.username);
  let user;
  try {
    if (b.action === "register") {
      const err = nameErr || checkPassword(b.password);
      if (err) return res.status(400).json({ error: err });
      const r = await db(cfg.key, "app_users", {
        method: "POST",
        body: [{ username, password_hash: hashPassword(b.password) }],
        prefer: "return=representation",
        query: { select: "id,username" },
      });
      if (r.status === 409) return res.status(409).json({ error: "that username is taken" });
      if (!r.ok) throw fail(r);
      user = r.data[0];
    } else if (b.action === "login") {
      // Same answer for an unknown name and a wrong password, so the form
      // can't be used to find out which usernames exist.
      const bad = () => res.status(401).json({ error: "wrong username or password" });
      if (nameErr) return bad();
      const r = await db(cfg.key, "app_users", {
        query: { username: `eq.${username}`, select: "id,username,password_hash" },
      });
      if (!r.ok) throw fail(r);
      const u = r.data[0];
      if (!u || !verifyPassword(String(b.password || ""), u.password_hash)) return bad();
      user = { id: u.id, username: u.username };
    } else {
      return res.status(400).json({ error: "action must be login, register or logout" });
    }
  } catch (e) {
    return res.status(502).json({ error: "account store failed", detail: String(e.message || e) });
  }

  res.setHeader("Set-Cookie", sessionCookie(req, signSession(cfg.secret, user.id, user.username), SESSION_MAX_AGE));
  return res.status(200).json({ ok: true, user: { id: user.id, username: user.username } });
}
