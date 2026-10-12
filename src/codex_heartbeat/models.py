from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class AgentState(str, Enum):
    IDLE = "idle"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_OUTPUT = "tool_output"
    OUTPUT = "output"
    COMPLETE = "complete"
    ERROR = "error"
    SYSTEM = "system"


STATE_AMPLITUDE: dict[AgentState, float] = {
    AgentState.IDLE: 0.0,
    AgentState.REASONING: -1.0,
    AgentState.TOOL_CALL: 0.55,
    AgentState.TOOL_OUTPUT: 0.30,
    AgentState.OUTPUT: 1.0,
    AgentState.COMPLETE: 1.25,
    AgentState.ERROR: 1.45,
    AgentState.SYSTEM: 0.05,
}


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    def total(self) -> int:
        if self.total_tokens is not None:
            return int(self.total_tokens)
        # Cached input and reasoning output are included in their parent counts.
        output = self.output_tokens if self.output_tokens is not None else self.reasoning_tokens
        return int((self.input_tokens or 0) + (output or 0))


@dataclass(slots=True)
class Event:
    ordinal: int
    timestamp: datetime | None
    raw_type: str
    subtype: str | None
    state: AgentState
    turn_id: str | None = None
    call_id: str | None = None
    tool_name: str | None = None
    text: str | None = None
    exit_code: int | None = None
    tokens: TokenUsage | None = None
    token_kind: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Session:
    session_id: str
    path: Path
    parent_session_id: str | None = None
    session_type: str = "main"
    cwd: str | None = None
    model: str | None = None
    source: str | None = None
    events: list[Event] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    provider: str = "codex"

    @property
    def started_at(self) -> datetime | None:
        vals = [e.timestamp for e in self.events if e.timestamp]
        return min(vals) if vals else None

    @property
    def ended_at(self) -> datetime | None:
        vals = [e.timestamp for e in self.events if e.timestamp]
        return max(vals) if vals else None
