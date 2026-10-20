"""Digital twin (day replay) and learner. Offline: straight-line routing, no AI."""

import os

import pytest
from fastapi.testclient import TestClient

from dispatch import demo
from dispatch.api import create_app
from dispatch.core.config import Settings
from dispatch.core.models import Incident, IncidentKind, Order, Policy
from dispatch.decide import DecisionEngine
from dispatch.incidents import IncidentService
from dispatch.optimize import OrToolsSolver
from dispatch.planning import Planner
from dispatch.routing import HaversineEngine
from dispatch.store import Store, StoreError
from dispatch.twin import TRUTH, DayResult, learn, run_day, simulate_day
from dispatch.twin.day import plan_start
from dispatch.twin.simulate import NOISE, StopTimes

ENGINE = HaversineEngine()


@pytest.fixture
def store(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 't.db'}")
    st.create_all()
    yield st
    st.engine.dispose()


def _day(store, flood=True, reset=False):
    d = demo.reset(store) if reset else demo.seed(store)
    pl = Planner(store, ENGINE, OrToolsSolver(), time_limit_s=0.3)
    pl.run(d.id, "initial")
    if flood:
        inc = Incident(kind=IncidentKind.other, description="Severe flooding on OMR near Sholinganallur")
        store.save_incident(inc)
        IncidentService(store, DecisionEngine(store, {}, Settings()), pl).resolve(inc.id, "ops", "flood", 1.0)
    return d


def test_quiet_day_has_no_error_and_nothing_to_learn(store):
    d = _day(store, flood=False)
    plan = store.current_plan(d.id)
    orders = {o.id: o for o in store.list(Order, depot_id=d.id)}
    res = simulate_day(plan, orders, d.location, plan_start(plan), ENGINE, store.list(Incident), run=1)
    assert res.delay_error() is None and res.observed_factor == {}
    assert all(abs(s.predicted_s - s.actual_s) < 1e-6 and abs(s.base_s - s.actual_s) < 1e-6 for s in res.stops)


def test_flood_day_reveals_true_factor(store):
    d = _day(store)
    plan = store.current_plan(d.id)
    orders = {o.id: o for o in store.list(Order, depot_id=d.id)}
    res = simulate_day(plan, orders, d.location, plan_start(plan), ENGINE, store.list(Incident), run=1)
    seen = res.observed_factor[IncidentKind.flood]
    assert TRUTH[IncidentKind.flood] * (1 - NOISE) - 1e-6 <= seen <= TRUTH[IncidentKind.flood] * (1 + NOISE) + 1e-6
    assert res.delay_error() > 0.1                         # policy guessed x4.0, truth ~x5: clearly off


def test_learner_steps_halfway_caps_and_rolls_back():
    p = Policy(version=1)
    day = DayResult(stops=[StopTimes("o", 100, 150, 200, None)], observed_factor={IncidentKind.flood: 5.0})
    p2, ch, rb = learn(p, day, run=1)
    assert ch["flood"] == [4.0, 4.5] and p2.version == 2 and p2.parent_version == 1 and not rb
    big = DayResult(stops=day.stops, observed_factor={IncidentKind.flood: 40.0})
    assert learn(p, big, run=1)[1]["flood"][1] == 5.0           # capped at +25%
    hist = [{"run": 1, "kind": "flood", "delay_error": 0.1}, {"run": 2, "kind": "flood", "delay_error": 0.2}]
    worse = Policy(version=3, delay_factor={IncidentKind.flood: 4.8}, history=hist)
    prev = Policy(version=2, delay_factor={IncidentKind.flood: 4.4})
    p4, ch4, rb4 = learn(worse, DayResult(stops=[StopTimes("o", 100, 150, 400, None)], observed_factor={IncidentKind.flood: 6.0}),
                         run=3, previous=prev)
    assert rb4 == ["flood"] and ch4["flood"] == [4.8, 4.4]     # error rose twice: back to the previous factor


def test_repeated_days_converge_and_learning_survives_demo_reset(store):
    errors, factors = [], []
    for day in range(4):
        d = _day(store, reset=day > 0)
        r = run_day(store, ENGINE, d.id)
        errors.append(r.delay_error)
        factors.append(r.changes["flood"][1])
    assert errors[0] > errors[1] > errors[2] and errors[-1] < 0.1
    assert abs(factors[-1] - TRUTH[IncidentKind.flood]) < 0.3
    pol = store.current_policy()
    assert pol.version == 5 and [h["run"] for h in pol.history] == [1, 2, 3, 4]
    assert len(store.events_since(0, types=["day.simulated"])) == 1     # this day's report (earlier days were reset)
    demo.reset(store, keep_learning=False)
    assert store.current_policy().version == 1 and store.current_policy().history == []


def test_run_day_needs_a_plan(store):
    d = demo.seed(store)
    with pytest.raises(StoreError, match="plan routes first"):
        run_day(store, ENGINE, d.id)


def test_api_day_run_and_reset_keeps_learning(store, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    d = _day(store)
    planner = Planner(store, ENGINE, OrToolsSolver(), time_limit_s=0.3)
    with TestClient(create_app(store, planner=planner, settings=Settings())) as c:
        r = c.post("/day/run", params={"depot_id": d.id}).json()
        assert r["run"] == 1 and r["changes"]["flood"][0] == 4.0 and r["policy_version"] == 2
        assert c.get("/state").json()["policy"]["history"][0]["kind"] == "flood"
        c.post("/demo/reset")
        assert c.get("/policy").json()["version"] == 2                  # kept
        c.post("/demo/reset", params={"forget": "true"})
        assert c.get("/policy").json()["version"] == 1                  # forgotten
    assert os.environ["DEMO_MODE"] == "1"
