"""Per-call state for the intake flow, for many concurrent calls.

Each call has an id, its own state and transcript, held in a registry. The
current call travels in a context variable set by the voice route, so the
safety gate, tools and prompt builder all reach the right call through
get_call_state() without threading an id through every signature. Code that
never sets a call (tests, the probe scripts, the text chat path) gets a
default call.

The registry is in-process memory. Running several server processes needs it
in a shared store (Redis or a database) keyed by the same call id.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from .mock_clinic import Slot, get_calendar, phone_readback, reset_calendar
from .transcript import CallTranscript

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
    call_id: str = "default"
    started_at: datetime = field(default_factory=datetime.now)
    last_active: float = field(default_factory=time.monotonic)
    transcript: CallTranscript = field(default_factory=CallTranscript)
    demo: DemoToggles = field(default_factory=DemoToggles)
    patient: Optional[Dict[str, Any]] = None
    intake: Dict[str, Any] = field(default_factory=dict)
    priority: Optional[Dict[str, Any]] = None
    offered_slot_ids: List[str] = field(default_factory=list)
    # The slot reserved for this caller while it's read back; booking can only
    # confirm this one.
    held_slot_id: Optional[str] = None
    booking: Optional[Dict[str, Any]] = None
    emergency: Optional[Dict[str, Any]] = None
    handoff: Optional[Dict[str, Any]] = None
    # Safety question in flight: which flags it covers and the exact wording,
    # so the caller's answer is judged against what they were actually asked.
    pending_check: List[str] = field(default_factory=list)
    pending_question: Optional[str] = None
    pending_labels: Optional[str] = None
    pending_category: str = "medical"
    pending_repeats: int = 0
    # Asked about and answered no: never re-asked, nudges priority up.
    cleared_flags: List[str] = field(default_factory=list)
    # Caller ruled it out unprompted ("no chest pain"): never asked about.
    denied_flags: List[str] = field(default_factory=list)
    tool_failures: int = 0
    events: List[Dict[str, str]] = field(default_factory=list)

    @property
    def calendar(self) -> Dict[str, Slot]:
        """The practice's shared calendar; every call books against the same one."""
        return get_calendar()

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
            "denied_by_caller": self.denied_flags,
            "offered_slots": [
                self.calendar[slot_id].as_option(now.date())
                for slot_id in self.offered_slot_ids
                if slot_id in self.calendar and self.calendar[slot_id].is_open_for(self.call_id)
            ],
            "held_slot": (
                self.calendar[self.held_slot_id].as_option(now.date())
                if self.held_slot_id in self.calendar
                else None
            ),
            "booking": self.booking,
            "emergency": self.emergency,
            "handoff": self.handoff,
        }
        if self.patient:
            # Minimum necessary: once verified, the model has no use for the
            # date of birth, so it stays out of the prompt (staff view adds it).
            view["caller"] = {
                "name": self.patient["name"],
                "verified": not self.patient.get("new_patient", False),
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
        if self.patient and view.get("caller"):
            view["caller"]["dob"] = self.patient["dob"]
        view["call_id"] = self.call_id
        view["events"] = self.events
        view["pending_safety_check"] = self.pending_question
        view["demo"] = self.demo.as_dict()
        return view


# Calls idle this long are dropped. A phone call that silent has ended.
CALL_IDLE_TIMEOUT_SECONDS = 30 * 60
MAX_CALLS = 5000

DEFAULT_CALL_ID = "default"
_lock = threading.Lock()
_calls: Dict[str, CallState] = {DEFAULT_CALL_ID: CallState()}
_current_call: contextvars.ContextVar[str] = contextvars.ContextVar("intake_call_id", default=DEFAULT_CALL_ID)


def _evict_idle_locked() -> None:
    cutoff = time.monotonic() - CALL_IDLE_TIMEOUT_SECONDS
    for call_id in [cid for cid, call in _calls.items() if call.last_active < cutoff and cid != DEFAULT_CALL_ID]:
        del _calls[call_id]
    if len(_calls) > MAX_CALLS:  # oldest first, as a memory backstop
        for call_id in sorted(_calls, key=lambda cid: _calls[cid].last_active)[: len(_calls) - MAX_CALLS]:
            if call_id != DEFAULT_CALL_ID:
                del _calls[call_id]


def start_call() -> CallState:
    """Open a new call with a fresh id."""

    with _lock:
        _evict_idle_locked()
        call = CallState(call_id=uuid.uuid4().hex[:12])
        _calls[call.call_id] = call
        return call


def find_call(call_id: str) -> Optional[CallState]:
    with _lock:
        return _calls.get(call_id)


@contextmanager
def use_call(call_id: str) -> Iterator[CallState]:
    """Make call_id the current call for everything inside the block."""

    call = find_call(call_id)
    if call is None:
        raise KeyError(call_id)
    call.last_active = time.monotonic()
    token = _current_call.set(call_id)
    try:
        yield call
    finally:
        _current_call.reset(token)


def get_call_state() -> CallState:
    call_id = _current_call.get()
    with _lock:
        call = _calls.get(call_id)
        if call is None:  # evicted mid-flight, or the default was cleared
            call = _calls.setdefault(call_id, CallState(call_id=call_id))
        return call


def get_demo_toggles() -> DemoToggles:
    return get_call_state().demo


def active_call_count() -> int:
    with _lock:
        return len(_calls) - (1 if DEFAULT_CALL_ID in _calls else 0)


def reset_call_state() -> CallState:
    """Fresh state for the current call, and a fresh practice calendar.

    For single-call use (tests, probes, the default call). Multi-call clients
    start a new call instead, which leaves other calls untouched.
    """

    call_id = _current_call.get()
    reset_calendar()
    with _lock:
        call = CallState(call_id=call_id)
        _calls[call_id] = call
        return call


def clear_all_calls() -> None:
    """Drop every call and rebuild the calendar (the DELETE /chat/history reset)."""

    reset_calendar()
    with _lock:
        _calls.clear()
        _calls[DEFAULT_CALL_ID] = CallState()
