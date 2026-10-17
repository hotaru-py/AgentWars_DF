"""Shared geometry helpers. Read-only for every track, so routing and feedback agree
exactly on distances, road shapes and how much of a road a disruption covers.

Road data: dispatch_agent/data/roads.json, real driving routes fetched once from
OSRM (OpenStreetMap) by scripts/fetch_roads.py. Each pair of stops has up to 5
road options (main route, alternatives, forced detours). If the file or a pair is
missing, everything falls back to straight-line distance x ROAD_FACTOR.
"""

import functools
import json
import math
import os

from . import seed

NODES = {seed.DEPOT["id"]: seed.DEPOT, **{o["id"]: o for o in seed.ORDERS}}
ROADS_PATH = os.path.join(os.path.dirname(__file__), "data", "roads.json")
SAMPLE_KM = 0.2   # zone test resolution along a road


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@functools.lru_cache(maxsize=1)
def _roads() -> dict:
    try:
        with open(ROADS_PATH, encoding="utf-8") as f:
            return json.load(f)["pairs"]
    except (OSError, ValueError, KeyError):
        return {}


@functools.lru_cache(maxsize=None)
def road_options(a: str, b: str) -> tuple:
    """Road options from a to b, shortest first: ({"km", "geometry": [[lat, lng], ...]}, ...).
    Geometry runs a -> b. Falls back to one straight-line option."""
    if a == b:
        n = NODES[a]
        return ({"km": 0.0, "geometry": [[n["lat"], n["lng"]]]},)
    pairs = _roads()
    if f"{a}|{b}" in pairs:
        return tuple(pairs[f"{a}|{b}"])
    if f"{b}|{a}" in pairs:
        return tuple({"km": o["km"], "geometry": o["geometry"][::-1]} for o in pairs[f"{b}|{a}"])
    na, nb = NODES[a], NODES[b]
    km = haversine_km(na["lat"], na["lng"], nb["lat"], nb["lng"]) * seed.ROAD_FACTOR
    return ({"km": km, "geometry": [[na["lat"], na["lng"]], [nb["lat"], nb["lng"]]]},)


def road_km(a: str, b: str) -> float:
    """Driving distance of the shortest road between two node ids."""
    return road_options(a, b)[0]["km"]


def km_to_min(km: float) -> float:
    return km / seed.SPEED_KMPH * 60


def _inside(lat: float, lng: float, disruption: dict) -> bool:
    clat, clng = disruption["center"]
    return haversine_km(clat, clng, lat, lng) <= disruption["radius_km"]


@functools.lru_cache(maxsize=4096)
def _zone_fraction(a: str, b: str, variant: int, center: tuple, radius_km: float) -> float:
    d = {"center": center, "radius_km": radius_km}
    pts = road_options(a, b)[variant]["geometry"]
    total = inside = 0.0
    for (lat1, lng1), (lat2, lng2) in zip(pts, pts[1:]):
        seg = haversine_km(lat1, lng1, lat2, lng2)
        n = max(1, math.ceil(seg / SAMPLE_KM))
        for i in range(n):  # midpoints of n equal pieces
            t = (i + 0.5) / n
            if _inside(lat1 + (lat2 - lat1) * t, lng1 + (lng2 - lng1) * t, d):
                inside += seg / n
        total += seg
    return inside / total if total else 0.0


def zone_fraction(a: str, b: str, variant: int, disruption: dict | None) -> float:
    """Share (0..1) of road option `variant` from a to b that lies inside the disruption circle."""
    if not disruption or not disruption.get("center") or a == b:
        return 0.0
    return _zone_fraction(a, b, variant, tuple(disruption["center"]), float(disruption["radius_km"]))


def leg_choice(a: str, b: str, disruption: dict | None, multiplier: float | None = None) -> dict:
    """The road a van takes from a to b, given a disruption: the option with the lowest
    expected minutes when the in-zone part runs `multiplier` times slower (default: the
    disruption's predicted multiplier). This is where real detours come from.
    Returns {"variant", "km", "base_min", "frac", "min"}."""
    m = multiplier if multiplier is not None else (disruption or {}).get("multiplier", 1.0)
    best = None
    for i, opt in enumerate(road_options(a, b)):
        base = km_to_min(opt["km"])
        frac = zone_fraction(a, b, i, disruption)
        minutes = base * (1 + (m - 1) * frac)
        if best is None or minutes < best["min"]:
            best = {"variant": i, "km": opt["km"], "base_min": base, "frac": frac, "min": minutes}
    return best


def leg_geometry(a: str, b: str, variant: int = 0) -> list:
    return road_options(a, b)[variant]["geometry"]


def edge_disrupted(a: str, b: str, disruption: dict | None) -> bool:
    """For a == b: is the stop inside the disruption circle. Otherwise: does the road the
    van would take from a to b (leg_choice) touch the circle, or start/end inside it."""
    if not disruption or not disruption.get("center"):
        return False
    na, nb = NODES[a], NODES[b]
    if _inside(na["lat"], na["lng"], disruption) or _inside(nb["lat"], nb["lng"], disruption):
        return True
    return a != b and leg_choice(a, b, disruption)["frac"] > 0
