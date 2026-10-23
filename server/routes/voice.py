"""Voice and intake endpoints.

/chat/send is fire-and-forget (202, then the browser polls history). A voice
turn can't wait on a 1s poll loop, so /voice/turn awaits the interaction
runtime and returns the reply text in the response.
"""

import asyncio
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..agents.interaction_agent.runtime import InteractionAgentRuntime
from ..logging_config import logger
from ..services import get_conversation_log
from ..services.intake import GREETING, get_call_state, get_demo_toggles
from ..utils import error_response

router = APIRouter(tags=["voice"])

# One caller, one conversation: serialise turns so a caller who talks again
# before the reply lands can't interleave two runs over the same call state.
_turn_lock = asyncio.Lock()


class VoiceTurnRequest(BaseModel):
    text: str = Field(..., min_length=1)


class DemoToggleRequest(BaseModel):
    fail_next_tool: Optional[bool] = None
    slot_conflict: Optional[bool] = None


def _turn_payload(reply: str, started: float) -> dict:
    state = get_call_state()
    return {
        "reply": reply,
        "stage": state.stage,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


@router.post("/voice/start", summary="Begin a call: speak and record the greeting")
async def voice_start() -> JSONResponse:
    started = time.perf_counter()
    log = get_conversation_log()
    if not any(True for _ in log.iter_entries()):
        log.record_reply(GREETING)
    return JSONResponse(_turn_payload(GREETING, started))


@router.post("/voice/turn", summary="Run one caller turn and return the reply text")
async def voice_turn(payload: VoiceTurnRequest) -> JSONResponse:
    text = payload.text.strip()
    if not text:
        return error_response("Empty turn", status_code=400)
    try:
        runtime = InteractionAgentRuntime()
    except ValueError as exc:
        return error_response(str(exc), status_code=400)

    started = time.perf_counter()
    async with _turn_lock:
        result = await runtime.execute(user_message=text)
    body = _turn_payload(result.response, started)
    body["llm_calls"] = result.llm_calls
    logger.info("voice turn", extra={"latency_ms": body["latency_ms"], "stage": body["stage"]})
    if not result.success:
        body["error"] = result.error
    return JSONResponse(body)


@router.get("/intake/state", summary="Structured call state for the staff panel")
def intake_state() -> JSONResponse:
    return JSONResponse(get_call_state().staff_view())


@router.post("/intake/demo", summary="Force failure paths for the demo")
def intake_demo(payload: DemoToggleRequest) -> JSONResponse:
    toggles = get_demo_toggles()
    if payload.fail_next_tool is not None:
        toggles.fail_next_tool = payload.fail_next_tool
    if payload.slot_conflict is not None:
        toggles.slot_conflict = payload.slot_conflict
    return JSONResponse(toggles.as_dict())


__all__ = ["router"]
