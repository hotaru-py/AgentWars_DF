"""RoutingEngine implementations (spec §3.2, §6.1).

- OsrmEngine:      real roads from a local OSRM server (infra/), table + route services.
- HaversineEngine: straight line x road factor at a fixed speed; offline, always available.
- FallbackEngine:  tries engines in order, so planning keeps working when OSRM is down.

Travel times: OSRM returns free-flow car times, which are optimistic for vans in Indian city
traffic. `time_factor` (OSRM_TIME_FACTOR, default 1.0 = raw OSRM) scales them; the ETA model
(phase 5) learns the real factors per zone and hour, so we don't hard-code a guess here.
"""

from __future__ import annotations

import math
from datetime import datetime

import httpx

from dispatch.core.models import LatLng
from dispatch.core.ports import Matrix, RouteLeg, RouteShape


def haversine_m(a: LatLng, b: LatLng) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lng - a.lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


class RoutingUnavailable(Exception):
    """The engine can't answer right now (server down, bad response). FallbackEngine moves on."""


class HaversineEngine:
    name = "haversine"

    def __init__(self, road_factor: float = 1.3, speed_kmph: float = 25.0):
        self.road_factor, self.speed_mps = road_factor, speed_kmph / 3.6

    def _m(self, a: LatLng, b: LatLng) -> float:
        return haversine_m(a, b) * self.road_factor

    def matrix(self, points: list[LatLng], depart_at: datetime | None = None) -> Matrix:
        meters = [[self._m(a, b) for b in points] for a in points]
        return Matrix(points=points, meters=meters, seconds=[[m / self.speed_mps for m in row] for row in meters],
                      source=self.name)

    def route(self, points: list[LatLng]) -> RouteShape:
        legs = [RouteLeg(meters=self._m(a, b), seconds=self._m(a, b) / self.speed_mps, geometry=[a, b])
                for a, b in zip(points, points[1:])]
        return RouteShape(legs=legs, geometry=list(points))


class OsrmEngine:
    name = "osrm"

    def __init__(self, url: str = "http://127.0.0.1:5000", profile: str = "driving", time_factor: float = 1.0,
                 block: int = 400, timeout_s: float = 30.0, client: httpx.Client | None = None):
        self.url, self.profile, self.time_factor, self.block = url.rstrip("/"), profile, time_factor, block
        self.client = client or httpx.Client(timeout=timeout_s)
        self._fallback = HaversineEngine()

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self.client.get(f"{self.url}/{path}", params=params)
        except httpx.HTTPError as e:
            raise RoutingUnavailable(f"OSRM unreachable at {self.url}: {e}") from e
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or data.get("code") != "Ok":
            raise RoutingUnavailable(f"OSRM {path.split('/')[0]} failed: {r.status_code} {data.get('code')} {data.get('message', '')}")
        return data

    @staticmethod
    def _coords(points: list[LatLng]) -> str:
        return ";".join(f"{p.lng:.6f},{p.lat:.6f}" for p in points)

    def matrix(self, points: list[LatLng], depart_at: datetime | None = None) -> Matrix:
        n = len(points)
        seconds = [[0.0] * n for _ in range(n)]
        meters = [[0.0] * n for _ in range(n)]
        coords = self._coords(points)
        # One request per (source block x destination block) so large fleets stay under max-table-size.
        for s0 in range(0, n, self.block):
            for d0 in range(0, n, self.block):
                src, dst = range(s0, min(n, s0 + self.block)), range(d0, min(n, d0 + self.block))
                data = self._get(f"table/v1/{self.profile}/{coords}", {
                    "sources": ";".join(map(str, src)), "destinations": ";".join(map(str, dst)),
                    "annotations": "duration,distance"})
                for i, row_s, row_m in zip(src, data["durations"], data["distances"]):
                    for j, sec, m in zip(dst, row_s, row_m):
                        if i == j:
                            continue
                        if sec is None or m is None:          # no road connection: straight-line estimate
                            fb_m = self._fallback._m(points[i], points[j])
                            sec, m = fb_m / self._fallback.speed_mps, fb_m
                        seconds[i][j], meters[i][j] = max(1.0, sec * self.time_factor), max(1.0, m)
        return Matrix(points=points, seconds=seconds, meters=meters, source=self.name)

    def route(self, points: list[LatLng]) -> RouteShape:
        data = self._get(f"route/v1/{self.profile}/{self._coords(points)}",
                         {"overview": "full", "geometries": "geojson", "steps": "true"})
        r = data["routes"][0]
        legs = []
        for leg in r["legs"]:
            geom: list[LatLng] = []
            for step in leg.get("steps", []):
                pts = [LatLng(lat=lat, lng=lng) for lng, lat in step["geometry"]["coordinates"]]
                geom.extend(pts[1:] if geom else pts)
            legs.append(RouteLeg(meters=leg["distance"], seconds=leg["duration"] * self.time_factor, geometry=geom))
        whole = [LatLng(lat=lat, lng=lng) for lng, lat in r["geometry"]["coordinates"]]
        return RouteShape(legs=legs, geometry=whole)


class FallbackEngine:
    """Try each engine in order; the matrix's `source` says which one answered."""

    def __init__(self, *engines):
        if not engines:
            raise ValueError("FallbackEngine needs at least one engine")
        self.engines = engines
        self.name = "+".join(e.name for e in engines)

    def _try(self, method: str, *args):
        errors = []
        for e in self.engines:
            try:
                return getattr(e, method)(*args)
            except RoutingUnavailable as err:
                errors.append(str(err))
        raise RoutingUnavailable("; ".join(errors))

    def matrix(self, points: list[LatLng], depart_at: datetime | None = None) -> Matrix:
        return self._try("matrix", points, depart_at)

    def route(self, points: list[LatLng]) -> RouteShape:
        return self._try("route", points)


def default_engine(osrm_url: str, time_factor: float = 1.0) -> FallbackEngine:
    return FallbackEngine(OsrmEngine(osrm_url, time_factor=time_factor), HaversineEngine())
