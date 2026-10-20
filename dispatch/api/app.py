"""HTTP API (spec §12). Run: python -m dispatch.api   (http://127.0.0.1:8080, docs at /docs)

`create_app(store, solver)` builds the app, so tests and the agent can inject their own store.
Endpoints are plain `def` (FastAPI runs them in a thread pool) because the Store is synchronous.
The live stream is Server-Sent Events over the append-only event log; clients resume with the
standard `Last-Event-ID` header (or `?after=`).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from datetime import datetime
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field

from dispatch import __version__
from dispatch.core.config import Settings, load
from dispatch.core.decisions import DecisionRecord
from dispatch.core.events import DecisionOverridden, Event, MessageEvent, StopEvent
from dispatch.core.ids import new_id
from dispatch.core.models import (Area, Circle, Customer, Depot, Driver, Incident, IncidentKind, IncidentState, LatLng,
                                  Model, Order,
                                  OrderKind, OrderState, Payment, Plan, Policy, Vehicle, utcnow)
from dispatch import demo
from dispatch.agent import ChatService, ToolContext
from dispatch.decide import DecisionEngine, default_providers
from dispatch.incidents import IncidentService
from dispatch.messaging import SimMessenger
from dispatch.replies import ReplyService
from dispatch.optimize import OrToolsSolver
from dispatch.planning import Planner
from dispatch.prevention import PreventionService
from dispatch.routing import default_engine
from dispatch.store import Conflict, InvalidTransition, NotFound, Store, StoreError
from dispatch.twin import run_day
from dispatch.twin.prevention import prevention_report

STREAM_POLL_S = 0.5


# ---------------------------------------------------------------- request bodies
class OrderIn(Model):
    depot_id: str
    customer_id: str
    raw_address: str = Field(min_length=3)
    kind: OrderKind = OrderKind.delivery
    location: LatLng | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    weight_kg: float = Field(default=1.0, ge=0)
    volume_l: float = Field(default=1.0, ge=0)
    payment: Payment = Payment.prepaid
    cod_amount: float = Field(default=0, ge=0)
    priority: Literal["normal", "urgent"] = "normal"
    notes: str = ""


class ImportResult(Model):
    created: list[str]
    errors: list[dict[str, Any]]


class StopActionIn(Model):
    vehicle_id: str
    location: LatLng | None = None
    reason: str = ""
    photo_ref: str | None = None
    dwell_s: float | None = Field(default=None, ge=0)   # time at the door; else measured from /arrive


class IncidentIn(Model):
    kind: IncidentKind = IncidentKind.other
    description: str = Field(min_length=3)
    area: Area | None = None
    vehicle_id: str | None = None
    source: str = "ops"


class MessageIn(Model):
    customer_id: str
    order_id: str | None = None
    channel: Literal["whatsapp", "sms", "voice", "sim"] = "whatsapp"
    text: str = Field(min_length=1, max_length=4000)


class OverrideIn(Model):
    by: str = Field(min_length=1)
    note: str = ""
    new_action: dict[str, Any] = {}


class Accepted(Model):
    seq: int


class TriageOut(Model):
    incident: Incident
    result: str
    decision_id: str | None
    replanned: dict[str, str] = {}
    notified: list[str] = []


class ReplyOut(Model):
    message_id: str
    order_id: str | None
    intent: str | None
    result: str
    decision_id: str | None
    when: str | None = None              # the new delivery window in words, when rescheduled


class ChatIn(Model):
    session_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_\-]+$")
    text: str = Field(min_length=1, max_length=2000)


class ReplyResolveIn(Model):
    by: str = Field(min_length=1)
    intent: Literal["confirm", "reschedule", "cancel", "deliver_to_neighbour", "give_directions", "other"]
    when: str | None = Field(default=None, max_length=200)    # reschedule: e.g. "tomorrow 10am"


class ResolveIn(Model):
    by: str = Field(min_length=1)
    kind: IncidentKind
    severity: float = Field(ge=0, le=1)
    active: bool = True
    vehicle_id: str | None = None
    area: Circle | None = None


class QueueItem(Model):
    kind: Literal["incident", "message", "attempt"]
    incident: Incident | None = None
    message: MessageEvent | None = None
    order: Order | None = None
    customer: str | None = None
    decision: DecisionRecord | None


class FailOut(Model):
    seq: int
    result: str                       # reattempt | rto | disputed | human (failed-attempt verify)
    genuine: bool | None
    reason: str | None
    decision_id: str | None
    distance_m: float | None
    dwell_s: float | None


class AttemptResolveIn(Model):
    genuine: bool
    reason: Literal["not_home", "refused", "wrong_address", "unsafe", "other"] = "other"
    by: str = "ops"


class RiskItemOut(Model):
    order_id: str
    customer: str
    risk: float
    refuse_risk: float
    risky: bool
    message_sent: bool
    decision_id: str


class PreventionRunOut(Model):
    planned_first: bool
    scored: list[RiskItemOut]
    messaged: int


class PlanRunOut(Model):
    verdict: Literal["apply", "hold"]
    plan: Plan | None
    decision_id: str
    candidates: list[dict[str, Any]]
    baseline: dict[str, Any] | None
    skipped_orders: list[str]
    timings_ms: dict[str, int]


CSV_COLUMNS = ["customer_name", "phone", "address", "weight_kg", "payment", "cod_amount", "priority",
               "window_start", "window_end", "lat", "lng", "notes"]


# ---------------------------------------------------------------- app
def create_app(store: Store | None = None, planner: Planner | None = None, settings: Settings | None = None,
               engine: DecisionEngine | None = None, chat_runner_factory=None) -> FastAPI:
    settings = settings or load()
    if store is None:
        store = Store(settings.database_url)
        store.migrate()
    app = FastAPI(title="Dispatch v2", version=__version__,
                  description="Last-mile dispatch with confidence-gated autonomy. See specs/v2-spec.md.")
    if planner is None:
        planner = Planner(store, default_engine(settings.osrm_url, settings.osrm_time_factor), OrToolsSolver(),
                          time_limit_s=settings.plan_time_limit_s)
    if engine is None:
        engine = DecisionEngine(store, default_providers(settings), settings)
    messenger = SimMessenger(store)
    incidents = IncidentService(store, engine, planner, messenger, settings.depot_tz)
    replies = ReplyService(store, engine, planner, messenger, settings.depot_tz)
    prevention = PreventionService(store, engine, planner, messenger, settings)
    chat = ChatService(ToolContext(store, planner, incidents, replies, settings.depot_tz, prevention), settings,
                       chat_runner_factory)
    app.state.store, app.state.planner, app.state.engine, app.state.incidents = store, planner, engine, incidents
    app.state.prevention = prevention
    origins = [o.strip() for o in os.environ.get(
        "CORS_ORIGINS", "http://localhost:8001,http://127.0.0.1:8001").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])
    demo_mode = os.environ.get("DEMO_MODE", "").strip().lower() in ("1", "true", "yes")

    @app.get("/state", tags=["meta"])
    def snapshot(depot_id: str | None = None) -> dict:
        """Everything the console draws, in one call: depot, vehicles, orders (with customer names),
        current plan, open incidents, the latest plan comparison, queue size and provider status."""
        depots = store.list(Depot)
        depot = next((d for d in depots if d.id == depot_id), depots[0] if depots else None)
        if depot is None:
            return {"depot": None}
        orders = store.list(Order, depot_id=depot.id, limit=10_000)
        customers = {c.id: c for c in store.list(Customer, limit=100_000)}
        incidents = [i for i in store.list(Incident) if i.state in (IncidentState.reported, IncidentState.active)]
        plan_dec = next(iter(store.decisions(type_="plan_select", limit=1)), None)
        return {
            "depot": depot.model_dump(mode="json"), "tz": settings.depot_tz,
            "vehicles": [v.model_dump(mode="json") for v in store.list(Vehicle, depot_id=depot.id)],
            "orders": [{**o.model_dump(mode="json"), "customer": customers[o.customer_id].name
                        if o.customer_id in customers else None} for o in orders],
            "plan": (p.model_dump(mode="json") if (p := store.current_plan(depot.id)) else None),
            "incidents": [i.model_dump(mode="json") for i in incidents],
            "plan_decision": plan_dec.model_dump(mode="json") if plan_dec else None,
            "queue": sum(1 for i in incidents if i.state == IncidentState.reported),
            "policy": store.current_policy().model_dump(mode="json"),
            "deciders": sorted(engine.providers), "llm": settings.gemini_enabled, "jev": settings.jev_enabled,
            "routing": planner.engine.name, "demo": demo_mode, "last_seq": store.last_seq(),
            "risk_threshold": settings.risk_confirm_threshold,
        }

    if demo_mode:
        @app.post("/demo/reset", tags=["demo"])
        def demo_reset(forget: bool = False) -> dict:
            """Demo only (DEMO_MODE=1): wipe the day and load a fresh Chennai demo day. What the system
            learned (policy versions) is kept unless forget=true."""
            d = demo.reset(store, keep_learning=not forget)
            return {"depot_id": d.id}

    @app.exception_handler(StoreError)
    def _store_errors(_: Request, e: StoreError):
        code = 404 if isinstance(e, NotFound) else 409 if isinstance(e, (Conflict, InvalidTransition)) else 422
        return JSONResponse(status_code=code, content={"detail": str(e)})

    # ------------------------------------------------------------ health
    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"ok": True, "version": __version__, "db": store.dialect, "last_seq": store.last_seq(),
                "routing": planner.engine.name, "deciders": sorted(engine.providers), "jev": settings.jev_enabled,
                "llm": settings.gemini_enabled}

    # ------------------------------------------------------------ reference data
    @app.post("/depots", response_model=Depot, status_code=201, tags=["fleet"])
    def put_depot(d: Depot) -> Depot:
        store.put(d)
        return d

    @app.get("/depots", response_model=list[Depot], tags=["fleet"])
    def list_depots() -> list[Depot]:
        return store.list(Depot)

    @app.post("/vehicles", response_model=Vehicle, status_code=201, tags=["fleet"])
    def put_vehicle(v: Vehicle) -> Vehicle:
        store.require(Depot, v.depot_id)
        store.put(v)
        return v

    @app.get("/vehicles", response_model=list[Vehicle], tags=["fleet"])
    def list_vehicles(depot_id: str | None = None) -> list[Vehicle]:
        return store.list(Vehicle, **({"depot_id": depot_id} if depot_id else {}))

    @app.post("/drivers", response_model=Driver, status_code=201, tags=["fleet"])
    def put_driver(d: Driver) -> Driver:
        store.put(d)
        return d

    @app.post("/customers", response_model=Customer, status_code=201, tags=["customers"])
    def put_customer(c: Customer) -> Customer:
        store.put(c)
        return c

    @app.get("/customers/{customer_id}", response_model=Customer, tags=["customers"])
    def get_customer(customer_id: str) -> Customer:
        return store.require(Customer, customer_id)

    # ------------------------------------------------------------ orders
    @app.post("/orders", response_model=Order, status_code=201, tags=["orders"])
    def create_order(body: OrderIn) -> Order:
        store.require(Depot, body.depot_id)
        store.require(Customer, body.customer_id)
        o = Order(**body.model_dump())
        store.create_order(o)
        return o

    @app.post("/orders/import", response_model=ImportResult, tags=["orders"])
    def import_orders(depot_id: str, file: UploadFile = File(...)) -> ImportResult:
        """CSV with a header row. Required: customer_name, phone, address. Optional: weight_kg, payment
        (prepaid|cod), cod_amount, priority (normal|urgent), window_start, window_end (ISO 8601), lat, lng, notes.
        Customers are matched by phone. Bad rows are reported and skipped; good rows are imported."""
        store.require(Depot, depot_id)
        text = file.file.read().decode("utf-8-sig")
        created, errors = [], []
        for n, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):     # line 1 is the header
            try:
                row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
                missing = [k for k in ("customer_name", "phone", "address") if not row.get(k)]
                if missing:
                    raise ValueError(f"missing {', '.join(missing)}")
                existing = store.list(Customer, phone=row["phone"], limit=1)
                cust = existing[0] if existing else Customer(name=row["customer_name"], phone=row["phone"])
                if not existing:
                    store.put(cust)
                o = Order(depot_id=depot_id, customer_id=cust.id, raw_address=row["address"],
                          weight_kg=float(row.get("weight_kg") or 1), payment=row.get("payment") or "prepaid",
                          cod_amount=float(row.get("cod_amount") or 0), priority=row.get("priority") or "normal",
                          window_start=row.get("window_start") or None, window_end=row.get("window_end") or None,
                          location=LatLng(lat=float(row["lat"]), lng=float(row["lng"])) if row.get("lat") and row.get("lng") else None,
                          notes=row.get("notes", ""))
                store.create_order(o)
                if o.location is not None:            # coordinates given: treat as geocoded (phase 4 adds real geocoding)
                    store.set_geocode(o.id, o.location, None, 1.0, "csv")
                created.append(o.id)
            except (ValueError, StoreError) as e:
                errors.append({"line": n, "error": str(e).splitlines()[0][:300]})
        return ImportResult(created=created, errors=errors)

    @app.get("/orders", response_model=list[Order], tags=["orders"])
    def list_orders(depot_id: str | None = None, state: OrderState | None = None,
                    limit: int = Query(500, ge=1, le=5000)) -> list[Order]:
        f = {k: v for k, v in (("depot_id", depot_id), ("state", state)) if v is not None}
        return store.list(Order, limit=limit, **f)

    @app.get("/orders/{order_id}", response_model=Order, tags=["orders"])
    def get_order(order_id: str) -> Order:
        return store.require(Order, order_id)

    @app.post("/orders/{order_id}/cancel", response_model=Order, tags=["orders"])
    def cancel_order(order_id: str, reason: str = "") -> Order:
        return store.transition_order(order_id, OrderState.cancelled, reason or "cancelled via API")

    # ------------------------------------------------------------ driver actions
    def _stop(order_id: str, body: StopActionIn, kind: str) -> Accepted:
        store.require(Vehicle, body.vehicle_id)
        return Accepted(seq=store.stop_event(kind, StopEvent(order_id=order_id, **body.model_dump())))

    @app.post("/driver/stops/{order_id}/arrive", response_model=Accepted, tags=["driver"])
    def arrive(order_id: str, body: StopActionIn) -> Accepted:
        return _stop(order_id, body, "stop.arrived")

    @app.post("/driver/stops/{order_id}/deliver", response_model=Accepted, tags=["driver"])
    def deliver(order_id: str, body: StopActionIn) -> Accepted:
        return _stop(order_id, body, "stop.delivered")

    @app.post("/driver/stops/{order_id}/fail", response_model=FailOut, tags=["driver"])
    async def fail(order_id: str, body: StopActionIn) -> FailOut:
        """A failed attempt. It is checked straight away (failed_attempt decision: GPS distance from the
        address and time since `arrive`): genuine -> reattempt, or return to origin after MAX_ATTEMPTS
        or a refused COD parcel; driver not at the address -> disputed (reattempt, not counted);
        unclear -> waits for a person in /queue."""
        if not body.reason:
            raise HTTPException(422, "a failed stop needs a reason")
        ev = StopEvent(order_id=order_id, **body.model_dump())
        seq = (await asyncio.to_thread(_stop, order_id, body, "stop.failed")).seq
        out = await prevention.check_attempt(order_id, ev)
        return FailOut(seq=seq, result=out.result, genuine=out.genuine, reason=out.reason,
                       decision_id=out.decision_id, distance_m=out.distance_m, dwell_s=out.dwell_s)

    @app.post("/orders/{order_id}/attempt/resolve", response_model=FailOut, tags=["driver"])
    def resolve_attempt(order_id: str, body: AttemptResolveIn) -> FailOut:
        """A person decides a failed attempt waiting in the queue (or overrides the check)."""
        out = prevention.resolve_attempt(order_id, body.genuine, body.reason, body.by)
        return FailOut(seq=store.last_seq(), result=out.result, genuine=out.genuine, reason=out.reason,
                       decision_id=out.decision_id, distance_m=None, dwell_s=None)

    # ------------------------------------------------------------ failed-delivery prevention
    @app.post("/prevention/run", response_model=PreventionRunOut, tags=["prevention"])
    async def prevention_run(depot_id: str) -> PreventionRunOut:
        """Score every open order's risk of a failed delivery and ask the risky customers to confirm
        (plans first if there is no plan, so the message can give an arrival time). Unconfirmed risky
        orders are then planned no earlier than the time we told them, with a "call first" note."""
        r = await prevention.run(depot_id)
        return PreventionRunOut(planned_first=r.planned_first, messaged=len(r.messaged),
                                scored=[RiskItemOut(**vars(x)) for x in r.scored])

    @app.get("/ml/status", tags=["decisions"])
    def ml_status() -> dict:
        """The trained `ml` provider per decision type: labelled outcomes so far, the check on recent
        outcomes (AUC vs the rules), and whether the model is live or why not."""
        return engine.ml.status()

    @app.get("/twin/prevention", tags=["twin"])
    def twin_prevention(days: int = Query(30, ge=1, le=365), orders_per_day: int = Query(60, ge=1, le=1000),
                        seed: int = 7, threshold: float | None = Query(None, ge=0, le=1)) -> dict:
        """Replay synthetic days with and without prevention (same customers, same random draws) and
        report failed deliveries per policy: none, risk-targeted (Dispatch) and confirm-everyone."""
        t = settings.risk_confirm_threshold if threshold is None else threshold
        return prevention_report(days=days, orders_per_day=orders_per_day, seed=seed, threshold=t)

    # ------------------------------------------------------------ plans
    @app.get("/plan/current", response_model=Plan | None, tags=["plans"])
    def current_plan(depot_id: str) -> Plan | None:
        store.require(Depot, depot_id)
        return store.current_plan(depot_id)

    @app.get("/plans/{plan_id}", response_model=Plan, tags=["plans"])
    def get_plan(plan_id: str) -> Plan:
        return store.require(Plan, plan_id)

    @app.post("/plan/run", response_model=PlanRunOut, tags=["plans"])
    def run_plan(depot_id: str, reason: str = "manual") -> PlanRunOut:
        """Plan (or re-plan) the depot now: 3 candidate strategies, scored by the critic against the
        current plan under current conditions. Applies the best, or holds. Logged in the decision ledger."""
        r = planner.run(depot_id, reason)
        return PlanRunOut(verdict=r.verdict, plan=r.plan, decision_id=r.decision.id, candidates=r.candidates,
                          baseline=r.baseline, skipped_orders=r.skipped_orders, timings_ms=r.timings_ms)

    # ------------------------------------------------------------ incidents
    def _triage_in_background(incident_id: str) -> None:
        try:
            asyncio.run(incidents.triage(incident_id))
        except StoreError:
            pass                                    # already triaged or resolved meanwhile

    def _out(o) -> TriageOut:
        return TriageOut(incident=o.incident, result=o.result, decision_id=o.decision_id, replanned=o.replanned,
                         notified=o.notified)

    @app.post("/incidents", response_model=Incident, status_code=201, tags=["incidents"])
    def report_incident(body: IncidentIn, background: BackgroundTasks, triage: bool = True) -> Incident:
        """Record a reported incident. By default it's triaged in the background (decision layer):
        it becomes active (and the plan is re-run), rejected, or waits in the human queue. Watch /stream."""
        if body.vehicle_id:
            store.require(Vehicle, body.vehicle_id)
        inc = Incident(**body.model_dump())
        store.save_incident(inc, "incident.reported")
        if triage:
            background.add_task(_triage_in_background, inc.id)
        return inc

    @app.post("/incidents/{incident_id}/triage", response_model=TriageOut, tags=["incidents"])
    async def triage_incident(incident_id: str) -> TriageOut:
        """Run triage now and wait for the result."""
        return _out(await incidents.triage(incident_id))

    @app.post("/incidents/{incident_id}/resolve", response_model=TriageOut, tags=["incidents"])
    def resolve_incident(incident_id: str, body: ResolveIn) -> TriageOut:
        """A human decides: from the queue, or overriding the machine. Logged as a human decision."""
        return _out(incidents.resolve(incident_id, body.by, body.kind.value, body.severity, body.active,
                                      body.vehicle_id, body.area))

    @app.post("/incidents/{incident_id}/close", response_model=TriageOut, tags=["incidents"])
    def close_incident(incident_id: str, by: str = "ops") -> TriageOut:
        return _out(incidents.close(incident_id, by))

    @app.get("/queue", response_model=list[QueueItem], tags=["decisions"])
    def human_queue() -> list[QueueItem]:
        """Everything waiting for a person: incidents and customer replies, with the machine's last suggestion."""
        latest = {}
        for d in store.decisions(type_="incident_triage", limit=2000):
            latest.setdefault(d.subject, d)          # newest first
        items = [QueueItem(kind="incident", incident=i, decision=latest.get(i.id))
                 for i in store.list(Incident, state=IncidentState.reported.value)]
        names = {c.id: c.name for c in store.list(Customer, limit=100_000)}
        items += [QueueItem(kind="message", message=m, customer=names.get(m.customer_id), decision=d)
                  for m, d in replies.pending()]
        items += [QueueItem(kind="attempt", order=o, customer=names.get(o.customer_id), decision=d)
                  for o, d in prevention.pending_attempts()]
        return items

    @app.get("/incidents", response_model=list[Incident], tags=["incidents"])
    def list_incidents(state: str | None = None) -> list[Incident]:
        return store.list(Incident, **({"state": state} if state else {}))

    # ------------------------------------------------------------ messages
    def _reply_in_background(msg: MessageEvent) -> None:
        asyncio.run(replies.handle(msg))

    @app.post("/webhooks/messages", response_model=Accepted, tags=["messages"])
    def inbound_message(body: MessageIn, background: BackgroundTasks, handle: bool = True) -> Accepted:
        """Inbound customer reply (from the messaging provider or the simulator). By default it's
        understood and acted on in the background (reply_intent decision); watch /stream."""
        if body.order_id:
            store.require(Order, body.order_id)
        msg = MessageEvent(message_id=new_id("message"), **body.model_dump())
        seq = store.record_message("message.received", msg)
        if handle:
            background.add_task(_reply_in_background, msg)
        return Accepted(seq=seq)

    @app.get("/messages", tags=["messages"])
    def list_messages(order_id: str | None = None, limit: int = Query(200, ge=1, le=5000)) -> list[dict]:
        """Messages sent and received, newest first, with customer names."""
        names = {c.id: c.name for c in store.list(Customer, limit=100_000)}
        evs = store.events_since(0, types=["message.sent", "message.received"], limit=100_000)
        out = [{"direction": "out" if e.type == "message.sent" else "in", "ts": e.ts, **e.payload,
                "customer": names.get(e.payload.get("customer_id"))}
               for e in evs if order_id is None or e.payload.get("order_id") == order_id]
        return out[::-1][:limit]

    @app.post("/messages/{message_id}/resolve", response_model=ReplyOut, tags=["messages"])
    def resolve_message(message_id: str, body: ReplyResolveIn) -> ReplyOut:
        """A person decides what a customer reply means; logged as a human decision and applied."""
        o = replies.resolve(message_id, body.intent, body.by, body.when)
        return ReplyOut(message_id=o.message_id, order_id=o.order_id, intent=o.intent, result=o.result,
                        decision_id=o.decision_id, when=o.when)

    # ------------------------------------------------------------ decisions and policy
    @app.get("/decisions", response_model=list[DecisionRecord], tags=["decisions"])
    def list_decisions(type: str | None = None, route: str | None = None,
                       limit: int = Query(200, ge=1, le=2000)) -> list[DecisionRecord]:
        return store.decisions(type_=type, route=route, limit=limit)

    @app.get("/decisions/{decision_id}", response_model=DecisionRecord, tags=["decisions"])
    def get_decision(decision_id: str) -> DecisionRecord:
        d = store.get_decision(decision_id)
        if d is None:
            raise HTTPException(404, f"decision {decision_id} not found")
        return d

    @app.post("/decisions/{decision_id}/override", response_model=Accepted, tags=["decisions"])
    def override(decision_id: str, body: OverrideIn) -> Accepted:
        return Accepted(seq=store.override_decision(DecisionOverridden(decision_id=decision_id, **body.model_dump())))

    @app.post("/chat", tags=["chat"])
    async def chat_stream(body: ChatIn):
        """Talk to the ops assistant. Streams Server-Sent Events, one JSON object per step:
        tool_call, tool_result (plain summary), text (the reply) or error."""
        async def gen():
            async for item in chat.stream(body.session_id, body.text):
                yield f"data: {json.dumps(item, default=str)}\n\n"
            yield 'data: {"type": "done"}\n\n'
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/day/run", tags=["twin"])
    def simulate_day(depot_id: str) -> dict:
        """Replay today's plan in the digital twin (hidden "true" slowdowns), compare predicted and
        actual arrivals, and let the learner update the policy. Returns the day report."""
        return run_day(store, planner.engine, depot_id).model_dump()

    @app.get("/policy", response_model=Policy, tags=["decisions"])
    def policy() -> Policy:
        return store.current_policy()

    # ------------------------------------------------------------ twin
    @app.post("/twin/replay", tags=["twin"])
    def replay() -> None:
        raise HTTPException(501, "digital twin arrives in phase 5")

    # ------------------------------------------------------------ live stream
    @app.get("/stream", tags=["meta"])
    async def stream(request: Request, after: int | None = None, types: str | None = None,
                     max_events: int | None = Query(None, ge=1), timeout_s: float | None = Query(None, gt=0)):
        """Server-Sent Events of the event log. Resume with `Last-Event-ID` (sent automatically by
        browsers' EventSource) or `?after=<seq>`. `types` filters, e.g. `plan.applied,decision.made`.
        `max_events` / `timeout_s` end the stream (for tests and scripts)."""
        header = request.headers.get("last-event-id")
        cursor = after if after is not None else int(header) if header and header.isdigit() else store.last_seq()
        wanted = [t.strip() for t in types.split(",")] if types else None

        async def gen():
            nonlocal cursor
            sent, loop = 0, asyncio.get_running_loop()
            started = loop.time()
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                batch: list[Event] = await asyncio.to_thread(store.events_since, cursor, wanted)
                for ev in batch:
                    cursor = ev.seq
                    yield f"id: {ev.seq}\nevent: {ev.type}\ndata: {json.dumps(ev.model_dump(mode='json'))}\n\n"
                    sent += 1
                    if max_events and sent >= max_events:
                        return
                if timeout_s and loop.time() - started >= timeout_s:
                    return
                if not batch:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(STREAM_POLL_S)
                # a full batch means more may be waiting: loop again without sleeping

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
