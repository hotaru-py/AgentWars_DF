"""Delivery times from customer text (spec §10, reschedule replies): deterministic, no AI.

    parse_when("after 6 pm", now)          -> When(today 18:00-21:00)
    parse_when("tomorrow morning", now)    -> When(tomorrow 09:00-12:00)
    parse_when("between 4 and 6", now)     -> When(today 16:00-18:00)
    parse_when("when I'm back", now)       -> None (the LLM or a person decides)

`now` is timezone-aware in the depot's zone; returned windows are in the same zone. Delivery hours
are 09:00-21:00. A time-only reply whose window has already passed today means tomorrow. Hours
without am/pm follow how people write them: 1-7 mean afternoon/evening, 8-11 morning.

SLOTS are the coarse choices the `reschedule_slot` decision offers an LLM (or Jev), so a vague reply
("once office is over") still lands on a concrete window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

DAY_START, DAY_END = 9, 21
PARTS = {"morning": (9, 12), "afternoon": (12, 16), "evening": (16, 20), "night": (18, 21)}
SLOTS = {
    "today_morning": "Later today, in the morning (9 AM to 12 PM)",
    "today_afternoon": "Later today, in the afternoon (12 to 4 PM)",
    "today_evening": "Later today, in the evening (4 to 8 PM)",
    "tomorrow_morning": "Tomorrow morning (9 AM to 12 PM)",
    "tomorrow_afternoon": "Tomorrow afternoon (12 to 4 PM)",
    "tomorrow_evening": "Tomorrow evening (4 to 8 PM)",
    "unclear": "No usable time or day: vague, conditional, or not about timing",
}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_TOMORROW = ("tomorrow", "tmrw", "tmr", "tomorow", "tommorow", "tommorrow", "naalai", "nalaiku", "naalaiku")
_TIME = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"


@dataclass
class When:
    start: datetime
    end: datetime
    days_ahead: int = 0

    @property
    def slot(self) -> str:
        """The SLOTS label containing the window's start (today/tomorrow only)."""
        part = "morning" if self.start.hour < 12 else "afternoon" if self.start.hour < 16 else "evening"
        return f"{'today' if self.days_ahead == 0 else 'tomorrow'}_{part}" if self.days_ahead <= 1 else "unclear"

    def describe(self) -> str:
        day = "today" if self.days_ahead == 0 else "tomorrow" if self.days_ahead == 1 else self.start.strftime("%A")
        fmt = lambda t: t.strftime("%I:%M %p").lstrip("0").replace(":00", "")  # noqa: E731
        return f"{day} between {fmt(self.start)} and {fmt(self.end)}"


def _hour(h: int, m: int, ampm: str | None) -> tuple[int, int] | None:
    if ampm:
        pm = ampm.startswith("p")
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if pm else 0)
    elif h <= 7 and h >= 1:
        h += 12                                        # "after 6" means 6 PM
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def _day(text: str, now: datetime) -> int | None:
    if "day after tomorrow" in text:
        return 2
    if any(re.search(rf"\b{w}\b", text) for w in _TOMORROW):
        return 1
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", text):
        return 0
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", text):
            return (i - now.weekday()) % 7 or 7
    return None


def slot_window(slot: str, now: datetime) -> When | None:
    if slot not in SLOTS or slot == "unclear":
        return None
    day, part = slot.split("_")
    start_h, end_h = PARTS[part]
    return _window(now, 0 if day == "today" else 1, (start_h, 0), (end_h, 0))


def _window(now: datetime, days: int, a: tuple[int, int], b: tuple[int, int]) -> When | None:
    base = (now + timedelta(days=days)).date()
    start = datetime.combine(base, time(*a), tzinfo=now.tzinfo)
    end = datetime.combine(base, time(*b), tzinfo=now.tzinfo)
    if days == 0 and start < now:
        start = now.replace(second=0, microsecond=0)
    if end <= start:
        return None
    return When(start=start, end=end, days_ahead=days)


def parse_when(text: str, now: datetime) -> When | None:
    t = " ".join(text.lower().replace("’", "'").split())
    day = _day(t, now)
    rng = re.search(rf"(?:between\s+|from\s+)?{_TIME}\s*(?:and|to|-|till|until)\s*{_TIME}", t)
    after = re.search(rf"\b(?:after|post|from)\s+{_TIME}", t)
    before = re.search(rf"\b(?:before|by)\s+{_TIME}", t)
    at = (re.search(rf"\b(?:at|around|@)\s*{_TIME}", t)
          or re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", t))     # "5:30pm" alone
    part = next((p for p in PARTS if p in t), None) or ("night" if "tonight" in t else None)
    window: tuple[tuple[int, int], tuple[int, int]] | None = None
    if rng:
        g = rng.groups()
        a = _hour(int(g[0]), int(g[1] or 0), g[2] or g[5])
        b = _hour(int(g[3]), int(g[4] or 0), g[5])
        if a and b and b > a:
            window = (a, b)
    elif after:
        a = _hour(int(after.group(1)), int(after.group(2) or 0), after.group(3))
        if a:
            window = (a, (min(DAY_END, a[0] + 3), a[1] if a[0] + 3 < DAY_END else 0))
    elif before:
        b = _hour(int(before.group(1)), int(before.group(2) or 0), before.group(3))
        if b:
            window = ((max(DAY_START, b[0] - 3), 0), b)
    elif at:
        a = _hour(int(at.group(1)), int(at.group(2) or 0), at.group(3))
        if a:
            window = (a, (min(23, a[0] + 1), a[1]))
    if window is None and part:
        window = ((PARTS[part][0], 0), (PARTS[part][1], 0))
    if window is None and day is None:
        return None
    if window is None:
        window = ((DAY_START, 0), (DAY_END, 0))
    if day is None:
        day = 0
        if datetime.combine(now.date(), time(*window[1]), tzinfo=now.tzinfo) <= now + timedelta(minutes=30):
            day = 1                                      # that time has (nearly) passed today: tomorrow
    return _window(now, day, *window)
