"""The learner (spec §9, ported from the v1 demo): update the policy from a day.

For each incident kind the day revealed:
  new factor = halfway between the old factor and the observed one, capped at +-25% per run.
Rollback: if a kind's delay error rose two runs in a row, its factor returns to the previous
version's value. The policy is only ever written here, in plain code (no AI writes it).
"""

from __future__ import annotations

from dispatch.core.models import IncidentKind, Policy, utcnow

from .simulate import DayResult

MAX_STEP = 0.25
BLEND = 0.5


def learn(policy: Policy, day: DayResult, run: int, previous: Policy | None = None) -> tuple[Policy, dict, list[str]]:
    """Returns (new policy version, {kind: [old, new]}, kinds rolled back)."""
    err = day.delay_error()
    factors = dict(policy.delay_factor)
    changes: dict[str, list[float]] = {}
    rolled_back: list[str] = []
    history = list(policy.history)
    for kind, observed in day.observed_factor.items():
        old = factors.get(kind, 1.5)
        errors = [h["delay_error"] for h in history if h.get("kind") == kind.value and h.get("delay_error") is not None]
        if err is not None and len(errors) >= 2 and errors[-2] < errors[-1] < err and previous is not None:
            new = previous.delay_factor.get(kind, old)                # getting worse twice: undo the last step
            rolled_back.append(kind.value)
        else:
            target = (1 - BLEND) * old + BLEND * observed
            new = min(old * (1 + MAX_STEP), max(old * (1 - MAX_STEP), target))
        factors[kind] = round(new, 3)
        changes[kind.value] = [old, round(new, 3)]
        history.append({"run": run, "kind": kind.value, "delay_error": None if err is None else round(err, 4),
                        "observed": observed, "factor": round(new, 3), "at": utcnow().isoformat()})
    if not day.observed_factor:
        history.append({"run": run, "kind": None, "delay_error": None if err is None else round(err, 4),
                        "observed": None, "factor": None, "at": utcnow().isoformat()})
    new_policy = policy.model_copy(update={"version": policy.version + 1, "delay_factor": factors,
                                           "history": history[-200:], "parent_version": policy.version,
                                           "created_at": utcnow(), "created_by": "learner"})
    return Policy.model_validate(new_policy.model_dump()), changes, rolled_back


def next_run(policy: Policy) -> int:
    return 1 + max((h["run"] for h in policy.history), default=0)


__all__ = ["learn", "next_run", "IncidentKind"]
