/**
 * Shared by /api/auth and /api/account. The leading underscore keeps Vercel
 * from serving this file as an endpoint of its own.
 *
 * Every format here matches accounts.py byte for byte -- PBKDF2-SHA256
 * password hashes, HMAC-SHA256 session tokens, the wallet rules -- so a user
 * created with `python accounts.py seed --remote` logs in here, and the
 * engine reads the rows these endpoints write.
 *
 * Both endpoints need two secrets in the Vercel project:
 *   SUPABASE_SECRET_KEY  the user tables have RLS on and no policies, so the
 *                        publishable key can read none of them
 *   AUTH_SECRET          32+ random characters; signs the login cookie
 * Without either they answer 501 naming what to set, as paper-control does.
 */
import crypto from "node:crypto";

export const SUPABASE_URL =
  process.env.SUPABASE_URL || "https://vjjnowmsvngijxvkvqhx.supabase.co";
export const COOKIE = "se_session";
export const SESSION_MAX_AGE = 30 * 86400;
const PBKDF2_ITER = 210000;
const USERNAME_RE = /^[a-z0-9_.-]{3,32}$/;
// Stripe's published test numbers: a checkout that feels real but cannot
// accept a real card.
const DEMO_CARD = "4242424242424242";
const DECLINE_CARD = "4000000000000002";
export const MAX_DEPOSIT = 1000000;

export function config() {
  const key = process.env.SUPABASE_SECRET_KEY;
  const secret = process.env.AUTH_SECRET;
  if (!key) {
    return {
      error: "SUPABASE_SECRET_KEY not configured",
      detail: "accounts unavailable -- add the Supabase service key as SUPABASE_SECRET_KEY in Vercel project settings",
    };
  }
  if (!secret || secret.length < 32) {
    return {
      error: "AUTH_SECRET not configured",
      detail: "accounts unavailable -- set AUTH_SECRET in Vercel project settings to a random string of 32 or more characters",
    };
  }
  return { key, secret };
}

export async function db(key, path, { method = "GET", body, prefer, query } = {}) {
  const url = new URL(`${SUPABASE_URL}/rest/v1/${path}`);
  for (const [k, v] of Object.entries(query || {})) url.searchParams.set(k, v);
  const headers = {
    apikey: key,
    Authorization: `Bearer ${key}`,
    "Content-Type": "application/json",
  };
  if (prefer) headers.Prefer = prefer;
  const r = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  return { ok: r.ok, status: r.status, data };
}

export function fail(r) {
  return new Error(`supabase ${r.status}: ${JSON.stringify(r.data).slice(0, 300)}`);
}

export function parseBody(req) {
  return typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body || {};
}

export function money(v) {
  return "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/* ---------------------------------------------------------------- auth */

export function hashPassword(pw) {
  const salt = crypto.randomBytes(16);
  const dk = crypto.pbkdf2Sync(pw, salt, PBKDF2_ITER, 32, "sha256");
  return `pbkdf2_sha256$${PBKDF2_ITER}$${salt.toString("base64")}$${dk.toString("base64")}`;
}

export function verifyPassword(pw, stored) {
  try {
    const [algo, it, salt, want] = String(stored).split("$");
    if (algo !== "pbkdf2_sha256") return false;
    const dk = crypto.pbkdf2Sync(pw, Buffer.from(salt, "base64"), Number(it), 32, "sha256");
    const w = Buffer.from(want, "base64");
    return w.length === dk.length && crypto.timingSafeEqual(dk, w);
  } catch {
    return false;
  }
}

export function signSession(secret, id, username) {
  const exp = Math.floor(Date.now() / 1000) + SESSION_MAX_AGE;
  const body = `v1.${Number(id)}.${Buffer.from(username).toString("base64url")}.${exp}`;
  const sig = crypto.createHmac("sha256", secret).update(body).digest("base64url");
  return `${body}.${sig}`;
}

export function readSession(secret, token) {
  try {
    const [v, id, name, exp, sig] = String(token || "").split(".");
    if (v !== "v1" || !sig) return null;
    const want = crypto.createHmac("sha256", secret).update(`${v}.${id}.${name}.${exp}`).digest();
    const got = Buffer.from(sig, "base64url");
    if (got.length !== want.length || !crypto.timingSafeEqual(got, want)) return null;
    if (Number(exp) < Date.now() / 1000) return null;
    return { id: Number(id), username: Buffer.from(name, "base64url").toString() };
  } catch {
    return null;
  }
}

export function sessionUser(req, secret) {
  const raw = req.headers.cookie || "";
  for (const part of raw.split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === COOKIE) {
      return readSession(secret, decodeURIComponent(part.slice(i + 1).trim()));
    }
  }
  return null;
}

export function sessionCookie(req, token, maxAge) {
  // Secure everywhere but plain-http localhost (vercel dev).
  const proto = req.headers["x-forwarded-proto"] || "https";
  return `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}` +
    (proto === "https" ? "; Secure" : "");
}

export function checkUsername(raw) {
  const u = String(raw || "").trim().toLowerCase();
  return [u, USERNAME_RE.test(u) ? null : "username must be 3-32 characters: letters, digits, . _ or -"];
}

export function checkPassword(pw) {
  if (typeof pw !== "string" || pw.length < 8) return "password must be at least 8 characters";
  if (pw.length > 128) return "password must be at most 128 characters";
  return null;
}

/* -------------------------------------------------------------- wallet */

export function parseAmount(v, hi, what = "amount") {
  const a = Math.round(Number(v) * 100) / 100;
  if (v == null || v === "" || !Number.isFinite(a)) return [null, `${what} must be a number`];
  if (a < 1) return [null, `${what} must be at least $1`];
  if (a > hi) return [null, `${what} can be at most ${money(hi)}`];
  return [a, null];
}

export function checkCard(card) {
  const digits = String(card || "").replace(/\D/g, "");
  if (digits === DECLINE_CARD) return [null, 402, "card declined (demo decline card)"];
  if (digits !== DEMO_CARD) {
    return [null, 400, "demo mode accepts only the test card 4242 4242 4242 4242 -- never enter a real card"];
  }
  return [`Demo card •••• ${digits.slice(-4)}`, 200, null];
}

// method "instant" adds demo money with no payment details; anything else
// goes through the card check.
export function depositMethod(b) {
  if (b.method === "instant") return ["Instant demo top-up", 200, null];
  return checkCard(b.card);
}

export function walletBalance(txs) {
  return Math.round(txs.reduce((s, t) => s + Number(t.amount || 0), 0) * 100) / 100;
}

export function armAmount(requested) {
  const b = Number(String(requested || "").split(":")[1]);
  return Number.isFinite(b) ? b : 0;
}
