"""Doctor's-office intake: safety screen, priority rules, mocked scheduling."""

from .mock_clinic import CLINIC_NAME
from .service import GREETING, GateDecision, allowed_tools, handoff_on_error, pre_screen, run_tool
from .state import (
    CallState,
    active_call_count,
    clear_all_calls,
    find_call,
    get_call_state,
    get_demo_toggles,
    reset_call_state,
    start_call,
    use_call,
)

__all__ = [
    "CLINIC_NAME",
    "GREETING",
    "CallState",
    "GateDecision",
    "active_call_count",
    "allowed_tools",
    "clear_all_calls",
    "find_call",
    "get_call_state",
    "get_demo_toggles",
    "handoff_on_error",
    "pre_screen",
    "reset_call_state",
    "run_tool",
    "start_call",
    "use_call",
]
