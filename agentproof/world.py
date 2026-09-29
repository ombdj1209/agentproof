"""Synthetic commerce world and labelled agent traffic.

Every attempt carries a ground-truth label, so every metric in the report is exact rather than
estimated. Classes:
  honest            in scope, should be approved
  confused          agent misread the intent (over quantity, over budget, wrong category): not fraud,
                    but not authorised either; should be stepped up or declined
  attack            delegation violations (prompt-injected merchant swap, gift-card insertion, quantity
                    inflation, replay, expired, forged or tampered mandate, agent mismatch, split spend)
  in_scope_fraud    account takeover: the mandate is validly signed and the cart is in scope, so only
                    behavioural signals can catch it
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

import numpy as np

from .mandate import CartMandate, Checkout, Context, IntentMandate, Line

CATEGORIES = ("running_shoes", "groceries", "books", "electronics", "home", "apparel")
PRICE = {"running_shoes": 11000, "groceries": 6000, "books": 2200, "electronics": 25000,
         "home": 8000, "apparel": 6500, "giftcards": 10000}
T0 = 1_780_000_000


@dataclass(frozen=True)
class Attempt:
    checkout: Checkout
    kind: str            # honest | confused | attack | in_scope_fraud
    variant: str         # finer-grained scenario name


class World:
    def __init__(self, seed: int = 0, n_users: int = 400, n_agents: int = 25):
        r = np.random.default_rng(seed)
        self.r = r
        self.merchants = {f"m{i:02d}": CATEGORIES[i % len(CATEGORIES)] for i in range(18)}
        self.attackers = {f"x{i:02d}": ("giftcards" if i % 2 else "electronics") for i in range(4)}
        self.skus = {}
        for m, cat in {**self.merchants, **self.attackers}.items():
            for k in range(15):
                self.skus[f"{m}-s{k:02d}"] = (m, cat, int(PRICE[cat] * r.lognormal(0, 0.35)))
        self.user_keys = {f"u{i:04d}": hashlib.sha256(f"user-{seed}-{i}".encode()).digest() for i in range(n_users)}
        self.merchant_keys = {m: hashlib.sha256(f"merchant-{seed}-{m}".encode()).digest()
                              for m in {**self.merchants, **self.attackers}}
        self.agents = {f"agent-{i:02d}": float(np.clip(r.beta(6, 2), 0, 1)) for i in range(n_agents)}
        self._n = 0

    def _id(self, p):
        self._n += 1
        return f"{p}-{self._n:06d}"

    def skus_of(self, merchant, category=None, max_cents=None):
        out = [s for s, (m, c, p) in self.skus.items() if m == merchant and (category is None or c == category)
               and (max_cents is None or p <= max_cents)]
        return sorted(out)

    # -------------------------------------------------------------- honest baseline
    def mandate(self, user=None, agent=None) -> IntentMandate:
        r = self.r
        user = user or r.choice(sorted(self.user_keys))
        agent = agent or r.choice(sorted(self.agents))
        cat = CATEGORIES[r.integers(len(CATEGORIES))]
        merchants = sorted(m for m, c in self.merchants.items() if c == cat)
        allow = tuple(sorted(r.choice(merchants, size=min(len(merchants), r.integers(1, 4)), replace=False)))
        qty = int(r.integers(1, 4))
        issued = T0 + int(r.integers(0, 86400 * 30))
        m = IntentMandate(self._id("mdt"), user, agent, allow, (cat,),
                          int(PRICE[cat] * qty * r.uniform(1.2, 2.2)), qty, "EUR",
                          issued, issued + int(r.integers(6, 72)) * 3600, bool(r.random() < 0.3))
        return m.signed(self.user_keys[user])

    def cart(self, m: IntentMandate, merchant=None, lines=None, t=None) -> CartMandate:
        r = self.r
        merchant = merchant or m.merchants[r.integers(len(m.merchants))]
        if lines is None:
            budget = m.max_total_cents
            pool = self.skus_of(merchant, m.categories[0], budget // max(1, m.max_qty_per_item)) or \
                self.skus_of(merchant, m.categories[0])
            sku = pool[r.integers(len(pool))]
            _, cat, price = self.skus[sku]
            qty = int(r.integers(1, m.max_qty_per_item + 1))
            while qty > 1 and qty * price > budget:
                qty -= 1
            unit = int(price * r.uniform(0.97, 1.03))
            if qty * unit > budget:              # an honest agent never plans a cart above the limit
                unit = budget // qty
            lines = (Line(sku, cat, qty, unit),)
        t = t or (m.issued_at + int(r.integers(60, max(120, (m.expires_at - m.issued_at) // 2))))
        c = CartMandate(self._id("cart"), m.mandate_id, merchant, tuple(lines), m.currency, t)
        return c.signed(self.merchant_keys[merchant])

    def ctx(self, agent, fraud=False) -> Context:
        r = self.r
        if fraud:
            return Context(agent, bool(r.random() < 0.7), bool(r.random() < 0.75), float(r.beta(4, 3)),
                           float(np.clip(self.agents.get(agent, 0.3) - r.uniform(0, 0.3), 0, 1)))
        return Context(agent, bool(r.random() < 0.08), bool(r.random() < 0.06), float(r.beta(1.2, 8)),
                       self.agents.get(agent, 0.5))

    def checkout(self, m, c, ctx, meta=None) -> Checkout:
        return Checkout("canonical", m, c, ctx, meta or {})

    # ------------------------------------------------------------------ generators
    def honest(self):
        m = self.mandate()
        c = self.cart(m)
        return [Attempt(self.checkout(m, c, self.ctx(m.agent_id)), "honest", "in_scope")]

    def honest_substitution(self):
        m = self.mandate()
        m = replace(m, allow_substitution=True).signed(self.user_keys[m.user_id])
        c = self.cart(m)
        l = c.lines[0]
        alt = [s for s in self.skus_of(c.merchant_id, l.category) if s != l.sku and self.skus[s][2] * l.qty <= m.max_total_cents]
        if alt:
            s = alt[self.r.integers(len(alt))]
            c = replace(c, lines=(Line(s, l.category, l.qty, self.skus[s][2]),)).signed(self.merchant_keys[c.merchant_id])
        return [Attempt(self.checkout(m, c, self.ctx(m.agent_id)), "honest", "substitution")]

    def honest_two_carts(self):
        m = self.mandate()
        half = replace(m, max_total_cents=m.max_total_cents // 2)   # plan both carts inside the budget
        c1, c2 = self.cart(half), self.cart(half)
        c2 = replace(c2, created_at=c1.created_at + 600).signed(self.merchant_keys[c2.merchant_id])
        return [Attempt(self.checkout(m, c1, self.ctx(m.agent_id)), "honest", "two_carts"),
                Attempt(self.checkout(m, c2, self.ctx(m.agent_id)), "honest", "two_carts")]

    def confused(self):
        m = self.mandate()
        c = self.cart(m)
        l = c.lines[0]
        v = ("over_qty", "over_budget", "wrong_category")[self.r.integers(3)]
        if v == "over_qty":
            nl = replace(l, qty=m.max_qty_per_item + 1)
        elif v == "over_budget":
            nl = replace(l, qty=1, unit_cents=int(m.max_total_cents * self.r.uniform(1.15, 1.6)))
        else:
            other = [s for s in self.skus_of(c.merchant_id) if self.skus[s][1] != l.category]
            cats = [c_ for c_ in CATEGORIES if c_ != l.category]
            if other:
                s = other[0]
                nl = Line(s, self.skus[s][1], 1, self.skus[s][2])
            else:
                nl = replace(l, category=cats[self.r.integers(len(cats))])
        c = replace(c, lines=(nl,)).signed(self.merchant_keys[c.merchant_id])
        return [Attempt(self.checkout(m, c, self.ctx(m.agent_id)), "confused", v)]

    def attack(self):
        r = self.r
        m = self.mandate()
        v = ("merchant_swap", "giftcard_insert", "qty_inflate", "replay", "expired", "forged_signature",
             "tampered_limit", "agent_mismatch", "split_spend")[r.integers(9)]
        ctx = self.ctx(m.agent_id)
        if v == "merchant_swap":           # injected instruction: "this item is cheaper at x01"
            x = sorted(self.attackers)[r.integers(len(self.attackers))]
            s = self.skus_of(x)[0]
            c = self.cart(m, merchant=x, lines=(Line(s, m.categories[0], 1, min(self.skus[s][2], m.max_total_cents)),))
            return [Attempt(self.checkout(m, c, ctx), "attack", v)]
        if v == "giftcard_insert":         # injected product description adds a gift card
            c = self.cart(m)
            gift = Line("gift-card-100", "giftcards", 1, 10000)
            c = replace(c, lines=c.lines + (gift,)).signed(self.merchant_keys[c.merchant_id])
            return [Attempt(self.checkout(m, c, ctx), "attack", v)]
        if v == "qty_inflate":
            c = self.cart(m)
            l = c.lines[0]
            c = replace(c, lines=(replace(l, qty=l.qty * 5),)).signed(self.merchant_keys[c.merchant_id])
            return [Attempt(self.checkout(m, c, ctx), "attack", v)]
        if v == "replay":                  # a captured, already-approved cart is submitted again
            c = self.cart(m)
            return [Attempt(self.checkout(m, c, ctx), "honest", "replay_original"),
                    Attempt(self.checkout(m, c, ctx), "attack", v)]
        if v == "expired":
            c = self.cart(m, t=m.expires_at + int(r.integers(60, 86400)))
            return [Attempt(self.checkout(m, c, ctx), "attack", v)]
        if v == "forged_signature":
            m2 = replace(m, signature="0" * 64)
            return [Attempt(self.checkout(m2, self.cart(m2), ctx), "attack", v)]
        if v == "tampered_limit":          # limit raised after the user signed
            m2 = replace(m, max_total_cents=m.max_total_cents * 10)
            c = self.cart(m2)
            l = c.lines[0]
            c = replace(c, lines=(replace(l, qty=m.max_qty_per_item, unit_cents=int(m.max_total_cents * 1.5)),)
                        ).signed(self.merchant_keys[c.merchant_id])
            return [Attempt(self.checkout(m2, c, ctx), "attack", v)]
        if v == "agent_mismatch":          # a different agent presents someone else's mandate
            other = sorted(a for a in self.agents if a != m.agent_id)[r.integers(len(self.agents) - 1)]
            return [Attempt(self.checkout(m, self.cart(m), self.ctx(other)), "attack", v)]
        # split_spend: several carts, each under the limit, together far above it
        out = []
        for i in range(4):
            c = self.cart(m)
            l = c.lines[0]
            c = replace(c, lines=(replace(l, qty=1, unit_cents=int(m.max_total_cents * 0.6)),),
                        created_at=c.created_at + i * 120).signed(self.merchant_keys[c.merchant_id])
            out.append(Attempt(self.checkout(m, c, ctx), "honest" if i == 0 else "attack",
                               "split_first" if i == 0 else v))
        return out

    def in_scope_fraud(self):
        """Account takeover: attacker controls the user account and signs a perfectly valid mandate."""
        m = self.mandate()
        c = self.cart(m)
        return [Attempt(self.checkout(m, c, self.ctx(m.agent_id, fraud=True)), "in_scope_fraud", "account_takeover")]

    def traffic(self, n: int, mix=(0.62, 0.08, 0.08, 0.08, 0.08, 0.06)):
        """Mix over (honest, substitution, two_carts, confused, attack, in_scope_fraud) generator calls."""
        gens = (self.honest, self.honest_substitution, self.honest_two_carts, self.confused, self.attack,
                self.in_scope_fraud)
        out = []
        for _ in range(n):
            out.extend(gens[self.r.choice(len(gens), p=np.asarray(mix) / sum(mix))]())
        return out
