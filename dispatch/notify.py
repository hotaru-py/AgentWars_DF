"""Tell customers when a re-plan changes their delivery (v1 parity, spec §10).

A customer is affected when their arrival moves by more than AFFECTED_S or their van changes.
Messages come from code templates (dispatch/messaging/templates.py), so times are always real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from dispatch.address.places import find as find_place
from dispatch.core.models import Customer, Incident, IncidentKind, Order, Plan
from dispatch.store import Store

AFFECTED_S = 10 * 60
KIND_PHRASE = {IncidentKind.flood: "there's flooding", IncidentKind.traffic: "there's heavy traffic",
               IncidentKind.closure: "a road is closed", IncidentKind.breakdown: "one of our vans had a problem",
               IncidentKind.other: "there's a problem on the roads"}


@dataclass
class Change:
    order_id: str
    old_eta: datetime | None
    new_eta: datetime
    van_changed: bool


def clock(ts: datetime, tz: str) -> str:
    return ts.astimezone(ZoneInfo(tz)).strftime("%I:%M %p").lstrip("0")


def changes(old: Plan | None, new: Plan) -> list[Change]:
    if old is None:
        return []
    before = {s.order_id: (r.vehicle_id, s.eta_p50) for r in old.routes for s in r.stops}
    out = []
    for r in new.routes:
        for s in r.stops:
            if s.order_id not in before:
                continue
            van, eta = before[s.order_id]
            if van != r.vehicle_id or abs((s.eta_p50 - eta).total_seconds()) > AFFECTED_S:
                out.append(Change(s.order_id, eta, s.eta_p50, van != r.vehicle_id))
    return out


def reason_for(incident: Incident | None) -> str:
    if incident is None:
        return "we've updated our delivery routes"
    phrase = KIND_PHRASE.get(incident.kind, "there's a problem on the roads")
    place = find_place(incident.description) if incident.kind != IncidentKind.breakdown else None
    return f"{phrase} near {place[0]}" if place else phrase


def notify_changes(store: Store, messenger, old: Plan | None, new: Plan, incident: Incident | None,
                   tz: str = "Asia/Kolkata") -> list[str]:
    """Send one ETA update per affected customer; returns the message ids."""
    sent = []
    reason = reason_for(incident)
    for ch in changes(old, new):
        order = store.get(Order, ch.order_id)
        cust = store.get(Customer, order.customer_id) if order else None
        if cust is None:
            continue
        ref = messenger.send_sync(cust, "eta_update", {"order_id": order.id, "reason": reason,
                                                       "eta": clock(ch.new_eta, tz)})
        sent.append(ref.id)
    return sent
