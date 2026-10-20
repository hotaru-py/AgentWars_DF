"""Reschedule times from customer text, and the trained `ml` provider (spec §7.2, §10).
Offline: straight-line routing, fake LLM, no keys."""

import asyncio
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dispatch import demo
from dispatch.core.config import Settings
from dispatch.core.events import MessageEvent, StopEvent
from dispatch.core.ids import new_id
from dispatch.core.models import Customer, Order
from dispatch.decide import DELIVERY_RISK, DecisionEngine
from dispatch.decide.ml import TASKS, LogisticModel, MlDecider, MlRegistry, MlTask, Sample, auc
from dispatch.messaging import SimMessenger
from dispatch.optimize import OrToolsSolver
from dispatch.planning import Planner
from dispatch.prevention import PreventionService
from dispatch.replies import ReplyService
from dispatch.routing import HaversineEngine
from dispatch.store import Store
from dispatch.twin.prevention import prevention_report
from dispatch.when import parse_when, slot_window
from tests.test_messages import FakeLlm

run = asyncio.run
IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 20, 11, 15, tzinfo=IST)          # a Tuesday, late morning


@pytest.fixture
def world(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'l.db'}")
    st.create_all()
    depot = demo.seed(st)
    planner = Planner(st, HaversineEngine(), OrToolsSolver(), time_limit_s=0.3)
    yield st, depot, planner
    st.engine.dispose()


# ---------------------------------------------------------------- times from text
@pytest.mark.parametrize("text, day, start, end", [
    ("after 6 pm", 0, "18:00", "21:00"),
    ("come after 6", 0, "18:00", "21:00"),                 # no am/pm: 6 means evening
    ("tomorrow morning", 1, "09:00", "12:00"),
    ("between 4 and 6", 0, "16:00", "18:00"),
    ("4-6pm", 0, "16:00", "18:00"),
    ("at 5:30pm", 0, "17:30", "18:30"),
    ("5.30 pm please", 0, "17:30", "18:30"),
    ("come at 10", 1, "10:00", "11:00"),                   # 10 AM has passed: tomorrow
    ("tonight", 0, "18:00", "21:00"),
    ("naalai", 1, "09:00", "21:00"),                       # Tamil for tomorrow: the whole day
    ("friday evening", 3, "16:00", "20:00"),
    ("evening around 7", 0, "19:00", "20:00"),
])
def test_parse_when(text, day, start, end):
    w = parse_when(text, NOW)
    assert w is not None and (w.start.date() - NOW.date()).days == day
    assert (w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) == (start, end)


@pytest.mark.parametrize("text", ["when I'm back", "call me at 98400 12345", "ok", "who is this?"])
def test_parse_when_gives_up(text):
    assert parse_when(text, NOW) is None


def test_slots_and_words():
    assert slot_window("tomorrow_evening", NOW).start.hour == 16 and slot_window("unclear", NOW) is None
    assert parse_when("tomorrow morning", NOW).slot == "tomorrow_morning"
    assert parse_when("after 6 pm", NOW).describe() == "today between 6 PM and 9 PM"


# ---------------------------------------------------------------- reschedule replies
def _reply(st, o, text):
    msg = MessageEvent(message_id=new_id("message"), customer_id=o.customer_id, order_id=o.id, channel="sim", text=text)
    st.record_message("message.received", msg)
    return msg


def _order(st, name):
    names = {c.id: c.name for c in st.list(Customer)}
    return next(o for o in st.list(Order) if names[o.customer_id] == name)


def test_reschedule_reply_with_a_time_needs_no_ai_for_the_time(world):
    st, depot, planner = world
    planner.run(depot.id, "start")
    eng = DecisionEngine(st, {"llm": FakeLlm(("reschedule", "unclear"))}, Settings())   # LLM: intent only
    replies = ReplyService(st, eng, planner, SimMessenger(st))
    o = _order(st, "Karthik S")
    out = run(replies.handle(_reply(st, o, "Please come tomorrow morning instead")))
    assert out.result == "rescheduled" and out.when.startswith("tomorrow between 9 AM")
    slot = st.decisions(type_="reschedule_slot", limit=1)[0]
    assert slot.decider == "rules" and slot.answers["slot"].choice == "tomorrow_morning"
    after = st.get(Order, o.id)
    assert after.window_start.astimezone(IST).hour == 9 and after.customer_reply == "reschedule"
    sent = st.events_since(0, types=["message.sent"], limit=100)[-1].payload["text"]
    assert "we'll deliver tomorrow between 9 AM and 12 PM" in sent
    stops = {s.order_id for r in st.current_plan(depot.id).routes for s in r.stops}
    tomorrow = after.window_start >= depot.closes_at
    assert (o.id not in stops) == tomorrow                # a later day leaves today's plan
    assert replies.pending() == []


def test_vague_reschedule_uses_the_llm_slot(world):
    st, depot, planner = world
    eng = DecisionEngine(st, {"llm": FakeLlm(("reschedule", "tomorrow_evening"))}, Settings())
    replies = ReplyService(st, eng, planner, SimMessenger(st))
    out = run(replies.handle(_reply(st, _order(st, "Arun Kumar"), "once my office is over")))
    assert out.result == "rescheduled" and out.when == "tomorrow between 4 PM and 8 PM"
    assert st.decisions(type_="reschedule_slot", limit=1)[0].decider == "llm"


def test_planner_leaves_out_orders_for_a_later_day(world):
    st, depot, planner = world
    o = _order(st, "Priya Venkat")
    st.reschedule_order(o.id, depot.closes_at + timedelta(hours=14), depot.closes_at + timedelta(hours=17), "tomorrow")
    problem, _ = planner.problem(depot.id)
    assert o.id not in {x.id for x in problem.orders} and len(problem.orders) == 9


# ---------------------------------------------------------------- the model
def _synthetic(n, seed=0, signal=2.0):
    rng = random.Random(seed)
    rows, ys = [], []
    for _ in range(n):
        x = {"a": rng.gauss(0, 1), "b": rng.gauss(0, 1), "noise": rng.gauss(0, 1)}
        p = 1 / (1 + pow(2.718281828, -(signal * x["a"] - signal * x["b"] - 1)))
        rows.append(x)
        ys.append(int(rng.random() < p))
    return rows, ys


def test_logistic_model_learns_the_signal():
    rows, ys = _synthetic(2000)
    m = LogisticModel.fit(rows, ys)
    w = m.weights()
    assert w["a"] > 1 and w["b"] < -1 and abs(w["noise"]) < 0.2
    test_rows, test_ys = _synthetic(1000, seed=1)
    assert auc([m.predict(r) for r in test_rows], test_ys) > 0.85
    assert auc([1, 2, 3, 4], [0, 0, 1, 1]) == 1.0 and auc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5 and auc([1], [1]) is None


def _task(samples, min_samples=100):
    return MlTask("delivery_risk", min_samples, lambda s: s, lambda store: samples, TASKS["delivery_risk"].answers)


def test_ml_waits_for_enough_labels_then_must_beat_the_rules():
    rows, ys = _synthetic(600)
    weak = [Sample(x, y, baseline=random.Random(i).random()) for i, (x, y) in enumerate(zip(rows, ys))]
    p, info = MlRegistry.train(_task(weak[:50]), weak[:50])
    assert not isinstance(p, MlDecider) and "needs 100" in p.reason
    p, info = MlRegistry.train(_task(weak), weak)                       # rules = noise: the model wins
    assert isinstance(p, MlDecider) and info["auc"] > info["baseline_auc"] + 0.01
    oracle = [Sample(x, y, baseline=2 * x["a"] - 2 * x["b"]) for x, y in zip(rows, ys)]
    p, info = MlRegistry.train(_task(oracle), oracle)                   # rules already perfect: keep them
    assert not isinstance(p, MlDecider) and "doesn't beat the rules" in p.reason


def test_engine_uses_a_trained_risk_model(world):
    st, depot, planner = world
    rows, ys = _synthetic(600)

    class Registry(MlRegistry):
        def provider(self, spec):
            if spec.type != "delivery_risk":
                return None
            keys = ["fail_rate", "refuse_rate", "log_past_orders", "first_time", "cod", "high_value_cod",
                    "address_doubt", "reattempt"]
            data = [Sample({k: x["a"] if k == "fail_rate" else 0.0 for k in keys}, y, baseline=0.5) for x, y in zip(rows, ys)]
            return MlRegistry.train(TASKS["delivery_risk"], data)[0]

    eng = DecisionEngine(st, {}, Settings(), ml=Registry(st))
    svc = PreventionService(st, eng, planner, SimMessenger(st), Settings())
    run(svc.run(depot.id))
    ledger = st.decisions(type_="delivery_risk", limit=20)
    assert {d.decider for d in ledger} == {"ml"} and {d.method for d in ledger} == {"classifier"}
    assert all("features" in d.inputs for d in ledger)                  # inputs are kept for training


def test_risk_labels_skip_messaged_orders_and_weight_holdouts(world):
    st, depot, planner = world
    eng = DecisionEngine(st, {}, Settings())
    settings = Settings(risk_holdout=0.5)
    svc = PreventionService(st, eng, planner, SimMessenger(st), settings)
    svc._rng = random.Random(3)
    r = run(svc.run(depot.id))
    held = [x.order_id for x in r.scored if x.risky and not x.message_sent]
    asked = [x.order_id for x in r.scored if x.message_sent]
    assert held and asked                                               # some of each with this seed
    plan = st.current_plan(depot.id)
    van = {s.order_id: rt.vehicle_id for rt in plan.routes for s in rt.stops}
    for oid in held + asked + [x.order_id for x in r.scored if not x.risky][:2]:
        st.stop_event("stop.delivered", StopEvent(order_id=oid, vehicle_id=van[oid]))
    samples = TASKS["delivery_risk"].labels(st)
    assert len(samples) == len(held) + 2                                # messaged orders left out
    assert sorted(s.weight for s in samples) == [1.0, 1.0] + [2.0] * len(held)
    assert all(s.y == 0 and s.baseline is not None for s in samples)


def test_attempt_labels_come_from_people(world, monkeypatch):
    st, depot, planner = world
    eng = DecisionEngine(st, {}, Settings())
    svc = PreventionService(st, eng, planner, SimMessenger(st), Settings())
    planner.run(depot.id, "start")
    plan = st.current_plan(depot.id)
    oid, van = plan.routes[0].stops[0].order_id, plan.routes[0].vehicle_id
    monkeypatch.setattr(svc, "_evidence", lambda order, ev: (200.0, 30.0))      # in between: a person decides
    ev = StopEvent(order_id=oid, vehicle_id=van, reason="nobody home")
    st.stop_event("stop.failed", ev)
    assert run(svc.check_attempt(oid, ev)).result == "human"
    svc.resolve_attempt(oid, True, "not_home", "ops")
    (s,) = TASKS["failed_attempt"].labels(st)
    assert s.y == 1 and s.x["log_distance"] > 5 and s.x["said_not_home"] == 1.0
    status = MlRegistry(st).status()
    assert status["failed_attempt"]["labelled"] == 1 and not status["failed_attempt"]["trained"]


def test_twin_learned_policy_goes_through_the_same_check():
    rep = prevention_report(seed=2)
    m = rep["model"]
    assert m["took_over"] is False and "needs 500" in m["why_not"]      # 10 days isn't enough data
    assert set(rep["ranking_auc"]) == {"rules", "learned"}
    risk, learned = rep["policies"]["risk"], rep["policies"]["learned"]
    # Rules stayed in charge; the only difference is exploration (10% of risky orders not messaged),
    # which is what unbiased training labels cost: fewer messages, a few more failures.
    assert learned["messages"] < risk["messages"] and learned["failed"] >= risk["failed"]
