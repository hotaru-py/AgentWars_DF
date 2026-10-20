"""Decision engine (spec §7): run a decision type's provider chain with calibration, gates,
shadow providers and the ledger.

For each provider in the chain (config DECIDERS overrides the catalogue default):
  unavailable / no answer        -> note it, try the next provider
  calibrated confidence >= act   -> ACT
  >= review, reversible action   -> REVIEW (act, flagged in the console)
  >= review, irreversible        -> ESCALATED: record it, try the next provider
  < review                       -> HUMAN: record it with the provider's suggestion
  "human" reached in the chain   -> HUMAN with no machine answer
Shadow providers (DECIDERS_SHADOW) answer the same questions concurrently; their answers are
logged on the final record and never acted on.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from dispatch.core.config import Settings
from dispatch.core.decisions import (Answer, DecisionAction, DecisionRecord, Question, Route, ShadowResult,
                                     gate_route)
from dispatch.core.ports import Decider
from dispatch.store import Store

from .calibrate import Calibrator, effective_gate, fit_from_ledger
from .catalogue import DecisionSpec
from .deciders.base import NoAnswer, ProviderUnavailable
from .deciders.rules import RulesDecider
from .ml import MlRegistry


@dataclass
class DecisionResult:
    record: DecisionRecord                       # the final ledger row (route act/review/human)
    answers: dict[str, Answer]                   # answers acted on; {} when no provider answered
    trail: list[dict[str, Any]] = field(default_factory=list)   # every provider tried and what happened

    @property
    def route(self) -> Route:
        return self.record.route

    @property
    def decided(self) -> bool:
        return self.record.route in (Route.act, Route.review)


class DecisionEngine:
    def __init__(self, store: Store, providers: dict[str, Decider], settings: Settings | None = None,
                 calibrators: dict[tuple[str, str], Calibrator] | None = None, ml: MlRegistry | None = None):
        self.store, self.providers = store, providers
        self.ml = ml if ml is not None else MlRegistry(store)
        self.settings = settings or Settings()
        self._cal = calibrators if calibrators is not None else {}

    def calibrator(self, decision_type: str, provider: str) -> Calibrator:
        key = (decision_type, provider)
        if key not in self._cal:
            self._cal[key] = fit_from_ledger(self.store, decision_type, provider)
        return self._cal[key]

    def refresh_calibration(self) -> None:
        self._cal.clear()
        self.ml.refresh()

    def chain(self, spec: DecisionSpec) -> list[str]:
        return self.settings.chain_overrides().get(spec.type) or spec.chain

    def _provider(self, name: str, spec: DecisionSpec) -> Decider | None:
        if name == "rules":
            return RulesDecider(spec.rules, label=f"rules:{spec.type}") if spec.rules else None
        if name == "ml" and "ml" not in self.providers:
            return self.ml.provider(spec)
        return self.providers.get(name)

    async def _shadow(self, spec: DecisionSpec, state, questions) -> list[ShadowResult]:
        async def one(name: str) -> ShadowResult:
            p = self._provider(name, spec)
            if p is None:
                return ShadowResult(decider=name, raw_confidence=0, error="not configured")
            t0 = time.perf_counter()
            try:
                ans = await p.decide(state, questions)
                return ShadowResult(decider=name, model=getattr(p, "meta", None) and p.meta.model, answers=ans,
                                    raw_confidence=spec.gating_confidence(ans),
                                    latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            except Exception as e:                    # a shadow must never break the live decision
                return ShadowResult(decider=name, raw_confidence=0, error=f"{type(e).__name__}: {str(e)[:160]}")
        names = [n for n in self.settings.shadow_providers() if n not in ("human",)]
        return list(await asyncio.gather(*(one(n) for n in names))) if names else []

    async def decide(self, spec: DecisionSpec, subject: str, state: dict[str, Any] | str,
                     action: DecisionAction | None = None) -> DecisionResult:
        questions: dict[str, Question] = spec.questions
        policy_gate = self.store.current_policy().gate(spec.type)
        action = action or DecisionAction(kind="none")
        inputs = state if isinstance(state, dict) else {"text": state}
        shadow_task = asyncio.create_task(self._shadow(spec, state, questions))
        trail: list[dict[str, Any]] = []
        prev_id: str | None = None
        final: DecisionResult | None = None
        for name in self.chain(spec):
            if name == "human":
                break
            p = self._provider(name, spec)
            if p is None:
                trail.append({"provider": name, "result": "not configured"})
                continue
            t0 = time.perf_counter()
            try:
                answers = await p.decide(state, questions)
            except (ProviderUnavailable, NoAnswer) as e:
                trail.append({"provider": name, "result": type(e).__name__, "detail": str(e)[:200]})
                continue
            meta = getattr(p, "meta", None)
            raw = spec.gating_confidence(answers)
            cal = self.calibrator(spec.type, name)
            gate = effective_gate(policy_gate, cal)
            conf = cal(raw)
            route = gate_route(conf, gate, spec.reversible)
            rec = DecisionRecord(type=spec.type, subject=subject, decider=name,
                                 model=meta.model if meta else getattr(p, "model", None),
                                 method=meta.method if meta else None, questions=questions, answers=answers,
                                 inputs=inputs,
                                 raw_confidence=raw, confidence=conf, gate=gate, route=route,
                                 action=action if route in (Route.act, Route.review) else DecisionAction(kind="none"),
                                 latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                                 cost=dict(meta.cost) if meta else {}, escalated_from=prev_id)
            trail.append({"provider": name, "result": route.value, "raw_confidence": raw, "confidence": conf})
            if route == Route.escalated:
                self.store.record_decision(rec)
                prev_id = rec.id
                continue
            final = DecisionResult(record=rec, answers=answers, trail=trail)
            break
        if final is None:                            # nothing decided: the human queue, no machine answer
            rec = DecisionRecord(type=spec.type, subject=subject, decider="human", questions=questions, answers={},
                                 inputs=inputs,
                                 raw_confidence=0, confidence=0, gate=policy_gate, route=Route.human,
                                 escalated_from=prev_id, cost={"trail": trail})
            final = DecisionResult(record=rec, answers={}, trail=trail)
        final.record.shadow = await shadow_task
        self.store.record_decision(final.record)
        return final
