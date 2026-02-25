#!/usr/bin/env python3
"""
examples/create_asset.py
========================
Issue a new DigiAsset on DigiByte testnet.

Requires:
    DGB_RPC_HOST    (default: 127.0.0.1)
    DGB_RPC_PORT    (default: 12022 for testnet)
    DGB_RPC_USER    (default: dgbrpc)
    DGB_RPC_PASS    (required)

Usage:
    python examples/create_asset.py
"""
import os
import sys

# Allow running from examples/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from digiforge import DigiForge, AssetMetadata, AssetUrl, Divisibility, AggregationPolicy


def main():
    print("digiforge — Asset Issuance Example")
    print("===================================
")

    # Connect to node
    print("Connecting to DigiByte node...")
    forge = DigiForge.from_env()
    print(f"Connected: {forge}")
    print(f"Network  : {forge.network}")
    print(f"Balance  : {forge.wallet_balance():.8f} DGB
")

    # Build metadata
    meta = AssetMetadata(
        asset_name="DigiDollar",
        issuer="Project Trinity",
        description="DigiByte-native stablecoin CDP collateral receipt. "
                    "Part of the Perpetual Giving Engine.",
        version=1,
    )
    meta.add_url(AssetUrl(
        name="blueprint",
        url="https://ipfs.io/ipfs/QmPlaceholderHashHere",
        mime_type="application/pdf",
    ))

    print(f"Asset name    : {meta.asset_name}")
    print(f"Issuer        : {meta.issuer}")
    print(f"Metadata hash : {meta.hash_hex()}")
    print()

    # Issue
    print("Issuing asset...")
    result = forge.issue(
        amount=1_000_000,
        metadata=meta,
        divisibility=Divisibility.WHOLE,
        lock=True,
        aggregation=AggregationPolicy.AGGREGATABLE,
    )

    print()
    print(result)  # pretty-printed result
    print()
    print(f"OP_RETURN payload : {result.payload_hex}")
    print(f"Payload size      : {len(bytes.fromhex(result.payload_hex))} bytes")

    # Save assetId for transfer example
    with open("/tmp/last_asset_id.txt", "w") as f:
        f.write(result.asset_id)
    print(f"
Asset ID saved to /tmp/last_asset_id.txt")


if __name__ == "__main__":
    main()
