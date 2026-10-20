"""Failed-delivery prevention (spec §10): score every order's risk before dispatch, ask risky
customers to confirm, and check failed attempts before a reattempt or a return.

    PreventionService(store, engine, planner, messenger, settings)
      .run(depot_id)                         score open orders; message the risky ones (plans first if needed)
      .check_attempt(order_id, stop_event)   failed-attempt verify -> reattempt / rto / disputed / human
      .resolve_attempt(order_id, genuine, reason, by)   a person decides a queued attempt
      .pending_attempts()                    failed attempts waiting for a person

Risk is a `delivery_risk` decision (chain ml -> rules; rules until a trained model exists).
An order is risky when P(fail) >= RISK_CONFIRM_THRESHOLD or P(refuses COD) >= REFUSE_THRESHOLD.
With RISK_HOLDOUT > 0, that share of risky orders is deliberately not messaged, so the `ml` provider
gets outcomes no message has changed (see dispatch/decide/ml.py).
Its customer gets the confirm_request message with the planned arrival time; replies go through
reply-intent triage (dispatch/replies.py). Until they answer, the planner keeps the stop no earlier
than the time we told them and marks it "call first" for the driver.

After a failed stop, `failed_attempt` (chain rules -> ml -> human) checks the driver's GPS distance
from the address and time at the door:
  genuine     -> counts toward the customer's history; reattempt with a message to the customer, or
                 return to origin (RTO) after MAX_ATTEMPTS genuine failures or a refused COD parcel
  not genuine -> "disputed": reattempt without counting it, with a note for ops
  unsure      -> a person decides (Needs you)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from dispatch.core.config import Settings
from dispatch.core.decisions import DecisionAction, DecisionRecord, Route
from dispatch.core.events import AttemptChecked, StopEvent
from dispatch.core.models import Customer, Depot, GateThresholds, Order, OrderState, Payment
from dispatch.decide import DecisionEngine
from dispatch.decide.catalogue import DELIVERY_RISK, FAILED_ATTEMPT
from dispatch.decide.risk import p_from_levels, risk_features
from dispatch.notify import clock
from dispatch.planning import NothingToPlan, Planner
from dispatch.routing.engines import haversine_m
from dispatch.store import Conflict, Store

REFUSE_THRESHOLD = 0.3
SCORABLE = {OrderState.geocoded, OrderState.confirmed, OrderState.planned, OrderState.reattempt}


@dataclass
class RiskItem:
    order_id: str
    customer: str
    risk: float
    refuse_risk: float
    risky: bool
    message_sent: bool
    decision_id: str


@dataclass
class PreventionRun:
    scored: list[RiskItem] = field(default_factory=list)
    planned_first: bool = False

    @property
    def messaged(self) -> list[RiskItem]:
        return [r for r in self.scored if r.message_sent]


@dataclass
class AttemptOutcome:
    order_id: str
    result: str                  # reattempt | rto | disputed | human
    genuine: bool | None
    reason: str | None
    decision_id: str | None
    distance_m: float | None = None
    dwell_s: float | None = None


class PreventionService:
    def __init__(self, store: Store, engine: DecisionEngine, planner: Planner, messenger=None,
                 settings: Settings | None = None):
        self.store, self.engine, self.planner, self.messenger = store, engine, planner, messenger
        self.settings = settings or Settings()
        self._rng = random.Random()

    # ------------------------------------------------------------ before dispatch
    async def run(self, depot_id: str) -> PreventionRun:
        self.store.require(Depot, depot_id)
        out = PreventionRun()
        plan = self.store.current_plan(depot_id)
        if plan is None:                         # the message needs an arrival time
            try:
                plan = self.planner.run(depot_id, "before risk check").plan
                out.planned_first = True
            except NothingToPlan:
                plan = None
        eta = {s.order_id: s for r in (plan.routes if plan else []) for s in r.stops}
        customers = {c.id: c for c in self.store.list(Customer, limit=100_000)}
        for o in self.store.list(Order, depot_id=depot_id, limit=100_000):
            if o.state not in SCORABLE:
                continue
            cust = customers.get(o.customer_id)
            features = risk_features(o.model_dump(mode="json"), cust.history.model_dump() if cust else {})
            state = {"order": {"payment": o.payment.value, "cod_amount": o.cod_amount, "attempts": o.attempts,
                               "notes": o.notes[:200]}, "features": features}
            res = await self.engine.decide(DELIVERY_RISK, o.id, state,
                                           action=DecisionAction(kind="score_risk", ref=o.id))
            if not res.decided:
                continue
            p_fail = p_from_levels(res.answers["will_fail"].probabilities)
            p_refuse = res.answers["refuse_risk_cod"].p if o.payment == Payment.cod else 0.0
            risky = p_fail >= self.settings.risk_confirm_threshold or p_refuse >= REFUSE_THRESHOLD
            ask = (risky and o.confirm_requested_at is None and o.customer_reply is None
                   and o.state != OrderState.confirmed and cust is not None)
            holdout = None
            if ask and self.settings.risk_holdout > 0 and self._rng.random() < self.settings.risk_holdout:
                ask, holdout = False, 1 / self.settings.risk_holdout     # exploration: an unbiased label
            self.store.set_risk(o.id, round(p_fail, 4), round(p_refuse, 4), res.record.id, confirm=ask,
                                holdout_weight=holdout)
            sent = False
            if ask and self.messenger:
                stop = eta.get(o.id)
                promised = stop.eta_p50 if stop else None
                tz = self.settings.depot_tz
                self.messenger.send_sync(cust, "confirm_request", {
                    "order_id": o.id,
                    "eta": clock(stop.eta_p50, tz) if stop else "today",
                    "eta_late": clock(stop.eta_p90, tz) if stop else "evening"})
                self.store.mark_confirm_requested(o.id, promised)
                sent = True
            out.scored.append(RiskItem(o.id, cust.name if cust else "", round(p_fail, 4), round(p_refuse, 4),
                                       risky, sent, res.record.id))
        out.scored.sort(key=lambda r: -r.risk)
        return out

    # ------------------------------------------------------------ after a failed stop
    def _evidence(self, order: Order, ev: StopEvent) -> tuple[float | None, float | None]:
        distance = (haversine_m(ev.location, order.location) if ev.location and order.location else None)
        arrived = self.store.events_since(0, types=["stop.arrived"], entity=order.id, limit=100_000)
        dwell = ev.dwell_s
        if dwell is None and arrived:
            failed = self.store.events_since(0, types=["stop.failed"], entity=order.id, limit=100_000)
            end = failed[-1].ts if failed else None
            if end is not None and end >= arrived[-1].ts:
                dwell = (end - arrived[-1].ts).total_seconds()
        return distance, dwell

    async def check_attempt(self, order_id: str, ev: StopEvent) -> AttemptOutcome:
        order = self.store.require(Order, order_id)
        distance, dwell = self._evidence(order, ev)
        state = {"distance_m": round(distance) if distance is not None else None,
                 "dwell_s": round(dwell) if dwell is not None else None, "reason_text": ev.reason}
        res = await self.engine.decide(FAILED_ATTEMPT, order_id, state,
                                       action=DecisionAction(kind="check_attempt", ref=order_id))
        if not res.decided:
            return AttemptOutcome(order_id, "human", None, None, res.record.id, distance, dwell)
        genuine = res.answers["genuine"].p >= 0.5
        out = self._apply(order, genuine, res.answers["reason"].choice, res.record.id)
        out.distance_m, out.dwell_s = distance, dwell
        return out

    def resolve_attempt(self, order_id: str, genuine: bool, reason: str, by: str) -> AttemptOutcome:
        order = self.store.require(Order, order_id)
        if order.state != OrderState.failed:
            raise Conflict(f"order {order_id} is {order.state.value}, not a failed attempt waiting for a check")
        rec = DecisionRecord(type=FAILED_ATTEMPT.type, subject=order_id, decider="human", model=by, method="human",
                             raw_confidence=1.0, confidence=1.0, gate=GateThresholds(), route=Route.act,
                             action=DecisionAction(kind="check_attempt", ref=order_id),
                             cost={"by": by, "genuine": genuine, "reason": reason})
        self.store.record_decision(rec)
        return self._apply(order, genuine, reason, rec.id)

    def pending_attempts(self) -> list[tuple[Order, DecisionRecord | None]]:
        latest: dict[str, DecisionRecord] = {}
        for d in self.store.decisions(type_=FAILED_ATTEMPT.type, limit=5000):
            latest.setdefault(d.subject, d)
        return [(o, latest[o.id]) for o in self.store.list(Order, state=OrderState.failed.value, limit=100_000)
                if o.id in latest and latest[o.id].route in (Route.human, Route.escalated)]

    def _apply(self, order: Order, genuine: bool, reason: str, decision_id: str) -> AttemptOutcome:
        if not genuine:
            result = "disputed"
        elif reason == "refused" and order.payment == Payment.cod:
            result = "rto"
        elif order.verified_failures + 1 >= self.settings.max_attempts:
            result = "rto"
        else:
            result = "reattempt"
        self.store.record_attempt_check(AttemptChecked(order_id=order.id, genuine=genuine, reason=reason,
                                                       result=result, decision_id=decision_id))
        if result == "disputed":
            self.store.add_order_note(order.id, "Attempt not verified (driver not at the address): reattempt.",
                                      source="ops")
        cust = self.store.get(Customer, order.customer_id)
        if self.messenger and cust:
            self.messenger.send_sync(cust, "returning" if result == "rto" else "reattempt_offer", {"order_id": order.id})
        return AttemptOutcome(order.id, result, genuine, reason, decision_id)
