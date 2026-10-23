"""Tool schemas for the intake receptionist, filtered by call stage."""

from typing import Any, Dict, Iterable, List

from ...services.intake import allowed_tools

_NULLABLE_INT = {"type": ["integer", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}

INTAKE_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "lookup_patient": {
        "description": "Find the caller in the patient records by full name and date of birth.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Caller's first and last name."},
                "date_of_birth": {"type": "string", "description": "Date of birth as YYYY-MM-DD."},
            },
            "required": ["name", "date_of_birth"],
        },
    },
    "register_new_patient": {
        "description": "Create a record for a caller who is not in the system and says they are a new patient.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["name", "date_of_birth"],
        },
    },
    "record_intake": {
        "description": (
            "Save details the caller has given: reason for visit, callback number, whether they "
            "confirmed the callback number after you read it back, or short notes. Use it for "
            "corrections too."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason_for_visit": {"type": "string", "description": "Short summary in the caller's own terms."},
                "callback_number": {"type": "string", "description": "Digits of the best number to reach them."},
                "callback_confirmed": {"type": "boolean", "description": "True only after the caller said yes to your read-back."},
                "notes": {"type": "string"},
            },
        },
    },
    "assign_priority": {
        "description": (
            "Compute how soon the caller should be seen from the facts gathered. The rules decide; "
            "pass null for anything the caller did not say rather than guessing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason_for_visit": {"type": "string"},
                "visit_type": {
                    "type": "string",
                    "enum": ["new_problem", "follow_up", "medication_refill", "wellness", "other"],
                },
                "severity": {**_NULLABLE_INT, "description": "Caller's own 0-10 rating, if asked and given."},
                "onset_days": {"type": ["number", "null"], "description": "Days since it started."},
                "worsening": {**_NULLABLE_BOOL, "description": "Is it getting worse?"},
                "high_risk": {
                    **_NULLABLE_BOOL,
                    "description": "Pregnant, an infant, immunocompromised, or otherwise high-risk, if the caller said so.",
                },
            },
            "required": ["reason_for_visit", "visit_type"],
        },
    },
    "check_availability": {
        "description": "Find open appointment slots within the window set by the assigned priority.",
        "parameters": {
            "type": "object",
            "properties": {
                "preferred_day": {"type": "string", "description": "Optional: 'today', 'tomorrow', a weekday name, or YYYY-MM-DD."},
                "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]},
            },
        },
    },
    "hold_slot": {
        "description": (
            "Reserve the slot the caller picked, so nobody else can take it while you read it back. "
            "Returns the exact wording to read back. Call again if they change their mind."
        ),
        "parameters": {
            "type": "object",
            "properties": {"slot_id": {"type": "string", "description": "A slot_id from check_availability."}},
            "required": ["slot_id"],
        },
    },
    "book_appointment": {
        "description": (
            "Book the held slot. Only call after reading back the held time and the caller saying yes. "
            "The callback number must already be confirmed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "caller_confirmed": {"type": "boolean"},
                "slot_id": {"type": "string", "description": "Optional; must be the held slot."},
            },
            "required": ["caller_confirmed"],
        },
    },
    "flag_emergency": {
        "description": (
            "Use if the caller describes something that could be a medical emergency right now "
            "(even if worded unusually). Stops booking and gives 911 instructions."
        ),
        "parameters": {
            "type": "object",
            "properties": {"description": {"type": "string", "description": "What the caller described."}},
            "required": ["description"],
        },
    },
    "transfer_to_human": {
        "description": (
            "Hand the call to staff with a summary. Use when the caller asks for a person, you are "
            "unsure what they need or have misunderstood twice, the topic is sensitive, they ask a "
            "clinical question you can't answer, or a tool fails."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": [
                        "caller_request",
                        "uncertain",
                        "sensitive",
                        "clinical_question",
                        "tool_failure",
                        "no_availability",
                        "emergency_followup",
                        "other",
                    ],
                },
                "note": {"type": "string", "description": "One or two sentences for the staff member picking up."},
            },
            "required": ["reason", "note"],
        },
    },
}


def get_intake_tool_schemas(already_used: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """OpenAI-style schemas for the tools allowed in the current call stage.

    Tools already called earlier in this turn stay defined even if the stage
    has moved on: the provider rejects a history containing calls to undefined
    tools. Calling one again is still refused by the stage allow-list.
    """

    names = list(allowed_tools())
    names += [name for name in already_used if name in INTAKE_TOOL_SCHEMAS and name not in names]
    return [
        {"type": "function", "function": {"name": name, **INTAKE_TOOL_SCHEMAS[name]}}
        for name in names
    ]
