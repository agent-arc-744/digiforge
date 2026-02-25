"""Tests for digiforge.utils - LEB128, hex helpers, asset_id derivation."""
import pytest

from digiforge.utils import (
    decode_leb128,
    encode_leb128,
    derive_asset_id,
    to_hex,
    from_hex,
)


# ---------------------------------------------------------------------------
# LEB128 encoding
# ---------------------------------------------------------------------------

class TestLEB128Encode:
    def test_zero(self):
        assert encode_leb128(0) == bytes.fromhex("00")

    def test_single_byte(self):
        assert encode_leb128(1)   == bytes.fromhex("01")
        assert encode_leb128(127) == bytes.fromhex("7f")

    def test_two_bytes(self):
        # 128 = 0x80 0x01 in LEB128
        assert encode_leb128(128) == bytes.fromhex("8001")
        # 300 = 0xAC 0x02 in LEB128
        assert encode_leb128(300) == bytes.fromhex("ac02")

    def test_reference_value(self):
        # 624485 = 0xE5 0x8E 0x26 in LEB128
        assert encode_leb128(624485) == bytes.fromhex("e58e26")

    def test_large_value(self):
        encoded = encode_leb128(100_000_000_000_000)
        value, _ = decode_leb128(encoded)
        assert value == 100_000_000_000_000

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            encode_leb128(-1)

    def test_roundtrip(self):
        for val in [0, 1, 127, 128, 255, 1000, 624485, 2**32, 2**53]:
            encoded = encode_leb128(val)
            decoded, n = decode_leb128(encoded)
            assert decoded == val
            assert n == len(encoded)


# ---------------------------------------------------------------------------
# LEB128 decoding
# ---------------------------------------------------------------------------

class TestLEB128Decode:
    def test_zero(self):
        val, n = decode_leb128(bytes.fromhex("00"))
        assert val == 0 and n == 1

    def test_one(self):
        val, n = decode_leb128(bytes.fromhex("01"))
        assert val == 1 and n == 1

    def test_128(self):
        val, n = decode_leb128(bytes.fromhex("8001"))
        assert val == 128 and n == 2

    def test_300(self):
        val, n = decode_leb128(bytes.fromhex("ac02"))
        assert val == 300 and n == 2

    def test_reference_value(self):
        val, n = decode_leb128(bytes.fromhex("e58e26"))
        assert val == 624485 and n == 3

    def test_multibyte_stream(self):
        data = bytes.fromhex("ac02") + encode_leb128(624485)
        val1, n1 = decode_leb128(data)
        val2, n2 = decode_leb128(data[n1:])
        assert val1 == 300 and val2 == 624485

    def test_empty_raises(self):
        with pytest.raises((ValueError, IndexError)):
            decode_leb128(b"")

    def test_truncated_raises(self):
        # 0x80 has continuation bit set but no next byte
        with pytest.raises((ValueError, IndexError)):
            decode_leb128(bytes.fromhex("80"))


# ---------------------------------------------------------------------------
# Hex helpers
# ---------------------------------------------------------------------------

class TestHexHelpers:
    def test_to_hex(self):
        assert to_hex(bytes.fromhex("deadbeef")) == "deadbeef"

    def test_from_hex(self):
        assert from_hex("deadbeef") == bytes.fromhex("deadbeef")

    def test_to_hex_empty(self):
        assert to_hex(b"") == ""

    def test_from_hex_empty(self):
        assert from_hex("") == b""


# ---------------------------------------------------------------------------
# AssetId derivation
# ---------------------------------------------------------------------------

class TestDeriveAssetId:
    def test_basic_derivation(self):
        txid = "a" * 64
        asset_id = derive_asset_id(txid, vout=0)
        assert isinstance(asset_id, str)
        assert len(asset_id) > 10

    def test_deterministic(self):
        txid = "b" * 64
        assert derive_asset_id(txid, vout=0) == derive_asset_id(txid, vout=0)

    def test_different_vout(self):
        txid = "c" * 64
        id0 = derive_asset_id(txid, vout=0)
        id1 = derive_asset_id(txid, vout=1)
        assert id0 != id1

    def test_different_txid(self):
        id_a = derive_asset_id("a" * 64, vout=0)
        id_b = derive_asset_id("b" * 64, vout=0)
        assert id_a != id_b

    def test_invalid_txid_raises(self):
        with pytest.raises((ValueError, Exception)):
            derive_asset_id("short", vout=0)
