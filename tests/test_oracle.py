"""
Tests for digiforge.oracle -- OracleClient with mocked RPC.
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock
from digiforge.oracle import (
    OracleClient,
    OraclePrice,
    OracleStatus,
    OraclePriceStaleError,
    OracleQuorumError,
    OracleUnavailableError,
    OracleError,
    ORACLE_QUORUM,
    ORACLE_TOTAL,
    ORACLE_STALE_THRESHOLD_SECONDS,
)
from digiforge.exceptions import RPCError


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

ORACLE_FRESH = {
    "price": "0.025000",
    "stale": False,
    "activeOracles": 6,
    "timestamp": 1772734114,
}

ORACLE_STALE = {
    "price": "0.025000",
    "stale": True,
    "activeOracles": 6,
    "timestamp": 1772733514,
}

ORACLE_LOW_QUORUM = {
    "price": "0.025000",
    "stale": False,
    "activeOracles": 3,
    "timestamp": 1772734114,
}

CHAIN_INFO = {"blocks": 3_500_000, "chain": "test"}

ORACLE_STATUS_RESPONSE = {
    "consensusPrice": "0.025000",
    "operators": [
        {"id": f"op{i}", "active": i < 6} for i in range(8)
    ],
    "lastUpdate": 1772734084,
}


def make_client(side_effects):
    client = OracleClient.__new__(OracleClient)
    mock_rpc = MagicMock()
    mock_rpc.call.side_effect = side_effects
    mock_rpc.config.url = "http://127.0.0.1:12022"
    client.rpc = mock_rpc
    client._stale_threshold = ORACLE_STALE_THRESHOLD_SECONDS
    return client


# ---------------------------------------------------------------------------
# OraclePrice dataclass
# ---------------------------------------------------------------------------

class TestOraclePriceDataclass:
    def make_price(self, stale=False, active=6):
        return OraclePrice(
            price_usd=Decimal("0.025"),
            timestamp=1772734114,
            age_seconds=10,
            stale=stale,
            active_oracles=active,
            quorum_met=active >= ORACLE_QUORUM,
            block_height=3_500_000,
        )

    def test_is_trusted_fresh_quorum(self):
        assert self.make_price().is_trusted

    def test_not_trusted_if_stale(self):
        assert not self.make_price(stale=True).is_trusted

    def test_not_trusted_if_no_quorum(self):
        assert not self.make_price(active=3).is_trusted

    def test_str_contains_price(self):
        assert "0.025" in str(self.make_price())

    def test_str_trusted_label(self):
        assert "TRUSTED" in str(self.make_price())

    def test_str_untrusted_label(self):
        assert "UNTRUSTED" in str(self.make_price(stale=True))


# ---------------------------------------------------------------------------
# OracleStatus dataclass
# ---------------------------------------------------------------------------

class TestOracleStatusDataclass:
    def make_status(self, active_count=6):
        operators = [{"id": f"op{i}", "active": i < active_count} for i in range(8)]
        return OracleStatus(
            operators=operators,
            consensus_price=Decimal("0.025"),
            quorum_met=active_count >= ORACLE_QUORUM,
            last_update=1772734114,
            block_height=3_500_000,
        )

    def test_active_count(self):
        assert self.make_status(6).active_count == 6

    def test_active_count_zero(self):
        assert self.make_status(0).active_count == 0

    def test_str_contains_price(self):
        assert "0.025" in str(self.make_status())

    def test_quorum_met(self):
        assert self.make_status(5).quorum_met
        assert not self.make_status(4).quorum_met


# ---------------------------------------------------------------------------
# price()
# ---------------------------------------------------------------------------

class TestPrice:
    def test_returns_oracle_price(self):
        client = make_client([ORACLE_FRESH, CHAIN_INFO])
        p = client.price()
        assert isinstance(p, OraclePrice)
        assert p.price_usd == Decimal("0.025000")

    def test_stale_flag_propagated(self):
        client = make_client([ORACLE_STALE, CHAIN_INFO])
        p = client.price()
        assert p.stale

    def test_active_oracles_correct(self):
        client = make_client([ORACLE_FRESH, CHAIN_INFO])
        p = client.price()
        assert p.active_oracles == 6

    def test_quorum_met_with_6_oracles(self):
        client = make_client([ORACLE_FRESH, CHAIN_INFO])
        p = client.price()
        assert p.quorum_met

    def test_quorum_not_met_with_3_oracles(self):
        client = make_client([ORACLE_LOW_QUORUM, CHAIN_INFO])
        p = client.price()
        assert not p.quorum_met

    def test_block_height_correct(self):
        client = make_client([ORACLE_FRESH, CHAIN_INFO])
        p = client.price()
        assert p.block_height == 3_500_000

    def test_rpc_not_found_raises_unavailable(self):
        client = OracleClient.__new__(OracleClient)
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RPCError(-32601, "method not found")
        mock_rpc.config.url = "http://127.0.0.1:12022"
        client.rpc = mock_rpc
        client._stale_threshold = ORACLE_STALE_THRESHOLD_SECONDS
        with pytest.raises(OracleUnavailableError):
            client.price()


# ---------------------------------------------------------------------------
# trusted_price()
# ---------------------------------------------------------------------------

class TestTrustedPrice:
    def test_returns_decimal_when_fresh(self):
        client = make_client([ORACLE_FRESH, CHAIN_INFO])
        price = client.trusted_price()
        assert isinstance(price, Decimal)
        assert price == Decimal("0.025000")

    def test_raises_stale_when_stale(self):
        client = make_client([ORACLE_STALE, CHAIN_INFO])
        with pytest.raises(OraclePriceStaleError):
            client.trusted_price()

    def test_raises_quorum_when_low(self):
        client = make_client([ORACLE_LOW_QUORUM, CHAIN_INFO])
        with pytest.raises(OracleQuorumError):
            client.trusted_price()

    def test_stale_error_has_age_info(self):
        client = make_client([ORACLE_STALE, CHAIN_INFO])
        try:
            client.trusted_price()
        except OraclePriceStaleError as e:
            assert e.age_seconds > 0
            assert e.threshold == ORACLE_STALE_THRESHOLD_SECONDS

    def test_quorum_error_has_count_info(self):
        client = make_client([ORACLE_LOW_QUORUM, CHAIN_INFO])
        try:
            client.trusted_price()
        except OracleQuorumError as e:
            assert e.active == 3
            assert e.required == ORACLE_QUORUM


# ---------------------------------------------------------------------------
# price_usd()
# ---------------------------------------------------------------------------

class TestPriceUSD:
    def test_returns_decimal(self):
        client = make_client([ORACLE_FRESH, CHAIN_INFO])
        usd = client.price_usd()
        assert isinstance(usd, Decimal)
        assert usd == Decimal("0.025000")


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------

class TestStatus:
    def test_returns_oracle_status(self):
        client = make_client([ORACLE_STATUS_RESPONSE, CHAIN_INFO])
        status = client.status()
        assert isinstance(status, OracleStatus)

    def test_consensus_price_parsed(self):
        client = make_client([ORACLE_STATUS_RESPONSE, CHAIN_INFO])
        status = client.status()
        assert status.consensus_price == Decimal("0.025000")

    def test_quorum_met(self):
        client = make_client([ORACLE_STATUS_RESPONSE, CHAIN_INFO])
        status = client.status()
        assert status.quorum_met

    def test_active_count_correct(self):
        client = make_client([ORACLE_STATUS_RESPONSE, CHAIN_INFO])
        status = client.status()
        assert status.active_count == 6

    def test_no_consensus_price(self):
        resp = {**ORACLE_STATUS_RESPONSE, "consensusPrice": None}
        client = make_client([resp, CHAIN_INFO])
        status = client.status()
        assert status.consensus_price is None

    def test_rpc_not_found_raises_unavailable(self):
        client = OracleClient.__new__(OracleClient)
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RPCError(-32601, "method not found")
        mock_rpc.config.url = "http://127.0.0.1:12022"
        client.rpc = mock_rpc
        client._stale_threshold = ORACLE_STALE_THRESHOLD_SECONDS
        with pytest.raises(OracleUnavailableError):
            client.status()


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_true_when_rpc_works(self):
        client = make_client([ORACLE_FRESH, CHAIN_INFO])
        assert client.is_available()

    def test_false_when_method_not_found(self):
        client = OracleClient.__new__(OracleClient)
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RPCError(-32601, "method not found")
        mock_rpc.config.url = "http://127.0.0.1:12022"
        client.rpc = mock_rpc
        client._stale_threshold = ORACLE_STALE_THRESHOLD_SECONDS
        assert not client.is_available()


# ---------------------------------------------------------------------------
# set_stale_threshold()
# ---------------------------------------------------------------------------

class TestSetStaleThreshold:
    def test_updates_threshold(self):
        client = make_client([])
        client.set_stale_threshold(60)
        assert client._stale_threshold == 60

    def test_zero_raises(self):
        client = make_client([])
        with pytest.raises(ValueError):
            client.set_stale_threshold(0)

    def test_negative_raises(self):
        client = make_client([])
        with pytest.raises(ValueError):
            client.set_stale_threshold(-1)


# ---------------------------------------------------------------------------
# repr
# ---------------------------------------------------------------------------

class TestRepr:
    def test_repr_contains_url(self):
        client = make_client([])
        r = repr(client)
        assert "127.0.0.1" in r
        assert "stale_threshold" in r
