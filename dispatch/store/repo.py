"""The Store: every read and write of domain state (spec §5, §6.4).

Rules:
- Every state change is written together with its event in ONE transaction, so the event log
  can never disagree with the tables.
- Order state changes go through `transition_order`, which enforces ORDER_TRANSITIONS.
- Events are append-only and decisions immutable except their outcome; the database enforces
  both with triggers (schema.install_triggers), not just this code.

Works on Postgres (DATABASE_URL=postgresql+psycopg://...) and SQLite (default, dev/tests).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TypeVar

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from dispatch.core.decisions import DecisionRecord
from dispatch.core.events import (AttemptChecked, DecisionMade, DecisionOverridden, Event, IncidentEvent, MessageEvent,
                                  OrderConfirmRequested, OrderCreated, OrderGeocoded, OrderNoted, OrderReplied, OrderRescheduled, OrderRiskScored,
                                  OrderStateChanged, PlanApplied, PlanCreated, PolicyUpdated,
                                  StopEvent, make_event)
from dispatch.core.models import (Customer, Depot, Driver, Incident, LatLng, Model, Order, OrderState, Plan,
                                  Policy, Vehicle, can_transition, utcnow)

from . import schema as S

M = TypeVar("M", bound=Model)


def _aware(ts: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything in dispatch is UTC-aware."""
    return ts.replace(tzinfo=timezone.utc) if ts is not None and ts.tzinfo is None else ts


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


class InvalidTransition(StoreError):
    pass


class Conflict(StoreError):
    pass


# model class -> (table, extra indexed columns derived from the model)
def _cols_depot(m: Depot) -> dict:
    return {}


def _cols_vehicle(m: Vehicle) -> dict:
    return {"depot_id": m.depot_id, "state": m.state.value}


def _cols_driver(m: Driver) -> dict:
    return {"vehicle_id": m.vehicle_id}


def _cols_customer(m: Customer) -> dict:
    return {"phone": m.phone}


def _cols_order(m: Order) -> dict:
    return {"depot_id": m.depot_id, "customer_id": m.customer_id, "state": m.state.value,
            "lat": m.location.lat if m.location else None, "lng": m.location.lng if m.location else None,
            "digipin": m.digipin, "created_at": m.created_at}


def _cols_plan(m: Plan) -> dict:
    return {"depot_id": m.depot_id, "created_at": m.created_at, "parent_plan_id": m.parent_plan_id}


def _cols_incident(m: Incident) -> dict:
    return {"kind": m.kind.value, "state": m.state.value, "starts_at": m.starts_at}


TABLES: dict[type, tuple[sa.Table, object]] = {
    Depot: (S.depots, _cols_depot), Vehicle: (S.vehicles, _cols_vehicle), Driver: (S.drivers, _cols_driver),
    Customer: (S.customers, _cols_customer), Order: (S.orders, _cols_order), Plan: (S.plans, _cols_plan),
    Incident: (S.incidents, _cols_incident),
}


class Store:
    def __init__(self, url: str = "sqlite:///dispatch_dev.db", echo: bool = False):
        kwargs: dict = {"echo": echo, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = sa.create_engine(url, **kwargs)
        if self.engine.dialect.name == "sqlite":
            @sa.event.listens_for(self.engine, "connect")
            def _fk_on(dbapi_conn, _):                      # enforce foreign keys on SQLite
                dbapi_conn.execute("PRAGMA foreign_keys=ON")

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    def create_all(self) -> None:
        with self.engine.begin() as c:
            S.create_all(c)

    def migrate(self, revision: str = "head") -> None:
        """Apply Alembic migrations (production path; create_all is for tests and quick dev)."""
        import os

        from alembic import command
        from alembic.config import Config
        cfg = Config(os.path.join(os.path.dirname(__file__), "alembic.ini"))
        # configparser treats "%" as interpolation; Windows paths and passwords can contain it
        cfg.set_main_option("sqlalchemy.url", self.engine.url.render_as_string(hide_password=False).replace("%", "%%"))
        command.upgrade(cfg, revision)

    def drop_all(self) -> None:
        with self.engine.begin() as c:
            S.metadata.drop_all(c)

    @contextmanager
    def tx(self) -> Iterator[sa.Connection]:
        with self.engine.begin() as c:
            yield c

    # ------------------------------------------------------------ generic documents
    def _upsert(self, c: sa.Connection, model: Model) -> None:
        table, cols = TABLES[type(model)]
        row = {"id": model.id, "doc": model.model_dump(mode="json"), **cols(model)}
        ins = (postgresql.insert(table) if self.dialect == "postgresql" else sqlite.insert(table)).values(**row)
        c.execute(ins.on_conflict_do_update(index_elements=["id"], set_={k: v for k, v in row.items() if k != "id"}))

    def put(self, model: Model) -> None:
        """Insert or replace a reference document (depot, vehicle, driver, customer). Orders, plans,
        incidents, decisions and policies have dedicated methods that also write events."""
        if type(model) not in (Depot, Vehicle, Driver, Customer):
            raise StoreError(f"use the dedicated method to write {type(model).__name__}")
        with self.tx() as c:
            self._upsert(c, model)

    def get(self, cls: type[M], id_: str) -> M | None:
        table, _ = TABLES[cls]
        with self.engine.connect() as c:
            doc = c.execute(sa.select(table.c.doc).where(table.c.id == id_)).scalar_one_or_none()
        return cls.model_validate(doc) if doc is not None else None

    def require(self, cls: type[M], id_: str) -> M:
        m = self.get(cls, id_)
        if m is None:
            raise NotFound(f"{cls.__name__} {id_} not found")
        return m

    def list(self, cls: type[M], limit: int = 1000, **filters) -> list[M]:
        table, _ = TABLES[cls]
        q = sa.select(table.c.doc)
        for k, v in filters.items():
            q = q.where(table.c[k] == (v.value if hasattr(v, "value") else v))
        if "created_at" in table.c:
            q = q.order_by(table.c.created_at)
        with self.engine.connect() as c:
            return [cls.model_validate(d) for d in c.execute(q.limit(limit)).scalars()]

    # ------------------------------------------------------------ events
    def _append(self, c: sa.Connection, ev: Event) -> int:
        res = c.execute(sa.insert(S.events).values(ts=ev.ts, type=ev.type, entity=ev.entity, payload=ev.payload))
        return int(res.inserted_primary_key[0])

    def events_since(self, seq: int = 0, types: list[str] | None = None, entity: str | None = None,
                     limit: int = 500) -> list[Event]:
        q = sa.select(S.events).where(S.events.c.seq > seq).order_by(S.events.c.seq).limit(limit)
        if types:
            q = q.where(S.events.c.type.in_(types))
        if entity:
            q = q.where(S.events.c.entity == entity)
        with self.engine.connect() as c:
            return [Event(seq=r.seq, ts=_aware(r.ts), type=r.type, entity=r.entity, payload=r.payload)
                    for r in c.execute(q)]

    def last_seq(self) -> int:
        with self.engine.connect() as c:
            return int(c.execute(sa.select(sa.func.coalesce(sa.func.max(S.events.c.seq), 0))).scalar_one())

    # ------------------------------------------------------------ orders
    def create_order(self, order: Order) -> int:
        with self.tx() as c:
            if c.execute(sa.select(S.orders.c.id).where(S.orders.c.id == order.id)).first():
                raise Conflict(f"order {order.id} already exists")
            self._upsert(c, order)
            return self._append(c, make_event("order.created", order.id, OrderCreated(order=order)))

    def _load_order(self, c: sa.Connection, order_id: str) -> Order:
        doc = c.execute(sa.select(S.orders.c.doc).where(S.orders.c.id == order_id)).scalar_one_or_none()
        if doc is None:
            raise NotFound(f"order {order_id} not found")
        return Order.model_validate(doc)

    def transition_order(self, order_id: str, new: OrderState, reason: str = "",
                         c: sa.Connection | None = None) -> Order:
        def run(c: sa.Connection) -> Order:
            o = self._load_order(c, order_id)
            if not can_transition(o.state, new):
                raise InvalidTransition(f"order {order_id}: {o.state.value} -> {new.value} is not allowed")
            before = o.state
            o.state = new
            if new == OrderState.failed:
                o.attempts += 1
            self._upsert(c, o)
            self._append(c, make_event("order.state_changed", o.id,
                                       OrderStateChanged(order_id=o.id, before=before, after=new, reason=reason)))
            return o
        if c is not None:
            return run(c)
        with self.tx() as c2:
            return run(c2)

    def set_geocode(self, order_id: str, location: LatLng | None, digipin: str | None, confidence: float,
                    method: str) -> Order:
        with self.tx() as c:
            o = self._load_order(c, order_id)
            o.location, o.digipin, o.geo_confidence = location, digipin, confidence
            self._upsert(c, o)
            self._append(c, make_event("order.geocoded", o.id, OrderGeocoded(
                order_id=o.id, location=location, digipin=digipin, confidence=confidence, method=method)))
            if location is not None and o.state == OrderState.new:
                o = self.transition_order(o.id, OrderState.geocoded, f"geocoded by {method}", c=c)
            return o

    _STOP_STATE = {"stop.arrived": None, "stop.delivered": OrderState.delivered, "stop.failed": OrderState.failed}

    def stop_event(self, event_type: str, ev: StopEvent) -> int:
        """Driver action: record the stop event and move the order (delivered / failed) in one transaction."""
        if event_type not in self._STOP_STATE:
            raise StoreError(f"bad stop event type {event_type}")
        with self.tx() as c:
            o = self._load_order(c, ev.order_id)
            new = self._STOP_STATE[event_type]
            if new is not None:
                if o.state == OrderState.planned:          # driver acted without an explicit start
                    self.transition_order(o.id, OrderState.out_for_delivery, "driver at stop", c=c)
                self.transition_order(o.id, new, ev.reason or event_type, c=c)
            if event_type == "stop.delivered":           # customer history feeds the risk score
                doc = c.execute(sa.select(S.customers.c.doc).where(S.customers.c.id == o.customer_id)).scalar_one_or_none()
                if doc is not None:
                    cust = Customer.model_validate(doc)
                    cust.history.delivered += 1
                    self._upsert(c, cust)
            return self._append(c, make_event(event_type, o.id, ev))

    # ------------------------------------------------------------ failed-delivery prevention (spec §10)
    def set_risk(self, order_id: str, risk: float, refuse_risk: float, decision_id: str, confirm: bool,
                 holdout_weight: float | None = None) -> Order:
        with self.tx() as c:
            o = self._load_order(c, order_id)
            o.risk, o.refuse_risk = risk, refuse_risk
            self._upsert(c, o)
            self._append(c, make_event("order.risk_scored", o.id, OrderRiskScored(
                order_id=o.id, risk=risk, refuse_risk=refuse_risk, decision_id=decision_id, confirm=confirm,
                holdout_weight=holdout_weight)))
            return o

    def mark_confirm_requested(self, order_id: str, promised_at: datetime | None) -> Order:
        with self.tx() as c:
            o = self._load_order(c, order_id)
            o.confirm_requested_at, o.promised_at = datetime.now(timezone.utc), promised_at
            self._upsert(c, o)
            self._append(c, make_event("order.confirm_requested", o.id,
                                       OrderConfirmRequested(order_id=o.id, promised_at=promised_at)))
            return o

    def set_customer_reply(self, order_id: str, intent: str, message_id: str) -> Order:
        with self.tx() as c:
            o = self._load_order(c, order_id)
            o.customer_reply = intent
            self._upsert(c, o)
            self._append(c, make_event("order.replied", o.id, OrderReplied(order_id=o.id, intent=intent,
                                                                           message_id=message_id)))
            return o

    def reschedule_order(self, order_id: str, start: datetime, end: datetime, source: str) -> Order:
        """The customer's new delivery window. It replaces the old window (and deadline)."""
        with self.tx() as c:
            o = self._load_order(c, order_id)
            o = Order.model_validate({**o.model_dump(), "window_start": start, "window_end": end})   # checks the window
            self._upsert(c, o)
            self._append(c, make_event("order.rescheduled", o.id, OrderRescheduled(
                order_id=o.id, window_start=start, window_end=end, source=source[:200])))
            return o

    def record_attempt_check(self, check: AttemptChecked) -> Order:
        """Failed-attempt verify outcome: a genuine failure counts toward RTO and the customer's history
        (which the risk score reads); then the order moves to reattempt or rto."""
        with self.tx() as c:
            o = self._load_order(c, check.order_id)
            if check.genuine:
                o.verified_failures += 1
                self._upsert(c, o)
                doc = c.execute(sa.select(S.customers.c.doc).where(S.customers.c.id == o.customer_id)).scalar_one_or_none()
                if doc is not None:
                    cust = Customer.model_validate(doc)
                    cust.history.failed += 1
                    cust.history.refused += check.reason == "refused"
                    self._upsert(c, cust)
            target = OrderState.rto if check.result == "rto" else OrderState.reattempt
            o = self.transition_order(o.id, target, f"attempt check: {check.result} ({check.reason})", c=c)
            self._append(c, make_event("order.attempt_checked", o.id, check))
            return o

    # ------------------------------------------------------------ messages
    def record_message(self, event_type: str, msg: MessageEvent) -> int:
        if event_type not in ("message.sent", "message.received"):
            raise StoreError(f"bad message event type {event_type}")
        with self.tx() as c:
            if not c.execute(sa.select(S.customers.c.id).where(S.customers.c.id == msg.customer_id)).first():
                raise NotFound(f"customer {msg.customer_id} not found")
            return self._append(c, make_event(event_type, msg.message_id, msg))

    def add_order_note(self, order_id: str, note: str, source: str = "customer") -> Order:
        """Append a note for the driver (directions, "leave with neighbour"...), with its event."""
        with self.tx() as c:
            o = self._load_order(c, order_id)
            o.notes = f"{o.notes}\n{note.strip()}" if o.notes else note.strip()
            self._upsert(c, o)
            self._append(c, make_event("order.noted", o.id, OrderNoted(order_id=o.id, note=note.strip(), source=source)))
            return o

    # ------------------------------------------------------------ plans
    def save_plan(self, plan: Plan) -> int:
        with self.tx() as c:
            if c.execute(sa.select(S.plans.c.id).where(S.plans.c.id == plan.id)).first():
                raise Conflict(f"plan {plan.id} already exists; plans are immutable, save a new one")
            self._upsert(c, plan)
            return self._append(c, make_event("plan.created", plan.id, PlanCreated(plan=plan)))

    def current_plan(self, depot_id: str) -> Plan | None:
        with self.engine.connect() as c:
            doc = c.execute(sa.select(S.plans.c.doc).join(S.current_plans, S.current_plans.c.plan_id == S.plans.c.id)
                            .where(S.current_plans.c.depot_id == depot_id)).scalar_one_or_none()
        return Plan.model_validate(doc) if doc is not None else None

    def apply_plan(self, plan_id: str, decision_id: str | None = None) -> Plan:
        """Make a saved plan current for its depot and move its orders to `planned`."""
        with self.tx() as c:
            doc = c.execute(sa.select(S.plans.c.doc).where(S.plans.c.id == plan_id)).scalar_one_or_none()
            if doc is None:
                raise NotFound(f"plan {plan_id} not found")
            plan = Plan.model_validate(doc)
            prev = c.execute(sa.select(S.current_plans.c.plan_id)
                             .where(S.current_plans.c.depot_id == plan.depot_id)).scalar_one_or_none()
            ins = (postgresql.insert(S.current_plans) if self.dialect == "postgresql"
                   else sqlite.insert(S.current_plans)).values(depot_id=plan.depot_id, plan_id=plan.id)
            c.execute(ins.on_conflict_do_update(index_elements=["depot_id"], set_={"plan_id": plan.id}))
            for r in plan.routes:
                for s in r.stops:
                    o = self._load_order(c, s.order_id)
                    if o.state != OrderState.planned and can_transition(o.state, OrderState.planned):
                        self.transition_order(o.id, OrderState.planned, f"in plan {plan.id}", c=c)
            self._append(c, make_event("plan.applied", plan.id, PlanApplied(
                plan_id=plan.id, replaced_plan_id=prev, decision_id=decision_id)))
            return plan

    # ------------------------------------------------------------ incidents
    def save_incident(self, incident: Incident, event_type: str = "incident.reported") -> int:
        if event_type not in ("incident.reported", "incident.triaged", "incident.closed"):
            raise StoreError(f"bad incident event type {event_type}")
        with self.tx() as c:
            self._upsert(c, incident)
            return self._append(c, make_event(event_type, incident.id, IncidentEvent(incident=incident)))

    # ------------------------------------------------------------ decision ledger
    def record_decision(self, rec: DecisionRecord) -> int:
        with self.tx() as c:
            c.execute(sa.insert(S.decisions).values(
                id=rec.id, ts=rec.ts, type=rec.type, subject=rec.subject, decider=rec.decider,
                route=rec.route.value, confidence=rec.confidence, outcome=rec.outcome, outcome_at=rec.outcome_at,
                doc=rec.model_dump(mode="json", exclude={"outcome", "outcome_at"})))
            return self._append(c, make_event("decision.made", rec.id, DecisionMade(decision=rec)))

    def set_outcome(self, decision_id: str, outcome: str, at: datetime | None = None) -> None:
        """Record what actually happened (for calibration). Allowed once per decision."""
        with self.tx() as c:
            res = c.execute(sa.update(S.decisions).where(S.decisions.c.id == decision_id,
                                                          S.decisions.c.outcome.is_(None))
                            .values(outcome=outcome, outcome_at=at or utcnow()))
            if res.rowcount == 0:
                exists = c.execute(sa.select(S.decisions.c.id).where(S.decisions.c.id == decision_id)).first()
                raise (Conflict(f"decision {decision_id} already has an outcome") if exists
                       else NotFound(f"decision {decision_id} not found"))

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        with self.engine.connect() as c:
            r = c.execute(sa.select(S.decisions).where(S.decisions.c.id == decision_id)).first()
        if r is None:
            return None
        return DecisionRecord.model_validate({**r.doc, "outcome": r.outcome, "outcome_at": _aware(r.outcome_at)})

    def decisions(self, type_: str | None = None, route: str | None = None, limit: int = 200) -> list[DecisionRecord]:
        q = sa.select(S.decisions).order_by(S.decisions.c.ts.desc()).limit(limit)
        if type_:
            q = q.where(S.decisions.c.type == type_)
        if route:
            q = q.where(S.decisions.c.route == route)
        with self.engine.connect() as c:
            return [DecisionRecord.model_validate({**r.doc, "outcome": r.outcome, "outcome_at": _aware(r.outcome_at)})
                    for r in c.execute(q)]

    def override_decision(self, ov: DecisionOverridden) -> int:
        """A human overrides a decision. The decision row stays immutable; the override is an event."""
        with self.tx() as c:
            if not c.execute(sa.select(S.decisions.c.id).where(S.decisions.c.id == ov.decision_id)).first():
                raise NotFound(f"decision {ov.decision_id} not found")
            return self._append(c, make_event("decision.overridden", ov.decision_id, ov))

    # ------------------------------------------------------------ policies
    def policies(self) -> list[Policy]:
        """Every policy version, oldest first."""
        with self.engine.connect() as c:
            return [Policy.model_validate(d) for d in c.execute(sa.select(S.policies.c.doc).order_by(S.policies.c.version)).scalars()]

    def append_event(self, event_type: str, entity: str, payload: Model) -> int:
        """A plain event with no state change (e.g. demo.step narration)."""
        with self.tx() as c:
            return self._append(c, make_event(event_type, entity, payload))

    def record_day(self, report) -> int:
        """Append a day.simulated event (the twin's report)."""
        with self.tx() as c:
            return self._append(c, make_event("day.simulated", report.plan_id, report))

    def current_policy(self) -> Policy:
        with self.engine.connect() as c:
            doc = c.execute(sa.select(S.policies.c.doc).order_by(S.policies.c.version.desc()).limit(1)).scalar_one_or_none()
        return Policy.model_validate(doc) if doc is not None else Policy(version=1)

    def save_policy(self, policy: Policy, reason: str = "") -> int:
        """Policies are versioned, never edited: the new version must be exactly latest + 1."""
        with self.tx() as c:
            latest = c.execute(sa.select(sa.func.max(S.policies.c.version))).scalar_one() or 0
            if policy.version != latest + 1:
                raise Conflict(f"policy version must be {latest + 1}, got {policy.version}")
            c.execute(sa.insert(S.policies).values(version=policy.version, created_at=policy.created_at,
                                                   doc=policy.model_dump(mode="json")))
            return self._append(c, make_event("policy.updated", f"pol_v{policy.version}",
                                              PolicyUpdated(policy=policy, reason=reason)))
