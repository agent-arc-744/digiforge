"""
digiforge.utils
===============
Low-level encoding utilities.

- LEB128 variable-length integer encoding (used in DigiAssets OP_RETURN payload)
- Hex / bytes helpers
- AssetId derivation

Kael — Project Trinity — 2026
"""
from __future__ import annotations

import hashlib
import struct
from typing import Tuple


# ---------------------------------------------------------------------------
# LEB128 (unsigned) — used by DigiAssets v3 for amount encoding
# ---------------------------------------------------------------------------

def encode_leb128(value: int) -> bytes:
    """
    Encode a non-negative integer as unsigned LEB128.

    LEB128 is a variable-length encoding where each byte carries 7 bits of
    data plus a continuation bit (MSB). DigiAssets uses this to pack asset
    amounts compactly into OP_RETURN payloads.

    >>> encode_leb128(0).hex()
    '00'
    >>> encode_leb128(624485).hex()
    'e58e26'
    """
    if value < 0:
        raise ValueError(f"LEB128 unsigned requires non-negative value, got {value}")
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value != 0:
            byte |= 0x80  # set continuation bit
        result.append(byte)
        if value == 0:
            break
    return bytes(result)


def decode_leb128(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """
    Decode unsigned LEB128 from *data* starting at *offset*.

    Returns (value, bytes_consumed).

    >>> decode_leb128(bytes.fromhex('e58e26'))
    (624485, 3)
    """
    result = 0
    shift = 0
    consumed = 0
    while True:
        if offset + consumed >= len(data):
            raise ValueError("Truncated LEB128 sequence")
        byte = data[offset + consumed]
        consumed += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if (byte & 0x80) == 0:
            break
        if shift >= 64:
            raise ValueError("LEB128 value exceeds 64-bit limit")
    return result, consumed


# ---------------------------------------------------------------------------
# Hex helpers
# ---------------------------------------------------------------------------

def to_hex(data: bytes) -> str:
    """Bytes to lowercase hex string."""
    return data.hex()


def from_hex(s: str) -> bytes:
    """Hex string to bytes. Strips whitespace."""
    return bytes.fromhex(s.strip())


def validate_hex(s: str, expected_bytes: int | None = None) -> bool:
    """Return True if *s* is valid hex with optional length check."""
    s = s.strip()
    if len(s) % 2 != 0:
        return False
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return False
    if expected_bytes is not None and len(raw) != expected_bytes:
        return False
    return True


def pad_hex(s: str, target_bytes: int) -> str:
    """Left-pad hex string to *target_bytes* bytes (zero-fill)."""
    return s.zfill(target_bytes * 2)


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

def hash256(data: bytes) -> bytes:
    """Double SHA-256 — standard Bitcoin/DGB hash."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def hash160(data: bytes) -> bytes:
    """RIPEMD-160(SHA-256(data)) — used in P2PKH address derivation."""
    sha = hashlib.sha256(data).digest()
    ripemd = hashlib.new("ripemd160")
    ripemd.update(sha)
    return ripemd.digest()


# ---------------------------------------------------------------------------
# TXID byte-order utilities
# ---------------------------------------------------------------------------

def txid_to_bytes(txid: str) -> bytes:
    """
    Convert a TXID hex string to bytes in internal (little-endian) order.
    Bitcoin/DGB display TXIDs in reversed byte order.
    """
    return bytes.fromhex(txid)[::-1]


def bytes_to_txid(raw: bytes) -> str:
    """Convert raw bytes (internal order) to display TXID (reversed hex)."""
    return raw[::-1].hex()


# ---------------------------------------------------------------------------
# DigiAssets AssetId derivation
# ---------------------------------------------------------------------------

def derive_asset_id(issuance_txid: str, vout: int = 0) -> str:
    """
    Derive a DigiAssets v3 AssetId from the issuance transaction.

    The AssetId is deterministically computed from the first input's
    outpoint (txid + vout) of the issuance transaction. This means the
    AssetId is known before broadcast.

    The resulting bytes are Base58Check-encoded with a DigiAssets prefix.

    Args:
        issuance_txid: TXID of the issuance transaction (hex, display order)
        vout:          Output index used for the first input (default 0)

    Returns:
        Base58Check-encoded AssetId string.
    """
    # Pack txid (internal order, 32 bytes) + vout (4 bytes LE)
    txid_bytes = txid_to_bytes(issuance_txid)
    vout_bytes = struct.pack("<I", vout)
    raw = txid_bytes + vout_bytes

    # SHA-256 x2
    asset_hash = hash256(raw)

    # DigiAssets v3 prefix — 0x4441 mapped to Base58 version bytes
    # Version: 23 (0x17) gives 'La' prefix in Base58Check for mainnet
    version = bytes([0x17])
    payload = version + asset_hash[:20]

    checksum = hash256(payload)[:4]
    final = payload + checksum

    return _base58_encode(final)


# ---------------------------------------------------------------------------
# Base58 (Bitcoin alphabet)
# ---------------------------------------------------------------------------

_BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_encode(data: bytes) -> str:
    """Encode bytes to Base58 (Bitcoin/DGB alphabet)."""
    # Count leading zero bytes
    count = 0
    for byte in data:
        if byte == 0:
            count += 1
        else:
            break

    num = int.from_bytes(data, "big")
    result = []
    while num > 0:
        num, remainder = divmod(num, 58)
        result.append(_BASE58_ALPHABET[remainder])

    result.extend([_BASE58_ALPHABET[0]] * count)
    return b"".join(reversed([bytes([c]) for c in result])).decode("ascii")


def _base58_decode(s: str) -> bytes:
    """Decode Base58 string to bytes."""
    alphabet = _BASE58_ALPHABET.decode("ascii")
    num = 0
    for char in s:
        if char not in alphabet:
            raise ValueError(f"Invalid Base58 character: {char!r}")
        num = num * 58 + alphabet.index(char)

    # Count leading '1's (zero bytes)
    padding = len(s) - len(s.lstrip("1"))
    result = num.to_bytes((num.bit_length() + 7) // 8, "big") if num > 0 else b""
    return b"\x00" * padding + result
