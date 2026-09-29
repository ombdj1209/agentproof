"""Training, threshold selection and evaluation helpers shared by run_eval.py and the tests."""
from __future__ import annotations

import itertools

import numpy as np

from .engine import (APPROVE, CARD_ERA_FEATURES, DECLINE, STEP_UP, Costs, Engine, Ledger, RiskModel,
                     cost_eur, features, integrity, scope)
from .world import World


def replay(world: World, attempts, engine: Engine):
    return [(a, engine.decide(a.checkout)) for a in attempts]


def behaviour_training_set(world: World, attempts):
    """Rows the behavioural model will actually see: attempts that pass integrity and scope."""
    led, X, y = Ledger(), [], []
    for a in attempts:
        c = a.checkout
        if integrity(c, world.user_keys, world.merchant_keys, led) or scope(c, led):
            continue
        X.append(features(c, led))
        y.append(1 if a.kind == "in_scope_fraud" else 0)
        if a.kind == "honest":
            m = c.mandate.mandate_id
            led.spent[m] = led.spent.get(m, 0) + c.cart.total_cents
            led.carts.add(c.cart.cart_id)
            led.carts_per_mandate[m] = led.carts_per_mandate.get(m, 0) + 1
    return X, y


def card_era_training_set(attempts):
    led, X, y = Ledger(), [], []
    for a in attempts:
        X.append(features(a.checkout, led))
        y.append(0 if a.kind == "honest" else 1)
    return X, y


def total_cost(results, k: Costs = Costs()) -> float:
    return float(sum(cost_eur(a.kind, d.outcome, a.checkout.cart.total_cents, k) for a, d in results))


def tune(make_engine, world: World, attempts, grid=np.linspace(0.05, 0.95, 19)):
    best = None
    for s, d in itertools.product(grid, grid):
        if d < s:
            continue
        e = make_engine(float(s), float(d))
        c = total_cost(replay(world, attempts, e))
        if best is None or c < best[0]:
            best = (c, float(s), float(d))
    return best


def summarise(results) -> dict:
    by = {}
    for a, d in results:
        for key in (a.kind, f"{a.kind}/{a.variant}"):
            row = by.setdefault(key, {APPROVE: 0, STEP_UP: 0, DECLINE: 0, "n": 0})
            row[d.outcome] += 1
            row["n"] += 1
    out = {}
    for k, v in sorted(by.items()):
        n = v["n"]
        out[k] = {"n": n, "approve": v[APPROVE] / n, "step_up": v[STEP_UP] / n, "decline": v[DECLINE] / n}
    return out
