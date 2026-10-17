"""Track C: ADK tool functions over the world state. Writes out/state.json for the map.

Contract: specs/WORKBOARD.md, section "Track C". Every tool returns
{"status": "ok"|"error", ...}; plans and orders never use the key "status" for
their own state. ADK builds each tool's schema from its type hints and docstring.
"""

import copy
import functools
import inspect
import json
import os
import time

from . import feedback, geo, places, routing, seed


def _replace(src: str, dst: str, tries: int = 20) -> None:
    """os.replace with retries: on Windows it fails while another process (the UI's web
    server polling the file) has dst open, so wait a moment and try again."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)

try:  # use Track A's routing once it's merged; until then, Track C's stand-in
    routing.build_graph()
    R = routing
except NotImplementedError:
    from . import _routing_fallback as R

STATE_PATH = os.path.join(feedback.OUT_DIR, "state.json")
AFFECTED_MIN = 10.0  # notify a customer when their arrival time moves by more than this
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
             policy=policy or feedback.load_policy(),
             # optional fields for map.html (System 3 contract); panels hide while None
             critic=None, kpis=None, baseline_plan=None, before_after=None, affected_orders=None,
             last_run=None, events=[])


def _write_state() -> None:
    os.makedirs(feedback.OUT_DIR, exist_ok=True)
    p = S["policy"]
    snapshot = {"depot": seed.DEPOT, "orders": seed.ORDERS, "vehicles": S["vehicles"], "plan": S["plan"],
                "disruption": S["disruption"], "notifications": S["notifications"],
                "policy": {"version": p["version"], "delay_factor": p["delay_factor"], "history": p["history"]},
                **{k: S[k] for k in ("critic", "kpis", "baseline_plan", "before_after", "affected_orders",
                                     "last_run", "events")}}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    _replace(tmp, STATE_PATH)


def _err(message: str) -> dict:
    return {"status": "error", "message": message}


def _event(tool: str, summary: str) -> None:
    """Technical one-liner for the dev panel; _traced attaches it to this call's event."""
    S["_log"] = summary


def _traced(fn):
    """Record every tool call for the dev panel: args, duration, technical log, plain summary."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        S.pop("_log", None)
        result = fn(*args, **kwargs)
        bound = sig.bind_partial(*args, **kwargs)
        S["events"].append({"ts": t0, "tool": fn.__name__, "args": dict(bound.arguments),
                            "ms": round((time.time() - t0) * 1000, 1), "ok": result.get("status") == "ok",
                            "summary": S.pop("_log", None) or result.get("message", ""),
                            "plain": result.get("summary") or result.get("message", "")})
        _write_state()
        return result
    return wrapper


def _dur(minutes: float) -> str:
    """150.4 -> "2 hours 30 minutes"; 45 -> "45 minutes"."""
    h, mm = divmod(int(round(abs(minutes))), 60)
    parts = [f"{h} hour{'s' if h != 1 else ''}"] if h else []
    if mm or not h:
        parts.append(f"{mm} minute{'s' if mm != 1 else ''}")
    return " ".join(parts)


def _who(order_ids: list, limit: int = 4) -> str:
    """["O6", "O10"] -> "Priya Venkat (Sholinganallur (OMR)) and Anitha Joseph (Thiruvanmiyur (OMR))"."""
    by_id = {o["id"]: o for o in seed.ORDERS}
    names = [f"{by_id[o]['customer']} ({by_id[o]['area']})" for o in order_ids if o in by_id]
    if len(names) > limit:
        names = names[:limit] + [f"{len(names) - limit} more"]
    if not names:
        return "nobody"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


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
@_traced
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
            "policy_version": S["policy"]["version"],
            "summary": (f"{len(seed.ORDERS)} deliveries today, "
                        f"{sum(v['status'] == 'active' for v in S['vehicles'])} vans working. "
                        + ("Routes are planned. " if S["plan"] else "No routes planned yet. ")
                        + (f"Current problem: {S['disruption']['label']}." if S["disruption"]
                           else "No problems reported."))}


@_traced
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
    k = feedback.kpis(S["plan"], seed.ORDERS, S["matrix"])
    S.update(critic=review, kpis=k, baseline_plan=None, before_after=None, affected_orders=None)
    _event("plan_routes", f"critic applied {S['plan']['params']['name']} ({k['overall']:.2f}), {k['total_min']} min")
    vans = sum(1 for r in S["plan"]["routes"] if r["stops"])
    summary = (f"Planned routes for all {len(_etas(S['plan']))} deliveries using {vans} vans, about "
               f"{_dur(k['total_min'])} of driving in total. "
               + ("Every delivery should arrive on time. " if not k["late_orders"]
                  else f"{k['late_orders']} deliveries may still be late. ")
               + "I tried 3 different ways of splitting the work and picked the best one.")
    return {"status": "ok", "verdict": "apply", "chosen": S["plan"]["params"]["name"],
            "candidates": _candidates(review["table"]), "routes": _summary(S["plan"]), "metrics": _metrics(k),
            "summary": summary}


_KIND_WORDS = {
    "flood": ("flood", "flooding", "waterlogging", "waterlogged", "rain", "inundation", "cyclone"),
    "traffic": ("traffic", "jam", "congestion", "convoy", "accident", "protest", "rally", "procession",
                "roadblock", "closure", "closed", "diversion", "vip", "construction", "metro work"),
    "breakdown": ("breakdown", "broke", "broken", "puncture", "engine", "van down", "vehicle down"),
}
_KIND_LABEL = {"flood": "Flooding", "traffic": "Traffic jam"}
_DEFAULT_RADIUS = {"flood": 5.0, "traffic": 2.5}


def _kind_of(text: str) -> str | None:
    t = (text or "").strip().lower()
    for kind, words in _KIND_WORDS.items():
        if t == kind or any(w in t for w in words):
            return kind
    return None


@_traced
def apply_disruption(kind: str, location: str = "", description: str = "", radius_km: float = 0.0,
                     vehicle_id: str = "", lat: float = 0.0, lng: float = 0.0) -> dict:
    """Register a disruption anywhere in Chennai so the road network (or fleet)
    reflects it. Uses the learned delay factor for that kind as the predicted slowdown.

    Args:
        kind: "flood" (flooding, waterlogging, heavy rain), "traffic" (jam, VIP or CM
            convoy, accident, protest, road closure, diversion) or "breakdown" (a van
            is out of service).
        location: Where it is, as the user said it: a Chennai area or road such as
            "Tambaram", "Velachery", "OMR", "GST Road", "Kathipara". Not needed for
            a breakdown.
        description: Short cause in the user's words, e.g. "CM convoy". Optional.
        radius_km: Size of the affected zone in km. 0 = default (flood 5, traffic 2.5).
        vehicle_id: For a breakdown: the van that is down, "V1", "V2" or "V3".
        lat: Only if the location is not a known area: its latitude (Chennai, 12.75-13.25).
        lng: Only if the location is not a known area: its longitude (Chennai, 79.95-80.35).

    Returns:
        {"status": "ok", "label", "kind", "location", "center", "radius_km",
        "predicted_multiplier", "orders_in_zone"}.
    """
    k = _kind_of(kind) or _kind_of(description)
    if not k:
        return _err(f"Unknown disruption kind '{kind}'. Valid: flood, traffic, breakdown.")
    if not S["plan"]:
        return _err("No current plan. Call plan_routes first.")
    cause = (description or "").strip()
    multiplier = S["policy"]["delay_factor"].get(k, 1.0)

    if k == "breakdown":
        vid = (vehicle_id or seed.SCENARIOS["breakdown"]["vehicle_id"]).strip().upper()
        van = next((v for v in S["vehicles"] if v["id"] == vid), None)
        if not van:
            return _err(f"Unknown van '{vehicle_id}'. Valid: {', '.join(v['id'] for v in S['vehicles'])}.")
        if van["status"] == "broken_down":
            return _err(f"Van {vid} is already out of service.")
        d = {"kind": "breakdown", "label": f"Van {vid} broke down" + (f" ({cause})" if cause else ""),
             "center": None, "radius_km": 0.0, "vehicle_id": vid, "location": None, "multiplier": multiplier}
        van["status"] = "broken_down"
        zone = next((r["stops"] for r in S["plan"]["routes"] if r["vehicle_id"] == vid), [])
    else:
        if location.strip():
            found = places.resolve(location)
            if found:
                place, center = found
            elif lat and lng and places.in_bounds(lat, lng):
                place, center = f"{location.strip()} (approx.)", (lat, lng)
            else:
                return _err(f"Unknown location '{location}'. Use a Chennai area such as "
                            f"{', '.join(list(places.PLACES)[:12])}, ..., or pass lat and lng inside Chennai.")
        elif lat and lng and places.in_bounds(lat, lng):
            place, center = f"{lat:.3f}, {lng:.3f}", (lat, lng)
        else:  # no location given: the preset scenario for this kind
            preset = seed.SCENARIOS[k]
            place, center = preset["label"].split(" on ")[-1].split(" in ")[-1], tuple(preset["center"])
        r_km = min(8.0, max(0.5, radius_km or _DEFAULT_RADIUS[k]))
        label = f"{_KIND_LABEL[k]}{f' ({cause})' if cause else ''} near {place}"
        d = {"kind": k, "label": label, "center": [center[0], center[1]], "radius_km": r_km,
             "vehicle_id": None, "location": place, "multiplier": multiplier}
        S["matrix"] = R.cost_matrix(R.apply_disruption(S["base_graph"], d))
        zone = [o["id"] for o in seed.ORDERS if geo.edge_disrupted(o["id"], o["id"], d)]

    S["disruption"] = d
    S["pre_disruption_plan"] = S["plan"]
    _event("apply_disruption", f"{d['label']}, predicted x{d['multiplier']}")
    if k == "breakdown":
        summary = f"Noted: {d['label']}. Its {len(zone)} deliveries need another van."
    else:
        summary = (f"Noted: {d['label']}. I'm assuming roads in that area are about {multiplier:.1f} times "
                   f"slower than normal. "
                   + (f"Deliveries inside the area: {_who(zone)}." if zone else
                      "None of today's deliveries are inside the area, but vans may drive through it."))
    return {"status": "ok", "label": d["label"], "kind": d["kind"], "location": d["location"],
            "center": d["center"], "radius_km": d["radius_km"],
            "predicted_multiplier": d["multiplier"], "orders_in_zone": zone, "summary": summary}


@_traced
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
    S.update(critic=review, kpis=after_k, baseline_plan=do_nothing,
             before_after={"before": _metrics(base_k), "after": _metrics(after_k)},
             affected_orders=[a["order_id"] for a in affected])
    _event("replan_routes", f"{review['verdict']}: {base_k['total_min']} -> {after_k['total_min']} min, "
                            f"{len(affected)} affected")
    dis = S["disruption"]
    detours = (sum(1 for r in S["plan"]["routes"] for x, y in zip(r["path"], r["path"][1:])
                   if geo.leg_choice(x, y, dis)["variant"] != 0) if dis.get("center") else 0)
    saved = base_k["total_min"] - after_k["total_min"]
    late_avoided = base_k["late_orders"] - after_k["late_orders"]
    if review["verdict"] == "hold":
        summary = "None of the new plans beat keeping the current routes, so I kept them."
    elif saved >= 1:
        summary = f"Found a better plan: about {_dur(saved)} less driving in total than sticking to the old routes."
    elif saved <= -1:
        summary = ("Switched to a plan that keeps deliveries on time and spreads the work more evenly, "
                   f"at the cost of about {_dur(-saved)} more driving.")
    else:
        summary = "There was no faster way around it, so the routes stay much the same."
    if detours:
        summary += f" {detours} stretch{'es' if detours != 1 else ''} of road swapped for a detour around the area."
    if late_avoided > 0:
        summary += f" {late_avoided} deliveries that would have been late are now on time."
    elif after_k["late_orders"]:
        summary += f" {after_k['late_orders']} deliveries will still be late."
    if affected:
        summary += (f" {len(affected)} customers' arrival times move by more than {AFFECTED_MIN:.0f} minutes, "
                    f"so they should be told: {_who([a['order_id'] for a in affected])}.")
    else:
        summary += " Nobody's arrival time changes much, so no messages are needed."
    return {"status": "ok", "verdict": review["verdict"], "chosen": S["plan"].get("params", {}).get("name", "do-nothing"),
            "detours": detours, "summary": summary,
            "do_nothing": _metrics(base_k), "after": _metrics(after_k),
            "time_saved_min": round(base_k["total_min"] - after_k["total_min"], 1),
            "late_orders_avoided": base_k["late_orders"] - after_k["late_orders"],
            "candidates": _candidates(review["table"]), "routes": _summary(S["plan"]),
            "affected_orders": affected}


@_traced
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
    _event("notify_customers", f"{sent} sent, {len(rejected)} rejected")
    summary = f"Sent {sent} message{'s' if sent != 1 else ''} to customers."
    if rejected:
        summary += f" {len(rejected)} could not be sent: " + "; ".join(f"{r['order_id']}: {r['reason']}" for r in rejected)
    return {"status": "ok", "sent": sent, "rejected": rejected, "summary": summary}


@_traced
def run_day() -> dict:
    """Run the simulated delivery day on the current plan, compare predicted and
    realized ETAs, and let the learner update the policy (delay factors). Call after
    customers are notified.

    Returns:
        {"status": "ok", "eta_error" (fraction), "eta_error_pct" (e.g. "15.8%"),
        "delay_factor_change": {kind: [old, new]},
        "rolled_back", "policy_version", "history"}.
    """
    if not S["plan"]:
        return _err("No current plan. Call plan_routes first.")
    d = S["disruption"]
    if not d:
        return _err("No disruption to learn from. run_day only runs after apply_disruption and replan_routes.")
    kind = d["kind"]
    run = len(S["policy"]["history"]) + 1
    sim = feedback.simulate_day(S["plan"], S["base_graph"], d, run)
    overall = feedback.kpis(S["plan"], seed.ORDERS, S["matrix"])["overall"]
    # Score the DELAY prediction (how much later than normal), not the whole trip time:
    # a flood that covers 2 km of a 20 km trip would otherwise look almost perfectly predicted.
    touched = sim["disrupted_orders"] or list(sim["realized_eta"])
    base, plan_eta = sim["base_eta"], _etas(S["plan"])
    predicted = {o: round(plan_eta[o] - base[o], 2) for o in touched if o in plan_eta and o in base}
    realized = {o: round(sim["realized_eta"][o] - base[o], 2) for o in touched if o in base}
    out = feedback.learn(S["policy"], kind, predicted, realized, sim["observed_ratio"], overall)
    S["policy"] = out["policy"]
    S["last_run"] = {"eta_error": out["eta_error"], "changes": out["changes"]}
    feedback.save_policy(S["policy"])
    _event("run_day", f"delay error {out['eta_error']:.1%}, {kind} x{out['changes']['delay_factor'][kind][0]} "
                      f"-> x{out['changes']['delay_factor'][kind][1]}")
    old, new = out["changes"]["delay_factor"][kind]
    obs = sim["observed_ratio"]
    if kind == "breakdown":
        summary = "The day is done. A breakdown doesn't change road speeds, so there was nothing new to learn about delays."
    elif not obs:
        summary = "The day is done. None of the vans' roads went through the affected area, so there was nothing new to learn."
    else:
        word = {"flood": "the flooding", "traffic": "the traffic"}.get(kind, kind)
        summary = (f"The day is done. On the affected roads, {word} actually slowed vans about {obs:.1f} times "
                   f"(I had assumed {old:.1f}). Next time I'll assume {new:.1f} times. "
                   f"My estimate of how late deliveries would be was off by {out['eta_error']:.0%}.")
        if out["changes"]["rolled_back"]:
            summary += " My estimates have been getting worse, so I went back to my previous setting."
    return {"status": "ok", "summary": summary,
            "eta_error": out["eta_error"], "eta_error_pct": f"{out['eta_error']:.1%}",
            "observed_slowdown": obs,
            "delay_factor_change": out["changes"]["delay_factor"],
            "rolled_back": out["changes"]["rolled_back"], "policy_version": S["policy"]["version"],
            "history": [{k: h[k] for k in ("run", "kind", "eta_error", "delay_factor")} for h in S["policy"]["history"]]}


@_traced
def get_policy() -> dict:
    """Get the learned policy: delay factors per disruption kind, candidate
    strategies, and the run history. Read-only.

    Returns:
        {"status": "ok", "version", "delay_factor", "candidates", "history"}.
    """
    p = S["policy"]
    df = p["delay_factor"]
    summary = (f"Right now I expect floods to slow vans about {df['flood']:.1f} times and traffic jams about "
               f"{df['traffic']:.1f} times. I've learned from {len(p['history'])} day"
               f"{'s' if len(p['history']) != 1 else ''} so far.")
    return {"status": "ok", "version": p["version"], "delay_factor": df,
            "candidates": p["candidates"], "history": p["history"], "summary": summary}


@_traced
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
    return {"status": "ok", "policy_version": S["policy"]["version"],
            "summary": "Started a fresh day: all orders reloaded and every van back at the depot. "
                       + ("I also forgot everything I had learned." if hard else "I kept what I've learned.")}


_fresh_state()
