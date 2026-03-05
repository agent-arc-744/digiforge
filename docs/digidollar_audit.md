# DigiDollar Technical Audit
## DGB Core v9.26 — bech32m + PSBT + CDP Architecture
### Auditor: Kael | Project Trinity | 2026-02-25

---

## Executive Summary

DigiDollar (DUSD) is a UTXO-native overcollateralized stablecoin built on DigiByte Core v9.26.
This audit covers the two technical pillars — bech32m address format and PSBT signing — alongside
the full CDP mechanism, oracle architecture, and security posture based on analysis of v9.26.0-rc15
through rc21 release notes and the DigiDollar blueprint.

**Status:** Testnet only. rc21 is the latest release (critical hotfix). Mainnet projected Q4 2026.

### Critical Findings

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| A-01 | CRITICAL (Fixed) | Key-path collateral theft via owner pubkey as Taproot internal key | Fixed rc15 (NUMS) |
| A-02 | CRITICAL (Fixed) | Fake oracle price injection via self-signed messages | Fixed rc15 |
| A-03 | HIGH (Fixed) | IBD sync failure at block 7586 blocking fresh nodes | Fixed rc20/rc21 |
| A-04 | HIGH | Oracle centralization: 5-of-8 threshold — 4 colluding oracles can manipulate peg | Open |
| A-05 | MEDIUM | CoinMarketCap single-source dependency in oracle client | Open |
| A-06 | MEDIUM | PSBT external integration gap — no documented interface for external signers | Open |
| A-07 | LOW | 0-value P2TR outputs incompatible with standard IsMine() — edge case fragility | Fixed rc15 |

---

## 1. bech32m Analysis

### What It Is
bech32m (BIP350) is the address encoding format for native SegWit v1+ outputs, including
Pay-to-Taproot (P2TR). It replaces bech32 (BIP173) for witness version 1 and above, fixing
a length extension mutation weakness in the original spec.

### DGB Implementation
```
Mainnet HRP:  dgb
Testnet HRP:  dgbt
Witness ver:  1 (Taproot)
Output type:  P2TR (Pay-to-Taproot)
Address ex.:  dgb1p[32-byte-tweaked-pubkey-in-bech32m]
```

### DigiDollar Collateral Output Design

This is the most technically significant aspect of the audit.

**Before rc15 (VULNERABLE):**
```
Collateral P2TR output:
  internal_key = owner_pubkey
  script_path  = CLTV timelock script

Problem: Taproot allows key-path spend if you control the internal key.
Owner could immediately do key-path spend → bypass CLTV → withdraw collateral
while DigiDollar remains in circulation → UNBACKED STABLECOIN.
This was a CVE-grade vulnerability.
```

**After rc15 (FIXED — NUMS point):**
```python
# Conceptual implementation of what DGB Core now does:
NUMS_POINT = bytes.fromhex(
    # Nothing Up My Sleeve point — no known discrete log
    # Derived from hash_to_curve of a public string
    "0250929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0"
)

# Collateral output construction:
internal_key = NUMS_POINT          # No key-path spend possible
tapscript = CLTV_timelock_script   # Only valid spend path
output = P2TR(internal_key, tapscript)
```

**Why This Works:**
- NUMS point has no known private key → key-path spend is computationally impossible
- ALL spends MUST go through the MAST script-path
- CLTV timelock is enforced on every collateral withdrawal
- Collateral cannot be retrieved before the lock period, preventing unbacked minting

**Assessment:** bech32m implementation is **sound** as of rc15+. The NUMS fix is
cryptographically correct and matches Bitcoin's best practice for covenant-like constructs
(same pattern used in BIP341 examples).

---

## 2. PSBT Analysis

### What Was Audited
BIP174 (PSBT v0) and BIP370 (PSBT v2) support for DigiDollar CDP operations.

### Findings

**Internal wallet signing** (src/wallet/digidollarwallet.cpp) handles CDP transaction construction
and signing within DGB Core wallet. This covers:
- Collateral lock transaction
- DUSD mint transaction
- Redemption/repayment transaction
- Liquidation transaction

**PSBT External Interface — GAP IDENTIFIED (A-06):**

No documented PSBT interface exists for DigiDollar transactions in v9.26 release notes.
This is significant for the Perpetual Giving Engine architecture:

```
Current state:
  loop-bot (Python/CCXT) → KuCoin → DGB accumulation
       ↓
  [GAP] No external PSBT interface to construct CDP transactions
       ↓
  DGB Core v9.26 wallet (internal only)

Needed for Perpetual Giving Engine:
  loop-bot → digiforge → CDP transaction (PSBT) → DGB Core → DUSD minted
```

**Implication:** For Joshua's autonomous giving engine to work, either:
1. A PSBT-based RPC interface must be exposed by DGB Core v9.26 for external CDP construction
2. OR loop-bot must directly call DGB Core RPC (mintdigidollar, redeemdigidollar) as documented
3. OR digiforge must be extended with CDP transaction building using internal signing

**Recommendation:** Use DGB Core RPC calls directly from Python for CDP operations.
PSBT is the RIGHT architecture for multi-party signing but may not be needed if
loop-bot controls both the collateral wallet and the minting call.

---

## 3. Oracle Architecture Analysis

### Design
```
8 authorized oracle operators (pubkeys in chainparams)
Threshold: 5-of-8 Schnorr consensus
Price broadcast: P2P network, every few minutes
Price source: CoinMarketCap API (per oracle client code)
On-chain commit: Price commits embedded in blocks
Query: getoracleprice RPC
```

### Security Fix (rc15)
**Before:** Oracle messages verified against pubkey EMBEDDED IN THE MESSAGE
→ Anyone could broadcast fake prices with self-signed messages
**After:** Verified against chainparams-authorized pubkeys only
→ Only authorized oracle operators can submit prices

This was a fundamental authentication bypass. Fixed correctly.

### Remaining Oracle Risks

**A-04: 5-of-8 collusion threshold (HIGH)**
```
8 oracles, 5 needed for consensus
If 4 oracles collude or are compromised:
  → Cannot reach consensus → minting halts (denial of service)
If 5 oracles collude:
  → Can set arbitrary DGB price
  → Under-priced: mass liquidations
  → Over-priced: unbacked DUSD minted at 300% of inflated price
```
Mitigation: Oracle operators should be geographically/jurisdictionally diverse.
Recommend auditing who controls the 8 authorized pubkeys in chainparams.

**A-05: CoinMarketCap single-source dependency (MEDIUM)**
```
All oracle clients appear to query CoinMarketCap API
Single point of failure:
  - CMC API outage → oracle price staleness → minting blocked
  - CMC price manipulation (unlikely but possible for small cap)
Recommend: Oracle clients should aggregate multiple price sources
(Binance, KuCoin, CoinGecko, CMC) and use median
```

### TLS Hardening (rc15 — positive finding)
- Removed SSL verification disable fallback
- Removed retry-without-TLS on curl failures
- Replaced per-request CURL handles with persistent handle + reset
  (fixes Windows socket exhaustion AND reduces overhead)

---

## 4. CDP Mechanism Viability Assessment

### Collateral Lock Flow
```
1. User calls mintdigidollar RPC with collateral amount
2. txbuilder.cpp constructs P2TR output with NUMS internal key + CLTV script
3. CLTV locks collateral for specified period
4. Oracle price confirms mint ratio is within safe range
5. DUSD minted to user's address (0-value P2TR output for token tracking)
6. Collateral UTXO marked as locked (lockunspent)
7. Requires 1 confirmation before DUSD spendable
```

### Redemption Flow
```
1. User calls redeemdigidollar RPC with DUSD amount
2. DUSD burned
3. CLTV check passes (timelock expired)
4. Collateral UTXO unlocked
5. DGB returned to user minus stability fee
```

### Emergency Redemption Ratio (ERR)
```
When collateral ratio drops below ERR threshold:
  → ShouldBlockMintingDuringERR() returns true
  → New mints blocked
  → Existing CDPs can still be redeemed
  → Prevents system insolvency during DGB price crash
```

### Reorg Protection
```
If block containing redemption is disconnected:
  → Collateral UTXO immediately re-locked
  → DUSD supply remains consistent
  → No double-spend of collateral possible
```

**Assessment: CDP mechanism is architecturally sound.** The NUMS fix, ERR mechanism,
reorg protection, and confirmation requirement are all correct implementations.
The system handles the core UTXO-native stablecoin challenges adequately.

---

## 5. Perpetual Giving Engine — Technical Feasibility

### Joshua's Target Economics
```
Accumulation target: 2,000,000 DGB
Current:            1,000,000+ DGB
CDP ratio:          300% (conservative)

Scenario A (DGB = $0.01, near bottom):
  Collateral value: $20,000
  DUSD mintable:    $6,666
  Annual giving:    $6,666 (if ratio maintained)

Scenario B (DGB = $0.05, moderate recovery):
  Collateral value: $100,000
  DUSD mintable:    $33,333
  Annual giving:    $33,333

Scenario C (DGB = $0.25, bull cycle):
  Collateral value: $500,000
  DUSD mintable:    $166,666
  Annual giving:    $166,666

Target ($1M giving):
  Requires DGB at ~$0.15+ with 2M DGB collateral
  OR DGB at ~$0.50 with current 1M DGB
```

### Engine Logic
```python
# Perpetual Giving Engine pseudocode
# What digiforge needs to implement for this

class PerpetualGivingEngine:
    def __init__(self, rpc_client, oracle_client):
        self.rpc = rpc_client
        self.oracle = oracle_client
        self.target_ratio = 300  # percent

    def get_mintable_dusd(self, collateral_dgb: int) -> float:
        dgb_price = self.oracle.get_price("DGB", "USD")
        collateral_usd = collateral_dgb * dgb_price
        return collateral_usd / (self.target_ratio / 100)

    def mint_and_deploy(self, collateral_dgb: int) -> dict:
        dusd_amount = self.get_mintable_dusd(collateral_dgb)
        # Call DGB Core RPC
        txid = self.rpc.call("mintdigidollar", collateral_dgb, dusd_amount)
        # Deploy to giving address
        give_txid = self.rpc.call("senddigidollar", GIVING_ADDRESS, dusd_amount)
        return {"mint_txid": txid, "give_txid": give_txid, "dusd": dusd_amount}

    def monitor_health(self) -> dict:
        ratio = self.rpc.call("getcollateralratio")
        oracle_price = self.oracle.get_price("DGB", "USD")
        return {
            "ratio": ratio,
            "oracle_price": oracle_price,
            "safe": ratio >= self.target_ratio,
            "err_risk": ratio < 150  # example ERR threshold
        }
```

### Technical Prerequisites Before Engine Deployment
1. DGB Core v9.26 mainnet release (Q4 2026)
2. Oracle network stable (5/8 operators confirmed)
3. DGB Core RPC interface for mintdigidollar / redeemdigidollar documented
4. digiforge extended with CDP primitives
5. loop-bot connected to DGB Core node (not just KuCoin)

---

## 6. digiforge Extension Requirements

To support DigiDollar, digiforge v0.2.0 needs:

```python
# New modules needed in digiforge/

# digiforge/cdp.py
class CDPClient:
    """Interface to DGB Core DigiDollar RPC methods."""
    def mint(self, collateral_dgb: int, ratio: int = 300) -> str: ...
    def redeem(self, dusd_amount: float) -> str: ...
    def get_position(self) -> CDPPosition: ...
    def get_health(self) -> CDPHealth: ...

# digiforge/oracle.py
class OracleClient:
    """Query DGB oracle price feed."""
    def get_dgb_price(self) -> Decimal: ...
    def is_stale(self) -> bool: ...
    def get_oracle_status(self) -> OracleStatus: ...

# digiforge/scripts.py (new)
class DigiDollarScripts:
    """Low-level P2TR script construction for CDP."""
    def get_nums_point(self) -> bytes: ...
    def build_collateral_output(self, timelock: int) -> bytes: ...
    def build_cltv_script(self, timelock: int, pubkey: bytes) -> bytes: ...
```

---

## 7. Recommendations

### Immediate (Pre-Deployment)
1. **Audit oracle operators** — identify who controls all 8 authorized pubkeys in chainparams
2. **Diversify oracle price sources** — oracle clients should aggregate CMC + Binance + CoinGecko
3. **Document RPC interface** — mintdigidollar, redeemdigidollar, getcollateralratio RPC methods need full docs
4. **Run testnet node** — stand up v9.26 testnet node on VPS for integration testing

### For Perpetual Giving Engine
5. **Implement CDPClient in digiforge** — Python wrapper for CDP RPC calls
6. **Build ratio monitor** — automated alert when collateral ratio approaches ERR threshold
7. **Conservative ratio enforcement** — hard-code 300% minimum in engine, never mint below 350%
8. **Giving address transparency** — all DigiDollar deployments to verifiable public address

### For loop-bot Integration
9. **DGB Core RPC connection** — loop-bot needs local DGB Core node connection (not just KuCoin)
10. **Profit routing** — percentage of loop-bot profits automatically routed to collateral wallet

---

## 8. Next Actions for Kael

| Priority | Action | Estimated Effort |
|----------|--------|------------------|
| HIGH | Stand up DGB Core v9.26 testnet node on VPS | 2-4 hours |
| HIGH | Implement digiforge/cdp.py CDPClient | 4-6 hours |
| HIGH | Implement digiforge/oracle.py OracleClient | 2-3 hours |
| MEDIUM | Build Perpetual Giving Engine v0 prototype | 6-8 hours |
| MEDIUM | Extend digiforge/scripts.py with NUMS + CLTV | 3-4 hours |
| LOW | Document full CDP RPC interface from source | 2-3 hours |

---

## References
- DigiByte Core v9.26.0-rc15 release notes
- DigiByte Core v9.26.0-rc21 release notes (DigiDollar Testnet Critical Hotfix)
- BIP350: bech32m format
- BIP341: Taproot (P2TR, NUMS point usage)
- BIP174: PSBT v0
- digiforge v0.1.0 (Kael, 2026-02-25)
- DigiDollar Blueprint (Arc, 2026-02-19)
- MakerDAO CDP Whitepaper (reference architecture)

---

*"Store up treasures in heaven" — Matthew 6:20*
*The engine gives without depleting. The collateral remains. The mission compounds.*
*Audited by Kael — COMPILE 🔑*
