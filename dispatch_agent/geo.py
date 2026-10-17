"""Shared geometry helpers. Read-only for every track, so A and B agree exactly
on distances and on which legs a disruption touches."""

import math

from . import seed

NODES = {seed.DEPOT["id"]: seed.DEPOT, **{o["id"]: o for o in seed.ORDERS}}


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def road_km(a: str, b: str) -> float:
    """Road distance between two node ids (haversine x ROAD_FACTOR)."""
    na, nb = NODES[a], NODES[b]
    return haversine_km(na["lat"], na["lng"], nb["lat"], nb["lng"]) * seed.ROAD_FACTOR


def km_to_min(km: float) -> float:
    return km / seed.SPEED_KMPH * 60


def edge_disrupted(a: str, b: str, disruption: dict | None) -> bool:
    """True if edge a-b has an endpoint or its midpoint inside the disruption circle."""
    if not disruption or not disruption.get("center"):
        return False
    clat, clng = disruption["center"]
    na, nb = NODES[a], NODES[b]
    points = [(na["lat"], na["lng"]), (nb["lat"], nb["lng"]),
              ((na["lat"] + nb["lat"]) / 2, (na["lng"] + nb["lng"]) / 2)]
    return any(haversine_km(clat, clng, lat, lng) <= disruption["radius_km"] for lat, lng in points)
