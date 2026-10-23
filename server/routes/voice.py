"""Voice and intake endpoints, one call per call_id.

/chat/send is fire-and-forget (202, then the browser polls history). A voice
turn can't wait on a 1s poll loop, so /voice/turn awaits the interaction
runtime and returns the reply text in the response.

POST /voice/start opens a call and returns its call_id; every other endpoint
takes it. Requests without a call_id use a single default call, which keeps
older clients and the text chat path working.
"""

import asyncio
import time
from contextlib import nullcontext
from typing import Dict, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..agents.interaction_agent.runtime import InteractionAgentRuntime
from ..logging_config import logger
from ..services.intake import (
    GREETING,
    active_call_count,
    find_call,
    get_call_state,
    get_demo_toggles,
    start_call,
    use_call,
)
from ..utils import error_response

router = APIRouter(tags=["voice"])

# Turns within one call run in order (a caller who talks again before the
# reply lands must not interleave two runs over the same state). Different
# calls never wait on each other.
_call_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(call_id: str) -> asyncio.Lock:
    lock = _call_locks.get(call_id)
    if lock is None:
        if len(_call_locks) > 10_000:  # drop locks of calls that have ended
            for stale in [cid for cid in _call_locks if find_call(cid) is None]:
                _call_locks.pop(stale, None)
        lock = _call_locks[call_id] = asyncio.Lock()
    return lock


def _call_scope(call_id: Optional[str]):
    """Context for a request: the named call, or the default call."""
    return use_call(call_id) if call_id else nullcontext()


def _unknown_call(call_id: str) -> JSONResponse:
    return error_response(f"Unknown or expired call_id {call_id}; start a new call", status_code=404)


class VoiceTurnRequest(BaseModel):
    text: str = Field(..., min_length=1)
    call_id: Optional[str] = None


class DemoToggleRequest(BaseModel):
    call_id: Optional[str] = None
    fail_next_tool: Optional[bool] = None
    slot_conflict: Optional[bool] = None


def _turn_payload(reply: str, started: float) -> dict:
    state = get_call_state()
    return {
        "call_id": state.call_id,
        "reply": reply,
        "stage": state.stage,
        # A handoff means a person takes over: the assistant's part of the call
        # is done, so the client stops listening instead of making small talk.
        "call_ended": state.stage == "handoff",
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


@router.post("/voice/start", summary="Open a new call and return its greeting and call_id")
async def voice_start() -> JSONResponse:
    started = time.perf_counter()
    call = start_call()
    call.transcript.record_reply(GREETING)
    with use_call(call.call_id):
        return JSONResponse(_turn_payload(GREETING, started))


@router.post("/voice/turn", summary="Run one caller turn and return the reply text")
async def voice_turn(payload: VoiceTurnRequest) -> JSONResponse:
    text = payload.text.strip()
    if not text:
        return error_response("Empty turn", status_code=400)
    if payload.call_id and find_call(payload.call_id) is None:
        return _unknown_call(payload.call_id)
    try:
        runtime = InteractionAgentRuntime()
    except ValueError as exc:
        return error_response(str(exc), status_code=400)

    started = time.perf_counter()
    with _call_scope(payload.call_id):
        async with _lock_for(get_call_state().call_id):
            result = await runtime.execute(user_message=text)
        body = _turn_payload(result.response, started)
    body["llm_calls"] = result.llm_calls
    body["timings"] = result.timings
    logger.info(
        "voice turn",
        extra={"latency_ms": body["latency_ms"], "stage": body["stage"], "active_calls": active_call_count()},
    )
    if not result.success:
        body["error"] = result.error
    return JSONResponse(body)


@router.get("/voice/transcript", summary="The call's transcript as chat messages")
def voice_transcript(call_id: Optional[str] = None) -> JSONResponse:
    if call_id and find_call(call_id) is None:
        return _unknown_call(call_id)
    with _call_scope(call_id):
        messages = [m.model_dump() for m in get_call_state().transcript.to_chat_messages()]
    return JSONResponse({"messages": messages})


@router.get("/intake/state", summary="Structured call state for the staff panel")
def intake_state(call_id: Optional[str] = None) -> JSONResponse:
    if call_id and find_call(call_id) is None:
        return _unknown_call(call_id)
    with _call_scope(call_id):
        return JSONResponse(get_call_state().staff_view())


@router.post("/intake/demo", summary="Force failure paths for the demo")
def intake_demo(payload: DemoToggleRequest) -> JSONResponse:
    if payload.call_id and find_call(payload.call_id) is None:
        return _unknown_call(payload.call_id)
    with _call_scope(payload.call_id):
        toggles = get_demo_toggles()
        if payload.fail_next_tool is not None:
            toggles.fail_next_tool = payload.fail_next_tool
        if payload.slot_conflict is not None:
            toggles.slot_conflict = payload.slot_conflict
        return JSONResponse(toggles.as_dict())


__all__ = ["router"]
