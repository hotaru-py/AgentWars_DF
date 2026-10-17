"""Track B tests: hand-built plans/matrices/graphs only, no dependency on routing.py."""

import copy

from dispatch_agent import feedback, seed

ORDERS = [o for o in seed.ORDERS if o["id"] in ("O1", "O6")]  # O1 deadline 90, O6 deadline 140
NODES = ["DEPOT", "O1", "O6"]
KM = {("DEPOT", "O1"): 6.0, ("DEPOT", "O6"): 15.0, ("O1", "O6"): 20.0}


def _matrix():
    m = {a: {} for a in NODES}
    for (a, b), km in KM.items():
        m[a][b] = m[b][a] = {"km": km, "min": km / 25 * 60, "path": [a, b]}
    for a in NODES:
        m[a][a] = {"km": 0.0, "min": 0.0, "path": [a]}
    return m


def _plan(pid, eta_o1, eta_o6, unassigned=()):
    return {"id": pid, "params": {"name": pid},
            "routes": [
                {"vehicle_id": "V1", "stops": ["O1"], "path": ["DEPOT", "O1"],
                 "eta_by_order": {"O1": eta_o1}, "duration_min": eta_o1, "distance_km": 6.0},
                {"vehicle_id": "V2", "stops": ["O6"], "path": ["DEPOT", "O6"],
                 "eta_by_order": {"O6": eta_o6}, "duration_min": eta_o6, "distance_km": 15.0}],
            "unassigned": list(unassigned)}


def _graph():
    return {a: {b: {"min": v["min"], "km": v["km"]} for b, v in row.items() if a != b}
            for a, row in _matrix().items()}


# ---------------------------------------------------------------- kpis
def test_kpis_all_on_time_direct_routes():
    k = feedback.kpis(_plan("p", 14.4, 36.0), ORDERS, _matrix())
    assert k["on_time"] == 1.0 and k["late_orders"] == 0
    assert k["efficiency"] == 1.0          # direct legs == MST
    assert k["cycle"] == 1.0               # ETAs equal direct times
    assert 0 <= k["balance"] < 1
    assert 0 < k["overall"] <= 1
    assert k["total_km"] == 21.0


def test_kpis_late_order_and_resilience():
    k = feedback.kpis(_plan("p", 80.0, 150.0), ORDERS, _matrix())
    assert k["on_time"] == 0.5 and k["late_orders"] == 1
    assert k["resilience"] == 0.0          # 80 * 1.2 = 96 > 90


# ---------------------------------------------------------------- critic
def test_critic_picks_best_and_skips_invalid():
    good, worse = _plan("good", 14.4, 36.0), _plan("worse", 80.0, 150.0)
    broken = _plan("broken", 1.0, 1.0, unassigned=["O6"])
    r = feedback.critic([worse, broken, good], ORDERS, _matrix())
    assert r["verdict"] == "apply" and r["best_id"] == "good"
    assert len(r["table"]) == 3
    assert next(row for row in r["table"] if row["plan_id"] == "broken")["valid"] is False


def test_critic_holds_when_nothing_beats_baseline():
    baseline = feedback.kpis(_plan("now", 14.4, 36.0), ORDERS, _matrix())
    r = feedback.critic([_plan("worse", 80.0, 150.0)], ORDERS, _matrix(), baseline)
    assert r["verdict"] == "hold" and r["best_id"] is None


# ---------------------------------------------------------------- simulator
def test_simulate_day_slows_only_disrupted_legs():
    flood = dict(seed.SCENARIOS["flood"], multiplier=4.0)
    plan = _plan("p", 14.4, 36.0)
    r = feedback.simulate_day(plan, _graph(), flood, run=1)
    assert r["realized_eta"]["O1"] == 14.4                     # T. Nagar road is outside the flood
    assert r["realized_eta"]["O6"] > 36.0                      # the in-zone part of the OMR road is slower
    assert 4.8 <= r["observed_ratio"] <= 5.2                   # measured on the in-zone part: true x5 +-3%


def test_simulate_day_is_deterministic_per_run_and_hides_truth():
    flood = dict(seed.SCENARIOS["flood"], multiplier=4.0)
    a = feedback.simulate_day(_plan("p", 14.4, 36.0), _graph(), flood, run=2)
    b = feedback.simulate_day(_plan("p", 14.4, 36.0), _graph(), flood, run=2)
    assert a == b
    assert set(a) == {"realized_eta", "base_eta", "disrupted_orders", "observed_ratio"}
    assert a["disrupted_orders"] == ["O6"]


def test_simulate_day_without_disruption():
    r = feedback.simulate_day(_plan("p", 14.4, 36.0), _graph(), None, run=1)
    assert r["observed_ratio"] is None and r["realized_eta"] == {"O1": 14.4, "O6": 36.0}


# ---------------------------------------------------------------- learner
def test_learn_moves_factor_toward_observation_three_runs():
    policy = copy.deepcopy(feedback.DEFAULT_POLICY)
    factors, errors = [], []
    for overall in (0.70, 0.72, 0.74):
        f = policy["delay_factor"]["flood"]
        predicted = {"O6": 36.0 * f}
        realized = {"O6": 36.0 * 5.0}
        out = feedback.learn(policy, "flood", predicted, realized, 5.0, overall)
        policy = out["policy"]
        factors.append(policy["delay_factor"]["flood"])
        errors.append(out["eta_error"])
    assert factors == [4.5, 4.75, 4.875]
    assert errors[0] > errors[1] > errors[2]
    assert policy["version"] == 4 and len(policy["history"]) == 3


def test_learn_step_is_capped():
    out = feedback.learn(copy.deepcopy(feedback.DEFAULT_POLICY), "flood", {"O6": 1}, {"O6": 1}, 50.0, 0.7)
    assert out["policy"]["delay_factor"]["flood"] == 5.0       # 4.0 * 1.25


def test_learn_rolls_back_after_eta_error_rises_twice():
    policy = copy.deepcopy(feedback.DEFAULT_POLICY)
    for err_pred in (101, 110):                                 # ETA error 1% then 10%
        policy = feedback.learn(policy, "flood", {"O6": err_pred}, {"O6": 100}, 5.0, 0.7)["policy"]
    before_last = copy.deepcopy(policy["previous"]["delay_factor"])
    out = feedback.learn(policy, "flood", {"O6": 130}, {"O6": 100}, 5.0, 0.7)   # 30%
    assert out["changes"]["rolled_back"] is True
    assert out["policy"]["delay_factor"] == before_last


def test_learn_no_signal_keeps_factor():
    out = feedback.learn(copy.deepcopy(feedback.DEFAULT_POLICY), "breakdown", {}, {}, None, 0.7)
    assert out["policy"]["delay_factor"]["breakdown"] == 1.0


# ---------------------------------------------------------------- policy store
def test_policy_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "POLICY_PATH", str(tmp_path / "policy.json"))
    assert feedback.load_policy() == feedback.DEFAULT_POLICY
    p = copy.deepcopy(feedback.DEFAULT_POLICY)
    p["version"] = 7
    feedback.save_policy(p)
    assert feedback.load_policy()["version"] == 7
