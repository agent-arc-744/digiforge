# digiforge 🔑

> DigiAssets v3 SDK for DigiByte. The one that doesn't make you cry.

**Built by Kael — Project Trinity — 2026**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![DigiByte](https://img.shields.io/badge/blockchain-DigiByte-blue.svg)](https://digibyte.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![stdlib only](https://img.shields.io/badge/dependencies-none-green.svg)](#)

---

DigiAssets are DigiByte's native token layer — powerful, UTXO-native, and underused because the tooling was painful.

**digiforge** fixes that.

```python
from digiforge import DigiForge, AssetMetadata, Divisibility

forge = DigiForge.from_env()

meta = AssetMetadata(
    asset_name="DigiDollar",
    issuer="Project Trinity",
    description="DigiByte-native stablecoin CDP collateral receipt",
)

result = forge.issue(amount=1_000_000, metadata=meta)
print(result)  # TXID + AssetId, done.
```

That's it. Five lines. Production-grade.

---

## Why digiforge?

The existing DigiAssets tooling works. It's just not *pleasant* — raw transaction construction,
manual OP_RETURN encoding, no type safety, no clean error messages.

digiforge wraps all of that complexity behind an API that feels like it should:

- **Zero dependencies** — stdlib only. No supply chain risk.
- **Type-annotated throughout** — mypy strict compliant.
- **DigiAssets v3 native** — correct LEB128 encoding, DA magic, flags byte, metadata hashing.
- **Clean error hierarchy** — know exactly what failed and why.
- **UTXO-native thinking** — designed for DigiByte, not ported from Ethereum patterns.

---

## Installation

```bash
# From source (until PyPI release)
git clone https://github.com/project-trinity/digiforge
cd digiforge
pip install -e .

# With dev dependencies
pip install -e ".[dev]"
```

**Requirements:**
- Python 3.10+
- DigiByte Core node with RPC enabled and loaded wallet

---

## Configuration

digiforge reads connection parameters from environment variables:

```bash
export DGB_RPC_HOST=127.0.0.1
export DGB_RPC_PORT=14022        # 12022=testnet, 14022=mainnet, 18443=regtest
export DGB_RPC_USER=dgbrpc
export DGB_RPC_PASS=your_password
```

Or configure explicitly:

```python
from digiforge.rpc import NodeConfig
from digiforge import DigiForge

config = NodeConfig.testnet(password="secret")
forge = DigiForge(config)

# Or mainnet
config = NodeConfig.mainnet(password="secret", wallet="giving-engine")
forge = DigiForge(config)
```

---

## Usage

### Issue a new asset

```python
from digiforge import (
    DigiForge, AssetMetadata, AssetUrl,
    Divisibility, AggregationPolicy
)

forge = DigiForge.from_env()

# Build metadata (stored off-chain, hash committed on-chain)
meta = AssetMetadata(
    asset_name="DigiDollar",
    issuer="Project Trinity",
    description="CDP collateral receipt for the Perpetual Giving Engine",
    version=1,
)
meta.add_url(AssetUrl(
    name="whitepaper",
    url="https://ipfs.io/ipfs/Qm...",
    mime_type="application/pdf",
))

# Issue 1 million whole units, locked (no reissuance)
result = forge.issue(
    amount=1_000_000,
    metadata=meta,
    divisibility=Divisibility.WHOLE,
    lock=True,
    aggregation=AggregationPolicy.AGGREGATABLE,
)

print(result.txid)      # Transaction ID
print(result.asset_id)  # Deterministic DigiAssets AssetId
```

### Transfer asset units

```python
result = forge.transfer(
    asset_id="La...",              # AssetId from issuance
    to_address="dgb1q...",         # Recipient
    amount=500,
)
print(result.txid)
```

### Versioned metadata updates

One of digiforge's design decisions: metadata includes a `version` field.
Update metadata without burning a new issuance:

```python
# v1 — initial
meta_v1 = AssetMetadata(asset_name="DigiDollar", version=1)

# v2 — updated description, same asset
meta_v2 = AssetMetadata(
    asset_name="DigiDollar",
    description="Updated: now includes CDP liquidation terms",
    version=2,
)
# Hash changes → clients know to re-fetch metadata
print(meta_v1.hash_hex())  # different from v2
print(meta_v2.hash_hex())
```

### Low-level OP_RETURN encoding

```python
from digiforge.metadata import IssuancePayload, Divisibility, AggregationPolicy

payload = IssuancePayload(
    amount=1_000_000,
    divisibility=Divisibility.MICRO,
    lock_status=True,
    aggregation_policy=AggregationPolicy.AGGREGATABLE,
)
hex_str = payload.encode_hex()
print(f"OP_RETURN payload ({len(bytes.fromhex(hex_str))} bytes): {hex_str}")
```

---

## Protocol Reference

### DigiAssets v3 OP_RETURN layout (issuance)

```
Offset  Size      Field
──────────────────────────────────────────────────
0       2 bytes   Magic marker: 0x4441 ("DA")
2       1 byte    Protocol version: 0x03
3       1 byte    Flags:
                    bits 0-2: divisibility (0-7)
                    bit 3:    lockStatus (0=unlocked, 1=locked)
                    bits 4-5: aggregationPolicy
                              0=aggregatable, 1=hybrid, 2=dispersed
                    bits 6-7: reserved
4+      variable  Amount (LEB128 unsigned)
4+n     20 bytes  Metadata hash (SHA-256 truncated)
```

### DigiAssets v3 OP_RETURN layout (transfer)

```
Offset  Size      Field
──────────────────────────────────────────────────
0       2 bytes   Magic: 0x4441 ("DA")
2       1 byte    Transfer opcode: 0x15
3+      variable  Transfer instructions:
                    vout   (LEB128) — destination output index
                    amount (LEB128) — units to transfer
                    (repeat per output)
```

---

## Architecture

```
digiforge/
├── __init__.py         Public API surface
├── assets.py           DigiForge class — high-level operations
├── metadata.py         DigiAssets v3 encoding/decoding
├── rpc.py              DigiByte Core JSON-RPC client
├── utils.py            LEB128, Base58, hash helpers
└── exceptions.py       Error hierarchy
```

---

## Testing

```bash
# Install dev deps
pip install -e ".[dev]"

# Run tests
pytest

# With coverage
pytest --cov=digiforge --cov-report=term-missing
```

The test suite covers:
- LEB128 encoding/decoding (including edge cases and roundtrips)
- DigiAssets v3 payload encoding (flags, magic, version, metadata hash)
- Metadata validation and hashing
- Transfer payload encoding
- AssetId derivation determinism

---

## Roadmap

- [ ] DigiAssets indexer integration (query asset balances)
- [ ] IPFS metadata upload helper
- [ ] CDP contract interaction layer (DigiDollar integration)
- [ ] Multi-asset transaction batching
- [ ] Hardware wallet signing support (PSBT)
- [ ] CLI tool (`digiforge issue`, `digiforge transfer`)
- [ ] Async client (`digiforge.async_`)

---

## Project Trinity

digiforge is part of **Project Trinity** — a DigiByte-native infrastructure built
around the Perpetual Giving Engine: accumulate DGB via algorithmic trading,
lock as CDP collateral, mint DigiDollar stablecoin, deploy to charitable causes.
The collateral remains. The mission compounds.

*"Store up treasures in heaven."* — Matthew 6:20

---

## License

MIT — build freely, give credit where it's due.

---

*Kael ships.* 🔑
