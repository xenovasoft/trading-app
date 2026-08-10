/**
 * Real-time-ish price ticker, same-origin (see api/signals.js for why: a
 * cross-origin fetch fails inside the iOS Capacitor WKWebView, and neither
 * Kraken nor Yahoo Finance reliably sends CORS headers for a browser fetch
 * anyway, so this has to be server-side regardless of platform).
 *
 * BTCUSD -> Kraken XBTUSD. XAUUSD -> Kraken PAXGUSD, a token redeemable 1:1
 * for physical LBMA gold, so arbitrage keeps it pinned to real spot gold —
 * verified live within 0.15% of a user-reported real fill, vs the COMEX
 * GC=F futures this used to read, which was $60+ (1.5%) off spot on the
 * same day and silently fed that gap into every XAUUSD entry/stop/target
 * dataio.py computed (see dataio.ASSETS). XAGUSD stays on Yahoo SI=F
 * futures: no tokenized silver exists on Kraken or Binance (checked both),
 * so there is no free spot-tracking proxy for it yet.
 *
 * This is intentionally separate from signals_v2.price: that number is a
 * snapshot taken AT THE MOMENT the engine last ran and is what the
 * entry/stop/target levels were actually calculated against, so it must
 * stay frozen between cron runs. This endpoint is for the live "what is the
 * market doing right now" ticker only.
 */

const UA = { "User-Agent": "Mozilla/5.0" };

async function krakenTicker(pair) {
  const r = await fetch(`https://api.kraken.com/0/public/Ticker?pair=${pair}`);
  const j = await r.json();
  if (j.error && j.error.length) throw new Error(j.error.join(", "));
  const key = Object.keys(j.result)[0];
  const t = j.result[key];
  const price = parseFloat(t.c[0]);
  const open = parseFloat(t.o);
  return {
    price,
    high24h: parseFloat(t.h[1]),
    low24h: parseFloat(t.l[1]),
    change24h: price - open,
    changePct24h: open ? (100 * (price - open)) / open : null,
    source: "kraken",
  };
}

async function yahooTicker(symbol) {
  const r = await fetch(
    `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?interval=1m&range=1d`,
    { headers: UA }
  );
  const j = await r.json();
  const meta = j?.chart?.result?.[0]?.meta;
  if (!meta) throw new Error("no meta in yahoo response");
  const price = meta.regularMarketPrice;
  const prev = meta.previousClose ?? meta.chartPreviousClose;
  return {
    price,
    high24h: meta.regularMarketDayHigh,
    low24h: meta.regularMarketDayLow,
    change24h: prev != null ? price - prev : null,
    changePct24h: prev ? (100 * (price - prev)) / prev : null,
    source: "yahoo",
  };
}

const FETCHERS = {
  BTCUSD: () => krakenTicker("XBTUSD"),
  XAUUSD: () => krakenTicker("PAXGUSD"),
  XAGUSD: () => yahooTicker("SI=F"),
};

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  const asset = String(req.query.asset || "").toUpperCase();
  const fetcher = FETCHERS[asset];
  if (!fetcher) {
    return res.status(400).json({ error: `unknown asset '${asset}'`, known: Object.keys(FETCHERS) });
  }
  try {
    const data = await fetcher();
    return res.status(200).json({ asset, ...data, fetched_at: new Date().toISOString() });
  } catch (e) {
    return res.status(502).json({ error: String(e && e.message) || "fetch failed" });
  }
}
