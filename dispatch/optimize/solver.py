"""OR-Tools multi-vehicle solver (spec §8): implements the Solver port.

Model:
- Open routes from the depot (vans don't need to return, as in v1).
- Time: travel seconds from the matrix (already incident-adjusted) + service time at each stop.
  Arriving before window_start means waiting (hard). Arriving after window_end is allowed but
  penalised per second (soft), x urgent_factor for urgent orders, so a plan always exists.
- Capacity: weight and volume dimensions.
- Driver shifts: a vehicle's driver (if any) caps that vehicle's route end.
- Vans leave at the plan start (or their driver's shift start), and waiting costs
  params["wait_penalty"] per second, so the solver reorders stops rather than idling when it can.
- Balance: global span cost on time, weighted by params["balance_weight"].
- Every order can be dropped at a large penalty; dropped orders go to Plan.unassigned.
- problem.not_before: a soft earliest arrival per order (unconfirmed risky orders go no earlier than
  the time we told the customer, which puts them later in the route; spec §10).
- Live insertion: problem.fixed_prefix locks each vehicle's committed stops in order, and the
  previous plan seeds the search.

ETA quantiles here are placeholders (p90 = p50 + 25% of elapsed time) until the ETA model
(spec §9, phase 5) replaces them.
"""

from __future__ import annotations

from datetime import timedelta

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from dispatch.core.models import Plan, Stop, VehicleRoute, VehicleState
from dispatch.core.ports import VrpProblem

COST_SCALE = 10                 # costs are integer tenths of a second
DROP_PENALTY = 10_000_000       # >> any route cost: drop an order only if it truly can't fit
MAX_WAIT_S = 4 * 3600
P90_SPREAD = 0.25

DEFAULT_PARAMS = {"late_penalty": 1.0, "balance_weight": 0.5, "urgent_factor": 3.0, "early_penalty": 1.0,
                  "wait_penalty": 0.2}


class OrToolsSolver:
    def __init__(self, first_solution: str = "PATH_CHEAPEST_ARC", metaheuristic: str = "GUIDED_LOCAL_SEARCH"):
        self.first_solution = getattr(routing_enums_pb2.FirstSolutionStrategy, first_solution)
        self.metaheuristic = getattr(routing_enums_pb2.LocalSearchMetaheuristic, metaheuristic)

    # ------------------------------------------------------------ public API
    def solve(self, problem: VrpProblem, time_limit_s: float = 5) -> Plan:
        return self._run(problem, time_limit_s, hint=None)

    def insert(self, plan: Plan, orders, problem: VrpProblem, time_limit_s: float = 2) -> Plan:
        """Re-plan with new `orders` included in `problem.orders`, keeping each vehicle's
        `problem.fixed_prefix` locked and seeding the search from `plan`."""
        return self._run(problem, time_limit_s, hint=plan)

    # ------------------------------------------------------------ model
    def _run(self, problem: VrpProblem, time_limit_s: float, hint: Plan | None) -> Plan:
        params = {**DEFAULT_PARAMS, **problem.params}
        orders = problem.orders
        if any(o.location is None for o in orders):
            raise ValueError("every order needs a location before planning")
        vehicles = [v for v in problem.vehicles if v.state == VehicleState.active]
        n = len(orders) + 1                                       # node 0 = depot
        if problem.matrix.size() != n:
            raise ValueError(f"matrix has {problem.matrix.size()} points, expected depot + {len(orders)} orders")
        if not vehicles:
            return Plan(depot_id=problem.depot_id, params=params, unassigned=[o.id for o in orders])

        secs = problem.matrix.seconds
        service = [0] + [o.service_s for o in orders]
        manager = pywrapcp.RoutingIndexManager(n, len(vehicles), 0)
        routing = pywrapcp.RoutingModel(manager)

        def travel(i: int, j: int) -> int:                        # node -> node seconds, open routes
            return 0 if j == 0 else int(round(secs[i][j] + service[i]))

        def time_cb(a: int, b: int) -> int:
            return travel(manager.IndexToNode(a), manager.IndexToNode(b))

        t_idx = routing.RegisterTransitCallback(time_cb)
        routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(
            lambda a, b: COST_SCALE * time_cb(a, b)))

        # Time dimension, shifts and windows (all relative to problem.start_at, in seconds)
        horizon = 24 * 3600
        routing.AddDimension(t_idx, MAX_WAIT_S, horizon, True, "time")
        time = routing.GetDimensionOrDie("time")
        time.SetGlobalSpanCostCoefficient(int(round(params["balance_weight"] * COST_SCALE)))
        drivers = {d.vehicle_id: d for d in problem.drivers if d.vehicle_id}
        rel = lambda dt: int((dt - problem.start_at).total_seconds())  # noqa: E731
        time.SetSlackCostCoefficientForAllVehicles(int(round(params["wait_penalty"] * COST_SCALE)))
        for vi, v in enumerate(vehicles):
            d = drivers.get(v.id)
            leave = max(0, rel(d.shift_start)) if d is not None else 0
            time.CumulVar(routing.Start(vi)).SetRange(leave, leave)
            if d is not None:
                time.CumulVar(routing.End(vi)).SetMax(max(0, rel(d.shift_end)))
        for k, o in enumerate(orders, start=1):
            idx = manager.NodeToIndex(k)
            if o.window_start is not None:
                time.CumulVar(idx).SetMin(max(0, rel(o.window_start)))
            if o.window_end is not None:
                penalty = params["late_penalty"] * (params["urgent_factor"] if o.priority == "urgent" else 1.0)
                time.SetCumulVarSoftUpperBound(idx, max(0, rel(o.window_end)), int(round(penalty * COST_SCALE)))
            if o.id in problem.not_before:
                time.SetCumulVarSoftLowerBound(idx, max(0, rel(problem.not_before[o.id])),
                                               int(round(params["early_penalty"] * COST_SCALE)))
            routing.AddDisjunction([idx], DROP_PENALTY)

        # Capacity dimensions (integers: grams and millilitres)
        for name, demand, cap in (("weight", [0] + [o.weight_kg for o in orders], [v.capacity_kg for v in vehicles]),
                                  ("volume", [0] + [o.volume_l for o in orders], [v.capacity_l for v in vehicles])):
            cb = routing.RegisterUnaryTransitCallback(
                lambda a, d=demand: int(round(d[manager.IndexToNode(a)] * 1000)))
            routing.AddDimensionWithVehicleCapacity(cb, 0, [int(round(c * 1000)) for c in cap], True, name)

        search = pywrapcp.DefaultRoutingSearchParameters()
        search.first_solution_strategy = self.first_solution
        search.local_search_metaheuristic = self.metaheuristic
        search.time_limit.FromMilliseconds(max(100, int(time_limit_s * 1000)))

        node_of = {o.id: k for k, o in enumerate(orders, start=1)}
        veh_of = {v.id: vi for vi, v in enumerate(vehicles)}
        locks = [[node_of[oid] for oid in problem.fixed_prefix.get(v.id, []) if oid in node_of] for v in vehicles]
        routing.CloseModelWithParameters(search)
        if any(locks):
            routing.ApplyLocksToAllVehicles([[manager.NodeToIndex(k) for k in lk] for lk in locks], False)

        solution = None
        if hint is not None:
            routes = [[] for _ in vehicles]
            for r in hint.routes:
                if r.vehicle_id in veh_of:
                    routes[veh_of[r.vehicle_id]] = [manager.NodeToIndex(node_of[s.order_id])
                                                    for s in r.stops if s.order_id in node_of]
            for vi, lk in enumerate(locks):                        # locked prefix must lead its route
                if lk:
                    lk_idx = [manager.NodeToIndex(k) for k in lk]
                    routes[vi] = lk_idx + [x for x in routes[vi] if x not in lk_idx]
            initial = routing.ReadAssignmentFromRoutes(routes, True)
            if initial is not None:
                solution = routing.SolveFromAssignmentWithParameters(initial, search)
        if solution is None:
            solution = routing.SolveWithParameters(search)
        if solution is None:
            return Plan(depot_id=problem.depot_id, params=params, unassigned=[o.id for o in orders])
        return self._extract(problem, params, orders, vehicles, manager, routing, solution, time)

    # ------------------------------------------------------------ solution -> Plan
    def _extract(self, problem, params, orders, vehicles, manager, routing, solution, time) -> Plan:
        meters = problem.matrix.meters
        routes, assigned = [], set()
        for vi, v in enumerate(vehicles):
            stops, idx, prev, dist, load = [], routing.Start(vi), 0, 0.0, 0.0
            idx = solution.Value(routing.NextVar(idx))
            while not routing.IsEnd(idx):
                node = manager.IndexToNode(idx)
                o = orders[node - 1]
                arrive_s = solution.Min(time.CumulVar(idx))
                p50 = problem.start_at + timedelta(seconds=arrive_s)
                stops.append(Stop(order_id=o.id, seq=len(stops), eta_p50=p50,
                                  eta_p90=p50 + timedelta(seconds=arrive_s * P90_SPREAD), planned_arrival=p50,
                                  leg_m=meters[prev][node], leg_s=problem.matrix.seconds[prev][node]))
                dist += meters[prev][node]
                load += o.weight_kg
                assigned.add(o.id)
                prev, idx = node, solution.Value(routing.NextVar(idx))
            duration = (stops[-1].eta_p50 - problem.start_at).total_seconds() if stops else 0.0
            routes.append(VehicleRoute(vehicle_id=v.id, stops=stops, distance_m=dist, duration_s=duration, load_kg=load))
        return Plan(depot_id=problem.depot_id, params=params, routes=routes,
                    unassigned=[o.id for o in orders if o.id not in assigned])
