"""Track C tests: the agent's tools end to end over real routing + feedback. No LLM calls."""

import json
import os

import pytest

from dispatch_agent import feedback, seed
from dispatch_agent import tools as T


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Write state/policy into a temp dir and start every test from a hard reset."""
    monkeypatch.setattr(feedback, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setattr(T, "STATE_PATH", str(tmp_path / "state.json"))
    T.reset(hard=True)
    yield tmp_path


def _state(tmp_path):
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


def _flood_run():
    a = T.apply_disruption("flood")
    r = T.replan_routes()
    aff = r["affected_orders"]
    T.notify_customers([x["order_id"] for x in aff], [f"Flooding on OMR, new ETA {x['new_eta_clock']}." for x in aff])
    return a, r, T.run_day()


def test_uses_real_routing():
    assert T.R.__name__ == "dispatch_agent.routing"


def test_plan_routes_assigns_every_order_once(isolated):
    p = T.plan_routes()
    assert p["status"] == "ok" and p["verdict"] == "apply" and len(p["candidates"]) == 3
    stops = [o for r in _state(isolated)["plan"]["routes"] for o in r["stops"]]
    assert sorted(stops) == sorted(o["id"] for o in seed.ORDERS)


def test_flood_replan_beats_do_nothing_and_writes_map_fields(isolated):
    T.plan_routes()
    a, r, d = _flood_run()
    assert a["orders_in_zone"] == ["O6", "O10"]
    assert r["verdict"] == "apply" and r["time_saved_min"] > 0
    assert all(x["new_eta_clock"].endswith(("AM", "PM")) for x in r["affected_orders"])
    s = _state(isolated)
    for key in ("critic", "kpis", "baseline_plan", "before_after", "affected_orders", "last_run", "events"):
        assert s[key] is not None, key
    assert s["before_after"]["after"]["total_min"] < s["before_after"]["before"]["total_min"]
    assert {e["tool"] for e in s["events"]} >= {"plan_routes", "apply_disruption", "replan_routes",
                                                 "notify_customers", "run_day"}
    assert len(s["notifications"]) == len(s["affected_orders"])


def test_three_floods_learn_without_rollback():
    errors, factors = [], []
    for i in range(3):
        if i:
            T.reset()
        T.plan_routes()
        _, _, d = _flood_run()
        errors.append(d["eta_error"])
        factors.append(d["delay_factor_change"]["flood"][1])
        assert d["rolled_back"] is False
    assert errors[0] > errors[1] > errors[2]
    assert 4.0 < factors[0] < factors[1] < factors[2] < 5.5


def test_soft_reset_keeps_policy_hard_reset_clears_it():
    T.plan_routes()
    _flood_run()
    assert T.reset()["policy_version"] == 2
    assert T.reset(hard=True)["policy_version"] == 1


def test_breakdown_reassigns_broken_van():
    T.plan_routes()
    T.apply_disruption("breakdown")
    r = T.replan_routes()
    assert r["status"] == "ok" and "V2" not in [x["vehicle"] for x in r["routes"]]
    assert r["after"]["late_orders"] <= r["do_nothing"]["late_orders"]


def test_guard_rails():
    assert T.apply_disruption("flood")["status"] == "error"         # no plan yet
    assert T.replan_routes()["status"] == "error"
    assert T.run_day()["status"] == "error"
    T.plan_routes()
    assert T.run_day()["status"] == "error"                          # no disruption to learn from
    assert "flood" in T.apply_disruption("tsunami")["message"]
    assert T.notify_customers(["O1"], ["a", "b"])["status"] == "error"
    r = T.notify_customers(["O1", "O99"], ["word " * 45, "ok 1"])
    assert r["sent"] == 0 and len(r["rejected"]) == 2


def test_state_file_is_valid_json_after_every_tool(isolated):
    T.plan_routes()
    assert os.path.exists(isolated / "state.json")
    _flood_run()
    s = _state(isolated)
    assert s["disruption"]["kind"] == "flood" and s["policy"]["version"] == 2
