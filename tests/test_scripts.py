"""
Tests for digiforge.scripts -- NUMS point, CLTV script, P2TR collateral output.
Pure computation -- no RPC mocking needed.
"""
import pytest
from digiforge.scripts import (
    NUMS_POINT,
    NUMS_POINT_HEX,
    DEFAULT_TIMELOCK_BLOCKS,
    CollateralOutput,
    DigiDollarScripts,
    InvalidPubkeyError,
    ScriptError,
    build_cltv_script,
    encode_script_number,
    push_bytes,
    taproot_tagged_hash,
    tapscript_leaf_hash,
    taproot_tweak,
)
from digiforge.exceptions import ValidationError


# Test pubkeys -- valid compressed secp256k1 (02/03 prefix, 33 bytes)
TEST_PUBKEY_02 = bytes.fromhex(
    "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
)  # secp256k1 generator G
TEST_PUBKEY_03 = bytes.fromhex(
    "03c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
)


# ---------------------------------------------------------------------------
# NUMS point
# ---------------------------------------------------------------------------

class TestNUMSPoint:
    def test_nums_point_length(self):
        assert len(NUMS_POINT) == 33

    def test_nums_point_prefix(self):
        assert NUMS_POINT[0] in (0x02, 0x03)

    def test_nums_point_hex_matches_bytes(self):
        assert NUMS_POINT == bytes.fromhex(NUMS_POINT_HEX)

    def test_nums_xonly_is_32_bytes(self):
        scripts = DigiDollarScripts()
        assert len(scripts.nums_point_xonly()) == 32

    def test_nums_xonly_strips_prefix(self):
        scripts = DigiDollarScripts()
        assert scripts.nums_point_xonly() == NUMS_POINT[1:]

    def test_nums_full_point_matches(self):
        scripts = DigiDollarScripts()
        assert scripts.nums_point() == NUMS_POINT


# ---------------------------------------------------------------------------
# Script number encoding
# ---------------------------------------------------------------------------

class TestEncodeScriptNumber:
    def test_zero(self):
        assert encode_script_number(0) == b""

    def test_one(self):
        assert encode_script_number(1) == bytes([0x01])

    def test_127(self):
        assert encode_script_number(127) == bytes([0x7f])

    def test_128(self):
        # 128 needs sign extension: 0x80 is sign bit, so add 0x00
        result = encode_script_number(128)
        assert len(result) == 2
        assert result == bytes([0x80, 0x00])

    def test_negative_one(self):
        result = encode_script_number(-1)
        assert result == bytes([0x81])  # 1 with sign bit set

    def test_roundtrip_large(self):
        # 3_500_000 block height for timelock
        encoded = encode_script_number(3_500_000)
        assert len(encoded) > 0
        assert isinstance(encoded, bytes)


# ---------------------------------------------------------------------------
# Push bytes
# ---------------------------------------------------------------------------

class TestPushBytes:
    def test_single_byte(self):
        result = push_bytes(bytes([0xab]))
        assert result == bytes([0x01, 0xab])

    def test_20_bytes(self):
        data = bytes(20)
        result = push_bytes(data)
        assert result[0] == 20
        assert result[1:] == data

    def test_75_bytes_max_single(self):
        data = bytes(75)
        result = push_bytes(data)
        assert result[0] == 75

    def test_76_bytes_uses_pushdata1(self):
        data = bytes(76)
        result = push_bytes(data)
        assert result[0] == 0x4c  # OP_PUSHDATA1
        assert result[1] == 76


# ---------------------------------------------------------------------------
# CLTV script
# ---------------------------------------------------------------------------

class TestBuildCLTVScript:
    def test_returns_bytes(self):
        script = build_cltv_script(3_500_000, TEST_PUBKEY_02)
        assert isinstance(script, bytes)
        assert len(script) > 0

    def test_contains_cltv_opcode(self):
        script = build_cltv_script(3_500_000, TEST_PUBKEY_02)
        assert 0xb1 in script  # OP_CLTV

    def test_contains_op_checksig(self):
        script = build_cltv_script(3_500_000, TEST_PUBKEY_02)
        assert 0xac in script  # OP_CHECKSIG

    def test_contains_op_drop(self):
        script = build_cltv_script(3_500_000, TEST_PUBKEY_02)
        assert 0x75 in script  # OP_DROP

    def test_different_timelocks_differ(self):
        s1 = build_cltv_script(3_000_000, TEST_PUBKEY_02)
        s2 = build_cltv_script(4_000_000, TEST_PUBKEY_02)
        assert s1 != s2

    def test_different_pubkeys_differ(self):
        s1 = build_cltv_script(3_500_000, TEST_PUBKEY_02)
        s2 = build_cltv_script(3_500_000, TEST_PUBKEY_03)
        assert s1 != s2

    def test_zero_timelock_raises(self):
        with pytest.raises(ValidationError):
            build_cltv_script(0, TEST_PUBKEY_02)

    def test_negative_timelock_raises(self):
        with pytest.raises((ValidationError, Exception)):
            build_cltv_script(-1, TEST_PUBKEY_02)

    def test_invalid_pubkey_raises(self):
        with pytest.raises(InvalidPubkeyError):
            build_cltv_script(3_500_000, bytes(33))  # all zeros, bad prefix

    def test_wrong_pubkey_length_raises(self):
        with pytest.raises(InvalidPubkeyError):
            build_cltv_script(3_500_000, bytes(32))  # 32 bytes, not 33


# ---------------------------------------------------------------------------
# Taproot tagged hash
# ---------------------------------------------------------------------------

class TestTaprootTaggedHash:
    def test_returns_32_bytes(self):
        result = taproot_tagged_hash("TapLeaf", b"test")
        assert len(result) == 32

    def test_deterministic(self):
        r1 = taproot_tagged_hash("TapTweak", b"data")
        r2 = taproot_tagged_hash("TapTweak", b"data")
        assert r1 == r2

    def test_different_tags_differ(self):
        r1 = taproot_tagged_hash("TapLeaf", b"same")
        r2 = taproot_tagged_hash("TapBranch", b"same")
        assert r1 != r2

    def test_different_data_differs(self):
        r1 = taproot_tagged_hash("TapTweak", b"data1")
        r2 = taproot_tagged_hash("TapTweak", b"data2")
        assert r1 != r2


# ---------------------------------------------------------------------------
# TapLeaf hash
# ---------------------------------------------------------------------------

class TestTapscriptLeafHash:
    def test_returns_32_bytes(self):
        script = build_cltv_script(3_500_000, TEST_PUBKEY_02)
        leaf = tapscript_leaf_hash(script)
        assert len(leaf) == 32

    def test_deterministic(self):
        script = build_cltv_script(3_500_000, TEST_PUBKEY_02)
        assert tapscript_leaf_hash(script) == tapscript_leaf_hash(script)

    def test_different_scripts_differ(self):
        s1 = build_cltv_script(3_500_000, TEST_PUBKEY_02)
        s2 = build_cltv_script(3_500_000, TEST_PUBKEY_03)
        assert tapscript_leaf_hash(s1) != tapscript_leaf_hash(s2)


# ---------------------------------------------------------------------------
# Taproot tweak
# ---------------------------------------------------------------------------

class TestTaprootTweak:
    def test_returns_32_bytes(self):
        xonly = NUMS_POINT[1:]
        tweak = taproot_tweak(xonly)
        assert len(tweak) == 32

    def test_with_merkle_root(self):
        xonly = NUMS_POINT[1:]
        root  = bytes(32)
        tweak = taproot_tweak(xonly, root)
        assert len(tweak) == 32

    def test_wrong_key_length_raises(self):
        with pytest.raises(ScriptError):
            taproot_tweak(bytes(33))  # must be 32 bytes x-only

    def test_deterministic(self):
        xonly = NUMS_POINT[1:]
        root  = bytes(range(32))
        assert taproot_tweak(xonly, root) == taproot_tweak(xonly, root)


# ---------------------------------------------------------------------------
# DigiDollarScripts
# ---------------------------------------------------------------------------

class TestDigiDollarScripts:
    def setup_method(self):
        self.scripts = DigiDollarScripts()

    def test_build_collateral_output_returns_dataclass(self):
        output = self.scripts.build_collateral_output(
            owner_pubkey=TEST_PUBKEY_02,
            timelock=3_500_000,
        )
        assert isinstance(output, CollateralOutput)

    def test_output_uses_nums_internal_key(self):
        output = self.scripts.build_collateral_output(
            owner_pubkey=TEST_PUBKEY_02,
            timelock=3_500_000,
        )
        assert self.scripts.verify_nums_internal_key(output)

    def test_output_internal_key_is_nums_xonly(self):
        output = self.scripts.build_collateral_output(
            owner_pubkey=TEST_PUBKEY_02,
            timelock=3_500_000,
        )
        assert output.internal_key == NUMS_POINT[1:]

    def test_scriptpubkey_is_34_bytes(self):
        output = self.scripts.build_collateral_output(
            owner_pubkey=TEST_PUBKEY_02,
            timelock=3_500_000,
        )
        assert len(output.scriptpubkey) == 34

    def test_scriptpubkey_starts_with_op1(self):
        output = self.scripts.build_collateral_output(
            owner_pubkey=TEST_PUBKEY_02,
            timelock=3_500_000,
        )
        assert output.scriptpubkey[0] == 0x51  # OP_1

    def test_leaf_hash_is_32_bytes(self):
        output = self.scripts.build_collateral_output(
            owner_pubkey=TEST_PUBKEY_02,
            timelock=3_500_000,
        )
        assert len(output.leaf_hash) == 32

    def test_merkle_root_equals_leaf_hash_single_script(self):
        output = self.scripts.build_collateral_output(
            owner_pubkey=TEST_PUBKEY_02,
            timelock=3_500_000,
        )
        # Single script: merkle root = leaf hash
        assert output.merkle_root == output.leaf_hash

    def test_different_owners_produce_different_scripts(self):
        o1 = self.scripts.build_collateral_output(TEST_PUBKEY_02, 3_500_000)
        o2 = self.scripts.build_collateral_output(TEST_PUBKEY_03, 3_500_000)
        assert o1.cltv_script != o2.cltv_script
        assert o1.leaf_hash   != o2.leaf_hash

    def test_different_timelocks_produce_different_scripts(self):
        o1 = self.scripts.build_collateral_output(TEST_PUBKEY_02, 3_000_000)
        o2 = self.scripts.build_collateral_output(TEST_PUBKEY_02, 4_000_000)
        assert o1.cltv_script != o2.cltv_script

    def test_deterministic_same_inputs(self):
        o1 = self.scripts.build_collateral_output(TEST_PUBKEY_02, 3_500_000)
        o2 = self.scripts.build_collateral_output(TEST_PUBKEY_02, 3_500_000)
        assert o1.cltv_script  == o2.cltv_script
        assert o1.scriptpubkey == o2.scriptpubkey
        assert o1.leaf_hash    == o2.leaf_hash

    def test_timelock_recorded_in_output(self):
        output = self.scripts.build_collateral_output(TEST_PUBKEY_02, 3_500_000)
        assert output.timelock == 3_500_000

    def test_owner_pubkey_recorded(self):
        output = self.scripts.build_collateral_output(TEST_PUBKEY_02, 3_500_000)
        assert output.owner_pubkey == TEST_PUBKEY_02

    def test_default_timelock_is_defined(self):
        output = self.scripts.build_collateral_output(TEST_PUBKEY_02)
        assert output.timelock == DEFAULT_TIMELOCK_BLOCKS

    def test_invalid_pubkey_raises(self):
        with pytest.raises(InvalidPubkeyError):
            self.scripts.build_collateral_output(bytes(33), 3_500_000)

    def test_str_representation(self):
        output = self.scripts.build_collateral_output(TEST_PUBKEY_02, 3_500_000)
        s = str(output)
        assert "NUMS" in s
        assert "3,500,000" in s

    def test_estimate_timelock_30_days(self):
        blocks = self.scripts.estimate_timelock_blocks(30)
        # 30 days * 24h * 60m * 60s / 15s = 172800
        assert blocks == 172_800

    def test_estimate_timelock_1_day(self):
        blocks = self.scripts.estimate_timelock_blocks(1)
        assert blocks == 5_760

    def test_estimate_timelock_zero_raises(self):
        with pytest.raises(ValidationError):
            self.scripts.estimate_timelock_blocks(0)

    def test_cltv_script_hex(self):
        output = self.scripts.build_collateral_output(TEST_PUBKEY_02, 3_500_000)
        hex_str = output.cltv_script_hex
        assert isinstance(hex_str, str)
        bytes.fromhex(hex_str)  # must be valid hex
