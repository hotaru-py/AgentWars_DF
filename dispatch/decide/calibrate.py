"""Calibration (spec §7.3): raw provider confidence -> probability the decision was right.

Per (decision type, provider), fitted on decision-ledger rows whose outcome is "correct" or
"incorrect" (the learner and humans set outcomes). Isotonic regression (pool-adjacent-violators),
so higher raw confidence never maps to lower calibrated confidence. Below MIN_OUTCOMES labelled
rows the identity map is used and gates use the stricter STRICT_ACT threshold (spec §6.2).
"""

from __future__ import annotations

from bisect import bisect_left

from dispatch.core.models import GateThresholds

MIN_OUTCOMES = 200
STRICT_ACT = 0.95
CORRECT, INCORRECT = "correct", "incorrect"


class Calibrator:
    def __init__(self, xs: list[float] | None = None, ys: list[float] | None = None, n: int = 0):
        self.xs, self.ys, self.n = xs or [], ys or [], n       # step function knots (upper bounds) and values

    @property
    def fitted(self) -> bool:
        return self.n >= MIN_OUTCOMES and bool(self.xs)

    @classmethod
    def fit(cls, pairs: list[tuple[float, bool]]) -> "Calibrator":
        if len(pairs) < MIN_OUTCOMES:
            return cls(n=len(pairs))
        by_x: dict[float, list[float]] = {}                    # group identical confidences first, so
        for x, y in pairs:                                     # ties are pooled, not ordered arbitrarily
            acc = by_x.setdefault(x, [0.0, 0.0])
            acc[0] += float(y)
            acc[1] += 1.0
        blocks: list[list[float]] = []                         # [sum_y, count, max_x]
        for x in sorted(by_x):
            blocks.append([by_x[x][0], by_x[x][1], x])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
                s, c, mx = blocks.pop()
                blocks[-1][0] += s
                blocks[-1][1] += c
                blocks[-1][2] = mx
        return cls([b[2] for b in blocks], [b[0] / b[1] for b in blocks], len(pairs))

    def __call__(self, raw: float) -> float:
        if not self.fitted:
            return raw
        i = bisect_left(self.xs, raw)                          # first block whose upper edge >= raw
        return self.ys[min(i, len(self.ys) - 1)]


def effective_gate(base: GateThresholds, cal: Calibrator) -> GateThresholds:
    """Until a provider is calibrated for this decision type, it must be surer to act alone."""
    if cal.fitted:
        return base
    return GateThresholds(act=max(base.act, STRICT_ACT), review=base.review)


def fit_from_ledger(store, decision_type: str, provider: str, limit: int = 5000) -> Calibrator:
    rows = [d for d in store.decisions(type_=decision_type, limit=limit)
            if d.decider == provider and d.outcome in (CORRECT, INCORRECT)]
    return Calibrator.fit([(d.raw_confidence, d.outcome == CORRECT) for d in rows])
