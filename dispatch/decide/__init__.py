"""Decision layer (spec §7): provider chains, calibration, gates, shadow mode, ledger."""

from .calibrate import Calibrator, effective_gate
from .catalogue import (CATALOGUE, DELIVERY_RISK, FAILED_ATTEMPT, INCIDENT_TRIAGE, REPLY_INTENT, RESCHEDULE_SLOT,
                        DecisionSpec)
from .deciders.base import NoAnswer, ProviderUnavailable
from .deciders.llm import LlmDecider
from .deciders.rules import RulesDecider, certain
from .engine import DecisionEngine, DecisionResult


def default_providers(settings) -> dict:
    """Providers available from configuration. `jev` only appears when a TypeSafe key is set."""
    providers = {"llm": LlmDecider(api_key=settings.google_api_key, model=settings.gemini_model,
                                   method=settings.llm_prob_method)}
    if settings.typesafe_api_key:
        from .deciders.optional.jev import JevDecider
        providers["jev"] = JevDecider(api_key=settings.typesafe_api_key, model=settings.jev_model)
    return providers


__all__ = ["DecisionEngine", "DecisionResult", "DecisionSpec", "CATALOGUE", "INCIDENT_TRIAGE", "REPLY_INTENT", "DELIVERY_RISK",
           "FAILED_ATTEMPT", "RESCHEDULE_SLOT", "Calibrator",
           "effective_gate", "LlmDecider", "RulesDecider", "certain", "ProviderUnavailable", "NoAnswer",
           "default_providers"]
