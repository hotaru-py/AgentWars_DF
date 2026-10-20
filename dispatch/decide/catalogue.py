"""Decision catalogue (spec §7.1): each decision type's questions, gating, reversibility, default
provider chain and rules. Question wording follows TypeSafe's guidance: one narrow judgment per
question, concrete level descriptions, and an explicit no-match option.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dispatch.core.decisions import Answer, Choice, Noul, Question, Score
from dispatch.decide.deciders.rules import Rule
from dispatch.decide.risk import FAIL_REASONS, RISK_LEVELS, attempt_rule, risk_rule
from dispatch.when import SLOTS


@dataclass
class DecisionSpec:
    type: str
    questions: dict[str, Question]
    gating: list[str]                    # questions whose confidence gates the action (min is used)
    reversible: bool                     # can the action be undone cheaply? (spec §6.2)
    chain: list[str]                     # default provider chain, ending in "human"
    rules: list[Rule] = field(default_factory=list)

    def gating_confidence(self, answers: dict[str, Answer]) -> float:
        return min(answers[q].confidence for q in self.gating) if self.gating else 1.0


# ---------------------------------------------------------------- incident triage
INCIDENT_KINDS = {
    "flood": "Water on the road: flooding, waterlogging, a submerged underpass, heavy rain stopping traffic",
    "traffic": "Vehicles slowed or stopped by congestion, an accident, a VIP or political convoy, a procession or protest",
    "closure": "A road is fully closed or blocked: barricades, construction, a fallen tree, police closure",
    "breakdown": "One of our own delivery vehicles cannot continue: mechanical failure, puncture, accident, driver unwell",
    "other": "A real event affecting deliveries that fits none of the above",
    "not_an_incident": "Not a current event affecting roads or our vehicles: a question, chit-chat, an old or hypothetical event, spam",
}

SEVERITY_LEVELS = [
    "Barely noticeable: traffic moves almost normally",
    "Minor: a few minutes of delay for vehicles passing through",
    "Moderate: clearly slower, around twice the normal time through the area",
    "Severe: crawling, several times the normal time; avoiding the area is better",
    "Blocked: vehicles cannot get through at all",
]

INCIDENT_TRIAGE = DecisionSpec(
    type="incident_triage",
    questions={
        "kind": Choice(instructions="What kind of incident does `report.text` describe for delivery vehicles "
                                    "in Chennai?", criteria=INCIDENT_KINDS),
        "severity": Score(instructions="How badly would this slow a delivery van passing through the affected "
                                       "area right now, judging only from `report.text`?", levels=SEVERITY_LEVELS),
        "credible": Noul(instructions="`report.text` describes a real event happening now (not a question, joke, "
                                      "rumour about the past, or test message) that affects roads or our vehicles."),
    },
    gating=["kind", "credible"],
    reversible=True,                      # an activated incident can be closed and the plan re-run
    chain=["llm", "human"],
)

# ---------------------------------------------------------------- customer reply intent
REPLY_INTENTS = {
    "confirm": "The customer confirms the delivery as planned (yes, ok, fine, will be home)",
    "reschedule": "The customer wants a different time or day",
    "cancel": "The customer wants to cancel the order",
    "deliver_to_neighbour": "The customer asks us to leave the parcel with a neighbour, security or someone else",
    "give_directions": "The customer gives directions, a landmark or a location to help the driver find them",
    "other": "Anything else: a question, a complaint, small talk, or unclear",
}

_BUTTONS = {"1": "confirm", "2": "reschedule", "3": "cancel"}
_CONFIRM_WORDS = {"yes", "y", "ok", "okay", "confirm", "confirmed", "sure", "fine", "yes please", "ok thanks",
                  "okay thanks", "yes confirmed"}


def _normalize(text: str) -> str:
    return " ".join("".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace()).split())


def button_reply_rule(state, questions):
    """Replies to our "Reply 1/2/3" messages and plain yes/ok: unambiguous, so no AI needed."""
    text = _normalize(state["reply"]["text"]) if isinstance(state, dict) else _normalize(str(state))
    intent = _BUTTONS.get(text) or ("confirm" if text in _CONFIRM_WORDS else None)
    if intent is None:
        return None
    from dispatch.decide.deciders.rules import certain
    return {"intent": certain(questions["intent"], intent), "has_time": certain(questions["has_time"], False)}


REPLY_INTENT = DecisionSpec(
    type="reply_intent",
    questions={
        "intent": Choice(instructions="What does the customer want, judging from `reply.text` as an answer to "
                                      "`our_last_message`?", criteria=REPLY_INTENTS),
        "has_time": Noul(instructions="`reply.text` names a specific time or day for the delivery."),
    },
    gating=["intent"],
    reversible=False,                     # a cancellation can't be silently undone
    chain=["rules", "llm", "human"],
    rules=[button_reply_rule],
)

# ---------------------------------------------------------------- delivery risk (spec §10)
DELIVERY_RISK = DecisionSpec(
    type="delivery_risk",
    questions={
        "will_fail": Score(instructions="How likely is it that delivering `order` fails today (nobody home, refused, "
                                        "address not found), given `features`?", levels=RISK_LEVELS),
        "refuse_risk_cod": Noul(instructions="The customer will refuse this cash-on-delivery parcel at the door."),
    },
    gating=[],                            # the action (a confirmation message) is cheap: always act on the score
    reversible=True,
    chain=["ml", "rules"],                # ml is unavailable until 500 labelled outcomes exist
    rules=[risk_rule],
)

# ---------------------------------------------------------------- failed-attempt verify (spec §10)
FAILED_ATTEMPT = DecisionSpec(
    type="failed_attempt",
    questions={
        "genuine": Noul(instructions="The driver really attempted this delivery at the customer's address "
                                     "(judging from `distance_m` and `dwell_s`)."),
        "reason": Choice(instructions="Why did the delivery fail, judging from `reason_text`?", criteria=FAIL_REASONS),
    },
    gating=["genuine"],
    reversible=False,                     # it decides RTO and counts against the customer's history
    chain=["rules", "ml", "human"],
    rules=[attempt_rule],
)

# ---------------------------------------------------------------- reschedule time (spec §10)
def reschedule_rule(state, questions):
    """Clock times and day words ("after 6 pm", "tomorrow morning") parse without AI."""
    from datetime import datetime

    from dispatch.decide.deciders.rules import certain
    from dispatch.when import parse_when
    w = parse_when(state["reply"]["text"], datetime.fromisoformat(state["now"]))
    if w is None:
        return None
    return {"slot": certain(questions["slot"], w.slot)}


RESCHEDULE_SLOT = DecisionSpec(
    type="reschedule_slot",
    questions={
        "slot": Choice(instructions="When does the customer want the delivery, judging from `reply.text`? "
                                    "`now` is the current local time.", criteria=SLOTS),
    },
    gating=["slot"],
    reversible=True,                      # the customer can reply again; nothing is lost
    chain=["rules", "llm", "human"],
    rules=[reschedule_rule],
)

CATALOGUE: dict[str, DecisionSpec] = {s.type: s for s in (INCIDENT_TRIAGE, REPLY_INTENT, DELIVERY_RISK, FAILED_ATTEMPT,
                                                          RESCHEDULE_SLOT)}
