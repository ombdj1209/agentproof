from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from agentproof.engine import APPROVE, DECLINE, STEP_UP, Engine, cost_eur
from agentproof.mandate import Line
from agentproof.protocols import ADAPTERS
from agentproof.service import build_app
from agentproof.world import World


@pytest.fixture
def w():
    return World(seed=11)


def engine(w):
    return Engine(w.user_keys, w.merchant_keys)


def test_honest_in_scope_cart_is_approved(w):
    e = engine(w)
    for _ in range(50):
        a = w.honest()[0]
        assert e.decide(a.checkout).outcome == APPROVE


def test_forged_and_tampered_mandates_are_declined(w):
    a = w.honest()[0].checkout
    forged = replace(a, mandate=replace(a.mandate, signature="0" * 64))
    tampered = replace(a, mandate=replace(a.mandate, max_total_cents=a.mandate.max_total_cents * 10))
    for c in (forged, tampered):
        d = engine(w).decide(c)
        assert d.outcome == DECLINE and "mandate_signature_invalid" in d.reasons


def test_replayed_cart_is_declined_after_first_approval(w):
    a = w.honest()[0].checkout
    e = engine(w)
    assert e.decide(a).outcome == APPROVE
    d = e.decide(a)
    assert d.outcome == DECLINE and "cart_replayed" in d.reasons


def test_agent_binding(w):
    a = w.honest()[0].checkout
    other = replace(a, ctx=replace(a.ctx, presenting_agent="agent-99"))
    assert "agent_not_delegate" in engine(w).decide(other).reasons


def test_cumulative_spend_is_enforced(w):
    m = w.mandate()
    e = engine(w)
    outcomes = []
    for i in range(3):
        c = w.cart(m)
        l = c.lines[0]
        c = replace(c, lines=(replace(l, qty=1, unit_cents=int(m.max_total_cents * 0.45)),)).signed(
            w.merchant_keys[c.merchant_id])
        outcomes.append(e.decide(w.checkout(m, c, w.ctx(m.agent_id))))
    assert [o.outcome for o in outcomes[:2]] == [APPROVE, APPROVE]
    assert outcomes[2].outcome == STEP_UP and "cumulative_spend_above_limit" in outcomes[2].reasons


def test_merchant_signature_covers_cart_contents(w):
    a = w.honest()[0].checkout
    l = a.cart.lines[0]
    edited = replace(a, cart=replace(a.cart, lines=(replace(l, unit_cents=l.unit_cents + 1),)))
    assert "cart_signature_invalid" in engine(w).decide(edited).reasons


def test_scope_breach_is_stepped_up_not_silently_approved(w):
    for _ in range(40):
        a = w.confused()[0]
        assert engine(w).decide(a.checkout).outcome != APPROVE


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_protocol_roundtrip_is_exact(w, name):
    ad = ADAPTERS[name]
    for a in w.traffic(80):
        back = ad.decode(ad.encode(a.checkout))
        assert (back.mandate, back.cart, back.ctx) == (a.checkout.mandate, a.checkout.cart, a.checkout.ctx)


def test_every_protocol_gets_the_same_decisions(w):
    traffic = w.traffic(150)
    seqs = []
    for ad in ADAPTERS.values():
        e = engine(w)
        seqs.append([e.decide(ad.decode(ad.encode(a.checkout))).outcome for a in traffic])
    assert seqs[0] == seqs[1] == seqs[2]


def test_cost_model_orders_outcomes_sensibly():
    assert cost_eur("honest", APPROVE, 10000) < cost_eur("honest", STEP_UP, 10000) < cost_eur("honest", DECLINE, 10000)
    assert cost_eur("attack", DECLINE, 10000) < cost_eur("attack", STEP_UP, 10000) < cost_eur("attack", APPROVE, 10000)


def test_http_endpoint(w):
    client = TestClient(build_app(engine(w)))
    a = w.honest()[0].checkout
    for name, ad in ADAPTERS.items():
        a2 = replace(a, cart=replace(a.cart, cart_id=a.cart.cart_id + name).signed(w.merchant_keys[a.cart.merchant_id]))
        r = client.post(f"/v1/{name}/authorize", json=ad.encode(a2))
        assert r.status_code == 200 and r.json()["outcome"] == APPROVE
    assert client.post("/v1/session/authorize", json={"bad": 1}).status_code == 422
    assert client.post("/v1/nope/authorize", json={}).status_code == 404
