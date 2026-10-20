"""The `rules` provider: deterministic, decision-specific rules (spec §7.2).

A rule looks at the state and either returns a full set of answers (with certainty) or None.
If no rule fires, the provider raises NoAnswer and the chain moves to the next provider.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dispatch.core.decisions import Answer, ChoiceAnswer, NoulAnswer, Question, ScoreAnswer

from .base import NoAnswer, ProviderMeta

Rule = Callable[[dict[str, Any] | str, dict[str, Question]], dict[str, Answer] | None]


def certain(question: Question, value: str | int | bool) -> Answer:
    """A certain answer of the right type: a Choice label, a Score level index, or a Noul bool."""
    if question.type == "choice":
        return ChoiceAnswer.from_probabilities({k: 1.0 if k == value else 0.0 for k in question.criteria})
    if question.type == "score":
        return ScoreAnswer.from_probabilities([1.0 if i == value else 0.0 for i in range(len(question.levels))])
    return NoulAnswer(p=1.0 if value else 0.0)


class RulesDecider:
    name = "rules"
    model = None
    capabilities = {"text", "tabular"}

    def __init__(self, rules: list[Rule], label: str = "rules"):
        self.rules, self.meta = rules, ProviderMeta()
        self.meta.model, self.meta.method = label, "rule"

    async def decide(self, state, questions):
        for rule in self.rules:
            out = rule(state, questions)
            if out is not None:
                missing = set(questions) - set(out)
                if missing:
                    raise ValueError(f"rule {getattr(rule, '__name__', rule)} left questions unanswered: {missing}")
                return out
        raise NoAnswer("no rule fired")
