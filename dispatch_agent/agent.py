"""Track C: ADK agent definition for the autonomous dispatcher.

One agent with function tools. Every number comes from a tool; the critic and
learner are deterministic code, so the LLM proposes and narrates but never grades
itself or writes the policy.
"""

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from .tools import (apply_disruption, get_policy, get_world_state, notify_customers, plan_routes,
                    replan_routes, reset, run_day)

MODEL = "gemini-3.8-flash"

# Free tier is 5 requests/min per model; back off on 429/5xx instead of crashing.
LLM = Gemini(
    model=MODEL,
    retry_options=types.HttpRetryOptions(
        attempts=6, initial_delay=5, max_delay=40, http_status_codes=[429, 500, 503],
    ),
)

INSTRUCTION = """You are an autonomous dispatch agent for a Chennai delivery fleet
(1 depot in Guindy, 10 orders, 3 vans). No human approves your actions: act, then report.

Rules:
- Never invent routes, times or metrics. Every number must come from a tool result.
- "Plan routes" / start of day: call plan_routes.
- On any disruption (flood / flooding on OMR -> "flood"; traffic jam / T. Nagar -> "traffic";
  van breakdown -> "breakdown"):
  1. If there is no current plan, call plan_routes first.
  2. apply_disruption(scenario)
  3. replan_routes()
  4. notify_customers once, with one message per affected order: polite, under 40 words,
     includes the new ETA as a clock time (new_eta_clock, e.g. "11:39 AM") and the reason.
  5. run_day() to measure the outcome and let the policy learn.
- "Reset" / "new day": call reset (hard only if explicitly asked to reset learning).
- Final answer, max 4 short lines: which plan the critic chose and why (overall score),
  time saved and late orders avoided vs doing nothing, customers notified, and what was
  learned (delay factor old -> new, ETA error). Use a small table only for candidates."""

root_agent = Agent(
    name="dispatch_agent",
    model=LLM,
    description="Autonomous Chennai delivery dispatcher: plans, re-plans on disruptions, notifies, learns.",
    instruction=INSTRUCTION,
    tools=[get_world_state, plan_routes, apply_disruption, replan_routes, notify_customers,
           run_day, get_policy, reset],
)
