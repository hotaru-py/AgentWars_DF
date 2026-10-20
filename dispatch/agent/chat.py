"""Ops chat (spec §12): an ADK agent inside the API, streaming plain-language steps to the console.

ChatService.stream(session_id, text) yields dicts:
  {"type": "tool_call", "name", "args"}   the agent is using a tool
  {"type": "tool_result", "name", "summary", "ok"}
  {"type": "text", "text"}                the agent's reply
  {"type": "error", "message"}            e.g. no key, quota, overload
The ADK runner is created lazily; tests inject `runner_factory` to avoid keys and network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from dispatch.core.config import Settings

from .tools import ToolContext, make_tools

APP = "dispatch"
INSTRUCTION = """You are the dispatcher's assistant for a delivery company in Chennai. You can see and act on
today's deliveries with your tools. The person you talk to runs operations but is not technical.

WHAT TO DO
- Questions about today ("how's it going?", "where is Priya's parcel?"): call get_overview or find_delivery.
- A problem on the roads or with a van, in any words: call report_problem with their words (and van_name
  for a van problem). It works out what and where it is and re-plans.
- "Plan the routes": plan_routes. "How did the day go?" / "learn from today": run_the_day.
- "Which deliveries might fail?" / "check before the vans leave": check_risky_deliveries.
- "What needs me?": list_waiting. Only call resolve_problem when the person tells you what a waiting
  problem is; never guess for them.

HOW TO TALK
- Base every number, name and time on the "summary" each tool returns. Never invent or change them.
- Lead with what needs attention: active problems, late deliveries, items waiting for a person. Don't
  say everything is fine while a problem is active.
- Short, warm, plain sentences. No jargon: never say critic, KPI, policy, multiplier, matrix, tool, API.
- Times as clock times ("11:40 AM") or durations ("1 hour 50 minutes").
- After acting: one sentence on the outcome, then at most 3 short bullets of what you did."""


class ChatUnavailable(Exception):
    pass


def default_runner_factory(tools: list, settings: Settings):
    """Build an ADK Runner (lazy imports: ADK is only needed when chat is used)."""
    from google.adk.agents import Agent
    from google.adk.models import Gemini
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    if not settings.google_api_key:
        raise ChatUnavailable("Chat needs a Gemini key: add GOOGLE_API_KEY to .env (or use Connect Gemini in the console).")
    model = Gemini(model=settings.gemini_model, retry_options=types.HttpRetryOptions(
        attempts=4, initial_delay=2, max_delay=20, http_status_codes=[429, 500, 503]))
    agent = Agent(name="dispatch_ops", model=model, instruction=INSTRUCTION, tools=tools,
                  description="Operations assistant for the Chennai delivery fleet.")
    import os
    os.environ.setdefault("GOOGLE_API_KEY", settings.google_api_key)      # ADK's Gemini reads the env
    return Runner(app_name=APP, agent=agent, session_service=InMemorySessionService(), auto_create_session=True)


class ChatService:
    def __init__(self, ctx: ToolContext, settings: Settings, runner_factory: Callable | None = None):
        self.ctx, self.settings = ctx, settings
        self._factory = runner_factory or default_runner_factory
        self._runner = None

    def _get_runner(self):
        if self._runner is None:
            self._runner = self._factory(make_tools(self.ctx), self.settings)
        return self._runner

    async def stream(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
        try:
            runner = self._get_runner()
        except ChatUnavailable as e:
            yield {"type": "error", "message": str(e)}
            return
        from google.genai import types
        msg = types.Content(role="user", parts=[types.Part(text=text)])
        try:
            async for ev in runner.run_async(user_id="ops", session_id=session_id, new_message=msg):
                if getattr(ev, "error_message", None):
                    yield {"type": "error", "message": ev.error_message}
                for part in (ev.content.parts if ev.content and ev.content.parts else []):
                    if part.function_call:
                        yield {"type": "tool_call", "name": part.function_call.name, "args": dict(part.function_call.args or {})}
                    elif part.function_response:
                        resp = part.function_response.response or {}
                        yield {"type": "tool_result", "name": part.function_response.name,
                               "summary": resp.get("summary", ""), "ok": resp.get("status") != "error"}
                    elif part.text and not getattr(part, "thought", False) and not getattr(ev, "partial", False):
                        yield {"type": "text", "text": part.text}
        except Exception as e:                       # quota, overload, network: tell the user plainly
            msg = str(e)
            hint = (" (the free daily quota is used up: try another GEMINI_MODEL in .env or a key with billing)"
                    if "429" in msg else " (the AI model is overloaded: try again in a minute)" if "503" in msg else "")
            yield {"type": "error", "message": f"The AI couldn't answer{hint}. Details: {msg[:200]}"}
