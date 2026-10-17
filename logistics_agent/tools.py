"""Plain Python tool functions for the logistics agents.

ADK builds each tool's schema from its type hints and docstring, so keep both
accurate. Every tool returns a dict with "status": "ok" | "error"; errors carry
a "message" that lists the valid options so the model can self-correct.
"""

import heapq
import os

from . import data


def _norm_warehouse(name: str) -> str | None:
    """Case-insensitive warehouse name match. Returns the canonical name or None."""
    key = (name or "").strip().lower()
    for wh in data.WAREHOUSES:
        if wh.lower() == key:
            return wh
    return None


def _norm_sku(sku: str) -> str | None:
    """Case-insensitive SKU match. Accepts "pump" as shorthand for "SKU-PUMP"."""
    key = (sku or "").strip().upper()
    if key in data.SKUS:
        return key
    if f"SKU-{key}" in data.SKUS:
        return f"SKU-{key}"
    return None


def _unknown_warehouse(name: str) -> dict:
    return {"status": "error",
            "message": f"Unknown warehouse '{name}'. Valid warehouses: {', '.join(data.WAREHOUSES)}."}


def _unknown_sku(sku: str) -> dict:
    return {"status": "error",
            "message": f"Unknown SKU '{sku}'. Valid SKUs: {', '.join(data.SKUS)}."}


# ---------------------------------------------------------------- inventory
def get_stock(sku: str, warehouse: str = "") -> dict:
    """Get the current stock quantity of a product.

    Args:
        sku: Product code: "SKU-PUMP", "SKU-VALVE" or "SKU-SENSOR".
        warehouse: Warehouse city name, e.g. "Hosur". Leave empty to get the
            quantity at every warehouse.

    Returns:
        {"status": "ok", "warehouse", "sku", "qty"} for one warehouse, or
        {"status": "ok", "sku", "by_warehouse": {city: qty}} for all of them.
    """
    canon_sku = _norm_sku(sku)
    if not canon_sku:
        return _unknown_sku(sku)
    if warehouse.strip():
        wh = _norm_warehouse(warehouse)
        if not wh:
            return _unknown_warehouse(warehouse)
        return {"status": "ok", "warehouse": wh, "sku": canon_sku,
                "qty": data.INVENTORY[wh].get(canon_sku, 0)}
    return {"status": "ok", "sku": canon_sku,
            "by_warehouse": {w: s.get(canon_sku, 0) for w, s in data.INVENTORY.items()}}


def find_low_stock(threshold: int = 10) -> dict:
    """List every warehouse/SKU pair whose quantity is at or below a threshold.

    Args:
        threshold: Quantity at or below which stock counts as low. Default 10.

    Returns:
        {"status": "ok", "threshold", "items": [{"warehouse", "sku", "qty"}, ...]},
        sorted with the emptiest first.
    """
    if threshold < 0:
        return {"status": "error", "message": "threshold must be 0 or greater."}
    low = [{"warehouse": w, "sku": k, "qty": q}
           for w, stock in data.INVENTORY.items() for k, q in stock.items() if q <= threshold]
    low.sort(key=lambda item: (item["qty"], item["warehouse"], item["sku"]))
    return {"status": "ok", "threshold": threshold, "items": low}


# ---------------------------------------------------------------- routing
def _dijkstra(src: str, dst: str) -> tuple[list[str], int] | None:
    """Shortest path over data.GRAPH. Returns (path, km) or None if unreachable."""
    dist = {src: 0}
    prev: dict[str, str] = {}
    pq = [(0, src)]
    while pq:
        d, node = heapq.heappop(pq)
        if node == dst:
            break
        if d > dist.get(node, float("inf")):
            continue
        for nxt, km in data.GRAPH.get(node, []):
            nd = d + km
            if nd < dist.get(nxt, float("inf")):
                dist[nxt], prev[nxt] = nd, node
                heapq.heappush(pq, (nd, nxt))
    if dst not in dist:
        return None
    path, cur = [dst], dst
    while cur != src:
        cur = prev[cur]
        path.append(cur)
    path.reverse()
    return path, dist[dst]


def _routes_api(src: str, dst: str) -> dict:
    # ==================================================================
    # STUB: Google Maps Routes API (enabled with USE_ROUTES_API=TRUE).
    # Replace this body with a call to
    #   POST https://routes.googleapis.com/directions/v2:computeRoutes
    # and return the same shape as the Dijkstra branch:
    #   {"status": "ok", "path": [...], "total_km": float, "est_hours": float}
    # Keep shortest_route()'s signature unchanged so the agents don't care.
    # ==================================================================
    return {"status": "error",
            "message": "USE_ROUTES_API is set but the Routes API is not implemented yet. "
                       "Unset USE_ROUTES_API to use the local road graph."}


def shortest_route(origin: str, destination: str) -> dict:
    """Find the shortest road route between two warehouses.

    Args:
        origin: Starting warehouse city name, e.g. "Chennai".
        destination: Destination warehouse city name, e.g. "Bengaluru".

    Returns:
        {"status": "ok", "origin", "destination", "path": [cities in order],
        "total_km", "est_hours"} where est_hours assumes an average truck speed.
    """
    src, dst = _norm_warehouse(origin), _norm_warehouse(destination)
    if not src:
        return _unknown_warehouse(origin)
    if not dst:
        return _unknown_warehouse(destination)
    if os.getenv("USE_ROUTES_API", "").strip().upper() in ("1", "TRUE", "YES"):
        return _routes_api(src, dst)
    found = _dijkstra(src, dst)
    if not found:
        return {"status": "error", "message": f"No road route from {src} to {dst}."}
    path, km = found
    return {"status": "ok", "origin": src, "destination": dst, "path": path,
            "total_km": km, "est_hours": round(km / data.AVG_TRUCK_KMPH, 1)}


# ---------------------------------------------------------------- shipments
def get_shipment_status(shipment_id: str) -> dict:
    """Look up a shipment's current state, location and any delay reason.

    Args:
        shipment_id: Shipment ID, e.g. "SHP-1001".

    Returns:
        {"status": "ok", "shipment_id", "sku", "qty", "from", "to",
        "shipment_status", "current_location", ...}. "shipment_status" is the
        shipment's own state (scheduled / in_transit / delayed / delivered).
    """
    sid = (shipment_id or "").strip().upper()
    shipment = data.SHIPMENTS.get(sid)
    if not shipment:
        return {"status": "error",
                "message": f"No shipment '{shipment_id}'. Known shipments: {', '.join(data.SHIPMENTS)}."}
    return {**shipment, "status": "ok", "shipment_id": sid}


def create_transfer(sku: str, qty: int, from_warehouse: str, to_warehouse: str) -> dict:
    """Transfer stock between two warehouses: deducts it from the source and
    creates a scheduled shipment along the shortest route with an ETA.

    IMPORTANT: Only call this after the user has explicitly confirmed the
    proposed transfer (e.g. said "yes"). Never call it just to check or propose.

    Args:
        sku: Product code: "SKU-PUMP", "SKU-VALVE" or "SKU-SENSOR".
        qty: Number of units to move. Must be a positive whole number.
        from_warehouse: Source warehouse city name (must have enough stock).
        to_warehouse: Destination warehouse city name.

    Returns:
        {"status": "ok", "shipment_id", "sku", "qty", "from", "to",
        "shipment_status": "scheduled", "route", "total_km", "eta_hours",
        "source_remaining"}, or an error if stock is insufficient.
    """
    canon_sku = _norm_sku(sku)
    if not canon_sku:
        return _unknown_sku(sku)
    src, dst = _norm_warehouse(from_warehouse), _norm_warehouse(to_warehouse)
    if not src:
        return _unknown_warehouse(from_warehouse)
    if not dst:
        return _unknown_warehouse(to_warehouse)
    if src == dst:
        return {"status": "error", "message": "Source and destination must be different warehouses."}
    if qty <= 0:
        return {"status": "error", "message": "qty must be a positive whole number."}
    have = data.INVENTORY[src].get(canon_sku, 0)
    if have < qty:
        return {"status": "error",
                "message": f"{src} only has {have} of {canon_sku}; cannot transfer {qty}."}
    route = shortest_route(src, dst)
    if route["status"] != "ok":
        return route

    data.INVENTORY[src][canon_sku] = have - qty
    sid = f"SHP-{next(data.shipment_ids)}"
    data.SHIPMENTS[sid] = {
        "sku": canon_sku, "qty": qty, "from": src, "to": dst,
        "shipment_status": "scheduled", "current_location": src,
        "route": route["path"], "total_km": route["total_km"], "eta_hours": route["est_hours"],
    }
    return {**data.SHIPMENTS[sid], "status": "ok", "shipment_id": sid,
            "source_remaining": data.INVENTORY[src][canon_sku]}
