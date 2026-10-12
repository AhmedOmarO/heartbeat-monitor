"""Regression checks for Flask session-family discovery."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_heartbeat.flask_app import _family


def _rollout(directory: Path, session_id: str, parent: str | None = None, source: str = "cli") -> Path:
    path = directory / f"rollout-{session_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "parent_session_id": parent, "source": source},
            }
        )
        + "\n"
    )
    return path


class FamilyDiscoveryTest(unittest.TestCase):
    def test_siblings_nested_children_and_guardian(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            root = _rollout(folder, "root")
            sibling_a = _rollout(folder, "sibling-a", "root", "subagent")
            sibling_b = _rollout(folder, "sibling-b", "root", "subagent")
            nested = _rollout(folder, "nested", "sibling-a", "subagent")
            guardian = _rollout(folder, "guardian", "root", "guardian")
            # Discovery order need not match parent-child order.
            with patch("codex_heartbeat.flask_app.session_files", return_value=[nested, guardian, sibling_b, root, sibling_a]):
                family = _family(root)
            self.assertEqual(family[0].session_id, "root")
            self.assertEqual({session.session_id for session in family}, {"root", "sibling-a", "sibling-b", "nested"})

    def test_child_after_newer_unrelated_rollouts(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            root = _rollout(folder, "root")
            recent = _rollout(folder, "recent", "root", "subagent")
            older = _rollout(folder, "older", "root", "subagent")
            unrelated = [_rollout(folder, f"unrelated-{index}") for index in range(119)]
            # The older sibling falls outside the previous 120-file scan.
            with patch("codex_heartbeat.flask_app.session_files", return_value=[recent, *unrelated, older, root]):
                family = _family(root)
            self.assertEqual({session.session_id for session in family}, {"root", "recent", "older"})


if __name__ == "__main__":
    unittest.main()
