"""
digiforge.metadata
==================
DigiAssets v3 metadata schema, encoding, and OP_RETURN payload construction.

DigiAssets Protocol v3 Reference:
    https://github.com/DigiAssets/digiassets-protocol

OP_RETURN payload layout (issuance):
    [0x44 0x41]          2 bytes  — "DA" magic marker
    [0x03]               1 byte   — protocol version
    [flags]              1 byte   — divisibility (3 bits) | lockStatus (1 bit)
                                    | aggregationPolicy (2 bits) | reserved (2 bits)
    [amount]             1-9 bytes — LEB128 encoded issuance amount
    [metadata_hash]      20 bytes  — SHA256 truncated hash of metadata JSON
                                     (or all zeros if no metadata)

OP_RETURN payload layout (transfer):
    [0x44 0x41]          2 bytes  — "DA" magic marker
    [0x15]               1 byte   — transfer opcode
    [instructions]       variable  — per-output transfer instructions

Kael — Project Trinity — 2026
"""
from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional

from .exceptions import MetadataEncodingError, MetadataValidationError
from .utils import encode_leb128


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DA_MAGIC = b"DA"          # "DA"
DA_VERSION_3 = 0x03
DA_TRANSFER_OPCODE = 0x15
OP_RETURN_MAX_BYTES = 80
METADATA_HASH_BYTES = 20         # truncated SHA-256


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AggregationPolicy(IntEnum):
    """
    Controls how asset units aggregate across UTXOs.

    AGGREGATABLE  — Units from multiple UTXOs can merge into one.
    HYBRID        — Aggregatable but with transfer restrictions.
    DISPERSED     — Each unit must remain in its own UTXO (NFT-like).
    """
    AGGREGATABLE = 0
    HYBRID = 1
    DISPERSED = 2


class Divisibility(IntEnum):
    """
    Decimal precision of the asset.
    Value N means the smallest unit is 10^-N.

    WHOLE = 0  → integer units only (e.g. share certificates)
    MICRO = 6  → 6 decimal places (e.g. currency)
    """
    WHOLE = 0
    DECI = 1
    CENTI = 2
    MILLI = 3
    DECI_MILLI = 4
    CENTI_MILLI = 5
    MICRO = 6
    NANO = 7


# ---------------------------------------------------------------------------
# Metadata document
# ---------------------------------------------------------------------------

@dataclass
class AssetUrl:
    """A URL reference embedded in asset metadata."""
    name: str
    url: str
    mime_type: str = "text/plain"

    def to_dict(self) -> Dict[str, str]:
        return {"name": self.name, "url": self.url, "mimeType": self.mime_type}


@dataclass
class AssetMetadata:
    """
    DigiAssets v3 metadata document.

    This document is stored off-chain (IPFS, web server, or any URL).
    Its SHA-256 hash (truncated to 20 bytes) is committed on-chain inside
    the OP_RETURN payload, making it tamper-evident.

    Example::

        meta = AssetMetadata(
            asset_name="DigiDollar",
            issuer="Project Trinity",
            description="DigiByte-native stablecoin CDP collateral receipt",
            version=1,
        )
        meta.add_url(AssetUrl(
            name="whitepaper",
            url="https://ipfs.io/ipfs/Qm...",
            mime_type="application/pdf",
        ))
        json_bytes = meta.encode()
        on_chain_hash = meta.hash_bytes()
    """
    asset_name: str
    issuer: str = ""
    description: str = ""
    version: int = 1
    urls: List[AssetUrl] = field(default_factory=list)
    user_data: Dict[str, Any] = field(default_factory=dict)
    # Rules
    fees: List[Dict] = field(default_factory=list)
    expiration: Optional[int] = None    # block height
    minters: List[str] = field(default_factory=list)  # addresses allowed to reissue

    # ---------------------------------------------------------------------------

    def add_url(self, url: AssetUrl) -> "AssetMetadata":
        """Append a URL reference. Returns self for chaining."""
        self.urls.append(url)
        return self

    def validate(self) -> None:
        """
        Raise MetadataValidationError if any field is invalid.
        Called automatically before encoding.
        """
        if not self.asset_name or not self.asset_name.strip():
            raise MetadataValidationError("asset_name cannot be empty")
        if len(self.asset_name) > 100:
            raise MetadataValidationError(
                f"asset_name too long ({len(self.asset_name)}/100 chars)"
            )
        if self.version < 1:
            raise MetadataValidationError("version must be >= 1")
        for url in self.urls:
            if not url.url.startswith(("http://", "https://", "ipfs://")):
                raise MetadataValidationError(
                    f"URL {url.url!r} must start with http://, https://, or ipfs://"
                )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the DigiAssets v3 JSON structure."""
        self.validate()
        doc = {
            "data": {
                "assetName": self.asset_name,
                "issuer": self.issuer,
                "description": self.description,
                "version": self.version,
                "urls": [u.to_dict() for u in self.urls],
                "encryptions": [],
                "userData": self.user_data,
            },
            "rules": {
                "version": 3,
                "fees": self.fees,
                "expiration": self.expiration,
                "minters": self.minters,
            },
        }
        return doc

    def encode(self, indent: Optional[int] = None) -> bytes:
        """Return UTF-8 JSON bytes of the metadata document."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False).encode("utf-8")

    def hash_bytes(self) -> bytes:
        """
        Compute the 20-byte on-chain commitment hash.
        SHA-256 of the canonical JSON, truncated to 20 bytes.
        """
        return hashlib.sha256(self.encode()).digest()[:METADATA_HASH_BYTES]

    def hash_hex(self) -> str:
        """Hex representation of hash_bytes()."""
        return self.hash_bytes().hex()


# ---------------------------------------------------------------------------
# OP_RETURN payload encoder / decoder
# ---------------------------------------------------------------------------

@dataclass
class IssuancePayload:
    """
    Encodes / decodes a DigiAssets v3 issuance OP_RETURN payload.

    Fields:
        amount              Total units to issue
        divisibility        Decimal precision (0-7)
        lock_status         If True, asset cannot be reissued
        aggregation_policy  How units aggregate
        metadata            Optional metadata document
    """
    amount: int
    divisibility: Divisibility = Divisibility.WHOLE
    lock_status: bool = True
    aggregation_policy: AggregationPolicy = AggregationPolicy.AGGREGATABLE
    metadata: Optional[AssetMetadata] = None

    def _flags_byte(self) -> int:
        """
        Encode flags byte:
            bits 0-2: divisibility
            bit 3:    lockStatus
            bits 4-5: aggregationPolicy
            bits 6-7: reserved (0)
        """
        flags = int(self.divisibility) & 0x07
        flags |= (1 << 3) if self.lock_status else 0
        flags |= (int(self.aggregation_policy) & 0x03) << 4
        return flags

    def encode(self) -> bytes:
        """
        Build the full OP_RETURN payload bytes.

        Raises:
            MetadataEncodingError: If encoded payload exceeds 80 bytes.
        """
        if self.amount <= 0:
            raise MetadataValidationError(f"amount must be > 0, got {self.amount}")

        payload = bytearray()
        payload += DA_MAGIC                           # 0x4441
        payload += bytes([DA_VERSION_3])              # 0x03
        payload += bytes([self._flags_byte()])        # flags
        payload += encode_leb128(self.amount)         # amount (variable)

        # Metadata hash — 20 bytes
        if self.metadata is not None:
            payload += self.metadata.hash_bytes()
        else:
            payload += b"\x00" * METADATA_HASH_BYTES

        if len(payload) > OP_RETURN_MAX_BYTES:
            raise MetadataEncodingError(
                f"Issuance payload is {len(payload)} bytes, max is {OP_RETURN_MAX_BYTES}"
            )

        # Pad to fixed width for clean OP_RETURN output
        return bytes(payload)

    def encode_hex(self) -> str:
        """Return hex string of the encoded payload."""
        return self.encode().hex()

    @classmethod
    def decode(cls, data: bytes) -> "IssuancePayload":
        """
        Parse a DigiAssets v3 issuance payload.

        Raises:
            MetadataEncodingError: If magic bytes or version are wrong.
        """
        from .utils import decode_leb128

        if len(data) < 4:
            raise MetadataEncodingError("Payload too short")
        if data[:2] != DA_MAGIC:
            raise MetadataEncodingError(
                f"Invalid DA magic: {data[:2].hex()!r}, expected '4441'"
            )
        if data[2] != DA_VERSION_3:
            raise MetadataEncodingError(
                f"Unsupported version: {data[2]}, expected {DA_VERSION_3}"
            )

        flags = data[3]
        divisibility = Divisibility(flags & 0x07)
        lock_status = bool(flags & (1 << 3))
        aggregation_policy = AggregationPolicy((flags >> 4) & 0x03)

        amount, consumed = decode_leb128(data, offset=4)
        offset = 4 + consumed

        return cls(
            amount=amount,
            divisibility=divisibility,
            lock_status=lock_status,
            aggregation_policy=aggregation_policy,
        )


@dataclass
class TransferInstruction:
    """
    A single asset transfer instruction directing *amount* units to output *vout*.
    Multiple instructions can target different outputs in the same transaction.
    """
    vout: int       # destination output index
    amount: int     # units to transfer

    def encode(self) -> bytes:
        """Encode as: vout (LEB128) + amount (LEB128)."""
        return encode_leb128(self.vout) + encode_leb128(self.amount)


@dataclass
class TransferPayload:
    """
    Encodes a DigiAssets v3 transfer OP_RETURN payload.

    A single transfer transaction can move multiple assets to multiple outputs.
    Instructions are processed in order — each maps an asset UTXO input to
    one or more destination outputs.
    """
    instructions: List[TransferInstruction] = field(default_factory=list)

    def add(self, vout: int, amount: int) -> "TransferPayload":
        """Append a transfer instruction. Returns self for chaining."""
        self.instructions.append(TransferInstruction(vout=vout, amount=amount))
        return self

    def encode(self) -> bytes:
        """Build the full OP_RETURN payload for a transfer."""
        payload = bytearray()
        payload += DA_MAGIC
        payload += bytes([DA_TRANSFER_OPCODE])
        for instr in self.instructions:
            payload += instr.encode()

        if len(payload) > OP_RETURN_MAX_BYTES:
            raise MetadataEncodingError(
                f"Transfer payload is {len(payload)} bytes, max is {OP_RETURN_MAX_BYTES}. "
                f"Reduce number of transfer instructions per transaction."
            )
        return bytes(payload)

    def encode_hex(self) -> str:
        return self.encode().hex()
