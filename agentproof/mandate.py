"""Delegation objects. An IntentMandate is what the shopper authorised the agent to do; a CartMandate
is what the merchant is asking to be paid for. Both are signed over a canonical JSON encoding.

HMAC-SHA256 stands in for the public-key / verifiable-credential signatures a production protocol
would use; the verification logic (canonical bytes, key lookup, constant-time compare) is the same."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field, replace


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sign(obj: dict, key: bytes) -> str:
    return hmac.new(key, canonical(obj), hashlib.sha256).hexdigest()


def verify(obj: dict, sig: str, key: bytes | None) -> bool:
    return key is not None and hmac.compare_digest(sign(obj, key), sig or "")


@dataclass(frozen=True)
class IntentMandate:
    mandate_id: str
    user_id: str
    agent_id: str                       # the agent this delegation is bound to
    merchants: tuple[str, ...]          # allow-list
    categories: tuple[str, ...]
    max_total_cents: int                # cumulative across all carts under this mandate
    max_qty_per_item: int
    currency: str
    issued_at: int                      # unix seconds
    expires_at: int
    allow_substitution: bool = False
    signature: str = ""

    def body(self) -> dict:
        d = asdict(self)
        d.pop("signature")
        d["merchants"], d["categories"] = list(self.merchants), list(self.categories)
        return d

    def signed(self, key: bytes) -> "IntentMandate":
        return replace(self, signature=sign(self.body(), key))


@dataclass(frozen=True)
class Line:
    sku: str
    category: str
    qty: int
    unit_cents: int


@dataclass(frozen=True)
class CartMandate:
    cart_id: str
    mandate_id: str
    merchant_id: str
    lines: tuple[Line, ...]
    currency: str
    created_at: int
    signature: str = ""

    @property
    def total_cents(self) -> int:
        return sum(l.qty * l.unit_cents for l in self.lines)

    def body(self) -> dict:
        d = asdict(self)
        d.pop("signature")
        d["lines"] = [asdict(l) for l in self.lines]
        return d

    def signed(self, key: bytes) -> "CartMandate":
        return replace(self, signature=sign(self.body(), key))


@dataclass(frozen=True)
class Context:
    """Signals available at authorisation time."""
    presenting_agent: str
    new_device: bool
    new_ship_address: bool
    ip_risk: float                     # 0..1
    agent_reputation: float            # 0..1, from the agent's transaction history


@dataclass(frozen=True)
class Checkout:
    """Protocol-independent view every adapter must produce."""
    protocol: str
    mandate: IntentMandate
    cart: CartMandate
    ctx: Context
    meta: dict = field(default_factory=dict, compare=False)
