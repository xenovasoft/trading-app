#!/usr/bin/env python
"""
CLI for the upgraded analysis engine.

    python run_analysis.py                # all assets, human-readable
    python run_analysis.py BTCUSD         # one asset
    python run_analysis.py BTCUSD --json  # machine-readable

This does NOT write to Supabase or send notifications — it is the analysis
layer only, so you can inspect output before wiring it into the live loop.
"""

import json
import sys
import traceback

import analysis
import dataio
import report


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    assets = args or list(dataio.ASSETS.keys())

    out = {}
    for asset in assets:
        if asset not in dataio.ASSETS:
            print(f"unknown asset {asset}; known: {list(dataio.ASSETS)}",
                  file=sys.stderr)
            continue
        try:
            res = analysis.analyze_asset(asset)
            out[asset] = res
            if not as_json:
                print(report.render(res))
                print()
        except Exception as e:
            print(f"[{asset}] FAILED: {e}", file=sys.stderr)
            traceback.print_exc()

    if as_json:
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
