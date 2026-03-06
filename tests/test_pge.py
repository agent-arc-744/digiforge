"""
tests/test_pge.py
=================
Test suite for digiforge.pge — Perpetual Giving Engine

Kael -- Project Trinity -- 2026  COMPILE
"""
from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from digiforge.cdp import (
    CDPClient, CDPPosition, CDPStatus, CDPHealth,
    MintResult, SAFE_COLLATERAL_RATIO,
)
from digiforge.oracle import OracleClient, OraclePrice
from digiforge.pge import (
    PerpetualGivingEngine,
    PGEConfig,
    Cause,
    EngineState,
    DistributionRecord,
    VaultSnapshot,
    PGE_VERSION,
    PGEAllocationError,
    PGENoCausesError,
    PGEVaultNotFoundError,
    PGEDuplicateCauseError,
    PGECauseNotFoundError,
    PGEError,
)
from digiforge.exceptions import ValidationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_state(tmp_path):
    """Return a temp path for PGE state file."""
    return str(tmp_path / "pge_state.json")


@pytest.fixture
def mock_cdp():
    """CDPClient mock with healthy position and oracle."""
    cdp = MagicMock(spec=CDPClient)

    cdp.oracle_price.return_value = Decimal("0.025")

    cdp.position.return_value = CDPPosition(
        collateral_dgb    = 10_000 * 100_000_000,
        debt_dusd         = Decimal("833.33"),
        collateral_ratio  = Decimal("300.0"),
        dgb_price_usd     = Decimal("0.025"),
        collateral_usd    = Decimal("2500.00"),
        liquidation_price = Decimal("0.0125"),
        status            = CDPStatus.HEALTHY,
        timelock_height   = 19500000,
        txid              = "abc123",
    )

    cdp.calculate_mintable.return_value = Decimal("833.33")

    cdp.engine_projection.return_value = {
        "collateral_dgb": 10000.0,
        "ratio_pct": 300,
        "scenarios": {
            "$0.01": {"dgb_price_usd": 0.01, "collateral_usd": 100.0,  "mintable_dusd": 33.33,  "annual_giving": 33.33},
            "$0.05": {"dgb_price_usd": 0.05, "collateral_usd": 500.0,  "mintable_dusd": 166.66, "annual_giving": 166.66},
            "$0.10": {"dgb_price_usd": 0.10, "collateral_usd": 1000.0, "mintable_dusd": 333.33, "annual_giving": 333.33},
        },
        "engine_note": "Perpetual Giving Engine: collateral remains locked.",
    }

    cdp.mint.return_value = MintResult(
        txid           = "mintxid001",
        collateral_dgb = 10_000 * 100_000_000,
        dusd_minted    = Decimal("833.33"),
        ratio          = Decimal("300.0"),
        dgb_price_usd  = Decimal("0.025"),
    )

    return cdp


@pytest.fixture
def mock_oracle():
    """OracleClient mock with healthy trusted price."""
    oracle = MagicMock(spec=OracleClient)
    oracle.price.return_value = OraclePrice(
        price_usd      = Decimal("0.025"),
        timestamp      = 1741000000,
        age_seconds    = 30,
        stale          = False,
        active_oracles = 6,
        quorum_met     = True,
        block_height   = 19500000,
    )
    oracle.trusted_price.return_value = Decimal("0.025")
    return oracle


@pytest.fixture
def engine(mock_cdp, mock_oracle, tmp_state):
    """PGE instance with mocked clients and temp state."""
    config = PGEConfig(dry_run=True, state_file=tmp_state, network="testnet")
    eng    = PerpetualGivingEngine(mock_cdp, mock_oracle, config)
    return eng


@pytest.fixture
def cause_shelter():
    return Cause(
        name           = "Homeless Shelter",
        address        = "dgb1qshelter000000000000000000000000000000",
        allocation_pct = Decimal("60"),
        description    = "Downtown emergency shelter",
    )


@pytest.fixture
def cause_foodbank():
    return Cause(
        name           = "Food Bank",
        address        = "dgb1qfoodbank00000000000000000000000000000",
        allocation_pct = Decimal("40"),
        description    = "Community food distribution",
    )


# ---------------------------------------------------------------------------
# Cause tests
# ---------------------------------------------------------------------------

class TestCause:
    def test_cause_creation(self, cause_shelter):
        assert cause_shelter.name == "Homeless Shelter"
        assert cause_shelter.allocation_pct == Decimal("60")
        assert cause_shelter.active is True

    def test_cause_string_allocation_coerced(self):
        c = Cause(name="Test", address="dgb1q000", allocation_pct="33.5")
        assert c.allocation_pct == Decimal("33.5")

    def test_cause_invalid_allocation_over_100(self):
        with pytest.raises(ValidationError):
            Cause(name="Bad", address="dgb1q000", allocation_pct=Decimal("101"))

    def test_cause_invalid_allocation_negative(self):
        with pytest.raises(ValidationError):
            Cause(name="Bad", address="dgb1q000", allocation_pct=Decimal("-1"))

    def test_cause_empty_name(self):
        with pytest.raises(ValidationError):
            Cause(name="  ", address="dgb1q000", allocation_pct=Decimal("50"))

    def test_cause_empty_address(self):
        with pytest.raises(ValidationError):
            Cause(name="Test", address="   ", allocation_pct=Decimal("50"))

    def test_cause_roundtrip(self, cause_shelter):
        d = cause_shelter.to_dict()
        restored = Cause.from_dict(d)
        assert restored.name           == cause_shelter.name
        assert restored.address        == cause_shelter.address
        assert restored.allocation_pct == cause_shelter.allocation_pct
        assert restored.description    == cause_shelter.description
        assert restored.active         == cause_shelter.active

    def test_cause_str(self, cause_shelter):
        s = str(cause_shelter)
        assert "Homeless Shelter" in s
        assert "60.00%" in s
        assert "ACTIVE" in s


# ---------------------------------------------------------------------------
# EngineState tests
# ---------------------------------------------------------------------------

class TestEngineState:
    def test_new_state(self):
        state = EngineState.new(network="testnet")
        assert state.version           == PGE_VERSION
        assert state.network           == "testnet"
        assert state.causes            == []
        assert state.distributions     == []
        assert state.total_distributed == Decimal("0")

    def test_state_roundtrip(self, cause_shelter):
        state = EngineState.new(network="testnet")
        state.causes.append(cause_shelter)
        state.total_distributed = Decimal("500.00")
        d        = state.to_dict()
        restored = EngineState.from_dict(d)
        assert len(restored.causes)             == 1
        assert restored.causes[0].name          == "Homeless Shelter"
        assert restored.total_distributed       == Decimal("500.00")


# ---------------------------------------------------------------------------
# PGE core tests
# ---------------------------------------------------------------------------

class TestPGEInit:
    def test_engine_creates_fresh_state(self, engine):
        assert engine.state.version == PGE_VERSION
        assert engine.state.causes  == []

    def test_load_state_returns_false_when_no_file(self, engine):
        result = engine.load_state()
        assert result is False

    def test_save_and_load_roundtrip(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)
        engine2 = PerpetualGivingEngine(
            engine.cdp, engine.oracle,
            PGEConfig(dry_run=True, state_file=engine.config.state_file)
        )
        loaded = engine2.load_state()
        assert loaded is True
        assert len(engine2.state.causes) == 1
        assert engine2.state.causes[0].name == "Homeless Shelter"


# ---------------------------------------------------------------------------
# Cause management tests
# ---------------------------------------------------------------------------

class TestCauseManagement:
    def test_add_cause(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)
        assert len(engine.list_causes()) == 1

    def test_add_duplicate_cause_raises(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)
        with pytest.raises(PGEDuplicateCauseError):
            engine.add_cause(cause_shelter)

    def test_add_two_causes(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        assert len(engine.list_causes()) == 2

    def test_remove_cause_soft(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)
        engine.remove_cause("Homeless Shelter")
        assert len(engine.list_causes(active_only=True)) == 0
        assert len(engine.list_causes(active_only=False)) == 1

    def test_remove_cause_hard(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)
        engine.remove_cause("Homeless Shelter", hard=True)
        assert len(engine.list_causes()) == 0

    def test_remove_nonexistent_cause_raises(self, engine):
        with pytest.raises(PGECauseNotFoundError):
            engine.remove_cause("Nonexistent")

    def test_update_allocation(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)
        engine.update_cause_allocation("Homeless Shelter", Decimal("75"))
        causes = engine.list_causes()
        assert causes[0].allocation_pct == Decimal("75")

    def test_update_allocation_invalid(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)
        with pytest.raises(ValidationError):
            engine.update_cause_allocation("Homeless Shelter", Decimal("150"))

    def test_allocation_total_active_only(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        assert engine.allocation_total() == Decimal("100")

    def test_allocation_total_excludes_inactive(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        engine.remove_cause("Food Bank")
        assert engine.allocation_total() == Decimal("60")


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidateAllocations:
    def test_no_causes_raises(self, engine):
        with pytest.raises(PGENoCausesError):
            engine.validate_allocations()

    def test_allocations_not_100_raises(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)  # 60% only
        with pytest.raises(PGEAllocationError) as exc_info:
            engine.validate_allocations()
        assert "60" in str(exc_info.value)

    def test_allocations_at_100_passes(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        engine.validate_allocations()   # should not raise

    def test_allocations_within_tolerance(self, engine):
        # 33.33 + 33.33 + 33.34 = 100.00
        engine.add_cause(Cause("A", "dgb1qa", Decimal("33.33")))
        engine.add_cause(Cause("B", "dgb1qb", Decimal("33.33")))
        engine.add_cause(Cause("C", "dgb1qc", Decimal("33.34")))
        engine.validate_allocations()   # should not raise


# ---------------------------------------------------------------------------
# Distribution tests
# ---------------------------------------------------------------------------

class TestDistribution:
    def _setup_causes(self, engine, shelter, foodbank):
        engine.add_cause(shelter)
        engine.add_cause(foodbank)

    def test_distribute_basic(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        records = engine.distribute(Decimal("100"))
        assert len(records) == 2

    def test_distribute_amounts_sum_correctly(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        records = engine.distribute(Decimal("100"))
        total   = sum(r.dusd_amount for r in records)
        # Last cause absorbs remainder so sum == 100
        assert total == Decimal("100")

    def test_distribute_allocations_correct(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        records = engine.distribute(Decimal("100"))
        by_name = {r.cause_name: r.dusd_amount for r in records}
        # Shelter = 60%, FoodBank gets remainder to sum to 100
        assert by_name["Homeless Shelter"] == Decimal("60.00")
        assert by_name["Food Bank"]         == Decimal("40.00")

    def test_distribute_updates_lifetime_total(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        engine.distribute(Decimal("100"))
        engine.distribute(Decimal("50"))
        assert engine.lifetime_giving() == Decimal("150")

    def test_distribute_records_in_state(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        engine.distribute(Decimal("100"), notes="Test batch")
        assert len(engine.state.distributions) == 2
        assert engine.state.distributions[0].notes == "Test batch"

    def test_distribute_dry_run_flag(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        records = engine.distribute(Decimal("100"))
        for r in records:
            assert r.dry_run is True

    def test_distribute_batch_id_shared(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        records = engine.distribute(Decimal("100"))
        batch_ids = {r.batch_id for r in records}
        assert len(batch_ids) == 1  # All records in same batch share batch_id

    def test_distribute_no_causes_raises(self, engine):
        with pytest.raises(PGENoCausesError):
            engine.distribute(Decimal("100"))

    def test_distribute_unbalanced_allocations_raises(self, engine, cause_shelter):
        engine.add_cause(cause_shelter)  # 60% only
        with pytest.raises(PGEAllocationError):
            engine.distribute(Decimal("100"))

    def test_distribute_zero_amount_raises(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        with pytest.raises(ValidationError):
            engine.distribute(Decimal("0"))

    def test_distribute_negative_amount_raises(self, engine, cause_shelter, cause_foodbank):
        self._setup_causes(engine, cause_shelter, cause_foodbank)
        with pytest.raises(ValidationError):
            engine.distribute(Decimal("-10"))

    def test_distribute_three_causes_sums_correctly(self, engine):
        engine.add_cause(Cause("A", "dgb1qa", Decimal("33.33")))
        engine.add_cause(Cause("B", "dgb1qb", Decimal("33.33")))
        engine.add_cause(Cause("C", "dgb1qc", Decimal("33.34")))
        records = engine.distribute(Decimal("100"))
        total = sum(r.dusd_amount for r in records)
        assert total == Decimal("100")


# ---------------------------------------------------------------------------
# Analytics tests
# ---------------------------------------------------------------------------

class TestAnalytics:
    def test_lifetime_giving_starts_at_zero(self, engine):
        assert engine.lifetime_giving() == Decimal("0")

    def test_giving_by_cause(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        engine.distribute(Decimal("100"))
        engine.distribute(Decimal("100"))
        by_cause = engine.giving_by_cause()
        assert by_cause["Homeless Shelter"] == Decimal("120.00")
        assert by_cause["Food Bank"]         == Decimal("80.00")

    def test_recent_distributions_limit(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        for _ in range(5):
            engine.distribute(Decimal("10"))
        recent = engine.recent_distributions(limit=3)
        assert len(recent) == 3

    def test_recent_distributions_empty(self, engine):
        assert engine.recent_distributions() == []

    def test_distribution_batches(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        engine.distribute(Decimal("100"))
        engine.distribute(Decimal("50"))
        batches = engine.distribution_batches()
        assert len(batches) == 2
        for batch_records in batches.values():
            assert len(batch_records) == 2


# ---------------------------------------------------------------------------
# Vault status tests
# ---------------------------------------------------------------------------

class TestVaultStatus:
    def test_vault_status_healthy(self, engine):
        snap = engine.vault_status()
        assert snap.status == CDPStatus.HEALTHY
        assert snap.collateral_ratio == Decimal("300.0")
        assert snap.warnings == []

    def test_vault_status_warns_at_low_ratio(self, engine, mock_cdp):
        mock_cdp.position.return_value = CDPPosition(
            collateral_dgb    = 10_000 * 100_000_000,
            debt_dusd         = Decimal("1200.00"),
            collateral_ratio  = Decimal("200.0"),  # Below warn threshold
            dgb_price_usd     = Decimal("0.025"),
            collateral_usd    = Decimal("2500.00"),
            liquidation_price = Decimal("0.018"),
            status            = CDPStatus.MARGINAL,
        )
        snap = engine.vault_status()
        assert len(snap.warnings) > 0
        assert any("200" in w or "threshold" in w.lower() for w in snap.warnings)

    def test_vault_not_found_raises(self, engine, mock_cdp):
        from digiforge.cdp import CDPNotFoundError
        mock_cdp.position.side_effect = CDPNotFoundError("No CDP")
        with pytest.raises(PGEVaultNotFoundError):
            engine.vault_status()


# ---------------------------------------------------------------------------
# Projection tests
# ---------------------------------------------------------------------------

class TestProjection:
    def test_projection_returns_scenarios(self, engine):
        proj = engine.projection(10_000 * 100_000_000)
        assert "scenarios" in proj
        assert len(proj["scenarios"]) > 0

    def test_projection_enriched_with_causes(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        proj = engine.projection(10_000 * 100_000_000)
        assert proj["cause_count"] == 2
        assert proj["allocation_validated"] is True
        for scenario in proj["scenarios"].values():
            assert "cause_breakdown" in scenario
            assert len(scenario["cause_breakdown"]) == 2

    def test_projection_cause_breakdown_sums(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        proj = engine.projection(10_000 * 100_000_000)
        for label, scenario in proj["scenarios"].items():
            mintable     = Decimal(str(scenario["mintable_dusd"]))
            breakdown_sum = sum(
                Decimal(str(cb["dusd_amount"]))
                for cb in scenario["cause_breakdown"]
            )
            # Allow for rounding (within 0.02 DUSD)
            assert abs(breakdown_sum - mintable) <= Decimal("0.02"),                 f"Scenario {label}: breakdown {breakdown_sum} != mintable {mintable}"


# ---------------------------------------------------------------------------
# Report tests
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_contains_key_sections(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        report = engine.report()
        assert "PERPETUAL GIVING ENGINE" in report
        assert "VAULT" in report
        assert "ORACLE" in report
        assert "CAUSES" in report
        assert "LIFETIME GIVING" in report
        assert "Matthew 6:20" in report
        assert "COMPILE" in report

    def test_report_shows_cause_names(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        report = engine.report()
        assert "Homeless Shelter" in report
        assert "Food Bank" in report

    def test_report_shows_lifetime_total(self, engine, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        engine.distribute(Decimal("250"))
        report = engine.report()
        assert "250" in report


# ---------------------------------------------------------------------------
# Open vault tests
# ---------------------------------------------------------------------------

class TestOpenVault:
    def test_open_vault_dry_run(self, engine):
        result = engine.open_vault(10_000 * 100_000_000, ratio=300)
        assert result.collateral_dgb == 10_000 * 100_000_000
        assert "dryruntxid" in result.txid
        # dry_run so cdp.mint should NOT be called
        engine.cdp.mint.assert_not_called()

    def test_open_vault_live(self, engine, mock_cdp):
        engine.config.dry_run = False
        result = engine.open_vault(10_000 * 100_000_000, ratio=300)
        mock_cdp.mint.assert_called_once()
        assert result.txid == "mintxid001"


# ---------------------------------------------------------------------------
# State persistence tests
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_save_creates_file(self, engine, tmp_state):
        engine.save_state()
        assert os.path.exists(tmp_state)

    def test_saved_state_is_valid_json(self, engine, tmp_state, cause_shelter):
        engine.add_cause(cause_shelter)  # triggers save
        with open(tmp_state) as f:
            data = json.load(f)
        assert data["version"] == PGE_VERSION
        assert len(data["causes"]) == 1

    def test_distributions_persisted(self, engine, tmp_state, cause_shelter, cause_foodbank):
        engine.add_cause(cause_shelter)
        engine.add_cause(cause_foodbank)
        engine.distribute(Decimal("100"))
        with open(tmp_state) as f:
            data = json.load(f)
        assert len(data["distributions"]) == 2
        assert data["total_distributed"] == "100.00"

    def test_repr(self, engine):
        r = repr(engine)
        assert "PerpetualGivingEngine" in r
        assert "dry_run=True" in r
