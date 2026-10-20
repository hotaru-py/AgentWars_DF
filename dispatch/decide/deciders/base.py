"""Shared bits for Decider providers (spec §7.2)."""

from __future__ import annotations

from typing import Any


class ProviderUnavailable(Exception):
    """The provider can't run right now (no key, SDK missing, service down). The chain moves on."""


class NoAnswer(Exception):
    """The provider ran but has no answer for this input (e.g. no rule fired). The chain moves on."""


class ProviderMeta:
    """Filled in by a provider during decide(): model id, probability method, token usage."""

    def __init__(self) -> None:
        self.model: str | None = None
        self.method: str | None = None
        self.cost: dict[str, Any] = {}
