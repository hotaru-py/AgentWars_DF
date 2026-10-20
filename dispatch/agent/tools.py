"""Ops-chat tools (spec §12): what the dispatcher agent can do, over the same services as the API.

Each tool returns {"status": "ok" | "error", "summary": <plain sentence built by code>, ...}, so the
agent rephrases real numbers instead of inventing them. Tools are built per app by make_tools(ctx).
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dispatch.core.models import Customer, Depot, Incident, IncidentKind, IncidentState, Order, Vehicle
from dispatch.incidents import IncidentService
from dispatch.planning import NothingToPlan, Planner
from dispatch.prevention import PreventionService
from dispatch.replies import ReplyService
from dispatch.store import Store, StoreError
from dispatch.twin import run_day


@dataclass
class ToolContext:
    store: Store
    planner: Planner
    incidents: IncidentService
    replies: ReplyService
    tz: str = "Asia/Kolkata"
    prevention: PreventionService | None = None


def _dur(s: float) -> str:
    m = int(round(s / 60))
    h, m = divmod(m, 60)
    return f"{h} hour{'s' if h != 1 else ''} {m} minutes" if h else f"{m} minutes"


def make_tools(ctx: ToolContext) -> list:
    st = ctx.store

    def _depot() -> Depot:
        depots = st.list(Depot, limit=1)
        if not depots:
            raise StoreError("there is no depot yet")
        return depots[0]

    def _clock(ts) -> str:
        return ts.astimezone(ZoneInfo(ctx.tz)).strftime("%I:%M %p").lstrip("0")

    def get_overview() -> dict:
        """Get today's situation: deliveries, vans, the current routes, active problems, what waits
        for a person, and how much the system expects floods and traffic to slow vans.

        Returns:
            {"status": "ok", "summary", "orders_by_state", "vans", "problems", "waiting"}.
        """
        d = _depot()
        orders = st.list(Order, depot_id=d.id, limit=100_000)
        by_state: dict[str, int] = {}
        for o in orders:
            by_state[o.state.value] = by_state.get(o.state.value, 0) + 1
        vans = st.list(Vehicle, depot_id=d.id)
        plan = st.current_plan(d.id)
        active = [i for i in st.list(Incident) if i.state == IncidentState.active]
        waiting = len(st.list(Incident, state=IncidentState.reported.value)) + len(ctx.replies.pending())
        pol = st.current_policy()
        summary = (f"{len(orders)} deliveries today, {sum(v.state.value == 'active' for v in vans)} of {len(vans)} vans "
                   "working. ")
        summary += (f"Routes planned: {_dur(plan.scores.total_duration_s)} of driving, {plan.scores.late_orders} late. "
                    if plan and plan.scores else "No routes planned yet. ")
        summary += (f"{len(active)} problem(s) being handled: " + "; ".join(i.description[:60] for i in active) + ". "
                    if active else "No problems on the roads. ")
        summary += f"{waiting} item(s) waiting for a person. " if waiting else ""
        summary += "I expect floods to slow vans about " + f"{pol.delay_factor.get('flood', 4.0):.1f} times."
        return {"status": "ok", "summary": summary, "orders_by_state": by_state,
                "vans": [{"name": v.name, "state": v.state.value} for v in vans],
                "problems": [{"id": i.id, "kind": i.kind.value, "text": i.description} for i in active], "waiting": waiting}

    def plan_routes() -> dict:
        """Plan (or re-plan) today's routes now. Compares 3 ways to split the work and applies the best.

        Returns:
            {"status": "ok", "summary", "verdict"}.
        """
        try:
            r = ctx.planner.run(_depot().id, "ops chat")
        except NothingToPlan as e:
            return {"status": "error", "summary": f"Nothing to plan: {e}."}
        if r.verdict == "hold":
            return {"status": "ok", "verdict": "hold", "summary": "I kept the current routes: none of the new plans was better."}
        sc = r.plan.scores
        vans = sum(1 for x in r.plan.routes if x.stops)
        return {"status": "ok", "verdict": "apply",
                "summary": f"Planned all deliveries on {vans} vans: {_dur(sc.total_duration_s)} of driving in total, "
                           f"{sc.late_orders} late. I compared 3 ways to split the work and used the best."}

    async def report_problem(description: str, van_name: str = "") -> dict:
        """Report a problem on the roads or with a van, in the user's own words. The system works out
        what and where it is, and re-plans if needed.

        Args:
            description: The problem as the user said it, e.g. "CM convoy causing traffic in Tambaram".
            van_name: Only for a van problem: the van's name as shown (e.g. "V2").

        Returns:
            {"status": "ok", "summary", "result"} where result is active, rejected, needs_location,
            needs_vehicle or human.
        """
        van_id = None
        if van_name.strip():
            vans = [v for v in st.list(Vehicle) if v.name.lower().startswith(van_name.strip().lower())]
            if not vans:
                return {"status": "error", "summary": f"I couldn't find a van called {van_name}."}
            van_id = vans[0].id
        inc = Incident(kind=IncidentKind.other, description=description, vehicle_id=van_id, source="ops chat")   # triage decides the kind
        st.save_incident(inc)
        out = await ctx.incidents.triage(inc.id)
        texts = {
            "active": f"Handling it: {out.incident.kind.value}"
                      + (f", roads expected about {out.incident.slowdown:.1f} times slower" if out.incident.slowdown > 1 else "")
                      + f". Routes re-planned; {len(out.notified)} customer(s) messaged.",
            "rejected": "This doesn't look like a real problem right now, so I left the routes as they are.",
            "needs_location": "I couldn't tell where this is. It's waiting for a person to place it on the map.",
            "needs_vehicle": "This sounds like a van problem, but no van was named. Tell me which van.",
            "human": "I'm not sure enough about this one, so it's waiting for a person.",
        }
        return {"status": "ok", "result": out.result, "incident_id": inc.id, "summary": texts.get(out.result, out.result)}

    def list_waiting() -> dict:
        """List what is waiting for a person: unclear problem reports, customer replies and failed
        deliveries the system couldn't check.

        Returns:
            {"status": "ok", "summary", "items": [{"id", "type", "text"}]}.
        """
        items = [{"id": i.id, "type": "problem", "text": i.description}
                 for i in st.list(Incident, state=IncidentState.reported.value)]
        items += [{"id": m.message_id, "type": "customer reply", "text": m.text} for m, _ in ctx.replies.pending()]
        if ctx.prevention is not None:
            names = {c.id: c.name for c in st.list(Customer, limit=100_000)}
            items += [{"id": o.id, "type": "failed delivery to check", "text": f"{names.get(o.customer_id)}: driver couldn't deliver"}
                      for o, _ in ctx.prevention.pending_attempts()]
        summary = (f"{len(items)} item(s) waiting: " + "; ".join(f"{x['type']}: {x['text'][:50]}" for x in items)
                   if items else "Nothing is waiting for a person.")
        return {"status": "ok", "summary": summary, "items": items}

    def resolve_problem(problem_id: str, kind: str, severity: float = 0.5, active: bool = True) -> dict:
        """Decide a waiting problem report on the user's behalf, ONLY when the user says what it is.

        Args:
            problem_id: The id from list_waiting (starts with "inc_").
            kind: flood, traffic, closure, breakdown or other.
            severity: 0 (barely noticeable) to 1 (blocked).
            active: False if the user says it isn't a real problem.

        Returns:
            {"status": "ok", "summary", "result"}.
        """
        out = ctx.incidents.resolve(problem_id, "ops chat", kind, min(1.0, max(0.0, severity)), active)
        texts = {"active": f"Done: handling it as {kind}; routes re-planned.", "rejected": "Done: marked as not real.",
                 "needs_location": "I still need a location: place it on the map in the Needs you tab.",
                 "needs_vehicle": "I still need to know which van."}
        return {"status": "ok", "result": out.result, "summary": texts.get(out.result, out.result)}

    def run_the_day() -> dict:
        """Replay today in the simulator to see how the day really went, and learn from it.

        Returns:
            {"status": "ok", "summary", "delay_error", "changes"}.
        """
        try:
            r = run_day(st, ctx.planner.engine, _depot().id)
        except StoreError as e:
            return {"status": "error", "summary": str(e)}
        if r.delay_error is None:
            return {"status": "ok", "summary": "I replayed the day: there were no problems on the roads, so nothing new to learn."}
        learned = ", ".join(f"{k} now about {b:.1f} times slower (was {a:.1f})" for k, (a, b) in r.changes.items())
        return {"status": "ok", "delay_error": r.delay_error, "changes": r.changes,
                "summary": f"I replayed the day. My guess of how late deliveries would be was off by "
                           f"{round(r.delay_error * 100)}%. Next time I'll expect {learned}."}

    async def check_risky_deliveries() -> dict:
        """Before vans leave: score every delivery's chance of failing (nobody home, refused, address
        not found) and ask the risky customers to confirm by message.

        Returns:
            {"status": "ok", "summary", "risky": [{"customer", "chance_of_failing"}]}.
        """
        if ctx.prevention is None:
            return {"status": "error", "summary": "The risk check isn't available here."}
        r = await ctx.prevention.run(_depot().id)
        risky = [x for x in r.scored if x.risky]
        lines = [f"{x.customer} ({round(x.risk * 100)}%)" for x in risky[:5]]
        summary = (f"I checked {len(r.scored)} deliveries. {len(risky)} look risky"
                   + (": " + ", ".join(lines) if lines else "") + f". I asked {len(r.messaged)} customer(s) to confirm. "
                   if risky else f"I checked {len(r.scored)} deliveries; none look risky. ")
        if r.planned_first:
            summary += "I planned the routes first so the messages could give arrival times."
        return {"status": "ok", "summary": summary.strip(),
                "risky": [{"customer": x.customer, "chance_of_failing": round(x.risk, 2)} for x in risky]}

    def customer_messages(limit: int = 8) -> dict:
        """Recent messages to and from customers, newest first.

        Args:
            limit: How many to return (default 8).

        Returns:
            {"status": "ok", "summary", "messages": [{"direction", "customer", "text"}]}.
        """
        names = {c.id: c.name for c in st.list(Customer, limit=100_000)}
        evs = st.events_since(0, types=["message.sent", "message.received"], limit=100_000)[::-1][:max(1, limit)]
        msgs = [{"direction": "to" if e.type == "message.sent" else "from", "customer": names.get(e.payload["customer_id"]),
                 "text": e.payload["text"]} for e in evs]
        return {"status": "ok", "messages": msgs,
                "summary": f"{len(msgs)} recent message(s)." if msgs else "No customer messages yet."}

    def find_delivery(customer_or_area: str) -> dict:
        """Find a delivery by customer name or area and say where it stands.

        Args:
            customer_or_area: e.g. "Priya" or "Velachery".

        Returns:
            {"status": "ok", "summary"} or an error if nothing matches.
        """
        key = customer_or_area.strip().lower()
        d = _depot()
        names = {c.id: c.name for c in st.list(Customer, limit=100_000)}
        hits = [o for o in st.list(Order, depot_id=d.id, limit=100_000)
                if key in names.get(o.customer_id, "").lower() or key in o.notes.lower() or key in o.raw_address.lower()]
        if not hits:
            return {"status": "error", "summary": f"I couldn't find a delivery for '{customer_or_area}'."}
        plan = st.current_plan(d.id)
        stop = {s.order_id: (r.vehicle_id, s) for r in (plan.routes if plan else []) for s in r.stops}
        vans = {v.id: v.name for v in st.list(Vehicle)}
        lines = []
        for o in hits[:3]:
            line = f"{names.get(o.customer_id)} ({o.notes.splitlines()[0] if o.notes else o.raw_address}): {o.state.value.replace('_', ' ')}"
            if o.id in stop:
                van, s = stop[o.id]
                line += f", on {vans.get(van, van)}, arriving around {_clock(s.eta_p50)}"
                if o.window_end:
                    line += f" (due by {_clock(o.window_end)})"
            lines.append(line)
        return {"status": "ok", "summary": "; ".join(lines) + "."}

    return [get_overview, plan_routes, report_problem, list_waiting, resolve_problem, run_the_day,
            check_risky_deliveries, customer_messages, find_delivery]
