"""v2 core contracts (dispatch/core): models, decisions, gates, events, ids, config."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from dispatch.core import config, ids
from dispatch.core.decisions import (Choice, ChoiceAnswer, DecisionRecord, Noul, NoulAnswer, Route, Score,
                                     ScoreAnswer, ShadowResult, choice_confidence, gate_route, score_confidence)
from dispatch.core.events import OrderCreated, PlanCreated, make_event, parse_payload
from dispatch.core.models import (Circle, Driver, GateThresholds, Incident, IncidentKind, LatLng, Order,
                                  OrderState, Plan, Policy, Vehicle, can_transition)

T0 = datetime(2026, 10, 17, 3, 30, tzinfo=timezone.utc)   # 9:00 AM IST


def _order(**kw):
    return Order(depot_id="dep_1", customer_id="cus_1", raw_address="12, 3rd St, opp. temple, Adyar", **kw)


# ---------------------------------------------------------------- ids
def test_ids_are_prefixed_unique_and_typed():
    a, b = ids.new_id("order"), ids.new_id("order")
    assert a.startswith("ord_") and len(a) == 30 and a != b
    assert ids.kind_of(a) == "order" and ids.kind_of("zzz_1") is None


# ---------------------------------------------------------------- models
def test_order_defaults_and_validation():
    o = _order()
    assert o.state == OrderState.new and o.id.startswith("ord_") and o.location is None
    with pytest.raises(ValidationError):
        _order(digipin="4P3-JK8-52C")                       # separators not allowed
    with pytest.raises(ValidationError):
        _order(window_start=T0, window_end=T0)               # empty window
    with pytest.raises(ValidationError):
        _order(surprise=1)                                   # extra fields forbidden
    assert _order(digipin="4P3JK852C9").digipin == "4P3JK852C9"


def test_latlng_bounds_and_circle():
    with pytest.raises(ValidationError):
        LatLng(lat=91, lng=0)
    c = Circle(center=LatLng(lat=13.0, lng=80.2), radius_m=2500)
    assert c.kind == "circle"


def test_order_state_machine():
    assert can_transition(OrderState.new, OrderState.geocoded)
    assert can_transition(OrderState.out_for_delivery, OrderState.failed)
    assert can_transition(OrderState.failed, OrderState.rto)
    assert not can_transition(OrderState.delivered, OrderState.failed)
    assert not can_transition(OrderState.new, OrderState.delivered)


def test_driver_shift_must_be_positive():
    with pytest.raises(ValidationError):
        Driver(name="Ganesh", phone="+91", shift_start=T0, shift_end=T0)


def test_customers_do_not_share_history():
    from dispatch.core.models import Customer
    a, b = Customer(name="A", phone="1"), Customer(name="B", phone="2")
    a.history.failed += 1
    assert b.history.failed == 0


def test_policy_gate_defaults_and_ordering():
    p = Policy(version=1)
    assert p.gate("incident_triage") == GateThresholds(act=0.9, review=0.5)
    with pytest.raises(ValidationError):
        GateThresholds(act=0.4, review=0.6)


def test_incident_slowdown_floor():
    with pytest.raises(ValidationError):
        Incident(kind=IncidentKind.flood, slowdown=0.5)


# ---------------------------------------------------------------- decisions
def test_choice_confidence_matches_jev_definition():
    assert choice_confidence({"a": 1.0, "b": 0.0}) == 1.0
    assert choice_confidence({"a": 0.5, "b": 0.5}) == 0.0
    assert abs(choice_confidence({"a": 0.6, "b": 0.2, "c": 0.2}) - 0.4) < 1e-9   # (0.6-1/3)/(2/3)


def test_choice_answer_normalizes():
    a = ChoiceAnswer.from_probabilities({"flood": 3, "traffic": 1})
    assert a.choice == "flood" and abs(a.probabilities["flood"] - 0.75) < 1e-9 and 0 < a.confidence < 1


def test_score_confidence_penalizes_far_mass_more():
    near = score_confidence([0.0, 0.5, 0.5, 0.0, 0.0])
    far = score_confidence([0.0, 0.5, 0.0, 0.0, 0.5])
    assert score_confidence([0, 0, 1, 0, 0]) == 1.0 and near > far >= 0
    s = ScoreAnswer.from_probabilities([0, 0, 0, 1])
    assert s.score == 1.0 and s.level == 3


def test_noul_confidence():
    assert NoulAnswer(p=0.95).confidence == pytest.approx(0.9)
    assert NoulAnswer(p=0.5).confidence == 0.0 and NoulAnswer(p=0.2).value is False


@pytest.mark.parametrize("conf,reversible,route", [
    (0.95, False, Route.act), (0.7, True, Route.review), (0.7, False, Route.escalated), (0.3, True, Route.human),
])
def test_gate_routes(conf, reversible, route):
    assert gate_route(conf, GateThresholds(), reversible) == route


def test_decision_record_requires_matching_answers():
    q = {"kind": Choice(instructions="What kind of incident", criteria={"flood": "water", "traffic": "jam"}),
         "credible": Noul(instructions="The report describes a real event")}
    a = {"kind": ChoiceAnswer.from_probabilities({"flood": 0.9, "traffic": 0.1}), "credible": NoulAnswer(p=0.97)}
    rec = DecisionRecord(type="incident_triage", subject="inc_1", decider="llm", method="logprobs",
                         questions=q, answers=a, raw_confidence=0.95, confidence=0.8, gate=GateThresholds(),
                         route=Route.review,
                         shadow=[ShadowResult(decider="jev", answers=a, raw_confidence=0.97, latency_ms=150)])
    assert DecisionRecord.model_validate_json(rec.model_dump_json()) == rec          # JSON round-trip
    with pytest.raises(ValidationError):
        DecisionRecord(type="t", subject="s", decider="jev", questions=q, answers={"kind": a["kind"]},
                       raw_confidence=0.9, confidence=0.8, gate=GateThresholds(), route=Route.act)  # missing answer
    with pytest.raises(ValidationError):
        DecisionRecord(type="t", subject="s", decider="jev", questions=q,
                       answers={"kind": NoulAnswer(p=0.9), "credible": a["credible"]},
                       raw_confidence=0.9, confidence=0.8, gate=GateThresholds(), route=Route.act)  # wrong type
    with pytest.raises(ValidationError):
        DecisionRecord(type="t", subject="s", decider="Bad Name", raw_confidence=0.9, confidence=0.8,
                       gate=GateThresholds(), route=Route.human)                       # provider names are slugs
    assert Score(instructions="risk", levels=["low", "high"]).type == "score"


# ---------------------------------------------------------------- events
def test_events_round_trip_and_reject_wrong_payloads():
    o = _order()
    ev = make_event("order.created", o.id, OrderCreated(order=o))
    back = parse_payload(ev)
    assert back.order == o
    with pytest.raises(TypeError):
        make_event("order.created", o.id, PlanCreated(plan=Plan(depot_id="dep_1")))
    with pytest.raises(ValueError):
        make_event("order.teleported", o.id, OrderCreated(order=o))


# ---------------------------------------------------------------- config
def test_settings_defaults_and_timezone(monkeypatch):
    for k in ("TYPESAFE_API_KEY", "DEPOT_TZ"):
        monkeypatch.delenv(k, raising=False)
    s = config.load(env_file=None)
    assert not s.jev_enabled and s.depot_tz == "Asia/Kolkata"
    assert T0.astimezone(s.tz).hour == 9
    monkeypatch.setenv("TYPESAFE_API_KEY", "x" * 30)
    assert config.load(env_file=None).jev_enabled


def test_decider_chain_config(monkeypatch):
    monkeypatch.setenv("DECIDERS", "incident_triage=jev, llm, human; reply_intent=rules,llm,human")
    monkeypatch.setenv("DECIDERS_SHADOW", "jev")
    s = config.load(env_file=None)
    assert s.chain_overrides() == {"incident_triage": ["jev", "llm", "human"],
                                   "reply_intent": ["rules", "llm", "human"]}
    assert s.shadow_providers() == ["jev"] and s.llm_prob_method == "auto"
    monkeypatch.setenv("DECIDERS", "incident_triage")
    with pytest.raises(ValueError):
        config.load(env_file=None).chain_overrides()
    monkeypatch.delenv("DECIDERS")
    assert config.load(env_file=None).chain_overrides() == {}


def test_vehicle_needs_capacity():
    with pytest.raises(ValidationError):
        Vehicle(depot_id="dep_1", capacity_kg=0, capacity_l=100)
    assert Vehicle(depot_id="dep_1", capacity_kg=50, capacity_l=200).range_km is None
    _ = timedelta  # keep import for future window tests
