"""In-memory mock data for the South India warehouse network.

Everything here is mutated in place by the tools (e.g. create_transfer), so a
fresh process always starts from this state. Tests snapshot and restore it.
"""

import itertools

SKUS = ["SKU-PUMP", "SKU-VALVE", "SKU-SENSOR"]

INVENTORY: dict[str, dict[str, int]] = {
    "Chennai":       {"SKU-PUMP": 40, "SKU-VALVE": 120, "SKU-SENSOR": 15},
    "Sriperumbudur": {"SKU-PUMP": 5,  "SKU-VALVE": 300, "SKU-SENSOR": 0},
    "Vellore":       {"SKU-PUMP": 0,  "SKU-VALVE": 60,  "SKU-SENSOR": 80},
    "Hosur":         {"SKU-PUMP": 90, "SKU-VALVE": 10,  "SKU-SENSOR": 25},
    "Bengaluru":     {"SKU-PUMP": 12, "SKU-VALVE": 0,   "SKU-SENSOR": 200},
    "Trichy":        {"SKU-PUMP": 30, "SKU-VALVE": 45,  "SKU-SENSOR": 5},
    "Madurai":       {"SKU-PUMP": 0,  "SKU-VALVE": 15,  "SKU-SENSOR": 60},
    "Coimbatore":    {"SKU-PUMP": 70, "SKU-VALVE": 90,  "SKU-SENSOR": 0},
}

WAREHOUSES = list(INVENTORY)

# Undirected road network, distances in km (approximate, mock).
ROADS: list[tuple[str, str, int]] = [
    ("Chennai", "Sriperumbudur", 45),
    ("Chennai", "Vellore", 140),
    ("Chennai", "Trichy", 330),
    ("Sriperumbudur", "Vellore", 95),
    ("Vellore", "Hosur", 145),
    ("Vellore", "Bengaluru", 210),
    ("Hosur", "Bengaluru", 40),
    ("Trichy", "Madurai", 135),
    ("Trichy", "Coimbatore", 215),
    ("Madurai", "Coimbatore", 215),
    ("Coimbatore", "Bengaluru", 360),
]

GRAPH: dict[str, list[tuple[str, int]]] = {}
for _a, _b, _km in ROADS:
    GRAPH.setdefault(_a, []).append((_b, _km))
    GRAPH.setdefault(_b, []).append((_a, _km))

AVG_TRUCK_KMPH = 45

# Note: the shipment's own state lives in "shipment_status", never "status",
# so merging a shipment into a tool result can't clobber {"status": "ok"}.
SHIPMENTS: dict[str, dict] = {
    "SHP-1001": {"sku": "SKU-PUMP", "qty": 20, "from": "Hosur", "to": "Chennai",
                 "shipment_status": "in_transit", "current_location": "Vellore"},
    "SHP-1002": {"sku": "SKU-SENSOR", "qty": 50, "from": "Bengaluru", "to": "Madurai",
                 "shipment_status": "delayed", "current_location": "Coimbatore",
                 "reason": "highway closure"},
}

shipment_ids = itertools.count(1003)
