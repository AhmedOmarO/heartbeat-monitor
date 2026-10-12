import json
import os
from pathlib import Path

from codex_heartbeat.discovery import current_session_file


def _rollout(folder: Path, session_id: str, mtime: int, *, parent: str | None = None, source: str = "cli") -> Path:
    path = folder / f"rollout-2026-10-09-{session_id}.jsonl"
    meta = {"id": session_id, "source": source}
    if parent:
        meta["parent_session_id"] = parent
    path.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def test_current_session_follows_newer_descendants_and_ignores_guardian(tmp_path: Path):
    folder = tmp_path / "sessions"
    folder.mkdir()
    root = _rollout(folder, "root-aa", 1)
    _rollout(folder, "child-bb", 2, parent="root-aa", source="subagent")
    assert current_session_file(tmp_path) == root

    _rollout(folder, "nested-cc", 3, parent="child-bb", source="subagent")
    assert current_session_file(tmp_path) == root

    _rollout(folder, "guardian-dd", 4, parent="nested-cc", source="guardian")
    assert current_session_file(tmp_path) == root
