"""Track A: road graph, Dijkstra, all-pairs matrix, multi-stop solver.

Contract: specs/WORKBOARD.md, section "Track A". Pure functions only: no ADK, no I/O.
"""

from . import geo, seed  # noqa: F401

Graph = dict[str, dict[str, dict]]  # graph[a][b] = {"min": float, "km": float}


def build_graph(k: int = 4) -> Graph:
    raise NotImplementedError("Track A")


def apply_disruption(graph: Graph, disruption: dict) -> Graph:
    raise NotImplementedError("Track A")


def dijkstra(graph: Graph, a: str, b: str) -> dict:
    raise NotImplementedError("Track A")


def cost_matrix(graph: Graph) -> dict[str, dict[str, dict]]:
    raise NotImplementedError("Track A")


def solve(matrix: dict, vehicles: list[dict], params: dict) -> dict:
    raise NotImplementedError("Track A")


def retime(plan: dict, matrix: dict) -> dict:
    raise NotImplementedError("Track A")
