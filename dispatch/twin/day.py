"""Run the day: replay the current plan in the twin, learn, save the new policy, log a report (spec §9)."""

from __future__ import annotations

from datetime import datetime

from dispatch.core.events import DaySimulated
from dispatch.core.models import Depot, Incident, Order, Policy
from dispatch.core.ports import RoutingEngine
from dispatch.store import Store, StoreError

from .learn import learn, next_run
from .simulate import simulate_day


def plan_start(plan) -> datetime:
    iso = plan.params.get("start_at")
    return datetime.fromisoformat(iso) if iso else plan.created_at


def run_day(store: Store, engine: RoutingEngine, depot_id: str) -> DaySimulated:
    depot = store.require(Depot, depot_id)
    plan = store.current_plan(depot_id)
    if plan is None:
        raise StoreError("no plan to run: plan routes first")
    orders = {o.id: o for o in store.list(Order, depot_id=depot_id, limit=100_000)}
    policy = store.current_policy()
    versions = store.policies()
    previous = next((p for p in versions if p.version == policy.version - 1), None)
    if not versions:                                   # first ever run: persist the default policy as v1
        store.save_policy(policy, reason="initial policy")
    run = next_run(policy)
    day = simulate_day(plan, orders, depot.location, plan_start(plan), engine, store.list(Incident), run)
    new_policy, changes, rolled_back = learn(policy, day, run, previous)
    err = day.delay_error()
    report = DaySimulated(run=run, plan_id=plan.id, stops=len(day.stops), late_predicted=day.late("predicted"),
                         late_actual=day.late("actual"), delay_error=None if err is None else round(err, 4),
                         observed={k.value: v for k, v in day.observed_factor.items()}, changes=changes,
                         rolled_back=rolled_back, policy_version=new_policy.version)
    store.save_policy(Policy.model_validate(new_policy.model_dump()),
                      reason=f"day {run}: " + (", ".join(f"{k} {a}->{b}" for k, (a, b) in changes.items()) or "nothing to learn"))
    store.record_day(report)
    return report
