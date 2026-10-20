"""Decision types shared by every Decider (Jev, Gemini, rules), the gates and the ledger (spec §6.1-6.3).

Questions and answers are three typed primitives shared by every provider (rules, ml, llm, and
optional judgment models such as Jev, whose API uses the same shapes):
  Choice -> {choice, probabilities, confidence}
  Score  -> {score, probabilities, confidence}     (score = expected level / (n-1), 0..1)
  Noul   -> {p}                                    (confidence = |2p - 1|, as Jev's docs define it)

Confidence formulas follow https://docs.typesafe.ai/confidence so every provider reports the
same statistic. These are RAW confidences (how concentrated the distribution is), not
correctness: gates read the calibrated value (spec §7.3).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import Field, model_validator

from .ids import new_id
from .models import GateThresholds, Model, utcnow


# ---------------------------------------------------------------- questions
class Choice(Model):
    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str] = Field(min_length=2)      # label -> description


class Score(Model):
    type: Literal["score"] = "score"
    instructions: str
    levels: list[str] = Field(min_length=2)             # low -> high


class Noul(Model):
    type: Literal["noul"] = "noul"
    instructions: str                                   # a statement; answer = P(true)


Question = Annotated[Union[Choice, Score, Noul], Field(discriminator="type")]


# ---------------------------------------------------------------- answers
def _normalize(p: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, v) for v in p.values())
    if total <= 0:
        raise ValueError("probabilities must not all be zero")
    return {k: max(0.0, v) / total for k, v in p.items()}


def choice_confidence(probabilities: dict[str, float]) -> float:
    """(p_max - 1/n) / (1 - 1/n): 1 = all mass on one option, 0 = even split."""
    n = len(probabilities)
    pmax = max(probabilities.values())
    return max(0.0, min(1.0, (pmax - 1 / n) / (1 - 1 / n)))


def score_confidence(probabilities: list[float]) -> float:
    """1 - (expected distance from the modal level) / (that distance under a uniform spread), floored at 0.
    Mass on an adjacent level costs less than mass at the far end."""
    n = len(probabilities)
    mode = max(range(n), key=lambda i: probabilities[i])
    spread = sum(p * abs(i - mode) for i, p in enumerate(probabilities))
    uniform = sum(abs(i - mode) for i in range(n)) / n
    return max(0.0, 1 - spread / uniform) if uniform else 1.0


class ChoiceAnswer(Model):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0, le=1)

    @classmethod
    def from_probabilities(cls, probabilities: dict[str, float]) -> "ChoiceAnswer":
        p = _normalize(probabilities)
        return cls(choice=max(p, key=p.get), probabilities=p, confidence=choice_confidence(p))


class ScoreAnswer(Model):
    type: Literal["score"] = "score"
    score: float = Field(ge=0, le=1)                    # expected level, scaled 0..1
    level: int = Field(ge=0)                            # most likely level index
    probabilities: list[float]
    confidence: float = Field(ge=0, le=1)

    @classmethod
    def from_probabilities(cls, probabilities: list[float]) -> "ScoreAnswer":
        p = list(_normalize({str(i): v for i, v in enumerate(probabilities)}).values())
        n = len(p)
        expected = sum(i * v for i, v in enumerate(p)) / (n - 1)
        return cls(score=expected, level=max(range(n), key=lambda i: p[i]), probabilities=p,
                   confidence=score_confidence(p))


class NoulAnswer(Model):
    type: Literal["noul"] = "noul"
    p: float = Field(ge=0, le=1)

    @property
    def confidence(self) -> float:
        return abs(2 * self.p - 1)

    @property
    def value(self) -> bool:
        return self.p >= 0.5


Answer = Annotated[Union[ChoiceAnswer, ScoreAnswer, NoulAnswer], Field(discriminator="type")]


def answer_confidence(a: ChoiceAnswer | ScoreAnswer | NoulAnswer) -> float:
    return a.confidence


# ---------------------------------------------------------------- gates
class Route(str, Enum):
    act = "act"                # executed automatically
    review = "review"          # executed (reversible) or passed to Gemini, flagged in the console
    escalated = "escalated"    # a stronger decider was asked (e.g. Jev unsure -> Gemini)
    human = "human"            # waiting in the human queue


def gate_route(confidence: float, gate: GateThresholds, reversible: bool) -> Route:
    """Spec §6.2: >= act -> act; [review, act) -> act if reversible else escalate; < review -> human."""
    if confidence >= gate.act:
        return Route.act
    if confidence >= gate.review:
        return Route.review if reversible else Route.escalated
    return Route.human


# ---------------------------------------------------------------- ledger
class DecisionAction(Model):
    kind: str                  # "replan", "create_incident", "send_confirmation", "none", ...
    ref: str | None = None     # id of what the action produced (plan, incident, message)


class ShadowResult(Model):
    decider: str
    model: str | None = None
    answers: dict[str, Answer] = {}
    raw_confidence: float = Field(ge=0, le=1)
    latency_ms: float | None = Field(default=None, ge=0)
    error: str | None = None


class DecisionRecord(Model):
    """One row of the decision ledger (spec §6.3). `outcome` is filled in later by the learner."""
    id: str = Field(default_factory=lambda: new_id("decision"))
    ts: datetime = Field(default_factory=utcnow)
    type: str                                   # catalogue entry, e.g. "incident_triage"
    subject: str                                # id of the thing decided about
    decider: str = Field(pattern=r"^[a-z][a-z0-9_]*$")   # "rules" | "ml" | "llm" | "jev" | plug-in | "human"
    model: str | None = None                    # e.g. "gemini-3.6-flash", "risk-gbm-v3", "jev-1.14.0"
    method: str | None = None                   # how probabilities were produced: "rule", "classifier",
                                                # "logprobs", "self_consistency", "native"
    questions: dict[str, Question] = {}
    inputs: dict[str, Any] = {}                 # the state the providers saw (training data for `ml`)
    answers: dict[str, Answer] = {}
    raw_confidence: float = Field(ge=0, le=1)   # provider's own statistic (min over gating questions)
    confidence: float = Field(ge=0, le=1)       # calibrated; what the gate used (spec §7.3)
    shadow: list[ShadowResult] = []             # shadow providers' answers: logged, never acted on
    gate: GateThresholds
    route: Route
    action: DecisionAction = Field(default_factory=lambda: DecisionAction(kind="none"))
    latency_ms: float | None = Field(default=None, ge=0)
    cost: dict[str, Any] = {}                   # tokens etc.
    escalated_from: str | None = None           # previous decision id when escalated
    outcome: str | None = None
    outcome_at: datetime | None = None

    @model_validator(mode="after")
    def _answers_match_questions(self):
        missing = set(self.questions) - set(self.answers)
        if self.decider != "human" and missing:
            raise ValueError(f"answers missing for questions: {sorted(missing)}")
        for k, a in self.answers.items():
            q = self.questions.get(k)
            if q is not None and q.type != a.type:
                raise ValueError(f"answer {k!r} is {a.type}, question is {q.type}")
        return self
