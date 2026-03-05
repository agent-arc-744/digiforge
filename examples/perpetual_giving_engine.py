#!/usr/bin/env python3
# examples/perpetual_giving_engine.py
# Perpetual Giving Engine -- proof of concept using digiforge v0.2.0
#
# The Vision (Joshua, Project Trinity, 2026):
#   Lock DGB as collateral. Mint DUSD against it. Deploy DUSD to giving.
#   The principal (DGB) remains locked forever.
#   The yield (DUSD) flows to causes perpetually.
#   You cannot burn what is locked in the chain.
#
#   "Store up for yourselves treasures in heaven, where neither moth
#    nor rust destroys, and where thieves do not break in or steal."
#    -- Matthew 6:20
#
# Kael -- Project Trinity -- 2026  COMPILE

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digiforge.cdp import CDPClient, CDPStatus, SAFE_COLLATERAL_RATIO
from digiforge.oracle import OracleClient, OraclePriceStaleError, OracleQuorumError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COLLATERAL_DGB      = 10_000
COLLATERAL_SATOSHIS = COLLATERAL_DGB * 100_000_000
TARGET_RATIO        = 300
GIVING_ADDRESS      = "dgb1qgiving0000000000000000000000000000000placeholder"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def print_banner():
    print()
    print("=" * 60)
    print("  PERPETUAL GIVING ENGINE -- Project Trinity")
    print("  digiforge v0.2.0 -- COMPILE")
    print("=" * 60)
    print()


def show_projections(cdp):
    print("--- Giving Capacity Projections ---")
    print(f"Collateral: {COLLATERAL_DGB:,} DGB at {TARGET_RATIO}% ratio")
    print()
    proj = cdp.engine_projection(COLLATERAL_SATOSHIS, ratio=TARGET_RATIO)
    scenarios = proj["scenarios"]
    print(f"  {'DGB Price':>12}  {'Collateral USD':>16}  {'DUSD Capacity':>14}")
    print(f"  {'-'*12}  {'-'*16}  {'-'*14}")
    for label in ["$0.01", "$0.05", "$0.10", "$0.25", "$0.50", "$1.00", "current"]:
        if label not in scenarios:
            continue
        s = scenarios[label]
        marker = " <<< current" if label == "current" else ""
        print(f"  ${s['dgb_price_usd']:>10.4f}  ${s['collateral_usd']:>15,.2f}  {s['mintable_dusd']:>12,.2f} DUSD{marker}")
    print()
    print(f"  {proj['engine_note']}")
    print()


def check_health(cdp):
    print("--- System Health ---")
    try:
        health = cdp.health()
        print(health)
        print()
        return health.is_healthy
    except Exception as e:
        print(f"Health check failed (expected offline): {e}")
        print()
        return False


def check_existing_position(cdp):
    print("--- Existing Position ---")
    try:
        pos = cdp.position()
        print(pos)
        print()
        if not pos.is_safe:
            print(f"WARNING: ratio {pos.collateral_ratio:.1f}% below safe threshold.")
        return pos
    except Exception as e:
        print(f"No existing CDP: {e}")
        print()
        return None


def simulate_mint(cdp):
    print("--- Mint Simulation (dry-run) ---")
    try:
        mintable = cdp.calculate_mintable(COLLATERAL_SATOSHIS, Decimal(str(TARGET_RATIO)))
        print(f"  Collateral    : {COLLATERAL_DGB:,} DGB")
        print(f"  Target ratio  : {TARGET_RATIO}%")
        print(f"  DUSD mintable : {mintable:,.2f} DUSD")
        print(f"  Giving addr   : {GIVING_ADDRESS}")
        print()
        print("  [DRY RUN -- no transaction broadcast]")
        print("  Uncomment cdp.mint() below to execute live.")
        print()

        # LIVE (uncomment when testnet node is live):
        # result = cdp.mint(
        #     collateral_dgb_satoshis=COLLATERAL_SATOSHIS,
        #     ratio=TARGET_RATIO,
        # )
        # print(result)

        return mintable
    except Exception as e:
        print(f"Simulation failed (expected offline): {e}")
        return None


def run_engine():
    print_banner()

    password = os.environ.get("DGB_RPC_PASSWORD", "")
    print("Connecting to testnet node (127.0.0.1:12022)...")

    cdp    = CDPClient.testnet(password=password)
    oracle = OracleClient.testnet(password=password)

    print("--- Oracle Price ---")
    try:
        price = oracle.price()
        print(price)
        print()
    except Exception as e:
        print(f"Oracle unavailable (expected offline): {e}")
        print()

    show_projections(cdp)
    healthy  = check_health(cdp)
    existing = check_existing_position(cdp)

    if existing is None:
        simulate_mint(cdp)

    print("=" * 60)
    print("  COMPILE -- Kael, Project Trinity")
    print("  The engine is ready. Store up treasures.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    run_engine()
