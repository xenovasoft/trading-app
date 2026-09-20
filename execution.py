#!/usr/bin/env python
"""
Order execution, behind a gate that will not open on evidence this thin.

WHAT THIS DOES
Turns a decision from analysis.py into actual orders: sizes the position from
risk.py, places the entry, attaches a protective stop, manages the exit, and
reconciles what the broker says actually filled against what was asked for.

The broker is behind an adapter. PaperBroker fills against live prices with
the same cost model as the backtest and touches no money. KrakenBroker is the
live path and is deliberately inert until credentials exist -- see below.

WHY THERE IS A GATE
Measured, not assumed, over this repo's own data:

    backtest expectancy   -0.0923R over 451 trades   profit factor 0.87
    gross edge            +0.0594R   (t=+0.77, indistinguishable from zero)
    cost drag             -0.1524R   per trade
    edgelab               0 of 44 models survive FDR, on XAUUSD AND BTCUSD
    confluence score      Spearman +0.024 with outcome

At 2.55 trades/day and 1% risk, automating that compounds to roughly -6.8% in
a month and -34.5% in six. Automation adds no edge; it removes the friction
that was limiting the rate of loss. So `mode` defaults to PAPER and going live
requires go_live_check() to pass, which today it does not -- it names the
failing check rather than refusing mutely.

The gate is overridable (force=True). It is a guardrail against acting on a
measurement nobody re-read, not a lock on the account owner's own decision.

CREDENTIALS
This module never reads, stores, prompts for or transmits an API key. It looks
for KRAKEN_API_KEY / KRAKEN_API_SECRET in the environment and, finding none,
stays in paper mode. Supplying them is the account owner's action alone.
"""

import argparse
import json
import os

import config
import papertrader
import risk

PAPER, LIVE = "PAPER", "LIVE"

# What must be true before live execution is allowed. Each is a claim that can
# be checked, not a feeling about readiness.
GO_LIVE_REQUIREMENTS = {
    "forward_trades_min": 60,       # the sample papertrader is collecting
    "forward_expectancy_min": 0.0,  # must not be losing forward
    "edgelab_survivors_min": 1,     # at least one model with measured edge
}

# Hard limits, applied regardless of mode. These are the last line between a
# logic bug and an account.
# risk_pct_per_trade is a PERCENT, not a fraction: risk.position_size() and
# config.ACCOUNT both use "0.5" to mean half a percent. Writing 0.01 here to
# mean 1% sized every position at one HUNDREDTH of the intended risk -- and
# the same confusion in the other direction would size 100x too LARGE, which
# on a live account is unrecoverable. Hence assert_risk_units() below.
LIMITS = {
    "risk_pct_per_trade": 1.0,          # = 1% of equity
    "max_concurrent_positions": 2,
    "max_daily_loss_r": 4.0,
    "max_notional_per_trade_pct": 0.25,   # of equity, after leverage
}


# ------------------------------------------------------------------ GATE ----

def go_live_check(state=None, edgelab_survivors=0, force=False):
    """(allowed, reasons) -- may live execution be enabled right now?

    Returns every failing reason rather than the first, because "fix this one
    thing" is misleading when three are wrong.
    """
    if force:
        return True, ["OVERRIDDEN by force=True -- gate bypassed deliberately"]

    state = state if state is not None else papertrader.load_state_remote_first()
    v = papertrader.verify(state)
    fails = []

    n = v.get("n", 0)
    if n < GO_LIVE_REQUIREMENTS["forward_trades_min"]:
        fails.append(
            f"forward test has {n} trades, needs "
            f"{GO_LIVE_REQUIREMENTS['forward_trades_min']} before its "
            "expectancy means anything")

    exp = v.get("expectancy_r")
    if exp is not None and n >= 10 and exp < GO_LIVE_REQUIREMENTS["forward_expectancy_min"]:
        fails.append(f"forward expectancy {exp:+.3f}R is negative")

    if edgelab_survivors < GO_LIVE_REQUIREMENTS["edgelab_survivors_min"]:
        fails.append(
            f"edgelab found {edgelab_survivors} models with measured edge; "
            "last full run was 0 of 44 on both XAUUSD and BTCUSD")

    return (not fails), (fails or ["all go-live checks pass"])


def credentials_present():
    """True only if BOTH Kraken variables are set. Never returns the values."""
    return bool(os.environ.get("KRAKEN_API_KEY")
                and os.environ.get("KRAKEN_API_SECRET"))


def resolve_mode(requested=PAPER, force=False, edgelab_survivors=0):
    """(mode, reasons). LIVE requires the gate AND credentials; anything less
    degrades to PAPER with the reason stated, never silently."""
    if requested != LIVE:
        return PAPER, ["paper mode requested"]
    allowed, reasons = go_live_check(edgelab_survivors=edgelab_survivors,
                                     force=force)
    if not allowed:
        return PAPER, ["LIVE refused, running PAPER:"] + reasons
    if not credentials_present():
        return PAPER, ["LIVE refused, running PAPER:",
                       "KRAKEN_API_KEY / KRAKEN_API_SECRET not set in the "
                       "environment (this module never prompts for them)"]
    return LIVE, reasons


# ---------------------------------------------------------------- BROKERS ---

class Order(dict):
    """Plain dict so it serialises into papertrader state without adapters."""


class PaperBroker:
    """Fills at the reference price plus half the round-turn cost as slippage.

    Identical to the assumption backtest.py and papertrader.py already make,
    so paper execution stays comparable with both. A more optimistic fill here
    would make the forward test look better than the backtest for reasons that
    have nothing to do with the strategy.
    """

    name = "paper"

    def __init__(self, equity=10000.0):
        self.equity = equity
        self.orders = []

    def place(self, asset, side, units, ref_price, stop=None, target=None):
        slip = ref_price * config.COST["round_turn_pct"] / 2
        fill = ref_price + slip if side == "BUY" else ref_price - slip
        o = Order(broker="paper", asset=asset, side=side, units=units,
                  requested=ref_price, fill=fill, stop=stop, target=target,
                  status="FILLED", id=f"paper-{len(self.orders)+1}")
        self.orders.append(o)
        return o

    def cancel(self, order_id):
        for o in self.orders:
            if o["id"] == order_id and o["status"] != "FILLED":
                o["status"] = "CANCELLED"
                return True
        return False

    def positions(self):
        return [o for o in self.orders if o["status"] == "FILLED"]


class KrakenBroker:
    """Live execution against Kraken's REST API.

    Intentionally unimplemented past the constructor. The account owner has
    not enabled live trading, the gate does not currently pass, and a
    half-written order path is worse than none: it invites a later change to
    "just finish it" without revisiting why it was gated.

    When it is written it needs, at minimum: HMAC-SHA512 request signing, a
    nonce that survives restarts, AddOrder with validate=true exercised first,
    reconciliation of partial fills, and idempotency keys so a retry after a
    timeout cannot double a position.
    """

    name = "kraken"

    def __init__(self):
        if not credentials_present():
            raise RuntimeError(
                "Kraken credentials not present. Set KRAKEN_API_KEY and "
                "KRAKEN_API_SECRET yourself; this module will not prompt for "
                "or store them.")
        raise NotImplementedError(
            "Live order placement is not implemented. go_live_check() does "
            "not pass on current evidence (0 of 44 models with measured edge, "
            "backtest PF 0.87), so writing this path would only make it "
            "easier to skip the check later.")


def broker_for(mode, equity=10000.0):
    return KrakenBroker() if mode == LIVE else PaperBroker(equity=equity)


# ------------------------------------------------------------------ RISK ----

def daily_loss_r(state):
    """Realised R today, from the paper/live trade record."""
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    tot = 0.0
    for t in (state.get("trades") or []):
        et = str(t.get("exit_time") or "")
        if et.startswith(today):
            tot += float(t.get("r_multiple") or 0.0)
    return tot


def pretrade_checks(state, asset, entry, stop, equity):
    """(ok, reasons, sizing). Every limit that can block a trade, in one place.

    Runs BEFORE sizing is acted on, so a breach cannot place "just this one".
    """
    reasons = []
    open_n = len(state.get("open") or {})
    if open_n >= LIMITS["max_concurrent_positions"]:
        reasons.append(f"{open_n} positions already open, limit "
                       f"{LIMITS['max_concurrent_positions']}")

    dl = daily_loss_r(state)
    if dl <= -LIMITS["max_daily_loss_r"]:
        reasons.append(f"daily loss {dl:+.2f}R at limit "
                       f"-{LIMITS['max_daily_loss_r']:.1f}R")

    if not entry or not stop or abs(entry - stop) <= 0:
        return False, reasons + ["entry and stop must differ"], None

    sizing = risk.position_size(asset, entry, stop, equity=equity,
                                risk_pct=LIMITS["risk_pct_per_trade"])
    if sizing.get("error"):
        return False, reasons + [sizing["error"]], None

    # Trust risk.py's own notional rather than recomputing it here; two
    # formulas for the same quantity drift, and this one gates a real order.
    notional = abs(sizing.get("notional", 0.0))

    # The risk actually budgeted must match what was asked for. This catches
    # a percent/fraction mix-up at the point it would do damage instead of
    # letting a mis-sized order reach a broker.
    want = equity * LIMITS["risk_pct_per_trade"] / 100.0
    got = sizing.get("risk_amount", 0.0)
    if abs(got - want) > max(0.01, want * 0.001):
        return False, reasons + [
            f"SIZING MISMATCH: budgeted {got:,.2f} but {LIMITS['risk_pct_per_trade']}% "
            f"of {equity:,.2f} is {want:,.2f} -- refusing to size an order"], None
    cap = equity * LIMITS["max_notional_per_trade_pct"]
    if notional > cap:
        reasons.append(f"notional {notional:,.0f} exceeds "
                       f"{100*LIMITS['max_notional_per_trade_pct']:.0f}% of "
                       f"equity ({cap:,.0f})")

    return (not reasons), (reasons or ["all pre-trade checks pass"]), sizing


def assert_risk_units(equity=10000.0):
    """Self-check that % and fraction have not been confused again.

    Cheap, and the failure it guards against is a 100x position. Run by
    --selftest and importable by any test runner.
    """
    sz = risk.position_size("XAUUSD", 4365.10, 4348.90, equity=equity,
                            risk_pct=LIMITS["risk_pct_per_trade"])
    want = equity * LIMITS["risk_pct_per_trade"] / 100.0
    assert abs(sz["risk_amount"] - want) < 0.01, (
        f"risk units wrong: budgeted {sz['risk_amount']} vs expected {want}")
    assert abs(sz["units"] * 4365.10 - sz["notional"]) < 1.0, "notional drift"
    return sz


# ------------------------------------------------------------------ MAIN ----

def status(edgelab_survivors=0):
    st = papertrader.load_state_remote_first()
    mode, why = resolve_mode(LIVE, edgelab_survivors=edgelab_survivors)
    allowed, reasons = go_live_check(st, edgelab_survivors)
    v = papertrader.verify(st)
    return {
        "mode_if_live_requested": mode,
        "go_live_allowed": allowed,
        "go_live_reasons": reasons,
        "credentials_present": credentials_present(),
        "session_status": st.get("status"),
        "forward_trades": v.get("n", 0),
        "forward_expectancy_r": v.get("expectancy_r"),
        "daily_loss_r": round(daily_loss_r(st), 3),
        "limits": LIMITS,
        "requirements": GO_LIVE_REQUIREMENTS,
        "mode_reasons": why,
    }


def main():
    ap = argparse.ArgumentParser(description="execution layer status and checks")
    ap.add_argument("--survivors", type=int, default=0,
                    help="edgelab models with measured edge (run edgelab.py)")
    ap.add_argument("--check-live", action="store_true",
                    help="report whether live execution would be permitted")
    ap.add_argument("--selftest", action="store_true",
                    help="assert sizing units and gate behaviour")
    a = ap.parse_args()

    if a.selftest:
        sz = assert_risk_units()
        print(f"  PASS  1% of 10,000 budgets {sz['risk_amount']:,.2f} risk "
              f"-> {sz['units']:.4f} units, notional {sz['notional']:,.2f}")
        allowed, _ = go_live_check(edgelab_survivors=0)
        assert not allowed, "gate must not open on current evidence"
        print("  PASS  go-live gate closed on current evidence")
        allowed, _ = go_live_check(edgelab_survivors=99)
        assert not allowed, "gate must still require the forward sample"
        print("  PASS  gate still closed when only edgelab is satisfied")
        m, _ = resolve_mode(LIVE, force=True)
        assert m == PAPER, "no credentials must mean PAPER even when forced"
        print("  PASS  forced override still cannot trade without credentials")
        print("\nall execution checks passed")
        return

    s = status(a.survivors)
    if a.check_live:
        print(f"live execution allowed: {s['go_live_allowed']}")
        for r in s["go_live_reasons"]:
            print(f"  - {r}")
        print(f"credentials present   : {s['credentials_present']}")
        print(f"\neffective mode if LIVE requested: {s['mode_if_live_requested']}")
        return
    print(json.dumps(s, indent=2, default=str))


if __name__ == "__main__":
    main()
