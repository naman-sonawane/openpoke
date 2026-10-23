"""In-memory stand-ins for the practice-management system.

Patient lookup, the appointment calendar and booking are all mocked, as the
brief allows. The shapes mirror what a real EHR/scheduling API would return so
swapping in a real backend only touches this module.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

CLINIC_NAME = "Maple Street Family Medicine"

PATIENTS: List[Dict[str, str]] = [
    {"id": "P-1001", "first": "Maria", "last": "Garcia", "dob": "1985-03-14", "phone": "5552013344"},
    {"id": "P-1002", "first": "James", "last": "Chen", "dob": "1990-07-22", "phone": "5554189920"},
    {"id": "P-1003", "first": "Priya", "last": "Patel", "dob": "1978-11-02", "phone": "5557301187"},
    {"id": "P-1004", "first": "Robert", "last": "Johnson", "dob": "1952-01-30", "phone": "5556620451"},
]

PROVIDERS = ("Dr. Alvarez", "Dr. Kim", "Nurse Practitioner Brooks")

_SLOT_TIMES = [time(h, m) for h in range(8, 17) for m in (0, 30)]


@dataclass
class Slot:
    id: str
    start: datetime
    provider: str
    booked: bool = False

    def spoken(self, today: date) -> str:
        day = self.start.date()
        if day == today:
            day_label = "today"
        elif day == today + timedelta(days=1):
            day_label = "tomorrow"
        else:
            day_label = self.start.strftime("%A, %B ") + str(self.start.day)
        clock = self.start.strftime("%I:%M %p").lstrip("0").replace(":00", "")
        return f"{day_label} at {clock} with {self.provider}"

    def as_option(self, today: date) -> Dict[str, str]:
        return {
            "slot_id": self.id,
            "when": self.spoken(today),
            "date": self.start.date().isoformat(),
            "time": self.start.strftime("%H:%M"),
            "provider": self.provider,
        }


def _business_days(start: date, count: int) -> List[date]:
    days: List[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def build_calendar(now: Optional[datetime] = None, seed: int = 7) -> Dict[str, Slot]:
    """Two weeks of slots, mostly booked, with a couple of same-day openings."""

    now = now or datetime.now()
    rng = random.Random(seed)
    slots: Dict[str, Slot] = {}
    for day in _business_days(now.date(), 10):
        for index, slot_time in enumerate(_SLOT_TIMES):
            start = datetime.combine(day, slot_time)
            if start <= now + timedelta(minutes=45):
                continue
            slot_id = f"S-{day.strftime('%m%d')}-{slot_time.strftime('%H%M')}"
            provider = PROVIDERS[index % len(PROVIDERS)]
            booked = rng.random() < 0.75
            slots[slot_id] = Slot(slot_id, start, provider, booked)

    # Guarantee the same-day path is demoable while the clinic is open: keep
    # the next two remaining slots today free.
    todays = sorted((s for s in slots.values() if s.start.date() == now.date()), key=lambda s: s.start)
    for slot in todays[:2]:
        slot.booked = False
    return slots


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def phone_readback(value: str) -> str:
    digits = digits_only(value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return digits
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def normalize_dob(value: str) -> Optional[str]:
    """Accept ISO or US-style dates; return ISO or None."""

    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def find_patient(name: str, dob_iso: str) -> Optional[Dict[str, str]]:
    """Match on last name and date of birth.

    First names are the part speech-to-text mangles most ("Mariah" for
    "Maria"), so they are not required to match exactly; date of birth is.
    """

    tokens = [token for token in re.split(r"[^a-z]+", (name or "").lower()) if token]
    for patient in PATIENTS:
        if patient["dob"] == dob_iso and patient["last"].lower() in tokens:
            return patient
    return None
