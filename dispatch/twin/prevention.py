"""Failed-delivery prevention on the digital twin (spec §9, §10, §13 P3).

    prevention_report(days=30, orders_per_day=60, seed=7, threshold=0.1) -> dict

A synthetic customer base with hidden habits: how often they're away, whether they refuse cash on
delivery, how findable their address is. The same days are replayed under three policies with the
same random draws (common random numbers), so the only difference is the policy:

  none       no confirmations (the baseline)
  risk       confirm orders whose rules risk score is >= threshold (LookAtDisPatch before it has data)
  learned    like risk for the first `train_days`; then the `ml` provider's training runs on those
             days' outcomes, and if its model passes the same check as in production (beat the rules
             on the newest 20%), the model scores instead. It learns only from orders nobody
             messaged: a message changes the outcome, so a messaged order's label is biased. A random
             HOLDOUT share of risky orders isn't messaged and counts 1/HOLDOUT times in training
             (exploration), so the model still sees risky customers
  everyone   confirm every order (the upper bound on benefit, at the most messages)

`after_training` repeats each policy's numbers for the days after `train_days` only. `ranking_auc`
is the cleanest comparison of the two scores: on those days, how well each ranks the orders that
fail when nobody is messaged (the `none` policy's outcomes, which no confirmation has changed).
0.5 is a coin toss, 1.0 is perfect.

The risk score is `dispatch.decide.risk.estimate`, the same rules the live `delivery_risk`
decision uses, reading each customer's history as the policy saw it unfold. Outcomes feed history,
so the score sees more as days pass.

What a confirmation changes is an assumption (ASSUMPTIONS below), not a measurement. The point
of the twin is to compare policies under explicit assumptions, and to rerun the comparison when
real reply data replaces them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from dispatch.decide.ml import TASKS, LogisticModel, MlDecider, MlRegistry, Sample, auc, risk_vector
from dispatch.decide.risk import HIGH_VALUE_COD, estimate, risk_features

ASSUMPTIONS = {
    "reply_rate": 0.65,               # customers who answer a confirmation
    "away_reschedules": 0.6,          # an away-prone customer who replies picks a time they're home...
    "away_after_reschedule": 0.25,    # ...which leaves this share of their away risk
    "away_after_confirm": 0.6,        # replied "yes" without rescheduling: they plan around it a bit
    "after_driver_call": 0.8,         # no reply: the driver calls ahead (away and address risk)
    "directions_fix_address": 0.5,    # a reply with directions removes this share of address risk
    "refuser_cancels": 0.6,           # someone who'd refuse the COD parcel cancels when asked instead
}
HOLDOUT = 0.1
WRONG_ADDRESS_SCALE = 0.3             # P(address not found) = 0.3 x (1 - geocode confidence)


@dataclass
class _Customer:
    away: float
    refuses_cod: float
    geo: float
    cod_pref: float
    history: dict = field(default_factory=lambda: {"delivered": 0, "failed": 0, "refused": 0})


def _population(rng: random.Random, n: int) -> list[_Customer]:
    out = []
    for _ in range(n):
        hard = rng.random() < 0.15
        out.append(_Customer(
            away=rng.betavariate(3, 5) if hard else rng.betavariate(1.2, 18),
            refuses_cod=rng.betavariate(3, 4) if rng.random() < 0.10 else 0.02,
            geo=rng.uniform(0.3, 0.8) if rng.random() < 0.25 else rng.uniform(0.9, 1.0),
            cod_pref=0.8 if rng.random() < 0.4 else 0.1))
    return out


def _attempt(c: _Customer, cod: bool, high_value: bool, u: list[float], contacted: bool) -> str:
    """One delivery attempt: 'delivered', 'cancelled', or a failure reason. `u` are the shared draws."""
    a = ASSUMPTIONS
    p_away, p_wrong = c.away, WRONG_ADDRESS_SCALE * (1 - c.geo)
    p_refuse = min(1.0, c.refuses_cod * (1.5 if high_value else 1.0)) if cod else 0.0
    if contacted:
        if u[3] < a["reply_rate"]:
            if u[2] < p_refuse and u[4] < a["refuser_cancels"]:
                return "cancelled"
            p_away *= a["away_after_reschedule"] if u[5] < a["away_reschedules"] else a["away_after_confirm"]
            p_wrong *= 1 - a["directions_fix_address"]
        else:
            p_away *= a["after_driver_call"]
            p_wrong *= a["after_driver_call"]
    if u[1] < p_wrong:
        return "wrong_address"
    if u[0] < p_away:
        return "not_home"
    if u[2] < p_refuse:
        return "refused"
    return "delivered"


def _blank() -> dict:
    return {"orders": 0, "failed": 0, "cancelled": 0, "messages": 0, "reasons": {}}


def _summary(s: dict, base_failed: int) -> dict:
    return {**s, "failure_rate": round(s["failed"] / s["orders"], 4) if s["orders"] else 0.0,
            "message_rate": round(s["messages"] / s["orders"], 4) if s["orders"] else 0.0,
            "fewer_failures": round(1 - s["failed"] / base_failed, 4) if base_failed else 0.0}


def prevention_report(days: int = 30, orders_per_day: int = 60, seed: int = 7, threshold: float = 0.1,
                      customers: int = 300, refuse_threshold: float = 0.3, train_days: int = 10) -> dict:
    rng = random.Random(seed)
    pop = _population(rng, customers)
    for c in pop:                                        # some past orders, so history means something
        for _ in range(rng.choice([0, 0, 0, 1, 2, 3, 5, 8, 12])):
            cod = rng.random() < c.cod_pref
            r = _attempt(c, cod, False, [rng.random() for _ in range(6)], contacted=False)
            c.history["delivered" if r == "delivered" else "failed"] += 1
            c.history["refused"] += r == "refused"
    arms = ("none", "risk", "learned", "everyone")
    hist = {arm: [dict(c.history) for c in pop] for arm in arms}
    stats = {arm: _blank() for arm in arms}
    later = {arm: _blank() for arm in arms}
    samples: list[Sample] = []                       # the learned arm's unmessaged outcomes, for training
    model: LogisticModel | None = None               # fitted regardless, to compare rankings
    live = False                                     # did it pass the check and take over?
    check: dict = {}
    caught = baseline_failures = 0
    ranked: list[tuple[float, float, int]] = []      # (rules score, model score, failed with no checks)
    for day in range(days):
        if day == train_days and len({s.y for s in samples}) == 2:
            model = LogisticModel.fit([s.x for s in samples], [s.y for s in samples],
                                      weights=[s.weight for s in samples])
            provider, check = MlRegistry.train(TASKS["delivery_risk"], samples)
            check["why_not"] = getattr(provider, "reason", None)
            live = isinstance(provider, MlDecider)
            if live:
                model = provider.model
        for _ in range(orders_per_day):
            ci = rng.randrange(customers)
            c = pop[ci]
            cod = rng.random() < c.cod_pref
            amount = round(rng.lognormvariate(6.7, 0.8)) if cod else 0
            order = {"payment": "cod" if cod else "prepaid", "cod_amount": amount, "geo_confidence": c.geo,
                     "attempts": 0}
            u = [rng.random() for _ in range(7)]
            outcome_base = None
            if model is not None:                    # both scores on the same history (the none policy's)
                f0 = risk_features(order, hist["none"][ci])
                ranked.append((estimate(f0)[0], model.predict(risk_vector(f0)), -1))
            for arm in arms:
                if arm == "none":
                    contacted = False
                elif arm == "everyone":
                    contacted = True
                else:
                    feats = risk_features(order, hist[arm][ci])
                    p_fail, p_refuse = estimate(feats)
                    if arm == "learned" and live:
                        p_fail = model.predict(risk_vector(feats))
                    contacted = p_fail >= threshold or (cod and p_refuse >= refuse_threshold)
                    weight = 1.0
                    if arm == "learned" and contacted and u[6] < HOLDOUT:
                        contacted, weight = False, 1 / HOLDOUT   # exploration: unbiased label, stands for 1/HOLDOUT
                r = _attempt(c, cod, amount >= HIGH_VALUE_COD, u, contacted)
                h = hist[arm][ci]
                if arm == "learned" and r != "cancelled" and not contacted:
                    samples.append(Sample(risk_vector(feats), int(r != "delivered"), weight, estimate(feats)[0]))
                for s in (stats[arm], later[arm]) if day >= train_days else (stats[arm],):
                    s["orders"] += 1
                    s["messages"] += contacted
                    if r == "cancelled":
                        s["cancelled"] += 1
                    elif r != "delivered":
                        s["failed"] += 1
                        s["reasons"][r] = s["reasons"].get(r, 0) + 1
                if r == "delivered":
                    h["delivered"] += 1
                elif r != "cancelled":
                    h["failed"] += 1
                    h["refused"] += r == "refused"
                if arm == "none":
                    outcome_base = r
                elif arm == "risk" and outcome_base not in ("delivered", None):
                    caught += contacted
            baseline_failures += outcome_base not in ("delivered", None)
            if model is not None:
                ranked[-1] = (*ranked[-1][:2], int(outcome_base not in ("delivered", "cancelled", None)))
    out = {"days": days, "orders_per_day": orders_per_day, "seed": seed, "threshold": threshold,
           "train_days": train_days, "assumptions": dict(ASSUMPTIONS),
           "policies": {arm: _summary(stats[arm], stats["none"]["failed"]) for arm in arms},
           "after_training": {arm: _summary(later[arm], later["none"]["failed"]) for arm in arms},
           "model": ({"trained_on": model.n, "took_over": live, **check, "weights": model.weights()}
                     if model else None)}
    out["risk_recall"] = round(caught / baseline_failures, 4) if baseline_failures else 0.0
    out["ranking_auc"] = ({"rules": auc([a for a, _, _ in ranked], [y for *_, y in ranked]),
                           "learned": auc([b for _, b, _ in ranked], [y for *_, y in ranked])} if ranked else None)
    return out
