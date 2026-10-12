from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .claude_parser import parse_claude_session
from .discovery import claude_session_files, session_files
from .models import Session
from .parser import parse_session


def _short(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    return text[:100].rstrip() + ("…" if len(text) > 100 else "") if text else None


def _content_text(content: object) -> str | None:
    if isinstance(content, str):
        return _short(content)
    if isinstance(content, list):
        parts = [block.get("text") for block in content
                 if isinstance(block, dict) and block.get("type") in {"text", "input_text"}
                 and isinstance(block.get("text"), str)]
        return _short(" ".join(parts)) if parts else None
    return None


@lru_cache(maxsize=2048)
def _name_from_file(path: Path, mtime_ns: int, provider: str) -> str | None:
    explicit = ai_title = first_prompt = fallback = slug = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_no, line in enumerate(stream):
                if provider == "codex" and line_no > 200:
                    break
                # Claude's /rename record can occur anywhere in a transcript.
                if provider == "claude" and first_prompt and not any(
                    marker in line for marker in ('"custom-title"', '"agent-name"', '"ai-title"')
                ):
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                if provider == "claude":
                    kind = raw.get("type")
                    if kind in {"custom-title", "agent-name"}:
                        explicit = _short(raw.get("customTitle") or raw.get("agentName")) or explicit
                    elif kind == "ai-title":
                        ai_title = _short(raw.get("aiTitle")) or ai_title
                    elif kind == "user" and not raw.get("isMeta") and first_prompt is None:
                        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
                        content = message.get("content")
                        if not (isinstance(content, list) and any(
                            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
                        )):
                            first_prompt = _content_text(content)
                    slug = _short(raw.get("slug")) or slug
                else:
                    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
                    if raw.get("type") == "session_meta":
                        explicit = _short(payload.get("title") or payload.get("name")) or explicit
                    if payload.get("type") == "user_message":
                        first_prompt = _short(payload.get("message")) or first_prompt
                        if first_prompt:
                            break
                    elif payload.get("type") == "message" and payload.get("role") == "user" and fallback is None:
                        fallback = _content_text(payload.get("content"))
    except OSError:
        return None
    return explicit or ai_title or first_prompt or slug or fallback


def session_name(session: Session) -> str:
    title = _name_from_file(session.path, session.path.stat().st_mtime_ns, session.provider)
    if title:
        return title
    if session.cwd:
        return Path(session.cwd).name or session.cwd
    return f"Session {session.session_id[:8]}"


def _entry(session: Session, updated: float) -> dict:
    project = Path(session.cwd).name if session.cwd else session.path.parent.name
    return {
        "key": f"{session.provider}:{session.session_id}",
        "name": session_name(session),
        "project": project,
        "provider": session.provider,
        "session_id": session.session_id,
        "updated_at": datetime.fromtimestamp(updated, timezone.utc).isoformat(),
        "updated_epoch": updated,
    }


def list_sessions(provider: str = "auto") -> list[dict]:
    entries: dict[str, dict] = {}
    if provider in {"auto", "codex"}:
        indexed = {}
        for path in session_files():
            session = parse_session(path, max_lines=50)
            if session.session_type != "guardian":
                indexed.setdefault(session.session_id, session)
        for session in indexed.values():
            root = session
            seen = {root.session_id}
            while root.parent_session_id in indexed and root.parent_session_id not in seen:
                seen.add(root.parent_session_id)
                root = indexed[root.parent_session_id]
            key = f"codex:{root.session_id}"
            updated = session.path.stat().st_mtime
            if key not in entries or updated > entries[key]["updated_epoch"]:
                entries[key] = _entry(root, updated)
    if provider in {"auto", "claude"}:
        for path in claude_session_files():
            session = parse_claude_session(path, max_lines=50)
            children = path.with_suffix("") / "subagents"
            updated = max([path.stat().st_mtime, *(
                child.stat().st_mtime for child in children.rglob("*.jsonl")
            )]) if children.exists() else path.stat().st_mtime
            entry = _entry(session, updated)
            entries[entry["key"]] = entry
    return sorted(entries.values(), key=lambda entry: (-entry["updated_epoch"], entry["key"]))


def resolve_session_key(key: str, provider: str = "auto") -> tuple[Path, str] | None:
    source, separator, session_id = key.partition(":")
    if not separator or not session_id or source not in {"codex", "claude"}:
        return None
    if provider != "auto" and source != provider:
        return None
    files = session_files() if source == "codex" else claude_session_files()
    parser = parse_session if source == "codex" else parse_claude_session
    for path in files:
        if session_id not in path.name:
            continue
        session = parser(path, max_lines=50)
        if session.session_id == session_id and session.session_type != "guardian":
            return path, source
    return None
