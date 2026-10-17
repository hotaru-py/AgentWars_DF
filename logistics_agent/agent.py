"""Agent definitions for the logistics dispatcher.

  root_agent (logistics_dispatcher)  talks to the user, takes actions
    ├── inventory_agent              stock lookups (wrapped as AgentTool)
    ├── routing_agent                shortest path (wrapped as AgentTool)
    ├── get_shipment_status
    └── create_transfer              only after the user confirms

Sub-agents are wrapped with AgentTool (not sub_agents) so the root agent stays
in control of multi-step requests instead of handing the conversation off.
"""

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from .data import SKUS, WAREHOUSES
from .tools import (create_transfer, find_low_stock, get_shipment_status, get_stock,
                    shortest_route)

# gemini-2.5-flash returns 404 for new AI Studio keys; pin a concrete current Flash
# (not "gemini-flash-latest") so behaviour doesn't shift under us mid-demo.
MODEL = "gemini-3.8-flash"

# Free-tier keys allow only a few requests per minute per model, and one
# "fix the shortage" turn makes ~7 calls. Back off and retry on 429/5xx so a
# rate limit slows the demo down instead of crashing it.
LLM = Gemini(
    model=MODEL,
    retry_options=types.HttpRetryOptions(
        attempts=6, initial_delay=5, max_delay=40, http_status_codes=[429, 500, 503],
    ),
)

inventory_agent = Agent(
    name="inventory_agent",
    model=LLM,
    description="Answers questions about stock levels and low-stock items across warehouses.",
    instruction=(
        "You check inventory using your tools. SKUs: " + ", ".join(SKUS) + ". "
        "Warehouses: " + ", ".join(WAREHOUSES) + ".\n"
        "Always call a tool before answering. Report the exact numbers the tools return. "
        "Never invent, estimate or round stock data. If a tool returns an error, say so."
    ),
    tools=[get_stock, find_low_stock],
)

routing_agent = Agent(
    name="routing_agent",
    model=LLM,
    description="Computes the shortest road route, distance and ETA between two warehouses.",
    instruction=(
        "Use shortest_route to answer. Return the path in order, total km and estimated "
        "truck hours exactly as the tool reports them. Never guess distances."
    ),
    tools=[shortest_route],
)

root_agent = Agent(
    name="logistics_dispatcher",
    model=LLM,
    description="Supply-chain dispatcher for a South India warehouse network.",
    instruction=(
        "You are a logistics dispatcher. Warehouses: " + ", ".join(WAREHOUSES) + ". "
        "SKUs: " + ", ".join(SKUS) + ".\n"
        "- For stock questions, call inventory_agent.\n"
        "- For routes, distances and ETAs, call routing_agent.\n"
        "- For shipment tracking, use get_shipment_status.\n"
        "- To fix a shortage: ask inventory_agent for that SKU at every warehouse, pick a "
        "surplus warehouse (prefer the closest one that can spare the units), get the route "
        "from routing_agent, then propose the transfer (SKU, qty, from, to, km, ETA) and ask "
        "the user to confirm.\n"
        "- Only call create_transfer after the user explicitly says yes. Never call it while "
        "proposing.\n"
        "Never invent data; rely on tool results. Be concise. Use a small markdown table "
        "when comparing warehouses."
    ),
    tools=[
        AgentTool(agent=inventory_agent),
        AgentTool(agent=routing_agent),
        get_shipment_status,
        create_transfer,
    ],
)
