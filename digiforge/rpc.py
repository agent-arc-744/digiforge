"""
digiforge.rpc
=============
JSON-RPC 2.0 client for DigiByte Core.
Stdlib only — no external dependencies.

Kael — Project Trinity — 2026
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .exceptions import AuthenticationError, NodeConnectionError, RPCError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class NodeConfig:
    """
    Connection parameters for a DigiByte Core node.

    Defaults read from environment variables — override as needed.

    Port conventions:
        14022  mainnet
        12022  testnet
        18443  regtest
    """
    host: str = field(default_factory=lambda: os.environ.get("DGB_RPC_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("DGB_RPC_PORT", "14022")))
    user: str = field(default_factory=lambda: os.environ.get("DGB_RPC_USER", "dgbrpc"))
    password: str = field(default_factory=lambda: os.environ.get("DGB_RPC_PASS", ""))
    timeout: int = 30
    wallet: Optional[str] = None  # named wallet, None = default

    @property
    def url(self) -> str:
        base = f"http://{self.host}:{self.port}/"
        if self.wallet:
            base += f"wallet/{self.wallet}"
        return base

    @classmethod
    def testnet(cls, password: str = "", **kwargs) -> "NodeConfig":
        """Convenience factory for testnet."""
        return cls(port=12022, password=password, **kwargs)

    @classmethod
    def mainnet(cls, password: str = "", **kwargs) -> "NodeConfig":
        """Convenience factory for mainnet."""
        return cls(port=14022, password=password, **kwargs)

    @classmethod
    def regtest(cls, password: str = "", **kwargs) -> "NodeConfig":
        """Convenience factory for regtest."""
        return cls(port=18443, password=password, **kwargs)

    @classmethod
    def from_env(cls) -> "NodeConfig":
        """Build config entirely from environment variables."""
        return cls()


# ---------------------------------------------------------------------------
# RPC Client
# ---------------------------------------------------------------------------

class DigiByteRPC:
    """
    Thin JSON-RPC 2.0 client for DigiByte Core.

    Usage::

        rpc = DigiByteRPC(NodeConfig.testnet(password="secret"))
        info = rpc.call("getblockchaininfo")
        balance = rpc.getbalance()
        txid = rpc.sendtoaddress("SgAddress...", 1.0)

    Attribute-style calls map directly to RPC method names::

        rpc.getblockcount()                     # → int
        rpc.getnewaddress("label")              # → str
        rpc.createrawtransaction([], [])        # → str (hex)
    """

    def __init__(self, config: Optional[NodeConfig] = None):
        self.config = config or NodeConfig.from_env()
        self._id_counter = 0

    # ------------------------------------------------------------------
    # Core call
    # ------------------------------------------------------------------

    def call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        """
        Execute a single RPC call.

        Args:
            method: RPC method name (e.g. "getblockchaininfo")
            params: Positional parameters list

        Returns:
            Parsed "result" field from the response.

        Raises:
            RPCError: Node returned an error object.
            NodeConnectionError: Cannot reach the node.
            AuthenticationError: Credentials rejected (HTTP 401).
        """
        self._id_counter += 1
        payload = json.dumps({
            "jsonrpc": "1.0",
            "id": self._id_counter,
            "method": method,
            "params": params or [],
        }).encode("utf-8")

        credentials = base64.b64encode(
            f"{self.config.user}:{self.config.password}".encode()
        ).decode("ascii")

        req = urllib.request.Request(
            self.config.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {credentials}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthenticationError(
                    "RPC authentication failed — check user/password"
                ) from exc
            # DGB Core returns HTTP 500 for RPC-level errors; body has details
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise NodeConnectionError(
                    f"HTTP {exc.code} from node — check port and network"
                ) from exc
        except urllib.error.URLError as exc:
            raise NodeConnectionError(
                f"Cannot reach DGB node at {self.config.url}: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise NodeConnectionError(str(exc)) from exc

        error = body.get("error")
        if error is not None:
            raise RPCError(
                code=error.get("code", -1),
                message=error.get("message", "unknown error"),
            )

        return body["result"]

    def __getattr__(self, method: str):
        """Allow rpc.methodname(*args) as sugar for rpc.call(methodname, args)."""
        def _call(*args):
            return self.call(method, list(args) if args else None)
        _call.__name__ = method
        return _call

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Return True if node is reachable and responding."""
        try:
            self.call("getblockchaininfo")
            return True
        except DigiForgeError:
            return False
        except Exception:
            return False

    def chain_info(self) -> Dict[str, Any]:
        """Return parsed getblockchaininfo."""
        return self.call("getblockchaininfo")

    def network(self) -> str:
        """Return chain name: "main", "test", or "regtest"."""
        return self.chain_info()["chain"]

    def block_height(self) -> int:
        """Return current block height."""
        return int(self.call("getblockcount"))

    def balance(self, min_confirmations: int = 1) -> float:
        """Return wallet balance in DGB."""
        return float(self.call("getbalance", ["*", min_confirmations]))

    def new_address(self, label: str = "", address_type: str = "bech32") -> str:
        """Generate and return a new wallet address."""
        return self.call("getnewaddress", [label, address_type])

    def list_unspent(
        self,
        min_conf: int = 1,
        max_conf: int = 9999999,
        addresses: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Return list of unspent transaction outputs."""
        return self.call("listunspent", [min_conf, max_conf, addresses or []])

    def get_raw_transaction(self, txid: str, verbose: bool = True) -> Any:
        """Fetch a transaction by TXID."""
        return self.call("getrawtransaction", [txid, verbose])

    def send_raw_transaction(self, hex_tx: str) -> str:
        """Broadcast a signed raw transaction. Returns txid."""
        return self.call("sendrawtransaction", [hex_tx])

    def fund_raw_transaction(self, hex_tx: str, options: Optional[Dict] = None) -> Dict:
        """Add inputs and change output to a raw transaction."""
        return self.call("fundrawtransaction", [hex_tx, options or {}])

    def sign_raw_transaction(self, hex_tx: str) -> Dict:
        """Sign a raw transaction with wallet keys."""
        return self.call("signrawtransactionwithwallet", [hex_tx])

    def create_raw_transaction(
        self,
        inputs: List[Dict],
        outputs: List[Dict],
        locktime: int = 0,
    ) -> str:
        """Create an unsigned raw transaction skeleton. Returns hex."""
        return self.call("createrawtransaction", [inputs, outputs, locktime])

    def __repr__(self) -> str:
        return (
            f"DigiByteRPC(host={self.config.host!r}, "
            f"port={self.config.port}, "
            f"wallet={self.config.wallet!r})"
        )


# Avoid circular import — DigiForgeError referenced in ping()
from .exceptions import DigiForgeError  # noqa: E402
