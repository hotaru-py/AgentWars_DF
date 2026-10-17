"""Track C: ADK tool functions over the world state. Writes out/state.json for the map.

Contract: specs/WORKBOARD.md, section "Track C". Every tool returns
{"status": "ok"|"error", ...}; plans and orders never use the key "status" for
their own state. ADK builds each tool's schema from its type hints and docstring.
"""

import copy
import json
import os

from . import feedback, geo, routing, seed

try:  # use Track A's routing once it's merged; until then, Track C's stand-in
    routing.build_graph()
    R = routing
except NotImplementedError:
    from . import _routing_fallback as R

STATE_PATH = os.path.join(feedback.OUT_DIR, "state.json")
AFFECTED_MIN = 5.0
MAX_WORDS = 40
SHIFT_START_MIN = 9 * 60  # 9:00 AM; ETAs are minutes from shift start


def _clock(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    h, m = divmod(int(round(SHIFT_START_MIN + minutes)), 60)
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"

S: dict = {}


def _fresh_state(policy: dict | None = None) -> None:
    graph = R.build_graph()
    S.clear()
    S.update(base_graph=graph, matrix=R.cost_matrix(graph), vehicles=copy.deepcopy(seed.VEHICLES),
             disruption=None, plan=None, pre_disruption_plan=None, notifications=[],
             policy=policy or feedback.load_policy())


def _write_state() -> None:
    os.makedirs(feedback.OUT_DIR, exist_ok=True)
    p = S["policy"]
    snapshot = {"depot": seed.DEPOT, "orders": seed.ORDERS, "vehicles": S["vehicles"], "plan": S["plan"],
                "disruption": S["disruption"], "notifications": S["notifications"],
                "policy": {"version": p["version"], "delay_factor": p["delay_factor"], "history": p["history"]}}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _err(message: str) -> dict:
    return {"status": "error", "message": message}


def _summary(plan: dict) -> list[dict]:
    return [{"vehicle": r["vehicle_id"], "stops": r["stops"], "minutes": r["duration_min"], "km": r["distance_km"]}
            for r in plan["routes"] if r["stops"]]


def _metrics(k: dict) -> dict:
    return {key: k[key] for key in ("total_min", "total_km", "late_orders", "on_time", "overall")}


def _candidates(table: list[dict]) -> list[dict]:
    return [{"name": row["name"], "overall": row["overall"], "on_time": row["on_time"],
             "total_min": row["total_min"], "late_orders": row["late_orders"], "valid": row["valid"]}
            for row in table]


def _etas(plan: dict) -> dict:
    return {o: eta for r in plan["routes"] for o, eta in r["eta_by_order"].items()}


# ---------------------------------------------------------------- tools
def get_world_state() -> dict:
    """Get the current situation: orders with deadlines, vans, the current route plan,
    any active disruption, and the learned policy version.

    Returns:
        {"status": "ok", "orders", "vehicles", "plan", "disruption", "policy_version"}.
    """
    return {"status": "ok",
            "orders": [{k: o[k] for k in ("id", "customer", "area", "deadline_min", "priority")} for o in seed.ORDERS],
            "vehicles": S["vehicles"],
            "plan": _summary(S["plan"]) if S["plan"] else None,
            "disruption": S["disruption"]["label"] if S["disruption"] else None,
            "policy_version": S["policy"]["version"]}


def plan_routes() -> dict:
    """Plan today's delivery routes from scratch. Builds 3 candidate plans with
    different strategies, has the critic score each on 5 KPIs, and applies the best.

    Returns:
        {"status": "ok", "verdict", "chosen", "candidates": [scored rows],
        "routes": [per-van stops, minutes, km], "metrics"}.
    """
    plans = [R.solve(S["matrix"], S["vehicles"], c) for c in S["policy"]["candidates"]]
    review = feedback.critic(plans, seed.ORDERS, S["matrix"])
    if review["verdict"] != "apply":
        return _err("No valid plan: every candidate left orders unassigned.")
    S["plan"] = next(p for p in plans if p["id"] == review["best_id"])
    _write_state()
    k = feedback.kpis(S["plan"], seed.ORDERS, S["matrix"])
    return {"status": "ok", "verdict": "apply", "chosen": S["plan"]["params"]["name"],
            "candidates": _candidates(review["table"]), "routes": _summary(S["plan"]), "metrics": _metrics(k)}


def apply_disruption(scenario: str) -> dict:
    """Register a disruption so the road network (or fleet) reflects it. Uses the
    learned delay factor from the policy as the predicted slowdown.

    Args:
        scenario: One of "flood" (flooding on OMR), "traffic" (jam in T. Nagar)
            or "breakdown" (van V2 broke down).

    Returns:
        {"status": "ok", "label", "kind", "predicted_multiplier", "orders_in_zone"}.
    """
    key = (scenario or "").strip().lower()
    if key not in seed.SCENARIOS:
        return _err(f"Unknown scenario '{scenario}'. Valid: {', '.join(seed.SCENARIOS)}.")
    if not S["plan"]:
        return _err("No current plan. Call plan_routes first.")
    d = dict(seed.SCENARIOS[key], multiplier=S["policy"]["delay_factor"].get(key, 1.0))
    S["disruption"] = d
    S["pre_disruption_plan"] = S["plan"]
    if d["kind"] == "breakdown":
        for v in S["vehicles"]:
            if v["id"] == d["vehicle_id"]:
                v["status"] = "broken_down"
        zone = next((r["stops"] for r in S["plan"]["routes"] if r["vehicle_id"] == d["vehicle_id"]), [])
    else:
        S["matrix"] = R.cost_matrix(R.apply_disruption(S["base_graph"], d))
        zone = [o["id"] for o in seed.ORDERS if geo.edge_disrupted(o["id"], o["id"], d)]
    _write_state()
    return {"status": "ok", "label": d["label"], "kind": d["kind"],
            "predicted_multiplier": d["multiplier"], "orders_in_zone": zone}


def replan_routes() -> dict:
    """Re-optimize the routes for the active disruption. Builds 3 candidates on the
    disrupted network; the critic compares them with doing nothing (keeping the old
    routes under the disruption) and applies the best, or holds the current plan.

    Returns:
        {"status": "ok", "verdict", "chosen", "do_nothing", "after", "time_saved_min",
        "late_orders_avoided", "candidates", "routes",
        "affected_orders": [{"order_id", "customer", "area", "old_eta", "new_eta",
        "new_eta_clock", "deadline_min"}]}. ETAs are minutes from the 9:00 AM shift start.
    """
    if not S["disruption"] or not S["pre_disruption_plan"]:
        return _err("No active disruption. Call apply_disruption first.")
    old = S["pre_disruption_plan"]
    do_nothing = R.retime(old, S["matrix"])
    broken = {v["id"] for v in S["vehicles"] if v["status"] == "broken_down"}
    stranded = [o for r in do_nothing["routes"] if r["vehicle_id"] in broken for o in r["stops"]]
    do_nothing["routes"] = [r for r in do_nothing["routes"] if r["vehicle_id"] not in broken]
    do_nothing["unassigned"] = stranded
    base_k = feedback.kpis(do_nothing, seed.ORDERS, S["matrix"])

    plans = [R.solve(S["matrix"], S["vehicles"], c) for c in S["policy"]["candidates"]]
    review = feedback.critic(plans, seed.ORDERS, S["matrix"], baseline=base_k)
    S["plan"] = next(p for p in plans if p["id"] == review["best_id"]) if review["verdict"] == "apply" else do_nothing
    after_k = feedback.kpis(S["plan"], seed.ORDERS, S["matrix"])

    before_eta, after_eta = _etas(old), _etas(S["plan"])
    before_van = {o: r["vehicle_id"] for r in old["routes"] for o in r["stops"]}
    after_van = {o: r["vehicle_id"] for r in S["plan"]["routes"] for o in r["stops"]}
    affected = [{"order_id": o["id"], "customer": o["customer"], "area": o["area"],
                 "old_eta": before_eta.get(o["id"]), "new_eta": after_eta.get(o["id"]),
                 "new_eta_clock": _clock(after_eta.get(o["id"])), "deadline_min": o["deadline_min"]}
                for o in seed.ORDERS
                if o["id"] in after_eta and (abs(after_eta[o["id"]] - before_eta.get(o["id"], 0)) > AFFECTED_MIN
                                             or before_van.get(o["id"]) != after_van.get(o["id"]))]
    _write_state()
    return {"status": "ok", "verdict": review["verdict"], "chosen": S["plan"].get("params", {}).get("name", "do-nothing"),
            "do_nothing": _metrics(base_k), "after": _metrics(after_k),
            "time_saved_min": round(base_k["total_min"] - after_k["total_min"], 1),
            "late_orders_avoided": base_k["late_orders"] - after_k["late_orders"],
            "candidates": _candidates(review["table"]), "routes": _summary(S["plan"]),
            "affected_orders": affected}


def notify_customers(order_ids: list[str], messages: list[str]) -> dict:
    """Send ETA update messages to affected customers (mock send). Call once with all
    messages. order_ids[i] receives messages[i].

    Args:
        order_ids: Order ids to notify, e.g. ["O6", "O10"].
        messages: One message per order id, same order. Each must be under 40 words,
            polite, and include the new ETA.

    Returns:
        {"status": "ok", "sent": n, "rejected": [{"order_id", "reason"}]}.
    """
    if len(order_ids) != len(messages):
        return _err("order_ids and messages must have the same length.")
    known = {o["id"]: o for o in seed.ORDERS}
    sent, rejected = 0, []
    for oid, msg in zip(order_ids, messages):
        oid = oid.strip().upper()
        if oid not in known:
            rejected.append({"order_id": oid, "reason": f"unknown order; valid: {', '.join(known)}"})
        elif len(msg.split()) >= MAX_WORDS:
            rejected.append({"order_id": oid, "reason": f"message has {len(msg.split())} words; must be under {MAX_WORDS}"})
        else:
            S["notifications"].append({"order_id": oid, "customer": known[oid]["customer"],
                                       "phone": known[oid]["phone"], "message": msg, "sent": True})
            sent += 1
    _write_state()
    return {"status": "ok", "sent": sent, "rejected": rejected}


def run_day() -> dict:
    """Run the simulated delivery day on the current plan, compare predicted and
    realized ETAs, and let the learner update the policy (delay factors). Call after
    customers are notified.

    Returns:
        {"status": "ok", "eta_error", "delay_factor_change": {kind: [old, new]},
        "rolled_back", "policy_version", "history"}.
    """
    if not S["plan"]:
        return _err("No current plan. Call plan_routes first.")
    d = S["disruption"]
    kind = d["kind"] if d else "none"
    run = len(S["policy"]["history"]) + 1
    sim = feedback.simulate_day(S["plan"], S["base_graph"], d, run)
    overall = feedback.kpis(S["plan"], seed.ORDERS, S["matrix"])["overall"]
    touched = sim["disrupted_orders"] or list(sim["realized_eta"])
    predicted = {o: e for o, e in _etas(S["plan"]).items() if o in touched}
    realized = {o: e for o, e in sim["realized_eta"].items() if o in touched}
    out = feedback.learn(S["policy"], kind, predicted, realized, sim["observed_ratio"], overall)
    S["policy"] = out["policy"]
    feedback.save_policy(S["policy"])
    _write_state()
    return {"status": "ok", "eta_error": out["eta_error"], "delay_factor_change": out["changes"]["delay_factor"],
            "rolled_back": out["changes"]["rolled_back"], "policy_version": S["policy"]["version"],
            "history": [{k: h[k] for k in ("run", "kind", "eta_error", "delay_factor")} for h in S["policy"]["history"]]}


def get_policy() -> dict:
    """Get the learned policy: delay factors per disruption kind, candidate
    strategies, and the run history. Read-only.

    Returns:
        {"status": "ok", "version", "delay_factor", "candidates", "history"}.
    """
    p = S["policy"]
    return {"status": "ok", "version": p["version"], "delay_factor": p["delay_factor"],
            "candidates": p["candidates"], "history": p["history"]}


def reset(hard: bool = False) -> dict:
    """Reset the day: reload orders and vans, clear disruptions, routes and
    notifications. The learned policy is kept unless hard is True.

    Args:
        hard: Also reset the learned policy to defaults.

    Returns:
        {"status": "ok", "policy_version"}.
    """
    policy = None
    if hard:
        policy = copy.deepcopy(feedback.DEFAULT_POLICY)
        feedback.save_policy(policy)
    _fresh_state(policy)
    _write_state()
    return {"status": "ok", "policy_version": S["policy"]["version"]}


_fresh_state()
