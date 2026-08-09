/**
 * OHLC candle data for the chart, same-origin for the same reason as every
 * other /api route here (WKWebView cross-origin fetch + Kraken/Yahoo don't
 * reliably send CORS headers for browser fetches anyway).
 *
 * Returns candles in the shape lightweight-charts expects directly:
 *   [{ time: <unix seconds>, open, high, low, close }, ...]
 */

const UA = { "User-Agent": "Mozilla/5.0" };

const KRAKEN_TF = { "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440 };
const YAHOO_TF = {
  "5m": { interval: "5m", range: "5d" },
  "15m": { interval: "15m", range: "5d" },
  "1h": { interval: "60m", range: "1mo" },
  "4h": { interval: "60m", range: "3mo" }, // resampled to 4h below
  "1d": { interval: "1d", range: "1y" },
};

function resampleTo4h(candles) {
  const buckets = new Map();
  for (const c of candles) {
    const bucketStart = Math.floor(c.time / (4 * 3600)) * 4 * 3600;
    let b = buckets.get(bucketStart);
    if (!b) {
      b = { time: bucketStart, open: c.open, high: c.high, low: c.low, close: c.close };
      buckets.set(bucketStart, b);
    } else {
      b.high = Math.max(b.high, c.high);
      b.low = Math.min(b.low, c.low);
      b.close = c.close;
    }
  }
  return [...buckets.values()].sort((a, b) => a.time - b.time);
}

async function krakenCandles(tf) {
  const minutes = KRAKEN_TF[tf];
  const r = await fetch(
    `https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=${minutes}`
  );
  const j = await r.json();
  if (j.error && j.error.length) throw new Error(j.error.join(", "));
  const key = Object.keys(j.result).find((k) => k !== "last");
  return j.result[key].map((row) => ({
    time: row[0],
    open: parseFloat(row[1]),
    high: parseFloat(row[2]),
    low: parseFloat(row[3]),
    close: parseFloat(row[4]),
  }));
}

async function yahooCandles(symbol, tf) {
  const { interval, range } = YAHOO_TF[tf];
  const r = await fetch(
    `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?interval=${interval}&range=${range}`,
    { headers: UA }
  );
  const j = await r.json();
  const res = j?.chart?.result?.[0];
  if (!res) throw new Error("no result in yahoo response");
  const ts = res.timestamp || [];
  const q = res.indicators?.quote?.[0] || {};
  const out = [];
  for (let i = 0; i < ts.length; i++) {
    if (
      q.open?.[i] == null || q.high?.[i] == null ||
      q.low?.[i] == null || q.close?.[i] == null
    ) continue;
    out.push({ time: ts[i], open: q.open[i], high: q.high[i], low: q.low[i], close: q.close[i] });
  }
  return tf === "4h" ? resampleTo4h(out) : out;
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  const asset = String(req.query.asset || "").toUpperCase();
  const tf = String(req.query.tf || "1h");
  if (!["5m", "15m", "1h", "4h", "1d"].includes(tf)) {
    return res.status(400).json({ error: `unknown timeframe '${tf}'` });
  }
  try {
    let candles;
    if (asset === "BTCUSD") candles = await krakenCandles(tf);
    else if (asset === "XAUUSD") candles = await yahooCandles("GC=F", tf);
    else if (asset === "XAGUSD") candles = await yahooCandles("SI=F", tf);
    else return res.status(400).json({ error: `unknown asset '${asset}'` });

    return res.status(200).json({ asset, tf, candles });
  } catch (e) {
    return res.status(502).json({ error: String(e && e.message) || "fetch failed" });
  }
}
