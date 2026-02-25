"""
Tests for digiforge.metadata — DigiAssets v3 payload encoding.
"""
import pytest
from digiforge.metadata import (
    AggregationPolicy,
    AssetMetadata,
    AssetUrl,
    DA_MAGIC,
    DA_TRANSFER_OPCODE,
    DA_VERSION_3,
    Divisibility,
    IssuancePayload,
    TransferInstruction,
    TransferPayload,
)
from digiforge.exceptions import MetadataValidationError, MetadataEncodingError


class TestAssetMetadata:
    def test_basic_creation(self):
        meta = AssetMetadata(asset_name="TestToken", issuer="Kael", description="Test")
        assert meta.asset_name == "TestToken"
        assert meta.version == 1

    def test_validate_empty_name_raises(self):
        meta = AssetMetadata(asset_name="")
        with pytest.raises(MetadataValidationError, match="asset_name"):
            meta.validate()

    def test_validate_name_too_long_raises(self):
        meta = AssetMetadata(asset_name="x" * 101)
        with pytest.raises(MetadataValidationError, match="too long"):
            meta.validate()

    def test_add_url_chaining(self):
        meta = AssetMetadata(asset_name="TestToken")
        result = meta.add_url(AssetUrl(name="site", url="https://example.com", mime_type="text/html"))
        assert result is meta  # returns self
        assert len(meta.urls) == 1

    def test_invalid_url_raises(self):
        meta = AssetMetadata(asset_name="TestToken")
        meta.add_url(AssetUrl(name="bad", url="ftp://invalid.com"))
        with pytest.raises(MetadataValidationError, match="URL"):
            meta.validate()

    def test_encode_returns_bytes(self):
        meta = AssetMetadata(asset_name="TestToken", issuer="Kael")
        data = meta.encode()
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_encode_valid_json(self):
        import json
        meta = AssetMetadata(asset_name="TestToken", issuer="Kael", description="Test asset")
        doc = json.loads(meta.encode())
        assert doc["data"]["assetName"] == "TestToken"
        assert doc["data"]["issuer"] == "Kael"
        assert doc["rules"]["version"] == 3

    def test_hash_bytes_length(self):
        meta = AssetMetadata(asset_name="TestToken")
        h = meta.hash_bytes()
        assert len(h) == 20

    def test_hash_deterministic(self):
        meta = AssetMetadata(asset_name="TestToken", issuer="Kael")
        assert meta.hash_bytes() == meta.hash_bytes()

    def test_hash_different_for_different_names(self):
        m1 = AssetMetadata(asset_name="Token A")
        m2 = AssetMetadata(asset_name="Token B")
        assert m1.hash_bytes() != m2.hash_bytes()

    def test_version_increment(self):
        # Simulates metadata update without new issuance
        meta_v1 = AssetMetadata(asset_name="DigiDollar", version=1)
        meta_v2 = AssetMetadata(asset_name="DigiDollar", version=2)
        assert meta_v1.hash_bytes() != meta_v2.hash_bytes()


class TestIssuancePayload:
    def _make_payload(self, amount=1_000_000, **kwargs) -> IssuancePayload:
        return IssuancePayload(amount=amount, **kwargs)

    def test_encode_starts_with_da_magic(self):
        payload = self._make_payload()
        data = payload.encode()
        assert data[:2] == DA_MAGIC

    def test_encode_version_byte(self):
        data = self._make_payload().encode()
        assert data[2] == DA_VERSION_3

    def test_flags_locked_aggregatable(self):
        payload = IssuancePayload(
            amount=1000,
            divisibility=Divisibility.WHOLE,
            lock_status=True,
            aggregation_policy=AggregationPolicy.AGGREGATABLE,
        )
        data = payload.encode()
        flags = data[3]
        assert flags & 0x07 == 0          # divisibility = 0
        assert flags & (1 << 3)           # lock bit set
        assert (flags >> 4) & 0x03 == 0   # aggregatable = 0

    def test_flags_unlocked_dispersed(self):
        payload = IssuancePayload(
            amount=1,
            divisibility=Divisibility.MICRO,
            lock_status=False,
            aggregation_policy=AggregationPolicy.DISPERSED,
        )
        flags = payload.encode()[3]
        assert flags & 0x07 == 6          # micro = 6
        assert not (flags & (1 << 3))     # unlocked
        assert (flags >> 4) & 0x03 == 2   # dispersed = 2

    def test_encode_with_metadata_hash(self):
        meta = AssetMetadata(asset_name="DigiDollar", issuer="Project Trinity")
        payload = IssuancePayload(amount=1_000_000, metadata=meta)
        data = payload.encode()
        # Extract hash at expected position (after magic + version + flags + LEB128 amount)
        # Amount 1_000_000 = 0xF4240 encodes as 3 bytes in LEB128
        from digiforge.utils import encode_leb128
        leb_amount = encode_leb128(1_000_000)
        hash_start = 4 + len(leb_amount)
        on_chain_hash = data[hash_start:hash_start + 20]
        assert on_chain_hash == meta.hash_bytes()

    def test_zero_amount_raises(self):
        with pytest.raises(Exception):
            IssuancePayload(amount=0).encode()

    def test_encode_hex_is_valid_hex(self):
        hex_str = IssuancePayload(amount=100).encode_hex()
        bytes.fromhex(hex_str)  # should not raise

    def test_decode_roundtrip(self):
        original = IssuancePayload(
            amount=500_000,
            divisibility=Divisibility.MICRO,
            lock_status=True,
            aggregation_policy=AggregationPolicy.HYBRID,
        )
        encoded = original.encode()
        decoded = IssuancePayload.decode(encoded)
        assert decoded.amount == original.amount
        assert decoded.divisibility == original.divisibility
        assert decoded.lock_status == original.lock_status
        assert decoded.aggregation_policy == original.aggregation_policy

    def test_decode_wrong_magic_raises(self):
        bad = b"\x00\x00\x00" + b"" + b"\x00" * 20
        with pytest.raises(MetadataEncodingError, match="magic"):
            IssuancePayload.decode(bad)


class TestTransferPayload:
    def test_encode_starts_with_da_magic(self):
        tp = TransferPayload().add(vout=1, amount=100)
        data = tp.encode()
        assert data[:2] == DA_MAGIC

    def test_encode_transfer_opcode(self):
        tp = TransferPayload().add(vout=1, amount=100)
        data = tp.encode()
        assert data[2] == DA_TRANSFER_OPCODE

    def test_multiple_outputs(self):
        tp = TransferPayload()
        tp.add(vout=1, amount=500)
        tp.add(vout=2, amount=300)
        data = tp.encode()
        assert data[:2] == DA_MAGIC
        assert len(data) > 3

    def test_chaining(self):
        tp = TransferPayload()
        result = tp.add(vout=1, amount=100)
        assert result is tp

    def test_fits_in_op_return(self):
        tp = TransferPayload()
        for i in range(10):
            tp.add(vout=i + 1, amount=100)
        data = tp.encode()
        assert len(data) <= 80

    def test_encode_hex(self):
        tp = TransferPayload().add(vout=1, amount=1000)
        hex_str = tp.encode_hex()
        assert hex_str.startswith("4441")  # DA magic
