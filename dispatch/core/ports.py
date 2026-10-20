"""Ports: the interfaces between tracks (spec §6.1). Implementations live in their track's package
and must pass tests/contracts/. Changing a signature here changes the contract: update the spec too.

    RoutingEngine  dispatch/routing/   (osrm, haversine, fallback)
    Solver         dispatch/optimize/  (OR-Tools)
    Decider        dispatch/decide/    (rules, ml, llm; optional jev or plug-ins)
    Messenger      dispatch/messaging/ (sim)
    SignalSource, Geocoder, EtaModel   contracts only: their phases are out of scope (specs/roadmap.md)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from .decisions import Answer, Question
from .models import (Customer, Driver, Incident, LatLng, Model, Order, Plan, Vehicle)


# ---------------------------------------------------------------- routing IO
class Matrix(Model):
    """Travel between `points` (same order as requested). seconds[i][j], meters[i][j]."""
    points: list[LatLng]
    seconds: list[list[float]]
    meters: list[list[float]]
    source: str = ""                   # "osrm", "cached", "haversine"
    detours: dict[str, LatLng] = {}    # "i-j" -> via point: going around an incident beat going through

    def size(self) -> int:
        return len(self.points)


class RouteLeg(Model):
    seconds: float = Field(ge=0)
    meters: float = Field(ge=0)
    geometry: list[LatLng] = []


class RouteShape(Model):
    legs: list[RouteLeg]               # len(points) - 1 legs
    geometry: list[LatLng] = []        # whole route, legs concatenated


# ---------------------------------------------------------------- optimization IO
class VrpProblem(Model):
    """Everything the solver needs. Travel times already include incident slowdowns (the caller
    applies them to the matrix), so the solver stays a pure optimizer."""
    depot_id: str
    depot: LatLng
    start_at: datetime                 # earliest departure
    orders: list[Order]                # all must have `location`
    vehicles: list[Vehicle]            # only state == active are used
    drivers: list[Driver] = []
    matrix: Matrix                     # points = [depot] + [o.location for o in orders]
    params: dict[str, Any] = {}        # late_penalty, balance_weight, urgent_factor, ...
    fixed_prefix: dict[str, list[str]] = {}   # vehicle_id -> order ids already done/committed (live insertion)
    not_before: dict[str, datetime] = {}      # order_id -> soft earliest arrival (unconfirmed risky orders)


# ---------------------------------------------------------------- ETA IO
class Leg(Model):
    seconds: float = Field(ge=0)       # base travel time from the routing engine
    meters: float = Field(ge=0)
    incident_share: float = Field(default=0, ge=0, le=1)   # share of the leg inside an active incident
    incident_kind: str | None = None


class EtaContext(Model):
    depart_at: datetime
    zone: str | None = None            # e.g. DIGIPIN prefix or ward
    vehicle_kind: str = "van"


class EtaQuantiles(Model):
    p50_s: float = Field(ge=0)         # seconds from depart_at to arrival at the stop
    p90_s: float = Field(ge=0)


# ---------------------------------------------------------------- address IO
class GeoResult(Model):
    location: LatLng | None
    digipin: str | None = None
    confidence: float = Field(ge=0, le=1)
    method: str                        # "cache", "nominatim", "digipin", "pin_drop", ...
    candidates: list[LatLng] = []
    parts: dict[str, str] = {}         # house, street, landmark, area, pincode


# ---------------------------------------------------------------- messaging / signals IO
class MessageRef(Model):
    id: str
    channel: Literal["whatsapp", "sms", "voice", "sim"]
    sent_at: datetime
    provider_ref: str | None = None


class RawSignal(Model):
    id: str
    source: str                        # "weather", "traffic", "news", "customer", "driver"
    observed_at: datetime
    text: str = ""
    location: LatLng | None = None
    data: dict[str, Any] = {}


# ---------------------------------------------------------------- ports
@runtime_checkable
class RoutingEngine(Protocol):
    name: str

    def matrix(self, points: list[LatLng], depart_at: datetime | None = None) -> Matrix: ...

    def route(self, points: list[LatLng]) -> RouteShape: ...


@runtime_checkable
class Solver(Protocol):
    def solve(self, problem: VrpProblem, time_limit_s: float = 5) -> Plan: ...

    def insert(self, plan: Plan, orders: list[Order], problem: VrpProblem, time_limit_s: float = 2) -> Plan: ...


@runtime_checkable
class Decider(Protocol):
    """One decision provider (spec §7.2). Returns RAW probabilities; calibration happens in decide/."""
    name: str                          # "rules" | "ml" | "llm" | "jev" | any plug-in
    model: str | None
    capabilities: set[str]             # subset of {"text", "tabular", "image"}

    async def decide(self, state: dict[str, Any] | str, questions: dict[str, Question]) -> dict[str, Answer]: ...


@runtime_checkable
class Geocoder(Protocol):
    async def geocode(self, raw: str, hint: LatLng | None = None) -> GeoResult: ...


@runtime_checkable
class EtaModel(Protocol):
    def predict(self, legs: list[Leg], context: EtaContext) -> list[EtaQuantiles]: ...


@runtime_checkable
class Messenger(Protocol):
    async def send(self, to: Customer, template: str, vars: dict[str, Any], channel: str = "whatsapp") -> MessageRef: ...


@runtime_checkable
class SignalSource(Protocol):
    name: str

    async def poll(self, since: datetime) -> list[RawSignal]: ...


__all__ = [
    "Matrix", "RouteLeg", "RouteShape", "VrpProblem", "Leg", "EtaContext", "EtaQuantiles", "GeoResult",
    "MessageRef", "RawSignal", "RoutingEngine", "Solver", "Decider", "Geocoder", "EtaModel", "Messenger",
    "SignalSource", "Incident", "Plan", "Order", "Vehicle", "Driver", "Customer", "LatLng",
]
