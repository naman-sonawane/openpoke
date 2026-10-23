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

    async def fake_llm(*, messages, **kwargs):
        await asyncio.sleep(FAKE_LATENCY)
        latest = re.search(r"<new_user_message>\n(.*?)\n</new_user_message>", messages[0]["content"], re.S)
        return {"choices": [{"message": {"content": f"Heard: {latest.group(1) if latest else ''}", "tool_calls": []}}]}

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
