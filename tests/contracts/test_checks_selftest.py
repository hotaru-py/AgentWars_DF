"""Prove the contract checks work: tiny fake implementations must pass, broken ones must fail."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from dispatch.core.decisions import ChoiceAnswer, NoulAnswer, ScoreAnswer
from dispatch.core.models import Customer, Order, Plan, Stop, Vehicle, VehicleRoute
from dispatch.core.ports import (EtaQuantiles, GeoResult, Matrix, MessageRef, RouteLeg, RouteShape, VrpProblem)
from tests.contracts.checks import (CHENNAI, check_decider, check_eta_model, check_geocoder, check_messenger,
                                    check_routing_engine, check_signal_source, check_solver)

T0 = datetime(2026, 10, 17, 3, 30, tzinfo=timezone.utc)


def _km(a, b):
    return math.dist((a.lat * 111, a.lng * 108), (b.lat * 111, b.lng * 108))


class FakeEngine:
    name = "fake"

    def matrix(self, points, depart_at=None):
        m = [[_km(a, b) * 1300 for b in points] for a in points]
        return Matrix(points=points, meters=m, seconds=[[x / 7 for x in r] for r in m], source="fake")

    def route(self, points):
        return RouteShape(legs=[RouteLeg(seconds=1, meters=1) for _ in points[1:]])


class FakeSolver:                      # one vehicle, orders in given order
    def solve(self, problem, time_limit_s=5):
        v = problem.vehicles[0]
        stops = [Stop(order_id=o.id, seq=i, eta_p50=T0 + timedelta(minutes=10 * i),
                      eta_p90=T0 + timedelta(minutes=10 * i + 5), planned_arrival=T0 + timedelta(minutes=10 * i))
                 for i, o in enumerate(problem.orders)]
        return Plan(depot_id=problem.depot_id, routes=[VehicleRoute(vehicle_id=v.id, stops=stops)])

    def insert(self, plan, orders, problem, time_limit_s=2):
        return self.solve(problem)


class FakeDecider:
    name, model, capabilities = "rules", None, {"text"}

    async def decide(self, state, questions):
        return {"kind": ChoiceAnswer.from_probabilities({"flood": 0.8, "traffic": 0.2}),
                "severity": ScoreAnswer.from_probabilities([0.1, 0.3, 0.6]),
                "credible": NoulAnswer(p=0.9)}


class FakeGeocoder:
    async def geocode(self, raw, hint=None):
        return GeoResult(location=CHENNAI[2], confidence=0.8, method="fake")


class FakeEta:
    def predict(self, legs, context):
        t, out = 0.0, []
        for leg in legs:
            t += leg.seconds * (1 + leg.incident_share)
            out.append(EtaQuantiles(p50_s=t, p90_s=t * 1.2))
        return out


class FakeMessenger:
    async def send(self, to, template, vars, channel="whatsapp"):
        return MessageRef(id="msg_1", channel="sim", sent_at=datetime.now(timezone.utc))


class FakeSignals:
    name = "fake"

    async def poll(self, since):
        return []


def _problem(capacity=50):
    m = FakeEngine().matrix(CHENNAI)
    orders = [Order(depot_id="dep_1", customer_id="cus_1", raw_address=f"addr {i}", location=p, weight_kg=10)
              for i, p in enumerate(CHENNAI[1:])]
    return VrpProblem(depot_id="dep_1", depot=CHENNAI[0], start_at=T0, orders=orders, matrix=m,
                      vehicles=[Vehicle(depot_id="dep_1", capacity_kg=capacity, capacity_l=100)])


def test_fakes_pass_every_check():
    check_routing_engine(FakeEngine())
    check_solver(FakeSolver(), _problem())
    check_decider(FakeDecider())
    check_geocoder(FakeGeocoder())
    check_eta_model(FakeEta())
    check_messenger(FakeMessenger(), Customer(name="A", phone="1"))
    check_signal_source(FakeSignals())


def test_solver_check_catches_overloaded_van():
    with pytest.raises(AssertionError, match="over capacity"):
        check_solver(FakeSolver(), _problem(capacity=15))


def test_solver_check_catches_dropped_order():
    class Dropping(FakeSolver):
        def solve(self, problem, time_limit_s=5):
            p = super().solve(problem)
            p.routes[0].stops = p.routes[0].stops[:-1]
            return p
    with pytest.raises(AssertionError, match="every order"):
        check_solver(Dropping(), _problem())


def test_engine_check_catches_nonzero_diagonal():
    class Bad(FakeEngine):
        def matrix(self, points, depart_at=None):
            m = super().matrix(points)
            m.seconds[0][0] = 5
            return m
    with pytest.raises(AssertionError):
        check_routing_engine(Bad())
