"""Intake flow logic: the per-turn safety gate and the tool implementations.

Everything here is deterministic and synchronous. The interaction agent decides
what to say and which tool to call; this module decides what is allowed to
happen. Booking, for instance, is refused unless the caller is identified, has
a priority, and has confirmed the read-back, however the model was prompted.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ...logging_config import logger
from .mock_clinic import (
    CLINIC_NAME,
    digits_only,
    find_patient,
    normalize_dob,
    phone_readback,
    try_book,
)
from . import triage
from .priority import VISIT_TYPES, assess_priority
from .red_flags import RedFlag, get_flag, screen
from .state import CallState, get_call_state, get_demo_toggles

ToolOutcome = Tuple[bool, Dict[str, Any]]

# Disclosure up front: AI, not a clinician (CA AB 3030 / AB 489, UT, TX
# TRAIGA), the call is transcribed (all-party consent states), and a person is
# available at any time.
GREETING = (
    f"Thanks for calling {CLINIC_NAME}. I'm the practice's automated AI assistant, "
    "not a person or a clinician, and this call is transcribed for our records. "
    "You can ask for a staff member at any time. Can I start with your first and last name?"
)

EMERGENCY_SCRIPT = (
    "I'm going to stop the booking here, because what you're describing could be "
    "a medical emergency. Please hang up and call 911 right now, or ask someone "
    "near you to call. I've also alerted our clinical team."
)

CRISIS_SCRIPT = (
    "Thank you for telling me. Your safety matters most right now. If you might "
    "act on these thoughts, please call 911. You can also call or text 988 any "
    "time to reach the Suicide and Crisis Lifeline. I'm connecting you with a "
    "member of our staff now."
)

UNCLEAR_CHECK_SCRIPT = (
    "I want to make sure you get the right help, so I'm connecting you with one "
    "of our nurses now. If anything gets worse while you wait, please hang up "
    "and call 911."
)

# Tools exposed to the model in each stage. Fewer tools per turn means fewer
# wrong calls and a shorter prompt, the same lesson as the roster work.
STAGE_TOOLS: Dict[str, Tuple[str, ...]] = {
    "identify": ("lookup_patient", "register_new_patient", "record_intake", "flag_emergency", "transfer_to_human"),
    "intake": ("record_intake", "assign_priority", "lookup_patient", "flag_emergency", "transfer_to_human"),
    "schedule": (
        "check_availability",
        "book_appointment",
        "record_intake",
        "assign_priority",
        "lookup_patient",
        "flag_emergency",
        "transfer_to_human",
    ),
    "booked": ("record_intake", "transfer_to_human"),
    "emergency": ("transfer_to_human",),
    # Kept non-empty so the model can still add a note for staff.
    "handoff": ("record_intake",),
}

# Tools that hit the (mocked) practice system and so can fail.
_BACKEND_TOOLS = {"lookup_patient", "register_new_patient", "check_availability", "book_appointment"}


# ---------------------------------------------------------------------------
# Per-turn safety gate
# ---------------------------------------------------------------------------


@dataclass
class GateDecision:
    """reply set: answer the caller with it and skip the model this turn.
    note set: pass it to the model alongside the caller's words."""

    reply: Optional[str] = None
    note: Optional[str] = None


def _declare_emergency(state: CallState, flag: RedFlag, source: str, evidence: str) -> str:
    state.pending_check = []
    state.pending_question = None
    state.emergency = {"flag": flag.key, "label": flag.label, "category": flag.category, "source": source}
    state.log("emergency", f"{flag.label} ({source}): \"{evidence}\"")
    if flag.category == "crisis":
        _create_handoff(state, "sensitive", f"Caller indicated {flag.label}. 988/911 given.", queue="nurse line")
        return CRISIS_SCRIPT
    state.log("escalation", "911 advised; clinical team alerted")
    return EMERGENCY_SCRIPT


def _most_serious(flags: List[RedFlag]) -> RedFlag:
    return next((flag for flag in flags if flag.category == "crisis"), flags[0])


def _labels(flags: List[RedFlag]) -> str:
    return ", ".join(flag.label for flag in flags)


async def pre_screen(caller_text: str, context: str = "") -> GateDecision:
    """Run the safety screen on a caller turn before the conversation model.

    definite keyword match       -> 911 script, decided in code, no model call
    answer to a pending question -> triage model classifies it; code acts on it
    possible keyword match       -> triage model: emergency / denied / ask
    nothing                      -> conversation model handles the turn
    """

    state = get_call_state()
    if state.stage in ("emergency", "handoff"):
        return GateDecision()

    hits = screen(caller_text)

    definite = next((hit for hit in hits if hit.level == "definite"), None)
    if definite:
        return GateDecision(reply=_declare_emergency(state, definite.flag, "screen", definite.matched))

    notes: List[str] = []
    just_cleared: List[str] = []

    if state.pending_check:
        flags = [get_flag(key) for key in state.pending_check]
        result = await triage.judge_answer(flags, state.pending_question or "", caller_text, context)
        state.log("safety_check", f"answer \"{caller_text}\" -> {result.verdict} ({result.source}: {result.reason})")
        if result.verdict == "emergency":
            return GateDecision(reply=_declare_emergency(state, _most_serious(flags), "confirmed", caller_text))
        if result.verdict in ("repeat", "unclear") and state.pending_repeats == 0:
            # One more try before a nurse: they may not have heard, or may have
            # answered a different question.
            state.pending_repeats = 1
            lead = "Sorry, let me ask again." if result.verdict == "repeat" else "We'll get to that. First, just to check:"
            return GateDecision(reply=f"{lead} {state.pending_question}")
        if result.verdict in ("unclear", "repeat"):
            state.pending_check, state.pending_question = [], None
            _create_handoff(
                state,
                "uncertain",
                f"Unclear answer when asked about {_labels(flags)}: \"{caller_text}\"",
                queue="nurse line",
            )
            return GateDecision(reply=UNCLEAR_CHECK_SCRIPT)
        just_cleared = list(state.pending_check)
        state.cleared_flags.extend(key for key in just_cleared if key not in state.cleared_flags)
        state.pending_check, state.pending_question = [], None
        notes.append(
            f"The caller just answered no to the safety question about {_labels(flags)}. "
            "Acknowledge briefly and carry on with the intake. Don't ask about it again."
        )

    # A negated mention of something already ruled out ("like I said, no chest
    # pain") needs no second look. An affirmative mention always does.
    ruled_out = set(state.cleared_flags) | set(state.denied_flags) | set(just_cleared)
    possible = [
        hit
        for hit in hits
        if hit.level == "possible"
        and not (hit.flag.key in ruled_out and hit.downgraded_because == "negated")
        and hit.flag.key not in just_cleared
    ]
    if possible:
        flags = [hit.flag for hit in possible]
        result = await triage.judge_mention(flags, caller_text, context)
        matched = ", ".join(f"\"{hit.matched}\"" for hit in possible)
        state.log("safety_check", f"{matched} -> {result.verdict} ({result.source}: {result.reason})")
        if result.verdict == "emergency":
            return GateDecision(reply=_declare_emergency(state, _most_serious(flags), "triage", caller_text))
        if result.verdict == "ask":
            state.pending_check = [flag.key for flag in flags]
            state.pending_question = result.question
            state.pending_repeats = 0
            return GateDecision(reply=result.question)
        state.denied_flags.extend(flag.key for flag in flags if flag.key not in state.denied_flags)
        notes.append(
            f"The caller mentioned {_labels(flags)} but isn't describing it as a current symptom. "
            "Don't ask about it; just continue the intake naturally."
        )

    return GateDecision(note=" ".join(notes) or None)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _maybe_fail(tool: str) -> Optional[ToolOutcome]:
    toggles = get_demo_toggles()
    if tool in _BACKEND_TOOLS and toggles.fail_next_tool:
        toggles.fail_next_tool = False
        return _backend_error(tool, "practice management system timed out")
    return None


def _backend_error(tool: str, message: str) -> ToolOutcome:
    state = get_call_state()
    state.tool_failures += 1
    state.log("tool_failure", f"{tool}: {message}")
    return False, {
        "error": message,
        "instruction": (
            "Do not guess or retry silently. Apologise briefly, tell the caller you're "
            "connecting them with the front desk, and call transfer_to_human with reason tool_failure."
        ),
    }


def lookup_patient(name: str, date_of_birth: str) -> ToolOutcome:
    state = get_call_state()
    if not (date_of_birth or "").strip():
        return False, {
            "error": "no date of birth yet",
            "instruction": "You don't have it yet. Simply ask, e.g. 'Thanks. And your date of birth?'",
        }
    dob = normalize_dob(date_of_birth)
    if not dob:
        return False, {"error": "date_of_birth not understood", "instruction": "Ask for the date of birth again: month, day and year."}
    # Injected failures hit only well-formed calls, like a real outage would.
    failure = _maybe_fail("lookup_patient")
    if failure:
        return failure
    patient = find_patient(name, dob)
    if not patient:
        state.log("lookup", f"no match for {name}, {dob}")
        return True, {
            "found": False,
            "instruction": (
                "Read the name and date of birth back once to check for a mishearing. "
                "If they are right, they may be a new patient: ask, and if so call register_new_patient."
            ),
        }
    full_name = f"{patient['first']} {patient['last']}"
    state.patient = {
        "id": patient["id"],
        "name": full_name,
        "dob": dob,
        "phone_on_file": patient["phone"],
        "new_patient": False,
    }
    state.log("identified", f"{full_name} ({patient['id']})")
    return True, {
        "found": True,
        "patient_id": patient["id"],
        "name_on_file": full_name,
        "phone_on_file_last4": patient["phone"][-4:],
    }


def register_new_patient(name: str, date_of_birth: str) -> ToolOutcome:
    state = get_call_state()
    dob = normalize_dob(date_of_birth)
    if not dob or not name.strip():
        return False, {"error": "need a full name and a valid date of birth"}
    state.patient = {
        "id": f"NEW-{random.randint(1000, 9999)}",
        "name": name.strip().title(),
        "dob": dob,
        "phone_on_file": None,
        "new_patient": True,
    }
    state.log("identified", f"new patient {state.patient['name']}")
    return True, {"registered": True, "patient_id": state.patient["id"], "instruction": "You will need a callback number before booking."}


def record_intake(
    reason_for_visit: Optional[str] = None,
    callback_number: Optional[str] = None,
    callback_confirmed: Optional[bool] = None,
    notes: Optional[str] = None,
) -> ToolOutcome:
    state = get_call_state()
    result: Dict[str, Any] = {"recorded": []}
    if reason_for_visit:
        state.intake["reason_for_visit"] = reason_for_visit
        result["recorded"].append("reason_for_visit")
    if callback_number:
        digits = digits_only(callback_number)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            return False, {"error": f"callback number has {len(digits)} digits, expected 10", "instruction": "Ask the caller to repeat the number slowly."}
        if digits != digits_only(state.intake.get("callback_number") or ""):
            state.intake["callback_confirmed"] = False
        state.intake["callback_number"] = digits
        result["recorded"].append("callback_number")
        result["read_back_as"] = phone_readback(digits)
    if callback_confirmed is not None:
        state.intake["callback_confirmed"] = bool(callback_confirmed)
        result["recorded"].append("callback_confirmed")
    if notes:
        state.intake["notes"] = (state.intake.get("notes", "") + " " + notes).strip()
        result["recorded"].append("notes")
    state.log("intake", ", ".join(result["recorded"]) or "nothing new")
    return True, result


def assign_priority(
    reason_for_visit: str,
    visit_type: str,
    severity: Optional[int] = None,
    onset_days: Optional[float] = None,
    worsening: Optional[bool] = None,
    high_risk: Optional[bool] = None,
) -> ToolOutcome:
    state = get_call_state()
    if visit_type not in VISIT_TYPES:
        visit_type = "other"
    decision = assess_priority(
        reason_for_visit=reason_for_visit,
        visit_type=visit_type,
        severity=severity,
        onset_days=onset_days,
        worsening=worsening,
        high_risk=high_risk,
        cleared_red_flags=[get_flag(key).label for key in state.cleared_flags],
    )
    state.intake.update(
        {
            "reason_for_visit": reason_for_visit,
            "visit_type": visit_type,
            "severity": severity,
            "onset_days": onset_days,
            "worsening": worsening,
            "high_risk": high_risk,
        }
    )
    state.priority = decision.as_dict()
    state.offered_slot_ids = []
    state.log("priority", f"{decision.level}: " + "; ".join(decision.reasons))
    payload = decision.as_dict()
    payload["instruction"] = (
        "Tell the caller in plain words when they should be seen (the window), without any "
        "clinical judgement, then check availability."
    )
    return True, payload


def _resolve_day(preferred_day: Optional[str], today: date) -> Optional[date]:
    if not preferred_day:
        return None
    text = preferred_day.strip().lower()
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for index, name in enumerate(weekdays):
        if name in text:
            delta = (index - today.weekday()) % 7 or 7
            return today + timedelta(days=delta)
    return None


def _window_bounds(level: str, today: date) -> Tuple[date, date]:
    if level == "same_day":
        return today, today
    if level == "soon":
        end, counted = today, 0
        while counted < 3:
            end += timedelta(days=1)
            if end.weekday() < 5:
                counted += 1
        return today, end
    return today + timedelta(days=1), today + timedelta(days=14)


def check_availability(preferred_day: Optional[str] = None, time_of_day: Optional[str] = None) -> ToolOutcome:
    failure = _maybe_fail("check_availability")
    if failure:
        return failure
    state = get_call_state()
    if not state.priority:
        return False, {"error": "assign_priority must run before checking availability"}

    now = datetime.now()
    level = state.priority["level"]
    start, end = _window_bounds(level, now.date())
    wanted_day = _resolve_day(preferred_day, now.date())

    open_slots = sorted(
        (slot for slot in state.calendar.values() if not slot.booked and slot.start > now),
        key=lambda slot: slot.start,
    )

    def fits_time(slot) -> bool:
        if time_of_day == "morning":
            return slot.start.hour < 12
        if time_of_day == "afternoon":
            return slot.start.hour >= 12
        return True

    in_window = [s for s in open_slots if start <= s.start.date() <= end]
    note = None
    candidates = [s for s in in_window if fits_time(s) and (wanted_day is None or s.start.date() == wanted_day)]

    if not candidates and wanted_day and level == "routine":
        candidates = [s for s in open_slots if s.start.date() == wanted_day and fits_time(s)]
    if not candidates and wanted_day and not (start <= wanted_day <= end):
        note = (
            f"The caller's preferred day is outside the recommended window ({state.priority['window']}). "
            "Offer the in-window options and explain gently why sooner is better; if they still "
            "prefer later, that is their choice for a non-urgent visit, so offer to look at that day."
        )
    if not candidates:
        candidates = [s for s in in_window if fits_time(s)] or in_window

    if not candidates:
        state.log("availability", f"no {level} slots available")
        if level == "same_day":
            return True, {
                "options": [],
                "instruction": (
                    "There are no same-day openings left. Do not book a later day for a same-day need. "
                    "Tell the caller you'll connect them with the nurse line to fit them in, and call "
                    "transfer_to_human with reason no_availability."
                ),
            }
        return True, {"options": [], "instruction": "No openings in the window. Offer to transfer to the front desk."}

    options = candidates[:3]
    # Accumulate: the model sometimes checks two preferences in parallel and
    # may offer from either result.
    for slot in options:
        if slot.id not in state.offered_slot_ids:
            state.offered_slot_ids.append(slot.id)
    state.log("availability", f"offered {', '.join(slot.id for slot in options)}")
    instruction = "Offer at most two of these out loud and ask which works."
    if not state.intake.get("callback_confirmed"):
        instruction += " Once they pick one, confirm the callback number before reading the appointment back."
    payload: Dict[str, Any] = {
        "priority": level,
        "options": [slot.as_option(now.date()) for slot in options],
        "instruction": instruction,
    }
    if note:
        payload["note"] = note
    return True, payload


def book_appointment(slot_id: str, caller_confirmed: bool) -> ToolOutcome:
    failure = _maybe_fail("book_appointment")
    if failure:
        return failure
    state = get_call_state()
    toggles = get_demo_toggles()

    if state.emergency:
        return False, {"error": "booking is blocked: this call was flagged as an emergency"}
    if not state.patient:
        return False, {"error": "caller is not identified yet"}
    if not state.priority:
        return False, {"error": "priority has not been assigned"}
    if not state.callback_number():
        return False, {"error": "no callback number", "instruction": "Ask for the best callback number first."}
    if not state.intake.get("callback_confirmed"):
        return False, {
            "error": "callback number not confirmed",
            "instruction": f"Read back {phone_readback(state.callback_number())} and get a yes, then record_intake(callback_confirmed=true).",
        }
    if not caller_confirmed:
        return False, {"error": "caller has not confirmed", "instruction": "Read back the day, time and provider and get an explicit yes first."}

    slot = state.calendar.get(slot_id)
    if slot is None:
        today = datetime.now().date()
        return False, {
            "error": f"unknown slot_id {slot_id}",
            "offered": [
                state.calendar[sid].as_option(today)
                for sid in state.offered_slot_ids
                if not state.calendar[sid].booked
            ],
            "instruction": "Retry with the slot_id of the option the caller chose. Don't mention this to the caller.",
        }

    if toggles.slot_conflict:
        # Demo switch: simulate another caller taking it first.
        toggles.slot_conflict = False
        try_book(slot_id)
        claimed = False
    else:
        # Atomic across concurrent calls: only one caller can claim a slot.
        claimed = try_book(slot_id)
    if not claimed:
        state.offered_slot_ids = [sid for sid in state.offered_slot_ids if sid != slot_id]
        state.log("conflict", f"{slot_id} was taken before booking")
        alternatives = [
            state.calendar[sid].as_option(datetime.now().date())
            for sid in state.offered_slot_ids
            if not state.calendar[sid].booked
        ]
        return False, {
            "error": "slot_taken",
            "alternatives": alternatives,
            "instruction": (
                "Apologise that the time was just taken. Offer an alternative (or check_availability "
                "again if none), and get a fresh confirmation before booking."
            ),
        }

    confirmation = f"MS-{random.randint(1000, 9999)}"
    state.booking = {
        "confirmation": confirmation,
        "slot_id": slot.id,
        "when": slot.spoken(datetime.now().date()),
        "start": slot.start.isoformat(timespec="minutes"),
        "provider": slot.provider,
        "patient": state.patient["name"],
        "callback_number": phone_readback(state.callback_number()),
        "reason": state.intake.get("reason_for_visit"),
        "priority": state.priority["level"],
    }
    state.log("booked", f"{slot.id} {confirmation}")
    return True, {
        "booked": True,
        "confirmation": confirmation,
        "when": state.booking["when"],
        "instruction": (
            "Confirm the booking in one or two sentences, say a text confirmation will go to their "
            "number, ask them to arrive 10 minutes early, and ask if there's anything else."
        ),
    }


def flag_emergency(description: str) -> ToolOutcome:
    """Model-side backstop for emergencies the keyword screen missed."""

    state = get_call_state()
    state.emergency = {"flag": "model", "label": description, "category": "medical", "source": "model"}
    state.pending_check, state.pending_question = [], None
    state.log("emergency", f"model flagged: {description}")
    state.log("escalation", "911 advised; clinical team alerted")
    return True, {
        "status": "emergency_flagged",
        "instruction": "Say exactly this, then stop: " + EMERGENCY_SCRIPT,
    }


_NURSE_REASONS = {"sensitive", "uncertain", "clinical_question", "no_availability", "emergency_followup"}


def _create_handoff(state: CallState, reason: str, note: str, queue: Optional[str] = None) -> Dict[str, Any]:
    queue = queue or ("nurse line" if reason in _NURSE_REASONS else "front desk")
    summary = {
        "queue": queue,
        "reason": reason,
        "note": note,
        "stage_at_transfer": state.stage,
        "caller": state.patient["name"] if state.patient else "not identified",
        "patient_id": state.patient["id"] if state.patient else None,
        "dob": state.patient["dob"] if state.patient else None,
        "callback_number": phone_readback(state.callback_number()) if state.callback_number() else "not collected",
        "reason_for_visit": state.intake.get("reason_for_visit", "not collected"),
        "priority": state.priority,
        "safety": {
            "emergency": state.emergency,
            "cleared_checks": [get_flag(key).label for key in state.cleared_flags],
        },
        "booking": state.booking,
        "created_at": datetime.now().strftime("%H:%M:%S"),
    }
    state.handoff = summary
    state.log("handoff", f"{queue}: {reason} - {note}")
    logger.info("intake handoff", extra={"queue": queue, "reason": reason})
    return summary


FAILSAFE_SCRIPT = (
    "I'm sorry, I'm having trouble on my end, so I'm connecting you with someone at "
    "the front desk. If this is an emergency, please hang up and call 911."
)


def handoff_on_error(error: str) -> str:
    """The assistant itself failed: hand off rather than leave the caller hanging."""

    state = get_call_state()
    if not state.handoff:
        _create_handoff(state, "tool_failure", f"Assistant error mid-call: {error[:200]}")
    return FAILSAFE_SCRIPT


def transfer_to_human(reason: str, note: str) -> ToolOutcome:
    state = get_call_state()
    summary = _create_handoff(state, reason, note)
    return True, {
        "status": "transferring",
        "queue": summary["queue"],
        "instruction": (
            f"Tell the caller, warmly and in one or two sentences, that you're connecting them with the "
            f"{summary['queue']} and that they've been passed your notes so they won't need to repeat everything."
        ),
    }


TOOL_FUNCTIONS = {
    "lookup_patient": lookup_patient,
    "register_new_patient": register_new_patient,
    "record_intake": record_intake,
    "assign_priority": assign_priority,
    "check_availability": check_availability,
    "book_appointment": book_appointment,
    "flag_emergency": flag_emergency,
    "transfer_to_human": transfer_to_human,
}


def run_tool(name: str, arguments: Dict[str, Any]) -> ToolOutcome:
    """Dispatch a tool call, enforcing the stage's allow-list."""

    state = get_call_state()
    allowed = STAGE_TOOLS.get(state.stage, ())
    if name not in allowed:
        return False, {"error": f"{name} is not available in stage '{state.stage}'", "available": list(allowed)}
    if name == "register_new_patient":
        failure = _maybe_fail(name)
        if failure:
            return failure
    return TOOL_FUNCTIONS[name](**arguments)


def allowed_tools() -> List[str]:
    return list(STAGE_TOOLS.get(get_call_state().stage, ()))
