from __future__ import annotations

import threading
import webbrowser
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from .catalog import list_sessions, resolve_session_key, session_name
from .discovery import (claude_home, current_claude_session_file, current_session_file,
                        find_claude_session_file, find_session_file, session_files)
from .claude_parser import cumulative_usage as claude_cumulative_usage, parse_claude_session
from .models import STATE_AMPLITUDE
from .memory import MEMORY_NAME, MEMORY_TEMPLATE, markdown_files, project_root
from .parser import extract_cumulative_usage, parse_session


def _provider_for(path: Path) -> str:
    return "claude" if path.suffix == ".jsonl" and (
        claude_home().resolve() in path.expanduser().resolve().parents or
        any(parent.name == "subagents" for parent in path.parents) or
        path.parent.parent.name == "projects"
    ) else "codex"


def _resolve(selector: str, provider: str = "auto") -> Path | None:
    if selector == "current":
        if provider == "codex":
            return current_session_file()
        if provider == "claude":
            return current_claude_session_file()
        candidates = [p for p in (current_session_file(), current_claude_session_file()) if p]
        if not candidates:
            return None
        def activity_time(path: Path) -> float:
            if _provider_for(path) != "claude":
                return path.stat().st_mtime
            children = path.with_suffix("") / "subagents"
            return max([path.stat().st_mtime, *(p.stat().st_mtime for p in children.rglob("*.jsonl"))]) if children.exists() else path.stat().st_mtime
        return max(candidates, key=activity_time)
    path = Path(selector).expanduser()
    if path.exists():
        return path
    if provider == "claude":
        return find_claude_session_file(selector)
    if provider == "codex":
        return find_session_file(selector)
    return find_session_file(selector) or find_claude_session_file(selector)


def _family(root_path: Path, provider: str = "auto"):
    if provider == "claude" or (provider == "auto" and _provider_for(root_path) == "claude"):
        root = parse_claude_session(root_path)
        if root.parent_session_id:
            parent_path = root_path.parent.parent.parent / f"{root.parent_session_id}.jsonl"
            if parent_path.exists():
                root_path = parent_path
                root = parse_claude_session(root_path)
        children_dir = root_path.with_suffix("") / "subagents"
        children = [parse_claude_session(path) for path in children_dir.rglob("*.jsonl")] if children_dir.exists() else []
        children.sort(key=lambda s: (s.started_at.isoformat() if s.started_at else "", s.session_id))
        return [root, *children]
    root = parse_session(root_path)
    family = [root]
    candidates = []
    for path in session_files():
        if path == root_path:
            continue
        try:
            child = parse_session(path, max_lines=40)
        except Exception:
            continue
        if child.session_type != "guardian":
            candidates.append(child)
    known = {root.session_id}
    while candidates:
        children = [child for child in candidates if child.parent_session_id in known]
        if not children:
            break
        family.extend(parse_session(child.path) for child in children)
        known.update(child.session_id for child in children)
        candidates = [child for child in candidates if child.session_id not in known]
    family[1:] = sorted(
        family[1:],
        key=lambda session: (session.started_at.isoformat() if session.started_at else "", session.session_id),
    )
    return family


def _turn_count(session) -> int:
    return len({e.turn_id for e in session.events if e.turn_id})


def _latest_total(session) -> int | None:
    if session.provider == "claude":
        rows = claude_cumulative_usage(session)
        return rows[-1][1].total() if rows else None
    cumulative = [
        (e.timestamp, e.ordinal, usage.total())
        for e in session.events if e.timestamp
        if (usage := extract_cumulative_usage(e.raw)) is not None
    ]
    return max(cumulative, key=lambda row: (row[0], row[1]))[2] if cumulative else None


def _latest_limits(sessions) -> dict | None:
    reports = []
    for session in sessions:
        for event in session.events:
            if not event.timestamp:
                continue
            payload = event.raw.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                reports.append((event.timestamp, event.ordinal, limits))
    if not reports:
        return None

    observed_at, _, limits = max(reports, key=lambda row: (row[0], row[1]))

    def window(name):
        value = limits.get(name)
        if not isinstance(value, dict):
            return None
        return {
            "used_percent": value.get("used_percent"),
            "window_minutes": value.get("window_minutes"),
            "resets_at": value.get("resets_at"),
        }

    return {"observed_at": observed_at.isoformat(), "primary": window("primary"), "secondary": window("secondary")}


def create_app(selector: str, provider: str = "auto") -> Flask:
    app = Flask(__name__)
    memory_token = secrets.token_urlsafe(32)

    def selected_root() -> tuple[Path, str] | None:
        key = request.args.get("session")
        if key:
            return resolve_session_key(key, provider)
        path = _resolve(selector, provider)
        if path is None:
            return None
        return path, provider if provider != "auto" else _provider_for(path)

    @app.get("/")
    def index():
        return render_template("dashboard.html", provider=provider,
                               default_session_label="Follow latest" if selector == "current" else "Launch default")

    @app.get("/memory")
    def memory_page():
        selection = selected_root()
        if selection is None:
            abort(404)
        root = project_root(*selection)
        files = markdown_files(root)
        selected = request.args.get("file", MEMORY_NAME)
        if selected not in files:
            abort(404)
        path = root / selected
        if not path.resolve().is_relative_to(root):
            abort(404)
        content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else MEMORY_TEMPLATE
        return render_template(
            "memory.html",
            root=root,
            files=files,
            selected=selected,
            content=content,
            csrf_token=memory_token,
            saved=request.args.get("saved") == "1",
            session_key=request.args.get("session"),
        )

    @app.post("/memory")
    def save_memory():
        if not hmac.compare_digest(request.form.get("csrf_token", ""), memory_token):
            abort(403)
        content = request.form.get("content", "")
        if len(content) > 200_000:
            abort(413)
        selection = selected_root()
        if selection is None:
            abort(404)
        root = project_root(*selection)
        path = root / MEMORY_NAME
        if not path.resolve().is_relative_to(root):
            abort(403)
        path.write_text(content, encoding="utf-8")
        return redirect(url_for("memory_page", saved=1, session=request.args.get("session")))

    @app.get("/api/sessions")
    def session_catalog():
        return jsonify({"sessions": list_sessions(provider)})

    @app.get("/api/snapshot")
    def snapshot():
        selection = selected_root()
        if selection is None:
            return jsonify({"error": "no session found"}), 404
        root_path, selected_provider = selection
        sessions = _family(root_path, selected_provider)
        root = sessions[0]
        timestamps = [e.timestamp for s in sessions for e in s.events if e.timestamp]
        if not timestamps:
            return jsonify({"error": "no timestamps"}), 404
        latest = max(timestamps)
        end = latest if request.args.get("session") or selector != "current" else max(
            datetime.now(latest.tzinfo or timezone.utc), latest
        )
        q = request.args.get("window", "300")
        start = min(timestamps) if q == "all" else end - timedelta(seconds=max(1, int(q)))

        session_rows = []
        for s in sessions:
            events = [
                {
                    "timestamp": e.timestamp.isoformat(),
                    "state": e.state.value,
                    "amplitude": STATE_AMPLITUDE[e.state],
                    "ordinal": e.ordinal,
                    "turn_id": e.turn_id,
                    "call_id": e.call_id,
                    "tool_name": e.tool_name,
                    "subtype": e.subtype,
                }
                for e in s.events
                if e.timestamp and start <= e.timestamp <= end
            ]
            session_rows.append({
                "id": s.session_id,
                "type": s.session_type,
                "nickname": s.meta.get("agent_nickname"),
                "events": events,
            })

        cumulative_rows = []
        if root.provider == "claude":
            cumulative_rows = [(e, usage) for e, usage in claude_cumulative_usage(root) if e.timestamp and e.timestamp <= end]
        else:
            for e in root.events:
                if not e.timestamp or e.timestamp > end:
                    continue
                usage = extract_cumulative_usage(e.raw)
                if usage is not None:
                    cumulative_rows.append((e, usage))
        cumulative_rows.sort(key=lambda row: (row[0].timestamp, row[0].ordinal))

        def token_row(event, usage, *, anchor=False):
            return {
                "timestamp": (start if anchor else event.timestamp).isoformat(),
                "total": int(usage.total()),
                "kind": "cumulative",
                "ordinal": event.ordinal,
                "input": int(usage.input_tokens or 0),
                "output": int(usage.output_tokens or 0),
                "cached": int(usage.cached_tokens or 0),
                "reasoning": int(usage.reasoning_tokens or 0),
                "anchor": anchor,
            }

        token_records = [token_row(e, usage) for e, usage in cumulative_rows if e.timestamp >= start]
        before_window = next(((e, usage) for e, usage in reversed(cumulative_rows) if e.timestamp < start), None)
        if before_window:
            token_records.insert(0, token_row(*before_window, anchor=True))

        return jsonify({
            "session_id": root.session_id,
            "session_key": f"{root.provider}:{root.session_id}",
            "session_name": session_name(root),
            "project": Path(root.cwd).name if root.cwd else root.path.parent.name,
            "provider": root.provider,
            "file": root.path.name,
            "turns": _turn_count(root),
            "events": len(root.events),
            "total_tokens": _latest_total(root),
            "rate_limits": _latest_limits(sessions),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "latest_activity": latest.isoformat(),
            "sessions": session_rows,
            "token_records": token_records,
        })

    return app


def run(selector: str = "current", port: int = 8765, provider: str = "auto") -> None:
    app = create_app(selector, provider)
    threading.Timer(0.7, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
