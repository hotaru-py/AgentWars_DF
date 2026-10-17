"""Track C: ADK agent definition for the autonomous dispatcher.

Contract: specs/WORKBOARD.md, section "Track C". Copy the Gemini retry setup from
logistics_agent/agent.py. MODEL = "gemini-3.8-flash".
"""

from google.adk.agents import Agent

from . import tools  # noqa: F401

MODEL = "gemini-3.8-flash"

root_agent = Agent(
    name="dispatch_agent",
    model=MODEL,
    description="Autonomous Chennai delivery dispatcher (skeleton: tools not wired yet).",
    instruction="Skeleton. Track C replaces this with the dispatcher instruction and tools.",
)
