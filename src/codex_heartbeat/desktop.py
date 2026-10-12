from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .discovery import current_session_file, find_session_file, session_files
from .models import AgentState, Event, Session, STATE_AMPLITUDE
from .parser import parse_session


WINDOW_PRESETS = {"1m": 60, "5m": 300, "15m": 900, "All": None}


@dataclass(slots=True)
class TurnUsage:
    turn_id: str
    timestamp: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


def _event_turn_ids(session: Session) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for event in sorted(session.events, key=lambda e: e.ordinal):
        if event.turn_id and event.turn_id not in seen:
            seen.add(event.turn_id)
            ordered.append(event.turn_id)
    return ordered


def _attach_missing_turn_ids(session: Session) -> list[tuple[Event, str | None]]:
    current: str | None = None
    attached: list[tuple[Event, str | None]] = []
    for event in sorted(session.events, key=lambda e: e.ordinal):
        if event.turn_id:
            current = event.turn_id
        attached.append((event, event.turn_id or current))
    return attached


def token_usage_by_turn(session: Session) -> list[TurnUsage]:
    grouped: dict[str, list[Event]] = {}
    order: list[str] = []
    for event, turn_id in _attach_missing_turn_ids(session):
        if not turn_id or not event.tokens or not event.timestamp:
            continue
        if turn_id not in grouped:
            grouped[turn_id] = []
            order.append(turn_id)
        grouped[turn_id].append(event)

    result: list[TurnUsage] = []
    previous_cumulative = {
        "input": 0, "output": 0, "cached": 0, "reasoning": 0, "total": 0
    }

    for turn_id in order:
        events = grouped[turn_id]
        last = [e for e in events if e.token_kind == "last"]
        cumulative = [e for e in events if e.token_kind == "cumulative"]
        generic = [e for e in events if e.token_kind not in {"last", "cumulative"}]

        if last:
            chosen = max(last, key=lambda e: e.timestamp)
            mode = "last"
        elif cumulative:
            chosen = max(cumulative, key=lambda e: e.timestamp)
            mode = "cumulative"
        elif generic:
            chosen = max(generic, key=lambda e: e.timestamp)
            mode = "generic"
        else:
            continue

        if not chosen.tokens:
            continue

        usage = chosen.tokens
        current = {
            "input": int(usage.input_tokens or 0),
            "output": int(usage.output_tokens or 0),
            "cached": int(usage.cached_tokens or 0),
            "reasoning": int(usage.reasoning_tokens or 0),
            "total": int(usage.total()),
        }

        if mode == "cumulative":
            values = {
                key: max(0, current[key] - previous_cumulative[key])
                for key in current
            }
            previous_cumulative = current
        else:
            values = current

        result.append(
            TurnUsage(
                turn_id=turn_id,
                timestamp=chosen.timestamp,
                input_tokens=values["input"],
                output_tokens=values["output"],
                cached_tokens=values["cached"],
                reasoning_tokens=values["reasoning"],
                total_tokens=values["total"],
            )
        )
    return result


def latest_total_tokens(session: Session) -> int:
    cumulative = [
        e for e in session.events
        if e.tokens and e.token_kind == "cumulative" and e.timestamp
    ]
    if cumulative:
        event = max(cumulative, key=lambda e: e.timestamp)
        return event.tokens.total() if event.tokens else 0
    return sum(t.total_tokens for t in token_usage_by_turn(session))


def token_usage_records(session: Session) -> list[tuple[datetime, int, str | None, int]]:
    """Every parsed token-usage observation, in event order.

    This intentionally does not collapse records by turn. If Codex emits token usage
    multiple times during one turn, Heartbeat shows every observation.
    """
    rows: list[tuple[datetime, int, str | None, int]] = []
    for event in sorted(session.events, key=lambda e: e.ordinal):
        if event.tokens and event.timestamp:
            rows.append(
                (
                    event.timestamp,
                    int(event.tokens.total()),
                    event.token_kind,
                    event.ordinal,
                )
            )
    return rows


class MonitorCanvas(QWidget):
    def __init__(self, parent: "HeartbeatWindow") -> None:
        super().__init__(parent)
        self.owner = parent
        self.setMouseTracking(True)
        self.setMinimumHeight(300)

    def mouseMoveEvent(self, event) -> None:
        points = self.owner.hover_points
        if not points:
            QToolTip.hideText()
            return
        pos = event.position()
        best = min(points, key=lambda p: (p[0] - pos.x()) ** 2 + (p[1] - pos.y()) ** 2)
        x, y, ev, session = best
        if (x - pos.x()) ** 2 + (y - pos.y()) ** 2 > 180:
            QToolTip.hideText()
            return
        lines = [
            f"#{ev.ordinal}  {ev.state.value}",
            ev.timestamp.astimezone().strftime("%H:%M:%S.%f")[:-3] if ev.timestamp else "",
            f"agent {session.session_id[:8]}",
        ]
        if ev.tool_name:
            lines.append(f"tool {ev.tool_name}")
        if ev.turn_id:
            lines.append(f"turn {ev.turn_id[:12]}…")
        if ev.call_id:
            lines.append(f"call {ev.call_id[:14]}…")
        if ev.subtype:
            lines.append(str(ev.subtype))
        QToolTip.showText(event.globalPosition().toPoint(), "\n".join(lines), self)

    def leaveEvent(self, event) -> None:
        QToolTip.hideText()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        self.owner.paint_monitor(self)


class HeartbeatWindow(QWidget):
    def __init__(self, session_selector: str = "current") -> None:
        super().__init__()
        self.session_selector = session_selector
        self.window_seconds: int | None = 300
        self.sessions: list[Session] = []
        self.root_path: Path | None = None
        self.hover_points: list[tuple[float, float, Event, Session]] = []

        self.setWindowTitle("Codex Heartbeat")
        self.resize(470, 650)
        self.setMinimumSize(360, 420)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setStyleSheet("""
            QWidget { background: #070b10; color: #cbd5e1; }
            QLabel { background: transparent; }
            QPushButton {
                background: #101820; color: #9aa7b4; border: 1px solid #1f2937;
                border-radius: 5px; padding: 4px 7px;
            }
            QPushButton:hover { background: #1b2732; color: #e5e7eb; }
            QCheckBox { color: #7d8996; }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 8)
        outer.setSpacing(5)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("HEARTBEAT")
        title.setStyleSheet("color:#d9f99d;font-weight:700;font-size:14px;")
        self.session_label = QLabel("waiting for Codex…")
        self.session_label.setStyleSheet("color:#7d8996;font-family:Menlo;font-size:10px;")
        title_box.addWidget(title)
        title_box.addWidget(self.session_label)
        header.addLayout(title_box, 1)

        for label, seconds in WINDOW_PRESETS.items():
            button = QPushButton(label)
            button.setFixedWidth(42)
            button.clicked.connect(lambda _checked=False, s=seconds: self.set_window(s))
            header.addWidget(button)
        outer.addLayout(header)

        self.keep_on_top = QCheckBox("keep on top")
        self.keep_on_top.setChecked(True)
        self.keep_on_top.stateChanged.connect(self.toggle_topmost)
        outer.addWidget(self.keep_on_top, alignment=Qt.AlignmentFlag.AlignRight)

        self.stats = QLabel("")
        self.stats.setStyleSheet("color:#9aa7b4;font-family:Menlo;font-size:10px;")
        outer.addWidget(self.stats)

        legend = QLabel(
            '<span style="color:#60a5fa">● THINKING</span>&nbsp;&nbsp;'
            '<span style="color:#f59e0b">● CALLING</span>&nbsp;&nbsp;'
            '<span style="color:#84cc16">● OUTPUT</span>&nbsp;&nbsp;'
            '<span style="color:#a78bfa">◆ COMPLETE</span>'
        )
        legend.setStyleSheet("font-family:Menlo;font-size:9px;")
        outer.addWidget(legend)

        self.canvas = MonitorCanvas(self)
        self.canvas.setStyleSheet("background:#05090d;border:1px solid #17212b;")
        outer.addWidget(self.canvas, 1)

        self.status = QLabel("starting…")
        self.status.setStyleSheet("color:#64748b;font-family:Menlo;font-size:9px;")
        outer.addWidget(self.status)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_data)
        self.timer.start(800)
        self.refresh_data()

    def toggle_topmost(self) -> None:
        self.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint,
            self.keep_on_top.isChecked(),
        )
        self.show()

    def set_window(self, seconds: int | None) -> None:
        self.window_seconds = seconds
        self.canvas.update()

    def resolve_root(self) -> Path | None:
        if self.session_selector == "current":
            return current_session_file()
        path = Path(self.session_selector).expanduser()
        if path.exists():
            return path
        return find_session_file(self.session_selector)

    def load_family(self, root_path: Path) -> list[Session]:
        root = parse_session(root_path)
        family = [root]
        for path in session_files()[:120]:
            if path == root_path:
                continue
            try:
                child = parse_session(path)
            except Exception:
                continue
            if child.parent_session_id == root.session_id and child.session_type != "guardian":
                family.append(child)
        family[1:] = sorted(
            family[1:],
            key=lambda s: s.started_at or datetime.max.replace(tzinfo=timezone.utc),
        )
        return family

    def refresh_data(self) -> None:
        try:
            root_path = self.resolve_root()
            if not root_path:
                self.status.setText("No Codex rollout session found yet.")
                return
            self.root_path = root_path
            self.sessions = self.load_family(root_path)
            root = self.sessions[0]
            turns = _event_turn_ids(root)
            tokens = latest_total_tokens(root)
            self.session_label.setText(f"{root.session_id[:12]}… · {root.path.name}")
            self.stats.setText(
                f"{len(turns)} turns   {len(root.events)} events   "
                f"{len(self.sessions)} agent{'s' if len(self.sessions) != 1 else ''}   "
                f"{tokens:,} tokens"
            )
            window_text = (
                "last 5 min" if self.window_seconds == 300
                else "all" if self.window_seconds is None
                else f"last {self.window_seconds // 60} min"
            )
            self.status.setText(f"live · {window_text}")
            self.canvas.update()
        except Exception as exc:
            self.status.setText(f"Heartbeat error: {exc}")

    def time_bounds(self) -> tuple[datetime, datetime] | None:
        timestamps = [
            e.timestamp for session in self.sessions for e in session.events if e.timestamp
        ]
        if not timestamps:
            return None
        latest_event = max(timestamps)
        now = datetime.now(latest_event.tzinfo or timezone.utc)
        end = max(now, latest_event)
        start = min(timestamps) if self.window_seconds is None else end - timedelta(seconds=self.window_seconds)
        if start >= end:
            start = end - timedelta(seconds=1)
        return start, end

    @staticmethod
    def map_x(ts: datetime, start: datetime, end: datetime, left: float, right: float) -> float:
        span = max((end - start).total_seconds(), 0.001)
        ratio = (ts - start).total_seconds() / span
        ratio = max(0.0, min(1.0, ratio))
        return left + ratio * (right - left)

    def paint_monitor(self, widget: QWidget) -> None:
        painter = QPainter(widget)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(widget.rect(), QColor("#05090d"))
        self.hover_points.clear()

        width = widget.width()
        height = widget.height()
        bounds = self.time_bounds()
        if not self.sessions or not bounds:
            painter.setPen(QColor("#64748b"))
            painter.drawText(widget.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for session events…")
            return

        start, end = bounds
        left, right = 92.0, float(width - 12)
        top = 25.0
        token_height = 92.0
        agent_area = max(110.0, height - top - token_height - 40.0)
        row_h = max(62.0, agent_area / max(1, len(self.sessions)))

        small = QFont("Menlo", 8)
        painter.setFont(small)
        grid_pen = QPen(QColor("#0f1a23"), 1)
        for i in range(6):
            x = left + (right - left) * i / 5
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(x, top), QPointF(x, height - 26))
            moment = start + (end - start) * (i / 5)
            ago = int((end - moment).total_seconds())
            label = "now" if ago < 2 else f"-{ago // 60}:{ago % 60:02d}"
            painter.setPen(QColor("#43515e"))
            painter.drawText(QRectF(x - 22, 2, 44, 16), Qt.AlignmentFlag.AlignCenter, label)

        colors = {
            AgentState.REASONING: QColor("#60a5fa"),
            AgentState.TOOL_CALL: QColor("#f59e0b"),
            AgentState.TOOL_OUTPUT: QColor("#fbbf24"),
            AgentState.OUTPUT: QColor("#84cc16"),
            AgentState.COMPLETE: QColor("#a78bfa"),
            AgentState.ERROR: QColor("#ef4444"),
            AgentState.SYSTEM: QColor("#475569"),
            AgentState.IDLE: QColor("#334155"),
        }

        for idx, session in enumerate(self.sessions):
            baseline = top + row_h * idx + row_h / 2
            label = "MAIN" if idx == 0 else session.session_type.upper()

            # Horizontal semantic zones across the whole agent lane.
            # These are intentionally subtle so the heartbeat remains the primary signal.
            lane_top = top + row_h * idx
            lane_bottom = lane_top + row_h
            output_bottom = baseline - row_h * 0.12
            tool_top = output_bottom
            tool_bottom = baseline + row_h * 0.12
            think_top = tool_bottom

            output_bg = QColor("#84cc16")
            output_bg.setAlpha(42)
            tool_bg = QColor("#f59e0b")
            tool_bg.setAlpha(42)
            think_bg = QColor("#60a5fa")
            think_bg.setAlpha(42)

            painter.fillRect(QRectF(left, lane_top, right - left, output_bottom - lane_top), output_bg)
            painter.fillRect(QRectF(left, tool_top, right - left, tool_bottom - tool_top), tool_bg)
            painter.fillRect(QRectF(left, think_top, right - left, lane_bottom - think_top), think_bg)

            # Zone labels on the right side of each lane.
            zone_font = QFont("Menlo", 7)
            zone_font.setBold(True)
            painter.setFont(zone_font)

            painter.setPen(QColor("#6f8f35"))
            painter.drawText(
                QRectF(right - 64, lane_top + 3, 58, max(12.0, output_bottom - lane_top - 4)),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                "OUTPUT",
            )
            painter.setPen(QColor("#9a6a13"))
            painter.drawText(
                QRectF(right - 64, tool_top, 58, max(12.0, tool_bottom - tool_top)),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                "TOOLS",
            )
            painter.setPen(QColor("#4a78a8"))
            painter.drawText(
                QRectF(right - 64, think_top + 2, 58, max(12.0, lane_bottom - think_top - 4)),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                "THINKING",
            )

            painter.setFont(small)
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRectF(7, baseline - 19, 78, 15), Qt.AlignmentFlag.AlignLeft, label)
            painter.setPen(QColor("#475569"))
            painter.drawText(QRectF(7, baseline + 2, 78, 15), Qt.AlignmentFlag.AlignLeft, session.session_id[:8])
            painter.setPen(QPen(QColor("#1f2937"), 1))
            painter.drawLine(QPointF(left, baseline), QPointF(right, baseline))

            events = [
                e for e in session.events
                if e.timestamp and start <= e.timestamp <= end
            ]
            if not events:
                painter.setPen(QColor("#334155"))
                painter.drawText(
                    QRectF(left + 8, baseline - 12, max(120.0, right - left - 16), 24),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    "no activity in selected window",
                )
                continue

            amplitude = min(26.0, row_h * 0.32)
            pulse_half_width = max(2.0, min(7.0, (right - left) / 180.0))

            # Draw each meaningful event as a short pulse that returns to baseline.
            # This avoids long diagonal ramps across idle time and makes the filled
            # area itself carry the state color.
            meaningful = {
                AgentState.REASONING,
                AgentState.TOOL_CALL,
                AgentState.TOOL_OUTPUT,
                AgentState.OUTPUT,
                AgentState.COMPLETE,
                AgentState.ERROR,
            }

            for event in events:
                if event.state not in meaningful:
                    continue

                x = self.map_x(event.timestamp, start, end, left, right)
                y = baseline - STATE_AMPLITUDE[event.state] * amplitude
                x0 = max(left, x - pulse_half_width)
                x1 = min(right, x + pulse_half_width)
                self.hover_points.append((x, y, event, session))

                pulse = QPainterPath()
                pulse.moveTo(x0, baseline)
                pulse.lineTo(x, y)
                pulse.lineTo(x1, baseline)
                pulse.closeSubpath()

                fill = QColor(colors[event.state])
                fill.setAlpha(150 if event.state != AgentState.COMPLETE else 190)
                painter.setPen(QPen(colors[event.state], 1.6))
                painter.setBrush(fill)
                painter.drawPath(pulse)

                if event.state == AgentState.COMPLETE:
                    # Completion gets a small cap so it remains distinct even when
                    # several pulses overlap.
                    painter.setPen(QPen(colors[event.state], 2.2))
                    painter.drawLine(
                        QPointF(x - 4, y),
                        QPointF(x + 4, y),
                    )

            # Extend the idle baseline through "now" so inactivity remains visible.
            painter.setPen(QPen(QColor("#1f2937"), 1))
            painter.drawLine(QPointF(left, baseline), QPointF(right, baseline))

        token_top = top + row_h * len(self.sessions) + 14
        painter.setPen(QPen(QColor("#17212b"), 1))
        painter.drawLine(QPointF(0, token_top - 10), QPointF(width, token_top - 10))
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(7, token_top - 2, 72, 16), Qt.AlignmentFlag.AlignLeft, "TOKENS")

        root = self.sessions[0]
        token_records = [
            row for row in token_usage_records(root)
            if start <= row[0] <= end
        ]
        max_tokens = max((row[1] for row in token_records), default=1)
        graph_top = token_top + 8
        graph_bottom = height - 28
        bar_height = max(18.0, graph_bottom - graph_top)

        painter.setBrush(QColor("#22d3ee"))
        painter.setPen(Qt.PenStyle.NoPen)
        for timestamp, total, _kind, _ordinal in token_records:
            x = self.map_x(timestamp, start, end, left, right)
            h = max(2.0, (total / max_tokens) * bar_height)
            painter.drawRect(QRectF(x - 1.5, graph_bottom - h, 3.0, h))

        painter.setPen(QPen(QColor("#1f2937"), 1))
        painter.drawLine(QPointF(left, graph_bottom), QPointF(right, graph_bottom))

        if token_records:
            latest = token_records[-1]
            painter.setPen(QColor("#22d3ee"))
            painter.drawText(
                QRectF(7, token_top + 14, 75, 34),
                Qt.AlignmentFlag.AlignLeft,
                f"latest\n{latest[1]:,}",
            )


def run(session: str = "current") -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    window = HeartbeatWindow(session)
    window.show()
    raise SystemExit(app.exec())
