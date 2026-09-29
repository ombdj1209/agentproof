"""Reproduces every number in the README.

    python run_eval.py            # full
    python run_eval.py --quick    # smaller, for CI
"""
from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path

import numpy as np

from agentproof.engine import APPROVE, CARD_ERA_FEATURES, Costs, Engine, RiskModel
from agentproof.lab import (behaviour_training_set, card_era_training_set, replay, summarise, total_cost,
                            tune)
from agentproof.protocols import ADAPTERS
from agentproof.report import write_report
from agentproof.world import World

OUT = Path(__file__).resolve().parent / "results"


def main(quick: bool):
    t_start = time.time()
    n_train, n_val, n_test = (600, 400, 600) if quick else (3000, 1200, 3000)
    grid = np.linspace(0.05, 0.95, 10)

    # separate worlds: no key, user or mandate overlap between train, validation and test
    wtr, wva, wte = World(seed=1), World(seed=2), World(seed=3)
    train, val, test = wtr.traffic(n_train), wva.traffic(n_val), wte.traffic(n_test)

    beh = RiskModel().fit(*behaviour_training_set(wtr, train))
    card = RiskModel(cols=list(CARD_ERA_FEATURES)).fit(*card_era_training_set(train))

    variants = {
        "card_era_model": lambda w, s, d: Engine(w.user_keys, w.merchant_keys, card, s, d, use_checks=False),
        "delegation_checks_only": lambda w, s, d: Engine(w.user_keys, w.merchant_keys, None, s, d),
        "layered": lambda w, s, d: Engine(w.user_keys, w.merchant_keys, beh, s, d),
    }
    results, tuned = {}, {}
    for name, mk in variants.items():
        if name == "delegation_checks_only":
            s, d = 1.0, 1.0
        else:
            _, s, d = tune(lambda s_, d_: mk(wva, s_, d_), wva, val, grid)
        tuned[name] = {"step_up_at": s, "decline_at": d}
        res = replay(wte, test, mk(wte, s, d))
        attacks = [(a, x) for a, x in res if a.kind in ("attack", "in_scope_fraud")]
        honest = [(a, x) for a, x in res if a.kind == "honest"]
        confused = [(a, x) for a, x in res if a.kind == "confused"]
        approved_bad = sum(a.checkout.cart.total_cents for a, x in attacks if x.outcome == APPROVE) / 100
        micros = [x.micros for _, x in res]
        results[name] = {
            "thresholds": tuned[name],
            "cost_eur_per_1000": 1000 * total_cost(res) / len(res),
            "attack_approved_rate": float(np.mean([x.outcome == APPROVE for a, x in attacks])),
            "attack_value_approved_eur": approved_bad,
            "honest_approved_rate": float(np.mean([x.outcome == APPROVE for a, x in honest])),
            "honest_declined_rate": float(np.mean([x.outcome == "decline" for a, x in honest])),
            "confused_approved_rate": float(np.mean([x.outcome == APPROVE for a, x in confused])),
            "latency_us_p50": float(np.percentile(micros, 50)),
            "latency_us_p99": float(np.percentile(micros, 99)),
            "by_class": summarise(res),
        }
        print(f"{name:24s} cost/1000={results[name]['cost_eur_per_1000']:9.1f}  "
              f"attack approved={results[name]['attack_approved_rate']:.1%}  "
              f"honest approved={results[name]['honest_approved_rate']:.1%}")

    # cross-protocol conformance: same traffic through every adapter -> identical decisions
    s, d = tuned["layered"]["step_up_at"], tuned["layered"]["decline_at"]
    per_proto, roundtrip_ok = {}, 0
    for pname, adapter in ADAPTERS.items():
        eng = Engine(wte.user_keys, wte.merchant_keys, beh, s, d)
        outs = []
        for a in test:
            back = adapter.decode(adapter.encode(a.checkout))
            roundtrip_ok += (back.mandate, back.cart, back.ctx) == (a.checkout.mandate, a.checkout.cart, a.checkout.ctx)
            outs.append(eng.decide(back).outcome)
        per_proto[pname] = outs
    names = sorted(per_proto)
    agree = float(np.mean([len({per_proto[p][i] for p in names}) == 1 for i in range(len(test))]))
    conformance = {"protocols": names, "attempts": len(test), "decision_agreement": agree,
                   "roundtrip_exact": roundtrip_ok / (len(test) * len(names))}
    print("conformance", conformance)

    out = {"environment": {"python": platform.python_version(), "machine": platform.machine(),
                           "runtime_s": round(time.time() - t_start, 1), "quick": quick},
           "traffic": {"train": len(train), "validation": len(val), "test": len(test),
                       "test_mix": {k: sum(1 for a in test if a.kind == k)
                                    for k in ("honest", "confused", "attack", "in_scope_fraud")}},
           "costs": Costs().__dict__, "engines": results, "conformance": conformance}
    OUT.mkdir(exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(out, indent=2))
    write_report(out, OUT / "report.html")
    print(f"done in {time.time() - t_start:.0f}s -> results/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    main(ap.parse_args().quick)
