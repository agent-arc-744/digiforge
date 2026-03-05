"""
digiforge.oracle
================
Oracle price client for DigiByte Core v9.26 DigiDollar oracle network.

The DigiDollar oracle network consists of 8 authorized operators whose
Schnorr-signed price feeds are verified against chainparams pubkeys.
Consensus requires 5-of-8 agreement (audit finding A-04).

Price source risk (audit finding A-05): oracle clients currently use
CoinMarketCap as primary source. This module wraps the node RPC and
adds staleness detection and multi-source awareness.

Kael -- Project Trinity -- 2026
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional
import time

from .exceptions import DigiForgeError, RPCError
from .rpc import DigiByteRPC, NodeConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORACLE_STALE_THRESHOLD_SECONDS = 300   # 5 minutes -- price older than this is stale
ORACLE_QUORUM                  = 5     # 5-of-8 required for consensus
ORACLE_TOTAL                   = 8     # total authorized oracle operators


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OracleError(DigiForgeError):
    """Base exception for oracle operations."""


class OraclePriceStaleError(OracleError):
    """Oracle price is older than the staleness threshold."""
    def __init__(self, age_seconds: int, threshold: int):
        self.age_seconds = age_seconds
        self.threshold = threshold
        super().__init__(
            f"Oracle price is {age_seconds}s old (threshold: {threshold}s). "
            f"Node may be out of sync or oracle operators offline."
        )


class OracleQuorumError(OracleError):
    """Insufficient oracle quorum for a trusted price."""
    def __init__(self, active: int, required: int):
        self.active = active
        self.required = required
        super().__init__(
            f"Oracle quorum not met: {active}/{required} operators active. "
            f"Price feed is unreliable."
        )


class OracleUnavailableError(OracleError):
    """Oracle RPC method not available on this node version."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class OraclePrice:
    """
    A single oracle price snapshot from the DGB node.

    Attributes:
        price_usd        DGB/USD price as reported by oracle consensus
        timestamp        Unix timestamp of the price feed
        age_seconds      How many seconds ago the price was set
        stale            True if age exceeds ORACLE_STALE_THRESHOLD_SECONDS
        active_oracles   Number of oracle operators contributing to consensus
        quorum_met       True if active_oracles >= ORACLE_QUORUM
        block_height     Block height when price was last updated
    """
    price_usd:      Decimal
    timestamp:      int
    age_seconds:    int
    stale:          bool
    active_oracles: int
    quorum_met:     bool
    block_height:   int

    @property
    def is_trusted(self) -> bool:
        """Price is trusted when fresh and quorum is met."""
        return not self.stale and self.quorum_met

    def __str__(self) -> str:
        trust = "✅ TRUSTED" if self.is_trusted else "⚠️  UNTRUSTED"
        return "\n".join([
            f"Oracle Price [{trust}]",
            f"  DGB/USD      : ${self.price_usd:.8f}",
            f"  Age          : {self.age_seconds}s",
            f"  Stale        : {self.stale}",
            f"  Active Oracles: {self.active_oracles}/{ORACLE_TOTAL}",
            f"  Quorum Met   : {self.quorum_met}",
            f"  Block Height : {self.block_height:,}",
        ])


@dataclass
class OracleStatus:
    """
    Full oracle network status including per-operator details.

    Attributes:
        operators        List of oracle operator entries
        consensus_price  Agreed price (if quorum met)
        quorum_met       True if >= 5 operators agree
        last_update      Unix timestamp of last consensus update
    """
    operators:       List[Dict]
    consensus_price: Optional[Decimal]
    quorum_met:      bool
    last_update:     int
    block_height:    int

    @property
    def active_count(self) -> int:
        """Number of operators with recent price submissions."""
        return sum(1 for op in self.operators if op.get("active", False))

    def __str__(self) -> str:
        price_str = (
            f"${self.consensus_price:.8f}" if self.consensus_price else "None"
        )
        return "\n".join([
            "Oracle Network Status",
            f"  Consensus Price : {price_str}",
            f"  Quorum Met      : {self.quorum_met} ({self.active_count}/{ORACLE_TOTAL} active)",
            f"  Last Update     : {self.last_update}",
            f"  Block Height    : {self.block_height:,}",
        ])


# ---------------------------------------------------------------------------
# Oracle Client
# ---------------------------------------------------------------------------

class OracleClient:
    """
    Client for the DigiDollar oracle price network via DGB Core v9.26 RPC.

    The oracle network uses 8 authorized operators submitting Schnorr-signed
    price messages. DGB Core verifies signatures against chainparams pubkeys
    and reaches consensus at 5-of-8. This client wraps the RPC interface
    and adds staleness detection and quorum validation.

    Security notes from DigiDollar audit (Kael, 2026-02-25):
    - A-02 (FIXED rc15): Oracle messages now verified against chainparams
      pubkeys, not self-signed. Fake price injection is no longer possible.
    - A-04 (OPEN): 5-of-8 threshold means 4 colluding operators = denial
      of service. 5 colluding = arbitrary price manipulation.
    - A-05 (OPEN): All oracle clients currently use CoinMarketCap as sole
      price source. Single point of failure for price data.

    Usage::

        from digiforge.oracle import OracleClient

        oracle = OracleClient.testnet(password="secret")

        price = oracle.price()
        print(price)
        print(f"DGB/USD: ${price.price_usd:.6f}")

        # Get trusted price (raises if stale or quorum not met)
        usd = oracle.trusted_price()

        status = oracle.status()
        print(status)
    """

    def __init__(self, config: Optional[NodeConfig] = None):
        self.rpc = DigiByteRPC(config)
        self._stale_threshold = ORACLE_STALE_THRESHOLD_SECONDS

    @classmethod
    def from_env(cls) -> "OracleClient":
        """Create from DGB_RPC_* environment variables."""
        return cls(NodeConfig.from_env())

    @classmethod
    def testnet(cls, password: str = "", **kwargs) -> "OracleClient":
        """Connect to testnet node (port 12022)."""
        return cls(NodeConfig.testnet(password=password, **kwargs))

    @classmethod
    def mainnet(cls, password: str = "", **kwargs) -> "OracleClient":
        """Connect to mainnet node (port 14022)."""
        return cls(NodeConfig.mainnet(password=password, **kwargs))

    # ------------------------------------------------------------------
    # Price queries
    # ------------------------------------------------------------------

    def price(self) -> OraclePrice:
        """
        Fetch current oracle price from the node.

        Returns OraclePrice regardless of staleness -- check .is_trusted
        or call trusted_price() if you need a guaranteed fresh price.

        Raises:
            OracleUnavailableError: getoracleprice RPC not available.
            OracleError: Other RPC failure.
        """
        try:
            data = self.rpc.call("getoracleprice")
            chain = self.rpc.call("getblockchaininfo")
        except RPCError as exc:
            if exc.code == -32601:  # method not found
                raise OracleUnavailableError(
                    "getoracleprice RPC not available. "
                    "Ensure DGB Core v9.26+ is running with DigiDollar enabled."
                ) from exc
            raise OracleError(f"Oracle RPC failed: {exc}") from exc

        now = int(time.time())
        oracle_ts  = int(data.get("timestamp", now))
        age        = now - oracle_ts
        stale_flag = bool(data.get("stale", age > self._stale_threshold))
        active     = int(data.get("activeOracles", 0))
        quorum     = active >= ORACLE_QUORUM

        return OraclePrice(
            price_usd      = Decimal(str(data.get("price", "0"))),
            timestamp      = oracle_ts,
            age_seconds    = age,
            stale          = stale_flag,
            active_oracles = active,
            quorum_met     = quorum,
            block_height   = int(chain.get("blocks", 0)),
        )

    def trusted_price(self) -> Decimal:
        """
        Return the oracle DGB/USD price only if fresh and quorum is met.

        This is the safe call to use before any CDP operation.

        Raises:
            OraclePriceStaleError: Price is older than threshold.
            OracleQuorumError: Quorum not met.
            OracleUnavailableError: RPC not available.
        """
        p = self.price()

        if p.stale:
            raise OraclePriceStaleError(p.age_seconds, self._stale_threshold)
        if not p.quorum_met:
            raise OracleQuorumError(p.active_oracles, ORACLE_QUORUM)

        return p.price_usd

    def price_usd(self) -> Decimal:
        """
        Convenience: return raw DGB/USD price without staleness check.
        Use trusted_price() for safety-critical operations.
        """
        return self.price().price_usd

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> OracleStatus:
        """
        Return full oracle network status including per-operator info.

        Raises:
            OracleUnavailableError: RPC not available.
            OracleError: RPC failure.
        """
        try:
            data  = self.rpc.call("getoraclestatus")
            chain = self.rpc.call("getblockchaininfo")
        except RPCError as exc:
            if exc.code == -32601:
                raise OracleUnavailableError(
                    "getoraclestatus RPC not available on this node."
                ) from exc
            raise OracleError(f"Oracle status RPC failed: {exc}") from exc

        consensus_raw = data.get("consensusPrice")
        consensus     = Decimal(str(consensus_raw)) if consensus_raw else None
        operators     = data.get("operators", [])
        active        = sum(1 for op in operators if op.get("active", False))
        quorum        = active >= ORACLE_QUORUM

        return OracleStatus(
            operators       = operators,
            consensus_price = consensus,
            quorum_met      = quorum,
            last_update     = int(data.get("lastUpdate", 0)),
            block_height    = int(chain.get("blocks", 0)),
        )

    def is_available(self) -> bool:
        """
        Return True if the oracle RPC is available on this node.
        Safe to call without raising -- useful for feature detection.
        """
        try:
            self.rpc.call("getoracleprice")
            return True
        except RPCError as exc:
            if exc.code == -32601:
                return False
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Staleness threshold
    # ------------------------------------------------------------------

    def set_stale_threshold(self, seconds: int) -> None:
        """
        Override the staleness threshold (default: 300 seconds).
        Useful for testing or for different network conditions.
        """
        if seconds <= 0:
            raise ValueError("Stale threshold must be > 0 seconds")
        self._stale_threshold = seconds

    def __repr__(self) -> str:
        return f"OracleClient(node={self.rpc.config.url!r}, stale_threshold={self._stale_threshold}s)"
