from __future__ import annotations

import os
from pathlib import Path

from .parser import parse_session


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def session_files(home: Path | None = None) -> list[Path]:
    home = home or codex_home()
    roots = [home / "sessions", home / "archived_sessions"]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(root.rglob("rollout-*.jsonl"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def current_session_file(home: Path | None = None) -> Path | None:
    files = session_files(home)
    for path in files:
        session = parse_session(path, max_lines=40)
        if session.session_type == "guardian":
            continue
        # A child can be the most recently written rollout during fanout.
        # Follow its parent chain so "current" keeps the whole agent family.
        seen = {session.session_id}
        while session.parent_session_id and session.parent_session_id not in seen:
            parent_id = session.parent_session_id
            parent_path = next((p for p in files if parent_id.lower() in p.name.lower()), None)
            if parent_path is None:
                break
            parent = parse_session(parent_path, max_lines=40)
            if parent.session_type == "guardian":
                break
            seen.add(parent_id)
            path, session = parent_path, parent
        return path
    return None


def find_session_file(session_id: str, home: Path | None = None) -> Path | None:
    needle = session_id.lower()
    for p in session_files(home):
        if needle in p.name.lower():
            return p
    return None


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()


def claude_session_files(home: Path | None = None, *, include_subagents: bool = False) -> list[Path]:
    projects = (home or claude_home()) / "projects"
    if not projects.exists():
        return []
    files = projects.glob("*/*.jsonl")
    if include_subagents:
        files = list(files) + list(projects.glob("*/**/subagents/**/*.jsonl"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def find_claude_session_file(session_id: str) -> Path | None:
    needle = session_id.lower()
    return next((p for p in claude_session_files(include_subagents=True) if needle in p.name.lower()), None)


def current_claude_session_file() -> Path | None:
    roots = claude_session_files()
    if not roots:
        return None
    # A busy subagent may update while its parent file is unchanged.
    def activity_time(path: Path) -> float:
        children = path.with_suffix("") / "subagents"
        times = [path.stat().st_mtime]
        if children.exists():
            times.extend(child.stat().st_mtime for child in children.rglob("*.jsonl"))
        return max(times)
    return max(roots, key=activity_time)
