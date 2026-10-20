"""Plan scoring and the critic, ported from the v1 demo (spec §8).

Five measures, each 0..1 and higher is better, combined with v1's weights:
  on_time     share of orders arriving by their window_end (no window = on time; unassigned = late)
  efficiency  minimum-spanning-tree meters over depot + orders / plan meters (a lower bound / actual)
  cycle       mean direct depot->order seconds / mean planned arrival (seconds after start)
  balance     1 - (longest - shortest route duration) / longest, over all active vehicles
  resilience  on_time if every arrival came 20% later
The critic is deterministic code: it picks a plan and can't be talked into a bad one.
"""

from __future__ import annotations

from dispatch.core.models import Plan, PlanScores
from dispatch.core.ports import VrpProblem

WEIGHTS = {"on_time": 0.30, "efficiency": 0.20, "cycle": 0.15, "balance": 0.15, "resilience": 0.20}
STRESS = 1.2

# The three candidate strategies from v1, in solver params.
STRATEGIES = [
    {"name": "balanced", "late_penalty": 1.0, "balance_weight": 0.5},
    {"name": "deadline-first", "late_penalty": 4.0, "balance_weight": 0.1},
    {"name": "compact", "late_penalty": 0.5, "balance_weight": 0.0},
]


def _mst_m(problem: VrpProblem) -> float:
    m, n = problem.matrix.meters, problem.matrix.size()
    if n < 2:
        return 0.0
    w = lambda i, j: min(m[i][j], m[j][i])  # noqa: E731  (roads can be one-way: use the shorter direction)
    best, total = {j: w(0, j) for j in range(1, n)}, 0.0
    while best:
        j = min(best, key=best.get)
        total += best.pop(j)
        for k in best:
            best[k] = min(best[k], w(j, k))
    return total


def score_plan(plan: Plan, problem: VrpProblem) -> PlanScores:
    start = problem.start_at
    elapsed = {s.order_id: (s.eta_p50 - start).total_seconds() for r in plan.routes for s in r.stops}
    n = len(problem.orders)

    def on_time(scale: float) -> float:
        if not n:
            return 1.0
        ok = 0
        for o in problem.orders:
            if o.id not in elapsed:
                continue
            if o.window_end is None or elapsed[o.id] * scale <= (o.window_end - start).total_seconds():
                ok += 1
        return ok / n

    plan_m = sum(r.distance_m for r in plan.routes)
    durations = [r.duration_s for r in plan.routes] or [0.0]
    direct = [problem.matrix.seconds[0][k] for k in range(1, n + 1)]
    mean_eta = sum(elapsed.values()) / len(elapsed) if elapsed else 0.0
    late = sum(1 for o in problem.orders if o.id not in elapsed
               or (o.window_end is not None and elapsed[o.id] > (o.window_end - start).total_seconds()))
    s = {
        "on_time": on_time(1.0),
        "efficiency": min(1.0, _mst_m(problem) / plan_m) if plan_m else 0.0,
        "cycle": min(1.0, (sum(direct) / len(direct)) / mean_eta) if mean_eta and direct else 0.0,
        "balance": 1 - (max(durations) - min(durations)) / max(durations) if max(durations) else 1.0,
        "resilience": on_time(STRESS),
    }
    s = {k: round(v, 4) for k, v in s.items()}
    return PlanScores(**s, overall=round(sum(WEIGHTS[k] * v for k, v in s.items()), 4), late_orders=late,
                      total_duration_s=round(sum(durations), 1), total_distance_m=round(plan_m, 1))


def critic(plans: list[Plan], problem: VrpProblem, baseline: PlanScores | None = None) -> tuple[Plan | None, list[dict]]:
    """Score every candidate; return (best plan or None for "hold", table). Plans that drop orders
    are rejected; if no valid plan beats `baseline` (e.g. the current plan re-timed), hold."""
    table, best, best_s = [], None, None
    for p in plans:
        sc = score_plan(p, problem)
        p.scores = sc
        valid = not p.unassigned
        table.append({"plan_id": p.id, "name": p.params.get("name", p.id), "valid": valid, **sc.model_dump()})
        if valid and (best_s is None or sc.overall > best_s.overall):
            best, best_s = p, sc
    if best is None or (baseline is not None and best_s.overall < baseline.overall):
        return None, table
    return best, table
