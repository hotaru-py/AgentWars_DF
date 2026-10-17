"""Track C: ADK agent definition for the autonomous dispatcher.

One agent with function tools. Every number comes from a tool; the critic and
learner are deterministic code, so the LLM proposes and narrates but never grades
itself or writes the policy.
"""

import os

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from .tools import (apply_disruption, get_policy, get_world_state, notify_customers, plan_routes,
                    replan_routes, reset, run_day)

# Set GEMINI_MODEL in dispatch_agent/.env to switch models without a code change. Free-tier
# quota is per project AND per model (about 20 requests/day each), so when one model's daily
# quota runs out, another model still has its own. gemini-flash-latest shares 3.8's quota.
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

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
- On any disruption, anywhere in Chennai:
  1. If there is no current plan, call plan_routes first.
  2. apply_disruption(kind, location, description): kind is "flood" (flooding, waterlogging,
     heavy rain), "traffic" (jam, VIP/CM convoy, accident, protest, road closure) or
     "breakdown" (pass vehicle_id). location = the area or road exactly as the user said it
     (e.g. "Tambaram", "GST Road"); description = the cause in a few words (e.g. "CM convoy").
     If the tool says the location is unknown and you know roughly where it is in Chennai,
     call again with lat and lng. If no orders are in the zone, still re-plan and say so.
  3. replan_routes()
  4. notify_customers once, with one message per affected order: polite, under 40 words,
     includes the new ETA as a clock time (new_eta_clock, e.g. "11:39 AM") and the reason.
  5. run_day() to measure the outcome and let the policy learn.
- "Reset" / "new day": call reset (hard only if explicitly asked to reset learning).
- After plan_routes only: one line on the plan the critic chose (overall score, total
  minutes, late orders) plus the candidate table. Don't mention disruptions or learning yet.
- After a disruption run, max 4 short lines: which plan the critic chose and why (overall
  score), time saved and late orders avoided vs doing nothing, customers notified, and what
  was learned (delay factor old -> new, and eta_error_pct as a percentage). Use a small
  table only for candidates.
- Only report numbers that a tool returned in this conversation; never fill a line with
  zeros or defaults."""

root_agent = Agent(
    name="dispatch_agent",
    model=LLM,
    description="Autonomous Chennai delivery dispatcher: plans, re-plans on disruptions, notifies, learns.",
    instruction=INSTRUCTION,
    tools=[get_world_state, plan_routes, apply_disruption, replan_routes, notify_customers,
           run_day, get_policy, reset],
)
