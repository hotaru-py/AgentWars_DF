"""Play a day (portfolio demo): a scripted day driven through the real services, so routing, AI
decisions, customer messages, failed attempts and learning all happen on their own.

    python -m dispatch.play                 play dispatch/scenarios/chennai_day.json on a fresh demo day
    POST /demo/play  (DEMO_MODE=1)          the same from the console's ▶ Play a day button

A scenario is JSON: `steps` run in order, each announced as a `demo.step` event (the console's
activity feed shows them), with `pace_s` seconds between steps so people can watch the map change.

    plan                 plan routes
    risk_check           score delivery risk, message risky customers
    replies              customers answer: [{"customer": name, "text": ...}]
    drive                each van arrives at and completes its next `stops` stops (all if absent)
    report               a problem in plain words (+ "van": name for a breakdown), triaged and handled
    run_day              replay the day in the twin and learn

`fails` = {customer name: what the driver says}: whenever a van reaches that customer, the attempt
fails with those words (and is checked like any failed attempt) instead of being delivered.

`recorded` holds AI answers for the scenario's texts. With no Gemini key, RecordedLlm serves them as
the `llm` provider, so the whole day plays offline. With a key, the live model answers, and the
recorded answers cover for it when it fails (daily quota, overload): demo_llm() builds either.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from dispatch.core.decisions import ChoiceAnswer, NoulAnswer, ScoreAnswer
from dispatch.core.events import MessageEvent, StopEvent
from dispatch.core.ids import new_id
from dispatch.core.models import (Customer, Depot, Incident, IncidentKind, Order, OrderState, Vehicle)
from dispatch.decide.deciders.base import ProviderMeta, ProviderUnavailable

SCENARIO = Path(__file__).parent / "scenarios" / "chennai_day.json"
DWELL_S = 180                      # simulated drivers spend 3 minutes at the door


def load(path: Path | str = SCENARIO) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class RecordedLlm:
    """The `llm` provider for demos without a key: answers recorded for a scenario's texts (by exact
    text), with high but not perfect confidence. Unknown text -> unavailable (a person decides)."""
    name, model, capabilities = "llm", "recorded", {"text"}

    def __init__(self, recorded: dict[str, dict[str, Any]]):
        self.recorded = recorded
        self.meta = ProviderMeta()
        self.meta.model, self.meta.method = "recorded", "recorded"

    async def decide(self, state, questions):
        text = (state.get("report") or state.get("reply") or {}).get("text") if isinstance(state, dict) else state
        rec = self.recorded.get(text or "")
        if rec is None or not set(questions) <= set(rec):
            raise ProviderUnavailable("no recorded answer for this text (set GOOGLE_API_KEY for live AI)")
        out = {}
        for qid, q in questions.items():
            v = rec[qid]
            if q.type == "choice":
                rest = 0.02 / (len(q.criteria) - 1)
                out[qid] = ChoiceAnswer.from_probabilities({k: 0.98 if k == v else rest for k in q.criteria})
            elif q.type == "score":
                n = len(q.levels)
                out[qid] = ScoreAnswer.from_probabilities([0.96 if i == v else 0.04 / (n - 1) for i in range(n)])
            else:
                out[qid] = NoulAnswer(p=0.99 if v else 0.01)
        return out


class LiveOrRecorded:
    """Demo mode with a key: the live model first; if it's unavailable (quota, overload) and the text is
    one the scenario recorded, the recorded answer. The ledger shows which one answered."""
    name, capabilities = "llm", {"text"}

    def __init__(self, live, recorded: RecordedLlm):
        self.live, self.recorded = live, recorded
        self.model = getattr(live, "model", None)
        self.meta = getattr(live, "meta", None) or ProviderMeta()

    async def decide(self, state, questions):
        try:
            out = await self.live.decide(state, questions)
            self.meta = self.live.meta
            return out
        except ProviderUnavailable as live_error:
            try:
                out = await self.recorded.decide(state, questions)
            except ProviderUnavailable:
                raise live_error from None
            self.meta = self.recorded.meta
            return out


def demo_llm(settings, providers: dict) -> object:
    """The `llm` provider for demos: recorded answers without a key, live-with-recorded-fallback with one."""
    recorded = RecordedLlm(load()["recorded"])
    return LiveOrRecorded(providers["llm"], recorded) if settings.gemini_enabled and "llm" in providers else recorded


class Player:
    def __init__(self, store, planner, incidents, replies, prevention, pace_s: float = 0.0):
        self.store, self.planner, self.incidents = store, planner, incidents
        self.replies, self.prevention, self.pace_s = replies, prevention, pace_s

    # ------------------------------------------------------------ helpers
    def _depot(self) -> Depot:
        return self.store.list(Depot, limit=1)[0]

    def _by_customer(self) -> dict[str, Order]:
        names = {c.id: c.name for c in self.store.list(Customer, limit=100_000)}
        return {names.get(o.customer_id): o for o in self.store.list(Order, limit=100_000)}

    def _say(self, i: int, total: int, text: str) -> None:
        from dispatch.core.events import DemoStep
        self.store.append_event("demo.step", "demo", DemoStep(step=i, total=total, text=text))

    def _pause(self, share: float = 1.0) -> None:
        if self.pace_s:
            time.sleep(self.pace_s * share)

    # ------------------------------------------------------------ steps
    def play(self, scenario: dict[str, Any]) -> list[str]:
        """Run every step; returns one line per step saying what happened."""
        log = []
        self.fails = scenario.get("fails", {})
        steps = scenario["steps"]
        for i, step in enumerate(steps, start=1):
            self._say(i, len(steps), step["say"])
            log.append(f"{i}. {step['say']}: {getattr(self, '_' + step['do'])(step)}")
            self._pause()
        self._say(len(steps) + 1, len(steps), "The day is over")
        return log

    def _plan(self, step) -> str:
        r = self.planner.run(self._depot().id, "play a day")
        return f"{r.verdict}, {sum(1 for x in r.plan.routes if x.stops) if r.plan else 0} vans"

    def _risk_check(self, step) -> str:
        r = asyncio.run(self.prevention.run(self._depot().id))
        return f"{sum(x.risky for x in r.scored)} risky, {len(r.messaged)} asked to confirm"

    def _replies(self, step) -> str:
        orders = self._by_customer()
        out = []
        for rep in step["replies"]:
            o = orders[rep["customer"]]
            msg = MessageEvent(message_id=new_id("message"), customer_id=o.customer_id, order_id=o.id,
                               channel="sim", text=rep["text"])
            self.store.record_message("message.received", msg)
            out.append(f"{rep['customer']}: {asyncio.run(self.replies.handle(msg)).result}")
            self._pause(0.3)
        return "; ".join(out)

    def _drive(self, step) -> str:
        plan = self.store.current_plan(self._depot().id)
        fails = getattr(self, "fails", {})
        names = {c.id: c.name for c in self.store.list(Customer, limit=100_000)}
        done = []
        for r in plan.routes:
            van = self.store.get(Vehicle, r.vehicle_id)
            if van is None or van.state.value != "active":
                continue
            todo = [s for s in r.stops if (o := self.store.get(Order, s.order_id))
                    and o.state in (OrderState.planned, OrderState.out_for_delivery)]
            for s in todo[:step.get("stops", len(todo))]:
                o = self.store.get(Order, s.order_id)
                who = names.get(o.customer_id, "")
                self.store.stop_event("stop.arrived", StopEvent(order_id=o.id, vehicle_id=r.vehicle_id, location=o.location))
                if who in fails:
                    ev = StopEvent(order_id=o.id, vehicle_id=r.vehicle_id, location=o.location, reason=fails[who],
                                   dwell_s=DWELL_S)
                    self.store.stop_event("stop.failed", ev)
                    res = asyncio.run(self.prevention.check_attempt(o.id, ev)).result
                    done.append(f"{who} failed ({res})")
                else:
                    self.store.stop_event("stop.delivered", StopEvent(order_id=o.id, vehicle_id=r.vehicle_id,
                                                                      location=o.location, dwell_s=DWELL_S))
                    done.append(f"{who} delivered")
                self._pause(0.25)
        return ", ".join(done) or "nothing left to deliver"

    def _report(self, step) -> str:
        van_id = None
        if step.get("van"):
            van_id = next(v.id for v in self.store.list(Vehicle) if v.name.lower().startswith(step["van"].lower()))
        inc = Incident(kind=IncidentKind.other, description=step["text"], vehicle_id=van_id, source="driver" if van_id else "ops")
        self.store.save_incident(inc)
        out = asyncio.run(self.incidents.triage(inc.id))
        return f"{out.result}" + (f", {len(out.notified)} customers messaged" if out.result == "active" else "")

    def _run_day(self, step) -> str:
        from dispatch.twin import run_day
        r = run_day(self.store, self.planner.engine, self._depot().id)
        if r.delay_error is None:
            return "nothing to learn"
        return (f"delay guess off by {round(r.delay_error * 100)}%; learned "
                + ", ".join(f"{k} x{a:.2f} -> x{b:.2f}" for k, (a, b) in r.changes.items()))


def services(store, settings, engine=None, planner=None):
    """Planner + decision engine + services wired as in the API, with the demo `llm` (demo_llm)."""
    from dispatch.decide import DecisionEngine, default_providers
    from dispatch.incidents import IncidentService
    from dispatch.messaging import SimMessenger
    from dispatch.optimize import OrToolsSolver
    from dispatch.planning import Planner
    from dispatch.prevention import PreventionService
    from dispatch.replies import ReplyService
    from dispatch.routing import default_engine
    if planner is None:
        planner = Planner(store, engine or default_engine(settings.osrm_url, settings.osrm_time_factor), OrToolsSolver(),
                          time_limit_s=settings.plan_time_limit_s)
    providers = default_providers(settings)
    providers["llm"] = demo_llm(settings, providers)
    eng = DecisionEngine(store, providers, settings)
    m = SimMessenger(store)
    return (planner, IncidentService(store, eng, planner, m, settings.depot_tz),
            ReplyService(store, eng, planner, m, settings.depot_tz), PreventionService(store, eng, planner, m, settings))


def main(argv: list[str]) -> int:
    from dispatch import demo
    from dispatch.core.config import load as load_settings
    from dispatch.store import Store
    settings = load_settings()
    store = Store(settings.database_url)
    store.migrate()
    demo.reset(store, keep_learning=True)
    planner, incidents, replies, prevention = services(store, settings)
    pace = float(argv[1]) if len(argv) > 1 else 0.0
    for line in Player(store, planner, incidents, replies, prevention, pace_s=pace).play(load()):
        print(line)
    store.engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
