"""Domain models (spec §5). Pydantic v2; every API body, event payload and table row maps to these.

Conventions:
- ids are prefixed strings (dispatch.core.ids.new_id).
- datetimes are timezone-aware UTC; display converts to Settings.tz.
- Durations are seconds, distances meters (ints/floats), unless a field name says otherwise.
- No model uses a bare `status` field for its own lifecycle (v1 lesson): use `state`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, populate_by_name=True)


# ---------------------------------------------------------------- geometry
class LatLng(Model):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lng)


class Circle(Model):
    kind: Literal["circle"] = "circle"
    center: LatLng
    radius_m: float = Field(gt=0, le=50_000)


class Polygon(Model):
    kind: Literal["polygon"] = "polygon"
    ring: list[LatLng] = Field(min_length=3)   # closed implicitly


Area = Circle | Polygon


# ---------------------------------------------------------------- fleet
class VehicleKind(str, Enum):
    van = "van"
    bike = "bike"
    ev3w = "ev3w"          # electric three-wheeler


class VehicleState(str, Enum):
    active = "active"
    broken_down = "broken_down"
    off_duty = "off_duty"


class Depot(Model):
    id: str = Field(default_factory=lambda: new_id("depot"))
    name: str
    location: LatLng
    opens_at: datetime
    closes_at: datetime


class Vehicle(Model):
    id: str = Field(default_factory=lambda: new_id("vehicle"))
    depot_id: str
    name: str = ""
    kind: VehicleKind = VehicleKind.van
    capacity_kg: float = Field(gt=0)
    capacity_l: float = Field(gt=0)
    range_km: float | None = Field(default=None, gt=0)    # None = not range-limited
    state: VehicleState = VehicleState.active


class BreakRule(Model):
    after_s: int = Field(ge=0)          # earliest start, seconds after shift start
    duration_s: int = Field(gt=0)


class Driver(Model):
    id: str = Field(default_factory=lambda: new_id("driver"))
    name: str
    phone: str
    vehicle_id: str | None = None
    shift_start: datetime
    shift_end: datetime
    breaks: list[BreakRule] = []

    @model_validator(mode="after")
    def _shift_order(self):
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be after shift_start")
        return self


# ---------------------------------------------------------------- customers and orders
class CustomerHistory(Model):
    delivered: int = 0
    failed: int = 0
    refused: int = 0


class Customer(Model):
    id: str = Field(default_factory=lambda: new_id("customer"))
    name: str
    phone: str
    language: str = "en"               # BCP-47-ish: en, ta, hi
    history: CustomerHistory = Field(default_factory=CustomerHistory)


class OrderKind(str, Enum):
    delivery = "delivery"
    pickup = "pickup"


class Payment(str, Enum):
    prepaid = "prepaid"
    cod = "cod"


class OrderState(str, Enum):
    new = "new"
    geocoded = "geocoded"
    confirmed = "confirmed"
    planned = "planned"
    out_for_delivery = "out_for_delivery"
    delivered = "delivered"
    failed = "failed"
    reattempt = "reattempt"
    rto = "rto"                        # return to origin
    cancelled = "cancelled"


ORDER_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.new: {OrderState.geocoded, OrderState.cancelled},
    OrderState.geocoded: {OrderState.confirmed, OrderState.planned, OrderState.cancelled},
    OrderState.confirmed: {OrderState.planned, OrderState.cancelled},
    OrderState.planned: {OrderState.planned, OrderState.out_for_delivery, OrderState.cancelled},
    OrderState.out_for_delivery: {OrderState.delivered, OrderState.failed},
    OrderState.failed: {OrderState.reattempt, OrderState.rto},
    OrderState.reattempt: {OrderState.planned, OrderState.rto, OrderState.cancelled},
    OrderState.delivered: set(), OrderState.rto: set(), OrderState.cancelled: set(),
}


def can_transition(a: OrderState, b: OrderState) -> bool:
    return b in ORDER_TRANSITIONS[a]


class Order(Model):
    id: str = Field(default_factory=lambda: new_id("order"))
    depot_id: str
    customer_id: str
    kind: OrderKind = OrderKind.delivery
    raw_address: str
    location: LatLng | None = None     # set by geocoding
    digipin: str | None = None
    geo_confidence: float | None = Field(default=None, ge=0, le=1)
    window_start: datetime | None = None
    window_end: datetime | None = None
    service_s: int = Field(default=120, ge=0)       # time at the door
    weight_kg: float = Field(default=1.0, ge=0)
    volume_l: float = Field(default=1.0, ge=0)
    payment: Payment = Payment.prepaid
    cod_amount: float = Field(default=0, ge=0)
    priority: Literal["normal", "urgent"] = "normal"
    attempts: int = Field(default=0, ge=0)
    verified_failures: int = Field(default=0, ge=0)   # failed attempts confirmed genuine (counts toward RTO)
    notes: str = ""
    risk: float | None = Field(default=None, ge=0, le=1)          # P(delivery fails), spec §10
    refuse_risk: float | None = Field(default=None, ge=0, le=1)   # P(customer refuses a COD parcel)
    confirm_requested_at: datetime | None = None                  # we asked the customer to confirm
    promised_at: datetime | None = None                           # arrival time told in that message
    customer_reply: str | None = None                             # last reply intent acted on (confirm, cancel...)
    state: OrderState = OrderState.new
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("digipin")
    @classmethod
    def _digipin(cls, v):
        if v is not None and (len(v) != 10 or "-" in v or " " in v):
            raise ValueError("digipin must be 10 characters, no separators")
        return v

    @model_validator(mode="after")
    def _window(self):
        if self.window_start and self.window_end and self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


# ---------------------------------------------------------------- plans
class Stop(Model):
    order_id: str
    seq: int = Field(ge=0)
    eta_p50: datetime
    eta_p90: datetime
    planned_arrival: datetime          # the time promised to the customer (promise policy, spec §9)
    leg_m: float = Field(default=0, ge=0)
    leg_s: float = Field(default=0, ge=0)
    call_first: bool = False           # unconfirmed risky order: the driver calls ahead (spec §10)


class VehicleRoute(Model):
    vehicle_id: str
    driver_id: str | None = None
    stops: list[Stop] = []
    distance_m: float = 0
    duration_s: float = 0
    load_kg: float = 0
    geometry: list[LatLng] = []        # road shape for the map; may be empty offline


class PlanScores(Model):
    """The five-measure critic carried over from v1 (each 0..1, higher is better) + totals."""
    on_time: float
    efficiency: float
    cycle: float
    balance: float
    resilience: float
    overall: float
    late_orders: int
    total_duration_s: float
    total_distance_m: float


class Plan(Model):
    id: str = Field(default_factory=lambda: new_id("plan"))
    depot_id: str
    created_at: datetime = Field(default_factory=utcnow)
    reason: str = ""                   # "initial", "incident inc_...", "insertion", ...
    params: dict[str, Any] = {}        # strategy weights used by the solver
    routes: list[VehicleRoute] = []
    unassigned: list[str] = []         # order ids
    scores: PlanScores | None = None
    parent_plan_id: str | None = None


# ---------------------------------------------------------------- incidents
class IncidentKind(str, Enum):
    flood = "flood"
    traffic = "traffic"
    closure = "closure"
    breakdown = "breakdown"
    other = "other"


class IncidentState(str, Enum):
    reported = "reported"
    active = "active"
    rejected = "rejected"              # triaged as not real
    closed = "closed"


class Incident(Model):
    id: str = Field(default_factory=lambda: new_id("incident"))
    kind: IncidentKind
    area: Area | None = None           # None for vehicle incidents
    vehicle_id: str | None = None
    severity: float = Field(default=0.5, ge=0, le=1)
    slowdown: float = Field(default=1.0, ge=1.0)   # predicted travel-time multiplier inside the area
    description: str = ""
    source: str = "ops"                # ops | customer | driver | weather | traffic | news
    starts_at: datetime = Field(default_factory=utcnow)
    ends_at: datetime | None = None
    state: IncidentState = IncidentState.reported


# ---------------------------------------------------------------- policies
class GateThresholds(Model):
    act: float = Field(default=0.90, ge=0, le=1)
    review: float = Field(default=0.50, ge=0, le=1)

    @model_validator(mode="after")
    def _order(self):
        if self.review > self.act:
            raise ValueError("review threshold must be <= act threshold")
        return self


class Policy(Model):
    version: int = Field(ge=1)
    delay_factor: dict[IncidentKind, float] = {IncidentKind.flood: 4.0, IncidentKind.traffic: 2.5,
                                               IncidentKind.closure: 10.0}
    gates: dict[str, GateThresholds] = {}          # decision type -> thresholds; missing = defaults
    solver: dict[str, Any] = {}
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str = "system"
    parent_version: int | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)   # one entry per simulated day (spec §9)

    def gate(self, decision_type: str) -> GateThresholds:
        return self.gates.get(decision_type, GateThresholds())
