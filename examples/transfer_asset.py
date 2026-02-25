#!/usr/bin/env python3
"""
examples/transfer_asset.py
==========================
Transfer DigiAsset units to another address.

Usage:
    # Uses asset ID from create_asset.py example
    python examples/transfer_asset.py <asset_id> <to_address> <amount>

    # Or reads from /tmp/last_asset_id.txt
    python examples/transfer_asset.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digiforge import DigiForge


def main():
    print("digiforge — Asset Transfer Example")
    print("===================================
")

    # Parse args or use defaults
    if len(sys.argv) >= 4:
        asset_id = sys.argv[1]
        to_address = sys.argv[2]
        amount = int(sys.argv[3])
    else:
        # Try reading saved asset ID
        try:
            with open("/tmp/last_asset_id.txt") as f:
                asset_id = f.read().strip()
        except FileNotFoundError:
            print("Usage: python transfer_asset.py <asset_id> <to_address> <amount>")
            print("       (or run create_asset.py first)")
            sys.exit(1)

        to_address = input("Recipient address: ").strip()
        amount = int(input("Amount to transfer: ").strip())

    forge = DigiForge.from_env()
    print(f"Connected : {forge}")
    print(f"Asset ID  : {asset_id}")
    print(f"To        : {to_address}")
    print(f"Amount    : {amount:,}")
    print()

    result = forge.transfer(
        asset_id=asset_id,
        to_address=to_address,
        amount=amount,
    )

    print(result)


if __name__ == "__main__":
    main()
