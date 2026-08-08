"""
Pure indicator functions. Every function takes a frame of CLOSED bars and
returns a Series/tuple aligned to it — no globals, no side effects, no
knowledge of the forming bar.

Guard rails: functions that need a minimum history return NaN rather than a
confident-looking number computed from too little data.
"""

import numpy as np
import pandas as pd

import config


def _insufficient(series_like, n_needed, n_have):
    return n_have < n_needed


def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def sma(series, n):
    return series.rolling(n).mean()


def rsi(series, n=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series, fast=12, slow=26, signal=9):
    line = ema(series, fast) - ema(series, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def true_range(df):
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df, n=14):
    return true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_n = true_range(df).ewm(alpha=1 / n, adjust=False).mean()
    atr_safe = atr_n.replace(0, np.nan)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_safe
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_safe
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=1 / n, adjust=False).mean(), plus_di, minus_di


def bollinger(series, n=20, k=2):
    mid = series.rolling(n).mean()
    sd = series.rolling(n).std()
    return mid + k * sd, mid, mid - k * sd


def bandwidth(upper, mid, lower):
    return (upper - lower) / mid.replace(0, np.nan)


def obv(df):
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def supertrend(df, n=10, mult=3.0):
    """Iterative by nature (band memory). Returns (line, trend) where trend is
    +1 up / -1 down. First `n` values are not trustworthy and are NaN-ed."""
    a = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    upper = (hl2 + mult * a).to_numpy(copy=True)
    lower = (hl2 - mult * a).to_numpy(copy=True)
    close = df["close"].to_numpy()
    trend = np.ones(len(df), dtype=int)
    line = np.full(len(df), np.nan)

    for i in range(1, len(df)):
        if close[i] > upper[i - 1]:
            trend[i] = 1
        elif close[i] < lower[i - 1]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]
            if trend[i] == 1:
                lower[i] = max(lower[i], lower[i - 1])
            else:
                upper[i] = min(upper[i], upper[i - 1])
        line[i] = lower[i] if trend[i] == 1 else upper[i]

    line[:n] = np.nan
    return pd.Series(line, index=df.index), pd.Series(trend, index=df.index)


def ichimoku(df):
    high, low = df["high"], df["low"]
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = (tenkan + kijun) / 2
    senkou_b = (high.rolling(52).max() + low.rolling(52).min()) / 2
    # NOTE: senkou spans are NOT shifted forward here. Shifting them +26 and
    # then reading .iloc[-1] would read a value plotted into the future, which
    # is a look-ahead trap. We compare price to the CURRENT unshifted cloud.
    return tenkan, kijun, senkou_a, senkou_b


def pivot_points(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3
    return {
        "PP": pp,
        "R1": 2 * pp - prev_low, "S1": 2 * pp - prev_high,
        "R2": pp + (prev_high - prev_low), "S2": pp - (prev_high - prev_low),
        "R3": prev_high + 2 * (pp - prev_low), "S3": prev_low - 2 * (prev_high - pp),
    }


def session_of(ts):
    h = ts.hour
    for name, (start, end) in config.SESSIONS.items():
        if start <= h < end:
            return name
    return "Offhours"


def session_vwap(df, session_col=None):
    """TRUE session VWAP: resets at each session/day boundary.

    The previous implementation ran a cumulative VWAP over the entire frame
    (~71 days of 15M bars), producing a slow-moving average ~5% away from
    price that was above/below price for weeks at a time — effectively a
    free confirmation vote in any trend. This resets daily.
    """
    if not len(df):
        return None
    d = df.copy()
    day = d["time"].dt.floor("D")
    tp = (d["high"] + d["low"] + d["close"]) / 3
    pv = (tp * d["volume"]).groupby(day).cumsum()
    vv = d["volume"].groupby(day).cumsum().replace(0, np.nan)
    vwap = pv / vv
    val = vwap.iloc[-1]
    return float(val) if pd.notna(val) else None


def anchored_vwap(df, anchor_idx):
    """VWAP anchored to a specific bar (e.g. a swing high/low or session open)."""
    if anchor_idx is None or anchor_idx >= len(df):
        return None
    seg = df.iloc[anchor_idx:]
    tp = (seg["high"] + seg["low"] + seg["close"]) / 3
    vol = seg["volume"].sum()
    if vol <= 0:
        return None
    return float((tp * seg["volume"]).sum() / vol)


def volume_profile(df, bins=None):
    """Approximate volume-at-price from OHLCV by distributing each bar's
    volume uniformly across its high-low range.

    THIS IS AN APPROXIMATION. A true volume profile requires tick or
    volume-at-price data, which our feeds do not provide. Treat HVN/LVN
    derived from this as a weak prior, not a measured fact.
    """
    if not len(df) or df["volume"].sum() <= 0:
        return None
    bins = bins or config.LIQUIDITY["volume_profile_bins"]
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    acc = np.zeros(bins)

    for h, l, v in zip(df["high"].to_numpy(), df["low"].to_numpy(),
                       df["volume"].to_numpy()):
        if v <= 0 or h <= l:
            continue
        first = np.searchsorted(edges, l, side="right") - 1
        last = np.searchsorted(edges, h, side="left") - 1
        first = max(0, min(first, bins - 1))
        last = max(0, min(last, bins - 1))
        n = last - first + 1
        acc[first:last + 1] += v / n

    total = acc.sum()
    if total <= 0:
        return None
    poc_i = int(np.argmax(acc))
    hvn_cut = np.percentile(acc, config.LIQUIDITY["hvn_percentile"])
    lvn_cut = np.percentile(acc, config.LIQUIDITY["lvn_percentile"])
    return {
        "centers": centers,
        "volume": acc,
        "poc": float(centers[poc_i]),
        "hvn": [float(c) for c, a in zip(centers, acc) if a >= hvn_cut],
        "lvn": [float(c) for c, a in zip(centers, acc) if a <= lvn_cut and a > 0],
        "approximation": True,
    }


def compute_all(df, label):
    """Attach every indicator to a copy of the frame, respecting minimum-bar
    requirements. Returns (frame, availability_dict)."""
    d = df.copy()
    n = len(d)
    avail = {k: n >= v for k, v in config.MIN_BARS_FOR.items()}

    for span in (20, 50, 100, 200):
        key = f"ema{span}"
        d[key] = ema(d["close"], span) if avail.get(key) else np.nan

    d["rsi14"] = rsi(d["close"]) if avail["rsi14"] else np.nan
    m, s, h = macd(d["close"])
    d["macd"], d["macd_signal"], d["macd_hist"] = (
        (m, s, h) if avail["macd"] else (np.nan, np.nan, np.nan))
    d["atr14"] = atr(d) if avail["atr14"] else np.nan

    a, pdi, mdi = adx(d)
    d["adx14"], d["plus_di"], d["minus_di"] = (
        (a, pdi, mdi) if avail["adx14"] else (np.nan, np.nan, np.nan))

    if avail["bollinger"]:
        u, mid, lo = bollinger(d["close"])
        d["bb_upper"], d["bb_mid"], d["bb_lower"] = u, mid, lo
        d["bb_bandwidth"] = bandwidth(u, mid, lo)
    else:
        d["bb_upper"] = d["bb_mid"] = d["bb_lower"] = d["bb_bandwidth"] = np.nan

    d["obv"] = obv(d)

    if avail["supertrend"]:
        st, tr = supertrend(d)
        d["supertrend"], d["st_trend"] = st, tr
    else:
        d["supertrend"], d["st_trend"] = np.nan, np.nan

    if avail["ichimoku"]:
        t, k, sa, sb = ichimoku(d)
        d["tenkan"], d["kijun"], d["senkou_a"], d["senkou_b"] = t, k, sa, sb
    else:
        d["tenkan"] = d["kijun"] = d["senkou_a"] = d["senkou_b"] = np.nan

    return d, avail
