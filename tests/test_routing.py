"""Track A tests: graph, Dijkstra, matrix, solver. No dependency on feedback.py."""

import copy

from dispatch_agent import geo, routing, seed

PARAMS = {"name": "balanced", "late_penalty": 1.0, "balance_weight": 0.5}
FLOOD = dict(seed.SCENARIOS["flood"], multiplier=4.0)


def _vehicles():
    return copy.deepcopy(seed.VEHICLES)


def _toy():
    g = {n: {} for n in "ABCDE"}
    for a, b, m in [("A", "B", 1), ("B", "C", 1), ("A", "C", 5), ("C", "D", 2), ("D", "E", 1)]:
        g[a][b] = g[b][a] = {"min": m, "km": m * 10}
    return g


# ---------------------------------------------------------------- graph
def test_build_graph_connected_undirected_sparse():
    g = routing.build_graph()
    assert set(g) == set(geo.NODES)
    assert len(routing._components(g)) == 1
    for a, row in g.items():
        assert len(row) >= 4 and len(row) < len(g) - 1 or len(g) <= 5
        for b, e in row.items():
            assert g[b][a] == e


def test_apply_disruption_copies_and_slows_flooded_edges():
    g = routing.build_graph()
    flooded = routing.apply_disruption(g, FLOOD)
    assert flooded is not g
    slowed = [(a, b) for a in g for b in g[a] if flooded[a][b]["min"] > g[a][b]["min"]]
    assert slowed
    for a, b in slowed:
        assert geo.edge_disrupted(a, b, FLOOD)
        assert flooded[a][b]["min"] == g[a][b]["min"] * 4.0
        assert flooded[a][b]["km"] == g[a][b]["km"]
    assert routing.build_graph() == g  # original untouched


def test_breakdown_leaves_graph_unchanged():
    g = routing.build_graph()
    assert routing.apply_disruption(g, dict(seed.SCENARIOS["breakdown"], multiplier=1.0)) == g


# ---------------------------------------------------------------- dijkstra / matrix
def test_dijkstra_takes_detour_when_cheaper():
    r = routing.dijkstra(_toy(), "A", "C")
    assert r == {"path": ["A", "B", "C"], "min": 2, "km": 20}


def test_dijkstra_same_node():
    assert routing.dijkstra(_toy(), "A", "A") == {"path": ["A"], "min": 0.0, "km": 0}


def test_cost_matrix_matches_dijkstra():
    g = _toy()
    m = routing.cost_matrix(g)
    for a in g:
        for b in g:
            d = routing.dijkstra(g, a, b)
            assert m[a][b] == {"min": d["min"], "km": d["km"], "path": d["path"]}


def test_flood_changes_matrix_for_omr():
    g = routing.build_graph()
    base = routing.cost_matrix(g)
    wet = routing.cost_matrix(routing.apply_disruption(g, FLOOD))
    assert wet["DEPOT"]["O6"]["min"] > base["DEPOT"]["O6"]["min"]


# ---------------------------------------------------------------- solve
def test_solve_assigns_every_order_once_within_capacity():
    m = routing.cost_matrix(routing.build_graph())
    plan = routing.solve(m, _vehicles(), PARAMS)
    stops = [s for r in plan["routes"] for s in r["stops"]]
    assert sorted(stops) == sorted(o["id"] for o in seed.ORDERS)
    assert plan["unassigned"] == []
    weights = {o["id"]: o["weight_kg"] for o in seed.ORDERS}
    for r in plan["routes"]:
        assert sum(weights[s] for s in r["stops"]) <= 50
        assert r["path"][0] == "DEPOT" and set(r["stops"]) <= set(r["path"])
        assert list(r["eta_by_order"]) == r["stops"]
        if r["stops"]:
            assert r["duration_min"] == r["eta_by_order"][r["stops"][-1]]


def test_solve_skips_broken_van_and_is_deterministic():
    m = routing.cost_matrix(routing.build_graph())
    vans = _vehicles()
    vans[1]["status"] = "broken_down"
    a, b = routing.solve(m, vans, PARAMS), routing.solve(m, vans, PARAMS)
    assert {r["vehicle_id"] for r in a["routes"]} == {"V1", "V3"}
    assert [r["stops"] for r in a["routes"]] == [r["stops"] for r in b["routes"]]
    assert a["id"] != b["id"]
    assert a["unassigned"] == []


def test_solve_reports_unassigned_when_capacity_short():
    m = routing.cost_matrix(routing.build_graph())
    vans = [dict(_vehicles()[0], capacity_kg=10)]
    plan = routing.solve(m, vans, PARAMS)
    assert plan["unassigned"]
    assigned = [s for r in plan["routes"] for s in r["stops"]]
    assert sorted(assigned + plan["unassigned"]) == sorted(o["id"] for o in seed.ORDERS)


def test_solve_no_active_vans():
    m = routing.cost_matrix(routing.build_graph())
    vans = [dict(v, status="broken_down") for v in _vehicles()]
    plan = routing.solve(m, vans, PARAMS)
    assert plan["routes"] == [] and len(plan["unassigned"]) == 10


# ---------------------------------------------------------------- retime / flood
def test_retime_keeps_stops_and_recomputes_etas():
    g = routing.build_graph()
    base = routing.solve(routing.cost_matrix(g), _vehicles(), PARAMS)
    wet_m = routing.cost_matrix(routing.apply_disruption(g, FLOOD))
    slow = routing.retime(base, wet_m)
    assert slow is not base and slow["id"] == base["id"]
    assert [r["stops"] for r in slow["routes"]] == [r["stops"] for r in base["routes"]]
    assert sum(r["duration_min"] for r in slow["routes"]) > sum(r["duration_min"] for r in base["routes"])
    assert routing.retime(base, routing.cost_matrix(g)) == base


def test_flood_replan_changes_at_least_one_route_and_beats_do_nothing():
    g = routing.build_graph()
    base = routing.solve(routing.cost_matrix(g), _vehicles(), PARAMS)
    wet_m = routing.cost_matrix(routing.apply_disruption(g, FLOOD))
    nothing = routing.retime(base, wet_m)
    replan = routing.solve(wet_m, _vehicles(), PARAMS)
    assert [r["stops"] for r in replan["routes"]] != [r["stops"] for r in base["routes"]] or \
        [r["path"] for r in replan["routes"]] != [r["path"] for r in base["routes"]]
    assert sum(r["duration_min"] for r in replan["routes"]) <= sum(r["duration_min"] for r in nothing["routes"])
