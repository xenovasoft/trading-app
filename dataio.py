"""
Data acquisition and integrity gating.

The single most important guarantee in this module: everything downstream sees
CLOSED BARS ONLY. The final, still-forming bar is stripped here and exposed
separately as `current_price`. That kills the main repainting vector at source
rather than relying on every consumer to remember.
"""

import datetime
import pandas as pd
import requests

import config

UA = {"User-Agent": "Mozilla/5.0"}


def _utcnow():
    return pd.Timestamp.now("UTC").tz_localize(None)


# ------------------------------------------------------------------ FETCH ---

def fetch_yahoo(symbol, interval, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": interval, "range": rng},
                     headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "time": pd.to_datetime(res["timestamp"], unit="s"),
        "open": q["open"], "high": q["high"], "low": q["low"],
        "close": q["close"], "volume": q["volume"],
    }).dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0)
    return df.reset_index(drop=True), res.get("meta", {})


def fetch_kraken(pair, interval_minutes):
    """Kraken is used for BTC because Binance returns HTTP 451 to US-hosted
    CI runners, which silently killed BTC on every GitHub Actions run."""
    r = requests.get("https://api.kraken.com/0/public/OHLC",
                     params={"pair": pair, "interval": interval_minutes},
                     timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")
    key = next(k for k in data["result"] if k != "last")
    df = pd.DataFrame(data["result"][key],
                      columns=["time", "open", "high", "low", "close",
                               "vwap", "volume", "count"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True), {}


def resample_ohlcv(df, rule):
    out = (df.set_index("time")
             .resample(rule)
             .agg({"open": "first", "high": "max", "low": "min",
                   "close": "last", "volume": "sum"})
             .dropna(subset=["open", "high", "low", "close"]))
    return out.reset_index()


# -------------------------------------------------------------- INTEGRITY ---

def validate_frame(df, label, expected_minutes):
    """Return (df_closed, report). Never raises on bad data — the caller
    decides, and bad data must produce NO TRADE rather than a guess."""
    report = {
        "timeframe": label,
        "raw_bars": len(df),
        "issues": [],
        "stale": False,
        "usable": True,
    }

    if len(df) < 30:
        report["issues"].append(f"only {len(df)} bars returned")
        report["usable"] = False
        return df, report

    # OHLC sanity
    bad = df[(df["high"] < df["low"]) |
             (df["high"] < df["close"]) | (df["high"] < df["open"]) |
             (df["low"] > df["close"]) | (df["low"] > df["open"])]
    if len(bad):
        report["issues"].append(f"{len(bad)} bars violate OHLC ordering")

    if df["time"].duplicated().any():
        n = int(df["time"].duplicated().sum())
        report["issues"].append(f"{n} duplicate timestamps")
        df = df.drop_duplicates(subset="time", keep="last").reset_index(drop=True)

    if not df["time"].is_monotonic_increasing:
        report["issues"].append("timestamps not monotonic; sorted")
        df = df.sort_values("time").reset_index(drop=True)

    # Drop the still-forming final bar. This is the anti-repaint guarantee.
    last_open_time = df["time"].iloc[-1]
    age_min = (_utcnow() - last_open_time).total_seconds() / 60.0
    forming = age_min < expected_minutes
    df_closed = df.iloc[:-1].reset_index(drop=True) if forming else df.copy()
    report["dropped_forming_bar"] = bool(forming)
    report["closed_bars"] = len(df_closed)

    # Staleness measured on the last CLOSED bar
    last_closed = df_closed["time"].iloc[-1]
    closed_age = (_utcnow() - last_closed).total_seconds() / 60.0
    report["last_closed_bar"] = str(last_closed)
    report["last_closed_age_min"] = round(closed_age, 1)

    limit = config.MAX_BAR_AGE_MINUTES.get(label)
    if limit and closed_age > limit:
        report["stale"] = True
        report["stale_reason"] = (
            f"last closed bar is {closed_age/60:.1f}h old "
            f"(limit {limit/60:.1f}h) — market likely closed")
        report["issues"].append(report["stale_reason"])

    zero_vol = int((df_closed["volume"] == 0).sum())
    if zero_vol:
        report["issues"].append(
            f"{zero_vol}/{len(df_closed)} bars have zero volume "
            f"(volume-derived signals degraded)")
    report["zero_volume_bars"] = zero_vol

    return df_closed, report


def indicator_availability(n_bars):
    """Which indicators have enough history to be meaningful on this frame."""
    return {name: n_bars >= need for name, need in config.MIN_BARS_FOR.items()}


# ----------------------------------------------------------------- ASSETS ---

ASSETS = {
    "XAUUSD": {"source": "yahoo", "symbol": "GC=F"},
    "XAGUSD": {"source": "yahoo", "symbol": "SI=F"},
    "BTCUSD": {"source": "kraken", "symbol": "XBTUSD"},
}

TF_MINUTES = {"5M": 5, "15M": 15, "1H": 60, "4H": 240,
              "Daily": 1440, "Weekly": 10080, "Monthly": 43200}


def load_asset(name):
    """Returns (frames, reports, current_price, meta).

    frames[tf] is a DataFrame of CLOSED bars only.
    current_price is taken from the live/forming bar when available — it is
    used ONLY for distance measurement and entry-zone checks, never to
    generate a structural signal.
    """
    cfg = ASSETS[name]
    frames, reports = {}, {}
    raw_last_close = None

    if cfg["source"] == "yahoo":
        spec = {
            "Monthly": ("1mo", "10y"), "Weekly": ("1wk", "10y"),
            "Daily": ("1d", "2y"), "1H": ("60m", "730d"),
            "15M": ("15m", "60d"), "5M": ("5m", "60d"),
        }
        for tf, (interval, rng) in spec.items():
            raw, meta = fetch_yahoo(cfg["symbol"], interval, rng)
            if tf == "5M" and len(raw):
                raw_last_close = float(raw["close"].iloc[-1])
            frames[tf], reports[tf] = validate_frame(raw, tf, TF_MINUTES[tf])
        # 4H is derived; Yahoo has no native 4H for these symbols.
        h4 = resample_ohlcv(frames["1H"], "4h")
        frames["4H"], reports["4H"] = validate_frame(h4, "4H", TF_MINUTES["4H"])
        reports["4H"]["issues"].append(
            "4H is resampled from 1H on UTC boundaries; bins may not match "
            "your broker's 4H candles")
    else:
        for tf, mins in [("Weekly", 10080), ("Daily", 1440), ("4H", 240),
                         ("1H", 60), ("15M", 15), ("5M", 5)]:
            raw, _ = fetch_kraken(cfg["symbol"], mins)
            if tf == "5M" and len(raw):
                raw_last_close = float(raw["close"].iloc[-1])
            frames[tf], reports[tf] = validate_frame(raw, tf, mins)
        monthly = resample_ohlcv(frames["Daily"], "ME")
        frames["Monthly"], reports["Monthly"] = validate_frame(
            monthly, "Monthly", TF_MINUTES["Monthly"])
        reports["Monthly"]["issues"].append(
            "Monthly derived from ~2y of daily data; long-lookback monthly "
            "indicators are unreliable")

    if raw_last_close is None:
        raw_last_close = float(frames["5M"]["close"].iloc[-1])

    meta = {
        "asset": name,
        "source": cfg["source"],
        "symbol": cfg["symbol"],
        "instrument_note": config.CONTRACT_SPECS[name]["instrument"],
        "fetched_at_utc": str(_utcnow()),
    }
    return frames, reports, raw_last_close, meta


def data_blockers(reports):
    """Hard reasons to refuse to trade, and soft warnings."""
    blockers, warnings = [], []
    for tf in ["5M", "15M", "1H", "4H", "Daily"]:
        rep = reports.get(tf)
        if not rep:
            blockers.append(f"{tf} frame missing entirely")
            continue
        if not rep["usable"]:
            blockers.append(f"{tf}: {'; '.join(rep['issues'])}")
        elif rep["stale"]:
            blockers.append(f"{tf}: {rep.get('stale_reason', 'stale data')}")
        if rep["usable"]:
            for msg in rep["issues"]:
                if msg != rep.get("stale_reason"):
                    warnings.append(f"{tf}: {msg}")
    return blockers, warnings
