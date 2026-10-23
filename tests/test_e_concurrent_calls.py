"""Many simultaneous calls: isolation, parallelism, and no double-booking.

Drives the real voice route handlers with a fake model that takes 200ms per
call, so the tests can tell parallel turns from serialised ones.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import threading
import time

import pytest

FAKE_LATENCY = 0.2


@pytest.fixture
def intake_server(sandbox, monkeypatch):
    settings = importlib.import_module("server.config").get_settings()
    monkeypatch.setattr(settings, "mode", "intake")
    runtime = importlib.import_module("server.agents.interaction_agent.runtime")
    triage = importlib.import_module("server.services.intake.triage")
    voice = importlib.import_module("server.routes.voice")
    state = importlib.import_module("server.services.intake.state")

    async def fake_llm(*, messages, system="", **kwargs):
        await asyncio.sleep(FAKE_LATENCY)
        content = messages[0]["content"]
        if (system or "").startswith("You are the safety check"):
            # Fake triage: only a newborn with a fever is an emergency.
            urgent = "newborn" in content and "hot" in content
            verdict = '{"verdict": "emergency", "category": "medical", "reason": "newborn fever"}'
            return {"choices": [{"message": {"content": verdict if urgent else '{"verdict": "none"}'}}]}
        latest = re.search(r"<new_user_message>\n(.*?)\n</new_user_message>", content, re.S)
        said = latest.group(1) if latest else ""
        if "newborn" in said and len(messages) == 1:
            # The conversation model tries to act on the flagged turn.
            call = {"id": "c1", "type": "function",
                    "function": {"name": "register_new_patient", "arguments": '{"name": "Test", "date_of_birth": "2026-09-01"}'}}
            return {"choices": [{"message": {"content": "", "tool_calls": [call]}}]}
        return {"choices": [{"message": {"content": f"Heard: {said}", "tool_calls": []}}]}

    monkeypatch.setattr(runtime, "request_chat_completion", fake_llm)
    monkeypatch.setattr(triage, "request_chat_completion", fake_llm)
    state.clear_all_calls()
    yield voice, state
    state.clear_all_calls()


def _body(response):
    return json.loads(response.body)


async def _call(voice, text):
    started = _body(await voice.voice_start())
    turn = _body(await voice.voice_turn(voice.VoiceTurnRequest(text=text, call_id=started["call_id"])))
    return started["call_id"], turn


async def test_fifty_calls_run_in_parallel_and_stay_isolated(intake_server):
    voice, state = intake_server
    t0 = time.perf_counter()
    results = await asyncio.gather(*[_call(voice, f"caller number {i}") for i in range(50)])
    elapsed = time.perf_counter() - t0

    # 50 turns x 200ms would be 10s if turns were serialised.
    assert elapsed < 2.0, f"turns look serialised: {elapsed:.2f}s"

    for i, (call_id, turn) in enumerate(results):
        assert turn["reply"] == f"Heard: caller number {i}"
        transcript = _body(voice.voice_transcript(call_id=call_id))["messages"]
        said = [m["content"] for m in transcript if m["role"] == "user"]
        assert said == [f"caller number {i}"], "another caller's words leaked into this call"


async def test_emergency_on_one_call_does_not_touch_another(intake_server):
    voice, state = intake_server
    (call_a, turn_a), (call_b, turn_b) = await asyncio.gather(
        _call(voice, "I have crushing chest pain right now"),
        _call(voice, "Hi, I'd like to book a physical"),
    )
    assert turn_a["stage"] == "emergency"
    assert turn_b["stage"] == "identify"
    assert _body(voice.intake_state(call_id=call_b))["emergency"] is None


async def test_turns_within_one_call_stay_in_order(intake_server):
    voice, _ = intake_server
    call_id = _body(await voice.voice_start())["call_id"]
    texts = [f"part {i}" for i in range(5)]
    await asyncio.gather(*[voice.voice_turn(voice.VoiceTurnRequest(text=t, call_id=call_id)) for t in texts])
    transcript = _body(voice.voice_transcript(call_id=call_id))["messages"]
    roles = [m["role"] for m in transcript]
    # greeting, then strictly alternating caller/agent: no two runs interleaved
    assert roles == ["assistant"] + ["user", "assistant"] * 5


async def test_always_on_screen_preempts_the_model_before_any_tool_runs(intake_server):
    voice, _ = intake_server
    # No keyword pattern matches this; only the model screen can catch it. The
    # conversation model meanwhile tries to register the patient.
    call_id, turn = await _call(voice, "please book my newborn, she feels really hot")
    assert "911" in turn["reply"]
    state = _body(voice.intake_state(call_id=call_id))
    assert state["stage"] == "emergency"
    assert state["caller"] is None, "a tool ran on a turn the safety screen flagged"


async def test_always_on_screen_adds_no_latency_when_quiet(intake_server):
    voice, _ = intake_server
    _, turn = await _call(voice, "I'd like a check-up")
    # Screen and model each take 200ms and run side by side, not one after the other.
    assert turn["timings"]["screen_wait_ms"] < 100
    assert turn["latency_ms"] < 2 * FAKE_LATENCY * 1000


async def test_unknown_call_id_is_rejected(intake_server):
    voice, _ = intake_server
    response = await voice.voice_turn(voice.VoiceTurnRequest(text="hello", call_id="nope"))
    assert response.status_code == 404


def test_a_slot_can_only_be_booked_once_under_contention(intake_server):
    _, state = intake_server
    clinic = importlib.import_module("server.services.intake.mock_clinic")
    slot_id = next(sid for sid, slot in clinic.get_calendar().items() if not slot.booked)
    wins = []
    barrier = threading.Barrier(32)

    def contend():
        barrier.wait()
        wins.append(clinic.try_book(slot_id))

    threads = [threading.Thread(target=contend) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert wins.count(True) == 1
