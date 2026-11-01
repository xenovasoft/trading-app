/**
 * The logged-in user's own trading account and demo wallet.
 *
 *   GET  /api/account -> { user, wallet: {balance, in_trading, transactions}, paper }
 *   POST /api/account { action: "deposit", amount, method: "instant" }   no card needed
 *   POST /api/account { action: "deposit", amount, card }                 demo card checkout
 *   POST /api/account { action: "payout",  amount }
 *   POST /api/account { action: "start",   amount, leverage }
 *   POST /api/account { action: "stop" }
 *
 * `paper` has the same shape as /api/paper, so the terminal draws a user's
 * account with the code it already uses for the shared one.
 *
 * Deposits and payouts are ledger rows and take effect at once. Start and
 * Stop are INTENTS on the user's row, exactly like paper-control: the trading
 * account is engine-owned, and the next engine cycle acts on them
 * (accounts.user_cycle). The money moves out of the wallet when Start is
 * pressed and comes back when the engine has actually stopped the account.
 */
import {
  armAmount,
  config,
  db,
  depositMethod,
  fail,
  MAX_DEPOSIT,
  money,
  parseAmount,
  parseBody,
  sessionUser,
  walletBalance,
} from "./_lib.js";

async function load(key, uid) {
  const [p, t] = await Promise.all([
    db(key, "user_paper", { query: { user_id: `eq.${uid}`, select: "payload,requested,updated_at" } }),
    db(key, "wallet_tx", {
      query: { user_id: `eq.${uid}`, select: "id,kind,amount,method,note,created_at", order: "id.desc" },
    }),
  ]);
  if (!p.ok) throw fail(p);
  if (!t.ok) throw fail(t);
  const row = p.data[0] || null;
  // _state is the engine's full resume record; the page renders from the
  // snapshot fields beside it, as /api/paper does.
  const { _state, ...snap } = (row && row.payload) || {};
  return {
    snap,
    requested: (row && row.requested) || null,
    updated_at: row && row.updated_at,
    txs: t.data.map((x) => ({ ...x, amount: Number(x.amount) })),
  };
}

function view(user, cur) {
  const status = cur.snap.status || "DISARMED";
  const paper = { ...cur.snap, status, pending_request: cur.requested, updated_at: cur.updated_at };
  if (status === "DISARMED" && !cur.snap.closed_trades) paper.never_run = true;
  const running = status === "ARMED" && cur.snap.account;
  const inTrading = running
    ? cur.snap.account.balance
    : String(cur.requested || "").startsWith("arm") ? armAmount(cur.requested) : 0;
  return {
    user,
    wallet: { balance: walletBalance(cur.txs), in_trading: inTrading, transactions: cur.txs.slice(0, 50) },
    paper,
  };
}

async function addTx(key, row) {
  const r = await db(key, "wallet_tx", { method: "POST", body: [row] });
  if (!r.ok) throw fail(r);
}

async function setRequest(key, uid, requested) {
  const r = await db(key, "user_paper", {
    method: "POST",
    body: [{ user_id: uid, requested, updated_at: new Date().toISOString() }],
    prefer: "resolution=merge-duplicates",
  });
  if (!r.ok) throw fail(r);
}

async function act(key, user, cur, b) {
  const uid = user.id;
  const bal = walletBalance(cur.txs);
  const running = cur.snap.status === "ARMED";
  const pendingStart = String(cur.requested || "").startsWith("arm");

  if (b.action === "deposit") {
    const [amt, err] = parseAmount(b.amount, MAX_DEPOSIT, "deposit");
    if (err) return [400, { error: err }];
    const [method, code, cardErr] = depositMethod(b);
    if (cardErr) return [code, { error: cardErr }];
    await addTx(key, { user_id: uid, kind: "deposit", amount: amt, method, note: "demo deposit" });
    return [200, { ok: true, detail: `${money(amt)} added to your wallet` }];
  }

  if (b.action === "payout") {
    if (bal < 1) return [400, { error: "your wallet is empty" }];
    const [amt, err] = parseAmount(b.amount, bal, "payout");
    if (err) return [400, { error: err }];
    await addTx(key, { user_id: uid, kind: "payout", amount: -amt, method: "Demo bank account", note: "demo payout" });
    return [200, { ok: true, detail: `${money(amt)} paid out to your demo bank account` }];
  }

  if (b.action === "start") {
    if (running || pendingStart) return [409, { error: "your account is already running -- stop it first" }];
    if (bal < 1) return [400, { error: "your wallet is empty -- deposit first" }];
    const [amt, err] = parseAmount(b.amount, bal);
    if (err) return [400, { error: err }];
    const lev = Number(b.leverage == null ? 1 : b.leverage);
    if (!Number.isFinite(lev) || lev < 1 || lev > 10) return [400, { error: "leverage must be between 1 and 10" }];
    await addTx(key, { user_id: uid, kind: "allocate", amount: -amt, method: "Trading account", note: `started at ${lev}x` });
    try {
      await setRequest(key, uid, `arm:${amt}:${lev}`);
    } catch (e) {
      // Put the money back rather than leave it out of the wallet for an
      // account that will never start.
      await addTx(key, { user_id: uid, kind: "return", amount: amt, method: "Trading account", note: "start failed" });
      throw e;
    }
    return [200, {
      ok: true,
      detail: `trading will start with ${money(amt)} at ${lev}x on the next engine cycle (within 15 minutes)`,
    }];
  }

  if (b.action === "stop") {
    if (pendingStart) {
      // Not picked up yet: cancel it and refund now. The filter on the
      // current request means this only wins if the engine hasn't already
      // taken it; if it has, fall through to an ordinary stop.
      const r = await db(key, "user_paper", {
        method: "PATCH",
        query: { user_id: `eq.${uid}`, requested: `eq.${cur.requested}` },
        body: { requested: null, updated_at: new Date().toISOString() },
        prefer: "return=representation",
      });
      if (!r.ok) throw fail(r);
      if (r.data.length) {
        const amt = armAmount(cur.requested);
        await addTx(key, { user_id: uid, kind: "return", amount: amt, method: "Trading account", note: "start cancelled" });
        return [200, { ok: true, detail: `start cancelled; ${money(amt)} returned to your wallet` }];
      }
    } else if (!running) {
      return [409, { error: "your account isn't running" }];
    }
    await setRequest(key, uid, "disarm");
    return [200, {
      ok: true,
      detail: "trading will stop on the next engine cycle (within 15 minutes), and the balance goes back to your wallet",
    }];
  }

  return [400, { error: "action must be deposit, payout, start or stop" }];
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  const cfg = config();
  if (cfg.error) return res.status(501).json(cfg);
  const user = sessionUser(req, cfg.secret);
  if (!user) return res.status(401).json({ error: "log in first" });
  if (req.method !== "GET" && req.method !== "POST") return res.status(405).json({ error: "GET or POST only" });

  let b = {};
  if (req.method === "POST") {
    try {
      b = parseBody(req);
    } catch {
      return res.status(400).json({ error: "body must be JSON" });
    }
  }
  try {
    const cur = await load(cfg.key, user.id);
    if (req.method === "GET") return res.status(200).json(view(user, cur));
    const [code, out] = await act(cfg.key, user, cur, b);
    return res.status(code).json(out);
  } catch (e) {
    return res.status(502).json({ error: "account store failed", detail: String(e.message || e) });
  }
}
