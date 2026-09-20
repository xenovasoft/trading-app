"""
Volume models, as per-bar causal series.

Every function here returns an array aligned to df's index where element i is
computable from bars 0..i ONLY. That constraint is not stylistic: the previous
audit found the engine's confluence score had a Spearman correlation of +0.02
with trade outcome, and the one thing that would have made that worse is a
feature that quietly reads the future and then fails live. edgelab.py asserts
this property mechanically (see test_volume_causality).

A note that must travel with every volume result on gold: this repo prices
XAUUSD as PAXG (Kraken live, Binance PAXGUSDT historical). PAXG turns over
roughly $20-30M/day against >$100B/day in COMEX gold futures. The PRICE tracks
spot gold via redemption arbitrage. The VOLUME does not represent institutional
gold flow -- it is a thin token's order book. Volume models below are therefore
well-defined on BTC and *suggestive at best* on XAUUSD.

Bars with zero volume (dataio.py already reports these) poison ratio-based
features. Every ratio here guards against that explicitly rather than emitting
inf and letting it propagate into a score.
"""

import numpy as np
import pandas as pd


def _safe_div(a, b):
    """Elementwise a/b with 0/0 and x/0 -> nan rather than inf."""
    b = np.asarray(b, dtype=float)
    out = np.full(len(b), np.nan)
    ok = np.isfinite(b) & (b > 0)
    out[ok] = np.asarray(a, dtype=float)[ok] / b[ok]
    return out


def _shifted_rolling(series, n, fn):
    """Rolling stat over the n bars ENDING AT i-1, i.e. excluding bar i.

    Excluding the current bar is what makes "this bar is unusual" a statement
    about the bar rather than a statement partly about itself. A 20-bar median
    that includes a spike is dragged toward the spike and understates it.
    """
    s = pd.Series(np.asarray(series, dtype=float))
    return s.rolling(n, min_periods=max(3, n // 2)).agg(fn).shift(1).to_numpy()


# --------------------------------------------------------------- RELATIVE ---

def rvol(df, n=20):
    """Relative volume: this bar's volume / median volume of the prior n bars.

    Median rather than mean: volume is heavy-tailed, and a single prior spike
    in the mean makes every subsequent bar look quiet.
    """
    v = df["volume"].to_numpy(dtype=float)
    base = _shifted_rolling(v, n, "median")
    return _safe_div(v, base)


def volume_zscore(df, n=50):
    """Volume in standard deviations above the prior-n mean (log-transformed).

    Volume is log-normal-ish, so a z-score on raw volume is dominated by the
    right tail. log1p first, then z.
    """
    v = np.log1p(df["volume"].to_numpy(dtype=float))
    mu = _shifted_rolling(v, n, "mean")
    sd = _shifted_rolling(v, n, "std")
    return _safe_div(v - mu, sd)


# ------------------------------------------------------------ EFFORT/RESULT --

def effort_vs_result(df, n=20):
    """High volume with a small range = absorption (effort without result).

    Returns range_per_unit_volume, normalised against its own prior-n median.
    Values well BELOW 1 mean price moved much less than the volume would
    normally produce: someone is absorbing. Values well ABOVE 1 mean price
    moved easily on little volume: thin book, prone to reversal.
    """
    rng = (df["high"] - df["low"]).to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    eff = _safe_div(rng, v)
    base = _shifted_rolling(eff, n, "median")
    return _safe_div(eff, base)


def delta_proxy(df):
    """Buy/sell pressure proxy from close position within the bar, weighted by
    volume. Range [-1, +1] per bar, +1 = closed on the high.

    This is a PROXY. True delta needs trade-side data, which config.py already
    lists under UNAVAILABLE_DATA. Naming it proxy keeps that honest -- a bar
    that closes on its high on heavy volume may be buying pressure or may be a
    single sweep into a thin offer, and OHLCV cannot distinguish them.
    """
    h, l, c = (df["high"].to_numpy(dtype=float),
               df["low"].to_numpy(dtype=float),
               df["close"].to_numpy(dtype=float))
    pos = _safe_div(2.0 * (c - l) - (h - l), h - l)   # -1..+1
    return pos * np.asarray(df["volume"], dtype=float)


def cum_delta_proxy(df, n=20):
    """Rolling sum of delta_proxy over n bars, normalised by rolling volume.

    Normalising matters: an unnormalised cumulative delta trends with total
    volume and you end up reading activity as direction.
    """
    d = delta_proxy(df)
    dv = pd.Series(d).rolling(n, min_periods=max(3, n // 2)).sum().to_numpy()
    vv = pd.Series(df["volume"].to_numpy(dtype=float)).rolling(
        n, min_periods=max(3, n // 2)).sum().to_numpy()
    return _safe_div(dv, vv)


# ------------------------------------------------------------------ EVENTS ---

def volume_climax(df, rvol_n=20, rvol_thresh=2.5, reject_frac=0.5):
    """Climax bar: exceptional volume AND a long wick against the close.

    Boolean series. The wick condition is what separates a climax (absorption
    at an extreme, often a turn) from plain expansion (continuation). Without
    it this fires on every breakout bar and tells you nothing directional.
    """
    rv = rvol(df, rvol_n)
    h, l, c, o = (df["high"].to_numpy(dtype=float),
                  df["low"].to_numpy(dtype=float),
                  df["close"].to_numpy(dtype=float),
                  df["open"].to_numpy(dtype=float))
    rng = h - l
    upper_wick = _safe_div(h - np.maximum(c, o), rng)
    lower_wick = _safe_div(np.minimum(c, o) - l, rng)
    heavy = np.nan_to_num(rv, nan=0.0) >= rvol_thresh
    return {
        "bearish": heavy & (np.nan_to_num(upper_wick, nan=0.0) >= reject_frac),
        "bullish": heavy & (np.nan_to_num(lower_wick, nan=0.0) >= reject_frac),
    }


def volume_dryup(df, n=20, thresh=0.5):
    """Volume contraction: rvol below `thresh` for the current bar.

    Included because the dry-up-then-expansion sequence is a standard claim in
    volume analysis, and claims are exactly what edgelab.py exists to test.
    """
    return np.nan_to_num(rvol(df, n), nan=1.0) <= thresh


def obv_divergence(df, obv_series, lookback=20):
    """Price makes a new `lookback` extreme but OBV does not.

    Returns {"bearish": bool[], "bullish": bool[]}. Uses only bars <= i: the
    rolling max is over the trailing window INCLUDING i, which is legitimate
    because we are asking "is bar i an extreme of the window ending at i".
    """
    c = pd.Series(df["close"].to_numpy(dtype=float))
    o = pd.Series(np.asarray(obv_series, dtype=float))
    price_hi = c >= c.rolling(lookback, min_periods=lookback).max()
    price_lo = c <= c.rolling(lookback, min_periods=lookback).min()
    obv_hi = o >= o.rolling(lookback, min_periods=lookback).max()
    obv_lo = o <= o.rolling(lookback, min_periods=lookback).min()
    return {
        "bearish": (price_hi & ~obv_hi).to_numpy(),
        "bullish": (price_lo & ~obv_lo).to_numpy(),
    }


# ----------------------------------------------------------------- PROFILE ---

def rolling_value_area(df, window=200, bins=40, va_frac=0.70):
    """POC / value-area high / value-area low over the trailing `window` bars.

    Returns (poc, vah, val) arrays. Element i uses bars i-window+1..i.

    Computed on a strided histogram rather than a Python loop per bar: at 5M
    over 180 days this runs ~50k times and a naive loop dominated profiling.
    Bars before `window` are nan rather than computed on a short window --
    a value area built from 30 bars is not a value area.
    """
    n = len(df)
    poc = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    val = np.full(n, np.nan)
    if n < window:
        return poc, vah, val

    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    mid = (h + l) / 2.0

    for i in range(window - 1, n):
        s = i - window + 1
        lo, hi = l[s:i + 1].min(), h[s:i + 1].max()
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            continue
        edges = np.linspace(lo, hi, bins + 1)
        hist, _ = np.histogram(mid[s:i + 1], bins=edges,
                               weights=np.nan_to_num(v[s:i + 1]))
        total = hist.sum()
        if total <= 0:
            continue
        centers = (edges[:-1] + edges[1:]) / 2.0
        k = int(hist.argmax())
        poc[i] = centers[k]

        # Grow outward from the POC until va_frac of volume is enclosed.
        lo_i = hi_i = k
        acc = hist[k]
        target = total * va_frac
        while acc < target and (lo_i > 0 or hi_i < bins - 1):
            take_lo = hist[lo_i - 1] if lo_i > 0 else -1.0
            take_hi = hist[hi_i + 1] if hi_i < bins - 1 else -1.0
            if take_hi >= take_lo:
                hi_i += 1
                acc += take_hi
            else:
                lo_i -= 1
                acc += take_lo
        val[i] = edges[lo_i]
        vah[i] = edges[hi_i + 1]
    return poc, vah, val


def vwap_zscore(df, n=100):
    """Distance from a rolling volume-weighted average price, in std units.

    Rolling rather than session-anchored so it is defined on every bar and
    comparable across sessions; session_vwap() in indicators.py already covers
    the anchored variant.
    """
    c = df["close"].to_numpy(dtype=float)
    v = np.nan_to_num(df["volume"].to_numpy(dtype=float))
    pv = pd.Series(c * v).rolling(n, min_periods=max(10, n // 2)).sum().to_numpy()
    vv = pd.Series(v).rolling(n, min_periods=max(10, n // 2)).sum().to_numpy()
    vwap = _safe_div(pv, vv)
    dev = c - vwap
    sd = pd.Series(dev).rolling(n, min_periods=max(10, n // 2)).std().to_numpy()
    return _safe_div(dev, sd)


# -------------------------------------------------------------------- ALL ----

def compute_all(df, obv_series=None):
    """Every volume feature as one dict of arrays aligned to df.

    obv_series is optional so this can run standalone; indicators.compute_all
    already attaches an "obv" column and passing it avoids recomputation.
    """
    if obv_series is None:
        v = df["volume"].to_numpy(dtype=float)
        sign = np.sign(np.diff(df["close"].to_numpy(dtype=float), prepend=np.nan))
        obv_series = np.nancumsum(np.nan_to_num(sign * v))

    climax = volume_climax(df)
    odiv = obv_divergence(df, obv_series)
    poc, vah, val = rolling_value_area(df)
    c = df["close"].to_numpy(dtype=float)

    return {
        "rvol": rvol(df),
        "volume_z": volume_zscore(df),
        "effort_result": effort_vs_result(df),
        "cum_delta": cum_delta_proxy(df),
        "vwap_z": vwap_zscore(df),
        "climax_bear": climax["bearish"],
        "climax_bull": climax["bullish"],
        "dryup": volume_dryup(df),
        "obv_div_bear": odiv["bearish"],
        "obv_div_bull": odiv["bullish"],
        "poc": poc, "vah": vah, "val": val,
        # Where price sits in the value area: >1 above VAH, <0 below VAL.
        "va_position": _safe_div(c - val, vah - val),
    }
