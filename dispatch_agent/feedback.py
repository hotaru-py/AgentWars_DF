"""Track B: KPIs, critic, day simulator, learner, policy store.

Contract: specs/WORKBOARD.md, section "Track B". Deterministic code, no LLM calls.
The critic is code, not an LLM, so it can't be talked into approving a bad plan,
and the learner is the only thing that writes the policy.
"""

import copy
import json
import os
import random

from . import geo, seed

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")
POLICY_PATH = os.path.join(OUT_DIR, "policy.json")

TRUTH = {"flood": 5.0, "traffic": 3.0, "breakdown": 1.0}  # hidden from the agent
NOISE = 0.10
SEED = 42

KPI_WEIGHTS = {"on_time": 0.30, "efficiency": 0.20, "cycle": 0.15, "balance": 0.15, "resilience": 0.20}
STRESS = 1.2           # every leg x1.2 for the resilience KPI
MAX_STEP = 0.25        # learner may move a factor at most +-25% per run

DEFAULT_POLICY = {
    "version": 1,
    "delay_factor": {"flood": 4.0, "traffic": 2.5, "breakdown": 1.0},
    "candidates": [
        {"name": "balanced", "late_penalty": 1.0, "balance_weight": 0.5},
        {"name": "deadline-first", "late_penalty": 4.0, "balance_weight": 0.1},
        {"name": "compact", "late_penalty": 0.5, "balance_weight": 0.0},
    ],
    "history": [],
}


# ---------------------------------------------------------------- KPIs
def _mst_km(nodes: list[str], matrix: dict) -> float:
    """Prim's MST weight (km) over the nodes: a lower bound on any plan's distance."""
    if len(nodes) < 2:
        return 0.0
    seen, total = {nodes[0]}, 0.0
    best = {n: matrix[nodes[0]][n]["km"] for n in nodes[1:]}
    while best:
        n = min(best, key=best.get)
        total += best.pop(n)
        seen.add(n)
        for m in best:
            best[m] = min(best[m], matrix[n][m]["km"])
    return total


def _on_time(etas: dict, orders: list[dict], scale: float = 1.0) -> float:
    if not orders:
        return 1.0
    hits = sum(1 for o in orders if o["id"] in etas and etas[o["id"]] * scale <= o["deadline_min"])
    return hits / len(orders)


def kpis(plan: dict, orders: list[dict], matrix: dict) -> dict:
    """Score a plan on 5 KPIs (each 0..1, higher is better) plus a weighted overall."""
    etas = {oid: eta for r in plan["routes"] for oid, eta in r["eta_by_order"].items()}
    plan_km = sum(r["distance_km"] for r in plan["routes"])
    route_min = [r["duration_min"] for r in plan["routes"]] or [0.0]
    depot = seed.DEPOT["id"]

    mst = _mst_km([depot] + [o["id"] for o in orders], matrix)
    direct = [matrix[depot][o["id"]]["min"] for o in orders]
    mean_eta = sum(etas.get(o["id"], 0.0) for o in orders) / len(orders) if orders else 0.0

    scores = {
        "on_time": _on_time(etas, orders),
        "efficiency": min(1.0, mst / plan_km) if plan_km else 0.0,
        "cycle": min(1.0, (sum(direct) / len(direct)) / mean_eta) if mean_eta else 0.0,
        "balance": 1 - (max(route_min) - min(route_min)) / max(route_min) if max(route_min) else 1.0,
        "resilience": _on_time(etas, orders, STRESS),
    }
    scores = {k: round(v, 3) for k, v in scores.items()}
    scores["overall"] = round(sum(KPI_WEIGHTS[k] * v for k, v in scores.items()), 3)
    scores["late_orders"] = sum(1 for o in orders if etas.get(o["id"], float("inf")) > o["deadline_min"])
    scores["total_min"] = round(sum(route_min), 1)
    scores["total_km"] = round(plan_km, 1)
    return scores


# ---------------------------------------------------------------- critic
def critic(plans: list[dict], orders: list[dict], matrix: dict, baseline: dict | None = None) -> dict:
    """Score candidate plans and pick one. Never approves a plan that drops an order,
    and holds (keeps the current plan) if no candidate beats the baseline."""
    table, best = [], None
    for p in plans:
        valid = not p.get("unassigned")
        row = {"plan_id": p["id"], "name": p.get("params", {}).get("name", p["id"]),
               "valid": valid, **kpis(p, orders, matrix)}
        table.append(row)
        if valid and (best is None or row["overall"] > best["overall"]):
            best = row
    if best is None or (baseline is not None and best["overall"] < baseline["overall"]):
        return {"best_id": None, "verdict": "hold", "table": table}
    return {"best_id": best["plan_id"], "verdict": "apply", "table": table}


# ---------------------------------------------------------------- simulator
def _true_multiplier(kind: str, run: int) -> float:
    noise = random.Random(SEED + run).uniform(1 - NOISE, 1 + NOISE)
    return TRUTH.get(kind, 1.0) * noise


def simulate_day(plan: dict, base_graph: dict, disruption: dict | None, run: int) -> dict:
    """Replay the plan in the 'real world': legs inside the disruption circle run at
    the hidden TRUE slowdown. Returns realized ETAs and the slowdown a fleet would
    observe on the disrupted legs (never TRUTH itself)."""
    mult = _true_multiplier(disruption["kind"], run) if disruption else 1.0
    realized, base_dis, real_dis = {}, 0.0, 0.0
    for r in plan["routes"]:
        stops, ptr, t = r["stops"], 0, 0.0
        path = r["path"]
        for a, b in zip(path, path[1:]):
            edge = base_graph.get(a, {}).get(b)
            leg = edge["min"] if edge else geo.km_to_min(geo.road_km(a, b))
            if geo.edge_disrupted(a, b, disruption):
                base_dis += leg
                leg *= mult
                real_dis += leg
            t += leg
            if ptr < len(stops) and b == stops[ptr]:
                realized[b] = round(t, 1)
                ptr += 1
    return {"realized_eta": realized,
            "observed_ratio": round(real_dis / base_dis, 3) if base_dis else None}


# ---------------------------------------------------------------- learner
def learn(policy: dict, kind: str, predicted_eta: dict, realized_eta: dict,
          observed_ratio: float | None, overall: float) -> dict:
    """Update the policy from one run: nudge delay_factor[kind] halfway toward the
    observed slowdown (capped at +-25%), and roll back if the overall score has
    dropped two runs in a row."""
    common = [o for o in realized_eta if o in predicted_eta]
    mean_real = sum(realized_eta[o] for o in common) / len(common) if common else 0.0
    eta_error = (sum(abs(predicted_eta[o] - realized_eta[o]) for o in common) / len(common) / mean_real
                 if mean_real else 0.0)

    new = copy.deepcopy(policy)
    new.pop("previous", None)
    history = new.setdefault("history", [])
    old = policy["delay_factor"].get(kind, 1.0)

    overalls = [h["overall"] for h in history] + [overall]
    rolled_back = (len(overalls) >= 3 and overalls[-1] < overalls[-2] < overalls[-3]
                   and "previous" in policy)
    if rolled_back:
        new["delay_factor"] = copy.deepcopy(policy["previous"]["delay_factor"])
    elif observed_ratio:
        target = 0.5 * old + 0.5 * observed_ratio
        new["delay_factor"][kind] = round(min(old * (1 + MAX_STEP), max(old * (1 - MAX_STEP), target)), 3)

    new["previous"] = {"delay_factor": copy.deepcopy(policy["delay_factor"])}
    new["version"] = policy.get("version", 1) + 1
    history.append({"run": len(history) + 1, "kind": kind, "eta_error": round(eta_error, 3),
                    "overall": overall, "delay_factor": new["delay_factor"].get(kind)})
    return {"policy": new,
            "changes": {"delay_factor": {kind: [old, new["delay_factor"].get(kind)]},
                        "rolled_back": rolled_back},
            "eta_error": round(eta_error, 3)}


# ---------------------------------------------------------------- policy store
def load_policy() -> dict:
    try:
        with open(POLICY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT_POLICY)


def save_policy(policy: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = POLICY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(policy, f, indent=2)
    os.replace(tmp, POLICY_PATH)
