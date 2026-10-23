"""Per-call transcript with the same interface as OpenPoke's ConversationLog.

OpenPoke keeps one conversation log file for its one user. A phone line has
many concurrent callers, so each call gets its own transcript, held with the
call's state. The runtime uses it in place of the shared log in intake mode.
"""

from __future__ import annotations

import threading
from datetime import datetime
from html import escape
from typing import Iterator, List, Tuple

from ...models import ChatMessage


class CallTranscript:
    def __init__(self) -> None:
        self._entries: List[Tuple[str, str, str]] = []
        self._lock = threading.Lock()

    def _append(self, tag: str, payload: str) -> None:
        with self._lock:
            self._entries.append((tag, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(payload)))

    def record_user_message(self, content: str) -> None:
        self._append("user_message", content)

    def record_agent_message(self, content: str) -> None:
        self._append("agent_message", content)

    def record_reply(self, content: str) -> None:
        self._append("poke_reply", content)

    def record_wait(self, reason: str) -> None:
        self._append("wait", reason)

    def iter_entries(self) -> Iterator[Tuple[str, str, str]]:
        with self._lock:
            entries = list(self._entries)
        return iter(entries)

    def load_transcript(self) -> str:
        return "\n".join(
            f'<{tag} timestamp="{timestamp}">{escape(payload, quote=False)}</{tag}>'
            for tag, timestamp, payload in self.iter_entries()
        )

    def to_chat_messages(self) -> List[ChatMessage]:
        roles = {"user_message": "user", "poke_reply": "assistant"}
        return [
            ChatMessage(role=roles[tag], content=payload, timestamp=timestamp)
            for tag, timestamp, payload in self.iter_entries()
            if tag in roles
        ]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
