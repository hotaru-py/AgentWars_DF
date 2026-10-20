"""Messenger implementations and customer message templates (spec §6.1, §10)."""

from .sim import SimMessenger
from .templates import TEMPLATES, render

__all__ = ["SimMessenger", "TEMPLATES", "render"]
