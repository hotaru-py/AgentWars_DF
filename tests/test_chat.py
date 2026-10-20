"""Ops chat: the agent's tools over a demo day, and the /chat stream with a fake ADK runner (no keys)."""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from google.genai import types

from dispatch import demo
from dispatch.agent import ChatService, ToolContext, make_tools
from dispatch.api import create_app
from dispatch.core.config import Settings
from dispatch.decide import DecisionEngine
from dispatch.incidents import IncidentService
from dispatch.messaging import SimMessenger
from dispatch.optimize import OrToolsSolver
from dispatch.planning import Planner
from dispatch.replies import ReplyService
from dispatch.routing import HaversineEngine
from dispatch.store import Store

run = asyncio.run


@pytest.fixture
def ctx(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'c.db'}")
    st.create_all()
    demo.seed(st)
    planner = Planner(st, HaversineEngine(), OrToolsSolver(), time_limit_s=0.3)
    eng = DecisionEngine(st, {}, Settings())                              # no AI: reports go to a person
    m = SimMessenger(st)
    yield ToolContext(st, planner, IncidentService(st, eng, planner, m), ReplyService(st, eng, planner, m))
    st.engine.dispose()


def _tools(ctx):
    return {f.__name__: f for f in make_tools(ctx)}


def test_tools_cover_the_day(ctx):
    t = _tools(ctx)
    assert "No routes planned yet" in t["get_overview"]()["summary"]
    p = t["plan_routes"]()
    assert p["verdict"] == "apply" and "late" in p["summary"]
    assert "Routes planned" in t["get_overview"]()["summary"]
    f = t["find_delivery"]("priya")
    assert f["status"] == "ok" and "Priya Venkat" in f["summary"] and "arriving around" in f["summary"]
    assert t["find_delivery"]("nobody here")["status"] == "error"
    r = run(t["report_problem"]("Severe flooding near Velachery"))
    assert r["result"] == "human" and "waiting for a person" in r["summary"]          # no AI configured
    w = t["list_waiting"]()
    assert len(w["items"]) == 1 and w["items"][0]["type"] == "problem"
    done = t["resolve_problem"](w["items"][0]["id"], "flood", 0.8)
    assert done["result"] == "active"
    day = t["run_the_day"]()
    assert day["status"] == "ok" and "off by" in day["summary"] and day["changes"]["flood"][0] == 4.0
    assert t["customer_messages"]()["status"] == "ok"
    assert t["report_problem"].__name__ == "report_problem" and asyncio.iscoroutinefunction(t["report_problem"])


def test_van_problem_needs_a_known_van(ctx):
    t = _tools(ctx)
    t["plan_routes"]()
    assert run(t["report_problem"]("puncture", van_name="V9"))["status"] == "error"
    assert run(t["report_problem"]("puncture, can't continue", van_name="V2"))["result"] == "human"


def _event(*parts):
    return NS(content=types.Content(role="model", parts=list(parts)), partial=False, error_message=None)


class FakeRunner:
    """Behaves like an ADK Runner: calls a real tool, then answers."""

    def __init__(self, tools, settings):
        self.tools = {f.__name__: f for f in tools}

    async def run_async(self, user_id, session_id, new_message):
        yield _event(types.Part(function_call=types.FunctionCall(name="get_overview", args={})))
        out = self.tools["get_overview"]()
        yield _event(types.Part(function_response=types.FunctionResponse(name="get_overview", response=out)))
        yield _event(types.Part(text=f"Here's today: {out['summary']}"))


def test_chat_stream_with_fake_runner(ctx):
    app = create_app(ctx.store, planner=ctx.planner, settings=Settings(), chat_runner_factory=FakeRunner)
    with TestClient(app) as c:
        r = c.post("/chat", json={"session_id": "s1", "text": "how's it going?"})
        events = [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "done"]
    assert events[1]["ok"] and "deliveries today" in events[1]["summary"] and "Here's today" in events[2]["text"]


def test_chat_without_key_explains(ctx):
    svc = ChatService(ctx, Settings(google_api_key=""))
    async def collect():
        return [e async for e in svc.stream("s", "hi")]
    out = run(collect())
    assert out == [{"type": "error", "message": out[0]["message"]}] and "Gemini key" in out[0]["message"]


def test_chat_quota_error_is_plain(ctx):
    class Boom:
        def __init__(self, tools, settings):
            pass

        async def run_async(self, **kw):
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
            yield                                                              # pragma: no cover
    svc = ChatService(ctx, Settings(), runner_factory=Boom)
    async def collect():
        return [e async for e in svc.stream("s", "hi")]
    out = run(collect())
    assert out[0]["type"] == "error" and "daily quota" in out[0]["message"]


def test_chat_rejects_bad_session_ids(ctx):
    app = create_app(ctx.store, planner=ctx.planner, settings=Settings(), chat_runner_factory=FakeRunner)
    with TestClient(app) as c:
        assert c.post("/chat", json={"session_id": "../etc", "text": "hi"}).status_code == 422
