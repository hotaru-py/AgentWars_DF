"""Customer replies: inbound message -> reply_intent decision -> action (spec §10).

    ReplyService(store, engine, planner, messenger)
      .handle(message)                 decide and apply (rules for "1/2/3/yes", the LLM otherwise)
      .resolve(message_id, intent, by) a person decides (from the queue or as an override)
      .pending()                       replies waiting for a person

Actions:
  confirm              -> order confirmed (if it isn't already planned or further along)
  cancel               -> order cancelled, customer told, depot re-planned
  deliver_to_neighbour -> the reply is added to the order's notes for the driver; customer thanked
  give_directions      -> same
  reschedule           -> reschedule_slot decision (rules parse "after 6 pm", "tomorrow morning"; the LLM
                          reads vaguer text): the window moves, the customer is told, the depot re-plans.
                          A day after the depot closes takes the order out of today's plan. No usable
                          time -> a person (who can type the time: resolve(..., when="tomorrow 10am"))
  other                -> a person handles it
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from dispatch.core.decisions import DecisionAction, DecisionRecord, Route
from dispatch.core.events import MessageEvent
from dispatch.core.models import Customer, GateThresholds, Order, OrderState, can_transition
from dispatch.decide import REPLY_INTENT, RESCHEDULE_SLOT, DecisionEngine
from dispatch.when import When, parse_when, slot_window
from dispatch.planning import NothingToPlan, Planner
from dispatch.store import Store

FINAL = {OrderState.delivered, OrderState.cancelled, OrderState.rto}
NEEDS_PERSON = {"reschedule", "other"}        # unless a reschedule found its time (see pending())


@dataclass
class ReplyOutcome:
    message_id: str
    order_id: str | None
    intent: str | None
    result: str          # "confirmed" | "cancelled" | "noted" | "rescheduled" | "needs_person" | "no_order" | "already_final"
    decision_id: str | None
    when: str | None = None              # the new window in words, when rescheduled


class ReplyService:
    def __init__(self, store: Store, engine: DecisionEngine, planner: Planner, messenger=None, tz: str = "Asia/Kolkata"):
        self.store, self.engine, self.planner, self.messenger = store, engine, planner, messenger
        self.tz = tz

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.tz))

    # ------------------------------------------------------------ helpers
    def _order_for(self, msg: MessageEvent) -> Order | None:
        if msg.order_id:
            return self.store.get(Order, msg.order_id)
        open_orders = [o for o in self.store.list(Order, customer_id=msg.customer_id) if o.state not in FINAL]
        return open_orders[-1] if open_orders else None

    def _last_sent(self, customer_id: str) -> str | None:
        sent = [e for e in self.store.events_since(0, types=["message.sent"], limit=100_000)
                if e.payload.get("customer_id") == customer_id]
        return sent[-1].payload["text"] if sent else None

    # ------------------------------------------------------------ machine
    async def handle(self, msg: MessageEvent) -> ReplyOutcome:
        order = self._order_for(msg)
        state = {"reply": {"text": msg.text, "channel": msg.channel},
                 "our_last_message": self._last_sent(msg.customer_id),
                 "order": {"area": order.notes.split("\n")[0] if order else None,
                           "state": order.state.value if order else None}}
        res = await self.engine.decide(REPLY_INTENT, msg.message_id, state,
                                       action=DecisionAction(kind="apply_reply", ref=order.id if order else None))
        intent = res.answers["intent"].choice if res.decided else None
        if intent is None:
            return ReplyOutcome(msg.message_id, order.id if order else None, None, "needs_person", res.record.id)
        when = None
        if intent == "reschedule" and order is not None and order.state not in FINAL:
            now = self._now()
            slot = await self.engine.decide(RESCHEDULE_SLOT, msg.message_id,
                                            {"reply": {"text": msg.text}, "now": now.isoformat()},
                                            action=DecisionAction(kind="reschedule", ref=order.id))
            if slot.decided:                      # exact window from the text if it parses, else the slot's
                when = parse_when(msg.text, now) or slot_window(slot.answers["slot"].choice, now)
        return self._apply(msg, order, intent, res.record.id, when)

    # ------------------------------------------------------------ person
    def resolve(self, message_id: str, intent: str, by: str, when: str | None = None) -> ReplyOutcome:
        """A person decides. For a reschedule they can type the time ("tomorrow 10am"); without one
        (or if it doesn't parse) they handle it themselves."""
        msg = self._message(message_id)
        window = parse_when(when, self._now()) if (intent == "reschedule" and when) else None
        if intent == "reschedule" and when and window is None:
            from dispatch.store import StoreError
            raise StoreError(f"couldn't read a time from {when!r}: try e.g. 'tomorrow 10am' or 'after 6 pm'")
        rec = DecisionRecord(type=REPLY_INTENT.type, subject=message_id, decider="human", model=by, method="human",
                             raw_confidence=1.0, confidence=1.0, gate=GateThresholds(), route=Route.act,
                             action=DecisionAction(kind="apply_reply"), cost={"by": by, "intent": intent})
        self.store.record_decision(rec)
        out = self._apply(msg, self._order_for(msg), intent, rec.id, window)
        if out.result == "needs_person":            # a person chose reschedule/other: they're handling it
            out.result = "handled_by_person"
        return out

    def _message(self, message_id: str) -> MessageEvent:
        evs = self.store.events_since(0, types=["message.received"], entity=message_id, limit=1)
        if not evs:
            from dispatch.store import NotFound
            raise NotFound(f"message {message_id} not found")
        return MessageEvent.model_validate(evs[0].payload)

    def pending(self) -> list[tuple[MessageEvent, DecisionRecord | None]]:
        latest: dict[str, DecisionRecord] = {}
        for d in self.store.decisions(type_=REPLY_INTENT.type, limit=5000):
            latest.setdefault(d.subject, d)          # newest first
        rescheduled = {d.subject for d in self.store.decisions(type_=RESCHEDULE_SLOT.type, limit=5000)
                       if d.route in (Route.act, Route.review) and d.answers["slot"].choice != "unclear"}
        out = []
        for e in self.store.events_since(0, types=["message.received"], limit=100_000):
            d = latest.get(e.entity)
            if d is None:
                continue                             # still being decided
            if d.decider == "human" and d.route != Route.human:
                continue                             # a person resolved it (route act); "human"+"human" = waiting
            intent = d.answers["intent"].choice if "intent" in d.answers else None
            if intent == "reschedule" and e.entity in rescheduled:
                continue                             # its time was found and applied
            if d.route in (Route.human, Route.escalated) or intent in NEEDS_PERSON:
                out.append((MessageEvent.model_validate(e.payload), d))
        return out

    # ------------------------------------------------------------ apply
    def _apply(self, msg: MessageEvent, order: Order | None, intent: str, decision_id: str,
               when: When | None = None) -> ReplyOutcome:
        oid = order.id if order else None
        if intent in NEEDS_PERSON and not (intent == "reschedule" and when is not None):
            return ReplyOutcome(msg.message_id, oid, intent, "needs_person", decision_id)
        if order is None:
            return ReplyOutcome(msg.message_id, None, intent, "no_order", decision_id)
        if order.state in FINAL:
            return ReplyOutcome(msg.message_id, oid, intent, "already_final", decision_id)
        cust = self.store.get(Customer, order.customer_id)
        self.store.set_customer_reply(order.id, intent, msg.message_id)
        if intent == "reschedule":
            self.store.reschedule_order(order.id, when.start, when.end, source=msg.text)
            if self.messenger and cust:
                self.messenger.send_sync(cust, "rescheduled", {"order_id": order.id, "when": when.describe()})
            self._replan(order, f"order {order.id} rescheduled by customer")
            return ReplyOutcome(msg.message_id, oid, intent, "rescheduled", decision_id, when.describe())
        if intent == "confirm":
            if can_transition(order.state, OrderState.confirmed) and order.state != OrderState.planned:
                self.store.transition_order(order.id, OrderState.confirmed, "customer confirmed")
            return ReplyOutcome(msg.message_id, oid, intent, "confirmed", decision_id)
        if intent == "cancel":
            self.store.transition_order(order.id, OrderState.cancelled, f"customer: {msg.text[:120]}")
            if self.messenger and cust:
                self.messenger.send_sync(cust, "cancelled", {"order_id": order.id})
            self._replan(order, f"order {order.id} cancelled by customer")
            return ReplyOutcome(msg.message_id, oid, intent, "cancelled", decision_id)
        label = "Leave with" if intent == "deliver_to_neighbour" else "Directions"
        self.store.add_order_note(order.id, f"{label} (customer): {msg.text[:300]}", source="customer")
        if self.messenger and cust:
            self.messenger.send_sync(cust, "noted", {"order_id": order.id})
        return ReplyOutcome(msg.message_id, oid, intent, "noted", decision_id)

    def _replan(self, order: Order, reason: str) -> None:
        if self.store.current_plan(order.depot_id):
            try:
                self.planner.run(order.depot_id, reason)
            except NothingToPlan:
                pass
