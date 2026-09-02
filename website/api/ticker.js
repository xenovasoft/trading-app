/**
 * Real-time-ish price ticker, same-origin (see api/signals.js for why: a
 * cross-origin fetch fails inside the iOS Capacitor WKWebView, and neither
 * Kraken nor Yahoo Finance reliably sends CORS headers for a browser fetch
 * anyway, so this has to be server-side regardless of platform).
 *
 * BTCUSD -> Kraken XBTUSD. XAGUSD -> Yahoo SI=F futures: no tokenized silver
 * exists on Kraken or Binance (checked both), so no free spot-tracking proxy
 * for it yet.
 *
 * XAUUSD DISPLAY price -> average of Kraken PAXGUSD and XAUTUSD (two
 * independently-run gold-backed tokens). Single-token tracking isn't as
 * tight as assumed: PAXG carried a PERSISTENT ~$11 (0.27%) premium over XAUT
 * across 721 hourly bars (~30 days) -- not noise that cancels out, a real
 * bias, confirmed on both Kraken and Binance so it's PAXG-the-asset, not a
 * Kraken liquidity quirk. A user cross-check against TradingView matched
 * XAUT almost exactly while PAXG was $9 high. Averaging two independent
 * tokens cancels idiosyncratic single-asset drift the way a Yahoo-only
 * futures read never could.
 *
 * The ENGINE (dataio.ASSETS, structure/liquidity analysis) stays on PAXGUSD
 * alone, not this average: XAUT's 5M bars are 40% zero-volume (vs PAXG's
 * ~15%), which would degrade SCALP's structure/liquidity-zone detection on
 * exactly the timeframe it depends on most. The ~0.27% price bias barely
 * moves relative structure (swing highs/ATR/zone distances); the volume gap
 * would have actively broken it. Display and engine deliberately use
 * different sources for different reasons -- see also the earlier GC=F fix,
 * where the same 1.5% bias DID silently corrupt entry/stop/target because
 * it fed the engine, not just the ticker.
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

async function goldTicker() {
  // If one token's request fails, fall back to the other rather than
  // erroring the whole ticker -- a single dead venue shouldn't take down
  // the gold price display.
  const [paxg, xaut] = await Promise.allSettled([
    krakenTicker("PAXGUSD"),
    krakenTicker("XAUTUSD"),
  ]);
  const ok = [paxg, xaut].filter((r) => r.status === "fulfilled").map((r) => r.value);
  if (!ok.length) throw new Error("both PAXGUSD and XAUTUSD ticker fetches failed");
  const avg = (key) => ok.reduce((s, v) => s + v[key], 0) / ok.length;
  const source =
    ok.length === 2 ? "kraken-paxg+xaut-avg"
    : paxg.status === "fulfilled" ? "kraken-paxg-only"
    : "kraken-xaut-only";
  return {
    price: avg("price"),
    high24h: avg("high24h"),
    low24h: avg("low24h"),
    change24h: avg("change24h"),
    changePct24h: avg("changePct24h"),
    source,
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
  XAUUSD: () => goldTicker(),
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
