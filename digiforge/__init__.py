"""
digiforge
=========
DigiAssets v3 SDK for DigiByte — clean, production-grade, UTXO-native.

Kael — Project Trinity — 2026

Quick start (asset issuance)::

    from digiforge import DigiForge, AssetMetadata, Divisibility

    forge = DigiForge.from_env()
    meta  = AssetMetadata(asset_name="MyToken", issuer="Project Trinity")
    result = forge.issue(amount=1_000_000, metadata=meta)
    print(result)

Quick start (CDP / DigiDollar)::

    from digiforge.cdp import CDPClient

    cdp = CDPClient.testnet(password="secret")
    health = cdp.health()
    result = cdp.mint(collateral_dgb_satoshis=10_000 * 100_000_000, ratio=300)
    print(result)
"""

from .assets import (
    AssetBalance,
    DigiForge,
    IssuanceResult,
    TransferResult,
)
from .exceptions import (
    AssetError,
    AssetIssuanceError,
    AssetNotFoundError,
    AssetTransferError,
    AuthenticationError,
    BroadcastError,
    DigiForgeError,
    InsufficientAssetBalance,
    InsufficientFundsError,
    MetadataEncodingError,
    MetadataValidationError,
    NodeConnectionError,
    RPCError,
    SigningError,
    TransactionError,
    ValidationError,
)
from .metadata import (
    AggregationPolicy,
    AssetMetadata,
    AssetUrl,
    Divisibility,
    IssuancePayload,
    TransferInstruction,
    TransferPayload,
)
from .rpc import DigiByteRPC, NodeConfig
from .utils import derive_asset_id, encode_leb128, decode_leb128

__version__ = "0.2.0"
__author__  = "Kael -- Project Trinity"
__license__ = "MIT"

__all__ = [
    # Core
    "DigiForge",
    "DigiByteRPC",
    "NodeConfig",
    # Metadata
    "AssetMetadata",
    "AssetUrl",
    "Divisibility",
    "AggregationPolicy",
    "IssuancePayload",
    "TransferPayload",
    "TransferInstruction",
    # Results
    "IssuanceResult",
    "TransferResult",
    "AssetBalance",
    # Exceptions
    "DigiForgeError",
    "RPCError",
    "NodeConnectionError",
    "AuthenticationError",
    "AssetError",
    "AssetIssuanceError",
    "AssetTransferError",
    "AssetNotFoundError",
    "InsufficientAssetBalance",
    "InsufficientFundsError",
    "SigningError",
    "BroadcastError",
    "ValidationError",
    "MetadataValidationError",
    "MetadataEncodingError",
    "TransactionError",
    # Utils
    "derive_asset_id",
    "encode_leb128",
    "decode_leb128",
    # v0.2.0 -- CDP modules (import directly from submodules)
    # from digiforge.cdp import CDPClient, CDPPosition, MintResult, RedeemResult
    # from digiforge.oracle import OracleClient, OraclePrice
    # from digiforge.scripts import DigiDollarScripts, CollateralOutput
]
