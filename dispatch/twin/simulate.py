"""Digital twin, day replay (spec §9): play the current plan against a hidden "true" world.

We have no real GPS yet, so the simulator plays reality: every active incident slows vans by its
kind's TRUE factor (hidden from the planner, +-3% seeded noise per run), scaled by the incident's
severity exactly as predictions are. Comparing predicted and simulated arrivals is the learning
signal; the slowdown experienced inside each incident zone is what a real fleet would measure.

Three timelines per route, cumulated the same way (service time at each stop):
  base       the routing engine's legs with no incidents (a normal day)
  predicted  the plan's legs (the matrix with the policy's predicted slowdowns)
  actual     the legs the van really drives (through its detour's via point, if the plan chose one),
             whose in-zone parts run at the TRUE slowdown
All three use the routing engine's matrix, so with no incidents they agree exactly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from dispatch.core.models import Incident, IncidentKind, IncidentState, LatLng, Order, Plan
from dispatch.core.ports import RoutingEngine
from dispatch.routing import share_inside

TRUTH = {IncidentKind.flood: 5.0, IncidentKind.traffic: 3.0, IncidentKind.closure: 12.0, IncidentKind.other: 1.8}
NOISE = 0.03
SEED = 42


def severity_weight(severity: float) -> float:
    """Same scaling the triage uses for predictions (dispatch/incidents.py)."""
    return 0.25 + severity


def true_factor(kind: IncidentKind, run: int) -> float:
    return TRUTH.get(kind, 1.0) * random.Random(SEED * 1000 + run).uniform(1 - NOISE, 1 + NOISE)


@dataclass
class StopTimes:
    order_id: str
    base_s: float
    predicted_s: float
    actual_s: float
    deadline_s: float | None          # seconds after the plan's start, if the order has a deadline


@dataclass
class DayResult:
    stops: list[StopTimes]
    observed_factor: dict[IncidentKind, float] = field(default_factory=dict)   # what the day revealed, per kind
    exposure_s: dict[IncidentKind, float] = field(default_factory=dict)        # base seconds driven inside zones

    def delay_error(self) -> float | None:
        """|predicted delay - actual delay| / actual delay, over stops the incidents actually delayed."""
        hit = [s for s in self.stops if s.actual_s - s.base_s > 1]
        if not hit:
            return None
        return sum(abs(s.predicted_s - s.actual_s) for s in hit) / sum(s.actual_s - s.base_s for s in hit)

    def late(self, which: str) -> int:
        return sum(1 for s in self.stops if s.deadline_s is not None and getattr(s, f"{which}_s") > s.deadline_s)


def simulate_day(plan: Plan, orders: dict[str, Order], depot: LatLng, start_at: datetime, engine: RoutingEngine,
                 incidents: list[Incident], run: int) -> DayResult:
    live = [i for i in incidents if i.state == IncidentState.active and i.area is not None and i.slowdown > 1]
    stops: list[StopTimes] = []
    zone_base: dict[IncidentKind, float] = {}
    zone_actual: dict[IncidentKind, float] = {}
    weight: dict[IncidentKind, float] = {}
    for r in plan.routes:
        if not r.stops:
            continue
        pts = [depot] + [orders[s.order_id].location for s in r.stops]
        vias = [s.via for s in r.stops if s.via is not None]
        m = engine.matrix(pts + vias)
        via_idx = {id(v): len(pts) + n for n, v in enumerate(vias)}
        tb = tp = ta = 0.0
        prev_service = 0
        for k, s in enumerate(r.stops):
            o = orders[s.order_id]
            a, b = pts[k], pts[k + 1]
            base = m.seconds[k][k + 1]
            driven = ([(k, via_idx[id(s.via)], a, s.via), (via_idx[id(s.via)], k + 1, s.via, b)] if s.via is not None
                      else [(k, k + 1, a, b)])
            actual = 0.0
            for i0, j0, p, q in driven:
                part = m.seconds[i0][j0]
                actual += part
                for inc in live:
                    share = share_inside(p, q, inc.area)
                    if not share:
                        continue
                    w = severity_weight(inc.severity)
                    true_slow = 1 + (true_factor(inc.kind, run) - 1) * w
                    actual += part * share * (true_slow - 1)
                    zone_base[inc.kind] = zone_base.get(inc.kind, 0.0) + part * share
                    zone_actual[inc.kind] = zone_actual.get(inc.kind, 0.0) + part * share * true_slow
                    weight[inc.kind] = w
            tb += prev_service + base
            tp += prev_service + s.leg_s
            ta += prev_service + actual
            deadline = (o.window_end - start_at).total_seconds() if o.window_end else None
            stops.append(StopTimes(o.id, tb, tp, ta, deadline))
            prev_service = o.service_s
    observed = {kind: round(1 + (zone_actual[kind] / b - 1) / weight[kind], 3)        # experienced slowdown,
                for kind, b in zone_base.items() if b > 0}                            # scaled back to the factor
    return DayResult(stops=stops, observed_factor=observed, exposure_s={k: round(v, 1) for k, v in zone_base.items()})
