"""Three mock protocol adapters with deliberately different wire shapes.

They are modelled loosely on how public agentic-commerce protocols differ (session-style checkout,
order-style payloads, and mandate-chain payloads) but they are NOT spec-conformant implementations.
The point is architectural: every protocol decodes to one canonical Checkout, so risk logic is written
once and a conformance suite can prove that the same purchase gets the same decision on every rail.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .mandate import CartMandate, Checkout, Context, IntentMandate, Line


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unix(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _dec(cents: int) -> str:
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def _cents(s: str) -> int:
    return int((Decimal(s) * 100).to_integral_value())


def _mandate_fields(m: IntentMandate) -> dict:
    return {"mandate_id": m.mandate_id, "user_id": m.user_id, "agent_id": m.agent_id,
            "merchants": list(m.merchants), "categories": list(m.categories),
            "max_total_cents": m.max_total_cents, "max_qty_per_item": m.max_qty_per_item,
            "currency": m.currency, "issued_at": m.issued_at, "expires_at": m.expires_at,
            "allow_substitution": m.allow_substitution, "signature": m.signature}


def _mandate_from(d: dict) -> IntentMandate:
    return IntentMandate(d["mandate_id"], d["user_id"], d["agent_id"], tuple(d["merchants"]),
                         tuple(d["categories"]), int(d["max_total_cents"]), int(d["max_qty_per_item"]),
                         d["currency"], int(d["issued_at"]), int(d["expires_at"]),
                         bool(d["allow_substitution"]), d["signature"])


def _ctx_from(d: dict, agent_key: str) -> Context:
    return Context(d[agent_key], bool(d["new_device"]), bool(d["new_ship_address"]),
                   float(d["ip_risk"]), float(d["agent_reputation"]))


class SessionStyle:
    """Checkout-session shape: decimal-string amounts, ISO timestamps, delegated payment block."""
    name = "session"

    @staticmethod
    def encode(c: Checkout) -> dict:
        m, cart, x = c.mandate, c.cart, c.ctx
        return {
            "checkout_session": {
                "id": cart.cart_id, "merchant": {"id": cart.merchant_id}, "currency": cart.currency.lower(),
                "created": _iso(cart.created_at), "merchant_signature": cart.signature,
                "line_items": [{"product_id": l.sku, "product_category": l.category, "quantity": l.qty,
                                "unit_amount": _dec(l.unit_cents)} for l in cart.lines]},
            "delegated_payment": {"mandate": {**_mandate_fields(m), "max_total": _dec(m.max_total_cents),
                                              "issued_at": _iso(m.issued_at), "expires_at": _iso(m.expires_at)}},
            "agent": {"id": x.presenting_agent, "reputation": x.agent_reputation},
            "risk_signals": {"new_device": x.new_device, "new_ship_address": x.new_ship_address, "ip_risk": x.ip_risk},
        }

    @staticmethod
    def decode(p: dict) -> Checkout:
        s, md = p["checkout_session"], dict(p["delegated_payment"]["mandate"])
        md["max_total_cents"] = _cents(md.pop("max_total"))
        md["issued_at"], md["expires_at"] = _unix(md["issued_at"]), _unix(md["expires_at"])
        cart = CartMandate(s["id"], md["mandate_id"], s["merchant"]["id"],
                           tuple(Line(i["product_id"], i["product_category"], int(i["quantity"]),
                                      _cents(i["unit_amount"])) for i in s["line_items"]),
                           s["currency"].upper(), _unix(s["created"]), s["merchant_signature"])
        ctx = Context(p["agent"]["id"], p["risk_signals"]["new_device"], p["risk_signals"]["new_ship_address"],
                      p["risk_signals"]["ip_risk"], p["agent"]["reputation"])
        return Checkout("session", _mandate_from(md), cart, ctx)


class OrderStyle:
    """Order shape: amounts as {value (minor units), currency}, camelCase, unix timestamps."""
    name = "order"

    @staticmethod
    def encode(c: Checkout) -> dict:
        m, cart, x = c.mandate, c.cart, c.ctx
        return {
            "order": {"orderRef": cart.cart_id, "merchantId": cart.merchant_id, "createdAt": cart.created_at,
                      "sig": cart.signature,
                      "items": [{"sku": l.sku, "cat": l.category, "qty": l.qty,
                                 "price": {"value": l.unit_cents, "currency": cart.currency}} for l in cart.lines]},
            "authorization": {"intent": {
                "id": m.mandate_id, "principal": m.user_id, "delegate": m.agent_id,
                "scope": {"merchants": list(m.merchants), "categories": list(m.categories),
                          "maxQtyPerItem": m.max_qty_per_item, "substitutions": m.allow_substitution},
                "limit": {"value": m.max_total_cents, "currency": m.currency},
                "validity": {"from": m.issued_at, "until": m.expires_at}, "proof": m.signature}},
            "client": {"agentId": x.presenting_agent, "reputation": x.agent_reputation, "newDevice": x.new_device,
                       "newShipAddress": x.new_ship_address, "ipRisk": x.ip_risk},
        }

    @staticmethod
    def decode(p: dict) -> Checkout:
        o, i, cl = p["order"], p["authorization"]["intent"], p["client"]
        m = IntentMandate(i["id"], i["principal"], i["delegate"], tuple(i["scope"]["merchants"]),
                          tuple(i["scope"]["categories"]), int(i["limit"]["value"]), int(i["scope"]["maxQtyPerItem"]),
                          i["limit"]["currency"], int(i["validity"]["from"]), int(i["validity"]["until"]),
                          bool(i["scope"]["substitutions"]), i["proof"])
        cur = o["items"][0]["price"]["currency"] if o["items"] else m.currency
        cart = CartMandate(o["orderRef"], m.mandate_id, o["merchantId"],
                           tuple(Line(it["sku"], it["cat"], int(it["qty"]), int(it["price"]["value"])) for it in o["items"]),
                           cur, int(o["createdAt"]), o["sig"])
        ctx = Context(cl["agentId"], cl["newDevice"], cl["newShipAddress"], cl["ipRisk"], cl["reputation"])
        return Checkout("order", m, cart, ctx)


class MandateChainStyle:
    """Mandate-chain shape: intent mandate, cart mandate and payment mandate travel as separate objects."""
    name = "chain"

    @staticmethod
    def encode(c: Checkout) -> dict:
        m, cart, x = c.mandate, c.cart, c.ctx
        return {
            "intent_mandate": _mandate_fields(m),
            "cart_mandate": {"cart_id": cart.cart_id, "mandate_ref": cart.mandate_id, "merchant": cart.merchant_id,
                             "currency": cart.currency, "created_at": _iso(cart.created_at),
                             "contents": [[l.sku, l.category, l.qty, l.unit_cents] for l in cart.lines],
                             "merchant_authorization": cart.signature},
            "payment_mandate": {"agent_id": x.presenting_agent, "agent_reputation": x.agent_reputation,
                                "new_device": x.new_device, "new_ship_address": x.new_ship_address, "ip_risk": x.ip_risk},
        }

    @staticmethod
    def decode(p: dict) -> Checkout:
        cm = p["cart_mandate"]
        cart = CartMandate(cm["cart_id"], cm["mandate_ref"], cm["merchant"],
                           tuple(Line(s, c, int(q), int(u)) for s, c, q, u in cm["contents"]),
                           cm["currency"], _unix(cm["created_at"]), cm["merchant_authorization"])
        return Checkout("chain", _mandate_from(p["intent_mandate"]), cart, _ctx_from(p["payment_mandate"], "agent_id"))


ADAPTERS = {a.name: a for a in (SessionStyle, OrderStyle, MandateChainStyle)}


def decode(protocol: str, payload: dict) -> Checkout:
    try:
        return ADAPTERS[protocol].decode(payload)
    except KeyError as e:
        raise ValueError(f"malformed {protocol} payload: missing {e}") from e
