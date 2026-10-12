from __future__ import annotations

import json
from pathlib import Path

from .models import AgentState, Event, Session, TokenUsage
from .parser import parse_ts


def _parent_from_path(path: Path) -> str | None:
    for directory in path.parents:
        if directory.name == "subagents":
            return directory.parent.name
    return None


def parse_claude_session(path: Path, max_lines: int | None = None) -> Session:
    events: list[Event] = []
    session_id = path.stem
    parent_id = _parent_from_path(path)
    cwd = model = nickname = None
    turn_id = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_no, line in enumerate(stream):
                if max_lines is not None and line_no >= max_lines:
                    break
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A live writer may leave an incomplete final line.
                if not isinstance(raw, dict):
                    continue
                kind = raw.get("type")
                timestamp = parse_ts(raw.get("timestamp"))
                cwd = cwd or raw.get("cwd")
                parent_id = parent_id or raw.get("parentSessionId")
                agent_id = raw.get("agentId")
                if agent_id:
                    session_id = str(agent_id)
                    nickname = raw.get("slug") or nickname or str(agent_id)
                elif raw.get("sessionId") and not _parent_from_path(path):
                    session_id = str(raw["sessionId"])
                message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
                model = model or message.get("model")
                content = message.get("content")
                blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}] if isinstance(content, str) else [])
                if kind == "user" and not raw.get("isMeta") and not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
                ):
                    turn_id = str(raw.get("uuid") or f"user-{line_no}")
                specs = []
                if kind == "assistant":
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        state = {"thinking": AgentState.REASONING, "redacted_thinking": AgentState.REASONING,
                                 "tool_use": AgentState.TOOL_CALL,
                                 "text": AgentState.OUTPUT}.get(block_type)
                        if state:
                            specs.append((state, block_type, block.get("id"), block.get("name"), block.get("text")))
                    if not specs and isinstance(message.get("usage"), dict):
                        specs.append((AgentState.SYSTEM, "usage", None, None, None))
                elif kind == "user":
                    for block in blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            state = AgentState.ERROR if block.get("is_error") else AgentState.TOOL_OUTPUT
                            specs.append((state, "tool_result", block.get("tool_use_id"), None, None))
                    if not specs and not raw.get("isMeta"):
                        specs.append((AgentState.SYSTEM, "message.user", None, None, None))
                elif kind == "system" and raw.get("subtype") in {"turn_duration", "error"}:
                    state = AgentState.ERROR if raw.get("subtype") == "error" else AgentState.COMPLETE
                    specs.append((state, str(raw.get("subtype")), None, None, None))
                for block_no, (state, subtype, call_id, tool_name, text) in enumerate(specs):
                    events.append(Event(
                        ordinal=line_no * 100 + block_no, timestamp=timestamp,
                        raw_type=str(kind), subtype=subtype, state=state, turn_id=turn_id,
                        call_id=str(call_id) if call_id else None,
                        tool_name=str(tool_name) if tool_name else None,
                        text=text[:500] if isinstance(text, str) else None, raw=raw,
                    ))
    except FileNotFoundError:
        pass
    if parent_id:
        meta_path = path.with_suffix(".meta.json")
        try:
            agent_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(agent_meta, dict):
                nickname = agent_meta.get("agentType") or nickname
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
    return Session(
        session_id=session_id, path=path, provider="claude", parent_session_id=parent_id,
        session_type="subagent" if parent_id else "main", cwd=cwd, model=model,
        source="claude", events=events, meta={"agent_nickname": nickname} if nickname else {},
    )


def cumulative_usage(session: Session) -> list[tuple[Event, TokenUsage]]:
    # Claude reports usage for each assistant message. Stream updates repeat the
    # same message ID, so retain only the latest (usually final) usage snapshot.
    messages: dict[str, tuple[Event, dict]] = {}
    for event in session.events:
        if event.raw_type != "assistant":
            continue
        message = event.raw.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        key = str(message.get("id") or event.raw.get("uuid") or event.ordinal)
        previous = messages.get(key)
        if previous is None or event.ordinal >= previous[0].ordinal:
            messages[key] = (event, message["usage"])
    totals = {"input": 0, "output": 0, "cached": 0}
    rows = []
    for event, usage in sorted(messages.values(), key=lambda item: item[0].ordinal):
        try:
            input_tokens = int(usage.get("input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_create = int(usage.get("cache_creation_input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
        totals["input"] += input_tokens + cache_read + cache_create
        totals["output"] += output_tokens
        totals["cached"] += cache_read
        rows.append((event, TokenUsage(
            input_tokens=totals["input"], output_tokens=totals["output"],
            cached_tokens=totals["cached"],
        )))
    return rows
