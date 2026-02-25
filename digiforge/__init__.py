"""
digiforge
=========
DigiAssets v3 SDK for DigiByte.

The SDK that makes DigiAsset creation feel like it should.

Kael — Project Trinity — 2026

Quick start::

    from digiforge import DigiForge, AssetMetadata, Divisibility

    forge = DigiForge.from_env()

    meta = AssetMetadata(
        asset_name="MyToken",
        issuer="Project Trinity",
        description="A DigiAsset on DigiByte.",
    )

    result = forge.issue(amount=1_000_000, metadata=meta)
    print(result)  # TXID + AssetId
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

__version__ = "0.1.0"
__author__ = "Kael — Project Trinity"
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
]
