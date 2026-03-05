"""
digiforge.cdp
=============
CDP (Collateralized Debt Position) client for DigiDollar (DUSD) on DigiByte Core v9.26+.

Wraps the DigiDollar RPC methods introduced in DGB Core v9.26:
    mintdigidollar       -- lock DGB collateral, mint DUSD
    redeemdigidollar     -- burn DUSD, unlock collateral
    getcollateralratio   -- current CDP health
    getcdpinfo           -- full position detail
    getcollateralbalance -- locked DGB amount
    getcdpstats          -- system-wide supply metrics
    getoracleprice       -- current oracle DGB/USD price

Architecture note (from DigiDollar audit, Kael 2026-02-25):
    Collateral outputs use a NUMS Taproot internal key (no known discrete log),
    forcing ALL spends through the CLTV script-path. This prevents key-path
    collateral theft -- the A-01 CVE-grade vulnerability fixed in rc15.

Kael -- Project Trinity -- 2026
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Any, Dict, Optional

from .exceptions import (
    DigiForgeError,
    InsufficientFundsError,
    RPCError,
    ValidationError,
)
from .rpc import DigiByteRPC, NodeConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAFE_COLLATERAL_RATIO    = Decimal("300")   # 300% -- conservative safe minimum
MINIMUM_COLLATERAL_RATIO = Decimal("150")   # Below this = Emergency Redemption Risk
DEFAULT_MINT_RATIO       = Decimal("300")   # Default ratio used when minting
DUSD_DECIMALS            = 2                # DigiDollar has 2 decimal places


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CDPError(DigiForgeError):
    """Base exception for CDP operations."""


class CDPRatioTooLowError(CDPError):
    """Mint would result in collateral ratio below safe threshold."""
    def __init__(self, actual: Decimal, minimum: Decimal):
        self.actual = actual
        self.minimum = minimum
        super().__init__(
            f"Collateral ratio {actual:.1f}% is below minimum {minimum:.1f}%. "
            f"Add more collateral or reduce DUSD amount."
        )


class CDPMintBlockedError(CDPError):
    """Minting blocked due to Emergency Redemption Ratio conditions."""


class CDPRedemptionError(CDPError):
    """Redemption failed -- timelock not expired or insufficient DUSD balance."""


class CDPNotFoundError(CDPError):
    """No active CDP found for this wallet."""


class OracleStaleError(CDPError):
    """Oracle price data is stale -- cannot safely perform CDP operation."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class CDPStatus(Enum):
    """Current status of a CDP position."""
    HEALTHY    = "healthy"     # ratio >= 300%
    MARGINAL   = "marginal"    # 200% <= ratio < 300%
    AT_RISK    = "at_risk"     # 150% <= ratio < 200%
    CRITICAL   = "critical"    # ratio < 150% -- near ERR threshold
    LIQUIDATED = "liquidated"  # position was liquidated


@dataclass
class CDPPosition:
    """
    Snapshot of a CDP (Collateralized Debt Position).

    Attributes:
        collateral_dgb     DGB locked as collateral (satoshis)
        debt_dusd          DUSD outstanding (2 decimal places)
        collateral_ratio   Current ratio as percentage (e.g. 350.0 = 350%)
        dgb_price_usd      Oracle price used for ratio calculation
        collateral_usd     USD value of locked collateral
        liquidation_price  DGB price at which position would be liquidated
        status             CDPStatus enum
        timelock_height    Block height when collateral can be unlocked
        txid               Collateral lock transaction ID
    """
    collateral_dgb:    int
    debt_dusd:         Decimal
    collateral_ratio:  Decimal
    dgb_price_usd:     Decimal
    collateral_usd:    Decimal
    liquidation_price: Decimal
    status:            CDPStatus
    timelock_height:   int = 0
    txid:              str = ""

    @property
    def collateral_dgb_display(self) -> float:
        """Human-readable DGB amount (converts from satoshis)."""
        return self.collateral_dgb / 1e8

    @property
    def is_safe(self) -> bool:
        """True if ratio is at or above the safe threshold."""
        return self.collateral_ratio >= SAFE_COLLATERAL_RATIO

    @property
    def distance_to_liquidation(self) -> Decimal:
        """How far DGB price can drop (USD) before liquidation."""
        if self.dgb_price_usd <= 0:
            return Decimal("0")
        return self.dgb_price_usd - self.liquidation_price

    def __str__(self) -> str:
        lines = [
            f"CDP Position [{self.status.value.upper()}]",
            f"  Collateral    : {self.collateral_dgb_display:,.8f} DGB",
            f"  Collateral USD: ${self.collateral_usd:,.2f}",
            f"  Debt          : {self.debt_dusd:,.2f} DUSD",
            f"  Ratio         : {self.collateral_ratio:.1f}%",
            f"  DGB Price     : ${self.dgb_price_usd:.6f}",
            f"  Liquidation @ : ${self.liquidation_price:.6f}",
            f"  Safe distance : ${self.distance_to_liquidation:.6f}",
        ]
        if self.txid:
            lines.append(f"  Lock TXID     : {self.txid}")
        return "\n".join(lines)


@dataclass
class MintResult:
    """
    Result of a successful mintdigidollar operation.

    Attributes:
        txid            Collateral lock transaction ID
        collateral_dgb  DGB locked (satoshis)
        dusd_minted     DUSD created
        ratio           Collateral ratio at mint time
        dgb_price_usd   Oracle price at mint time
    """
    txid:           str
    collateral_dgb: int
    dusd_minted:    Decimal
    ratio:          Decimal
    dgb_price_usd:  Decimal

    def __str__(self) -> str:
        return "\n".join([
            "Mint successful 🔑",
            f"  TXID          : {self.txid}",
            f"  DGB Locked    : {self.collateral_dgb / 1e8:,.8f} DGB",
            f"  DUSD Minted   : {self.dusd_minted:,.2f} DUSD",
            f"  Ratio         : {self.ratio:.1f}%",
            f"  DGB Price     : ${self.dgb_price_usd:.6f}",
        ])


@dataclass
class RedeemResult:
    """Result of a successful redeemdigidollar operation."""
    txid:                str
    dusd_burned:         Decimal
    collateral_returned: int    # satoshis
    stability_fee_dgb:   int    # satoshis

    @property
    def collateral_returned_display(self) -> float:
        return self.collateral_returned / 1e8

    @property
    def stability_fee_display(self) -> float:
        return self.stability_fee_dgb / 1e8

    def __str__(self) -> str:
        return "\n".join([
            "Redemption successful 🔑",
            f"  TXID              : {self.txid}",
            f"  DUSD Burned       : {self.dusd_burned:,.2f} DUSD",
            f"  DGB Returned      : {self.collateral_returned_display:,.8f} DGB",
            f"  Stability Fee     : {self.stability_fee_display:,.8f} DGB",
        ])


@dataclass
class CDPHealth:
    """
    System-wide CDP health snapshot.

    Attributes:
        oracle_price_usd     Current oracle DGB/USD price
        oracle_stale         True if oracle data is older than expected
        minting_blocked      True if ERR conditions block new mints
        total_dusd_supply    Total DUSD in circulation
        avg_collateral_ratio Average ratio across all CDPs
        err_threshold        Emergency Redemption Ratio threshold
        block_height         Current chain tip height
    """
    oracle_price_usd:     Decimal
    oracle_stale:         bool
    minting_blocked:      bool
    total_dusd_supply:    Decimal
    avg_collateral_ratio: Decimal
    err_threshold:        Decimal
    block_height:         int

    @property
    def is_healthy(self) -> bool:
        """System is healthy when oracle is fresh and minting is open."""
        return not self.oracle_stale and not self.minting_blocked

    def __str__(self) -> str:
        status = "✅ HEALTHY" if self.is_healthy else "⚠️  DEGRADED"
        return "\n".join([
            f"CDP System Health [{status}]",
            f"  DGB Price       : ${self.oracle_price_usd:.6f}",
            f"  Oracle Stale    : {self.oracle_stale}",
            f"  Minting Blocked : {self.minting_blocked}",
            f"  DUSD Supply     : {self.total_dusd_supply:,.2f} DUSD",
            f"  Avg CDP Ratio   : {self.avg_collateral_ratio:.1f}%",
            f"  ERR Threshold   : {self.err_threshold:.1f}%",
            f"  Block Height    : {self.block_height:,}",
        ])


# ---------------------------------------------------------------------------
# CDP Client
# ---------------------------------------------------------------------------

class CDPClient:
    """
    High-level interface to DigiDollar CDP operations on DigiByte Core v9.26+.

    Wraps DGB Core RPC methods for collateralized debt positions.

    Usage::

        from digiforge.cdp import CDPClient

        cdp = CDPClient.testnet(password="secret")

        # Check system health before any operation
        health = cdp.health()
        print(health)

        # Mint DUSD -- 10,000 DGB collateral at 300% ratio
        result = cdp.mint(
            collateral_dgb_satoshis=10_000 * 100_000_000,
            ratio=300,
        )
        print(result)

        # Check position
        position = cdp.position()
        print(position)

        # Project engine capacity
        projection = cdp.engine_projection(10_000 * 100_000_000)
    """

    def __init__(self, config: Optional[NodeConfig] = None):
        self.rpc = DigiByteRPC(config)

    @classmethod
    def from_env(cls) -> "CDPClient":
        """Create from DGB_RPC_* environment variables."""
        return cls(NodeConfig.from_env())

    @classmethod
    def testnet(cls, password: str = "", **kwargs) -> "CDPClient":
        """Connect to testnet node (port 12022)."""
        return cls(NodeConfig.testnet(password=password, **kwargs))

    @classmethod
    def mainnet(cls, password: str = "", **kwargs) -> "CDPClient":
        """Connect to mainnet node (port 14022)."""
        return cls(NodeConfig.mainnet(password=password, **kwargs))

    # ------------------------------------------------------------------
    # Health and oracle
    # ------------------------------------------------------------------

    def health(self) -> CDPHealth:
        """
        Query system-wide CDP health from the node.

        Raises:
            CDPError: Node returned an error.
        """
        try:
            oracle_data = self.rpc.call("getoracleprice")
            chain_info  = self.rpc.call("getblockchaininfo")
            cdp_stats   = self.rpc.call("getcdpstats")
        except RPCError as exc:
            raise CDPError(f"Failed to fetch CDP health: {exc}") from exc

        return CDPHealth(
            oracle_price_usd     = Decimal(str(oracle_data.get("price", "0"))),
            oracle_stale         = bool(oracle_data.get("stale", False)),
            minting_blocked      = bool(cdp_stats.get("mintingBlocked", False)),
            total_dusd_supply    = Decimal(str(cdp_stats.get("totalSupply", "0"))),
            avg_collateral_ratio = Decimal(str(cdp_stats.get("avgCollateralRatio", "0"))),
            err_threshold        = Decimal(str(cdp_stats.get("errThreshold", "150"))),
            block_height         = int(chain_info.get("blocks", 0)),
        )

    def oracle_price(self) -> Decimal:
        """
        Return current oracle DGB/USD price.

        Raises:
            OracleStaleError: Oracle data is marked stale.
            CDPError: RPC failure.
        """
        try:
            data = self.rpc.call("getoracleprice")
        except RPCError as exc:
            raise CDPError(f"Oracle query failed: {exc}") from exc

        if data.get("stale", False):
            raise OracleStaleError(
                "Oracle price is stale. Node may be out of sync or oracles offline."
            )
        return Decimal(str(data["price"]))

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------

    def position(self) -> CDPPosition:
        """
        Return the current CDP position for the loaded wallet.

        Raises:
            CDPNotFoundError: No active CDP exists.
            RPCError: Node error.
        """
        try:
            info = self.rpc.call("getcdpinfo")
        except RPCError as exc:
            if exc.code == -5 or "no cdp" in exc.message.lower():
                raise CDPNotFoundError("No active CDP for this wallet.") from exc
            raise

        if not info or info.get("status") == "none":
            raise CDPNotFoundError("No active CDP for this wallet.")

        raw_status = str(info.get("status", "healthy")).lower()
        try:
            status = CDPStatus(raw_status)
        except ValueError:
            ratio_val = Decimal(str(info.get("collateralRatio", "0")))
            status = CDPStatus.HEALTHY if ratio_val >= SAFE_COLLATERAL_RATIO else CDPStatus.MARGINAL

        return CDPPosition(
            collateral_dgb    = int(info.get("collateralSatoshis", 0)),
            debt_dusd         = Decimal(str(info.get("debtDUSD", "0"))),
            collateral_ratio  = Decimal(str(info.get("collateralRatio", "0"))),
            dgb_price_usd     = Decimal(str(info.get("dgbPriceUSD", "0"))),
            collateral_usd    = Decimal(str(info.get("collateralUSD", "0"))),
            liquidation_price = Decimal(str(info.get("liquidationPrice", "0"))),
            status            = status,
            timelock_height   = int(info.get("timelockHeight", 0)),
            txid              = str(info.get("txid", "")),
        )

    def collateral_balance(self) -> int:
        """Return locked collateral balance in satoshis."""
        try:
            result = self.rpc.call("getcollateralbalance")
            return int(result.get("satoshis", 0))
        except RPCError as exc:
            raise CDPError(f"Failed to get collateral balance: {exc}") from exc

    # ------------------------------------------------------------------
    # Mint
    # ------------------------------------------------------------------

    def calculate_mintable(
        self,
        collateral_dgb_satoshis: int,
        ratio: Decimal = DEFAULT_MINT_RATIO,
    ) -> Decimal:
        """
        Calculate how much DUSD can be minted for a given collateral amount and ratio.

        Args:
            collateral_dgb_satoshis: Collateral in satoshis
            ratio: Target collateral ratio percentage (default 300%)

        Returns:
            Mintable DUSD (rounded down to 2 decimal places)

        Raises:
            CDPRatioTooLowError: Ratio below minimum.
            OracleStaleError: Oracle price is stale.
        """
        if collateral_dgb_satoshis <= 0:
            raise ValidationError("collateral_dgb_satoshis must be > 0")
        if ratio < MINIMUM_COLLATERAL_RATIO:
            raise CDPRatioTooLowError(ratio, MINIMUM_COLLATERAL_RATIO)

        price = self.oracle_price()
        collateral_dgb = Decimal(str(collateral_dgb_satoshis)) / Decimal("1e8")
        collateral_usd = collateral_dgb * price
        return (collateral_usd / (ratio / Decimal("100"))).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )

    def mint(
        self,
        collateral_dgb_satoshis: int,
        ratio: int = 300,
        dusd_amount: Optional[Decimal] = None,
    ) -> MintResult:
        """
        Lock DGB as collateral and mint DUSD.

        Enforces minimum safe ratio. If dusd_amount is omitted, it is
        calculated automatically from collateral and ratio.

        Args:
            collateral_dgb_satoshis: DGB to lock (satoshis)
            ratio: Target collateral ratio percent (default 300, minimum 150)
            dusd_amount: Explicit DUSD to mint (calculated if omitted)

        Returns:
            MintResult

        Raises:
            CDPRatioTooLowError: Ratio below safe minimum.
            CDPMintBlockedError: ERR conditions block new mints.
            OracleStaleError: Oracle price is stale.
            InsufficientFundsError: Not enough DGB to lock + pay fees.
        """
        if collateral_dgb_satoshis <= 0:
            raise ValidationError("collateral_dgb_satoshis must be > 0")

        ratio_d = Decimal(str(ratio))
        if ratio_d < MINIMUM_COLLATERAL_RATIO:
            raise CDPRatioTooLowError(ratio_d, MINIMUM_COLLATERAL_RATIO)

        if dusd_amount is None:
            dusd_amount = self.calculate_mintable(collateral_dgb_satoshis, ratio_d)

        if dusd_amount <= 0:
            raise ValidationError("Calculated DUSD amount is zero -- collateral too small.")

        price = self.oracle_price()

        try:
            result = self.rpc.call("mintdigidollar", [collateral_dgb_satoshis, str(dusd_amount)])
        except RPCError as exc:
            msg = exc.message.lower()
            if "minting blocked" in msg or exc.code == -32:
                raise CDPMintBlockedError(
                    f"Minting blocked (Emergency Redemption conditions): {exc.message}"
                ) from exc
            if "insufficient" in msg:
                raise InsufficientFundsError(
                    f"Insufficient DGB to lock {collateral_dgb_satoshis} satoshis + fees"
                ) from exc
            raise CDPError(f"Mint failed: {exc}") from exc

        collateral_usd = (Decimal(str(collateral_dgb_satoshis)) / Decimal("1e8")) * price
        actual_ratio   = (collateral_usd / dusd_amount * Decimal("100")).quantize(
            Decimal("0.1"), rounding=ROUND_DOWN
        )

        return MintResult(
            txid           = str(result.get("txid", "")),
            collateral_dgb = collateral_dgb_satoshis,
            dusd_minted    = dusd_amount,
            ratio          = actual_ratio,
            dgb_price_usd  = price,
        )

    # ------------------------------------------------------------------
    # Redeem
    # ------------------------------------------------------------------

    def redeem(self, dusd_amount: Decimal) -> RedeemResult:
        """
        Burn DUSD and unlock collateral.

        The CLTV timelock must have expired before redemption succeeds.

        Args:
            dusd_amount: DUSD to burn and redeem

        Returns:
            RedeemResult

        Raises:
            CDPRedemptionError: Timelock not expired or insufficient DUSD.
            CDPNotFoundError: No active CDP.
        """
        if dusd_amount <= 0:
            raise ValidationError("dusd_amount must be > 0")

        try:
            result = self.rpc.call("redeemdigidollar", [str(dusd_amount)])
        except RPCError as exc:
            msg = exc.message.lower()
            if exc.code == -5 or "no cdp" in msg:
                raise CDPNotFoundError("No active CDP to redeem.") from exc
            if "timelock" in msg or "not yet" in msg:
                raise CDPRedemptionError(
                    f"Timelock not expired: {exc.message}"
                ) from exc
            if "insufficient" in msg:
                raise CDPRedemptionError(
                    f"Insufficient DUSD balance for {dusd_amount} DUSD redemption"
                ) from exc
            raise CDPError(f"Redemption failed: {exc}") from exc

        return RedeemResult(
            txid                 = str(result.get("txid", "")),
            dusd_burned          = dusd_amount,
            collateral_returned  = int(result.get("collateralReturnedSatoshis", 0)),
            stability_fee_dgb    = int(result.get("stabilityFeeSatoshis", 0)),
        )

    # ------------------------------------------------------------------
    # Perpetual Giving Engine
    # ------------------------------------------------------------------

    def engine_projection(
        self,
        collateral_dgb_satoshis: int,
        ratio: int = 300,
    ) -> Dict[str, Any]:
        """
        Project Perpetual Giving Engine capacity at multiple DGB price scenarios.

        Shows potential annual giving capacity (in DUSD) if DGB reaches
        various price points, using the collateral amount provided.

        The engine's key insight: collateral remains locked permanently.
        Only DUSD yield is deployed to giving. The principal never depletes.

        Args:
            collateral_dgb_satoshis: DGB collateral in satoshis
            ratio: Target collateral ratio (default 300%)

        Returns:
            Dict with projections across price scenarios
        """
        try:
            current_price = self.oracle_price()
        except (OracleStaleError, CDPError):
            current_price = Decimal("0")

        collateral_dgb = Decimal(str(collateral_dgb_satoshis)) / Decimal("1e8")
        ratio_d = Decimal(str(ratio)) / Decimal("100")

        price_points = {
            "current":  current_price,
            "$0.01":    Decimal("0.01"),
            "$0.05":    Decimal("0.05"),
            "$0.10":    Decimal("0.10"),
            "$0.25":    Decimal("0.25"),
            "$0.50":    Decimal("0.50"),
            "$1.00":    Decimal("1.00"),
        }

        scenarios = {}
        for label, p in price_points.items():
            if p <= 0:
                continue
            coll_usd    = collateral_dgb * p
            mintable    = (coll_usd / ratio_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            scenarios[label] = {
                "dgb_price_usd":  float(p),
                "collateral_usd": float(coll_usd),
                "mintable_dusd":  float(mintable),
                "annual_giving":  float(mintable),
            }

        return {
            "collateral_dgb": float(collateral_dgb),
            "ratio_pct":      ratio,
            "scenarios":      scenarios,
            "engine_note": (
                "Perpetual Giving Engine: collateral remains locked. "
                "DUSD deployed to giving address. Principal never depleted. "
                "Store up treasures in heaven. -- Matthew 6:20"
            ),
        }

    def __repr__(self) -> str:
        return f"CDPClient(node={self.rpc.config.url!r})"
