"""Customer messages: templates, simulated messenger, ETA updates after re-plans, reply handling.
Fake AI providers only; no keys or network."""

import asyncio
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from dispatch import demo
from dispatch.api import create_app
from dispatch.core.config import Settings
from dispatch.core.decisions import ChoiceAnswer, NoulAnswer, ScoreAnswer
from dispatch.core.events import MessageEvent
from dispatch.core.ids import new_id
from dispatch.core.models import (Customer, Depot, Incident, IncidentKind, Order, OrderState, Plan, Stop,
                                  VehicleRoute)
from dispatch.decide import DecisionEngine, ProviderUnavailable
from dispatch.incidents import IncidentService
from dispatch.messaging import TEMPLATES, SimMessenger, render
from dispatch.notify import AFFECTED_S, changes, reason_for
from dispatch.optimize import OrToolsSolver
from dispatch.planning import Planner
from dispatch.replies import ReplyService
from dispatch.routing import HaversineEngine
from dispatch.store import Store, StoreError
from tests.contracts.checks import check_messenger

T0 = datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc)
run = asyncio.run


class FakeLlm:
    """Answers any decision type: for each Choice question, the first of `choice` (a label or a tuple of
    labels) that the question offers, else its first option."""
    name, model, capabilities = "llm", "fake-llm", {"text"}

    def __init__(self, choice=None, fail=False, p=0.999):
        self.choice, self.fail, self.p = choice, fail, p
        self.meta = NS(model=self.model, method="fake", cost={})

    async def decide(self, state, questions):
        if self.fail:
            raise ProviderUnavailable("no key")
        out = {}
        for qid, q in questions.items():
            if q.type == "choice":
                prefs = self.choice if isinstance(self.choice, tuple) else (self.choice,)
                pick = next((c for c in prefs if c in q.criteria), next(iter(q.criteria)))
                out[qid] = ChoiceAnswer.from_probabilities({k: self.p if k == pick else (1 - self.p) / (len(q.criteria) - 1)
                                                            for k in q.criteria})
            elif q.type == "score":
                out[qid] = ScoreAnswer.from_probabilities([0, 0, 0.1, 0.8, 0.1][:len(q.levels)])
            else:
                out[qid] = NoulAnswer(p=self.p)
        return out


@pytest.fixture
def store(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'm.db'}")
    st.create_all()
    yield st
    st.engine.dispose()


@pytest.fixture
def day(store):
    d = demo.seed(store)
    planner = Planner(store, HaversineEngine(), OrToolsSolver(), time_limit_s=0.3)
    planner.run(d.id, "initial")
    return d, planner


def _services(store, planner, llm):
    eng = DecisionEngine(store, {"llm": llm}, Settings())
    m = SimMessenger(store)
    return IncidentService(store, eng, planner, m), ReplyService(store, eng, planner, m), m


def _sent(store):
    return [e.payload for e in store.events_since(0, types=["message.sent"])]


# ---------------------------------------------------------------- templates and messenger
def test_templates_stay_under_40_words_with_long_values():
    long = {"name": "Priyadharshini", "reason": "there's heavy traffic near Kathipara Junction", "eta": "11:45 AM",
            "eta_late": "12:15 PM", "order_id": "ord_x", "when": "Wednesday between 11:30 AM and 12:30 PM"}
    for t in TEMPLATES:
        assert len(render(t, long).split()) < 40
    with pytest.raises(KeyError):
        render("nope", long)


def test_sim_messenger_meets_contract_and_records_event(store):
    c = Customer(name="Arun Kumar", phone="1")
    store.put(c)
    check_messenger(SimMessenger(store), c)
    ev = store.events_since(0, types=["message.sent"])[-1]
    assert ev.payload["customer_id"] == c.id and ev.payload["text"].startswith("Hi Arun,")


# ---------------------------------------------------------------- who is affected, and why
def _plan(stops_by_van):
    return Plan(depot_id="dep_1", routes=[VehicleRoute(vehicle_id=v, stops=[
        Stop(order_id=o, seq=i, eta_p50=T0 + timedelta(seconds=t), eta_p90=T0 + timedelta(seconds=t),
             planned_arrival=T0 + timedelta(seconds=t)) for i, (o, t) in enumerate(stops)]) for v, stops in stops_by_van.items()])


def test_changes_threshold_and_van_moves():
    old = _plan({"V1": [("a", 600), ("b", 1200)], "V2": [("c", 900)]})
    new = _plan({"V1": [("a", 600 + AFFECTED_S - 1), ("b", 1200 + AFFECTED_S + 60)], "V2": [], "V3": [("c", 900)]})
    got = {c.order_id: c.van_changed for c in changes(old, new)}
    assert got == {"b": False, "c": True}
    assert changes(None, new) == []


def test_reasons_are_plain_and_placed():
    inc = Incident(kind=IncidentKind.traffic, description="CM convoy causing heavy traffic in Tambaram, terrible!!")
    assert reason_for(inc) == "there's heavy traffic near Tambaram"
    assert reason_for(Incident(kind=IncidentKind.breakdown, description="V2 puncture near Guindy")) == "one of our vans had a problem"
    assert reason_for(None) == "we've updated our delivery routes"


# ---------------------------------------------------------------- incidents notify affected customers
def test_incident_replan_messages_affected_customers(store, day):
    d, planner = day
    incidents, _, _ = _services(store, planner, FakeLlm("flood"))
    inc = Incident(kind=IncidentKind.other, description="Severe flooding on OMR near Sholinganallur")
    store.save_incident(inc)
    out = run(incidents.triage(inc.id))
    assert out.result == "active"
    sent = _sent(store)
    assert len(sent) == len(out.notified)
    if out.replanned[d.id] == "apply":
        for m in sent:
            assert "flooding near Sholinganallur" in m["text"] and "arrives around" in m["text"] and m["order_id"]


# ---------------------------------------------------------------- replies
def _reply(store, order, text):
    msg = MessageEvent(message_id=new_id("message"), customer_id=order.customer_id, order_id=order.id,
                       channel="whatsapp", text=text)
    store.record_message("message.received", msg)
    return msg


def _geocoded_order(store, d):
    return next(o for o in store.list(Order, depot_id=d.id) if o.state == OrderState.planned)


def test_button_reply_confirms_without_ai(store, day):
    d, planner = day
    _, replies, _ = _services(store, planner, FakeLlm(fail=True))          # AI unavailable: rules still work
    o = store.list(Order, depot_id=d.id)[0]
    out = run(replies.handle(_reply(store, o, "1")))
    assert out.intent == "confirm" and out.result == "confirmed"
    assert store.get_decision(out.decision_id).decider == "rules"


def test_cancel_reply_cancels_tells_customer_and_replans(store, day):
    d, planner = day
    _, replies, _ = _services(store, planner, FakeLlm(fail=True))
    o = _geocoded_order(store, d)
    before = store.current_plan(d.id).id
    out = run(replies.handle(_reply(store, o, "3")))
    assert out.result == "cancelled" and store.require(Order, o.id).state == OrderState.cancelled
    assert "cancelled as requested" in _sent(store)[-1]["text"]
    plan = store.current_plan(d.id)
    assert o.id not in {s.order_id for r in plan.routes for s in r.stops} or plan.id == before


def test_directions_are_noted_for_driver(store, day):
    d, planner = day
    _, replies, _ = _services(store, planner, FakeLlm("give_directions"))
    o = _geocoded_order(store, d)
    out = run(replies.handle(_reply(store, o, "Blue gate opposite the Murugan temple, 2nd floor")))
    assert out.result == "noted" and "Murugan temple" in store.require(Order, o.id).notes
    assert store.events_since(0, types=["order.noted"])[-1].entity == o.id
    assert _sent(store)[-1]["text"].startswith("Thanks")


def test_reschedule_without_a_time_waits_for_person_then_resolves(store, day):
    d, planner = day
    _, replies, _ = _services(store, planner, FakeLlm(("reschedule", "unclear")))   # no time in the text
    o = _geocoded_order(store, d)
    msg = _reply(store, o, "can you come some other time, I'm busy")
    assert run(replies.handle(msg)).result == "needs_person"
    assert [m.message_id for m, _ in replies.pending()] == [msg.message_id]
    with pytest.raises(StoreError, match="couldn't read a time"):
        replies.resolve(msg.message_id, "reschedule", "ops", when="whenever")
    out = replies.resolve(msg.message_id, "reschedule", "ops", when="tomorrow 10am")
    assert out.result == "rescheduled" and out.when.startswith("tomorrow between 10 AM") and replies.pending() == []
    assert store.get(Order, o.id).window_start.astimezone(ZoneInfo("Asia/Kolkata")).hour == 10


def test_no_provider_available_still_queues_for_person(store, day):
    d, planner = day
    _, replies, _ = _services(store, planner, FakeLlm(fail=True))          # quota/overload: rules pass, AI fails
    msg = _reply(store, _geocoded_order(store, d), "can you come after 6 pm instead")
    assert run(replies.handle(msg)).result == "needs_person"
    assert [m.message_id for m, _ in replies.pending()] == [msg.message_id]  # regression: was silently dropped
    replies.resolve(msg.message_id, "reschedule", "ops")
    assert replies.pending() == []


def test_unsure_ai_goes_to_person(store, day):
    d, planner = day
    _, replies, _ = _services(store, planner, FakeLlm("confirm", p=0.4))
    msg = _reply(store, _geocoded_order(store, d), "hmm maybe")
    assert run(replies.handle(msg)).result == "needs_person"
    assert len(replies.pending()) == 1


# ---------------------------------------------------------------- API
def test_api_reply_flow_messages_and_queue(store, day, monkeypatch):
    d, planner = day
    eng = DecisionEngine(store, {"llm": FakeLlm(("reschedule", "unclear"))}, Settings())
    with TestClient(create_app(store, planner=planner, engine=eng, settings=Settings())) as c:
        o = _geocoded_order(store, d)
        assert c.post("/webhooks/messages", json={"customer_id": o.customer_id, "order_id": o.id, "text": "3"}).status_code == 200
        assert c.get(f"/orders/{o.id}").json()["state"] == "cancelled"            # handled in the background
        o3 = _geocoded_order(store, d)
        c.post("/webhooks/messages", json={"customer_id": o3.customer_id, "order_id": o3.id, "text": "come at 7pm"})
        assert c.get(f"/orders/{o3.id}").json()["window_start"] is not None         # the time was read: rescheduled
        assert "we'll deliver" in c.get("/messages").json()[0]["text"]
        o2 = _geocoded_order(store, d)
        c.post("/webhooks/messages", json={"customer_id": o2.customer_id, "order_id": o2.id, "text": "come later"})
        q = [x for x in c.get("/queue").json() if x["kind"] == "message"]
        assert len(q) == 1 and q[0]["message"]["text"] == "come later" and q[0]["customer"]
        r = c.post(f"/messages/{q[0]['message']['message_id']}/resolve", json={"by": "ops", "intent": "give_directions"}).json()
        assert r["result"] == "noted" and [x for x in c.get("/queue").json() if x["kind"] == "message"] == []
        msgs = c.get("/messages").json()
        assert {m["direction"] for m in msgs} == {"in", "out"} and msgs[0]["customer"]
