"""Track C: ADK tool functions over the world state. Writes out/state.json for the map.

Contract: specs/WORKBOARD.md, section "Track C". Every tool returns {"status": "ok"|"error", ...}
and needs type hints plus a clear docstring (ADK builds the tool schema from them).
"""

from . import feedback, routing, seed  # noqa: F401
