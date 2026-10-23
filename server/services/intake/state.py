"""Per-call state for the intake flow.

One call at a time, held in memory: this is a demo, and the conversation log
it sits beside is also a single shared transcript. reset_call_state() is wired
to DELETE /chat/history so each demo scenario starts clean.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .mock_clinic import Slot, build_calendar, phone_readback

STAGES = ("identify", "intake", "schedule", "booked", "emergency", "handoff")


@dataclass
class DemoToggles:
    """Switches the demo uses to force failure paths on demand."""

    fail_next_tool: bool = False
    slot_conflict: bool = False

    def as_dict(self) -> Dict[str, bool]:
        return {"fail_next_tool": self.fail_next_tool, "slot_conflict": self.slot_conflict}


@dataclass
class CallState:
    started_at: datetime = field(default_factory=datetime.now)
    patient: Optional[Dict[str, Any]] = None
    intake: Dict[str, Any] = field(default_factory=dict)
    priority: Optional[Dict[str, Any]] = None
    offered_slot_ids: List[str] = field(default_factory=list)
    booking: Optional[Dict[str, Any]] = None
    emergency: Optional[Dict[str, Any]] = None
    handoff: Optional[Dict[str, Any]] = None
    pending_check: Optional[str] = None
    cleared_flags: List[str] = field(default_factory=list)
    tool_failures: int = 0
    events: List[Dict[str, str]] = field(default_factory=list)
    calendar: Dict[str, Slot] = field(default_factory=build_calendar)

    @property
    def stage(self) -> str:
        if self.handoff:
            return "handoff"
        if self.emergency:
            return "emergency"
        if self.booking:
            return "booked"
        if not self.patient:
            return "identify"
        if not self.priority:
            return "intake"
        return "schedule"

    def log(self, kind: str, detail: str) -> None:
        self.events.append({"at": datetime.now().strftime("%H:%M:%S"), "kind": kind, "detail": detail})

    def callback_number(self) -> Optional[str]:
        number = self.intake.get("callback_number")
        if number:
            return number
        if self.patient:
            return self.patient.get("phone_on_file")
        return None

    def prompt_view(self) -> Dict[str, Any]:
        """What the model sees about the call each turn: facts, not history."""

        now = datetime.now()
        callback = self.callback_number()
        view: Dict[str, Any] = {
            "now": now.strftime("%A, %B %d, %Y, %I:%M %p"),
            "stage": self.stage,
            "caller": None,
            "callback_number": None,
            "intake": {k: v for k, v in self.intake.items() if k != "callback_number"},
            "priority": self.priority,
            "cleared_safety_checks": self.cleared_flags,
            "offered_slots": [
                self.calendar[slot_id].as_option(now.date())
                for slot_id in self.offered_slot_ids
                if slot_id in self.calendar and not self.calendar[slot_id].booked
            ],
            "booking": self.booking,
            "emergency": self.emergency,
            "handoff": self.handoff,
        }
        if self.patient:
            view["caller"] = {
                "name": self.patient["name"],
                "dob": self.patient["dob"],
                "new_patient": self.patient.get("new_patient", False),
            }
        if callback:
            view["callback_number"] = {
                "number": phone_readback(callback),
                "confirmed_by_caller": bool(self.intake.get("callback_confirmed")),
                "source": "caller" if self.intake.get("callback_number") else "on file",
            }
        return view

    def staff_view(self) -> Dict[str, Any]:
        view = self.prompt_view()
        view["events"] = self.events
        view["pending_safety_check"] = self.pending_check
        view["demo"] = _toggles.as_dict()
        return view


_lock = threading.Lock()
_state = CallState()
_toggles = DemoToggles()


def get_call_state() -> CallState:
    return _state


def get_demo_toggles() -> DemoToggles:
    return _toggles


def reset_call_state() -> CallState:
    """Start a fresh call. Demo toggles reset too, so each scenario is clean."""

    global _state, _toggles
    with _lock:
        _state = CallState()
        _toggles = DemoToggles()
        return _state
