"""Layered decision engine for agent-initiated payments.

Layer 1  integrity   signatures, expiry, replay, agent binding           -> decline
Layer 2  scope       merchant, category, quantity, cumulative spend       -> step-up (user confirms) or decline
Layer 3  behaviour   ML score on context for carts that pass 1 and 2      -> approve / step-up / decline

The design claim this repo tests: card-era fraud models ask "is this the cardholder?"; with agents the
first question becomes "is this within what the cardholder delegated?", which is deterministic and
must be checked before any model runs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier

from .mandate import Checkout, verify

APPROVE, STEP_UP, DECLINE = "approve", "step_up", "decline"
SCOPE_TOLERANCE = 0.02   # price drift allowed above the limit before we treat it as a scope breach


@dataclass
class Decision:
    outcome: str
    reasons: list[str]
    risk: float = 0.0
    micros: float = 0.0


@dataclass
class Ledger:
    """Per-mandate state. In production this is a strongly consistent store keyed by mandate id."""
    spent: dict = field(default_factory=dict)
    carts: set = field(default_factory=set)
    carts_per_mandate: dict = field(default_factory=dict)


def integrity(c: Checkout, user_keys: dict, merchant_keys: dict, ledger: Ledger) -> list[str]:
    m, cart = c.mandate, c.cart
    out = []
    if not verify(m.body(), m.signature, user_keys.get(m.user_id)):
        out.append("mandate_signature_invalid")
    if not verify(cart.body(), cart.signature, merchant_keys.get(cart.merchant_id)):
        out.append("cart_signature_invalid")
    if cart.mandate_id != m.mandate_id:
        out.append("cart_not_bound_to_mandate")
    if not (m.issued_at <= cart.created_at <= m.expires_at):
        out.append("mandate_expired_or_not_yet_valid")
    if cart.cart_id in ledger.carts:
        out.append("cart_replayed")
    if c.ctx.presenting_agent != m.agent_id:
        out.append("agent_not_delegate")
    if cart.currency != m.currency:
        out.append("currency_mismatch")
    return out


def scope(c: Checkout, ledger: Ledger) -> list[str]:
    m, cart = c.mandate, c.cart
    out = []
    if cart.merchant_id not in m.merchants:
        out.append("merchant_not_allowed")
    for l in cart.lines:
        if l.category not in m.categories:
            out.append(f"category_not_allowed:{l.category}")
        if l.qty > m.max_qty_per_item:
            out.append("quantity_above_limit")
    limit = m.max_total_cents * (1 + SCOPE_TOLERANCE)
    if cart.total_cents > limit:
        out.append("cart_above_limit")
    elif ledger.spent.get(m.mandate_id, 0) + cart.total_cents > limit:
        out.append("cumulative_spend_above_limit")
    return out


FEATURES = ("total_to_limit", "n_lines", "new_device", "new_ship_address", "ip_risk", "agent_reputation",
            "hours_since_issue", "carts_so_far", "log_total")


def features(c: Checkout, ledger: Ledger) -> list[float]:
    m, cart, x = c.mandate, c.cart, c.ctx
    return [cart.total_cents / max(1, m.max_total_cents), len(cart.lines), float(x.new_device),
            float(x.new_ship_address), x.ip_risk, x.agent_reputation,
            (cart.created_at - m.issued_at) / 3600.0, ledger.carts_per_mandate.get(m.mandate_id, 0),
            float(np.log1p(cart.total_cents))]


CARD_ERA_FEATURES = (2, 3, 4, 8)  # device, address, IP, amount: what a classic model sees


class RiskModel:
    def __init__(self, cols=None):
        self.cols = cols
        self.m = GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=0)

    def _x(self, X):
        X = np.asarray(X, dtype=float)
        return X[:, self.cols] if self.cols is not None else X

    def fit(self, X, y):
        self.m.fit(self._x(X), np.asarray(y))
        return self

    def score(self, x) -> float:
        return float(self.m.predict_proba(self._x([x]))[0, 1])


@dataclass
class Engine:
    user_keys: dict
    merchant_keys: dict
    model: RiskModel | None = None
    step_up_at: float = 0.3
    decline_at: float = 0.8
    use_checks: bool = True
    ledger: Ledger = field(default_factory=Ledger)

    def decide(self, c: Checkout) -> Decision:
        t0 = time.perf_counter()
        d = self._decide(c)
        d.micros = (time.perf_counter() - t0) * 1e6
        if d.outcome == APPROVE:                      # only approved carts consume budget
            m = c.mandate.mandate_id
            self.ledger.spent[m] = self.ledger.spent.get(m, 0) + c.cart.total_cents
            self.ledger.carts.add(c.cart.cart_id)
            self.ledger.carts_per_mandate[m] = self.ledger.carts_per_mandate.get(m, 0) + 1
        return d

    def _decide(self, c: Checkout) -> Decision:
        if self.use_checks:
            bad = integrity(c, self.user_keys, self.merchant_keys, self.ledger)
            if bad:
                return Decision(DECLINE, bad)
            out = scope(c, self.ledger)
            if out:
                return Decision(STEP_UP, out)          # the user can still authorise it explicitly
        risk = self.model.score(features(c, self.ledger)) if self.model else 0.0
        if risk >= self.decline_at:
            return Decision(DECLINE, ["behavioural_risk_high"], risk)
        if risk >= self.step_up_at:
            return Decision(STEP_UP, ["behavioural_risk_elevated"], risk)
        return Decision(APPROVE, [], risk)


# ----------------------------------------------------------------------------- economics
@dataclass(frozen=True)
class Costs:
    """Euro cost of each (truth, outcome) pair. Explicit so a risk team can argue with it."""
    margin: float = 0.25                # merchant margin lost on a false decline
    decline_friction_eur: float = 2.0
    step_up_abandon: float = 0.12       # honest shoppers who abandon when asked to confirm
    step_up_eur: float = 0.30
    chargeback_eur: float = 15.0
    step_up_stops_attack: float = 0.95  # an attacker without the user's device cannot confirm
    step_up_stops_ato: float = 0.40     # account takeover often controls the confirmation channel too
    dispute_share: float = 0.35         # share of unauthorised-but-benign spend that ends in a dispute


def cost_eur(kind: str, outcome: str, amount_cents: int, k: Costs = Costs()) -> float:
    a = amount_cents / 100.0
    if kind == "honest":
        return {APPROVE: 0.0, STEP_UP: k.step_up_eur + k.step_up_abandon * k.margin * a,
                DECLINE: k.decline_friction_eur + k.margin * a}[outcome]
    if kind == "confused":
        unauth = k.dispute_share * (a + k.chargeback_eur)
        return {APPROVE: unauth, STEP_UP: k.step_up_eur, DECLINE: k.decline_friction_eur}[outcome]
    stop = k.step_up_stops_ato if kind == "in_scope_fraud" else k.step_up_stops_attack
    loss = a + k.chargeback_eur
    return {APPROVE: loss, STEP_UP: k.step_up_eur + (1 - stop) * loss, DECLINE: 0.0}[outcome]
