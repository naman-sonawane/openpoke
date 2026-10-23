"""Interaction agent helpers for prompt construction."""

import json
import re
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

from ...config import get_settings
from ...services.execution import get_agent_roster
from ...services.intake import get_call_state

_prompt_path = Path(__file__).parent / "system_prompt.md"
SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8").strip()
INTAKE_PROMPT = (Path(__file__).parent / "intake_prompt.md").read_text(encoding="utf-8").strip()

#: How many trailing conversation entries feed agent selection. A follow-up such
#: as "did she reply?" shares no words with any agent name, so the selector has
#: to see what came before it.
RECENT_CONTEXT_ENTRIES = 6

_ENTRY_RE = re.compile(r"<(\w+)(?:\s[^>]*)?>(.*?)</\1>", re.DOTALL)


# Load and return the pre-defined system prompt from markdown file
def build_system_prompt() -> str:
    """Return the static system prompt for the interaction agent."""
    return INTAKE_PROMPT if get_settings().intake_mode else SYSTEM_PROMPT


# Build structured message with conversation history, active agents, and current turn
def prepare_message_with_history(
    latest_text: str,
    transcript: str,
    message_type: str = "user",
    safety_note: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Compose a message that bundles history, roster, and the latest turn."""
    sections: List[str] = []

    sections.append(_render_conversation_history(transcript))
    if get_settings().intake_mode:
        # The receptionist has no use for the email agents; it sees the call's
        # structured state instead.
        state = json.dumps(get_call_state().prompt_view(), indent=1, default=str)
        sections.append(f"<call_state>\n{state}\n</call_state>")
    else:
        sections.append(
            f"<active_agents>\n{_render_active_agents(latest_text, transcript)}\n</active_agents>"
        )
    if safety_note:
        sections.append(f"<safety_note>\n{safety_note}\n</safety_note>")
    sections.append(_render_current_turn(latest_text, message_type))

    content = "\n\n".join(sections)
    return [{"role": "user", "content": content}]


# Format conversation transcript into XML tags for LLM context
def _render_conversation_history(transcript: str) -> str:
    history = transcript.strip()
    if not history:
        history = "None"
    return f"<conversation_history>\n{history}\n</conversation_history>"


# Extract the payloads of the last few conversation entries for agent selection
def _recent_context(transcript: str, latest_text: str) -> str:
    payloads = [match.group(2).strip() for match in _ENTRY_RE.finditer(transcript or "")]
    recent = payloads[-RECENT_CONTEXT_ENTRIES:]
    return "\n".join([*recent, latest_text or ""])


# Format the execution agents relevant to this turn into XML tags for LLM awareness
def _render_active_agents(latest_text: str = "", transcript: str = "") -> str:
    roster = get_agent_roster()
    roster.load()
    roster.archive_stale()

    context = _recent_context(transcript, latest_text)
    records = roster.get_agents_for_prompt(context)

    if not records:
        return "None"

    rendered: List[str] = []
    for record in records:
        name = escape(record.name or "agent", quote=True)
        parts = [f'name="{name}"']
        if record.description:
            parts.append(f'purpose="{escape(record.description, quote=True)}"')
        if record.emails:
            parts.append(f'contacts="{escape(", ".join(record.emails[:3]), quote=True)}"')
        if record.archived:
            parts.append('archived="true"')
        rendered.append(f'<agent {" ".join(parts)} />')

    counts = roster.counts()
    hidden = counts["total"] - len(records)
    if hidden > 0:
        rendered.append(
            f"<!-- {hidden} further agents are not shown. If none of the agents "
            f"above fits, reuse one by its exact name or create a new one. -->"
        )

    return "\n".join(rendered)


# Wrap the current message in appropriate XML tags based on sender type
def _render_current_turn(latest_text: str, message_type: str) -> str:
    tag = "new_agent_message" if message_type == "agent" else "new_user_message"
    body = latest_text.strip()
    return f"<{tag}>\n{body}\n</{tag}>"
