import requests
import pandas as pd
import numpy as np

UA = {"User-Agent": "Mozilla/5.0"}

# ---------------- DATA FETCH ----------------

def fetch_binance(symbol, interval, limit=500):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=15)
    r.raise_for_status()
    data = r.json()
    df = pd.DataFrame(data, columns=[
        "open_time","open","high","low","close","volume","close_time",
        "qav","trades","taker_base","taker_quote","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df[["time","open","high","low","close","volume"]]

def fetch_yahoo(symbol, interval, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": interval, "range": rng}, headers=UA, timeout=15)
    r.raise_for_status()
    j = r.json()
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    meta = res["meta"]
    df = pd.DataFrame({
        "time": pd.to_datetime(ts, unit="s"),
        "open": q["open"], "high": q["high"], "low": q["low"],
        "close": q["close"], "volume": q["volume"]
    }).dropna()
    return df, meta

def resample(df, rule):
    d = df.set_index("time")
    out = d.resample(rule).agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    return out.reset_index()

# ---------------- INDICATORS ----------------

def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()

def rsi(series, n=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1/n, adjust=False).mean()
    roll_down = down.ewm(alpha=1/n, adjust=False).mean()
    rs = roll_up / roll_down
    return 100 - (100 / (1 + rs))

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def atr(df, n=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def adx(df, n=14):
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr_n = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_n
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1/n, adjust=False).mean(), plus_di, minus_di

def bollinger(series, n=20, k=2):
    mid = series.rolling(n).mean()
    std = series.rolling(n).std()
    return mid + k*std, mid, mid - k*std

def obv(df):
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()

def supertrend(df, n=10, mult=3):
    atr_n = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    upperband = hl2 + mult*atr_n
    lowerband = hl2 - mult*atr_n
    st = pd.Series(index=df.index, dtype=float)
    trend = pd.Series(index=df.index, dtype=int)
    st.iloc[0] = upperband.iloc[0]
    trend.iloc[0] = 1
    for i in range(1, len(df)):
        if df["close"].iloc[i] > upperband.iloc[i-1]:
            trend.iloc[i] = 1
        elif df["close"].iloc[i] < lowerband.iloc[i-1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[i-1]
            if trend.iloc[i] == 1 and lowerband.iloc[i] < lowerband.iloc[i-1]:
                lowerband.iloc[i] = lowerband.iloc[i-1]
            if trend.iloc[i] == -1 and upperband.iloc[i] > upperband.iloc[i-1]:
                upperband.iloc[i] = upperband.iloc[i-1]
        st.iloc[i] = lowerband.iloc[i] if trend.iloc[i] == 1 else upperband.iloc[i]
    return st, trend

def ichimoku(df):
    high, low = df["high"], df["low"]
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2)
    senkou_b = (high.rolling(52).max() + low.rolling(52).min()) / 2
    return tenkan, kijun, senkou_a, senkou_b

def pivot_points(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3
    r1 = 2*pp - prev_low
    s1 = 2*pp - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    r3 = prev_high + 2*(pp - prev_low)
    s3 = prev_low - 2*(prev_high - pp)
    return {"PP":pp,"R1":r1,"S1":s1,"R2":r2,"S2":s2,"R3":r3,"S3":s3}

def session_vwap(df):
    tp = (df["high"]+df["low"]+df["close"])/3
    cum_vwap = (tp*df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)
    return float(cum_vwap.iloc[-1]) if pd.notna(cum_vwap.iloc[-1]) else None

def nearest_pivot_levels(daily_df):
    if len(daily_df) < 2:
        return None
    prev = daily_df.iloc[-2]
    return pivot_points(prev["high"], prev["low"], prev["close"])

# ---------------- MARKET STRUCTURE / SMC ----------------

def swing_points(df, left=3, right=3):
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    for i in range(left, len(df)-right):
        if h[i] == max(h[i-left:i+right+1]):
            highs.append((i, h[i]))
        if l[i] == min(l[i-left:i+right+1]):
            lows.append((i, l[i]))
    return highs, lows

def structure_sequence(highs, lows, n=6):
    highs = highs[-n:]
    lows = lows[-n:]
    seq = []
    for i in range(1, len(highs)):
        seq.append("HH" if highs[i][1] > highs[i-1][1] else "LH")
    lseq = []
    for i in range(1, len(lows)):
        lseq.append("HL" if lows[i][1] > lows[i-1][1] else "LL")
    return seq, lseq

def detect_bos_choch(df, highs, lows):
    """Simplified: compare last close to most recent swing high/low broken."""
    if len(highs) < 2 or len(lows) < 2:
        return "INSUFFICIENT_DATA", None
    last_close = df["close"].iloc[-1]
    last_swing_high = highs[-1][1]
    last_swing_low = lows[-1][1]
    prev_swing_high = highs[-2][1]
    prev_swing_low = lows[-2][1]
    if last_close > last_swing_high and last_swing_high >= prev_swing_high:
        return "BOS_BULLISH", last_swing_high
    if last_close > last_swing_high and last_swing_high < prev_swing_high:
        return "CHOCH_BULLISH", last_swing_high
    if last_close < last_swing_low and last_swing_low <= prev_swing_low:
        return "BOS_BEARISH", last_swing_low
    if last_close < last_swing_low and last_swing_low > prev_swing_low:
        return "CHOCH_BEARISH", last_swing_low
    return "NO_BREAK", None

def detect_fvg(df, lookback=60):
    fvgs = []
    n = len(df)
    start = max(2, n-lookback)
    for i in range(start, n):
        h0, l0 = df["high"].iloc[i-2], df["low"].iloc[i-2]
        h2, l2 = df["high"].iloc[i], df["low"].iloc[i]
        if l2 > h0:
            fvgs.append(("bullish", h0, l2, df["time"].iloc[i]))
        if h2 < l0:
            fvgs.append(("bearish", h2, l0, df["time"].iloc[i]))
    return fvgs

def detect_equal_highs_lows(highs, lows, tol=0.0015):
    eq_highs, eq_lows = [], []
    for i in range(1, len(highs)):
        if abs(highs[i][1]-highs[i-1][1])/highs[i-1][1] < tol:
            eq_highs.append((highs[i-1], highs[i]))
    for i in range(1, len(lows)):
        if abs(lows[i][1]-lows[i-1][1])/lows[i-1][1] < tol:
            eq_lows.append((lows[i-1], lows[i]))
    return eq_highs, eq_lows

def detect_liquidity_sweep(df, highs, lows):
    """Last candle wicks beyond a prior swing extreme then closes back inside."""
    if not highs or not lows:
        return None
    last = df.iloc[-1]
    sweep = None
    for idx, val in reversed(highs[:-1]):
        if last["high"] > val and last["close"] < val:
            sweep = ("sell_side_sweep_of_high", val)
            break
    for idx, val in reversed(lows[:-1]):
        if last["low"] < val and last["close"] > val:
            sweep = ("buy_side_sweep_of_low", val)
            break
    return sweep

def detect_order_block(df, direction, lookback=40):
    """Last opposite-colored candle before an impulsive move in `direction`."""
    n = len(df)
    start = max(1, n-lookback)
    for i in range(n-2, start, -1):
        body = df["close"].iloc[i] - df["open"].iloc[i]
        next_body = df["close"].iloc[i+1] - df["open"].iloc[i+1]
        if direction == "bullish" and body < 0 and next_body > 0 and abs(next_body) > abs(body):
            return (str(df["time"].iloc[i]), float(df["low"].iloc[i]), float(df["high"].iloc[i]))
        if direction == "bearish" and body > 0 and next_body < 0 and abs(next_body) > abs(body):
            return (str(df["time"].iloc[i]), float(df["low"].iloc[i]), float(df["high"].iloc[i]))
    return None

# ---------------- CANDLESTICK PATTERNS ----------------

def candle_patterns(df):
    o,h,l,c = df["open"].iloc[-1], df["high"].iloc[-1], df["low"].iloc[-1], df["close"].iloc[-1]
    po,ph,pl,pc = df["open"].iloc[-2], df["high"].iloc[-2], df["low"].iloc[-2], df["close"].iloc[-2]
    body = abs(c-o)
    rng = h-l if h!=l else 1e-9
    upper_wick = h - max(c,o)
    lower_wick = min(c,o) - l
    patterns = []
    if c > po and o < pc and c > o and pc < po:
        patterns.append("Bullish Engulfing")
    if c < po and o > pc and c < o and pc > po:
        patterns.append("Bearish Engulfing")
    if body/rng < 0.1:
        patterns.append("Doji")
    if lower_wick > 2*body and upper_wick < body:
        patterns.append("Hammer" if c>=o else "Hanging Man")
    if upper_wick > 2*body and lower_wick < body:
        patterns.append("Shooting Star" if c<=o else "Inverted Hammer")
    if body < abs(pc-po) and h <= ph and l >= pl:
        patterns.append("Inside Bar")
    return patterns

# ---------------- PER-TIMEFRAME ANALYSIS ----------------

def analyze_tf(df, label):
    d = df.copy()
    d["ema20"] = ema(d["close"],20)
    d["ema50"] = ema(d["close"],50)
    d["ema100"] = ema(d["close"],100)
    d["ema200"] = ema(d["close"],200)
    d["rsi14"] = rsi(d["close"],14)
    macd_l, macd_s, macd_h = macd(d["close"])
    d["macd"], d["macd_signal"], d["macd_hist"] = macd_l, macd_s, macd_h
    d["atr14"] = atr(d,14)
    adx14, pdi, mdi = adx(d,14)
    d["adx14"], d["plus_di"], d["minus_di"] = adx14, pdi, mdi
    bb_u, bb_m, bb_l = bollinger(d["close"])
    d["bb_upper"], d["bb_mid"], d["bb_lower"] = bb_u, bb_m, bb_l
    d["obv"] = obv(d)
    st, sttrend = supertrend(d)
    d["supertrend"], d["st_trend"] = st, sttrend
    tenkan, kijun, sa, sb = ichimoku(d)
    d["tenkan"], d["kijun"], d["senkou_a"], d["senkou_b"] = tenkan, kijun, sa, sb

    # Structure/BOS/sweep are evaluated on CLOSED candles only — the last row may
    # still be forming, and its high/low drift tick-to-tick would otherwise flicker
    # the detected state (e.g. CHOCH_BULLISH <-> NO_BREAK) and cause duplicate alerts.
    closed = d.iloc[:-1] if len(d) > 1 else d
    highs, lows = swing_points(closed)
    hseq, lseq = structure_sequence(highs, lows)
    bos, bos_level = detect_bos_choch(closed, highs, lows)
    fvgs = detect_fvg(closed)
    eqh, eql = detect_equal_highs_lows(highs, lows)
    sweep = detect_liquidity_sweep(closed, highs, lows)
    patterns = candle_patterns(d)

    last = d.iloc[-1]
    trend_ema = "BULLISH" if last["ema20"]>last["ema50"]>last["ema100"]>last["ema200"] else (
        "BEARISH" if last["ema20"]<last["ema50"]<last["ema100"]<last["ema200"] else "MIXED")

    return {
        "label": label,
        "last_close": float(last["close"]),
        "last_time": str(d["time"].iloc[-1]),
        "ema_trend": trend_ema,
        "rsi14": float(last["rsi14"]) if pd.notna(last["rsi14"]) else None,
        "macd": float(last["macd"]) if pd.notna(last["macd"]) else None,
        "macd_signal": float(last["macd_signal"]) if pd.notna(last["macd_signal"]) else None,
        "atr14": float(last["atr14"]) if pd.notna(last["atr14"]) else None,
        "adx14": float(last["adx14"]) if pd.notna(last["adx14"]) else None,
        "structure_highs_seq": hseq,
        "structure_lows_seq": lseq,
        "bos_choch": bos,
        "bos_level": float(bos_level) if bos_level is not None else None,
        "fvgs_recent": [(f[0], float(f[1]), float(f[2]), str(f[3])) for f in fvgs[-3:]],
        "equal_highs": len(eqh),
        "equal_lows": len(eql),
        "liquidity_sweep": [sweep[0], float(sweep[1])] if sweep else None,
        "candle_patterns": patterns,
        "swing_highs_last3": [(int(i), float(v)) for i, v in highs[-3:]],
        "swing_lows_last3": [(int(i), float(v)) for i, v in lows[-3:]],
    }

# ---------------- MULTI-TIMEFRAME FETCH ----------------

def run_symbol_binance(binance_symbol):
    tfs = {"Monthly":"1M","Weekly":"1w","Daily":"1d","4H":"4h","1H":"1h","15M":"15m","5M":"5m"}
    out, raw = {}, {}
    for label, interval in tfs.items():
        df = fetch_binance(binance_symbol, interval, limit=500)
        out[label] = analyze_tf(df, label)
        raw[label] = df
    return out, raw

def run_symbol_yahoo(yahoo_symbol):
    out, raw = {}, {}
    monthly, meta = fetch_yahoo(yahoo_symbol, "1mo", "10y")
    out["Monthly"] = analyze_tf(monthly, "Monthly"); raw["Monthly"] = monthly
    weekly, _ = fetch_yahoo(yahoo_symbol, "1wk", "5y")
    out["Weekly"] = analyze_tf(weekly, "Weekly"); raw["Weekly"] = weekly
    daily, _ = fetch_yahoo(yahoo_symbol, "1d", "2y")
    out["Daily"] = analyze_tf(daily, "Daily"); raw["Daily"] = daily
    h1, _ = fetch_yahoo(yahoo_symbol, "60m", "730d")
    out["1H"] = analyze_tf(h1, "1H"); raw["1H"] = h1
    h4 = resample(h1, "4h")
    out["4H"] = analyze_tf(h4, "4H"); raw["4H"] = h4
    m15, _ = fetch_yahoo(yahoo_symbol, "15m", "60d")
    out["15M"] = analyze_tf(m15, "15M"); raw["15M"] = m15
    m5, _ = fetch_yahoo(yahoo_symbol, "5m", "60d")
    out["5M"] = analyze_tf(m5, "5M"); raw["5M"] = m5
    out["_meta"] = meta
    return out, raw

ASSETS = {
    "XAUUSD": {"kind": "yahoo", "symbol": "GC=F"},
    "XAGUSD": {"kind": "yahoo", "symbol": "SI=F"},
    "BTCUSD": {"kind": "binance", "symbol": "BTCUSDT"},
}

def fetch_asset(name):
    cfg = ASSETS[name]
    if cfg["kind"] == "binance":
        return run_symbol_binance(cfg["symbol"])
    return run_symbol_yahoo(cfg["symbol"])

# ---------------- SIGNAL SYNTHESIS ----------------

def synthesize(asset_name, data, raw_dfs):
    mo, wk, da, h4, h1, m15, m5 = (data["Monthly"], data["Weekly"], data["Daily"],
                                    data["4H"], data["1H"], data["15M"], data["5M"])
    price = m5["last_close"]

    bull_votes = sum(1 for tf in [mo, wk, da] if tf["ema_trend"] == "BULLISH")
    bear_votes = sum(1 for tf in [mo, wk, da] if tf["ema_trend"] == "BEARISH")
    if bull_votes >= 2:
        bias = "BULLISH"
    elif bear_votes >= 2:
        bias = "BEARISH"
    else:
        bias = "MIXED"

    direction = "BUY" if bias == "BULLISH" else ("SELL" if bias == "BEARISH" else None)

    confirmations = {}
    ob = None
    if direction:
        want_bull = direction == "BUY"

        confirmations["Trend Alignment (D/W EMA)"] = (
            (wk["ema_trend"]=="BULLISH" and da["ema_trend"] in ("BULLISH","MIXED")) if want_bull
            else (wk["ema_trend"]=="BEARISH" and da["ema_trend"] in ("BEARISH","MIXED")))

        struct_dir_ok = any(tf["bos_choch"] in (("BOS_BULLISH","CHOCH_BULLISH") if want_bull else ("BOS_BEARISH","CHOCH_BEARISH"))
                             for tf in [h4, h1, m15])
        confirmations["Market Structure (BOS/CHoCH 4H/1H/15M)"] = struct_dir_ok

        ob = detect_order_block(raw_dfs["1H"], "bullish" if want_bull else "bearish")
        ob_near = False
        if ob:
            _, lo, hi = ob
            ob_near = bool(lo*0.985 <= price <= hi*1.015)
        confirmations["Order Block (1H, near price)"] = ob_near

        sweep_ok = False
        for tf in [h4, h1, m15]:
            sw = tf["liquidity_sweep"]
            if sw and ((want_bull and sw[0]=="buy_side_sweep_of_low") or (not want_bull and sw[0]=="sell_side_sweep_of_high")):
                sweep_ok = True
        confirmations["Liquidity Sweep (4H/1H/15M)"] = sweep_ok

        fvg_ok = False
        for tf in [h1, m15]:
            for fvg in tf["fvgs_recent"]:
                if (want_bull and fvg[0]=="bullish") or (not want_bull and fvg[0]=="bearish"):
                    lo, hi = fvg[1], fvg[2]
                    if lo*0.98 <= price <= hi*1.02:
                        fvg_ok = True
        confirmations["Fair Value Gap (1H/15M, near price)"] = fvg_ok

        obv_series = obv(raw_dfs["1H"])
        vol_ok = (obv_series.iloc[-1] > obv_series.iloc[-20]) if want_bull else (obv_series.iloc[-1] < obv_series.iloc[-20])
        confirmations["Volume Confirmation (OBV 1H)"] = bool(vol_ok)

        confirmations["EMA Alignment (1H)"] = (h1["ema_trend"]=="BULLISH") if want_bull else (h1["ema_trend"]=="BEARISH")

        rsi_val = h1["rsi14"]
        rsi_ok = (50 <= rsi_val <= 72) if want_bull else (28 <= rsi_val <= 50)
        confirmations["RSI Confirmation (1H, not exhausted)"] = bool(rsi_ok) if rsi_val is not None else False

        confirmations["MACD Confirmation (1H)"] = bool((h1["macd"] > h1["macd_signal"]) if want_bull else (h1["macd"] < h1["macd_signal"]))

        vw = session_vwap(raw_dfs["15M"])
        confirmations["VWAP Confirmation (15M session)"] = bool((price > vw) if (want_bull and vw) else ((price < vw) if vw else False))

        piv = nearest_pivot_levels(raw_dfs["Daily"])
        sr_ok = False
        if piv:
            sr_ok = any(abs(price-lv)/price < 0.006 for lv in piv.values())
        confirmations["Support/Resistance (Daily pivots)"] = sr_ok

        want_patterns_bull = {"Bullish Engulfing","Hammer","Inverted Hammer"}
        want_patterns_bear = {"Bearish Engulfing","Shooting Star","Hanging Man"}
        pat_ok = any(p in (want_patterns_bull if want_bull else want_patterns_bear) for p in m15["candle_patterns"]+m5["candle_patterns"])
        confirmations["Candlestick Pattern (15M/5M)"] = pat_ok

        adx_ok = (h4["adx14"] or 0) > 25 or (h1["adx14"] or 0) > 25
        confirmations["ADX Trend Strength (>25 on 4H/1H)"] = bool(adx_ok)

        confirmations = {k: bool(v) for k, v in confirmations.items()}

    passed = sum(1 for v in confirmations.values() if v) if direction else 0
    total = len(confirmations)
    probability = round(100*passed/total) if total else 0

    atr_1h = h1["atr14"] or 0
    entry = price
    sl = tp1 = tp2 = tp3 = None
    zone_low = zone_high = None
    chasing = False

    if direction == "BUY":
        if ob:
            _, zone_low, zone_high = ob
            extended = price > zone_high + atr_1h
            entry = (zone_low + zone_high) / 2 if extended else price
            chasing = extended
            sl = zone_low - 0.5*atr_1h
        else:
            swing_lows = h1["swing_lows_last3"]
            struct_low = swing_lows[-1][1] if swing_lows else entry - 1.5*atr_1h
            sl = min(struct_low - 0.25*atr_1h, entry - 0.75*atr_1h)
        risk = entry - sl
        tp1, tp2, tp3 = entry + 1.5*risk, entry + 2.5*risk, entry + 4*risk

    elif direction == "SELL":
        if ob:
            _, zone_low, zone_high = ob
            extended = price < zone_low - atr_1h
            entry = (zone_low + zone_high) / 2 if extended else price
            chasing = extended
            sl = zone_high + 0.5*atr_1h
        else:
            swing_highs = h1["swing_highs_last3"]
            struct_high = swing_highs[-1][1] if swing_highs else entry + 1.5*atr_1h
            sl = max(struct_high + 0.25*atr_1h, entry + 0.75*atr_1h)
        risk = sl - entry
        tp1, tp2, tp3 = entry - 1.5*risk, entry - 2.5*risk, entry - 4*risk

    rr = round(abs(tp1-entry)/abs(entry-sl), 2) if sl and entry != sl else None

    return {
        "asset": asset_name,
        "price": price,
        "bias": bias,
        "direction": direction,
        "confirmations": confirmations,
        "passed": passed,
        "total": total,
        "probability": probability,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr,
        "zone_low": zone_low, "zone_high": zone_high, "chasing": chasing,
        "atr_1h": atr_1h,
        "h1_bos_choch": h1["bos_choch"],
        "h1_liquidity_sweep": h1["liquidity_sweep"],
        "trade_valid": bool(direction and passed >= 7 and probability >= 80),
    }

def analyze(asset_name):
    data, raw = fetch_asset(asset_name)
    return synthesize(asset_name, data, raw)
