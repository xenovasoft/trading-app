"""
Central configuration. Every threshold that was previously a magic number in
engine.py lives here, and volatility-sensitive thresholds are expressed in ATR
units rather than raw percentages so they transfer across XAUUSD / XAGUSD /
BTCUSD without re-tuning.

NOTHING in this file is fitted to historical results. The defaults are
structural conventions (e.g. "an order block needs a displacement candle"),
not optimised parameters. Treat them as priors to be validated by the
walk-forward process, not as known-good values.
"""

# ---------------------------------------------------------------- ACCOUNT ---
# !! YOU MUST SET THESE. Position sizes are meaningless until you do. !!
ACCOUNT = {
    "equity": 10_000.0,          # account equity in USD
    "risk_pct_per_trade": 0.5,   # max % of equity risked per trade
    "max_open_risk_pct": 1.5,    # max aggregate % at risk across open trades
    "equity_is_placeholder": True,  # set False once you've entered real equity
}

# Contract specifications. Used to convert a stop distance in price into a
# position size. Verify these against YOUR broker — they vary between
# spot CFD, futures and crypto venues.
CONTRACT_SPECS = {
    "XAUUSD": {"units_per_lot": 100,    "tick_size": 0.01,  "price_dp": 2,
               "instrument": "Kraken PAXGUSD (physically-redeemable gold "
                              "token, tracks LBMA spot) used as spot proxy"},
    "XAGUSD": {"units_per_lot": 5000,   "tick_size": 0.005, "price_dp": 3,
               "instrument": "COMEX Silver futures (SI=F) used as spot proxy"},
    "BTCUSD": {"units_per_lot": 1,      "tick_size": 0.1,   "price_dp": 2,
               "instrument": "Kraken XBTUSD spot"},
}

# Estimated round-turn cost as a fraction of notional. Replace with your real
# commission + typical spread. Spread is NOT available from our OHLCV feeds,
# so this is an assumption, not a measurement.
COST = {
    "round_turn_pct": 0.0004,   # 0.04% round turn (commission + assumed spread)
    "spread_is_estimated": True,
}

# ------------------------------------------------------------- DATA GATES ---
# A signal derived from stale data is worse than no signal. Futures close over
# the weekend, so these limits are what force NO TRADE outside market hours.
MAX_BAR_AGE_MINUTES = {
    "5M": 45,
    "15M": 90,
    "1H": 240,
    "4H": 720,
    "Daily": 4 * 1440,
    "Weekly": 10 * 1440,
    "Monthly": 45 * 1440,
}

# Minimum closed bars required before an indicator is trusted. An EMA200 over
# 104 bars is arithmetically defined but informationally meaningless.
MIN_BARS_FOR = {
    "ema20": 20, "ema50": 50, "ema100": 100, "ema200": 200,
    "rsi14": 15, "macd": 35, "atr14": 15, "adx14": 28,
    "bollinger": 20, "ichimoku": 52, "supertrend": 11,
}

# ------------------------------------------------------------ STRUCTURE -----
STRUCTURE = {
    "swing_left": 3,
    "swing_right": 3,            # a swing needs this many CLOSED bars after it
    "bos_displacement_atr": 0.5, # break must clear the level by this * ATR
    "choch_lookback_swings": 4,
    "regime_adx_trend": 22,      # ADX above -> trending
    "regime_adx_range": 18,      # ADX below -> ranging
    "bb_squeeze_pct": 0.6,       # bandwidth < this * median bandwidth -> compression
}

# ------------------------------------------------------------ LIQUIDITY -----
LIQUIDITY = {
    "equal_level_atr_tol": 0.15,   # equal highs/lows within this * ATR
    "cluster_merge_atr": 0.35,     # merge zones closer than this * ATR
    "max_zone_width_atr": 1.2,     # hard cap; blocks transitive merge blowup
    "sweep_lookback_bars": 12,     # look for sweeps in the last N closed bars
    "sweep_reclaim_bars": 4,       # reclaim must happen within N bars
    "fvg_lookback_bars": 120,
    "fvg_min_size_atr": 0.15,      # ignore trivially small gaps
    "ob_lookback_bars": 80,
    "ob_displacement_atr": 0.8,    # candle after OB must displace this * ATR
    "round_number_steps": {        # price increments treated as round numbers
        "XAUUSD": [50, 100],
        "XAGUSD": [1, 5],
        "BTCUSD": [1000, 5000],
    },
    "volume_profile_bins": 60,
    "hvn_percentile": 85,          # volume-at-price above this pct -> HVN
    "lvn_percentile": 15,          # below this -> LVN
    "max_zones_reported": 12,
    "near_price_atr": 3.0,         # "nearest" zones within this * ATR
}

# Weight of each confirmation type in a liquidity zone's 0-100 strength score.
# These are judgement-based priors. They are the single most important thing
# to challenge during walk-forward validation.
ZONE_WEIGHTS = {
    "equal_levels": 22,
    "prev_period_extreme": 20,
    "session_extreme": 14,
    "swing_point": 16,
    "unfilled_fvg": 14,
    "order_block": 18,
    "breaker": 16,
    "hvn": 12,
    "lvn": 6,
    "vwap": 10,
    "anchored_vwap": 10,
    "round_number": 6,
    "untested_bonus": 12,      # never been traded into since forming
    "htf_confluence": 15,      # same zone appears on a higher timeframe
}

# ------------------------------------------------------------ EXECUTION -----
EXECUTION = {
    "min_rr": 2.0,                 # reject setups below this MEASURED R:R
    "min_confluence_score": 65,    # 0-100, see analysis.confluence_score()
    "min_confirmations": 5,
    "stop_buffer_atr": 0.25,       # keep stop away from the swept level
    "stop_buffer_ticks": 4,        # plus a fixed tick cushion
    "max_stop_atr": 3.5,           # reject setups needing a wider stop than this
    "entry_zone_atr": 0.4,         # limit-entry zone half-width
    "setup_expiry_bars": 24,       # PENDING setup dies after N bars untriggered
    "target_liquidity_only": True, # targets sit at real liquidity, not fixed R
}

# --------------------------------------------------------------- PROFILES ---
# Two independent setups run on every asset. SCALP exists so that short-side
# opportunities are actually reachable: the SWING bias is driven by Monthly/
# Weekly/Daily EMA structure, which in a sustained uptrend almost never flips
# bearish, so a swing-only system is structurally long-biased. SCALP derives
# its own bias from 1H/15M and can therefore short a pullback inside an uptrend.
PROFILES = {
    "SWING": {
        "bias_timeframes": ["Weekly", "Daily", "4H"],
        "structure_timeframes": ["4H", "1H"],
        "entry_timeframes": ["15M", "5M"],
        "target_timeframes": ["4H", "Daily"],
        # ATR must come from the SAME timeframe the structural stop is derived
        # from, otherwise stop-width gates are measured in the wrong units
        # (a 4H-structure stop measured in 1H ATR reads ~6 ATR and always
        # trips max_stop_atr).
        "atr_timeframe": "4H",
        "min_rr": 2.0,
        "min_confluence_score": 65,
        "min_confirmations": 5,
        "stop_buffer_atr": 0.25,
        "max_stop_atr": 3.5,
        "setup_expiry_bars": 24,        # in entry-timeframe bars (15M) => 6h
        "max_target_atr": 15.0,         # targets beyond this are unreachable
        "expected_hold": "1-5 days",
        "allow_counter_htf": False,     # never fade the higher-timeframe bias
    },
    "SCALP": {
        "bias_timeframes": ["1H", "15M"],
        "structure_timeframes": ["15M", "5M"],
        "entry_timeframes": ["5M"],
        "target_timeframes": ["15M", "1H"],
        "atr_timeframe": "15M",   # matches structure_timeframes[0]
        "min_rr": 1.5,                  # scalps clear cost at lower R than swings
        "min_confluence_score": 60,
        "min_confirmations": 4,
        "stop_buffer_atr": 0.35,        # proportionally wider: LTF noise is worse
        "max_stop_atr": 2.5,
        "setup_expiry_bars": 12,        # in 5M bars => 1h
        "max_target_atr": 12.0,
        "expected_hold": "15min-4h",
        "allow_counter_htf": True,      # may counter-trend, but see penalty below
    },
}

# A scalp taken against the daily trend is a lower-quality trade. Rather than
# ban it (which would remove most shorts in an uptrend), penalise its score so
# it must clear a higher evidence bar to qualify.
COUNTER_TREND_SCORE_PENALTY = 12

# Cost realism gate: a scalp's target must clear estimated round-turn cost by
# this multiple, otherwise the edge is eaten by fees/spread.
MIN_COST_MULTIPLE = 3.0

# --------------------------------------------------------------- SESSIONS ---
# UTC hour ranges. Futures/crypto trade nearly 24h, so these are analytical
# windows for session-high/low liquidity, not exchange hours.
SESSIONS = {
    "Asia":   (0, 8),
    "London": (7, 16),
    "NewYork": (12, 21),
}

# Data we do NOT have. Anything requiring these must be reported as
# unavailable rather than approximated silently.
UNAVAILABLE_DATA = [
    "order book / depth of market",
    "open interest",
    "liquidation clusters",
    "bid-ask spread (live)",
    "tick-level volume (true volume profile)",
    "futures contract roll calendar",
]
