"""Fetch real road routes between every pair of stops once, and cache them.

    python scripts/fetch_roads.py            # writes dispatch_agent/data/roads.json

Uses the free OSRM demo server (OpenStreetMap data, no key). For each of the 55
pairs (depot + 10 orders) it stores up to 5 road options:
  - OSRM's main route and any alternatives it offers, and
  - forced detours via points to either side of the straight line, offset by ~30% and
    ~60% of the leg's length (1-4 km), i.e. realistic parallel streets,
so a disruption on the main road always has a real alternative street.
Run once; the demo then needs no internet for routing.
"""

import itertools
import json
import math
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dispatch_agent import geo  # noqa: E402

OSRM = "https://router.project-osrm.org/route/v1/driving/"
OUT = os.path.join(ROOT, "dispatch_agent", "data", "roads.json")
DETOUR_SHARES = (0.3, 0.6)   # detour offset as a share of the leg's length
DETOUR_MIN_KM, DETOUR_MAX_KM = 1.0, 4.0
MIN_GAP_M = 60      # thin the geometry: keep points at least this far apart
PAUSE_S = 0.35      # be polite to the shared demo server


def _get(coords: list[tuple[float, float]], alternatives: bool) -> list[dict]:
    path = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in coords)
    url = f"{OSRM}{path}?overview=full&geometries=geojson&alternatives={'3' if alternatives else 'false'}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "agentwars-dispatch-demo"}),
                                        timeout=30) as r:
                data = json.load(r)
            time.sleep(PAUSE_S)
            return data.get("routes", []) if data.get("code") == "Ok" else []
        except OSError:
            time.sleep(2 * (attempt + 1))
    return []


def _thin(lnglat: list[list[float]]) -> list[list[float]]:
    out = [[round(lnglat[0][1], 5), round(lnglat[0][0], 5)]]
    for lng, lat in lnglat[1:-1]:
        if geo.haversine_km(out[-1][0], out[-1][1], lat, lng) * 1000 >= MIN_GAP_M:
            out.append([round(lat, 5), round(lng, 5)])
    out.append([round(lnglat[-1][1], 5), round(lnglat[-1][0], 5)])
    return out


def _via_points(a: dict, b: dict) -> list[tuple[float, float]]:
    """Points either side of the midpoint, perpendicular to a->b, offset in proportion to the leg."""
    mlat, mlng = (a["lat"] + b["lat"]) / 2, (a["lng"] + b["lng"]) / 2
    kx = 111.32 * math.cos(math.radians(mlat))           # km per degree of longitude
    dx, dy = (b["lng"] - a["lng"]) * kx, (b["lat"] - a["lat"]) * 110.57
    n = math.hypot(dx, dy) or 1.0
    px, py = -dy / n, dx / n                              # unit perpendicular (km space)
    out = []
    for share in DETOUR_SHARES:
        off = min(DETOUR_MAX_KM, max(DETOUR_MIN_KM, share * n))
        out += [(mlat + s * py * off / 110.57, mlng + s * px * off / kx) for s in (1, -1)]
    return out


def main() -> None:
    nodes = geo.NODES
    pairs = list(itertools.combinations(nodes, 2))
    roads = {}
    for i, (a, b) in enumerate(pairs, 1):
        na, nb = nodes[a], nodes[b]
        ends = [(na["lat"], na["lng"]), (nb["lat"], nb["lng"])]
        options = _get(ends, alternatives=True)
        for via in _via_points(na, nb):
            options += _get([ends[0], via, ends[1]], alternatives=False)[:1]
        seen, kept = [], []
        for r in sorted(options, key=lambda r: r["distance"]):
            km = round(r["distance"] / 1000, 2)
            if any(abs(km - k) < 0.15 for k in seen):   # (almost) the same road twice
                continue
            seen.append(km)
            kept.append({"km": km, "geometry": _thin(r["geometry"]["coordinates"])})
        if not kept:
            print(f"  ! no route for {a}-{b}; the app falls back to straight-line estimates")
            continue
        roads[f"{a}|{b}"] = kept
        print(f"[{i:2}/{len(pairs)}] {a:>5}-{b:<5} {len(kept)} options, "
              f"{' / '.join(str(o['km']) for o in kept)} km")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"source": "OSRM demo server (OpenStreetMap)", "fetched": time.strftime("%Y-%m-%d"),
                   "pairs": roads}, f, separators=(",", ":"))
    print(f"\nWrote {OUT} ({os.path.getsize(OUT) // 1024} KB, {len(roads)} pairs)")


if __name__ == "__main__":
    main()
