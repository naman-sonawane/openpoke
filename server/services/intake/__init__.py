"""Doctor's-office intake: safety screen, priority rules, mocked scheduling."""

from .mock_clinic import CLINIC_NAME
from .service import GREETING, GateDecision, allowed_tools, handoff_on_error, pre_screen, run_tool
from .state import CallState, get_call_state, get_demo_toggles, reset_call_state

__all__ = [
    "CLINIC_NAME",
    "GREETING",
    "CallState",
    "GateDecision",
    "allowed_tools",
    "get_call_state",
    "get_demo_toggles",
    "handoff_on_error",
    "pre_screen",
    "reset_call_state",
    "run_tool",
]
