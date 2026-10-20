"""Planning service: store + routing engine + solver + critic -> an applied plan and a ledger entry.

    Planner(store, engine, solver).run(depot_id, reason)

1. Load the depot, its active vehicles and drivers, and its plannable orders (geocoded, confirmed,
   planned or reattempt, with a location). Risky orders still waiting for the customer's reply go no
   earlier than the time we told them, and their stops are marked "call first" (spec §10). Orders
   rescheduled to start after the depot closes belong to a later day and are left out.
2. Travel matrix from the routing engine, with active incidents applied.
3. Solve each candidate strategy; the critic scores them against the current plan re-timed under
   the same conditions ("do nothing").
4. Apply the winner (with road geometry for the map), or hold. Either way, record a decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from dispatch.core.decisions import DecisionAction, DecisionRecord, Route
from dispatch.core.models import (Depot, Driver, GateThresholds, Incident, IncidentState, Order, OrderState, Plan,
                                  Vehicle, VehicleState, utcnow)
from dispatch.core.ports import RoutingEngine, Solver, VrpProblem
from dispatch.optimize import STRATEGIES, critic, score_plan
from dispatch.optimize.retime import retime
from dispatch.routing import RoutingUnavailable, apply_incidents
from dispatch.store import Store, StoreError

PLANNABLE = {OrderState.geocoded, OrderState.confirmed, OrderState.planned, OrderState.reattempt}


class NothingToPlan(StoreError):
    pass


def awaiting_reply(o: Order) -> bool:
    """We asked a risky order's customer to confirm and they haven't answered (spec §10)."""
    return o.confirm_requested_at is not None and o.customer_reply is None and o.state != OrderState.confirmed


@dataclass
class PlanResult:
    verdict: str                        # "apply" | "hold"
    plan: Plan | None                   # the applied plan (None on hold)
    decision: DecisionRecord
    candidates: list[dict]
    baseline: dict | None
    skipped_orders: list[str] = field(default_factory=list)    # plannable state but no location yet
    timings_ms: dict = field(default_factory=dict)


class Planner:
    def __init__(self, store: Store, engine: RoutingEngine, solver: Solver, time_limit_s: float = 1.0,
                 strategies: list[dict] | None = None, with_geometry: bool = True):
        self.store, self.engine, self.solver = store, engine, solver
        self.time_limit_s, self.strategies, self.with_geometry = time_limit_s, strategies or STRATEGIES, with_geometry

    def problem(self, depot_id: str) -> tuple[VrpProblem, list[str]]:
        depot = self.store.require(Depot, depot_id)
        vehicles = [v for v in self.store.list(Vehicle, depot_id=depot_id) if v.state == VehicleState.active]
        vids = {v.id for v in vehicles}
        drivers = [d for d in self.store.list(Driver) if d.vehicle_id in vids]
        candidates = [o for o in self.store.list(Order, depot_id=depot_id, limit=10_000) if o.state in PLANNABLE
                      and not (o.window_start and o.window_start >= depot.closes_at)]   # another day
        orders = [o for o in candidates if o.location is not None]
        skipped = [o.id for o in candidates if o.location is None]
        if not orders:
            raise NothingToPlan(f"depot {depot_id} has no plannable orders with a location")
        matrix = self.engine.matrix([depot.location] + [o.location for o in orders])
        incidents = self.store.list(Incident, state=IncidentState.active.value)
        matrix = apply_incidents(matrix, incidents, self.engine)
        not_before = {o.id: o.promised_at for o in orders if awaiting_reply(o) and o.promised_at}
        return VrpProblem(depot_id=depot_id, depot=depot.location, start_at=max(utcnow(), depot.opens_at),
                          orders=orders, vehicles=vehicles, drivers=drivers, matrix=matrix,
                          not_before=not_before), skipped

    def run(self, depot_id: str, reason: str = "manual") -> PlanResult:
        t0 = time.perf_counter()
        problem, skipped = self.problem(depot_id)
        t1 = time.perf_counter()
        plans = []
        for st in self.strategies:
            p = self.solver.solve(problem.model_copy(update={"params": st}), time_limit_s=self.time_limit_s)
            p.reason, p.params = reason, {**p.params, **st, "start_at": problem.start_at.isoformat()}
            for r in p.routes:
                for s in r.stops:
                    s.call_first = s.order_id in problem.not_before
            plans.append(p)
        t2 = time.perf_counter()
        current = self.store.current_plan(depot_id)
        base = retime(current, problem) if current else None
        base_scores = score_plan(base, problem) if base else None
        best, table = critic(plans, problem, base_scores)
        verdict = "apply" if best is not None else "hold"
        if best is not None:
            best.parent_plan_id = current.id if current else None
            if self.with_geometry:
                self._add_geometry(best, problem)
            self.store.save_plan(best)
        rec = DecisionRecord(
            type="plan_select", subject=best.id if best else (current.id if current else depot_id), decider="rules",
            model="critic-v1", method="rule", raw_confidence=1.0, confidence=1.0, gate=GateThresholds(),
            route=Route.act,
            action=DecisionAction(kind="apply_plan" if best else "hold", ref=best.id if best else None),
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            cost={"candidates": table, "baseline": base_scores.model_dump() if base_scores else None, "reason": reason})
        self.store.record_decision(rec)
        if best is not None:
            self.store.apply_plan(best.id, decision_id=rec.id)
        t3 = time.perf_counter()
        return PlanResult(verdict=verdict, plan=best, decision=rec, candidates=table,
                          baseline=base_scores.model_dump() if base_scores else None, skipped_orders=skipped,
                          timings_ms={"matrix": round((t1 - t0) * 1000), "solve": round((t2 - t1) * 1000),
                                      "total": round((t3 - t0) * 1000)})

    def _add_geometry(self, plan: Plan, problem: VrpProblem) -> None:
        loc = {o.id: o.location for o in problem.orders}
        for r in plan.routes:
            if not r.stops:
                continue
            try:
                pts = [problem.depot]
                for s in r.stops:                       # detour legs go through their via point
                    pts += ([s.via] if s.via else []) + [loc[s.order_id]]
                r.geometry = self.engine.route(pts).geometry
            except RoutingUnavailable:
                r.geometry = []                 # the map falls back to straight lines between stops
