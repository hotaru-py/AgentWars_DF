"""Failed-delivery prevention (spec §10, P3): risk scoring, confirmations, failed-attempt verify,
reattempt / RTO, and the twin comparison. Offline: straight-line routing, rules only, no AI."""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from dispatch import demo
from dispatch.agent import ToolContext, make_tools
from dispatch.api import create_app
from dispatch.core.config import Settings
from dispatch.core.events import MessageEvent, StopEvent
from dispatch.core.ids import new_id
from dispatch.core.models import Customer, LatLng, Order, OrderState, Vehicle, utcnow
from dispatch.core.ports import VrpProblem
from dispatch.decide import DecisionEngine
from dispatch.decide.catalogue import FAILED_ATTEMPT
from dispatch.decide.risk import (LEVEL_P, attempt_rule, estimate, levels_for, p_from_levels, reason_from_text,
                                  risk_features)
from dispatch.incidents import IncidentService
from dispatch.messaging import SimMessenger
from dispatch.messaging.templates import render
from dispatch.optimize import OrToolsSolver
from dispatch.planning import Planner
from dispatch.prevention import PreventionService
from dispatch.replies import ReplyService
from dispatch.routing import HaversineEngine
from dispatch.store import Store
from dispatch.twin.prevention import prevention_report

run = asyncio.run
RISKY = {"Divya Raman", "Rahul Nair", "Suresh Babu", "Vignesh R", "Anitha Joseph"}      # demo.PROFILE


@pytest.fixture
def world(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'p.db'}")
    st.create_all()
    depot = demo.seed(st)
    planner = Planner(st, HaversineEngine(), OrToolsSolver(), time_limit_s=0.3)
    eng = DecisionEngine(st, {}, Settings())
    m = SimMessenger(st)
    svc = PreventionService(st, eng, planner, m, Settings())
    yield st, depot, planner, eng, m, svc
    st.engine.dispose()


def _names(st):
    return {c.id: c.name for c in st.list(Customer)}


def _sent(st, template_word):
    return [e for e in st.events_since(0, types=["message.sent"], limit=10_000) if template_word in e.payload["text"]]


# ---------------------------------------------------------------- the rules
def test_risk_estimate_reads_history_payment_and_address():
    regular, _ = estimate(risk_features({"payment": "prepaid", "geo_confidence": 1.0}, {"delivered": 6}))
    newcod, refuse_new = estimate(risk_features({"payment": "cod", "geo_confidence": 0.6}, {}))
    flaky, _ = estimate(risk_features({"payment": "prepaid", "geo_confidence": 1.0}, {"delivered": 2, "failed": 3}))
    assert regular < 0.05 < 0.15 < newcod < flaky
    _, refuse_prepaid = estimate(risk_features({"payment": "prepaid"}, {}))
    _, refuse_refuser = estimate(risk_features({"payment": "cod", "geo_confidence": 1.0},
                                               {"delivered": 2, "failed": 1, "refused": 1}))
    assert refuse_prepaid == 0 and refuse_refuser > refuse_new > 0


def test_risk_levels_round_trip():
    for p in (0.025, 0.07, 0.2, 0.33, 0.6, 0.7):
        dist = levels_for(p)
        assert abs(sum(dist) - 1) < 1e-9 and abs(p_from_levels(dist) - p) < 1e-9
    assert p_from_levels(levels_for(0.0)) == LEVEL_P[0] and p_from_levels(levels_for(1.0)) == LEVEL_P[-1]


def test_attempt_rule_geofence_and_dwell():
    q = FAILED_ATTEMPT.questions
    far = attempt_rule({"distance_m": 900, "dwell_s": 300, "reason_text": "customer not home"}, q)
    assert far["genuine"].p == 0
    near = attempt_rule({"distance_m": 40, "dwell_s": 180, "reason_text": "Customer refused, no cash"}, q)
    assert near["genuine"].p == 1 and near["reason"].choice == "refused"
    assert attempt_rule({"distance_m": 40, "dwell_s": 5, "reason_text": "not home"}, q) is None      # too quick
    assert attempt_rule({"distance_m": None, "dwell_s": None, "reason_text": "not home"}, q) is None  # no GPS
    assert attempt_rule({"distance_m": 250, "dwell_s": 300, "reason_text": "not home"}, q) is None   # in between
    assert reason_from_text("couldn't find the building") == "wrong_address"
    assert reason_from_text("door locked, phone switched off") == "not_home"
    assert reason_from_text("big dog at the gate") == "unsafe"


def test_new_templates_stay_short():
    assert "try again" in render("reattempt_offer", {"name": "Divya"})
    assert "returned" in render("returning", {"name": "Divya"})


# ---------------------------------------------------------------- before dispatch
def test_risk_check_messages_only_risky_customers(world):
    st, depot, *_, svc = world
    r = run(svc.run(depot.id))
    assert r.planned_first and len(r.scored) == 10
    names = _names(st)
    risky = {x.customer for x in r.scored if x.risky}
    assert risky == RISKY and {x.customer for x in r.messaged} == RISKY
    msgs = _sent(st, "Reply 1 to confirm")
    assert {names[e.payload["customer_id"]] for e in msgs} == RISKY
    assert all(len(e.payload["text"].split()) < 40 and "arrives today between" in e.payload["text"] for e in msgs)
    divya = next(o for o in st.list(Order) if names[o.customer_id] == "Divya Raman")
    assert divya.risk > 0.2 and divya.confirm_requested_at and divya.promised_at
    ledger = st.decisions(type_="delivery_risk", limit=100)
    assert len(ledger) == 10 and {d.decider for d in ledger} == {"rules"}
    again = run(svc.run(depot.id))                                     # no second message to the same people
    assert not again.planned_first and not again.messaged and len(_sent(st, "Reply 1 to confirm")) == 5


def test_unconfirmed_orders_call_first_until_the_customer_replies(world):
    st, depot, planner, eng, m, svc = world
    run(svc.run(depot.id))
    planner.run(depot.id, "dispatch")
    names = _names(st)
    plan = st.current_plan(depot.id)
    flagged = {names[st.get(Order, s.order_id).customer_id] for r in plan.routes for s in r.stops if s.call_first}
    assert flagged == RISKY
    divya = next(o for o in st.list(Order) if names[o.customer_id] == "Divya Raman")
    replies = ReplyService(st, eng, planner, m)
    msg = MessageEvent(message_id=new_id("message"), customer_id=divya.customer_id, order_id=divya.id,
                       channel="whatsapp", text="1")
    st.record_message("message.received", msg)
    assert run(replies.handle(msg)).result == "confirmed"
    assert st.get(Order, divya.id).customer_reply == "confirm"
    planner.run(depot.id, "after replies")
    plan = st.current_plan(depot.id)
    flagged = {names[st.get(Order, s.order_id).customer_id] for r in plan.routes for s in r.stops if s.call_first}
    assert flagged == RISKY - {"Divya Raman"}


def test_solver_keeps_an_order_no_earlier_than_promised():
    """Two neighbouring stops: holding the first one back swaps their order instead of idling the van;
    a lone held-back stop is served no earlier than promised."""
    eng = HaversineEngine()
    depot = LatLng(lat=13.0067, lng=80.2206)
    near = Order(depot_id="d", customer_id="c", raw_address="near", location=LatLng(lat=13.0400, lng=80.2206))
    far = Order(depot_id="d", customer_id="c", raw_address="far", location=LatLng(lat=13.0420, lng=80.2215))
    van = Vehicle(depot_id="d", name="V", capacity_kg=50, capacity_l=400)
    start = utcnow()
    pts = [depot, near.location, far.location]
    base = VrpProblem(depot_id="d", depot=depot, start_at=start, orders=[near, far], vehicles=[van], matrix=eng.matrix(pts))
    stops = lambda p: OrToolsSolver().solve(p, time_limit_s=0.3).routes[0].stops  # noqa: E731
    first = stops(base)
    assert [s.order_id for s in first] == [near.id, far.id]
    hold = first[1].eta_p50                                       # "no earlier than when you'd reach the 2nd stop"
    swapped = stops(base.model_copy(update={"not_before": {near.id: hold}}))
    assert [s.order_id for s in swapped] == [far.id, near.id]
    alone = base.model_copy(update={"orders": [near], "matrix": eng.matrix(pts[:2]),
                                    "not_before": {near.id: start + timedelta(hours=1)}})
    assert stops(alone)[0].eta_p50 >= start + timedelta(hours=1) - timedelta(seconds=1)


# ---------------------------------------------------------------- after a failed stop
def _go(st, planner, depot, who):
    planner.run(depot.id, "dispatch")
    names = _names(st)
    o = next(o for o in st.list(Order) if names[o.customer_id] == who)
    van = next(r.vehicle_id for r in st.current_plan(depot.id).routes for s in r.stops if s.order_id == o.id)
    return o, van


def test_genuine_failure_reattempts_then_returns(world, monkeypatch):
    st, depot, planner, eng, m, svc = world
    o, van = _go(st, planner, depot, "Karthik S")
    monkeypatch.setattr(svc, "_evidence", lambda order, ev: (30.0, 240.0))       # at the door for 4 minutes
    before = st.get(Customer, o.customer_id).history.failed
    st.stop_event("stop.failed", StopEvent(order_id=o.id, vehicle_id=van, reason="customer not home"))
    out = run(svc.check_attempt(o.id, StopEvent(order_id=o.id, vehicle_id=van, reason="customer not home")))
    assert (out.result, out.genuine, out.reason) == ("reattempt", True, "not_home")
    assert st.get(Order, o.id).state == OrderState.reattempt and st.get(Order, o.id).verified_failures == 1
    assert st.get(Customer, o.customer_id).history.failed == before + 1
    assert _sent(st, "try again")
    for n in (2, 3):                                     # MAX_ATTEMPTS = 3 genuine failures -> return to origin
        planner.run(depot.id, f"reattempt {n}")
        st.stop_event("stop.failed", StopEvent(order_id=o.id, vehicle_id=van, reason="no one at home"))
        out = run(svc.check_attempt(o.id, StopEvent(order_id=o.id, vehicle_id=van, reason="no one at home")))
    assert out.result == "rto" and st.get(Order, o.id).state == OrderState.rto and _sent(st, "being returned")


def test_refused_cod_goes_straight_back(world, monkeypatch):
    st, depot, planner, eng, m, svc = world
    o, van = _go(st, planner, depot, "Anitha Joseph")
    monkeypatch.setattr(svc, "_evidence", lambda order, ev: (20.0, 120.0))
    st.stop_event("stop.failed", StopEvent(order_id=o.id, vehicle_id=van, reason="refused to pay"))
    out = run(svc.check_attempt(o.id, StopEvent(order_id=o.id, vehicle_id=van, reason="refused to pay")))
    assert out.result == "rto" and st.get(Customer, o.customer_id).history.refused == 2


def test_delivery_updates_customer_history(world):
    st, depot, planner, *_ = world
    o, van = _go(st, planner, depot, "Priya Venkat")
    st.stop_event("stop.delivered", StopEvent(order_id=o.id, vehicle_id=van))
    assert st.get(Customer, o.customer_id).history.delivered == 13


def test_api_fail_disputed_queue_and_resolve(world, monkeypatch):
    st, depot, planner, eng, *_ = world
    monkeypatch.setenv("DEMO_MODE", "0")
    with TestClient(create_app(st, planner=planner, settings=Settings(), engine=eng)) as c:
        c.post("/plan/run", params={"depot_id": depot.id})
        plan = c.get("/plan/current", params={"depot_id": depot.id}).json()
        (oa, va), (ob, vb) = [(s["order_id"], r["vehicle_id"]) for r in plan["routes"] for s in r["stops"]][:2]
        far = {"lat": 13.2, "lng": 80.3}                                # nowhere near any demo address
        r = c.post(f"/driver/stops/{oa}/fail", json={"vehicle_id": va, "reason": "not home", "location": far}).json()
        assert r["result"] == "disputed" and r["genuine"] is False and r["distance_m"] > 400
        assert c.get(f"/orders/{oa}").json()["state"] == "reattempt"
        assert "not verified" in c.get(f"/orders/{oa}").json()["notes"]
        here = c.get(f"/orders/{ob}").json()["location"]               # at the door but instantly: unclear
        c.post(f"/driver/stops/{ob}/arrive", json={"vehicle_id": vb, "location": here})
        r = c.post(f"/driver/stops/{ob}/fail", json={"vehicle_id": vb, "reason": "not home", "location": here}).json()
        assert r["result"] == "human"
        q = [i for i in c.get("/queue").json() if i["kind"] == "attempt"]
        assert [i["order"]["id"] for i in q] == [ob]
        r = c.post(f"/orders/{ob}/attempt/resolve", json={"genuine": True, "reason": "not_home"}).json()
        assert r["result"] == "reattempt" and not [i for i in c.get("/queue").json() if i["kind"] == "attempt"]
        risk = c.post("/prevention/run", params={"depot_id": depot.id}).json()
        assert risk["messaged"] >= 1 and len(risk["scored"]) == 10
        rep = c.get("/twin/prevention", params={"days": 5}).json()
        assert set(rep["policies"]) == {"none", "risk", "learned", "everyone"}


def test_chat_tool_checks_risky_deliveries(world):
    st, depot, planner, eng, m, svc = world
    ctx = ToolContext(st, planner, IncidentService(st, eng, planner, m), ReplyService(st, eng, planner, m),
                      prevention=svc)
    tools = {f.__name__: f for f in make_tools(ctx)}
    out = run(tools["check_risky_deliveries"]())
    assert out["status"] == "ok" and "5 look risky" in out["summary"] and "asked 5" in out["summary"]


# ---------------------------------------------------------------- the twin (P3 acceptance)
def test_twin_prevention_meets_the_p3_target():
    reports = [prevention_report(seed=s) for s in range(1, 6)]
    risk = [r["policies"]["risk"] for r in reports]
    everyone = [r["policies"]["everyone"] for r in reports]
    mean_cut = sum(x["fewer_failures"] for x in risk) / len(risk)
    assert mean_cut >= 0.25                                            # spec §13 P3: 25% fewer failed deliveries
    assert all(x["fewer_failures"] > 0.15 for x in risk)
    assert all(e["fewer_failures"] >= x["fewer_failures"] for e, x in zip(everyone, risk))   # upper bound
    assert all(x["message_rate"] < 0.6 for x in risk)                  # far fewer messages than everyone (100%)
    assert prevention_report(seed=3) == reports[2]                     # deterministic
