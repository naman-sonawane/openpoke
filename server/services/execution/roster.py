"""Agent roster with per-agent context, relevance selection, and archiving.

The roster used to be a flat list of every agent name ever created, rendered in
full into every interaction prompt. That grows without bound and the model has
to pick the right target out of the whole list, which degrades as it grows.

This version stores a record per agent (description, associated email addresses
and Gmail thread ids, activity timestamps, status) and hands the prompt only the
agents plausibly relevant to the turn. Agents are archived rather than deleted,
and a strong identifier match - the same email address or thread id - brings an
archived agent back into consideration.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from ...logging_config import logger

SCHEMA_VERSION = 2

#: Agents idle longer than this are archived. They stay in the file and remain
#: reachable by exact name or by a strong identifier match.
ARCHIVE_AFTER_DAYS = 30

#: Default number of agents rendered into a prompt.
DEFAULT_PROMPT_LIMIT = 15

#: Per-agent caps so a long-lived agent cannot grow without bound.
MAX_EMAILS = 12
MAX_THREAD_IDS = 20

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_THREAD_RE = re.compile(r"\b(?:thread[-_][\w-]+|[0-9a-f]{16,})\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    """
    the a an and or but for to of in on at by with from about into over after
    is are was were be been being do does did done have has had will would can
    could should may might must shall re fwd
    it its this that these those there here what when where which who whom whose
    how why any some all no not yet still just also too very more most
    i me my mine you your yours he him his she her hers they them their ours we us
    get got give gave go goes went send sent ask asked tell told need needs
    update updates any word back hear heard reply replied confirm confirmed
    """.split()
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _tokenize(text: str) -> Set[str]:
    return {
        word
        for word in _WORD_RE.findall((text or "").lower())
        if len(word) >= 3 and word not in _STOPWORDS
    }


def extract_identifiers(text: str) -> Set[str]:
    """Email addresses and Gmail thread ids mentioned in free text."""
    lowered = (text or "").lower()
    found = set(_EMAIL_RE.findall(lowered))
    found.update(match.lower() for match in _THREAD_RE.findall(lowered))
    return found


@dataclass
class AgentRecord:
    """What the roster knows about one execution agent."""

    name: str
    description: str = ""
    emails: List[str] = field(default_factory=list)
    thread_ids: List[str] = field(default_factory=list)
    status: str = "active"
    created_at: str = ""
    last_active_at: str = ""

    @property
    def archived(self) -> bool:
        return self.status == "archived"

    def identifiers(self) -> Set[str]:
        return {value.lower() for value in (*self.emails, *self.thread_ids) if value}

    def search_tokens(self) -> Set[str]:
        return _tokenize(f"{self.name} {self.description} {' '.join(self.emails)}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "emails": self.emails,
            "thread_ids": self.thread_ids,
            "status": self.status,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["AgentRecord"]:
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        stamp = str(data.get("created_at") or "") or _iso(_now())
        return cls(
            name=name,
            description=str(data.get("description") or ""),
            emails=[str(v) for v in (data.get("emails") or []) if str(v).strip()],
            thread_ids=[str(v) for v in (data.get("thread_ids") or []) if str(v).strip()],
            status=str(data.get("status") or "active"),
            created_at=stamp,
            last_active_at=str(data.get("last_active_at") or "") or stamp,
        )


class AgentRoster:
    """Roster of execution agents, persisted as JSON."""

    def __init__(self, roster_path: Path):
        self._roster_path = roster_path
        self._agents: List[AgentRecord] = []
        self._lock = threading.RLock()
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Read roster.json, migrating the old list-of-names format in place."""
        with self._lock:
            if not self._roster_path.exists():
                self._agents = []
                self.save()
                return
            try:
                raw = json.loads(self._roster_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"Failed to load roster.json: {exc}")
                self._agents = []
                return
            self._agents = self._deserialize(raw)

    def _deserialize(self, raw: Any) -> List[AgentRecord]:
        stamp = _iso(_now())
        if isinstance(raw, list):
            # v1: a bare list of names. Migrate with empty metadata.
            records = []
            for name in raw:
                text = str(name).strip()
                if text:
                    records.append(
                        AgentRecord(name=text, created_at=stamp, last_active_at=stamp)
                    )
            if records:
                logger.info(f"Migrated {len(records)} agents from roster v1 to v{SCHEMA_VERSION}")
            return records

        if isinstance(raw, dict):
            entries = raw.get("agents")
            if isinstance(entries, list):
                records = []
                for entry in entries:
                    if isinstance(entry, dict):
                        record = AgentRecord.from_dict(entry)
                        if record is not None:
                            records.append(record)
                    elif str(entry).strip():
                        records.append(
                            AgentRecord(
                                name=str(entry).strip(),
                                created_at=stamp,
                                last_active_at=stamp,
                            )
                        )
                return records

        logger.warning("roster.json has an unrecognised shape; starting empty")
        return []

    def save(self) -> None:
        """Write atomically. A torn roster file loses every agent's context."""
        with self._lock:
            payload = {
                "version": SCHEMA_VERSION,
                "agents": [record.to_dict() for record in self._agents],
            }
            try:
                self._roster_path.parent.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=str(self._roster_path.parent),
                    prefix=".roster-",
                    suffix=".tmp",
                    delete=False,
                )
                try:
                    with handle:
                        json.dump(payload, handle, indent=2)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(handle.name, self._roster_path)
                except Exception:
                    try:
                        os.unlink(handle.name)
                    except OSError:
                        pass
                    raise
            except Exception as exc:
                logger.warning(f"Failed to save roster.json: {exc}")

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def add_agent(self, agent_name: str, description: str = "") -> AgentRecord:
        """Create the agent, or reactivate and enrich it when it already exists."""
        with self._lock:
            existing = self._find(agent_name)
            stamp = _iso(_now())
            if existing is not None:
                existing.status = "active"
                existing.last_active_at = stamp
                if description and not existing.description:
                    existing.description = description
                self.save()
                return existing

            record = AgentRecord(
                name=agent_name,
                description=description,
                created_at=stamp,
                last_active_at=stamp,
            )
            self._agents.append(record)
            self.save()
            return record

    def touch(
        self,
        agent_name: str,
        *,
        emails: Optional[Iterable[str]] = None,
        thread_ids: Optional[Iterable[str]] = None,
        description: Optional[str] = None,
    ) -> Optional[AgentRecord]:
        """Record activity and merge in any identifiers the agent worked with."""
        with self._lock:
            record = self._find(agent_name)
            if record is None:
                return None
            record.last_active_at = _iso(_now())
            record.status = "active"
            if description and not record.description:
                record.description = description
            if emails:
                record.emails = self._merge(record.emails, emails, MAX_EMAILS)
            if thread_ids:
                record.thread_ids = self._merge(record.thread_ids, thread_ids, MAX_THREAD_IDS)
            self.save()
            return record

    @staticmethod
    def _merge(current: List[str], incoming: Iterable[str], cap: int) -> List[str]:
        seen = {value.lower() for value in current}
        merged = list(current)
        for value in incoming:
            text = str(value).strip()
            if text and text.lower() not in seen:
                merged.append(text)
                seen.add(text.lower())
        return merged[-cap:]

    def archive_stale(self, *, older_than_days: int = ARCHIVE_AFTER_DAYS) -> int:
        """Archive idle agents. Nothing is deleted."""
        cutoff = _now() - timedelta(days=older_than_days)
        changed = 0
        with self._lock:
            for record in self._agents:
                if record.archived:
                    continue
                last = _parse_iso(record.last_active_at)
                if last is not None and last < cutoff:
                    record.status = "archived"
                    changed += 1
            if changed:
                self.save()
        return changed

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def get_agents(self) -> List[str]:
        """Every active agent name. Kept for callers that want the full list."""
        with self._lock:
            return [record.name for record in self._agents if not record.archived]

    def get_records(self) -> List[AgentRecord]:
        with self._lock:
            return list(self._agents)

    def counts(self) -> Dict[str, int]:
        with self._lock:
            archived = sum(1 for record in self._agents if record.archived)
            return {
                "total": len(self._agents),
                "active": len(self._agents) - archived,
                "archived": archived,
            }

    def get_agents_for_prompt(
        self,
        context_text: str = "",
        *,
        limit: int = DEFAULT_PROMPT_LIMIT,
    ) -> List[AgentRecord]:
        """Agents worth showing the model for this turn, best match first.

        context_text should include the new message AND the last few
        conversation messages: a follow-up like "did she reply?" carries no
        overlap with any agent name and is only resolvable from what came
        before it.
        """
        with self._lock:
            records = list(self._agents)

        tokens = _tokenize(context_text)
        identifiers = extract_identifiers(context_text)

        strong: List[AgentRecord] = []
        scored: List[tuple] = []

        for record in records:
            if identifiers & record.identifiers():
                # Same email address or thread id. Strong enough to pull an
                # archived agent back into the running.
                strong.append(record)
                continue
            if record.archived:
                continue
            overlap = tokens & record.search_tokens()
            if overlap:
                scored.append((len(overlap), record.last_active_at, record))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        selected: List[AgentRecord] = list(strong)
        seen = {record.name for record in selected}

        for _, _, record in scored:
            if len(selected) >= limit:
                break
            if record.name not in seen:
                selected.append(record)
                seen.add(record.name)

        if len(selected) < limit:
            recent = sorted(
                (r for r in records if not r.archived and r.name not in seen),
                key=lambda r: r.last_active_at,
                reverse=True,
            )
            for record in recent:
                if len(selected) >= limit:
                    break
                selected.append(record)
                seen.add(record.name)

        return selected

    def clear(self) -> None:
        with self._lock:
            self._agents = []
            try:
                if self._roster_path.exists():
                    self._roster_path.unlink()
                logger.info("Cleared agent roster")
            except Exception as exc:
                logger.warning(f"Failed to clear roster.json: {exc}")

    def _find(self, agent_name: str) -> Optional[AgentRecord]:
        target = (agent_name or "").strip().lower()
        for record in self._agents:
            if record.name.lower() == target:
                return record
        return None


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_ROSTER_PATH = _DATA_DIR / "execution_agents" / "roster.json"

_agent_roster = AgentRoster(_ROSTER_PATH)


def get_agent_roster() -> AgentRoster:
    """Get the singleton roster instance."""
    return _agent_roster


__all__ = [
    "AgentRecord",
    "AgentRoster",
    "get_agent_roster",
    "extract_identifiers",
    "SCHEMA_VERSION",
]
