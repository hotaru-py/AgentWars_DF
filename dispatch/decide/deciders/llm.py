"""The `llm` provider: Gemini answers typed questions with measured probabilities (spec §7.2).

Probabilities are never the model's own stated confidence:
- `logprobs`: one constrained (enum) call per question; the token log-probabilities of the
  answer options give the distribution. Exact, but not every model/endpoint supports it.
- `self_consistency`: k samples (temperature 0.7) of all questions in one JSON call; the answer
  frequencies give the distribution. Costs k calls, works everywhere.
- `auto` (default): try logprobs once; if the endpoint rejects it, switch to sampling for good.

The Gemini client is injectable so tests run without network or keys.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import Counter
from typing import Any

from dispatch.core.decisions import Answer, ChoiceAnswer, NoulAnswer, Question, ScoreAnswer

from .base import ProviderMeta, ProviderUnavailable

YES, NO = "yes", "no"
SMOOTH = 0.1          # pseudo-count per option in sampling, so 5/5 agreement isn't "certain"

SYSTEM = ("You answer typed questions about the STATE for a delivery dispatch system. Use only the STATE. "
          "Pick exactly one allowed value for each question.")


def options(q: Question) -> list[str]:
    if q.type == "choice":
        return list(q.criteria)
    if q.type == "score":
        return [f"{i}" for i in range(len(q.levels))]
    return [YES, NO]


def describe(qid: str, q: Question) -> str:
    if q.type == "choice":
        opts = "; ".join(f"{k}: {v}" for k, v in q.criteria.items())
        return f"- {qid}: {q.instructions}. Options -> {opts}"
    if q.type == "score":
        opts = "; ".join(f"{i}: {lvl}" for i, lvl in enumerate(q.levels))
        return f"- {qid}: {q.instructions}. Levels (answer the number) -> {opts}"
    return f"- {qid}: Is this true? \"{q.instructions}\" Answer yes or no."


def to_answer(q: Question, dist: dict[str, float]) -> Answer:
    if q.type == "choice":
        return ChoiceAnswer.from_probabilities(dist)
    if q.type == "score":
        return ScoreAnswer.from_probabilities([dist.get(f"{i}", 0.0) for i in range(len(q.levels))])
    total = dist.get(YES, 0.0) + dist.get(NO, 0.0)
    return NoulAnswer(p=dist.get(YES, 0.0) / total if total else 0.5)


class LlmDecider:
    name = "llm"
    capabilities = {"text"}

    def __init__(self, api_key: str = "", model: str = "gemini-3.6-flash", method: str = "auto",
                 samples: int = 5, client: Any = None):
        if method not in ("auto", "logprobs", "self_consistency", "sampling"):
            raise ValueError(f"unknown LLM probability method {method!r}")
        self.model, self.samples = model, samples
        self.method = "self_consistency" if method == "sampling" else method
        self._client, self._api_key = client, api_key
        self._logprobs_ok: bool | None = None if self.method == "auto" else self.method == "logprobs"
        self.meta = ProviderMeta()

    # ------------------------------------------------------------ client
    def _gemini(self):
        if self._client is None:
            if not self._api_key:
                raise ProviderUnavailable("no GOOGLE_API_KEY set for the llm provider")
            from google import genai
            from google.genai import types
            # Retry transient errors (model overloaded 503, 500, per-minute 429) with backoff, so a
            # demand spike doesn't send every report to a human. A daily-quota 429 still fails after
            # the retries, and the chain moves on.
            self._client = genai.Client(api_key=self._api_key, http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=4, initial_delay=2, max_delay=20,
                                                     http_status_codes=[429, 500, 503])))
        return self._client

    def _config(self, **kw):
        from google.genai import types
        return types.GenerateContentConfig(system_instruction=SYSTEM, **kw)

    def _state_text(self, state) -> str:
        return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, default=str, indent=1)

    # ------------------------------------------------------------ methods
    def _logprobs_one(self, state_txt: str, qid: str, q: Question) -> dict[str, float]:
        opts = options(q)
        r = self._gemini().models.generate_content(
            model=self.model,
            contents=f"STATE:\n{state_txt}\n\nQUESTION:\n{describe(qid, q)}\nAnswer with one allowed value only.",
            config=self._config(response_mime_type="text/x.enum",
                                response_schema={"type": "STRING", "enum": opts},
                                response_logprobs=True, logprobs=min(20, max(5, len(opts) + 2)), temperature=0))
        lp = r.candidates[0].logprobs_result
        if not lp or not lp.top_candidates:
            raise RuntimeError("logprobs not returned")
        dist = {o: 0.0 for o in opts}
        for cand in lp.top_candidates[0].candidates:
            tok = (cand.token or "").strip().strip('"').lower()
            if not tok:
                continue
            match = [o for o in opts if o.lower().startswith(tok)]
            if len(match) == 1:                     # a token prefixing several options is ambiguous: skip it
                dist[match[0]] += math.exp(cand.log_probability)
        if sum(dist.values()) <= 0:
            raise RuntimeError("no answer option matched the returned tokens")
        return dist

    def _sample_all(self, state_txt: str, questions: dict[str, Question]) -> dict[str, dict[str, float]]:
        schema = {"type": "OBJECT", "required": list(questions),
                  "properties": {qid: {"type": "STRING", "enum": options(q)} for qid, q in questions.items()}}
        prompt = f"STATE:\n{state_txt}\n\nQUESTIONS:\n" + "\n".join(describe(k, q) for k, q in questions.items())
        counts = {qid: Counter() for qid in questions}
        for _ in range(self.samples):
            r = self._gemini().models.generate_content(
                model=self.model, contents=prompt,
                config=self._config(response_mime_type="application/json", response_schema=schema, temperature=0.7))
            try:
                data = json.loads(r.text)
            except (TypeError, ValueError):
                continue
            for qid in questions:
                if data.get(qid) in options(questions[qid]):
                    counts[qid][data[qid]] += 1
        out = {}
        for qid, q in questions.items():
            opts = options(q)
            n = sum(counts[qid].values())
            if n == 0:
                raise RuntimeError(f"no valid samples for {qid}")
            out[qid] = {o: (counts[qid][o] + SMOOTH) / (n + SMOOTH * len(opts)) for o in opts}
        return out

    def _decide_sync(self, state, questions: dict[str, Question]) -> dict[str, Answer]:
        state_txt = self._state_text(state)
        self.meta.model = self.model
        if self._logprobs_ok is not False:
            try:
                dists = {qid: self._logprobs_one(state_txt, qid, q) for qid, q in questions.items()}
                self._logprobs_ok, self.meta.method = True, "logprobs"
                self.meta.cost = {"calls": len(questions)}
                return {qid: to_answer(q, dists[qid]) for qid, q in questions.items()}
            except ProviderUnavailable:
                raise
            except Exception as e:                  # endpoint/model without logprobs: remember and sample
                if self.method == "logprobs":
                    raise ProviderUnavailable(f"logprobs unavailable: {e}") from e
                self._logprobs_ok = False
        dists = self._sample_all(state_txt, questions)
        self.meta.method, self.meta.cost = "self_consistency", {"calls": self.samples}
        return {qid: to_answer(q, dists[qid]) for qid, q in questions.items()}

    async def decide(self, state, questions: dict[str, Question]) -> dict[str, Answer]:
        self.meta = ProviderMeta()
        try:
            return await asyncio.to_thread(self._decide_sync, state, questions)
        except ProviderUnavailable:
            raise
        except Exception as e:                      # quota, network, bad output: let the chain move on
            raise ProviderUnavailable(f"llm failed: {type(e).__name__}: {str(e)[:200]}") from e
