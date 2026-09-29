"""HTTP surface: one endpoint per protocol, one engine behind all of them.

    uvicorn agentproof.service:app --port 8000
    curl -XPOST localhost:8000/v1/chain/authorize -d @payload.json
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .engine import Engine
from .protocols import ADAPTERS, decode


def build_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="Agentproof", version="0.1.0")

    @app.get("/v1/protocols")
    def protocols():
        return sorted(ADAPTERS)

    @app.post("/v1/{protocol}/authorize")
    def authorize(protocol: str, payload: dict):
        if protocol not in ADAPTERS:
            raise HTTPException(404, f"unknown protocol {protocol}")
        try:
            checkout = decode(protocol, payload)
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
        d = engine.decide(checkout)
        return {"outcome": d.outcome, "reasons": d.reasons, "risk": round(d.risk, 4),
                "cart_id": checkout.cart.cart_id, "mandate_id": checkout.mandate.mandate_id}

    return app


def _default_app() -> FastAPI:
    from .world import World
    w = World(seed=0)
    return build_app(Engine(w.user_keys, w.merchant_keys))


app = _default_app()
