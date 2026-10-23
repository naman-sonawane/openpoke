"""Intake safety screen, priority rules and booking guards.

All deterministic: no LLM, no network. These are the parts of the intake flow
that must not depend on how the model was prompted.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def intake(sandbox):
    modules = {
        name: importlib.import_module(f"server.services.intake.{name}")
        for name in ("red_flags", "priority", "service", "state")
    }
    modules["state"].reset_call_state()
    yield modules
    modules["state"].reset_call_state()


def _levels(red_flags, text):
    return [(hit.flag.key, hit.level) for hit in red_flags.screen(text)]


# --- red-flag screen -------------------------------------------------------


@pytest.mark.parametrize(
    "text, key",
    [
        ("I have really bad chest pain", "chest_pain"),
        ("my husband can't breathe", "breathing"),
        ("her face is drooping on one side", "stroke"),
        ("I've been thinking about killing myself", "self_harm"),
        ("no fever, but I have chest pain", "chest_pain"),
        ("the bleeding won't stop", "bleeding"),
    ],
)
def test_unambiguous_phrases_are_definite(intake, text, key):
    assert (key, "definite") in _levels(intake["red_flags"], text)


@pytest.mark.parametrize(
    "text, key",
    [
        ("no chest pain, just a cough", "chest_pain"),
        ("I don't have any chest pain", "chest_pain"),
        ("no chest pain or shortness of breath", "breathing"),
        ("my dad had a stroke last year", "stroke"),
        ("my chest feels tight", "chest_pain"),
        ("I'm not suicidal", "self_harm"),
    ],
)
def test_negated_or_ambiguous_phrases_need_confirmation(intake, text, key):
    assert (key, "possible") in _levels(intake["red_flags"], text)


@pytest.mark.parametrize(
    "text",
    ["I need a refill on my blood pressure meds", "sore throat for three days", "I'm confused about my bill"],
)
def test_routine_requests_raise_nothing(intake, text):
    assert intake["red_flags"].screen(text) == []


@pytest.mark.parametrize(
    "answer, expected",
    [("no", "no"), ("nope, not at all", "no"), ("yes", "yes"), ("a little bit", "yes"),
     ("I'm not sure", "yes"), ("I don't think so", "no"), ("well it started tuesday", "unclear")],
)
def test_confirmation_classifier_treats_doubt_as_yes(intake, answer, expected):
    assert intake["red_flags"].classify_confirmation(answer) == expected


# --- gate ------------------------------------------------------------------


def test_definite_red_flag_goes_straight_to_911(intake):
    decision = intake["service"].pre_screen("I'm having crushing chest pain right now")
    assert "911" in decision.reply
    assert intake["state"].get_call_state().stage == "emergency"


def test_negated_flag_asks_once_then_continues_on_no(intake):
    service, state = intake["service"], intake["state"]
    first = service.pre_screen("no chest pain, just a bad cough")
    assert first.reply and "chest pain" in first.reply.lower()

    second = service.pre_screen("no, nothing like that")
    assert second.reply is None and "not currently having chest pain" in second.note
    assert state.get_call_state().stage == "identify"

    # Mentioning it again in the negative must not re-ask.
    assert service.pre_screen("like I said no chest pain").reply is None


def test_yes_to_confirm_question_is_an_emergency(intake):
    service = intake["service"]
    service.pre_screen("my chest feels tight")
    decision = service.pre_screen("yeah kind of")
    assert "911" in decision.reply


def test_unclear_answer_hands_off_to_nurse(intake):
    service, state = intake["service"], intake["state"]
    service.pre_screen("my chest feels tight")
    decision = service.pre_screen("well it comes and goes, mostly at night")
    assert "nurse" in decision.reply
    assert state.get_call_state().handoff["queue"] == "nurse line"


def test_crisis_gives_988_and_hands_off(intake):
    decision = intake["service"].pre_screen("I just want to end my life")
    assert "988" in decision.reply
    assert intake["state"].get_call_state().handoff is not None


# --- priority --------------------------------------------------------------


def test_refill_is_routine(intake):
    decision = intake["priority"].assess_priority(reason_for_visit="blood pressure refill", visit_type="medication_refill")
    assert decision.level == "routine"


def test_fever_is_same_day_with_reason(intake):
    decision = intake["priority"].assess_priority(
        reason_for_visit="fever and sore throat", visit_type="new_problem", severity=3, onset_days=2
    )
    assert decision.level == "same_day"
    assert any("fever" in reason for reason in decision.reasons)


def test_missing_information_rounds_up(intake):
    decision = intake["priority"].assess_priority(reason_for_visit="my knee", visit_type="new_problem")
    assert decision.level == "soon" and decision.needs_nurse_review


def test_ruled_out_warning_sign_is_soon_with_nurse_review(intake):
    decision = intake["priority"].assess_priority(
        reason_for_visit="cough", visit_type="new_problem", severity=1, onset_days=5,
        cleared_red_flags=["chest pain"],
    )
    assert decision.level == "soon" and decision.needs_nurse_review


def test_high_severity_is_same_day(intake):
    decision = intake["priority"].assess_priority(
        reason_for_visit="back pain", visit_type="new_problem", severity=8, onset_days=1
    )
    assert decision.level == "same_day"


# --- booking guards --------------------------------------------------------


def _identified_with_priority(service):
    ok, _ = service.lookup_patient("Maria Garcia", "1985-03-14")
    assert ok
    service.assign_priority(reason_for_visit="annual physical", visit_type="wellness")
    ok, payload = service.check_availability()
    assert ok and payload["options"]
    return payload["options"][0]["slot_id"]


def test_booking_requires_confirmed_callback_and_caller_yes(intake):
    service, state = intake["service"], intake["state"]
    slot_id = _identified_with_priority(service)

    ok, payload = service.book_appointment(slot_id, caller_confirmed=True)
    assert not ok and payload["error"] == "callback number not confirmed"

    service.record_intake(callback_confirmed=True)
    ok, payload = service.book_appointment(slot_id, caller_confirmed=False)
    assert not ok and payload["error"] == "caller has not confirmed"

    ok, payload = service.book_appointment(slot_id, caller_confirmed=True)
    assert ok and state.get_call_state().stage == "booked"


def test_slot_conflict_returns_alternatives(intake):
    service, state = intake["service"], intake["state"]
    slot_id = _identified_with_priority(service)
    service.record_intake(callback_confirmed=True)
    state.get_demo_toggles().slot_conflict = True

    ok, payload = service.book_appointment(slot_id, caller_confirmed=True)
    assert not ok and payload["error"] == "slot_taken"
    assert all(option["slot_id"] != slot_id for option in payload["alternatives"])


def test_tool_failure_instructs_handoff(intake):
    service, state = intake["service"], intake["state"]
    state.get_demo_toggles().fail_next_tool = True
    ok, payload = service.run_tool("lookup_patient", {"name": "Maria Garcia", "date_of_birth": "1985-03-14"})
    assert not ok and "transfer_to_human" in payload["instruction"]


def test_booking_tools_are_not_exposed_after_an_emergency(intake):
    service = intake["service"]
    service.pre_screen("I can't breathe")
    assert "book_appointment" not in service.allowed_tools()
    ok, payload = service.run_tool("book_appointment", {"slot_id": "x", "caller_confirmed": True})
    assert not ok and "not available" in payload["error"]
