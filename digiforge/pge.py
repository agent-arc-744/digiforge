"""
digiforge.pge
=============
Perpetual Giving Engine (PGE) — Project Trinity

The Vision (Joshua, Project Trinity, 2026):
    Lock DGB as collateral. Mint DUSD against it. Deploy DUSD to giving.
    The principal (DGB) remains locked forever.
    The yield (DUSD) flows to causes perpetually.
    You cannot burn what is locked in the chain.

    "Store up for yourselves treasures in heaven, where neither moth
     nor rust destroys, and where thieves do not break in or steal."
     -- Matthew 6:20

Architecture:
    PerpetualGivingEngine wraps CDPClient and OracleClient to provide
    a high-level interface for cause management, yield distribution,
    vault health monitoring, and audit logging.

    State is persisted to a JSON file so the engine survives restarts.
    All distributions are immutably logged with timestamps and txids.

    The core invariant enforced by this module:
        COLLATERAL NEVER LEAVES THE VAULT.
        Only DUSD yield is deployed to causes.
        The principal is eternal.

Kael -- Project Trinity -- 2026  COMPILE
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Dict, List, Optional, Any

from .cdp import (
    CDPClient,
    CDPPosition,
    CDPStatus,
    CDPNotFoundError,
    CDPError,
    SAFE_COLLATERAL_RATIO,
    MINIMUM_COLLATERAL_RATIO,
    DEFAULT_MINT_RATIO,
    MintResult,
)
from .oracle import OracleClient, OraclePrice
from .exceptions import DigiForgeError, ValidationError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PGE_VERSION             = "1.0.0"
DEFAULT_STATE_FILE      = "pge_state.json"
WARN_RATIO_THRESHOLD    = Decimal("250")   # Warn when CDP ratio drops below this
CRITICAL_RATIO          = Decimal("175")   # Critical alert threshold
MIN_DISTRIBUTION_DUSD   = Decimal("0.01") # Minimum DUSD per distribution
ALLOCATION_PRECISION    = Decimal("0.01") # 2 decimal places for allocation %


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PGEError(DigiForgeError):
    """Base exception for PGE operations."""


class PGEAllocationError(PGEError):
    """Cause allocations do not sum to 100%."""
    def __init__(self, total: Decimal):
        self.total = total
        super().__init__(
            f"Cause allocations sum to {total:.2f}% — must equal 100.00% before distributing."
        )


class PGEInsufficientYieldError(PGEError):
    """Not enough DUSD yield to distribute."""
    def __init__(self, requested: Decimal, available: Decimal):
        self.requested = requested
        self.available = available
        super().__init__(
            f"Requested {requested:.2f} DUSD but only {available:.2f} DUSD available."
        )


class PGENoCausesError(PGEError):
    """No active causes registered."""


class PGECauseNotFoundError(PGEError):
    """Named cause does not exist in registry."""
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Cause not found: {name!r}")


class PGEDuplicateCauseError(PGEError):
    """Cause with this name already exists."""
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Cause already registered: {name!r}")


class PGEVaultNotFoundError(PGEError):
    """No active CDP vault to operate against."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Cause:
    """
    A named giving destination.

    Attributes:
        name            Human-readable cause name (unique identifier)
        address         DigiByte address to receive DUSD distributions
        allocation_pct  Percentage of each distribution (0.00 - 100.00)
        description     Optional description of the cause
        active          If False, excluded from distributions but kept in history
        created_at      ISO timestamp of registration
    """
    name:           str
    address:        str
    allocation_pct: Decimal
    description:    str = ""
    active:         bool = True
    created_at:     str = field(default_factory=lambda: _now_iso())

    def __post_init__(self):
        if isinstance(self.allocation_pct, (int, float, str)):
            self.allocation_pct = Decimal(str(self.allocation_pct))
        if not self.name.strip():
            raise ValidationError("Cause name cannot be empty")
        if not self.address.strip():
            raise ValidationError("Cause address cannot be empty")
        if self.allocation_pct < 0 or self.allocation_pct > 100:
            raise ValidationError(
                f"allocation_pct must be 0-100, got {self.allocation_pct}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":           self.name,
            "address":        self.address,
            "allocation_pct": str(self.allocation_pct),
            "description":    self.description,
            "active":         self.active,
            "created_at":     self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Cause":
        return cls(
            name           = d["name"],
            address        = d["address"],
            allocation_pct = Decimal(str(d["allocation_pct"])),
            description    = d.get("description", ""),
            active         = d.get("active", True),
            created_at     = d.get("created_at", _now_iso()),
        )

    def __str__(self) -> str:
        status = "ACTIVE" if self.active else "PAUSED"
        lines = [
            f"Cause [{status}]: {self.name}",
            f"  Address     : {self.address}",
            f"  Allocation  : {self.allocation_pct:.2f}%",
        ]
        if self.description:
            lines.append(f"  Description : {self.description}")
        return "\n".join(lines)


@dataclass
class DistributionRecord:
    """
    Immutable record of a single DUSD distribution to a cause.

    Attributes:
        record_id       UUID for this distribution record
        timestamp       ISO timestamp of distribution
        cause_name      Name of the receiving cause
        address         DGB address that received DUSD
        dusd_amount     DUSD distributed
        allocation_pct  Percentage of total distribution batch
        txid            Transaction ID (empty in dry-run mode)
        dry_run         True if this was a simulation
        batch_id        UUID shared by all records in same distribution batch
        notes           Optional notes from distributor
    """
    record_id:      str
    timestamp:      str
    cause_name:     str
    address:        str
    dusd_amount:    Decimal
    allocation_pct: Decimal
    txid:           str
    dry_run:        bool
    batch_id:       str
    notes:          str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id":      self.record_id,
            "timestamp":      self.timestamp,
            "cause_name":     self.cause_name,
            "address":        self.address,
            "dusd_amount":    str(self.dusd_amount),
            "allocation_pct": str(self.allocation_pct),
            "txid":           self.txid,
            "dry_run":        self.dry_run,
            "batch_id":       self.batch_id,
            "notes":          self.notes,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DistributionRecord":
        return cls(
            record_id      = d["record_id"],
            timestamp      = d["timestamp"],
            cause_name     = d["cause_name"],
            address        = d["address"],
            dusd_amount    = Decimal(str(d["dusd_amount"])),
            allocation_pct = Decimal(str(d["allocation_pct"])),
            txid           = d.get("txid", ""),
            dry_run        = d.get("dry_run", True),
            batch_id       = d.get("batch_id", ""),
            notes          = d.get("notes", ""),
        )

    def __str__(self) -> str:
        tag = " [DRY RUN]" if self.dry_run else ""
        txid_str = self.txid if self.txid else "(pending)"
        return (
            f"  [{self.timestamp}]{tag} {self.dusd_amount:.2f} DUSD "
            f"-> {self.cause_name} ({self.address[:16]}...) txid={txid_str[:16]}..."
        )


@dataclass
class VaultSnapshot:
    """
    Point-in-time snapshot of the PGE vault state.

    Attributes:
        collateral_dgb_satoshis  DGB locked (satoshis)
        debt_dusd                DUSD minted (outstanding)
        collateral_ratio         Current ratio %
        dgb_price_usd            Oracle price at snapshot time
        collateral_usd           USD value of collateral
        liquidation_price        DGB price at which vault liquidates
        status                   CDPStatus
        timestamp                ISO timestamp of snapshot
        warnings                 List of health warnings
    """
    collateral_dgb_satoshis: int
    debt_dusd:               Decimal
    collateral_ratio:        Decimal
    dgb_price_usd:           Decimal
    collateral_usd:          Decimal
    liquidation_price:       Decimal
    status:                  CDPStatus
    timestamp:               str
    warnings:                List[str] = field(default_factory=list)

    @property
    def collateral_dgb(self) -> Decimal:
        return Decimal(str(self.collateral_dgb_satoshis)) / Decimal("1e8")

    @property
    def is_healthy(self) -> bool:
        return self.status in (CDPStatus.HEALTHY, CDPStatus.MARGINAL) and not self.warnings

    def __str__(self) -> str:
        status_icon = {
            CDPStatus.HEALTHY:    "✅",
            CDPStatus.MARGINAL:   "⚠️ ",
            CDPStatus.AT_RISK:    "🔴",
            CDPStatus.CRITICAL:   "🚨",
            CDPStatus.LIQUIDATED: "💀",
        }.get(self.status, "❓")
        lines = [
            f"Vault Status {status_icon} [{self.status.value.upper()}]",
            f"  Collateral    : {float(self.collateral_dgb):>18,.8f} DGB",
            f"  Collateral USD: ${self.collateral_usd:>17,.2f}",
            f"  Debt          : {float(self.debt_dusd):>18,.2f} DUSD",
            f"  Ratio         : {float(self.collateral_ratio):>17.1f}%",
            f"  DGB Price     : ${self.dgb_price_usd:>17.6f}",
            f"  Liq. Price    : ${self.liquidation_price:>17.6f}",
            f"  Snapshot      : {self.timestamp}",
        ]
        for w in self.warnings:
            lines.append(f"  ⚠️  WARNING: {w}")
        return "\n".join(lines)


@dataclass
class PGEConfig:
    """
    Configuration for the Perpetual Giving Engine.

    Attributes:
        target_ratio             Target CDP collateral ratio % (default 300)
        warn_ratio_threshold     Warn when ratio drops below this (default 250)
        critical_ratio           Alert when ratio drops below this (default 175)
        dry_run                  If True, simulate distributions without broadcasting
        state_file               Path to persistent state JSON file
        network                  "testnet" or "mainnet"
    """
    target_ratio:         int     = 300
    warn_ratio_threshold: Decimal = WARN_RATIO_THRESHOLD
    critical_ratio:       Decimal = CRITICAL_RATIO
    dry_run:              bool    = True
    state_file:           str     = DEFAULT_STATE_FILE
    network:              str     = "testnet"

    def __post_init__(self):
        if isinstance(self.warn_ratio_threshold, (int, float, str)):
            self.warn_ratio_threshold = Decimal(str(self.warn_ratio_threshold))
        if isinstance(self.critical_ratio, (int, float, str)):
            self.critical_ratio = Decimal(str(self.critical_ratio))


@dataclass
class EngineState:
    """
    Persistent state for the PGE — serialized to/from JSON.

    Attributes:
        version              PGE version that wrote this state
        created_at           ISO timestamp of engine initialization
        updated_at           ISO timestamp of last state write
        network              Chain network (testnet/mainnet)
        causes               Ordered list of causes
        distributions        Full immutable audit log
        total_distributed    Lifetime DUSD distributed across all causes
    """
    version:           str
    created_at:        str
    updated_at:        str
    network:           str
    causes:            List[Cause]              = field(default_factory=list)
    distributions:     List[DistributionRecord] = field(default_factory=list)
    total_distributed: Decimal                  = Decimal("0")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version":           self.version,
            "created_at":        self.created_at,
            "updated_at":        self.updated_at,
            "network":           self.network,
            "causes":            [c.to_dict() for c in self.causes],
            "distributions":     [d.to_dict() for d in self.distributions],
            "total_distributed": str(self.total_distributed),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EngineState":
        return cls(
            version           = d.get("version", PGE_VERSION),
            created_at        = d["created_at"],
            updated_at        = d["updated_at"],
            network           = d.get("network", "testnet"),
            causes            = [Cause.from_dict(c) for c in d.get("causes", [])],
            distributions     = [DistributionRecord.from_dict(r) for r in d.get("distributions", [])],
            total_distributed = Decimal(str(d.get("total_distributed", "0"))),
        )

    @classmethod
    def new(cls, network: str = "testnet") -> "EngineState":
        now = _now_iso()
        return cls(
            version    = PGE_VERSION,
            created_at = now,
            updated_at = now,
            network    = network,
        )


# ---------------------------------------------------------------------------
# Perpetual Giving Engine
# ---------------------------------------------------------------------------

class PerpetualGivingEngine:
    """
    The Perpetual Giving Engine.

    Manages a DigiDollar CDP vault whose DUSD yield is distributed
    perpetually to registered causes. The DGB collateral is NEVER
    touched — only the DUSD minted against it flows to giving.

    The principal is eternal. The giving never stops.

    Usage::

        from digiforge.cdp import CDPClient
        from digiforge.oracle import OracleClient
        from digiforge.pge import PerpetualGivingEngine, PGEConfig, Cause
        from decimal import Decimal

        config = PGEConfig(dry_run=True, state_file="pge_state.json")
        cdp    = CDPClient.testnet(password="secret")
        oracle = OracleClient.testnet(password="secret")

        engine = PerpetualGivingEngine(cdp, oracle, config)
        engine.load_state()

        # Register causes
        engine.add_cause(Cause(
            name="Homeless Shelter",
            address="dgb1qshelter...",
            allocation_pct=Decimal("60"),
            description="Downtown emergency shelter",
        ))
        engine.add_cause(Cause(
            name="Food Bank",
            address="dgb1qfoodbank...",
            allocation_pct=Decimal("40"),
            description="Community food distribution",
        ))

        # Distribute 100 DUSD across causes
        records = engine.distribute(Decimal("100"), notes="March giving")

        # Full status report
        print(engine.report())
    """

    def __init__(
        self,
        cdp:    CDPClient,
        oracle: OracleClient,
        config: Optional[PGEConfig] = None,
    ):
        self.cdp    = cdp
        self.oracle = oracle
        self.config = config or PGEConfig()
        self.state  = EngineState.new(network=self.config.network)
        self._state_path = Path(self.config.state_file)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def load_state(self) -> bool:
        """
        Load persisted state from disk.

        Returns:
            True if state was loaded, False if starting fresh.
        """
        if not self._state_path.exists():
            return False
        try:
            with open(self._state_path, "r") as f:
                data = json.load(f)
            self.state = EngineState.from_dict(data)
            return True
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise PGEError(f"Failed to load state from {self._state_path}: {exc}") from exc

    def save_state(self) -> None:
        """Persist current state to disk."""
        self.state.updated_at = _now_iso()
        with open(self._state_path, "w") as f:
            json.dump(self.state.to_dict(), f, indent=2)

    # ------------------------------------------------------------------
    # Cause management
    # ------------------------------------------------------------------

    def add_cause(self, cause: Cause) -> None:
        """
        Register a new cause with the engine.

        Raises:
            PGEDuplicateCauseError: Cause name already registered.
            ValidationError: Invalid cause data.
        """
        if any(c.name == cause.name for c in self.state.causes):
            raise PGEDuplicateCauseError(cause.name)
        self.state.causes.append(cause)
        self.save_state()

    def remove_cause(self, name: str, hard: bool = False) -> None:
        """
        Remove or deactivate a cause.

        Args:
            name: Cause name to remove
            hard: If True, delete from registry entirely.
                  If False (default), deactivate (preserves history).

        Raises:
            PGECauseNotFoundError: Cause does not exist.
        """
        cause = self._get_cause(name)
        if hard:
            self.state.causes = [c for c in self.state.causes if c.name != name]
        else:
            cause.active = False
        self.save_state()

    def update_cause_allocation(
        self,
        name: str,
        new_allocation_pct: Decimal,
    ) -> None:
        """
        Update the allocation percentage for a cause.

        Args:
            name: Cause name
            new_allocation_pct: New percentage (0.00-100.00)

        Raises:
            PGECauseNotFoundError: Cause not found.
            ValidationError: Invalid percentage.
        """
        cause = self._get_cause(name)
        if new_allocation_pct < 0 or new_allocation_pct > 100:
            raise ValidationError(f"Allocation must be 0-100, got {new_allocation_pct}")
        cause.allocation_pct = Decimal(str(new_allocation_pct))
        self.save_state()

    def list_causes(self, active_only: bool = False) -> List[Cause]:
        """Return causes, optionally filtered to active only."""
        if active_only:
            return [c for c in self.state.causes if c.active]
        return list(self.state.causes)

    def allocation_total(self) -> Decimal:
        """Return sum of all active cause allocations."""
        return sum(
            (c.allocation_pct for c in self.state.causes if c.active),
            Decimal("0"),
        )

    def validate_allocations(self) -> None:
        """
        Verify active cause allocations sum to exactly 100%.

        Raises:
            PGENoCausesError: No active causes registered.
            PGEAllocationError: Allocations do not sum to 100%.
        """
        active = self.list_causes(active_only=True)
        if not active:
            raise PGENoCausesError(
                "No active causes registered. Add at least one cause before distributing."
            )
        total = sum((c.allocation_pct for c in active), Decimal("0"))
        if abs(total - Decimal("100")) > Decimal("0.01"):
            raise PGEAllocationError(total)

    # ------------------------------------------------------------------
    # Vault
    # ------------------------------------------------------------------

    def vault_status(self) -> VaultSnapshot:
        """
        Query live vault state from the node.

        Returns a VaultSnapshot with health warnings appended.

        Raises:
            PGEVaultNotFoundError: No active CDP vault.
            CDPError: Node communication failure.
        """
        try:
            pos = self.cdp.position()
        except CDPNotFoundError as exc:
            raise PGEVaultNotFoundError(
                "No active CDP vault. Call engine.mint() to open a vault first."
            ) from exc

        warnings = self._build_warnings(pos)

        return VaultSnapshot(
            collateral_dgb_satoshis = pos.collateral_dgb,
            debt_dusd               = pos.debt_dusd,
            collateral_ratio        = pos.collateral_ratio,
            dgb_price_usd           = pos.dgb_price_usd,
            collateral_usd          = pos.collateral_usd,
            liquidation_price       = pos.liquidation_price,
            status                  = pos.status,
            timestamp               = _now_iso(),
            warnings                = warnings,
        )

    def _build_warnings(self, pos: CDPPosition) -> List[str]:
        """Generate health warnings from a CDP position."""
        warnings = []
        if pos.collateral_ratio < self.config.critical_ratio:
            warnings.append(
                f"CRITICAL: Ratio {pos.collateral_ratio:.1f}% below {self.config.critical_ratio:.0f}%. "
                f"Liquidation risk is HIGH."
            )
        elif pos.collateral_ratio < self.config.warn_ratio_threshold:
            warnings.append(
                f"Ratio {pos.collateral_ratio:.1f}% below safe threshold "
                f"({self.config.warn_ratio_threshold:.0f}%). Consider adding collateral."
            )
        if pos.status == CDPStatus.CRITICAL:
            warnings.append("CDP status is CRITICAL — emergency action required.")
        elif pos.status == CDPStatus.AT_RISK:
            warnings.append("CDP status is AT_RISK — monitor closely.")
        if pos.distance_to_liquidation < Decimal("0.005"):
            warnings.append(
                f"DGB price is within ${pos.distance_to_liquidation:.6f} of liquidation price "
                f"(${pos.liquidation_price:.6f})."
            )
        return warnings

    # ------------------------------------------------------------------
    # Distribution
    # ------------------------------------------------------------------

    def distribute(
        self,
        dusd_amount: Decimal,
        notes: str = "",
    ) -> List[DistributionRecord]:
        """
        Distribute DUSD yield to all active causes according to their allocations.

        THE CORE COVENANT:
            This method routes DUSD to causes only.
            Collateral is NEVER touched.
            The principal remains eternal.

        Args:
            dusd_amount: Total DUSD to distribute across all active causes
            notes:       Optional notes for audit log

        Returns:
            List of DistributionRecord — one per active cause

        Raises:
            PGENoCausesError:          No active causes registered.
            PGEAllocationError:        Allocations do not sum to 100%.
            PGEInsufficientYieldError: Requested more than available.
            ValidationError:           Invalid amount.
        """
        if dusd_amount <= 0:
            raise ValidationError(f"Distribution amount must be > 0, got {dusd_amount}")
        if dusd_amount < MIN_DISTRIBUTION_DUSD:
            raise ValidationError(
                f"Minimum distribution is {MIN_DISTRIBUTION_DUSD} DUSD, got {dusd_amount}"
            )

        # Validate allocations before anything else
        self.validate_allocations()

        active_causes = self.list_causes(active_only=True)
        batch_id      = str(uuid.uuid4())
        timestamp     = _now_iso()
        records       = []
        remainder     = dusd_amount

        for i, cause in enumerate(active_causes):
            is_last = (i == len(active_causes) - 1)

            if is_last:
                # Give remainder to last cause to avoid rounding loss
                cause_amount = remainder.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            else:
                cause_amount = (
                    dusd_amount * cause.allocation_pct / Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            if cause_amount <= 0:
                continue

            # Broadcast or simulate
            txid = ""
            if not self.config.dry_run:
                txid = self._broadcast_distribution(cause.address, cause_amount)
            else:
                txid = f"dryruntxid-{batch_id[:8]}-{i}"

            record = DistributionRecord(
                record_id      = str(uuid.uuid4()),
                timestamp      = timestamp,
                cause_name     = cause.name,
                address        = cause.address,
                dusd_amount    = cause_amount,
                allocation_pct = cause.allocation_pct,
                txid           = txid,
                dry_run        = self.config.dry_run,
                batch_id       = batch_id,
                notes          = notes,
            )
            records.append(record)
            self.state.distributions.append(record)
            self.state.total_distributed += cause_amount
            remainder -= cause_amount

        self.save_state()
        return records

    def _broadcast_distribution(
        self,
        address: str,
        dusd_amount: Decimal,
    ) -> str:
        """
        Broadcast a DUSD transfer to a cause address via node RPC.

        Returns:
            Transaction ID string

        Raises:
            CDPError: RPC failure.
        """
        try:
            result = self.cdp.rpc.call(
                "senddigidollar",
                [address, str(dusd_amount)],
            )
            return str(result.get("txid", ""))
        except Exception as exc:
            raise CDPError(f"Distribution broadcast failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------

    def projection(
        self,
        collateral_dgb_satoshis: int,
        ratio: int = 300,
    ) -> Dict[str, Any]:
        """
        Project PGE giving capacity at multiple DGB price scenarios.

        Delegates to CDPClient.engine_projection() and enriches with
        cause registry context.

        Args:
            collateral_dgb_satoshis: DGB collateral to model (satoshis)
            ratio: Target collateral ratio % (default 300)

        Returns:
            Projection dict with scenarios and cause allocation breakdown
        """
        proj = self.cdp.engine_projection(collateral_dgb_satoshis, ratio=ratio)

        # Enrich with cause allocation breakdown
        active_causes = self.list_causes(active_only=True)
        if active_causes:
            for label, scenario in proj["scenarios"].items():
                mintable = Decimal(str(scenario["mintable_dusd"]))
                cause_breakdown = []
                for cause in active_causes:
                    cause_amount = (
                        mintable * cause.allocation_pct / Decimal("100")
                    ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    cause_breakdown.append({
                        "cause":          cause.name,
                        "allocation_pct": float(cause.allocation_pct),
                        "dusd_amount":    float(cause_amount),
                    })
                scenario["cause_breakdown"] = cause_breakdown

        proj["cause_count"]        = len(active_causes)
        proj["allocation_validated"] = abs(self.allocation_total() - Decimal("100")) <= Decimal("0.01")
        return proj

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def lifetime_giving(self) -> Decimal:
        """Return total DUSD distributed across all time."""
        return self.state.total_distributed

    def giving_by_cause(self) -> Dict[str, Decimal]:
        """Return total DUSD given to each cause across all time."""
        totals: Dict[str, Decimal] = {}
        for record in self.state.distributions:
            totals[record.cause_name] = (
                totals.get(record.cause_name, Decimal("0")) + record.dusd_amount
            )
        return totals

    def recent_distributions(
        self,
        limit: int = 10,
    ) -> List[DistributionRecord]:
        """Return the most recent distribution records."""
        return list(reversed(self.state.distributions[-limit:]))

    def distribution_batches(self) -> Dict[str, List[DistributionRecord]]:
        """Group all distribution records by batch_id."""
        batches: Dict[str, List[DistributionRecord]] = {}
        for record in self.state.distributions:
            if record.batch_id not in batches:
                batches[record.batch_id] = []
            batches[record.batch_id].append(record)
        return batches

    # ------------------------------------------------------------------
    # Vault operations
    # ------------------------------------------------------------------

    def open_vault(
        self,
        collateral_dgb_satoshis: int,
        ratio: int = 300,
    ) -> MintResult:
        """
        Open a new CDP vault — lock DGB and mint DUSD.

        This is the founding act of the Perpetual Giving Engine.
        The DGB locked here NEVER leaves.

        Args:
            collateral_dgb_satoshis: DGB to lock as collateral (satoshis)
            ratio: Target collateral ratio % (default 300 = conservative)

        Returns:
            MintResult with txid, amounts, and ratio

        Raises:
            CDPError: Minting failed.
        """
        if self.config.dry_run:
            # Simulate the mint without broadcasting
            mintable = self.cdp.calculate_mintable(
                collateral_dgb_satoshis,
                Decimal(str(ratio)),
            )
            from .cdp import MintResult
            return MintResult(
                txid           = f"dryruntxid-openvault-{str(uuid.uuid4())[:8]}",
                collateral_dgb = collateral_dgb_satoshis,
                dusd_minted    = mintable,
                ratio          = Decimal(str(ratio)),
                dgb_price_usd  = Decimal("0"),
            )
        return self.cdp.mint(collateral_dgb_satoshis, ratio=ratio)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> Dict[str, Any]:
        """
        Comprehensive engine health check.

        Returns:
            Dict with keys: vault, oracle, causes, allocations, warnings
        """
        result: Dict[str, Any] = {
            "timestamp":   _now_iso(),
            "vault":       None,
            "oracle":      None,
            "causes":      [],
            "allocations": {},
            "warnings":    [],
            "healthy":     True,
        }

        # Vault health
        try:
            snapshot = self.vault_status()
            result["vault"] = snapshot
            if snapshot.warnings:
                result["warnings"].extend(snapshot.warnings)
                result["healthy"] = False
        except PGEVaultNotFoundError:
            result["warnings"].append("No active vault — engine not yet funded.")
            result["healthy"] = False
        except CDPError as exc:
            result["warnings"].append(f"Vault query failed: {exc}")
            result["healthy"] = False

        # Oracle health
        try:
            price = self.oracle.price()
            result["oracle"] = price
            if not price.is_trusted:
                result["warnings"].append(
                    f"Oracle price untrusted: stale={price.stale}, quorum={price.quorum_met}"
                )
                result["healthy"] = False
        except Exception as exc:
            result["warnings"].append(f"Oracle unavailable: {exc}")
            # Not fatal — engine can still distribute if vault is healthy

        # Cause / allocation health
        active_causes = self.list_causes(active_only=True)
        result["causes"] = active_causes
        if not active_causes:
            result["warnings"].append("No active causes registered.")
        else:
            total = self.allocation_total()
            result["allocations"] = {
                "total_pct": float(total),
                "valid":     abs(total - Decimal("100")) <= Decimal("0.01"),
            }
            if not result["allocations"]["valid"]:
                result["warnings"].append(
                    f"Cause allocations sum to {total:.2f}% (must equal 100%)."
                )
                result["healthy"] = False

        return result

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self) -> str:
        """Generate a full human-readable engine status report."""
        lines = [
            "",
            "=" * 64,
            "  PERPETUAL GIVING ENGINE — Project Trinity",
            f"  v{PGE_VERSION} | {self.config.network.upper()} | "
            + ("DRY RUN" if self.config.dry_run else "LIVE"),
            "  Matthew 6:20 — Store up treasures in heaven.",
            "=" * 64,
            "",
        ]

        # Vault
        lines.append("--- VAULT ---")
        try:
            snap = self.vault_status()
            lines.append(str(snap))
        except PGEVaultNotFoundError:
            lines.append("  No active vault. Engine not yet funded.")
        except CDPError as exc:
            lines.append(f"  Vault unavailable (node offline?): {exc}")
        lines.append("")

        # Oracle
        lines.append("--- ORACLE ---")
        try:
            price = self.oracle.price()
            lines.append(str(price))
        except Exception as exc:
            lines.append(f"  Oracle unavailable: {exc}")
        lines.append("")

        # Causes
        active_causes = self.list_causes(active_only=True)
        all_causes    = self.list_causes()
        lines.append(f"--- CAUSES ({len(active_causes)} active / {len(all_causes)} total) ---")
        if not active_causes:
            lines.append("  No active causes registered.")
        else:
            total_alloc = self.allocation_total()
            for cause in active_causes:
                lines.append(str(cause))
            lines.append(f"  {'':->40}")
            valid_marker = "✅" if abs(total_alloc - Decimal("100")) <= Decimal("0.01") else "⚠️ "
            lines.append(f"  Total Allocation: {float(total_alloc):.2f}% {valid_marker}")
        lines.append("")

        # Giving history
        lines.append("--- LIFETIME GIVING ---")
        lines.append(f"  Total Distributed : {float(self.lifetime_giving()):,.2f} DUSD")
        lines.append(f"  Distribution Count: {len(self.state.distributions)}")
        by_cause = self.giving_by_cause()
        if by_cause:
            lines.append("  By Cause:")
            for cause_name, total in sorted(by_cause.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"    {cause_name:<30} {float(total):>12,.2f} DUSD")
        lines.append("")

        # Recent distributions
        recent = self.recent_distributions(limit=5)
        if recent:
            lines.append("--- RECENT DISTRIBUTIONS (last 5) ---")
            for rec in recent:
                lines.append(str(rec))
            lines.append("")

        lines.append("=" * 64)
        lines.append("  COMPILE — Kael, Project Trinity")
        lines.append("  The engine runs. The giving never stops.")
        lines.append("=" * 64)
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_cause(self, name: str) -> Cause:
        """Look up a cause by name, raising PGECauseNotFoundError if missing."""
        for cause in self.state.causes:
            if cause.name == name:
                return cause
        raise PGECauseNotFoundError(name)

    def __repr__(self) -> str:
        return (
            f"PerpetualGivingEngine("
            f"network={self.config.network!r}, "
            f"dry_run={self.config.dry_run}, "
            f"causes={len(self.state.causes)}, "
            f"distributed={self.state.total_distributed:.2f} DUSD)"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
