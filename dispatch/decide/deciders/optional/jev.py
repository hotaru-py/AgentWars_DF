"""Optional `jev` provider: TypeSafe's Jev via the official SDK (typesafe-sdk), spec §7.2.

Only used when TYPESAFE_API_KEY is set and a chain or DECIDERS_SHADOW lists `jev`. The SDK is
imported lazily, so the app runs without it installed.

Mapping (checked against typesafe-sdk 0.7.5 and https://docs.typesafe.ai/sdk/python):
- All questions for one subject go in ONE system_one call (TypeSafe: batching is ~10x faster).
- State is sent as JSON content when it's a dict (system_one accepts structured content).
- Choice criteria: {label: description}; Score criteria: ordered level descriptions;
  Noul: instructions only.
- ChoiceAnswer.probabilities -> our Choice distribution; ScoreAnswer.probabilities is keyed by
  integer level -> ordered list; NoulAnswer.noul is P(yes).
"""

from __future__ import annotations

import time
from typing import Any

from dispatch.core.decisions import Answer, ChoiceAnswer, NoulAnswer, Question, ScoreAnswer
from dispatch.decide.deciders.base import ProviderMeta, ProviderUnavailable


class JevDecider:
    name = "jev"
    capabilities = {"text"}           # images exist in Jev but are not production-ready (spec §16)

    def __init__(self, api_key: str = "", model: str = "jev-latest", client: Any = None, sdk: Any = None):
        self.model, self._api_key, self._client, self._sdk = model, api_key, client, sdk
        self.meta = ProviderMeta()

    def _load(self):
        if self._sdk is None:
            try:
                import typesafe_sdk
            except ImportError as e:
                raise ProviderUnavailable("typesafe-sdk is not installed (pip install typesafe-sdk)") from e
            self._sdk = typesafe_sdk
        if self._client is None:
            if not self._api_key:
                raise ProviderUnavailable("no TYPESAFE_API_KEY set for the jev provider")
            self._client = self._sdk.AsyncTypeSafeClient(api_key=self._api_key, model=self.model)
        return self._sdk, self._client

    def _questions(self, sdk, questions: dict[str, Question]) -> dict[str, Any]:
        out = {}
        for qid, q in questions.items():
            if q.type == "choice":
                out[qid] = sdk.Choice(instructions=q.instructions, criteria=dict(q.criteria))
            elif q.type == "score":
                out[qid] = sdk.Score(instructions=q.instructions, criteria=list(q.levels))
            else:
                out[qid] = sdk.Noul(instructions=q.instructions)
        return out

    @staticmethod
    def _answer(q: Question, a: Any) -> Answer:
        if q.type == "choice":
            return ChoiceAnswer.from_probabilities({k: float(a.probabilities.get(k, 0.0)) for k in q.criteria})
        if q.type == "score":
            probs = {int(k): float(v) for k, v in a.probabilities.items()}
            return ScoreAnswer.from_probabilities([probs.get(i, 0.0) for i in range(len(q.levels))])
        return NoulAnswer(p=min(1.0, max(0.0, float(a.noul))))

    async def decide(self, state, questions: dict[str, Question]) -> dict[str, Answer]:
        self.meta = ProviderMeta()
        sdk, client = self._load()
        t0 = time.perf_counter()
        try:
            result = await client.system_one(state, self._questions(sdk, questions), model=self.model)
        except Exception as e:                       # auth, rate limit, network: the chain moves on
            raise ProviderUnavailable(f"jev failed: {type(e).__name__}: {str(e)[:200]}") from e
        usage = getattr(result, "usage", None)
        self.meta.model, self.meta.method = getattr(result, "model", self.model), "native"
        self.meta.cost = {"input_tokens": getattr(usage, "input_tokens", None),
                          "output_tokens": getattr(usage, "output_tokens", None),
                          "request_id": getattr(result, "request_id", None),
                          "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return {qid: self._answer(q, result.answers[qid]) for qid, q in questions.items()}
