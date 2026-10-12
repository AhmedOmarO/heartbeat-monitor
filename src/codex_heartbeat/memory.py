from __future__ import annotations

import os
from pathlib import Path

from .parser import parse_session
from .claude_parser import parse_claude_session


MEMORY_NAME = "MEMORY.md"
MEMORY_TEMPLATE = """# Project memory

## Current focus

## Decisions

## Useful commands

## Gotchas

## Next steps
"""


def project_root(session_path: Path | None, provider: str = "codex") -> Path:
    if session_path is not None:
        parser = parse_claude_session if provider == "claude" else parse_session
        cwd = parser(session_path, max_lines=40).cwd
        if cwd:
            path = Path(cwd).expanduser().resolve()
            if path.is_dir():
                return path
    return Path.cwd().resolve()


def markdown_files(root: Path) -> list[str]:
    names: list[str] = []
    excluded = {".git", ".venv", ".codex", ".agents", "node_modules", "__pycache__"}
    for directory, children, files in os.walk(root):
        relative = Path(directory).relative_to(root)
        children[:] = sorted(
            child for child in children
            if child not in excluded and not child.startswith(".") and len(relative.parts) < 3
        )
        for name in sorted(files):
            if not name.lower().endswith(".md"):
                continue
            path = Path(directory) / name
            if path.resolve().is_relative_to(root):
                names.append(path.relative_to(root).as_posix())
        if len(names) >= 100:
            break
    names = sorted(set(names))[:100]
    if MEMORY_NAME in names:
        names.remove(MEMORY_NAME)
    return [MEMORY_NAME, *names]
