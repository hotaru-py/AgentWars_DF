"""Track C: minimal stand-in for routing.py, same contract (specs/WORKBOARD.md, Track A).

tools.py uses this only while routing.py is still a stub, so Track C can run the
agent before Track A lands. Once the real routing.py is merged it takes over.
"""

import copy
import heapq
import itertools

from . import geo, seed

_plan_ids = itertools.count(1)


def build_graph(k: int = 4) -> dict:
    nodes = list(geo.NODES)
    g = {n: {} for n in nodes}

    def link(a, b):
        km = geo.road_km(a, b)
        # separate dicts per direction: a shared one would get multiplied twice by apply_disruption
        g[a][b] = {"min": round(geo.km_to_min(km), 2), "km": round(km, 2)}
        g[b][a] = dict(g[a][b])

    for a in nodes:
        for b in sorted((n for n in nodes if n != a), key=lambda n: geo.road_km(a, n))[:k]:
            link(a, b)
    # make sure the graph is connected: join components by their closest pair
    while True:
        seen, stack = {nodes[0]}, [nodes[0]]
        while stack:
            for nxt in g[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        if len(seen) == len(nodes):
            return g
        link(*min(((a, b) for a in seen for b in nodes if b not in seen),
                  key=lambda p: geo.road_km(*p)))


def apply_disruption(graph: dict, disruption: dict) -> dict:
    g = copy.deepcopy(graph)
    for a in g:
        for b, e in g[a].items():
            if geo.edge_disrupted(a, b, disruption):
                e["min"] = round(e["min"] * disruption.get("multiplier", 1.0), 2)
    return g


def dijkstra(graph: dict, a: str, b: str) -> dict:
    dist, prev, pq = {a: 0.0}, {}, [(0.0, a)]
    while pq:
        d, n = heapq.heappop(pq)
        if n == b:
            break
        if d > dist.get(n, float("inf")):
            continue
        for m, e in graph[n].items():
            nd = d + e["min"]
            if nd < dist.get(m, float("inf")):
                dist[m], prev[m] = nd, n
                heapq.heappush(pq, (nd, m))
    path = [b]
    while path[-1] != a:
        path.append(prev[path[-1]])
    path.reverse()
    km = sum(graph[x][y]["km"] for x, y in zip(path, path[1:]))
    return {"path": path, "min": round(dist[b], 2), "km": round(km, 2)}


def cost_matrix(graph: dict) -> dict:
    return {a: {b: dijkstra(graph, a, b) for b in graph} for a in graph}


def _route(matrix: dict, vehicle_id: str, stops: list[str]) -> dict:
    t, km, path, etas, cur = 0.0, 0.0, [seed.DEPOT["id"]], {}, seed.DEPOT["id"]
    for s in stops:
        leg = matrix[cur][s]
        t, km = t + leg["min"], km + leg["km"]
        path += leg["path"][1:]
        etas[s] = round(t, 1)
        cur = s
    return {"vehicle_id": vehicle_id, "stops": list(stops), "path": path, "eta_by_order": etas,
            "duration_min": round(t, 1), "distance_km": round(km, 1)}


def _cost(routes: list[dict], params: dict) -> float:
    orders = {o["id"]: o for o in seed.ORDERS}
    total = sum(r["duration_min"] for r in routes)
    late = sum(max(0.0, eta - orders[o]["deadline_min"]) * (3 if orders[o]["priority"] == "urgent" else 1)
               for r in routes for o, eta in r["eta_by_order"].items())
    spans = [r["duration_min"] for r in routes] or [0.0]
    return total + params.get("late_penalty", 1.0) * late + params.get("balance_weight", 0.0) * (max(spans) - min(spans))


def solve(matrix: dict, vehicles: list[dict], params: dict) -> dict:
    """Cheapest insertion: repeatedly insert the order/position that raises plan cost least."""
    active = [v for v in vehicles if v.get("status") == "active"]
    weights = {o["id"]: o["weight_kg"] for o in seed.ORDERS}
    stops = {v["id"]: [] for v in active}
    todo = [o["id"] for o in seed.ORDERS if o["id"] in matrix]
    unassigned = []
    while todo:
        best = None
        for oid in todo:
            for v in active:
                if sum(weights[s] for s in stops[v["id"]]) + weights[oid] > v["capacity_kg"]:
                    continue
                for i in range(len(stops[v["id"]]) + 1):
                    trial = dict(stops)
                    trial[v["id"]] = stops[v["id"]][:i] + [oid] + stops[v["id"]][i:]
                    c = _cost([_route(matrix, vid, s) for vid, s in trial.items()], params)
                    if best is None or c < best[0]:
                        best = (c, oid, v["id"], trial[v["id"]])
        if best is None:
            unassigned += todo
            break
        _, oid, vid, new_stops = best
        stops[vid] = new_stops
        todo.remove(oid)
    return {"id": f"plan-{next(_plan_ids)}", "params": dict(params),
            "routes": [_route(matrix, vid, s) for vid, s in stops.items()],
            "unassigned": unassigned}


def retime(plan: dict, matrix: dict) -> dict:
    p = copy.deepcopy(plan)
    p["routes"] = [_route(matrix, r["vehicle_id"], r["stops"]) for r in plan["routes"]]
    return p
