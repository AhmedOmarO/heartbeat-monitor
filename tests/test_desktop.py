import json
from datetime import datetime, timezone
from pathlib import Path

from codex_heartbeat.desktop import _event_turn_ids, token_usage_by_turn
from codex_heartbeat.models import AgentState, Event, Session, TokenUsage


def _event(ordinal, turn_id=None, tokens=None, kind=None):
    return Event(
        ordinal=ordinal,
        timestamp=datetime(2026, 10, 6, 20, 0, ordinal, tzinfo=timezone.utc),
        raw_type="response_item",
        subtype=None,
        state=AgentState.SYSTEM,
        turn_id=turn_id,
        tokens=tokens,
        token_kind=kind,
    )


def test_turn_count_uses_unique_explicit_turn_ids(tmp_path: Path):
    s = Session(
        session_id="s",
        path=tmp_path / "x.jsonl",
        events=[
            _event(1, "turn-a"),
            _event(2, "turn-a"),
            _event(3, None),
            _event(4, "turn-b"),
            _event(5, "turn-b"),
        ],
    )
    assert _event_turn_ids(s) == ["turn-a", "turn-b"]


def test_token_record_without_turn_id_attaches_to_current_turn(tmp_path: Path):
    usage = TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120)
    s = Session(
        session_id="s",
        path=tmp_path / "x.jsonl",
        events=[
            _event(1, "turn-a"),
            _event(2, None, usage, "last"),
            _event(3, "turn-b"),
        ],
    )
    turns = token_usage_by_turn(s)
    assert len(turns) == 1
    assert turns[0].turn_id == "turn-a"
    assert turns[0].total_tokens == 120
