"""
Historical OHLCV for backtesting only. NOT used by the live loop.

Why a separate module from dataio.py: the live feeds cannot supply enough
intraday history to backtest against.

  * Kraken's OHLC endpoint returns only the most recent ~720 candles and
    ignores `since` for going further back (verified empirically: asking for
    3, 10, 30 and 90 days ago all return the identical 2.5-day window). At 5M
    that is 2.5 days of history — far too little to evaluate anything.
  * Yahoo caps 5m/15m at 60 days.

Binance's klines endpoint paginates properly via startTime and holds years of
intraday history, so it is used here. It is deliberately NOT used in dataio.py
because Binance returns HTTP 451 to US-hosted GitHub Actions runners, which is
exactly why the live loop runs on Kraken.

Two consequences worth stating plainly:
  * The backtest prices BTC as Binance BTCUSDT, while the live engine prices
    it as Kraken XBTUSD. The two track each other closely but are not the same
    instrument, so backtested fills are an approximation of live fills.
  * Higher timeframes are fetched over a much longer span than the replay
    window, because the engine needs >= 30 bars on a timeframe before it will
    compute indicators there. A 90-day window alone would leave Weekly and
    Monthly too short to produce an HTF bias, silently crippling SWING.
"""

import os
import time

import pandas as pd
import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bt_cache")

BINANCE = "https://api.binance.com/api/v3/klines"
BINANCE_SYMBOL = {"BTCUSD": "BTCUSDT"}

# Binance interval code per engine timeframe.
INTERVAL = {
    "Monthly": "1M", "Weekly": "1w", "Daily": "1d",
    "4H": "4h", "1H": "1h", "15M": "15m", "5M": "5m",
}

# How far back to fetch each timeframe. The engine needs >= 30 bars per frame
# to compute indicators, so slow frames must reach back much further than the
# replay window itself.
LOOKBACK_DAYS = {
    "Monthly": 2000, "Weekly": 2000, "Daily": 1500,
    "4H": 700, "1H": 700,
}


def _fetch_klines(symbol, interval, start_ms, end_ms):
    """Page through Binance klines 1000 bars at a time."""
    out, cursor = [], start_ms
    while cursor < end_ms:
        r = requests.get(BINANCE, params={
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "endTime": end_ms, "limit": 1000,
        }, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        out.extend(rows)
        last_open = rows[-1][0]
        if last_open <= cursor:      # no forward progress, stop rather than spin
            break
        cursor = last_open + 1
        if len(rows) < 1000:
            break
        time.sleep(0.12)             # stay well inside the public rate limit
    return out


def _to_frame(rows):
    df = pd.DataFrame(rows, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbb", "tbq", "ignore"])
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df = df[["time", "open", "high", "low", "close", "volume"]]
    return df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)


def load_history(asset, window_days=180, use_cache=True):
    """Return frames[tf] -> DataFrame of closed bars, for backtesting.

    window_days sets how far back the fast frames (5M/15M) reach; that is the
    period the replay can actually cover. Slow frames always reach back
    LOOKBACK_DAYS so indicators on them are warm from the first replay step.
    """
    symbol = BINANCE_SYMBOL.get(asset)
    if not symbol:
        raise ValueError(f"no backtest history source configured for {asset}")

    os.makedirs(CACHE_DIR, exist_ok=True)
    now_ms = int(time.time() * 1000)
    frames = {}

    for tf, interval in INTERVAL.items():
        days = LOOKBACK_DAYS.get(tf, window_days)
        cache = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{days}d.pkl")
        # Cache is keyed by span, not by end time, so a stale cache would
        # silently backtest yesterday's data. Refuse anything over a day old.
        if use_cache and os.path.exists(cache) and \
                (time.time() - os.path.getmtime(cache)) < 86400:
            frames[tf] = pd.read_pickle(cache)
            continue
        start_ms = now_ms - days * 86400 * 1000
        rows = _fetch_klines(symbol, interval, start_ms, now_ms)
        df = _to_frame(rows)
        # Drop the final bar: it is still forming, and the entire engine is
        # built on the guarantee that it only ever sees closed bars.
        df = df.iloc[:-1].reset_index(drop=True)
        df.to_pickle(cache)
        frames[tf] = df
        print(f"  fetched {tf:8s} {len(df):6d} bars  "
              f"{df['time'].iloc[0]} -> {df['time'].iloc[-1]}")

    return frames
