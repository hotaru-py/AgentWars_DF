"""Re-time a plan: same vehicles and stop order, arrival times recomputed on a (new) matrix.
Used for the critic's "do nothing" baseline after conditions change (spec §8)."""

from __future__ import annotations

from datetime import timedelta

from dispatch.core.models import Plan, Stop, VehicleRoute
from dispatch.core.ports import VrpProblem

from .solver import P90_SPREAD


def retime(plan: Plan, problem: VrpProblem) -> Plan:
    """Orders missing from `problem` are dropped from routes; orders in `problem` but not in the plan
    are listed as unassigned, so the baseline is honest about what it doesn't cover."""
    node = {o.id: k for k, o in enumerate(problem.orders, start=1)}
    order = {o.id: o for o in problem.orders}
    secs, meters, start = problem.matrix.seconds, problem.matrix.meters, problem.start_at
    active = {v.id for v in problem.vehicles if v.state == "active"}
    routes, covered = [], set()
    for r in plan.routes:
        if r.vehicle_id not in active:
            continue
        t, prev, prev_service, dist, load, stops = 0.0, 0, 0, 0.0, 0.0, []
        for s in r.stops:
            if s.order_id not in node:
                continue
            k, o = node[s.order_id], order[s.order_id]
            t += prev_service + secs[prev][k]
            if o.window_start is not None:
                t = max(t, (o.window_start - start).total_seconds())
            p50 = start + timedelta(seconds=t)
            stops.append(Stop(order_id=o.id, seq=len(stops), eta_p50=p50, eta_p90=p50 + timedelta(seconds=t * P90_SPREAD),
                              planned_arrival=s.planned_arrival, leg_m=meters[prev][k], leg_s=secs[prev][k],
                              via=problem.matrix.detours.get(f"{prev}-{k}")))
            dist += meters[prev][k]
            load += o.weight_kg
            covered.add(o.id)
            prev, prev_service = k, o.service_s
        routes.append(VehicleRoute(vehicle_id=r.vehicle_id, driver_id=r.driver_id, stops=stops, distance_m=dist,
                                   duration_s=t if stops else 0.0, load_kg=load))
    return Plan(id=plan.id, depot_id=plan.depot_id, reason="baseline (current plan re-timed)", params=plan.params,
                routes=routes, unassigned=[o.id for o in problem.orders if o.id not in covered],
                parent_plan_id=plan.parent_plan_id)
