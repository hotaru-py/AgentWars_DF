"""Apply active incidents to a travel-time matrix (spec §8).

Each arc's time is scaled by every incident it crosses: factor = 1 + (slowdown - 1) * share,
where `share` is the part of the arc inside the incident's area. Several incidents multiply.
The arc is approximated by the straight segment between its ends (sampled every ~200 m); real
road shapes per arc would need one route call per pair, which is too slow for a full matrix.

Detours: with an engine, each circular zone gets RING_POINTS via points on a ring just outside it.
One extra matrix call gives travel to and from every via point; an arc that crosses a zone takes
the best "a -> via -> b" when that beats driving through (both scored with the slowdowns). The
chosen via point is kept in `Matrix.detours`, so the solver's stops, the map and the twin all
follow the same detour.

Vehicle incidents (breakdowns) don't change travel times; the planner drops that vehicle instead.
"""

from __future__ import annotations

import math

from dispatch.core.models import Circle, Incident, IncidentState, LatLng, Polygon
from dispatch.core.ports import Matrix

from .engines import haversine_m

SAMPLE_M = 200.0
RING_POINTS = 8
RING_SCALE, RING_PAD_M = 1.25, 300.0          # ring radius = 1.25 x zone radius + 300 m
MIN_GAIN = 0.02                               # a detour must save 2%: not sampling noise, worth the turn-off


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


def slow_factor(a: LatLng, b: LatLng, live: list[Incident]) -> float:
    """How much slower the straight segment a->b runs, given the live incidents."""
    f = 1.0
    for inc in live:
        share = share_inside(a, b, inc.area)
        if share:
            f *= 1 + (inc.slowdown - 1) * share
    return f


def ring(area: Circle) -> list[LatLng]:
    r = area.radius_m * RING_SCALE + RING_PAD_M
    c = area.center
    out = []
    for k in range(RING_POINTS):
        t = 2 * math.pi * k / RING_POINTS
        out.append(LatLng(lat=c.lat + (r * math.cos(t)) / 111_320,
                          lng=c.lng + (r * math.sin(t)) / (111_320 * math.cos(math.radians(c.lat)))))
    return out


def apply_incidents(matrix: Matrix, incidents: list[Incident], engine=None) -> Matrix:
    live = [i for i in incidents if i.area is not None and i.slowdown > 1 and i.state == IncidentState.active]
    if not live:
        return matrix
    pts, n = matrix.points, len(matrix.points)
    seconds = [row[:] for row in matrix.seconds]
    meters = [row[:] for row in matrix.meters]
    slowed = []
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            f = slow_factor(pts[i], pts[j], live)
            if f > 1:
                seconds[i][j] *= f
                slowed.append((i, j))
    detours: dict[str, LatLng] = {}
    vias = [v for inc in live if isinstance(inc.area, Circle) for v in ring(inc.area)]
    if engine is not None and vias and slowed:
        from .engines import RoutingUnavailable
        try:
            m2 = engine.matrix(list(pts) + vias)
        except RoutingUnavailable:
            m2 = None
        if m2 is not None:
            to_via = {(i, k): m2.seconds[i][n + k] * slow_factor(pts[i], v, live)
                      for i in range(n) for k, v in enumerate(vias)}
            from_via = {(k, j): m2.seconds[n + k][j] * slow_factor(v, pts[j], live)
                        for j in range(n) for k, v in enumerate(vias)}
            for i, j in slowed:
                best, via = seconds[i][j] * (1 - MIN_GAIN), None
                for k in range(len(vias)):
                    t = to_via[i, k] + from_via[k, j]
                    if t < best:
                        best, via = t, k
                if via is not None:
                    seconds[i][j] = best
                    meters[i][j] = m2.meters[i][n + via] + m2.meters[n + via][j]
                    detours[f"{i}-{j}"] = vias[via]
    return Matrix(points=pts, seconds=seconds, meters=meters, source=matrix.source + "+incidents", detours=detours)
