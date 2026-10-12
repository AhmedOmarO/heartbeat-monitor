from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import AgentState, Event, Session, TokenUsage


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _recursive_dicts(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _recursive_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _recursive_dicts(v)


def _usage_from_dict(d: dict[str, Any]) -> TokenUsage | None:
    candidates = {
        "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
        "cached_tokens", "reasoning_output_tokens", "reasoning_tokens",
    }
    if not candidates.intersection(d):
        return None
    return TokenUsage(
        input_tokens=_first(d, "input_tokens", "input"),
        output_tokens=_first(d, "output_tokens", "output"),
        cached_tokens=_first(d, "cached_input_tokens", "cached_tokens", "cached"),
        reasoning_tokens=_first(d, "reasoning_output_tokens", "reasoning_tokens", "reasoning"),
        total_tokens=_first(d, "total_tokens", "total"),
    )


def extract_usage(raw: dict[str, Any]) -> tuple[TokenUsage | None, str | None]:
    for d in _recursive_dicts(raw):
        for key in ("last_token_usage", "last_usage", "turn_usage"):
            v = d.get(key)
            if isinstance(v, dict):
                u = _usage_from_dict(v)
                if u:
                    return u, "last"
    for d in _recursive_dicts(raw):
        for key in ("total_token_usage", "total_usage", "cumulative_usage"):
            v = d.get(key)
            if isinstance(v, dict):
                u = _usage_from_dict(v)
                if u:
                    return u, "cumulative"
    for d in _recursive_dicts(raw):
        u = _usage_from_dict(d)
        if u:
            return u, None
    return None, None


def extract_cumulative_usage(raw: dict[str, Any]) -> TokenUsage | None:
    """Read the session total even when the same event also has last-turn usage."""
    for d in _recursive_dicts(raw):
        for key in ("total_token_usage", "total_usage", "cumulative_usage"):
            value = d.get(key)
            if isinstance(value, dict):
                usage = _usage_from_dict(value)
                if usage:
                    return usage
    return None


def _message_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content") or payload.get("output")
    if isinstance(content, str):
        return content[:500]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)[:500]
    return None


def classify(raw: dict[str, Any]) -> tuple[str, str | None, AgentState]:
    outer = str(raw.get("type") or "unknown")
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    inner = str(payload.get("type") or "") or None
    role = payload.get("role")

    if inner == "reasoning":
        return outer, inner, AgentState.REASONING
    if inner in {"custom_tool_call", "function_call", "tool_call"}:
        return outer, inner, AgentState.TOOL_CALL
    if inner in {"custom_tool_call_output", "function_call_output", "tool_output"}:
        return outer, inner, AgentState.TOOL_OUTPUT
    if inner == "message" and role == "assistant":
        return outer, "message.assistant", AgentState.OUTPUT
    if inner == "message" and role == "user":
        return outer, "message.user", AgentState.SYSTEM
    if inner == "message" and role == "developer":
        return outer, "message.developer", AgentState.SYSTEM
    if outer == "event_msg":
        if inner in {"task_complete", "task_completed"}:
            return outer, inner, AgentState.COMPLETE
        if inner in {"turn_aborted", "error", "task_failed"}:
            return outer, inner, AgentState.ERROR
        return outer, inner, AgentState.SYSTEM
    if outer in {"token_usage_record", "world_state", "turn_context", "session_meta", "compacted"}:
        return outer, inner, AgentState.SYSTEM
    return outer, inner, AgentState.SYSTEM


def parse_line(line: str, line_no: int) -> Event | None:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    outer, subtype, state = classify(raw)
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict):
        metadata = {}
    turn_id = _first(payload, "turn_id") or metadata.get("turn_id")
    call_id = _first(payload, "call_id")
    tool_name = _first(payload, "name", "tool_name")
    exit_code = payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None
    usage, token_kind = extract_usage(raw)
    return Event(
        ordinal=int(raw.get("ordinal") if isinstance(raw.get("ordinal"), int) else line_no),
        timestamp=parse_ts(raw.get("timestamp")),
        raw_type=outer,
        subtype=subtype,
        state=state,
        turn_id=str(turn_id) if turn_id else None,
        call_id=str(call_id) if call_id else None,
        tool_name=str(tool_name) if tool_name else None,
        text=_message_text(payload),
        exit_code=exit_code,
        tokens=usage,
        token_kind=token_kind,
        raw=raw,
    )


def _session_id_from_path(path: Path) -> str:
    stem = path.stem
    parts = stem.split("-")
    if len(parts) >= 5:
        maybe = "-".join(parts[-5:])
        if len(maybe) >= 32:
            return maybe
    return stem


def _discover_meta(events: list[Event]) -> dict[str, Any]:
    for e in events:
        if e.raw_type == "session_meta":
            p = e.raw.get("payload")
            if isinstance(p, dict):
                return p
    return {}


def _parent_and_type(meta: dict[str, Any], events: list[Event]) -> tuple[str | None, str]:
    parent = _first(meta, "parent_session_id", "parent_thread_id", "parent_id")
    source = str(_first(meta, "source", "session_type", "origin") or "").lower()
    blob = json.dumps(meta, default=str).lower()
    if "guardian" in source or "guardian" in blob:
        return str(parent) if parent else None, "guardian"
    if "subagent" in source or "sub_agent" in source or "subagent" in blob:
        return str(parent) if parent else None, "subagent"
    if not parent:
        for e in events[:40]:
            for d in _recursive_dicts(e.raw):
                candidate = _first(d, "parent_session_id", "parent_thread_id", "parent_id")
                if candidate:
                    parent = candidate
                    break
            if parent:
                break
    return str(parent) if parent else None, "main" if not parent else "child"


def parse_session(path: Path, max_lines: int | None = None) -> Session:
    events: list[Event] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if max_lines is not None and i >= max_lines:
                    break
                if not line.strip():
                    continue
                ev = parse_line(line, i)
                if ev:
                    events.append(ev)
    except FileNotFoundError:
        pass
    meta = _discover_meta(events)
    session_id = str(_first(meta, "id", "session_id", "thread_id") or _session_id_from_path(path))
    parent, session_type = _parent_and_type(meta, events)
    return Session(
        session_id=session_id,
        path=path,
        parent_session_id=parent,
        session_type=session_type,
        cwd=_first(meta, "cwd"),
        model=_first(meta, "model"),
        source=_first(meta, "source"),
        events=events,
        meta=meta,
    )
