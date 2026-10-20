"""dispatch.store: runs on SQLite always, and also on Postgres when TEST_DATABASE_URL is set, e.g.
TEST_DATABASE_URL=postgresql+psycopg://dispatch:dispatch@127.0.0.1:5432/dispatch_test pytest tests/test_store.py
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from dispatch.core.decisions import Choice, ChoiceAnswer, DecisionRecord, Route
from dispatch.core.models import (Circle, Customer, Depot, GateThresholds, Incident, IncidentKind, IncidentState,
                                  LatLng, Order, OrderState, Plan, Policy, Stop, Vehicle, VehicleRoute)
from dispatch.store import Conflict, InvalidTransition, NotFound, Store

T0 = datetime(2026, 10, 18, 3, 30, tzinfo=timezone.utc)
BACKENDS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_DATABASE_URL") else [])


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}" if request.param == "sqlite" else os.environ["TEST_DATABASE_URL"]
    st = Store(url)
    st.drop_all()
    st.create_all()
    yield st
    st.drop_all()
    st.engine.dispose()


@pytest.fixture
def world(store):
    depot = Depot(name="Guindy", location=LatLng(lat=13.0067, lng=80.2206), opens_at=T0, closes_at=T0 + timedelta(hours=10))
    store.put(depot)
    van = Vehicle(depot_id=depot.id, name="V1", capacity_kg=50, capacity_l=300)
    store.put(van)
    cust = Customer(name="Priya", phone="+91-90000-00006")
    store.put(cust)
    return depot, van, cust


def _order(depot, cust, **kw):
    return Order(depot_id=depot.id, customer_id=cust.id, raw_address="12, 3rd Main Rd, near bus depot, Adyar", **kw)


def test_reference_documents_round_trip(store, world):
    depot, van, cust = world
    assert store.get(Depot, depot.id) == depot
    assert store.require(Vehicle, van.id).capacity_kg == 50
    assert store.list(Vehicle, depot_id=depot.id) == [van]
    assert store.get(Customer, "cus_missing") is None
    with pytest.raises(NotFound):
        store.require(Customer, "cus_missing")


def test_order_lifecycle_writes_matching_events(store, world):
    depot, _, cust = world
    o = _order(depot, cust, payment="cod", cod_amount=499)
    start = store.last_seq()
    store.create_order(o)
    store.set_geocode(o.id, LatLng(lat=13.0012, lng=80.2565), "4P3JK852C9", 0.92, "nominatim")
    for st in (OrderState.planned, OrderState.out_for_delivery, OrderState.failed, OrderState.reattempt):
        store.transition_order(o.id, st, reason="test")
    got = store.require(Order, o.id)
    assert got.state == OrderState.reattempt and got.attempts == 1 and got.digipin == "4P3JK852C9"
    evs = store.events_since(start, entity=o.id)
    assert [e.type for e in evs] == ["order.created", "order.geocoded", "order.state_changed", "order.state_changed",
                                     "order.state_changed", "order.state_changed", "order.state_changed"]
    assert [e.payload["after"] for e in evs if e.type == "order.state_changed"] == \
        ["geocoded", "planned", "out_for_delivery", "failed", "reattempt"]
    assert all(e.ts.tzinfo is not None for e in evs)
    assert [e.seq for e in evs] == sorted(e.seq for e in evs)


def test_illegal_transition_is_rejected_and_writes_nothing(store, world):
    depot, _, cust = world
    o = _order(depot, cust)
    store.create_order(o)
    seq = store.last_seq()
    with pytest.raises(InvalidTransition):
        store.transition_order(o.id, OrderState.delivered)
    assert store.require(Order, o.id).state == OrderState.new and store.last_seq() == seq


def test_duplicate_order_is_a_conflict(store, world):
    depot, _, cust = world
    o = _order(depot, cust)
    store.create_order(o)
    with pytest.raises(Conflict):
        store.create_order(o)


def test_order_needs_existing_depot_and_customer(store, world):
    depot, _, _ = world
    with pytest.raises(sa.exc.IntegrityError):
        store.create_order(Order(depot_id=depot.id, customer_id="cus_nobody", raw_address="x"))


def test_events_are_append_only_in_the_database(store, world):
    depot, _, cust = world
    store.create_order(_order(depot, cust))
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        with store.tx() as c:
            c.execute(sa.text("UPDATE events SET type = 'tampered'"))
    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        with store.tx() as c:
            c.execute(sa.text("DELETE FROM events"))
    assert store.events_since(0)[0].type == "order.created"


def test_plan_apply_moves_orders_and_tracks_current(store, world):
    depot, van, cust = world
    orders = [_order(depot, cust) for _ in range(2)]
    for o in orders:
        store.create_order(o)
        store.set_geocode(o.id, LatLng(lat=13.0, lng=80.25), None, 0.9, "test")
    stops = [Stop(order_id=o.id, seq=i, eta_p50=T0 + timedelta(minutes=20 * i), eta_p90=T0 + timedelta(minutes=20 * i + 9),
                  planned_arrival=T0 + timedelta(minutes=20 * i)) for i, o in enumerate(orders)]
    p1 = Plan(depot_id=depot.id, reason="initial", routes=[VehicleRoute(vehicle_id=van.id, stops=stops)])
    store.save_plan(p1)
    with pytest.raises(Conflict):
        store.save_plan(p1)                                   # plans are immutable
    assert store.current_plan(depot.id) is None
    store.apply_plan(p1.id)
    assert store.current_plan(depot.id).id == p1.id
    assert {store.require(Order, o.id).state for o in orders} == {OrderState.planned}
    p2 = Plan(depot_id=depot.id, reason="incident", routes=p1.routes, parent_plan_id=p1.id)
    store.save_plan(p2)
    store.apply_plan(p2.id, decision_id="dec_x")
    applied = [e for e in store.events_since(0, types=["plan.applied"])]
    assert store.current_plan(depot.id).id == p2.id
    assert applied[-1].payload == {"plan_id": p2.id, "replaced_plan_id": p1.id, "decision_id": "dec_x"}


def test_decision_ledger_outcome_once_and_rows_immutable(store):
    q = {"kind": Choice(instructions="kind", criteria={"flood": "water", "traffic": "jam"})}
    rec = DecisionRecord(type="incident_triage", subject="inc_1", decider="llm", method="logprobs", questions=q,
                         answers={"kind": ChoiceAnswer.from_probabilities({"flood": 0.9, "traffic": 0.1})},
                         raw_confidence=0.8, confidence=0.75, gate=GateThresholds(), route=Route.review)
    store.record_decision(rec)
    assert store.get_decision(rec.id) == rec
    store.set_outcome(rec.id, "incident_real")
    got = store.get_decision(rec.id)
    assert got.outcome == "incident_real" and got.outcome_at.tzinfo is not None
    with pytest.raises(Conflict):
        store.set_outcome(rec.id, "changed_my_mind")
    with pytest.raises(NotFound):
        store.set_outcome("dec_missing", "x")
    with pytest.raises(sa.exc.DBAPIError, match="immutable"):
        with store.tx() as c:
            c.execute(sa.text("UPDATE decisions SET route = 'act'"))
    with pytest.raises(sa.exc.DBAPIError, match="cannot be deleted"):
        with store.tx() as c:
            c.execute(sa.text("DELETE FROM decisions"))
    assert [d.id for d in store.decisions(type_="incident_triage", route="review")] == [rec.id]


def test_incidents_and_policy_versions(store):
    inc = Incident(kind=IncidentKind.flood, area=Circle(center=LatLng(lat=12.94, lng=80.24), radius_m=5000),
                   slowdown=4.0, description="waterlogging on OMR", source="customer")
    store.save_incident(inc)
    inc.state = IncidentState.active
    store.save_incident(inc, "incident.triaged")
    assert store.require(Incident, inc.id).state == IncidentState.active
    assert store.current_policy().version == 1                 # default before any is saved
    store.save_policy(Policy(version=1))
    p2 = Policy(version=2, parent_version=1, delay_factor={IncidentKind.flood: 4.43, IncidentKind.traffic: 2.5})
    store.save_policy(p2, reason="learned from run 1")
    with pytest.raises(Conflict):
        store.save_policy(Policy(version=5))                    # versions can't skip
    cur = store.current_policy()
    assert cur.version == 2 and cur.delay_factor[IncidentKind.flood] == 4.43
    assert store.events_since(0, types=["policy.updated"])[-1].payload["reason"] == "learned from run 1"
