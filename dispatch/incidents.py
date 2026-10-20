"""Incident service: reported incident -> triage decision -> active / rejected / human queue -> re-plan.

    IncidentService(store, engine, planner)
      .triage(incident_id)                   run the incident_triage decision and apply it
      .resolve(incident_id, by, ...)         a human decides (from the queue or as an override)
      .close(incident_id, by)                the incident is over: close it and re-plan

Applying a decision:
- not credible or "not_an_incident"   -> rejected (no re-plan)
- breakdown                           -> the named vehicle goes off the road; no vehicle named -> human
- road incidents (flood/traffic/...)  -> placed from the report text via the Chennai gazetteer;
                                         no place found -> human. Slowdown = 1 + (policy factor - 1) x
                                         severity weight, so the learned policy drives predictions.
- then every depot with a current plan is re-planned.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dispatch.address.places import find as find_place
from dispatch.core.decisions import (ChoiceAnswer, DecisionAction, DecisionRecord, NoulAnswer, Route, ScoreAnswer)
from dispatch.core.models import (Circle, Depot, GateThresholds, Incident, IncidentKind, IncidentState, Vehicle,
                                  VehicleState)
from dispatch.decide import INCIDENT_TRIAGE, DecisionEngine
from dispatch.notify import notify_changes
from dispatch.planning import NothingToPlan, Planner
from dispatch.store import Store, StoreError

RADIUS_M = {IncidentKind.flood: 3000, IncidentKind.traffic: 2000, IncidentKind.closure: 800, IncidentKind.other: 1500}


@dataclass
class TriageOutcome:
    incident: Incident
    result: str                          # "active" | "rejected" | "needs_location" | "needs_vehicle" | "human"
    decision_id: str | None
    replanned: dict[str, str] = field(default_factory=dict)   # depot_id -> "apply" | "hold" | "nothing to plan"
    notified: list[str] = field(default_factory=list)          # message ids of ETA updates sent to customers


def severity_weight(score: float) -> float:
    """Map severity 0..1 to how much of the policy's slowdown applies: barely noticeable 0.25x ... blocked 1.25x."""
    return 0.25 + score


class IncidentService:
    def __init__(self, store: Store, engine: DecisionEngine, planner: Planner, messenger=None, tz: str = "Asia/Kolkata"):
        self.store, self.engine, self.planner, self.messenger, self.tz = store, engine, planner, messenger, tz

    # ------------------------------------------------------------ machine triage
    async def triage(self, incident_id: str) -> TriageOutcome:
        inc = self.store.require(Incident, incident_id)
        if inc.state != IncidentState.reported:
            raise StoreError(f"incident {incident_id} is already {inc.state.value}")
        place = find_place(inc.description)
        state = {"report": {"text": inc.description, "source": inc.source,
                            "place_mentioned": place[0] if place else None,
                            "names_a_vehicle": bool(inc.vehicle_id)}}
        res = await self.engine.decide(INCIDENT_TRIAGE, inc.id, state,
                                       action=DecisionAction(kind="activate_incident", ref=inc.id))
        if not res.decided:
            return TriageOutcome(incident=inc, result="human", decision_id=res.record.id)
        kind: ChoiceAnswer = res.answers["kind"]
        sev: ScoreAnswer = res.answers["severity"]
        cred: NoulAnswer = res.answers["credible"]
        return self._apply(inc, kind.choice, sev.score, cred.value, res.record.id)

    # ------------------------------------------------------------ human decisions
    def resolve(self, incident_id: str, by: str, kind: str, severity: float, active: bool = True,
                vehicle_id: str | None = None, area: Circle | None = None) -> TriageOutcome:
        inc = self.store.require(Incident, incident_id)
        if vehicle_id:
            inc.vehicle_id = vehicle_id
        if area is not None:
            inc.area = area
        rec = DecisionRecord(type=INCIDENT_TRIAGE.type, subject=inc.id, decider="human", model=by, method="human",
                             raw_confidence=1.0, confidence=1.0, gate=GateThresholds(), route=Route.act,
                             action=DecisionAction(kind="activate_incident" if active else "reject_incident", ref=inc.id),
                             cost={"by": by, "kind": kind, "severity": severity, "active": active})
        self.store.record_decision(rec)
        return self._apply(inc, kind if active else "not_an_incident", severity, active, rec.id)

    def close(self, incident_id: str, by: str = "ops") -> TriageOutcome:
        inc = self.store.require(Incident, incident_id)
        inc.state = IncidentState.closed
        if inc.kind == IncidentKind.breakdown and inc.vehicle_id:
            v = self.store.require(Vehicle, inc.vehicle_id)
            v.state = VehicleState.active
            self.store.put(v)
        self.store.save_incident(inc, "incident.closed")
        replanned, notified = self._replan(f"incident {inc.id} closed by {by}", None)
        return TriageOutcome(incident=inc, result="closed", decision_id=None, replanned=replanned, notified=notified)

    # ------------------------------------------------------------ apply
    def _apply(self, inc: Incident, kind: str, severity: float, credible: bool, decision_id: str) -> TriageOutcome:
        if not credible or kind == "not_an_incident":
            inc.state = IncidentState.rejected
            self.store.save_incident(inc, "incident.triaged")
            return TriageOutcome(incident=inc, result="rejected", decision_id=decision_id)
        inc.kind, inc.severity = IncidentKind(kind), round(severity, 3)
        if inc.kind == IncidentKind.breakdown:
            if not inc.vehicle_id:
                return TriageOutcome(incident=inc, result="needs_vehicle", decision_id=decision_id)
            v = self.store.require(Vehicle, inc.vehicle_id)
            v.state = VehicleState.broken_down
            self.store.put(v)
        else:
            if inc.area is None:
                place = find_place(inc.description)
                if place is None:
                    return TriageOutcome(incident=inc, result="needs_location", decision_id=decision_id)
                inc.area = Circle(center=place[1], radius_m=RADIUS_M.get(inc.kind, 1500))
            factor = self.store.current_policy().delay_factor.get(inc.kind, 1.5)
            inc.slowdown = round(max(1.0, 1 + (factor - 1) * severity_weight(severity)), 3)
        inc.state = IncidentState.active
        self.store.save_incident(inc, "incident.triaged")
        replanned, notified = self._replan(f"incident {inc.id}", inc)
        return TriageOutcome(incident=inc, result="active", decision_id=decision_id, replanned=replanned, notified=notified)

    def _replan(self, reason: str, incident: Incident | None) -> tuple[dict[str, str], list[str]]:
        """Re-plan every depot that has a plan; message customers whose delivery changed."""
        out, notified = {}, []
        for d in self.store.list(Depot):
            old = self.store.current_plan(d.id)
            if old is None:
                continue
            try:
                res = self.planner.run(d.id, reason)
            except NothingToPlan:
                out[d.id] = "nothing to plan"
                continue
            out[d.id] = res.verdict
            if res.verdict == "apply" and self.messenger is not None:
                notified += notify_changes(self.store, self.messenger, old, res.plan, incident, self.tz)
        return out, notified
