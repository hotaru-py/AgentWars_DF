"""Chennai demo data for v2 (the v1 scenario: Guindy depot, 3 vans, 10 orders).

    python -m dispatch.demo seed     load the demo into an empty database (no-op if a depot exists)
    python -m dispatch.demo reset    wipe the database and load a fresh demo day

The demo day starts "now": each order's deadline is v1's minutes-after-start counted from now,
so the demo works at any time of day. Orders arrive already geocoded (they carry coordinates).
"""

from __future__ import annotations

import sys
from datetime import timedelta

from dispatch.core.config import load
from dispatch.core.models import Customer, CustomerHistory, Depot, LatLng, Order, Payment, Vehicle, utcnow
from dispatch.store import Store

DEPOT = {"name": "Guindy depot", "lat": 13.0067, "lng": 80.2206}
VANS = [("V1", "Ganesh"), ("V2", "Farhan"), ("V3", "Selvi")]
# (customer, phone, area, lat, lng, deadline minutes from start, priority, kg, payment)
ORDERS = [
    ("Arun Kumar", "+91-90000-00001", "T. Nagar", 13.0418, 80.2341, 90, "normal", 4, "prepaid"),
    ("Divya Raman", "+91-90000-00002", "Anna Nagar", 13.0850, 80.2101, 120, "urgent", 6, "cod"),
    ("Karthik S", "+91-90000-00003", "Adyar", 13.0012, 80.2565, 100, "normal", 3, "prepaid"),
    ("Meena Iyer", "+91-90000-00004", "Velachery", 12.9815, 80.2180, 80, "normal", 8, "cod"),
    ("Rahul Nair", "+91-90000-00005", "Tambaram", 12.9249, 80.1000, 150, "normal", 12, "prepaid"),
    ("Priya Venkat", "+91-90000-00006", "Sholinganallur (OMR)", 12.9010, 80.2279, 140, "urgent", 5, "prepaid"),
    ("Suresh Babu", "+91-90000-00007", "Porur", 13.0382, 80.1565, 110, "normal", 7, "cod"),
    ("Lakshmi P", "+91-90000-00008", "Mylapore", 13.0368, 80.2676, 130, "normal", 2, "prepaid"),
    ("Vignesh R", "+91-90000-00009", "Egmore", 13.0732, 80.2609, 120, "normal", 5, "prepaid"),
    ("Anitha Joseph", "+91-90000-00010", "Thiruvanmiyur (OMR)", 12.9830, 80.2594, 100, "normal", 4, "cod"),
]
# Customer history (delivered, failed, refused), address confidence and COD amount, so the risk check
# (spec §10) has a realistic mix: regulars, a first-timer on a vague address, past failures and a refusal.
PROFILE = {
    "Arun Kumar": ((8, 0, 0), 1.0, 0), "Divya Raman": ((3, 2, 0), 1.0, 499), "Karthik S": ((5, 0, 0), 1.0, 0),
    "Meena Iyer": ((10, 0, 0), 1.0, 499), "Rahul Nair": ((0, 0, 0), 0.6, 0), "Priya Venkat": ((12, 0, 0), 1.0, 0),
    "Suresh Babu": ((0, 0, 0), 0.9, 2499), "Lakshmi P": ((4, 0, 0), 1.0, 0), "Vignesh R": ((1, 1, 0), 1.0, 0),
    "Anitha Joseph": ((2, 1, 1), 1.0, 899),
}


def seed(store: Store) -> Depot | None:
    """Load the demo if the database has no depot yet. Returns the new depot, or None if skipped."""
    if store.list(Depot, limit=1):
        return None
    start = utcnow().replace(second=0, microsecond=0)
    depot = Depot(name=DEPOT["name"], location=LatLng(lat=DEPOT["lat"], lng=DEPOT["lng"]),
                  opens_at=start, closes_at=start + timedelta(hours=10))
    store.put(depot)
    for name, driver in VANS:
        store.put(Vehicle(depot_id=depot.id, name=f"{name} · {driver}", capacity_kg=50, capacity_l=400))
    for cust, phone, area, lat, lng, minutes, prio, kg, pay in ORDERS:
        (delivered, failed, refused), geo, cod_amount = PROFILE[cust]
        c = Customer(name=cust, phone=phone,
                     history=CustomerHistory(delivered=delivered, failed=failed, refused=refused))
        store.put(c)
        o = Order(depot_id=depot.id, customer_id=c.id, raw_address=f"{area}, Chennai", priority=prio, weight_kg=kg,
                  payment=Payment(pay), cod_amount=cod_amount if pay == "cod" else 0,
                  window_end=start + timedelta(minutes=minutes), notes=area)
        store.create_order(o)
        store.set_geocode(o.id, LatLng(lat=lat, lng=lng), None, geo, "demo")
    return depot


def reset(store: Store, keep_learning: bool = True) -> Depot:
    """Wipe the day (tables, events, decisions) and load a fresh demo day. By default the learned
    policy versions survive, so repeated demo days show the system getting better."""
    kept = store.policies() if keep_learning else []
    store.drop_all()
    store.create_all()
    for p in kept:
        store.save_policy(p, reason="kept across demo reset")
    return seed(store)


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "seed"
    store = Store(load().database_url)
    store.migrate()
    if cmd == "seed":
        d = seed(store)
        print(f"Demo loaded: {d.name} ({d.id})" if d else "Database already has data; demo not loaded.")
    elif cmd == "reset":
        d = reset(store, keep_learning=False)          # --fresh: forget everything, learning included
        print(f"Database reset; demo loaded: {d.name} ({d.id})")
    else:
        print(__doc__)
        return 2
    store.engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
