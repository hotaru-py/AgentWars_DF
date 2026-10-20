"""Routing engines, incidents, OR-Tools solver, critic, retime, planner. Offline by default;
the OSRM contract test runs only when a local OSRM answers on OSRM_URL (default 127.0.0.1:5000)."""

import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from dispatch.core.models import (Circle, Customer, Depot, Driver, Incident, IncidentKind, IncidentState, LatLng,
                                  Order, OrderState, Vehicle, VehicleState)
from dispatch.core.ports import VrpProblem
from dispatch.optimize import STRATEGIES, OrToolsSolver, critic, retime, score_plan
from dispatch.planning import NothingToPlan, Planner
from dispatch.routing import FallbackEngine, HaversineEngine, OsrmEngine, apply_incidents, share_inside
from dispatch.store import Store
from tests.contracts.checks import check_routing_engine, check_solver

T0 = datetime(2026, 10, 18, 3, 30, tzinfo=timezone.utc)
DEPOT = LatLng(lat=13.0067, lng=80.2206)                          # Guindy
SPOTS = [(13.0418, 80.2341), (13.0850, 80.2101), (13.0012, 80.2565), (12.9815, 80.2180), (12.9249, 80.1000),
         (12.9010, 80.2279), (13.0382, 80.1565), (13.0368, 80.2676), (13.0732, 80.2609), (12.9830, 80.2594)]
OSRM_URL = os.environ.get("OSRM_URL", "http://127.0.0.1:5000")


def _osrm_up() -> bool:
    try:
        return httpx.get(f"{OSRM_URL}/nearest/v1/driving/80.22,13.0", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def _orders(n=10, **kw):
    return [Order(depot_id="dep_1", customer_id="cus_1", raw_address=f"stop {i}", location=LatLng(lat=a, lng=b),
                  weight_kg=kw.get("weight", 5), **{k: v for k, v in kw.items() if k != "weight"})
            for i, (a, b) in enumerate(SPOTS[:n])]


def _problem(orders, vans=3, cap=50, **kw):
    vehicles = kw.pop("vehicles", None) or [Vehicle(depot_id="dep_1", name=f"V{i+1}", capacity_kg=cap, capacity_l=500)
                                            for i in range(vans)]
    m = HaversineEngine().matrix([DEPOT] + [o.location for o in orders])
    return VrpProblem(depot_id="dep_1", depot=DEPOT, start_at=T0, orders=orders, vehicles=vehicles, matrix=m, **kw)


# ---------------------------------------------------------------- routing
def test_haversine_meets_contract():
    check_routing_engine(HaversineEngine())


@pytest.mark.skipif(not _osrm_up(), reason="no local OSRM (docker compose --profile osrm up -d)")
def test_osrm_meets_contract_and_is_near_haversine():
    m = check_routing_engine(OsrmEngine(OSRM_URL))
    h = HaversineEngine().matrix(m.points)
    for i in range(m.size()):
        for j in range(m.size()):
            if i != j:                                            # real roads within 0.6x-2.5x of the estimate
                assert 0.6 < m.meters[i][j] / h.meters[i][j] < 2.5


def test_fallback_engine_uses_next_engine_when_osrm_is_down():
    e = FallbackEngine(OsrmEngine("http://127.0.0.1:9", timeout_s=1), HaversineEngine())
    assert e.matrix([DEPOT, LatLng(lat=13.04, lng=80.23)]).source == "haversine"


def test_incident_share_and_matrix_slowdown():
    flood = Circle(center=LatLng(lat=12.942, lng=80.243), radius_m=5000)
    a_in, b_in = LatLng(lat=12.95, lng=80.24), LatLng(lat=12.93, lng=80.245)
    assert share_inside(a_in, b_in, flood) == 1.0
    assert share_inside(DEPOT, LatLng(lat=13.0850, lng=80.2101), flood) == 0.0
    m = HaversineEngine().matrix([DEPOT, a_in, LatLng(lat=13.0850, lng=80.2101)])
    inc = Incident(kind=IncidentKind.flood, area=flood, slowdown=4.0, state=IncidentState.active)
    wet = apply_incidents(m, [inc])
    assert wet.seconds[1][1] == 0 and wet.seconds[0][2] == m.seconds[0][2]          # Anna Nagar leg untouched
    assert m.seconds[0][1] < wet.seconds[0][1] <= 4 * m.seconds[0][1]               # partial share slowed
    reported = inc.model_copy(update={"state": IncidentState.reported})
    assert apply_incidents(m, [reported]) is m                                       # only active incidents count
    two = apply_incidents(m, [inc, inc])
    assert two.seconds[0][1] > wet.seconds[0][1]                                     # overlapping incidents multiply


def test_bad_zone_is_driven_around_through_a_via_point():
    """Two points on opposite sides of a small, badly flooded zone: going around beats going through."""
    eng = HaversineEngine()
    zone = Circle(center=LatLng(lat=13.0, lng=80.22), radius_m=1000)
    west, east = LatLng(lat=13.0, lng=80.19), LatLng(lat=13.0, lng=80.25)
    m = eng.matrix([west, east])
    flood = Incident(kind=IncidentKind.flood, area=zone, slowdown=8.0, state=IncidentState.active)
    through = apply_incidents(m, [flood])                         # no engine: slowed, no detours
    around = apply_incidents(m, [flood], eng)
    assert through.detours == {} and "0-1" in around.detours and "1-0" in around.detours
    assert m.seconds[0][1] < around.seconds[0][1] < through.seconds[0][1]          # around: longer than a dry day
    assert around.meters[0][1] > m.meters[0][1]                                     # ...and more km
    via = around.detours["0-1"]
    assert share_inside(west, via, zone) == 0 and share_inside(via, east, zone) == 0
    mild = flood.model_copy(update={"slowdown": 1.05})
    assert apply_incidents(m, [mild], eng).detours == {}                            # not worth the extra km


def test_detour_flows_into_stops_geometry_and_twin(tmp_path):
    from dispatch.core.models import Order as O
    from dispatch.twin import simulate_day
    st = Store(f"sqlite:///{tmp_path / 'd.db'}")
    st.create_all()
    west = LatLng(lat=13.0, lng=80.19)
    depot = Depot(name="W", location=west, opens_at=datetime.now(timezone.utc), closes_at=datetime.now(timezone.utc) + timedelta(hours=10))
    st.put(depot)
    st.put(Vehicle(depot_id=depot.id, name="V", capacity_kg=50, capacity_l=400))
    c = Customer(name="E", phone="1")
    st.put(c)
    o = O(depot_id=depot.id, customer_id=c.id, raw_address="east")
    st.create_order(o)
    st.set_geocode(o.id, LatLng(lat=13.0, lng=80.25), None, 1.0, "test")
    flood = Incident(kind=IncidentKind.flood, area=Circle(center=LatLng(lat=13.0, lng=80.22), radius_m=1000),
                     slowdown=8.0, state=IncidentState.active, severity=1.0, description="flood")
    st.save_incident(flood, "incident.triaged")
    pl = Planner(st, HaversineEngine(), OrToolsSolver(), time_limit_s=0.3)
    plan = pl.run(depot.id, "flood").plan
    stop = plan.routes[0].stops[0]
    assert stop.via is not None and stop.via in plan.routes[0].geometry              # the map draws the detour
    day = simulate_day(plan, {o.id: st.get(O, o.id)}, west, plan.params and depot.opens_at, HaversineEngine(),
                       [flood], run=1)
    assert day.stops[0].actual_s < day.stops[0].base_s * 2                         # the van went around, not through
    st.engine.dispose()


# ---------------------------------------------------------------- solver
def test_solver_meets_contract_and_plans_everything():
    plan = check_solver(OrToolsSolver(), _problem(_orders()))
    assert not plan.unassigned and len(plan.routes) == 3


def test_capacity_forces_unassigned_when_fleet_is_too_small():
    plan = check_solver(OrToolsSolver(), _problem(_orders(weight=30), vans=2, cap=50))
    assert len(plan.unassigned) == 8                              # a 50 kg van fits one 30 kg order: 2 of 10 planned


def test_broken_vehicle_gets_nothing():
    vans = [Vehicle(depot_id="dep_1", capacity_kg=50, capacity_l=500, state=VehicleState.broken_down),
            Vehicle(depot_id="dep_1", capacity_kg=100, capacity_l=500)]
    plan = check_solver(OrToolsSolver(), _problem(_orders(), vehicles=vans))
    assert [r.vehicle_id for r in plan.routes] == [vans[1].id] and not plan.unassigned


def test_time_windows_wait_and_urgent_deadlines():
    orders = _orders(4)
    orders[0].window_start = T0 + timedelta(hours=2)              # can't arrive before 11:00
    orders[1].window_end, orders[1].priority = T0 + timedelta(minutes=45), "urgent"   # Anna Nagar: ~28 min direct
    plan = check_solver(OrToolsSolver(), _problem(orders, vans=2))
    eta = {s.order_id: s.eta_p50 for r in plan.routes for s in r.stops}
    assert eta[orders[0].id] >= orders[0].window_start
    assert eta[orders[1].id] <= orders[1].window_end


def test_driver_shift_caps_route():
    vans = [Vehicle(depot_id="dep_1", capacity_kg=100, capacity_l=500) for _ in range(2)]
    short = Driver(name="Selvi", phone="1", vehicle_id=vans[0].id, shift_start=T0, shift_end=T0 + timedelta(minutes=30))
    plan = check_solver(OrToolsSolver(), _problem(_orders(), vehicles=vans, drivers=[short]))
    r0 = next(r for r in plan.routes if r.vehicle_id == vans[0].id)
    assert all(s.eta_p50 <= short.shift_end for s in r0.stops)


def test_insert_keeps_locked_prefix():
    orders = _orders(8)
    vans = [Vehicle(depot_id="dep_1", name=f"V{i+1}", capacity_kg=50, capacity_l=500) for i in range(3)]
    base = check_solver(OrToolsSolver(), _problem(orders, vehicles=vans))
    route = next(r for r in base.routes if len(r.stops) >= 2)
    prefix = [s.order_id for s in route.stops[:2]]
    new = _orders(10)[8:]
    p2 = OrToolsSolver().insert(base, new, _problem(orders + new, vehicles=vans, fixed_prefix={route.vehicle_id: prefix}))
    r2 = next(r for r in p2.routes if r.vehicle_id == route.vehicle_id)
    assert [s.order_id for s in r2.stops[:2]] == prefix and not p2.unassigned
    assert {s.order_id for r in p2.routes for s in r.stops} >= {o.id for o in new}


# ---------------------------------------------------------------- critic and retime
def test_critic_picks_best_valid_and_holds_against_better_baseline():
    prob = _problem(_orders())
    plans = [OrToolsSolver().solve(prob.model_copy(update={"params": st}), time_limit_s=0.5) for st in STRATEGIES]
    best, table = critic(plans, prob)
    assert best is not None and len(table) == 3 and best.scores.overall == max(r["overall"] for r in table)
    perfect = best.scores.model_copy(update={"overall": 1.01})
    held, _ = critic(plans, prob, baseline=perfect)
    assert held is None


def test_retime_matches_solver_and_reflects_incidents():
    prob = _problem(_orders())
    plan = OrToolsSolver().solve(prob, time_limit_s=0.5)
    same = retime(plan, prob)
    assert [[s.order_id for s in r.stops] for r in same.routes] == [[s.order_id for s in r.stops] for r in plan.routes]
    for r, r2 in zip(plan.routes, same.routes):
        for s, s2 in zip(r.stops, r2.stops):
            assert abs((s.eta_p50 - s2.eta_p50).total_seconds()) <= len(r.stops) + 1    # integer rounding only
    flood = Incident(kind=IncidentKind.flood, area=Circle(center=LatLng(lat=12.942, lng=80.243), radius_m=5000),
                     slowdown=4.0, state=IncidentState.active)
    wet = prob.model_copy(update={"matrix": apply_incidents(prob.matrix, [flood])})
    assert score_plan(retime(plan, wet), wet).total_duration_s > score_plan(same, prob).total_duration_s


# ---------------------------------------------------------------- planner (store + engine + solver + critic)
@pytest.fixture
def planner(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'plan.db'}")
    st.create_all()
    depot = Depot(name="Guindy", location=DEPOT, opens_at=T0, closes_at=T0 + timedelta(hours=10))
    st.put(depot)
    for i in range(3):
        st.put(Vehicle(depot_id=depot.id, name=f"V{i+1}", capacity_kg=50, capacity_l=500))
    cust = Customer(name="A", phone="1")
    st.put(cust)
    for o in _orders():
        o2 = o.model_copy(update={"depot_id": depot.id, "customer_id": cust.id, "location": None})
        st.create_order(o2)
        st.set_geocode(o2.id, o.location, None, 0.9, "test")
    yield Planner(st, HaversineEngine(), OrToolsSolver(), time_limit_s=0.5), st, depot
    st.engine.dispose()


def test_planner_applies_logs_and_holds_when_nothing_changes(planner):
    pl, st, depot = planner
    r1 = pl.run(depot.id, "initial")
    assert r1.verdict == "apply" and st.current_plan(depot.id).id == r1.plan.id
    assert all(o.state == OrderState.planned for o in st.list(Order, depot_id=depot.id))
    assert all(len(r.geometry) >= 2 for r in r1.plan.routes if r.stops)
    d = st.get_decision(r1.decision.id)
    assert d.type == "plan_select" and d.action.ref == r1.plan.id and len(d.cost["candidates"]) == 3
    r2 = pl.run(depot.id, "again")                                 # nothing changed: may only apply if not worse
    assert r2.verdict in ("apply", "hold") and (r2.verdict == "hold" or r2.plan.scores.overall >= r2.baseline["overall"])


def test_planner_replans_around_active_incident(planner):
    pl, st, depot = planner
    first = pl.run(depot.id, "initial")
    flood = Incident(kind=IncidentKind.flood, area=Circle(center=LatLng(lat=12.942, lng=80.243), radius_m=5000),
                     slowdown=4.0, state=IncidentState.active, description="OMR flood")
    st.save_incident(flood, "incident.triaged")
    r = pl.run(depot.id, f"incident {flood.id}")
    assert r.baseline is not None and r.baseline["total_duration_s"] > first.plan.scores.total_duration_s
    if r.verdict == "apply":
        assert r.plan.scores.overall >= r.baseline["overall"] and r.plan.parent_plan_id == first.plan.id


def test_planner_with_nothing_to_plan(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'empty.db'}")
    st.create_all()
    d = Depot(name="X", location=DEPOT, opens_at=T0, closes_at=T0 + timedelta(hours=1))
    st.put(d)
    with pytest.raises(NothingToPlan):
        Planner(st, HaversineEngine(), OrToolsSolver()).run(d.id)
    st.engine.dispose()
