"""Decision layer: providers (fake clients, no keys/network), calibration, engine routes, incident triage."""

import asyncio
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from dispatch.api import create_app
from dispatch.core.config import Settings
from dispatch.core.decisions import ChoiceAnswer, NoulAnswer, Route, ScoreAnswer
from dispatch.core.models import (Customer, Depot, GateThresholds, Incident, IncidentKind, IncidentState, LatLng,
                                  Order, Policy, Vehicle, VehicleState)
from dispatch.decide import (INCIDENT_TRIAGE, Calibrator, DecisionEngine, LlmDecider, NoAnswer, ProviderUnavailable,
                             RulesDecider, certain, effective_gate)
from dispatch.decide.calibrate import MIN_OUTCOMES
from dispatch.decide.deciders.optional.jev import JevDecider
from dispatch.incidents import IncidentService
from dispatch.optimize import OrToolsSolver
from dispatch.planning import Planner
from dispatch.routing import HaversineEngine
from dispatch.store import Store
from tests.contracts.checks import check_decider

T0 = datetime(2026, 10, 19, 3, 30, tzinfo=timezone.utc)
Q = INCIDENT_TRIAGE.questions
run = asyncio.run


# ---------------------------------------------------------------- fakes
class FakeProvider:
    """Answers with fixed probabilities; counts calls."""
    capabilities = {"text"}

    def __init__(self, name="llm", kind=None, credible=0.98, severity=None, fail=None):
        self.name, self.model, self.calls, self.fail = name, f"fake-{name}", 0, fail
        self.kind = kind or {"flood": 0.95, "traffic": 0.05}
        self.credible, self.severity = credible, severity or [0, 0, 0.2, 0.7, 0.1]
        self.meta = NS(model=self.model, method="fake", cost={})

    async def decide(self, state, questions):
        self.calls += 1
        if self.fail:
            raise self.fail
        k = {c: self.kind.get(c, 0.0) for c in questions["kind"].criteria}
        return {"kind": ChoiceAnswer.from_probabilities(k), "severity": ScoreAnswer.from_probabilities(self.severity),
                "credible": NoulAnswer(p=self.credible)}


def _gemini_logprobs(top: dict[str, float]):
    cands = [NS(token=t, log_probability=math.log(p)) for t, p in top.items()]
    return NS(candidates=[NS(logprobs_result=NS(top_candidates=[NS(candidates=cands)]))], text=max(top, key=top.get))


class FakeGemini:
    """Mimics google.genai Client.models.generate_content for both probability methods."""

    def __init__(self, logprobs=True, json_answers=None):
        self.logprobs, self.json_answers, self.calls = logprobs, json_answers or [], 0
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls += 1
        if config.response_mime_type == "text/x.enum":
            if not self.logprobs:
                raise RuntimeError("400 INVALID_ARGUMENT: Logprobs is not enabled for this model")
            opts = config.response_schema["enum"]
            return _gemini_logprobs({opts[0]: 0.8, opts[1]: 0.15} if len(opts) > 1 else {opts[0]: 1.0})
        import json
        return NS(text=json.dumps(self.json_answers[(self.calls - 1) % len(self.json_answers)]))


# ---------------------------------------------------------------- calibration and gates
def test_calibrator_identity_until_enough_outcomes_then_monotone():
    few = Calibrator.fit([(0.9, True)] * 10)
    assert not few.fitted and few(0.73) == 0.73
    strict = effective_gate(GateThresholds(), few)
    assert strict.act == 0.95 and strict.review == 0.5
    pairs = [(0.95, i % 10 != 0) for i in range(150)] + [(0.6, i % 2 == 0) for i in range(100)]
    cal = Calibrator.fit(pairs)
    assert cal.fitted and cal.n == 250
    assert cal(0.6) == pytest.approx(0.5) and cal(0.95) == pytest.approx(0.9) and cal(0.6) <= cal(0.8) <= cal(0.95)
    assert effective_gate(GateThresholds(act=0.85), cal).act == 0.85
    assert len(pairs) >= MIN_OUTCOMES


# ---------------------------------------------------------------- providers
def test_rules_decider_fires_or_passes():
    def flood_rule(state, qs):
        if "flood" in state["report"]["text"]:
            return {"kind": certain(qs["kind"], "flood"), "severity": certain(qs["severity"], 3),
                    "credible": certain(qs["credible"], True)}
    r = RulesDecider([flood_rule])
    ans = run(r.decide({"report": {"text": "flood on OMR"}}, Q))
    assert ans["kind"].choice == "flood" and ans["kind"].confidence == 1.0 and ans["credible"].p == 1.0
    with pytest.raises(NoAnswer):
        run(r.decide({"report": {"text": "jam"}}, Q))


def test_llm_logprobs_path_and_contract():
    g = FakeGemini(logprobs=True)
    d = LlmDecider(client=g, method="auto")
    check_decider(d)
    assert d.meta.method == "logprobs" and g.calls == 3              # one enum call per question


def test_llm_auto_falls_back_to_sampling_and_remembers():
    answers = [{"kind": "flood", "severity": "3", "credible": "yes"}] * 4 + [{"kind": "traffic", "severity": "2", "credible": "yes"}]
    g = FakeGemini(logprobs=False, json_answers=answers)
    d = LlmDecider(client=g, method="auto", samples=5)
    ans = run(d.decide({"report": {"text": "water"}}, Q))
    assert d.meta.method == "self_consistency" and d._logprobs_ok is False
    assert ans["kind"].choice == "flood" and 0.5 < ans["kind"].probabilities["flood"] < 1     # 4/5 smoothed
    calls = g.calls
    run(d.decide({"report": {"text": "water"}}, Q))
    assert g.calls - calls == 5                                        # no logprobs retry the second time


def test_llm_quota_error_does_not_switch_methods():
    """A 429 on the first logprobs call is not 'logprobs unsupported': stay on logprobs (3 calls per
    report), don't fall back to sampling (5+ calls) for the rest of the run."""
    g = FakeGemini(logprobs=True)
    real = g.generate_content

    def quota_once(model, contents, config):
        if g.calls == 0:
            g.calls += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED. You exceeded your current quota")
        return real(model, contents, config)
    g.generate_content = quota_once
    d = LlmDecider(client=g, method="auto")
    with pytest.raises(ProviderUnavailable, match="429"):
        run(d.decide({"report": {"text": "water"}}, Q))
    assert d._logprobs_ok is None                                      # undecided, not "unsupported"
    run(d.decide({"report": {"text": "water"}}, Q))
    assert d.meta.method == "logprobs" and d._logprobs_ok is True


def test_llm_unavailable_without_key_or_on_errors():
    with pytest.raises(ProviderUnavailable):
        run(LlmDecider(api_key="").decide("x", Q))

    class Boom:
        models = property(lambda self: self)

        def generate_content(self, **kw):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
    with pytest.raises(ProviderUnavailable, match="429"):
        run(LlmDecider(client=Boom(), method="self_consistency").decide("x", Q))


def test_jev_maps_sdk_answers_and_meets_contract():
    class Sdk:                                   # shapes as in typesafe-sdk 0.7.5
        Choice = Score = Noul = staticmethod(lambda **kw: kw)

    class Client:
        async def system_one(self, state, questions, model=None):
            self.seen = (state, questions, model)
            return NS(model="jev-1.14.0", usage=NS(input_tokens=400, output_tokens=60), request_id="req_1",
                      answers={"kind": NS(probabilities={"flood": 0.9, "traffic": 0.1}),
                               "severity": NS(probabilities={0: 0.1, 1: 0.2, 2: 0.7}),
                               "credible": NS(noul=0.97)})
    c = Client()
    j = JevDecider(api_key="k", client=c, sdk=Sdk())
    check_decider(j)
    assert c.seen[1]["severity"]["criteria"] == ["low", "medium", "high"] and j.meta.model == "jev-1.14.0"
    assert j.meta.cost["input_tokens"] == 400
    with pytest.raises(ProviderUnavailable):
        run(JevDecider(api_key="").decide("x", Q))


# ---------------------------------------------------------------- engine
@pytest.fixture
def store(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'd.db'}")
    st.create_all()
    yield st
    st.engine.dispose()


def _engine(store, providers, **env):
    return DecisionEngine(store, providers, Settings(**env))


def test_engine_acts_when_confident_and_logs(store):
    p = FakeProvider(credible=0.995, kind={"flood": 0.999})
    res = run(_engine(store, {"llm": p}).decide(INCIDENT_TRIAGE, "inc_1", {"report": {"text": "flood"}}))
    assert res.route == Route.act and res.record.decider == "llm" and res.record.gate.act == 0.95   # strict: uncalibrated
    assert store.get_decision(res.record.id).answers["kind"].choice == "flood"


def test_engine_review_for_reversible_medium_confidence(store):
    res = run(_engine(store, {"llm": FakeProvider(kind={"flood": 0.85, "traffic": 0.15})}).decide(
        INCIDENT_TRIAGE, "inc_1", {"report": {"text": "x"}}))
    assert res.route == Route.review and res.decided


def test_engine_escalates_irreversible_then_tries_next(store):
    spec = INCIDENT_TRIAGE.__class__(**{**INCIDENT_TRIAGE.__dict__, "reversible": False, "chain": ["jev", "llm", "human"]})
    jev, llm = FakeProvider("jev", kind={"flood": 0.85, "traffic": 0.15}), FakeProvider("llm", kind={"flood": 0.999}, credible=0.999)
    res = run(_engine(store, {"jev": jev, "llm": llm}).decide(spec, "inc_1", {"report": {"text": "x"}}))
    assert res.route == Route.act and res.record.decider == "llm" and res.record.escalated_from
    assert store.get_decision(res.record.escalated_from).route == Route.escalated


def test_engine_low_confidence_goes_to_human_with_suggestion(store):
    res = run(_engine(store, {"llm": FakeProvider(kind={"flood": 0.5, "traffic": 0.5})}).decide(
        INCIDENT_TRIAGE, "inc_1", {"report": {"text": "x"}}))
    assert res.route == Route.human and res.record.decider == "llm" and not res.decided


def test_engine_all_unavailable_goes_to_human_with_trail(store):
    res = run(_engine(store, {"llm": FakeProvider(fail=ProviderUnavailable("no key"))}).decide(
        INCIDENT_TRIAGE, "inc_1", {"report": {"text": "x"}}))
    assert res.route == Route.human and res.record.decider == "human" and res.answers == {}
    assert res.trail[0] == {"provider": "llm", "result": "ProviderUnavailable", "detail": "no key"}


def test_engine_shadow_and_chain_override(store):
    jev = FakeProvider("jev", kind={"flood": 0.97, "traffic": 0.03})
    llm = FakeProvider("llm", kind={"flood": 0.999}, credible=0.999)
    eng = _engine(store, {"jev": jev, "llm": llm}, deciders_shadow="jev,ghost")
    res = run(eng.decide(INCIDENT_TRIAGE, "inc_1", {"report": {"text": "x"}}))
    assert res.record.decider == "llm" and jev.calls == 1
    shadows = {s.decider: s for s in store.get_decision(res.record.id).shadow}
    assert shadows["jev"].answers["kind"].choice == "flood" and shadows["ghost"].error == "not configured"
    eng2 = _engine(store, {"jev": jev, "llm": llm}, deciders="incident_triage=jev,human")
    assert run(eng2.decide(INCIDENT_TRIAGE, "inc_2", "x")).record.decider == "jev"


# ---------------------------------------------------------------- incident triage service
@pytest.fixture
def ops(store):
    depot = Depot(name="Guindy", location=LatLng(lat=13.0067, lng=80.2206), opens_at=T0, closes_at=T0 + timedelta(hours=10))
    store.put(depot)
    vans = [Vehicle(depot_id=depot.id, name=f"V{i}", capacity_kg=50, capacity_l=500) for i in range(2)]
    for v in vans:
        store.put(v)
    cust = Customer(name="A", phone="1")
    store.put(cust)
    for lat, lng in [(13.04, 80.23), (12.93, 80.24), (12.98, 80.26), (12.9249, 80.1)]:
        o = Order(depot_id=depot.id, customer_id=cust.id, raw_address="x")
        store.create_order(o)
        store.set_geocode(o.id, LatLng(lat=lat, lng=lng), None, 0.9, "test")
    planner = Planner(store, HaversineEngine(), OrToolsSolver(), time_limit_s=0.3)
    planner.run(depot.id, "initial")
    return depot, vans, planner


def _report(store, text, **kw):
    inc = Incident(kind=IncidentKind.other, description=text, **kw)
    store.save_incident(inc)
    return inc


def test_triage_activates_places_and_replans(store, ops):
    depot, _, planner = ops
    store.save_policy(Policy(version=1))
    svc = IncidentService(store, _engine(store, {"llm": FakeProvider(kind={"flood": 0.999}, credible=0.999)}), planner)
    before = store.current_plan(depot.id).id
    out = run(svc.triage(_report(store, "Knee-deep water on Old Mahabalipuram Road near Thoraipakkam").id))
    inc = out.incident
    assert out.result == "active" and inc.kind == IncidentKind.flood and inc.state == IncidentState.active
    assert inc.area.center == LatLng(lat=12.9416, lng=80.2362) and 1 < inc.slowdown <= 4.0 * 1.25   # Thoraipakkam: most specific
    assert out.replanned[depot.id] in ("apply", "hold")
    assert store.events_since(0, types=["incident.triaged"])[-1].entity == inc.id
    if out.replanned[depot.id] == "apply":
        assert store.current_plan(depot.id).parent_plan_id == before


def test_triage_rejects_not_credible_and_queues_unplaceable(store, ops):
    _, _, planner = ops
    svc = IncidentService(store, _engine(store, {"llm": FakeProvider(kind={"not_an_incident": 0.999}, credible=0.02)}), planner)
    assert run(svc.triage(_report(store, "is there traffic anywhere today?").id)).result == "rejected"
    svc2 = IncidentService(store, _engine(store, {"llm": FakeProvider(kind={"traffic": 0.999}, credible=0.999)}), planner)
    out = run(svc2.triage(_report(store, "huge jam near my house").id))
    assert out.result == "needs_location" and out.incident.state == IncidentState.reported


def test_breakdown_needs_vehicle_and_close_restores_it(store, ops):
    depot, vans, planner = ops
    svc = IncidentService(store, _engine(store, {"llm": FakeProvider(kind={"breakdown": 0.999}, credible=0.999)}), planner)
    assert run(svc.triage(_report(store, "our van broke down").id)).result == "needs_vehicle"
    out = run(svc.triage(_report(store, "V0 puncture on GST road", vehicle_id=vans[0].id).id))
    assert out.result == "active" and store.require(Vehicle, vans[0].id).state == VehicleState.broken_down
    plan = store.current_plan(depot.id)
    assert vans[0].id not in {r.vehicle_id for r in plan.routes if r.stops}
    svc.close(out.incident.id, "ops")
    assert store.require(Vehicle, vans[0].id).state == VehicleState.active


def test_human_resolve_logs_decision(store, ops):
    _, _, planner = ops
    svc = IncidentService(store, _engine(store, {"llm": FakeProvider(fail=ProviderUnavailable("no key"))}), planner)
    inc = _report(store, "something on the flyover")
    assert run(svc.triage(inc.id)).result == "human"
    out = svc.resolve(inc.id, "ops lead", "closure", 0.9, area=None)
    assert out.result == "needs_location"
    from dispatch.core.models import Circle
    out = svc.resolve(inc.id, "ops lead", "closure", 0.9, area=Circle(center=LatLng(lat=13.0084, lng=80.205), radius_m=800))
    assert out.result == "active" and store.get_decision(out.decision_id).decider == "human"


def test_api_reports_triage_in_background_and_queue(store, ops):
    depot, _, planner = ops
    eng = _engine(store, {"llm": FakeProvider(kind={"traffic": 0.999}, credible=0.999)})
    with TestClient(create_app(store, planner=planner, engine=eng, settings=Settings())) as c:
        r = c.post("/incidents", json={"description": "CM convoy causing heavy traffic in Tambaram", "source": "driver"})
        assert r.status_code == 201
        inc = c.get("/incidents", params={"state": "active"}).json()        # background task already ran
        assert len(inc) == 1 and inc[0]["kind"] == "traffic" and inc[0]["area"]["center"]["lat"] == 12.9249
        r2 = c.post("/incidents", json={"description": "jam somewhere"}).json()
        q = c.get("/queue").json()
        assert [x["incident"]["id"] for x in q] == [r2["id"]]
        ok = c.post(f"/incidents/{r2['id']}/resolve", json={"by": "ops", "kind": "traffic", "severity": 0.5,
                    "area": {"kind": "circle", "center": {"lat": 13.04, "lng": 80.23}, "radius_m": 1500}}).json()
        assert ok["result"] == "active" and c.get("/queue").json() == []
        assert "llm" in c.get("/health").json()["deciders"]
