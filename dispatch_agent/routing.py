"""Track A: road graph, Dijkstra, all-pairs matrix, multi-stop solver.

Contract: specs/WORKBOARD.md, section "Track A". Pure functions only: no ADK, no I/O.
"""

import copy
import heapq
import itertools

from . import geo, seed

Graph = dict[str, dict[str, dict]]  # graph[a][b] = {"min": float, "km": float, "variant": int}
# "variant" = which real road option (geo.road_options) the van takes on that edge.

_plan_ids = itertools.count(1)


# ---------------------------------------------------------------- graph
def _edge(a: str, b: str) -> dict:
    km = geo.road_km(a, b)
    return {"min": geo.km_to_min(km), "km": km, "variant": 0}


def build_graph(k: int = 4) -> Graph:
    """Sparse undirected graph: each node linked to its k nearest, then bridged until connected."""
    ids = list(geo.NODES)
    graph: Graph = {n: {} for n in ids}
    for a in ids:
        nearest = sorted((b for b in ids if b != a), key=lambda b: geo.road_km(a, b))[:k]
        for b in nearest:
            graph[a][b], graph[b][a] = _edge(a, b), _edge(a, b)

    # Join any islands via the closest cross-component pair.
    while True:
        comp = _components(graph)
        if len(comp) == 1:
            return graph
        first = comp[0]
        rest = [n for c in comp[1:] for n in c]
        a, b = min(((a, b) for a in first for b in rest), key=lambda p: geo.road_km(*p))
        graph[a][b], graph[b][a] = _edge(a, b), _edge(a, b)


def _components(graph: Graph) -> list[set[str]]:
    seen, out = set(), []
    for start in graph:
        if start in seen:
            continue
        comp, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in comp:
                continue
            comp.add(n)
            stack.extend(graph[n])
        seen |= comp
        out.append(comp)
    return out


def apply_disruption(graph: Graph, disruption: dict) -> Graph:
    """Return a copy where each edge takes its best real road under the disruption: the
    part of a road inside the zone runs disruption["multiplier"] times slower, so a
    longer road that avoids the zone can win (a real detour)."""
    if disruption.get("kind") not in ("flood", "traffic"):
        return {a: {b: dict(e) for b, e in row.items()} for a, row in graph.items()}
    out: Graph = {}
    for a, row in graph.items():
        out[a] = {}
        for b in row:
            c = geo.leg_choice(a, b, disruption)
            out[a][b] = {"min": c["min"], "km": c["km"], "variant": c["variant"]}
    return out


# ---------------------------------------------------------------- shortest paths
def _sssp(graph: Graph, src: str) -> tuple[dict, dict]:
    dist, prev = {src: 0.0}, {}
    pq = [(0.0, src)]
    while pq:
        d, n = heapq.heappop(pq)
        if d > dist.get(n, float("inf")):
            continue
        for m, e in graph[n].items():
            nd = d + e["min"]
            if nd < dist.get(m, float("inf")):
                dist[m], prev[m] = nd, n
                heapq.heappush(pq, (nd, m))
    return dist, prev


def _path(prev: dict, src: str, dst: str) -> list[str]:
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return path[::-1]


def _path_km(graph: Graph, path: list[str]) -> float:
    return sum(graph[a][b]["km"] for a, b in zip(path, path[1:]))


def dijkstra(graph: Graph, a: str, b: str) -> dict:
    """Fastest path by minutes: {"path", "min", "km"}."""
    dist, prev = _sssp(graph, a)
    if b not in dist:
        raise ValueError(f"No path from {a} to {b}")
    path = _path(prev, a, b)
    return {"path": path, "min": dist[b], "km": _path_km(graph, path)}


def _path_geometry(graph: Graph, path: list[str]) -> list:
    pts: list = []
    for a, b in zip(path, path[1:]):
        if a not in geo.NODES or b not in geo.NODES:  # hand-built test graphs have no road data
            return []
        seg = geo.leg_geometry(a, b, graph[a][b].get("variant", 0))
        pts.extend(seg[1:] if pts else seg)
    return pts


def cost_matrix(graph: Graph) -> dict[str, dict[str, dict]]:
    """All-pairs Dijkstra: m[a][b] = {"min", "km", "path", "geometry"} (geometry = real road shape)."""
    m: dict[str, dict[str, dict]] = {}
    for a in graph:
        dist, prev = _sssp(graph, a)
        m[a] = {}
        for b in dist:
            path = _path(prev, a, b)
            m[a][b] = {"min": dist[b], "km": _path_km(graph, path), "path": path,
                       "geometry": _path_geometry(graph, path)}
    return m


# ---------------------------------------------------------------- solver
_ORDERS = {o["id"]: o for o in seed.ORDERS}
_DEPOT = seed.DEPOT["id"]
URGENT_FACTOR = 3.0


def _timeline(stops: list[str], matrix: dict) -> dict:
    """Drive depot -> stops in order (no return leg); ETAs in minutes from shift start."""
    path, etas, t, km, cur, geom = [_DEPOT], {}, 0.0, 0.0, _DEPOT, []
    for s in stops:
        leg = matrix[cur][s]
        t += leg["min"]
        km += leg["km"]
        path.extend(leg["path"][1:])
        lg = leg.get("geometry") or []
        geom.extend(lg[1:] if geom else lg)
        etas[s] = round(t, 2)
        cur = s
    return {"stops": list(stops), "path": path, "eta_by_order": etas,
            "duration_min": round(t, 2), "distance_km": round(km, 2),
            "geometry": geom}  # [[lat, lng], ...] along real roads, for the map


def _lateness(stops: list[str], matrix: dict, penalty: float) -> float:
    t, cur, cost = 0.0, _DEPOT, 0.0
    for s in stops:
        t += matrix[cur][s]["min"]
        cur = s
        o = _ORDERS[s]
        late = max(0.0, t - o["deadline_min"])
        cost += penalty * late * (URGENT_FACTOR if o["priority"] == "urgent" else 1.0)
    return cost


def _route_min(stops: list[str], matrix: dict) -> float:
    cur, t = _DEPOT, 0.0
    for s in stops:
        t += matrix[cur][s]["min"]
        cur = s
    return t


def _cost(routes: dict[str, list[str]], matrix: dict, params: dict) -> float:
    mins = [_route_min(s, matrix) for s in routes.values()]
    late = sum(_lateness(s, matrix, params["late_penalty"]) for s in routes.values())
    spread = (max(mins) - min(mins)) if mins else 0.0
    return sum(mins) + late + params["balance_weight"] * spread


def solve(matrix: dict, vehicles: list[dict], params: dict) -> dict:
    """Cheapest insertion over active vehicles, respecting capacity_kg."""
    active = [v for v in vehicles if v.get("status", "active") != "broken_down"]
    routes = {v["id"]: [] for v in active}
    cap = {v["id"]: v["capacity_kg"] for v in active}
    load = {v["id"]: 0.0 for v in active}
    remaining = [oid for oid in _ORDERS if oid in matrix]
    unassigned = []

    cur_cost = _cost(routes, matrix, params) if routes else 0.0
    while remaining:
        best = None  # (cost, order, vehicle, position)
        for oid in remaining:
            w = _ORDERS[oid]["weight_kg"]
            for vid, stops in routes.items():
                if load[vid] + w > cap[vid]:
                    continue
                for pos in range(len(stops) + 1):
                    routes[vid] = stops[:pos] + [oid] + stops[pos:]
                    c = _cost(routes, matrix, params)
                    routes[vid] = stops
                    if best is None or c < best[0]:
                        best = (c, oid, vid, pos)
        if best is None:  # nothing fits anywhere
            unassigned.extend(remaining)
            break
        cur_cost, oid, vid, pos = best
        routes[vid].insert(pos, oid)
        load[vid] += _ORDERS[oid]["weight_kg"]
        remaining.remove(oid)

    return {"id": f"plan-{next(_plan_ids)}", "params": dict(params),
            "routes": [{"vehicle_id": vid, **_timeline(stops, matrix)} for vid, stops in routes.items()],
            "unassigned": unassigned}


def retime(plan: dict, matrix: dict) -> dict:
    """Same stop order, ETAs/paths recomputed on a (disrupted) matrix."""
    out = copy.deepcopy(plan)
    out["routes"] = [{"vehicle_id": r["vehicle_id"], **_timeline(r["stops"], matrix)}
                     for r in plan["routes"]]
    return out
