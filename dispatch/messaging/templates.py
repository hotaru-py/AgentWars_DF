"""Customer message templates (spec §10). Code fills them from real numbers, so a message can't
invent a time. Every rendered message stays under 40 words (checked by tests)."""

from __future__ import annotations

TEMPLATES = {
    "eta_update": ("Hi {name}, {reason}. Your delivery now arrives around {eta}. "
                   "Sorry for the change. Reply 2 to reschedule or 3 to cancel."),
    "confirm_request": ("Hi {name}, your order arrives today between {eta} and {eta_late}. "
                        "Reply 1 to confirm, 2 to reschedule, 3 to cancel. You can also send directions."),
    "cancelled": "Hi {name}, your order has been cancelled as requested. Reply if this was a mistake.",
    "noted": "Thanks {name}, we've passed your note to the driver.",
    "rescheduled": "Thanks {name}, done: we'll deliver {when}. Reply 3 if you'd rather cancel.",
    "reattempt_offer": ("Hi {name}, sorry we missed you today. We'll try again on the next delivery run. "
                        "Reply 1 to confirm, 2 to pick another time, 3 to cancel."),
    "returning": "Hi {name}, we couldn't deliver your order, so it is being returned to the sender. Reply if you have questions.",
}

MAX_WORDS = 40


def render(template: str, vars: dict) -> str:
    if template not in TEMPLATES:
        raise KeyError(f"unknown template {template!r}; known: {', '.join(TEMPLATES)}")
    text = TEMPLATES[template].format(**vars)
    if len(text.split()) >= MAX_WORDS:
        raise ValueError(f"message is {len(text.split())} words; must be under {MAX_WORDS}")
    return text
