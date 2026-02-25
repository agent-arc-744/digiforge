"""
digiforge.exceptions
====================
Error hierarchy for the digiforge SDK.

Kael — Project Trinity — 2026
"""


class DigiForgeError(Exception):
    """Base exception for all digiforge errors."""


# RPC / Connection
class RPCError(DigiForgeError):
    """Node returned a JSON-RPC error response."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"RPC error {code}: {message}")


class NodeConnectionError(DigiForgeError):
    """Cannot reach the DigiByte node."""


class AuthenticationError(DigiForgeError):
    """RPC credentials rejected."""


# Asset errors
class AssetError(DigiForgeError):
    """Base for DigiAsset operation errors."""


class AssetIssuanceError(AssetError):
    """Failure during asset issuance."""


class AssetTransferError(AssetError):
    """Failure during asset transfer."""


class AssetNotFoundError(AssetError):
    """Requested asset does not exist."""


class InsufficientAssetBalance(AssetError):
    """Not enough asset units for the requested operation."""
    def __init__(self, asset_id: str, required: int, available: int):
        self.asset_id = asset_id
        self.required = required
        self.available = available
        super().__init__(
            f"Asset {asset_id}: need {required} units, have {available}"
        )


# Metadata errors
class MetadataError(DigiForgeError):
    """Base for metadata errors."""


class MetadataValidationError(MetadataError):
    """Metadata fields fail schema validation."""


class MetadataEncodingError(MetadataError):
    """Cannot encode metadata into OP_RETURN payload."""


# Transaction errors
class TransactionError(DigiForgeError):
    """Base for transaction errors."""


class InsufficientFundsError(TransactionError):
    """Wallet has insufficient DGB to cover transaction + fees."""


class SigningError(TransactionError):
    """Transaction signing failed or incomplete."""


class BroadcastError(TransactionError):
    """Transaction rejected on broadcast."""


class ValidationError(DigiForgeError):
    """Generic input validation failure."""
