"""Reusable contract checks for every port implementation (spec §6.1).

Each track calls the matching check from its own tests, e.g.

    from tests.contracts.checks import check_routing_engine
    def test_osrm_meets_contract(): check_routing_engine(OsrmEngine(url))

The checks assert behaviour every caller relies on, not implementation details.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from dispatch.core.decisions import (Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer)
from dispatch.core.models import LatLng, OrderState, Plan
from dispatch.core.ports import (Decider, EtaContext, EtaModel, Geocoder, Leg, Matrix, Messenger,
                                 RoutingEngine, SignalSource, Solver, VrpProblem)

CHENNAI = [LatLng(lat=13.0067, lng=80.2206), LatLng(lat=13.0418, lng=80.2341),
           LatLng(lat=13.0012, lng=80.2565), LatLng(lat=12.9815, lng=80.2180)]


def check_routing_engine(engine: RoutingEngine, points: list[LatLng] = CHENNAI) -> Matrix:
    assert isinstance(engine, RoutingEngine) and engine.name
    m = engine.matrix(points)
    n = len(points)
    assert m.points == points and m.source
    assert len(m.seconds) == n and all(len(r) == n for r in m.seconds)
    assert len(m.meters) == n and all(len(r) == n for r in m.meters)
    for i in range(n):
        assert m.seconds[i][i] == 0 and m.meters[i][i] == 0
        for j in range(n):
            assert m.seconds[i][j] >= 0 and m.meters[i][j] >= 0
            if i != j:
                assert m.seconds[i][j] > 0, "distinct points must take time to travel"
    shape = engine.route(points[:3])
    assert len(shape.legs) == 2
    return m


def check_solver(solver: Solver, problem: VrpProblem) -> Plan:
    assert isinstance(solver, Solver)
    plan = solver.solve(problem, time_limit_s=2)
    planned = [s.order_id for r in plan.routes for s in r.stops]
    ids = {o.id for o in problem.orders}
    assert len(planned) == len(set(planned)), "an order appears twice"
    assert set(planned) | set(plan.unassigned) == ids, "every order is planned or listed unassigned"
    active = {v.id for v in problem.vehicles if v.state == "active"}
    assert {r.vehicle_id for r in plan.routes if r.stops} <= active, "only active vehicles get stops"
    for r in plan.routes:
        cap = next(v.capacity_kg for v in problem.vehicles if v.id == r.vehicle_id)
        load = sum(o.weight_kg for o in problem.orders if o.id in {s.order_id for s in r.stops})
        assert load <= cap + 1e-6, f"{r.vehicle_id} over capacity"
        assert [s.seq for s in r.stops] == list(range(len(r.stops)))
        assert all(s.eta_p50 <= s.eta_p90 for s in r.stops)
        assert all(a.eta_p50 <= b.eta_p50 for a, b in zip(r.stops, r.stops[1:])), "ETAs go forward"
    return plan


def check_decider(decider: Decider) -> None:
    assert isinstance(decider, Decider) and decider.name and decider.name.isidentifier()
    assert decider.capabilities and decider.capabilities <= {"text", "tabular", "image"}
    qs = {"kind": Choice(instructions="Kind of incident", criteria={"flood": "Water on the road",
                                                                   "traffic": "Slow or stopped vehicles"}),
          "severity": Score(instructions="How badly it slows deliveries", levels=["low", "medium", "high"]),
          "credible": Noul(instructions="The report describes a real, current event")}
    ans = asyncio.run(decider.decide("Knee-deep water on OMR near Thoraipakkam, bikes stalling", qs))
    assert set(ans) == set(qs), "one answer per question, same keys"
    assert isinstance(ans["kind"], ChoiceAnswer) and ans["kind"].choice in qs["kind"].criteria
    assert abs(sum(ans["kind"].probabilities.values()) - 1) < 1e-6 and 0 <= ans["kind"].confidence <= 1
    assert isinstance(ans["severity"], ScoreAnswer) and len(ans["severity"].probabilities) == 3
    assert isinstance(ans["credible"], NoulAnswer) and 0 <= ans["credible"].p <= 1


def check_geocoder(geocoder: Geocoder, raw: str = "12, 3rd Main Rd, near Adyar bus depot, Chennai 600020") -> None:
    assert isinstance(geocoder, Geocoder)
    r = asyncio.run(geocoder.geocode(raw))
    assert 0 <= r.confidence <= 1 and r.method
    if r.location is None:
        assert r.confidence < 0.5, "no location must mean low confidence"


def check_eta_model(model: EtaModel) -> None:
    assert isinstance(model, EtaModel)
    legs = [Leg(seconds=600, meters=5000), Leg(seconds=300, meters=2000, incident_share=0.5, incident_kind="flood")]
    q = model.predict(legs, EtaContext(depart_at=datetime(2026, 10, 17, 3, 30, tzinfo=timezone.utc)))
    assert len(q) == len(legs)
    assert all(x.p50_s <= x.p90_s for x in q)
    assert all(a.p50_s <= b.p50_s for a, b in zip(q, q[1:])), "cumulative arrival times"


def check_messenger(messenger: Messenger, customer) -> None:
    assert isinstance(messenger, Messenger)
    ref = asyncio.run(messenger.send(customer, "eta_update", {"eta": "11:30 AM", "reason": "we've updated our delivery routes"}))
    assert ref.id and ref.sent_at.tzinfo is not None


def check_signal_source(source: SignalSource) -> None:
    assert isinstance(source, SignalSource) and source.name
    out = asyncio.run(source.poll(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert all(s.observed_at.tzinfo is not None for s in out)


__all__ = ["check_routing_engine", "check_solver", "check_decider", "check_geocoder", "check_eta_model",
           "check_messenger", "check_signal_source", "CHENNAI", "OrderState"]
