"""HTTP API (dispatch.api) over a temp SQLite store. No network, no AI keys."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from dispatch.api import create_app
from dispatch.core.config import Settings
from dispatch.core.decisions import Choice, ChoiceAnswer, DecisionRecord, Route
from dispatch.core.models import GateThresholds, LatLng, Plan, Stop, VehicleRoute
from dispatch.optimize import OrToolsSolver
from dispatch.planning import Planner
from dispatch.routing import HaversineEngine
from dispatch.store import Store

T0 = datetime(2026, 10, 18, 3, 30, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'api.db'}")
    st.create_all()
    yield st
    st.engine.dispose()


@pytest.fixture
def api(store):
    planner = Planner(store, HaversineEngine(), OrToolsSolver(), time_limit_s=0.3)     # offline routing in tests
    with TestClient(create_app(store, planner=planner, settings=Settings())) as c:   # no .env, no keys
        yield c


@pytest.fixture
def world(api):
    depot = api.post("/depots", json={"name": "Guindy", "location": {"lat": 13.0067, "lng": 80.2206},
                                      "opens_at": T0.isoformat(), "closes_at": (T0 + timedelta(hours=10)).isoformat()}).json()
    van = api.post("/vehicles", json={"depot_id": depot["id"], "name": "V1", "capacity_kg": 50, "capacity_l": 300}).json()
    cust = api.post("/customers", json={"name": "Priya", "phone": "+91-90000-00006"}).json()
    return depot, van, cust


def _order(api, depot, cust, **kw):
    r = api.post("/orders", json={"depot_id": depot["id"], "customer_id": cust["id"],
                                  "raw_address": "12, 3rd Main Rd, Adyar", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _plan_orders(store, depot, van, order_ids):
    for oid in order_ids:
        store.set_geocode(oid, LatLng(lat=13.0, lng=80.25), None, 0.9, "test")
    stops = [Stop(order_id=o, seq=i, eta_p50=T0, eta_p90=T0, planned_arrival=T0) for i, o in enumerate(order_ids)]
    p = Plan(depot_id=depot["id"], routes=[VehicleRoute(vehicle_id=van["id"], stops=stops)])
    store.save_plan(p)
    store.apply_plan(p.id)
    return p


def test_health(api):
    h = api.get("/health").json()
    assert h["ok"] and h["db"] == "sqlite" and h["routing"] == "haversine"


def test_reference_data_and_validation(api, world):
    depot, van, cust = world
    assert api.get("/vehicles", params={"depot_id": depot["id"]}).json() == [van]
    assert api.post("/vehicles", json={"depot_id": "dep_nope", "capacity_kg": 5, "capacity_l": 5}).status_code == 404
    assert api.post("/vehicles", json={"depot_id": depot["id"], "capacity_kg": 0, "capacity_l": 5}).status_code == 422
    assert api.get(f"/customers/{cust['id']}").json()["name"] == "Priya"


def test_order_create_get_cancel(api, world):
    depot, _, cust = world
    o = _order(api, depot, cust, payment="cod", cod_amount=499, priority="urgent")
    assert o["state"] == "new" and o["id"].startswith("ord_")
    assert api.get(f"/orders/{o['id']}").json() == o
    assert api.post("/orders", json={"depot_id": depot["id"], "customer_id": "cus_x", "raw_address": "abc"}).status_code == 404
    assert api.post(f"/orders/{o['id']}/cancel").json()["state"] == "cancelled"
    assert api.post(f"/orders/{o['id']}/cancel").status_code == 409          # already final
    assert [x["id"] for x in api.get("/orders", params={"state": "cancelled"}).json()] == [o["id"]]


def test_csv_import_reports_bad_rows_and_reuses_customers(api, world):
    depot, _, cust = world
    csv_text = ("customer_name,phone,address,weight_kg,payment,cod_amount,lat,lng\n"
                "Priya,+91-90000-00006,12 3rd Main Rd Adyar,2,cod,350,13.0012,80.2565\n"
                "Arun,+91-90000-00001,4 Usman Rd T Nagar,1.5,prepaid,,,\n"
                ",+91-1,missing name row,1,prepaid,,,\n"
                "Bad,+91-2,row with bad payment,1,bitcoin,,,\n")
    r = api.post("/orders/import", params={"depot_id": depot["id"]}, files={"file": ("orders.csv", csv_text, "text/csv")})
    body = r.json()
    assert r.status_code == 200 and len(body["created"]) == 2
    assert [e["line"] for e in body["errors"]] == [4, 5]
    first = api.get(f"/orders/{body['created'][0]}").json()
    assert first["customer_id"] == cust["id"] and first["location"] == {"lat": 13.0012, "lng": 80.2565}


def test_driver_flow_and_rules(api, store, world):
    depot, van, cust = world
    a, b = _order(api, depot, cust), _order(api, depot, cust)
    assert api.post(f"/driver/stops/{a['id']}/deliver", json={"vehicle_id": van["id"]}).status_code == 409   # not planned
    _plan_orders(store, depot, van, [a["id"], b["id"]])
    assert api.post(f"/driver/stops/{a['id']}/arrive", json={"vehicle_id": van["id"]}).status_code == 200
    assert api.post(f"/driver/stops/{a['id']}/deliver", json={"vehicle_id": van["id"]}).status_code == 200
    assert api.get(f"/orders/{a['id']}").json()["state"] == "delivered"
    assert api.post(f"/driver/stops/{b['id']}/fail", json={"vehicle_id": van["id"]}).status_code == 422      # needs reason
    assert api.post(f"/driver/stops/{b['id']}/fail", json={"vehicle_id": van["id"], "reason": "not home"}).status_code == 200
    got = api.get(f"/orders/{b['id']}").json()
    assert got["state"] == "failed" and got["attempts"] == 1
    assert api.get("/plan/current", params={"depot_id": depot["id"]}).json()["routes"][0]["vehicle_id"] == van["id"]


def test_plan_run_end_to_end(api, store, world):
    depot, van, cust = world
    assert api.post("/plan/run", params={"depot_id": depot["id"]}).status_code == 422      # nothing to plan yet
    ids = [_order(api, depot, cust)["id"] for _ in range(3)]
    for oid, (lat, lng) in zip(ids, [(13.04, 80.23), (13.0, 80.256), (12.98, 80.218)]):
        store.set_geocode(oid, LatLng(lat=lat, lng=lng), None, 0.9, "test")
    r = api.post("/plan/run", params={"depot_id": depot["id"], "reason": "initial"})
    body = r.json()
    assert r.status_code == 200 and body["verdict"] == "apply" and len(body["candidates"]) == 3
    stops = [s["order_id"] for rt in body["plan"]["routes"] for s in rt["stops"]]
    assert sorted(stops) == sorted(ids) and body["baseline"] is None
    assert api.get("/plan/current", params={"depot_id": depot["id"]}).json()["id"] == body["plan"]["id"]
    assert api.get(f"/decisions/{body['decision_id']}").json()["type"] == "plan_select"
    assert {o["state"] for o in api.get("/orders", params={"depot_id": depot["id"]}).json()} == {"planned"}


def test_incidents_messages_decisions(api, store, world):
    depot, van, cust = world
    inc = api.post("/incidents", json={"kind": "flood", "description": "knee-deep water on OMR", "source": "customer",
                                       "area": {"kind": "circle", "center": {"lat": 12.94, "lng": 80.24}, "radius_m": 4000}})
    assert inc.status_code == 201 and inc.json()["state"] == "reported"
    assert len(api.get("/incidents", params={"state": "reported"}).json()) == 1
    assert api.post("/webhooks/messages", json={"customer_id": cust["id"], "text": "come after 6 pm"}).status_code == 200
    assert api.post("/webhooks/messages", json={"customer_id": "cus_x", "text": "hi"}).status_code == 404
    rec = DecisionRecord(type="incident_triage", subject=inc.json()["id"], decider="llm",
                         questions={"kind": Choice(instructions="k", criteria={"flood": "w", "traffic": "j"})},
                         answers={"kind": ChoiceAnswer.from_probabilities({"flood": 0.95, "traffic": 0.05})},
                         raw_confidence=0.9, confidence=0.9, gate=GateThresholds(), route=Route.act)
    store.record_decision(rec)
    assert [d["id"] for d in api.get("/decisions", params={"route": "act"}).json()] == [rec.id]
    assert api.post(f"/decisions/{rec.id}/override", json={"by": "ops lead", "note": "not a flood"}).status_code == 200
    assert api.post("/decisions/dec_x/override", json={"by": "x"}).status_code == 404
    assert api.get("/policy").json()["version"] == 1


def test_stream_replays_events_and_resumes(api, world):
    depot, _, cust = world
    o1, o2 = _order(api, depot, cust), _order(api, depot, cust)
    with api.stream("GET", "/stream", params={"after": 0, "types": "order.created", "max_events": 2}) as r:
        text = "".join(r.iter_text())
    ids = [int(line[4:]) for line in text.splitlines() if line.startswith("id: ")]
    assert r.headers["content-type"].startswith("text/event-stream")
    assert text.count("event: order.created") == 2 and ids == sorted(ids) and o1["id"] in text and o2["id"] in text
    with api.stream("GET", "/stream", params={"types": "order.created", "max_events": 1},
                    headers={"Last-Event-ID": str(ids[0])}) as r:                  # resume after the first
        resumed = "".join(r.iter_text())
    assert o2["id"] in resumed and o1["id"] not in resumed
    with api.stream("GET", "/stream", params={"timeout_s": 0.3}) as r:            # nothing new: ends on timeout
        assert "event:" not in "".join(r.iter_text())


# ---------------------------------------------------------------- console support (run.py / web/console.html)
def test_state_snapshot_and_demo_reset(store, monkeypatch):
    from dispatch import demo
    monkeypatch.setenv("DEMO_MODE", "1")
    demo.seed(store)
    assert demo.seed(store) is None                                   # seeding twice is a no-op
    planner = Planner(store, HaversineEngine(), OrToolsSolver(), time_limit_s=0.3)
    with TestClient(create_app(store, planner=planner, settings=Settings())) as c:
        s = c.get("/state").json()
        assert s["depot"]["name"] == "Guindy depot" and len(s["vehicles"]) == 3 and len(s["orders"]) == 10
        assert {o["state"] for o in s["orders"]} == {"geocoded"} and s["orders"][0]["customer"] == "Arun Kumar"
        assert s["plan"] is None and s["demo"] is True and s["queue"] == 0
        c.post("/plan/run", params={"depot_id": s["depot"]["id"]})
        s = c.get("/state").json()
        assert s["plan"]["scores"]["late_orders"] == 0 and len(s["plan_decision"]["cost"]["candidates"]) == 3
        assert c.post("/demo/reset").status_code == 200
        assert c.get("/state").json()["plan"] is None
        cors = c.get("/state", headers={"Origin": "http://127.0.0.1:8001"})
        assert cors.headers["access-control-allow-origin"] == "http://127.0.0.1:8001"


def test_demo_reset_is_off_without_demo_mode(api):
    assert api.post("/demo/reset").status_code == 404
    assert api.get("/state").json() == {"depot": None}


def test_csv_rows_with_coordinates_are_plannable(api, world):
    depot, _, _ = world
    csv_text = "customer_name,phone,address,lat,lng\nZ,+91-7,Egmore,13.0732,80.2609\nY,+91-8,somewhere,,\n"
    body = api.post("/orders/import", params={"depot_id": depot["id"]}, files={"file": ("o.csv", csv_text, "text/csv")}).json()
    states = [api.get(f"/orders/{i}").json()["state"] for i in body["created"]]
    assert states == ["geocoded", "new"]


def test_recent_events_as_json(api, world):
    depot, _, cust = world
    for _ in range(3):
        _order(api, depot, cust)
    first = api.get("/events").json()
    assert first and [e["seq"] for e in first] == sorted(e["seq"] for e in first)        # oldest first
    after = first[0]["seq"]
    later = api.get("/events", params={"after": after, "limit": 2}).json()
    assert len(later) <= 2 and all(e["seq"] > after for e in later)
    assert all(e["type"] == "order.created" for e in api.get("/events", params={"types": "order.created"}).json())
    assert api.post("/twin/replay").status_code in (404, 405)                            # the stub is gone
