"""Rules for the failed-delivery decisions (spec §7.1, §10): pure functions, shared by the
`rules` provider and the digital twin.

Delivery risk
  risk_features(order, customer) -> the numbers the score reads (history, payment, address quality)
  estimate(features)             -> (p_fail, p_refuse): a hand-set logistic model, used until the
                                    `ml` provider has 500 labelled outcomes to train on
  levels_for(p) / p_from_levels  -> map P(fail) to the 5-level Score answer and back, so every
                                    provider (rules, ml, llm, jev) reports risk the same way

Failed-attempt verify
  attempt_rule(state, questions) -> genuine + reason from the geofence (driver distance from the
                                    address) and time at the door; None when the numbers can't settle it
"""

from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------- delivery risk
RISK_LEVELS = [
    "Very unlikely to fail (under 5%)",
    "Unlikely to fail (5 to 15%)",
    "Possible failure (15 to 30%)",
    "Likely to fail (30 to 50%)",
    "Very likely to fail (over 50%)",
]
LEVEL_P = [0.025, 0.10, 0.22, 0.40, 0.70]      # representative P(fail) per level
HIGH_VALUE_COD = 2000                          # rupees


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


PRIOR_WEIGHT = 3        # past orders' worth of trust in the prior (Beta-binomial smoothing)


def risk_features(order: dict[str, Any], history: dict[str, int]) -> dict[str, float]:
    """Features from an order (model_dump) and its customer's history."""
    delivered, failed, refused = history.get("delivered", 0), history.get("failed", 0), history.get("refused", 0)
    cod = order.get("payment") == "cod"
    geo = order.get("geo_confidence")
    return {
        "past_orders": float(delivered + failed),
        "past_failed": float(failed),
        "past_refused": float(refused),
        "cod": float(cod),
        "high_value_cod": float(cod and order.get("cod_amount", 0) >= HIGH_VALUE_COD),
        "address_doubt": 1 - geo if geo is not None else 0.5,
        "reattempt": float(order.get("attempts", 0) > 0),
    }


def estimate(f: dict[str, float]) -> tuple[float, float]:
    """(P(fail), P(refuses COD)). A prior from the order (payment, value, address quality, reattempt),
    updated by the customer's own record as if the prior were PRIOR_WEIGHT past orders. A regular
    prepaid customer with a good address scores ~4%; a first-time COD order on a doubtful address ~25%;
    someone who failed 3 of their last 5 orders ~45%."""
    prior = _sigmoid(-2.9 + 0.6 * f["cod"] + 0.4 * f["high_value_cod"] + 2.2 * f["address_doubt"]
                     + 0.7 * f["reattempt"] + 0.4 * (f["past_orders"] == 0))
    p_fail = (f["past_failed"] + PRIOR_WEIGHT * prior) / (f["past_orders"] + PRIOR_WEIGHT)
    p_refuse = 0.0
    if f["cod"]:
        prior_r = _sigmoid(-3.0 + 0.7 * f["high_value_cod"] + 0.6 * (f["past_orders"] == 0))
        p_refuse = (f["past_refused"] + PRIOR_WEIGHT * prior_r) / (f["past_orders"] + PRIOR_WEIGHT)
    return p_fail, p_refuse


def levels_for(p: float) -> list[float]:
    """A distribution over RISK_LEVELS whose expected P(fail) (via LEVEL_P) equals p, clamped to the
    representable range: mass split between the two adjacent levels."""
    p = min(max(p, LEVEL_P[0]), LEVEL_P[-1])
    out = [0.0] * len(LEVEL_P)
    for i in range(len(LEVEL_P) - 1):
        lo, hi = LEVEL_P[i], LEVEL_P[i + 1]
        if lo <= p <= hi:
            w = (p - lo) / (hi - lo)
            out[i], out[i + 1] = 1 - w, w
            return out
    out[-1] = 1.0                                           # unreachable after the clamp
    return out


def p_from_levels(probabilities: list[float]) -> float:
    return sum(p * v for p, v in zip(probabilities, LEVEL_P))


def risk_rule(state, questions):
    """Always fires: the hand-set model until a trained one replaces it (chain `ml -> rules`)."""
    from dispatch.core.decisions import NoulAnswer, ScoreAnswer
    p_fail, p_refuse = estimate(state["features"])
    return {"will_fail": ScoreAnswer.from_probabilities(levels_for(p_fail)),
            "refuse_risk_cod": NoulAnswer(p=p_refuse)}


# ---------------------------------------------------------------- failed-attempt verify
FAIL_REASONS = {
    "not_home": "Nobody was there to receive it: not home, door locked, phone not answered",
    "refused": "The customer refused the parcel or would not pay cash on delivery",
    "wrong_address": "The address was wrong, incomplete or could not be found",
    "unsafe": "The driver could not reach the door safely: dogs, flooding, a blocked lane",
    "other": "Any other reason",
}
_KEYWORDS = {
    "refused": ("refus", "reject", "didn't want", "did not want", "doesn't want", "won't pay", "no money",
                "not paying", "cancel at door"),
    "wrong_address": ("wrong address", "address not found", "can't find", "cannot find", "couldn't find",
                      "could not find", "no such", "incomplete address", "address wrong"),
    "unsafe": ("unsafe", "dog", "flood", "waterlog", "danger", "blocked lane"),
    "not_home": ("not home", "no one", "nobody", "locked", "not available", "no answer", "not answering",
                 "unreachable", "switched off", "not picking", "away"),
}
GEOFENCE_OK_M = 150          # within this of the address: the driver was there
GEOFENCE_FAR_M = 400         # beyond this: the driver was not at the address
MIN_DWELL_S = 60             # a real attempt takes at least a minute at the door


def reason_from_text(text: str) -> str:
    t = text.lower()
    for reason, words in _KEYWORDS.items():
        if any(w in t for w in words):
            return reason
    return "other"


def attempt_rule(state, questions):
    """Geofence and dwell time settle most attempts; anything in between goes on down the chain."""
    from dispatch.decide.deciders.rules import certain
    d, dwell = state.get("distance_m"), state.get("dwell_s")
    reason = reason_from_text(state.get("reason_text", ""))
    if d is not None and d > GEOFENCE_FAR_M:
        genuine = False
    elif d is not None and d <= GEOFENCE_OK_M and dwell is not None and dwell >= MIN_DWELL_S:
        genuine = True
    else:
        return None
    return {"genuine": certain(questions["genuine"], genuine), "reason": certain(questions["reason"], reason)}
