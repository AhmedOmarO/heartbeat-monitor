import json
from pathlib import Path

from codex_heartbeat.models import AgentState, TokenUsage
from codex_heartbeat.parser import parse_line, parse_session


def test_reasoning_classification():
    line = json.dumps(
        {
            "ordinal": 1,
            "timestamp": "2026-10-06T19:40:19.589Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "encrypted_content": "abc"},
        }
    )
    e = parse_line(line, 0)
    assert e.state == AgentState.REASONING


def test_partial_json_is_ignored():
    assert parse_line('{"ordinal": 1', 0) is None


def test_tool_output_call_id():
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "call_123"},
        }
    )
    e = parse_line(line, 0)
    assert e.state == AgentState.TOOL_OUTPUT
    assert e.call_id == "call_123"


def test_parent_session_detection(tmp_path: Path):
    p = tmp_path / "rollout-test.jsonl"
    p.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "parent_session_id": "parent",
                    "source": "guardian",
                },
            }
        )
        + "\n"
    )
    s = parse_session(p)
    assert s.session_id == "child"
    assert s.parent_session_id == "parent"
    assert s.session_type == "guardian"


def test_task_complete_classification():
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        }
    )
    e = parse_line(line, 0)
    assert e.state == AgentState.COMPLETE


def test_token_total_does_not_double_count_subsets():
    usage = TokenUsage(input_tokens=100, output_tokens=40, cached_tokens=25, reasoning_tokens=15)
    assert usage.total() == 140
    assert TokenUsage(input_tokens=100, reasoning_tokens=15).total() == 115
    assert TokenUsage(input_tokens=100, output_tokens=40, total_tokens=145).total() == 145
