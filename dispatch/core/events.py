"""Event log types (spec §6.4). The event log is append-only; every state change is an event.

Each event type has a payload model registered in PAYLOADS. Use `make_event` to build one,
and `parse_payload` to read one back with validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .decisions import DecisionRecord
from .models import Incident, LatLng, Model, Order, OrderState, Plan, Policy, utcnow


class Event(Model):
    seq: int | None = None             # assigned by the store (bigserial)
    ts: datetime = Field(default_factory=utcnow)
    type: str
    entity: str                        # id of the main entity (order, plan, incident, decision, ...)
    payload: dict[str, Any]


# ---------------------------------------------------------------- payloads
class OrderCreated(Model):
    order: Order


class OrderGeocoded(Model):
    order_id: str
    location: LatLng | None
    digipin: str | None
    confidence: float
    method: str


class OrderStateChanged(Model):
    order_id: str
    before: OrderState
    after: OrderState
    reason: str = ""


class OrderNoted(Model):
    order_id: str
    note: str
    source: str = "customer"           # customer | driver | ops


class OrderRiskScored(Model):
    order_id: str
    risk: float
    refuse_risk: float
    decision_id: str
    confirm: bool                      # above the threshold: we ask the customer to confirm
    holdout_weight: float | None = None   # risky but deliberately not asked (RISK_HOLDOUT): training weight


class OrderConfirmRequested(Model):
    order_id: str
    promised_at: datetime | None


class OrderReplied(Model):
    order_id: str
    intent: str
    message_id: str


class OrderRescheduled(Model):
    order_id: str
    window_start: datetime
    window_end: datetime
    source: str                        # customer text it came from, or "ops"


class AttemptChecked(Model):           # failed-attempt verify (spec §10)
    order_id: str
    genuine: bool
    reason: str
    result: str                        # reattempt | rto | disputed
    decision_id: str | None = None


class PlanCreated(Model):
    plan: Plan


class PlanApplied(Model):
    plan_id: str
    replaced_plan_id: str | None = None
    decision_id: str | None = None


class StopEvent(Model):                # arrived / delivered / failed
    order_id: str
    vehicle_id: str
    location: LatLng | None = None
    reason: str = ""                   # failure reason, free text from the driver
    photo_ref: str | None = None
    dwell_s: float | None = Field(default=None, ge=0)   # time at the door, if the driver app measured it


class IncidentEvent(Model):            # reported / triaged / closed
    incident: Incident


class MessageEvent(Model):             # sent / received
    message_id: str
    customer_id: str
    order_id: str | None = None
    channel: str
    text: str


class DecisionMade(Model):
    decision: DecisionRecord


class DecisionOverridden(Model):
    decision_id: str
    by: str
    note: str = ""
    new_action: dict[str, Any] = {}


class PolicyUpdated(Model):
    policy: Policy
    reason: str = ""


class DaySimulated(Model):
    """A day replayed by the digital twin and what the learner did with it (spec §9)."""
    run: int
    plan_id: str
    stops: int
    late_predicted: int
    late_actual: int
    delay_error: float | None                 # |predicted - actual delay| / actual delay, disrupted stops only
    observed: dict[str, float] = {}           # incident kind -> slowdown factor the day revealed
    changes: dict[str, list[float]] = {}      # kind -> [old factor, new factor]
    rolled_back: list[str] = []
    policy_version: int


class VehicleGps(Model):
    vehicle_id: str
    location: LatLng
    speed_mps: float | None = None


PAYLOADS: dict[str, type[Model]] = {
    "order.created": OrderCreated,
    "order.geocoded": OrderGeocoded,
    "order.state_changed": OrderStateChanged,
    "order.confirmed": OrderStateChanged,
    "order.noted": OrderNoted,
    "order.risk_scored": OrderRiskScored,
    "order.confirm_requested": OrderConfirmRequested,
    "order.attempt_checked": AttemptChecked,
    "order.replied": OrderReplied,
    "order.rescheduled": OrderRescheduled,
    "plan.created": PlanCreated,
    "plan.applied": PlanApplied,
    "stop.arrived": StopEvent,
    "stop.delivered": StopEvent,
    "stop.failed": StopEvent,
    "incident.reported": IncidentEvent,
    "incident.triaged": IncidentEvent,
    "incident.closed": IncidentEvent,
    "message.sent": MessageEvent,
    "message.received": MessageEvent,
    "decision.made": DecisionMade,
    "decision.overridden": DecisionOverridden,
    "policy.updated": PolicyUpdated,
    "vehicle.gps": VehicleGps,
    "day.simulated": DaySimulated,
}


def make_event(type_: str, entity: str, payload: Model) -> Event:
    expected = PAYLOADS.get(type_)
    if expected is None:
        raise ValueError(f"unknown event type {type_!r}; register it in PAYLOADS")
    if not isinstance(payload, expected):
        raise TypeError(f"{type_} needs a {expected.__name__} payload, got {type(payload).__name__}")
    return Event(type=type_, entity=entity, payload=payload.model_dump(mode="json"))


def parse_payload(event: Event) -> Model:
    return PAYLOADS[event.type].model_validate(event.payload)
