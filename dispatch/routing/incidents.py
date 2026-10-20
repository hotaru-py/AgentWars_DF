"""Apply active incidents to a travel-time matrix (spec §8).

Each arc's time is scaled by every incident it crosses: factor = 1 + (slowdown - 1) * share,
where `share` is the part of the arc inside the incident's area. Several incidents multiply.
The arc is approximated by the straight segment between its ends (sampled every ~200 m); real
road shapes per arc would need one route call per pair, which is too slow for a full matrix.
Vehicle incidents (breakdowns) don't change travel times; the planner drops that vehicle instead.
"""

from __future__ import annotations

import math

from dispatch.core.models import Circle, Incident, IncidentState, LatLng, Polygon
from dispatch.core.ports import Matrix

from .engines import haversine_m

SAMPLE_M = 200.0


def _inside(p: LatLng, area: Circle | Polygon) -> bool:
    if isinstance(area, Circle):
        return haversine_m(p, area.center) <= area.radius_m
    inside, ring = False, area.ring                     # ray casting in lat/lng (fine at city scale)
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if (a.lat > p.lat) != (b.lat > p.lat):
            x = a.lng + (p.lat - a.lat) * (b.lng - a.lng) / (b.lat - a.lat)
            if p.lng < x:
                inside = not inside
    return inside


def share_inside(a: LatLng, b: LatLng, area: Circle | Polygon) -> float:
    """Fraction (0..1) of the straight segment a->b inside the area, by midpoint sampling."""
    n = max(1, math.ceil(haversine_m(a, b) / SAMPLE_M))
    hits = sum(_inside(LatLng(lat=a.lat + (b.lat - a.lat) * (k + 0.5) / n,
                              lng=a.lng + (b.lng - a.lng) * (k + 0.5) / n), area) for k in range(n))
    return hits / n


def apply_incidents(matrix: Matrix, incidents: list[Incident]) -> Matrix:
    live = [i for i in incidents if i.area is not None and i.slowdown > 1 and i.state == IncidentState.active]
    if not live:
        return matrix
    pts, n = matrix.points, len(matrix.points)
    seconds = [row[:] for row in matrix.seconds]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            f = 1.0
            for inc in live:
                share = share_inside(pts[i], pts[j], inc.area)
                if share:
                    f *= 1 + (inc.slowdown - 1) * share
            seconds[i][j] *= f
    return Matrix(points=pts, seconds=seconds, meters=matrix.meters, source=matrix.source + "+incidents")
