"""Unit tests for the logistics tools. No network, no LLM calls."""

import copy
import itertools

import pytest

from logistics_agent import data, tools


@pytest.fixture(autouse=True)
def fresh_data(monkeypatch):
    """Give every test a pristine copy of the mutable mock data."""
    monkeypatch.setattr(data, "INVENTORY", copy.deepcopy(data.INVENTORY))
    monkeypatch.setattr(data, "SHIPMENTS", copy.deepcopy(data.SHIPMENTS))
    monkeypatch.setattr(data, "shipment_ids", itertools.count(1003))
    monkeypatch.delenv("USE_ROUTES_API", raising=False)


# ---------------------------------------------------------------- get_stock
def test_get_stock_single_warehouse_case_insensitive():
    r = tools.get_stock("sku-pump", "hOSUR")
    assert r == {"status": "ok", "warehouse": "Hosur", "sku": "SKU-PUMP", "qty": 90}


def test_get_stock_all_warehouses():
    r = tools.get_stock("SKU-PUMP")
    assert r["status"] == "ok"
    assert set(r["by_warehouse"]) == set(data.WAREHOUSES)
    assert r["by_warehouse"]["Madurai"] == 0
    assert r["by_warehouse"]["Vellore"] == 0


def test_get_stock_unknown_warehouse_lists_options():
    r = tools.get_stock("SKU-PUMP", "Mumbai")
    assert r["status"] == "error"
    assert "Chennai" in r["message"] and "Coimbatore" in r["message"]


def test_get_stock_unknown_sku_lists_options():
    r = tools.get_stock("SKU-WIDGET")
    assert r["status"] == "error"
    assert "SKU-PUMP" in r["message"]


# ---------------------------------------------------------------- find_low_stock
def test_find_low_stock_default_threshold():
    r = tools.find_low_stock()
    assert r["status"] == "ok"
    pairs = {(i["warehouse"], i["sku"]) for i in r["items"]}
    assert ("Madurai", "SKU-PUMP") in pairs
    assert ("Vellore", "SKU-PUMP") in pairs
    assert ("Hosur", "SKU-VALVE") in pairs  # exactly 10: "at or below"
    assert ("Hosur", "SKU-PUMP") not in pairs
    assert all(i["qty"] <= 10 for i in r["items"])


def test_find_low_stock_zero_threshold():
    items = tools.find_low_stock(0)["items"]
    assert items and all(i["qty"] == 0 for i in items)


# ---------------------------------------------------------------- shortest_route
def test_shortest_route_chennai_bengaluru():
    r = tools.shortest_route("Chennai", "Bengaluru")
    assert r["status"] == "ok"
    assert r["path"] == ["Chennai", "Vellore", "Hosur", "Bengaluru"]
    assert r["total_km"] == 325
    assert r["est_hours"] == round(325 / 45, 1)


def test_shortest_route_is_case_insensitive_and_symmetric():
    r = tools.shortest_route("bengaluru", "CHENNAI")
    assert r["path"] == ["Bengaluru", "Hosur", "Vellore", "Chennai"]
    assert r["total_km"] == 325


def test_shortest_route_same_city():
    r = tools.shortest_route("Trichy", "Trichy")
    assert r["path"] == ["Trichy"] and r["total_km"] == 0


def test_shortest_route_unknown_city():
    r = tools.shortest_route("Chennai", "Atlantis")
    assert r["status"] == "error" and "Madurai" in r["message"]


def test_shortest_route_api_flag_uses_stub(monkeypatch):
    monkeypatch.setenv("USE_ROUTES_API", "TRUE")
    r = tools.shortest_route("Chennai", "Bengaluru")
    assert r["status"] == "error" and "USE_ROUTES_API" in r["message"]


# ---------------------------------------------------------------- shipments
def test_get_shipment_status_keeps_ok_status():
    r = tools.get_shipment_status("shp-1002")
    assert r["status"] == "ok"
    assert r["shipment_id"] == "SHP-1002"
    assert r["shipment_status"] == "delayed"
    assert r["current_location"] == "Coimbatore"
    assert r["reason"] == "highway closure"


def test_get_shipment_status_unknown_lists_known():
    r = tools.get_shipment_status("SHP-9999")
    assert r["status"] == "error" and "SHP-1001" in r["message"]


# ---------------------------------------------------------------- create_transfer
def test_create_transfer_deducts_stock_and_creates_shipment():
    r = tools.create_transfer("SKU-PUMP", 30, "hosur", "Madurai")
    assert r["status"] == "ok"  # not overwritten by shipment_status
    assert r["shipment_status"] == "scheduled"
    assert r["shipment_id"] == "SHP-1003"
    assert data.INVENTORY["Hosur"]["SKU-PUMP"] == 60
    assert r["source_remaining"] == 60
    assert r["route"][0] == "Hosur" and r["route"][-1] == "Madurai"
    assert r["eta_hours"] > 0

    looked_up = tools.get_shipment_status("SHP-1003")
    assert looked_up["status"] == "ok"
    assert looked_up["shipment_status"] == "scheduled"

    assert tools.create_transfer("SKU-PUMP", 1, "Hosur", "Vellore")["shipment_id"] == "SHP-1004"


def test_create_transfer_over_quantity_errors_without_side_effects():
    before = copy.deepcopy(data.INVENTORY)
    r = tools.create_transfer("SKU-PUMP", 91, "Hosur", "Madurai")
    assert r["status"] == "error" and "90" in r["message"]
    assert data.INVENTORY == before
    assert set(data.SHIPMENTS) == {"SHP-1001", "SHP-1002"}


@pytest.mark.parametrize("args", [
    ("SKU-PUMP", 0, "Hosur", "Madurai"),
    ("SKU-PUMP", -5, "Hosur", "Madurai"),
    ("SKU-PUMP", 5, "Hosur", "Hosur"),
    ("SKU-PUMP", 5, "Hosur", "Mumbai"),
    ("SKU-NOPE", 5, "Hosur", "Madurai"),
])
def test_create_transfer_invalid_inputs(args):
    assert tools.create_transfer(*args)["status"] == "error"
    assert data.INVENTORY["Hosur"]["SKU-PUMP"] == 90
