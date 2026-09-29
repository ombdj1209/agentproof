# Agentproof

**Risk and conformance lab for agent-initiated payments.** Simulated shopping agents (honest, confused and prompt-injected), mandate-scope verification, three mock protocol adapters decoding to one canonical checkout, and a layered decision engine evaluated on labelled traffic with an explicit euro cost model.

The core question it tests: card-era fraud models ask *"is this the cardholder?"*. When an AI agent buys on someone's behalf, the first question becomes *"is this within what the cardholder delegated?"*, and that question is deterministic.

All traffic, keys, merchants and shoppers are synthetic. The protocol adapters are mocks with deliberately different wire shapes, not spec-conformant implementations of any real protocol.

## Why this exists

In June 2026 Adyen launched Adyen Agentic: a product feed, a cart orchestration layer, and a payments and fraud layer for agent-led transactions that must work across competing protocols. Adyen has publicly rated agentic commerce maturity at about 0.5 out of 5 and named catalog normalisation, fraud liability and protocol fragmentation as the main barriers. Agentproof works on two of those three: **fraud liability** (who authorised this?) and **fragmentation** (does the same purchase get the same decision on every rail?).

## How it works

```
wire payload ──► protocol adapter ──► canonical Checkout ──► Layer 1 integrity ──► Layer 2 scope ──► Layer 3 behaviour
 (session|order|chain)                (mandate, cart, context)  signatures, expiry,     merchant, category,   ML on device, address,
                                                                 replay, agent binding   quantity, cumulative  IP, agent reputation
                                                                 → decline               spend → step-up       → approve/step-up/decline
```

| Piece | File |
|---|---|
| Intent mandate (what the shopper delegated) and cart mandate (what the merchant asks for), signed over canonical JSON | `agentproof/mandate.py` |
| Session-style, order-style and mandate-chain adapters with exact round-trips | `agentproof/protocols.py` |
| Synthetic world and 15 labelled scenario generators | `agentproof/world.py` |
| Layered engine, per-mandate ledger, euro cost model | `agentproof/engine.py` |
| Threshold tuning, training sets, summaries | `agentproof/lab.py` |
| FastAPI service, one endpoint per protocol | `agentproof/service.py` |

### Traffic classes

| Class | Scenarios | Should be |
|---|---|---|
| Honest | In scope, allowed substitution, two carts within budget | Approved |
| Confused agent | One over the quantity limit, over budget, wrong category | Stepped up (not fraud, not authorised) |
| Delegation attack | Prompt-injected merchant swap, injected gift card, quantity inflation, replayed cart, expired mandate, forged signature, limit tampered after signing, wrong agent, split spend | Not approved |
| In-scope fraud | Account takeover: validly signed mandate, in-scope cart | Caught by behaviour only |

## Results

`python run_eval.py`: 45 s on one CPU core. Train, validation and test come from three separate synthetic worlds (different keys, shoppers and mandates). Thresholds are tuned on validation to minimise expected cost, then frozen. Test set: 3,362 attempts (2,617 honest, 263 confused, 318 delegation attacks, 164 account takeovers).

| Engine | Cost per 1,000 attempts | Honest approved | Delegation attacks approved | Account takeovers approved | p99 latency |
|---|---|---|---|---|---|
| Card-era model (device, address, IP, amount) | €5,828 | 0.1% | 0.0% | 0.0% | 327 µs |
| Delegation checks only | €7,631 | 100.0% | 0.0% | 100.0% | 91 µs |
| **Layered (checks, then behaviour)** | **€1,719** | **99.1%** | **0.0%** | **9.8%** | 427 µs |

**Reading it:**

- **The card-era model can only stop attacks by stepping up 99.8% of honest shoppers.** Its signals cannot tell a prompt-injected merchant swap from a normal purchase, so the cheapest policy it can reach is to challenge everyone. It blocks everything and converts nothing.
- **Delegation checks alone stop every delegation attack with zero honest friction**, because each attack breaks a verifiable rule. They are blind to account takeover, where the mandate is genuinely signed.
- **Layering fixes both.** Checks handle delegation, and the behavioural model only sees carts that are already in scope, where it catches 90% of account takeovers (84.8% declined, 5.5% stepped up) while approving 99.1% of honest traffic. Expected cost is 71% below the card-era model.
- **Confused agents are never silently approved**: 100% are sent back to the shopper for confirmation instead of being declined as fraud.

### Same purchase, every protocol

All 3,362 test attempts were encoded in each of the three wire formats, decoded and decided by a fresh engine per protocol: **decisions agree on 100.00%** and **encode/decode round-trips are exact on 100.00%**. Risk logic is written once, and the conformance suite proves it behaves identically on every rail.

## Run it

```bash
pip install -e ".[dev]"
python -m pytest -q        # 13 tests: signatures, replay, agent binding, cumulative spend, round-trips, cross-protocol agreement, HTTP
python run_eval.py         # full evaluation, under a minute
open results/report.html
uvicorn agentproof.service:app --port 8000   # POST /v1/{session|order|chain}/authorize
```

## Design notes

- **Deterministic before probabilistic.** A signature, an expiry or an allow-list is either satisfied or not. Running a model before those checks wastes its capacity on cases a rule answers exactly, and makes the rule's answer negotiable.
- **Scope breaches are stepped up, not declined.** A confused agent is not a fraudster; the shopper gets a chance to approve. Integrity failures (forged, tampered, replayed, wrong agent) are declined outright.
- **The ledger enforces cumulative spend.** Split-spend attacks pass every per-cart check; only per-mandate state catches them. Only approved carts consume budget.
- **The merchant signature covers cart contents.** Editing one line after signing is detected (tested).
- **Costs are explicit.** False declines lose margin, step-ups lose a share of honest shoppers, approved attacks cost the amount plus a chargeback fee, and step-ups stop 95% of attackers but only 40% of account takeovers. Changing these moves the tuned thresholds, which is the point.

## Limitations

- HMAC-SHA256 stands in for the public-key or verifiable-credential signatures a real protocol would use. The verification flow is the same; the key distribution problem is not modelled.
- The three adapters are mocks inspired by how public protocols differ in shape (session, order, mandate chain). They are not implementations of ACP, UCP or AP2.
- The behavioural signals and the account-takeover distribution are synthetic, so the 90% catch rate measures the architecture, not a real-world detection rate.
- The ledger is in-process. Production needs a strongly consistent store keyed by mandate, or split-spend checks race.
- Latency includes a scikit-learn single-row prediction (about 200 µs). A compiled model would be far faster.

## Next steps

1. Replace HMAC with Ed25519 plus a key-discovery endpoint, and test key rotation.
2. Add a prompt-injection corpus: product descriptions that try to redirect agents, scored for which get through to Layer 2.
3. Per-merchant liability rules: who pays when a confused agent's step-up is approved by the shopper, and later disputed.
4. Property-based fuzzing of the adapters with malformed payloads.
