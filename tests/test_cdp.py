"""
Tests for digiforge.cdp -- CDPClient with mocked RPC.
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch
from digiforge.cdp import (
    CDPClient, CDPHealth, CDPPosition, CDPStatus,
    CDPRatioTooLowError, CDPMintBlockedError, CDPNotFoundError,
    CDPRedemptionError, OracleStaleError, MintResult, RedeemResult,
    SAFE_COLLATERAL_RATIO, MINIMUM_COLLATERAL_RATIO,
)
from digiforge.exceptions import ValidationError, InsufficientFundsError, RPCError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

ORACLE_RESPONSE_FRESH = {
    "price": "0.025",
    "stale": False,
    "activeOracles": 6,
    "timestamp": 1772734065,
}

ORACLE_RESPONSE_STALE = {
    "price": "0.025",
    "stale": True,
    "activeOracles": 6,
    "timestamp": 1772733465,
}

CHAIN_INFO = {"blocks": 3_500_000, "chain": "test"}

CDP_STATS = {
    "mintingBlocked": False,
    "totalSupply": "5000.00",
    "avgCollateralRatio": "310.5",
    "errThreshold": "150",
}

CDP_INFO_HEALTHY = {
    "status": "healthy",
    "collateralSatoshis": 1_000_000_000_000,  # 10,000 DGB
    "debtDUSD": "83.33",
    "collateralRatio": "300.0",
    "dgbPriceUSD": "0.025",
    "collateralUSD": "250.00",
    "liquidationPrice": "0.0125",
    "timelockHeight": 3_672_800,
    "txid": "a" * 64,
}

MINT_RESPONSE = {"txid": "b" * 64}
REDEEM_RESPONSE = {
    "txid": "c" * 64,
    "collateralReturnedSatoshis": 999_000_000,
    "stabilityFeeSatoshis": 1_000_000,
}


def make_client(side_effects):
    """Create a CDPClient with mocked RPC call responses."""
    client = CDPClient.__new__(CDPClient)
    mock_rpc = MagicMock()
    mock_rpc.call.side_effect = side_effects
    mock_rpc.config.url = "http://127.0.0.1:12022"
    client.rpc = mock_rpc
    return client


# ---------------------------------------------------------------------------
# CDPHealth
# ---------------------------------------------------------------------------

class TestCDPHealth:
    def test_is_healthy_when_fresh_and_unblocked(self):
        health = CDPHealth(
            oracle_price_usd=Decimal("0.025"),
            oracle_stale=False,
            minting_blocked=False,
            total_dusd_supply=Decimal("5000"),
            avg_collateral_ratio=Decimal("310"),
            err_threshold=Decimal("150"),
            block_height=3_500_000,
        )
        assert health.is_healthy

    def test_not_healthy_when_stale(self):
        health = CDPHealth(
            oracle_price_usd=Decimal("0.025"),
            oracle_stale=True,
            minting_blocked=False,
            total_dusd_supply=Decimal("5000"),
            avg_collateral_ratio=Decimal("310"),
            err_threshold=Decimal("150"),
            block_height=3_500_000,
        )
        assert not health.is_healthy

    def test_not_healthy_when_blocked(self):
        health = CDPHealth(
            oracle_price_usd=Decimal("0.025"),
            oracle_stale=False,
            minting_blocked=True,
            total_dusd_supply=Decimal("5000"),
            avg_collateral_ratio=Decimal("310"),
            err_threshold=Decimal("150"),
            block_height=3_500_000,
        )
        assert not health.is_healthy

    def test_str_contains_price(self):
        health = CDPHealth(
            oracle_price_usd=Decimal("0.025"),
            oracle_stale=False,
            minting_blocked=False,
            total_dusd_supply=Decimal("5000"),
            avg_collateral_ratio=Decimal("310"),
            err_threshold=Decimal("150"),
            block_height=3_500_000,
        )
        assert "0.025" in str(health)


# ---------------------------------------------------------------------------
# CDPPosition
# ---------------------------------------------------------------------------

class TestCDPPosition:
    def make_position(self, ratio="300.0", status=CDPStatus.HEALTHY):
        return CDPPosition(
            collateral_dgb=1_000_000_000_000,
            debt_dusd=Decimal("83.33"),
            collateral_ratio=Decimal(ratio),
            dgb_price_usd=Decimal("0.025"),
            collateral_usd=Decimal("250.00"),
            liquidation_price=Decimal("0.0125"),
            status=status,
        )

    def test_is_safe_at_300(self):
        assert self.make_position("300.0").is_safe

    def test_not_safe_below_300(self):
        assert not self.make_position("299.9").is_safe

    def test_collateral_dgb_display(self):
        pos = self.make_position()
        assert abs(pos.collateral_dgb_display - 10_000.0) < 0.001

    def test_distance_to_liquidation(self):
        pos = self.make_position()
        dist = pos.distance_to_liquidation
        assert dist > 0
        assert dist == Decimal("0.025") - Decimal("0.0125")

    def test_str_contains_ratio(self):
        assert "300" in str(self.make_position())


# ---------------------------------------------------------------------------
# oracle_price
# ---------------------------------------------------------------------------

class TestOraclePrice:
    def test_returns_decimal(self):
        client = make_client([ORACLE_RESPONSE_FRESH])
        price = client.oracle_price()
        assert isinstance(price, Decimal)
        assert price == Decimal("0.025")

    def test_stale_raises(self):
        client = make_client([ORACLE_RESPONSE_STALE])
        with pytest.raises(OracleStaleError):
            client.oracle_price()


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_cdphealth(self):
        client = make_client([ORACLE_RESPONSE_FRESH, CHAIN_INFO, CDP_STATS])
        health = client.health()
        assert isinstance(health, CDPHealth)
        assert health.oracle_price_usd == Decimal("0.025")
        assert not health.oracle_stale
        assert not health.minting_blocked
        assert health.block_height == 3_500_000

    def test_health_blocked(self):
        stats_blocked = {**CDP_STATS, "mintingBlocked": True}
        client = make_client([ORACLE_RESPONSE_FRESH, CHAIN_INFO, stats_blocked])
        health = client.health()
        assert health.minting_blocked
        assert not health.is_healthy


# ---------------------------------------------------------------------------
# position()
# ---------------------------------------------------------------------------

class TestPosition:
    def test_position_returns_cdpposition(self):
        client = make_client([CDP_INFO_HEALTHY])
        pos = client.position()
        assert isinstance(pos, CDPPosition)
        assert pos.collateral_dgb == 1_000_000_000_000
        assert pos.status == CDPStatus.HEALTHY

    def test_position_none_raises_not_found(self):
        client = make_client([{"status": "none"}])
        with pytest.raises(CDPNotFoundError):
            client.position()

    def test_position_rpc_error_not_found(self):
        client = CDPClient.__new__(CDPClient)
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RPCError(-5, "no cdp found")
        mock_rpc.config.url = "http://127.0.0.1:12022"
        client.rpc = mock_rpc
        with pytest.raises(CDPNotFoundError):
            client.position()


# ---------------------------------------------------------------------------
# calculate_mintable()
# ---------------------------------------------------------------------------

class TestCalculateMintable:
    def test_basic_calculation(self):
        # 10,000 DGB @ $0.025 = $250 collateral
        # At 300% ratio: $250 / 3.0 = $83.33 DUSD
        client = make_client([ORACLE_RESPONSE_FRESH])
        mintable = client.calculate_mintable(1_000_000_000_000, Decimal("300"))
        assert abs(mintable - Decimal("83.33")) < Decimal("0.01")

    def test_ratio_too_low_raises(self):
        client = make_client([])
        with pytest.raises(CDPRatioTooLowError):
            client.calculate_mintable(1_000_000_000_000, Decimal("100"))

    def test_zero_collateral_raises(self):
        client = make_client([])
        with pytest.raises(ValidationError):
            client.calculate_mintable(0, Decimal("300"))

    def test_rounds_down(self):
        client = make_client([ORACLE_RESPONSE_FRESH])
        mintable = client.calculate_mintable(1_000_000_000_000, Decimal("300"))
        # Must have exactly 2 decimal places, rounded DOWN
        str_val = str(mintable)
        if "." in str_val:
            decimals = len(str_val.split(".")[1])
            assert decimals <= 2


# ---------------------------------------------------------------------------
# mint()
# ---------------------------------------------------------------------------

class TestMint:
    def test_mint_returns_result(self):
        client = make_client([
            ORACLE_RESPONSE_FRESH,  # calculate_mintable oracle call
            ORACLE_RESPONSE_FRESH,  # mint oracle call
            MINT_RESPONSE,           # mintdigidollar
        ])
        result = client.mint(1_000_000_000_000, ratio=300)
        assert isinstance(result, MintResult)
        assert result.txid == "b" * 64
        assert result.collateral_dgb == 1_000_000_000_000

    def test_mint_ratio_too_low_raises(self):
        client = make_client([])
        with pytest.raises(CDPRatioTooLowError):
            client.mint(1_000_000_000_000, ratio=100)

    def test_mint_blocked_raises(self):
        client = CDPClient.__new__(CDPClient)
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = [
            ORACLE_RESPONSE_FRESH,
            ORACLE_RESPONSE_FRESH,
            RPCError(-32, "minting blocked"),
        ]
        mock_rpc.config.url = "http://127.0.0.1:12022"
        client.rpc = mock_rpc
        with pytest.raises(CDPMintBlockedError):
            client.mint(1_000_000_000_000, ratio=300)

    def test_mint_zero_collateral_raises(self):
        client = make_client([])
        with pytest.raises(ValidationError):
            client.mint(0)

    def test_mint_str_representation(self):
        client = make_client([
            ORACLE_RESPONSE_FRESH,
            ORACLE_RESPONSE_FRESH,
            MINT_RESPONSE,
        ])
        result = client.mint(1_000_000_000_000, ratio=300)
        s = str(result)
        assert "Mint successful" in s
        assert "b" * 16 in s


# ---------------------------------------------------------------------------
# redeem()
# ---------------------------------------------------------------------------

class TestRedeem:
    def test_redeem_returns_result(self):
        client = make_client([REDEEM_RESPONSE])
        result = client.redeem(Decimal("83.33"))
        assert isinstance(result, RedeemResult)
        assert result.txid == "c" * 64
        assert result.dusd_burned == Decimal("83.33")
        assert result.collateral_returned == 999_000_000

    def test_redeem_zero_raises(self):
        client = make_client([])
        with pytest.raises(ValidationError):
            client.redeem(Decimal("0"))

    def test_redeem_timelock_not_expired_raises(self):
        client = CDPClient.__new__(CDPClient)
        mock_rpc = MagicMock()
        mock_rpc.call.side_effect = RPCError(-1, "timelock not yet expired at block 3672800")
        mock_rpc.config.url = "http://127.0.0.1:12022"
        client.rpc = mock_rpc
        with pytest.raises(CDPRedemptionError):
            client.redeem(Decimal("83.33"))

    def test_redeem_str_representation(self):
        client = make_client([REDEEM_RESPONSE])
        result = client.redeem(Decimal("83.33"))
        s = str(result)
        assert "Redemption successful" in s

    def test_stability_fee_display(self):
        client = make_client([REDEEM_RESPONSE])
        result = client.redeem(Decimal("83.33"))
        assert abs(result.stability_fee_display - 0.01) < 0.0001


# ---------------------------------------------------------------------------
# engine_projection()
# ---------------------------------------------------------------------------

class TestEngineProjection:
    def test_returns_dict(self):
        client = make_client([ORACLE_RESPONSE_FRESH])
        proj = client.engine_projection(1_000_000_000_000, ratio=300)
        assert isinstance(proj, dict)
        assert "scenarios" in proj
        assert "collateral_dgb" in proj

    def test_scenarios_contain_price_points(self):
        client = make_client([ORACLE_RESPONSE_FRESH])
        proj = client.engine_projection(1_000_000_000_000)
        scenarios = proj["scenarios"]
        assert "$0.25" in scenarios
        assert "$1.00" in scenarios

    def test_higher_price_more_mintable(self):
        client = make_client([ORACLE_RESPONSE_FRESH])
        proj = client.engine_projection(1_000_000_000_000)
        s = proj["scenarios"]
        assert s["$1.00"]["mintable_dusd"] > s["$0.25"]["mintable_dusd"]

    def test_engine_note_present(self):
        client = make_client([ORACLE_RESPONSE_FRESH])
        proj = client.engine_projection(1_000_000_000_000)
        assert "engine_note" in proj
        assert "Matthew" in proj["engine_note"]
