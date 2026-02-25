"""
digiforge.assets
================
High-level DigiAssets v3 API.

Kael — Project Trinity — 2026
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .exceptions import (
    AssetIssuanceError,
    AssetTransferError,
    BroadcastError,
    InsufficientFundsError,
    RPCError,
    SigningError,
    ValidationError,
)
from .metadata import (
    AggregationPolicy,
    AssetMetadata,
    Divisibility,
    IssuancePayload,
    TransferPayload,
)
from .rpc import DigiByteRPC, NodeConfig
from .utils import derive_asset_id


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class IssuanceResult:
    """
    Returned by DigiForge.issue() on success.

    Attributes:
        txid        Transaction ID (64 hex chars)
        asset_id    Deterministic DigiAssets v3 AssetId
        amount      Units issued
        metadata    Metadata document used (if any)
        payload_hex OP_RETURN payload that was embedded
    """
    txid: str
    asset_id: str
    amount: int
    metadata: Optional[AssetMetadata] = None
    payload_hex: str = ""

    def __str__(self) -> str:
        parts = [
            "Issuance successful 🔑",
            f"  TXID     : {self.txid}",
            f"  Asset ID : {self.asset_id}",
            f"  Amount   : {self.amount:,}",
        ]
        if self.metadata:
            parts.append(f"  Name     : {self.metadata.asset_name}")
            parts.append(f"  Issuer   : {self.metadata.issuer}")
        return chr(10).join(parts)


@dataclass
class TransferResult:
    """
    Returned by DigiForge.transfer() on success.

    Attributes:
        txid        Transaction ID
        asset_id    Asset that was transferred
        amount      Units transferred
        to_address  Destination address
    """
    txid: str
    asset_id: str
    amount: int
    to_address: str

    def __str__(self) -> str:
        parts = [
            "Transfer successful 🔑",
            f"  TXID     : {self.txid}",
            f"  Asset ID : {self.asset_id}",
            f"  Amount   : {self.amount:,}",
            f"  To       : {self.to_address}",
        ]
        return chr(10).join(parts)


@dataclass
class AssetBalance:
    """Asset balance entry returned by DigiForge.balances()"""
    asset_id: str
    name: str
    amount: int
    divisibility: int
    issuer: str = ""
    txid: str = ""

    @property
    def display_amount(self) -> float:
        """Human-readable amount respecting divisibility."""
        if self.divisibility == 0:
            return float(self.amount)
        return self.amount / (10 ** self.divisibility)

    def __str__(self) -> str:
        return (
            f"{self.name} ({self.asset_id[:12]}...): "
            f"{self.display_amount:,.{self.divisibility}f}"
        )


# ---------------------------------------------------------------------------
# Core SDK class
# ---------------------------------------------------------------------------

class DigiForge:
    """
    Primary interface to the DigiAssets v3 protocol on DigiByte.

    All methods require an active DigiByte Core node with a loaded wallet.

    Usage:
        forge = DigiForge.from_env()       # reads DGB_RPC_* env vars
        forge = DigiForge.testnet(password="secret")
        forge = DigiForge.mainnet(password="secret", wallet="mywallet")
    """

    def __init__(self, config: Optional[NodeConfig] = None):
        self.rpc = DigiByteRPC(config)
        self._network = self.rpc.chain_info().get("chain", "unknown")

    @classmethod
    def from_env(cls) -> "DigiForge":
        """Create DigiForge from DGB_RPC_* environment variables."""
        return cls(NodeConfig.from_env())

    @classmethod
    def testnet(cls, password: str = "", **kwargs) -> "DigiForge":
        """Convenience factory for testnet node."""
        return cls(NodeConfig.testnet(password=password, **kwargs))

    @classmethod
    def mainnet(cls, password: str = "", **kwargs) -> "DigiForge":
        """Convenience factory for mainnet node."""
        return cls(NodeConfig.mainnet(password=password, **kwargs))

    # ------------------------------------------------------------------
    # Node info
    # ------------------------------------------------------------------

    @property
    def network(self) -> str:
        """Current network: main, test, or regtest."""
        return self._network

    def node_info(self) -> Dict:
        """Return full getblockchaininfo dict."""
        return self.rpc.chain_info()

    def wallet_balance(self, min_confirmations: int = 1) -> float:
        """Return spendable DGB balance."""
        return self.rpc.balance(min_confirmations)

    def new_address(self, label: str = "digiforge") -> str:
        """Generate and return a new wallet address."""
        return self.rpc.new_address(label)

    # ------------------------------------------------------------------
    # Asset issuance
    # ------------------------------------------------------------------

    def issue(
        self,
        amount: int,
        metadata: Optional[AssetMetadata] = None,
        divisibility: Divisibility = Divisibility.WHOLE,
        lock: bool = True,
        aggregation: AggregationPolicy = AggregationPolicy.AGGREGATABLE,
        to_address: Optional[str] = None,
    ) -> IssuanceResult:
        """
        Issue a new DigiAsset.

        Steps: encode payload -> create tx -> fund -> sign -> broadcast.

        Args:
            amount:       Total units to issue (must be > 0)
            metadata:     Asset metadata document
            divisibility: Decimal precision (0=whole, 6=micro)
            lock:         If True, no additional issuance is possible
            aggregation:  How units aggregate across UTXOs
            to_address:   Issue directly to this address (default: wallet)

        Returns:
            IssuanceResult with txid and asset_id
        """
        if amount <= 0:
            raise ValidationError(f"amount must be > 0, got {amount}")
        if metadata is not None:
            metadata.validate()

        payload = IssuancePayload(
            amount=amount,
            divisibility=divisibility,
            lock_status=lock,
            aggregation_policy=aggregation,
            metadata=metadata,
        )

        try:
            payload_hex = payload.encode_hex()
        except Exception as exc:
            raise AssetIssuanceError(f"Failed to encode payload: {exc}") from exc

        outputs: List[Dict] = [{"data": payload_hex}]
        if to_address:
            outputs.append({to_address: 0.0001})

        try:
            txid = self._build_and_broadcast(outputs)
        except InsufficientFundsError:
            raise
        except RPCError as exc:
            raise AssetIssuanceError(f"RPC error during issuance: {exc}") from exc
        except Exception as exc:
            raise AssetIssuanceError(f"Issuance failed: {exc}") from exc

        return IssuanceResult(
            txid=txid,
            asset_id=derive_asset_id(txid, vout=0),
            amount=amount,
            metadata=metadata,
            payload_hex=payload_hex,
        )

    # ------------------------------------------------------------------
    # Asset transfer
    # ------------------------------------------------------------------

    def transfer(
        self,
        asset_id: str,
        to_address: str,
        amount: int,
        from_utxos: Optional[List[Dict]] = None,
    ) -> TransferResult:
        """
        Transfer DigiAsset units to an address.

        Args:
            asset_id:    Target DigiAsset ID
            to_address:  Recipient DigiByte address
            amount:      Units to transfer
            from_utxos:  Optional explicit UTXOs holding the asset

        Returns:
            TransferResult with txid
        """
        if amount <= 0:
            raise ValidationError(f"amount must be > 0, got {amount}")
        if not to_address:
            raise ValidationError("to_address cannot be empty")

        transfer = TransferPayload()
        transfer.add(vout=1, amount=amount)

        try:
            payload_hex = transfer.encode_hex()
        except Exception as exc:
            raise AssetTransferError(f"Failed to encode transfer: {exc}") from exc

        outputs: List[Dict] = [
            {"data": payload_hex},
            {to_address: 0.0001},
        ]

        try:
            txid = self._build_and_broadcast(outputs, inputs=from_utxos)
        except RPCError as exc:
            raise AssetTransferError(f"RPC error: {exc}") from exc
        except Exception as exc:
            raise AssetTransferError(f"Transfer failed: {exc}") from exc

        return TransferResult(
            txid=txid,
            asset_id=asset_id,
            amount=amount,
            to_address=to_address,
        )

    # ------------------------------------------------------------------
    # Internal transaction builder
    # ------------------------------------------------------------------

    def _build_and_broadcast(
        self,
        outputs: List[Dict],
        inputs: Optional[List[Dict]] = None,
    ) -> str:
        """Create -> Fund -> Sign -> Broadcast. Returns TXID."""
        raw = self.rpc.create_raw_transaction(inputs or [], outputs)

        try:
            fund_result = self.rpc.fund_raw_transaction(
                raw,
                {"changePosition": -1, "includeWatching": False},
            )
        except RPCError as exc:
            if "insufficient" in exc.message.lower() or exc.code == -6:
                raise InsufficientFundsError(
                    f"Insufficient DGB for fees: {exc.message}"
                ) from exc
            raise

        funded_hex = fund_result["hex"]

        sign_result = self.rpc.sign_raw_transaction(funded_hex)
        if not sign_result.get("complete", False):
            raise SigningError(f"Signing incomplete: {sign_result.get(chr(101)+chr(114)+chr(114)+chr(111)+chr(114)+chr(115), [])}")
        signed_hex = sign_result["hex"]

        try:
            txid = self.rpc.send_raw_transaction(signed_hex)
        except RPCError as exc:
            raise BroadcastError(f"Broadcast rejected: {exc.message}") from exc

        return txid

    # ------------------------------------------------------------------
    # Verify / inspect
    # ------------------------------------------------------------------

    def verify_asset_tx(self, txid: str) -> Optional[Dict]:
        """
        Inspect a transaction and decode DigiAssets data if present.
        Returns parsed dict or None if not a DigiAssets transaction.
        """
        from .metadata import DA_MAGIC
        try:
            tx = self.rpc.get_raw_transaction(txid, verbose=True)
        except RPCError:
            return None

        for vout in tx.get("vout", []):
            script = vout.get("scriptPubKey", {})
            asm = script.get("asm", "")
            hex_data = script.get("hex", "")
            if not asm.startswith("OP_RETURN"):
                continue
            if hex_data.startswith("6a") and len(hex_data) > 6:
                try:
                    raw_bytes = bytes.fromhex(hex_data[4:])
                except ValueError:
                    continue
                if raw_bytes[:2] == DA_MAGIC:
                    return {
                        "txid": txid,
                        "da_magic": True,
                        "version": raw_bytes[2] if len(raw_bytes) > 2 else None,
                        "payload_hex": raw_bytes.hex(),
                        "asset_id": derive_asset_id(txid, vout=0),
                    }
        return None

    def __repr__(self) -> str:
        return f"DigiForge(network={self._network!r}, node={self.rpc.config.url!r})"
