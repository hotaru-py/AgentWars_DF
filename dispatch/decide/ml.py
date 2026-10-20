"""The `ml` provider (spec §7.2): classifiers trained on the decision ledger joined with what
really happened. `ml` is unavailable, and its chain moves on (to `rules` for delivery risk, to a
person for failed attempts), until a decision type has enough labelled examples AND a model trained
on the older 80% passes a check on the newest 20%:
  delivery_risk   its ranking (AUC) must beat the rules' by MARGIN, or the rules stay in charge
  failed_attempt  AUC >= MIN_AUC (the rules abstain on these cases, so there's nothing to beat)
When it passes, the model is refit on everything.

    MlRegistry(store).provider(spec)   -> a trained MlDecider, or a stub that raises ProviderUnavailable
    MlRegistry(store).status()         -> {type: {"labelled", "needed", "trained", "model"}}

Tasks (what is learned, from which labels):
  delivery_risk   inputs.features of each risk decision -> 1 if that order's next attempt was a
                  genuine failure, 0 if it was delivered. Needs 500 labels (spec §7.1). Orders we
                  messaged are left out: the message changed the outcome, so their label is biased.
                  Holdout orders (risky but deliberately not messaged, RISK_HOLDOUT) count
                  1/RISK_HOLDOUT times, so the model still learns what risky customers look like.
  failed_attempt  inputs (GPS distance, time at the door, driver's words) of attempts the rules
                  couldn't settle -> what the person decided (genuine or not). Needs 200 labels.

The model is L2-regularised logistic regression fitted by Newton's method (IRLS) in numpy:
small, deterministic, no extra dependency, and its probabilities are reasonably calibrated; the
engine's isotonic calibrator (spec §7.3) corrects the rest once outcomes exist. Swap in LightGBM
when the features outgrow a linear model.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from dispatch.core.decisions import Answer, NoulAnswer, ScoreAnswer

from .deciders.base import ProviderMeta, ProviderUnavailable
from .deciders.rules import certain
from .risk import estimate, levels_for, reason_from_text


# ---------------------------------------------------------------- the model
class LogisticModel:
    def __init__(self, names: list[str], w: np.ndarray, mean: np.ndarray, std: np.ndarray, n: int):
        self.names, self.w, self.mean, self.std, self.n = names, w, mean, std, n

    @classmethod
    def fit(cls, rows: list[dict[str, float]], y: list[int], l2: float = 1.0, iters: int = 50,
            weights: list[float] | None = None) -> "LogisticModel":
        """`weights` (default 1) up-weight rows that stand for many others, e.g. holdout orders."""
        names = sorted(rows[0])
        X = np.array([[r[k] for k in names] for r in rows], dtype=float)
        mean, std = X.mean(axis=0), X.std(axis=0)
        std[std == 0] = 1.0
        Z = np.hstack([np.ones((len(X), 1)), (X - mean) / std])
        t = np.asarray(y, dtype=float)
        sw = np.ones(len(t)) if weights is None else np.asarray(weights, dtype=float)
        w = np.zeros(Z.shape[1])
        reg = np.eye(Z.shape[1]) * l2
        reg[0, 0] = 0.0                                   # don't shrink the intercept
        for _ in range(iters):
            p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
            grad = Z.T @ (sw * (p - t)) + reg @ w
            H = (Z * (sw * p * (1 - p))[:, None]).T @ Z + reg
            step = np.linalg.solve(H, grad)
            w -= step
            if np.abs(step).max() < 1e-8:
                break
        return cls(names, w, mean, std, len(rows))

    def predict(self, row: dict[str, float]) -> float:
        x = (np.array([row[k] for k in self.names], dtype=float) - self.mean) / self.std
        return float(1 / (1 + math.exp(-max(-30.0, min(30.0, self.w[0] + x @ self.w[1:])))))

    def weights(self) -> dict[str, float]:
        """Per-feature weights on the standardised scale (bigger = more influence), for the console."""
        return {k: round(float(v), 3) for k, v in zip(self.names, self.w[1:])}


MARGIN = 0.01
MIN_AUC = 0.7


def auc(scores: list[float], labels: list[int]) -> float | None:
    """Area under the ROC curve: P(a random positive scores above a random negative); ties count half."""
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return round((sum(r for r, y in zip(ranks, labels) if y) - pos * (pos + 1) / 2) / (pos * neg), 4)


@dataclass
class Sample:
    x: dict[str, float]
    y: int
    weight: float = 1.0
    baseline: float | None = None          # the current provider's score for the same case, if any


# ---------------------------------------------------------------- tasks
def risk_vector(f: dict[str, float]) -> dict[str, float]:
    past = f["past_orders"]
    return {"fail_rate": (f["past_failed"] + 0.2) / (past + 2), "refuse_rate": f["past_refused"] / (past + 1),
            "log_past_orders": math.log1p(past), "first_time": float(past == 0), "cod": f["cod"],
            "high_value_cod": f["high_value_cod"], "address_doubt": f["address_doubt"], "reattempt": f["reattempt"]}


def attempt_vector(inputs: dict[str, Any]) -> dict[str, float]:
    d, dwell = inputs.get("distance_m"), inputs.get("dwell_s")
    reason = reason_from_text(inputs.get("reason_text", ""))
    return {"has_gps": float(d is not None), "log_distance": math.log1p(d or 0),
            "has_dwell": float(dwell is not None), "log_dwell": math.log1p(dwell or 0),
            **{f"said_{r}": float(reason == r) for r in ("not_home", "refused", "wrong_address", "unsafe")}}


def _risk_labels(store) -> list[Sample]:
    """Each risk decision paired with the next outcome of that order: delivered (0) or a genuine
    failure (1). Disputed attempts (driver not at the address) say nothing about the customer;
    messaged orders are skipped (biased), holdout orders are up-weighted."""
    by_order: dict[str, list] = {}
    for d in store.decisions(type_="delivery_risk", limit=100_000):
        if "features" in d.inputs:
            by_order.setdefault(d.subject, []).append(d)
    messaged: dict[str, list] = {}
    holdout: dict[str, list] = {}
    for e in store.events_since(0, types=["order.confirm_requested", "order.risk_scored"], limit=1_000_000):
        if e.type == "order.confirm_requested":
            messaged.setdefault(e.entity, []).append(e.ts)
        elif e.payload.get("holdout_weight"):
            holdout.setdefault(e.entity, []).append((e.ts, e.payload["holdout_weight"]))
    out = []
    for e in store.events_since(0, types=["stop.delivered", "order.attempt_checked"], limit=1_000_000):
        if e.type == "order.attempt_checked" and not e.payload["genuine"]:
            continue
        if any(t <= e.ts for t in messaged.get(e.entity, [])):
            continue
        before = [d for d in by_order.get(e.entity, []) if d.ts <= e.ts]
        if before:
            f = max(before, key=lambda d: d.ts).inputs["features"]
            w = max((w for t, w in holdout.get(e.entity, []) if t <= e.ts), default=1.0)
            out.append(Sample(risk_vector(f), int(e.type == "order.attempt_checked"), w, estimate(f)[0]))
    return out


def _attempt_labels(store) -> list[Sample]:
    """Attempts a person decided, paired with the evidence the machine saw (its record for the
    same order, just before the person's)."""
    rows = store.decisions(type_="failed_attempt", limit=100_000)
    evidence = [d for d in rows if d.inputs]
    out = []
    for h in rows:
        if h.decider != "human" or "genuine" not in h.cost:
            continue
        seen = [d for d in evidence if d.subject == h.subject and d.ts <= h.ts]
        if seen:
            out.append(Sample(attempt_vector(max(seen, key=lambda d: d.ts).inputs), int(bool(h.cost["genuine"]))))
    return out


def _risk_answers(p: float, state: dict, questions) -> dict[str, Answer]:
    return {"will_fail": ScoreAnswer.from_probabilities(levels_for(p)),
            "refuse_risk_cod": NoulAnswer(p=estimate(state["features"])[1])}   # refusals: rules until enough data


def _attempt_answers(p: float, state: dict, questions) -> dict[str, Answer]:
    return {"genuine": NoulAnswer(p=p), "reason": certain(questions["reason"], reason_from_text(state.get("reason_text", "")))}


@dataclass
class MlTask:
    type: str
    min_samples: int
    vector: Callable[[dict], dict[str, float]]                 # decision inputs -> named features
    labels: Callable[[Any], list[Sample]]                     # oldest first
    answers: Callable[[float, dict, dict], dict[str, Answer]]


TASKS = {
    "delivery_risk": MlTask("delivery_risk", 500, lambda s: risk_vector(s["features"]), _risk_labels, _risk_answers),
    "failed_attempt": MlTask("failed_attempt", 200, attempt_vector, _attempt_labels, _attempt_answers),
}


# ---------------------------------------------------------------- provider
class MlDecider:
    name = "ml"
    capabilities = {"tabular"}

    def __init__(self, task: MlTask, model: LogisticModel):
        self.task, self.model = task, model
        self.meta = ProviderMeta()
        self.meta.model, self.meta.method = f"{task.type}-logreg-n{model.n}", "classifier"

    async def decide(self, state, questions):
        return self.task.answers(self.model.predict(self.task.vector(state)), state, questions)


class _Untrained:
    name = "ml"
    capabilities = {"tabular"}

    def __init__(self, reason: str):
        self.reason, self.meta = reason, ProviderMeta()

    async def decide(self, state, questions):
        raise ProviderUnavailable(self.reason)


class MlRegistry:
    """Trains lazily per decision type and retrains after `ttl_s` (new outcomes keep arriving)."""

    def __init__(self, store, tasks: dict[str, MlTask] | None = None, ttl_s: float = 300):
        self.store, self.tasks, self.ttl_s = store, tasks if tasks is not None else TASKS, ttl_s
        self._cache: dict[str, tuple[float, MlDecider | _Untrained, dict]] = {}

    def refresh(self) -> None:
        self._cache.clear()

    def provider(self, spec_or_type) -> MlDecider | _Untrained | None:
        kind = getattr(spec_or_type, "type", spec_or_type)
        task = self.tasks.get(kind)
        if task is None:
            return None                                   # no ml task for this decision: "not configured"
        hit = self._cache.get(kind)
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        p, info = self.train(task, task.labels(self.store))
        self._cache[kind] = (time.monotonic(), p, info)
        return p

    @staticmethod
    def train(task: MlTask, data: list[Sample]) -> tuple[MlDecider | _Untrained, dict]:
        """The check on a time split (older 80% to fit, newest 20% to test), then a refit on all."""
        info: dict[str, Any] = {"labelled": len(data), "needed": task.min_samples}
        if len(data) < task.min_samples or len({s.y for s in data}) < 2:
            return _Untrained(f"needs {task.min_samples} labelled outcomes with both results, has {len(data)}"), info
        cut = int(len(data) * 0.8)
        train, test = data[:cut], data[cut:]
        if len({s.y for s in train}) < 2 or len({s.y for s in test}) < 2:
            return _Untrained("not enough of both results to test a model"), info
        m = LogisticModel.fit([s.x for s in train], [s.y for s in train], weights=[s.weight for s in train])
        ys = [s.y for s in test]
        info["auc"] = auc([m.predict(s.x) for s in test], ys)
        has_base = all(s.baseline is not None for s in test)
        info["baseline_auc"] = auc([s.baseline for s in test], ys) if has_base else None
        bar = (info["baseline_auc"] + MARGIN) if has_base else MIN_AUC
        if info["auc"] is None or info["auc"] < bar:
            why = (f"the trained model doesn't beat the rules yet (AUC {info['auc']} vs {info['baseline_auc']})"
                   if has_base else f"the trained model isn't good enough yet (AUC {info['auc']} < {MIN_AUC})")
            return _Untrained(why), info
        model = LogisticModel.fit([s.x for s in data], [s.y for s in data], weights=[s.weight for s in data])
        return MlDecider(task, model), info

    def status(self) -> dict[str, dict[str, Any]]:
        out = {}
        for kind in self.tasks:
            p = self.provider(kind)
            out[kind] = {**self._cache[kind][2], "trained": isinstance(p, MlDecider),
                         "model": p.meta.model if isinstance(p, MlDecider) else None,
                         "why_not": None if isinstance(p, MlDecider) else p.reason,
                         "weights": p.model.weights() if isinstance(p, MlDecider) else None}
        return out
