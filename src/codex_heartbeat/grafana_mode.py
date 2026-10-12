from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

from prometheus_client import Gauge, start_http_server

from .discovery import current_session_file, find_session_file, session_files
from .models import AgentState, STATE_AMPLITUDE
from .parser import parse_session


AGENT_AMPLITUDE = Gauge(
    "codex_heartbeat_agent_amplitude",
    "Current normalized heartbeat amplitude per agent",
    ["agent_id", "agent_type"],
)
AGENT_STATE = Gauge(
    "codex_heartbeat_agent_state",
    "One-hot current agent state per agent",
    ["agent_id", "agent_type", "state"],
)
TURNS = Gauge("codex_heartbeat_turns", "Distinct explicit Codex turn IDs")
EVENTS = Gauge("codex_heartbeat_events", "Parsed Codex events")
AGENTS = Gauge("codex_heartbeat_agents", "Agent sessions in the current execution tree")
TASK_COMPLETIONS = Gauge("codex_heartbeat_task_completions", "Task completion events")
TOKEN_LATEST = Gauge(
    "codex_heartbeat_token_latest",
    "Latest token usage record",
    ["kind"],
)
TOKEN_RECORDS = Gauge(
    "codex_heartbeat_token_records",
    "Number of token usage observations seen in the session",
)


def _resolve(selector: str) -> Path | None:
    if selector == "current":
        return current_session_file()
    path = Path(selector).expanduser()
    return path if path.exists() else find_session_file(selector)


def _family(root_path: Path):
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
    return family


def _update_metrics(selector: str) -> None:
    root_path = _resolve(selector)
    if not root_path:
        return

    sessions = _family(root_path)
    root = sessions[0]
    TURNS.set(len({e.turn_id for e in root.events if e.turn_id}))
    EVENTS.set(len(root.events))
    AGENTS.set(len(sessions))
    TASK_COMPLETIONS.set(sum(1 for e in root.events if e.state == AgentState.COMPLETE))

    AGENT_AMPLITUDE.clear()
    AGENT_STATE.clear()
    states = [state.value for state in AgentState]
    for session in sessions:
        stamped = [e for e in session.events if e.timestamp]
        if stamped:
            latest = max(stamped, key=lambda e: e.timestamp)
            AGENT_AMPLITUDE.labels(session.session_id[:12], session.session_type).set(
                STATE_AMPLITUDE[latest.state]
            )
            for state in states:
                AGENT_STATE.labels(session.session_id[:12], session.session_type, state).set(
                    1 if latest.state.value == state else 0
                )

    token_events = [e for e in root.events if e.tokens and e.timestamp]
    TOKEN_RECORDS.set(len(token_events))
    if token_events:
        latest = max(token_events, key=lambda e: (e.timestamp, e.ordinal))
        usage = latest.tokens
        TOKEN_LATEST.labels("total").set(usage.total())
        TOKEN_LATEST.labels("input").set(usage.input_tokens or 0)
        TOKEN_LATEST.labels("output").set(usage.output_tokens or 0)
        TOKEN_LATEST.labels("cached").set(usage.cached_tokens or 0)
        TOKEN_LATEST.labels("reasoning").set(usage.reasoning_tokens or 0)


def _write_stack(root: Path, grafana_port: int, metrics_port: int) -> tuple[Path, Path]:
    prom = root / "prometheus.yml"
    prom.write_text(
        f"""global:
  scrape_interval: 500ms
  evaluation_interval: 500ms
scrape_configs:
  - job_name: heartbeat
    static_configs:
      - targets: ['host.docker.internal:{metrics_port}']
""",
        encoding="utf-8",
    )

    provisioning = root / "provisioning"
    (provisioning / "datasources").mkdir(parents=True)
    (provisioning / "dashboards").mkdir(parents=True)
    dashboards = root / "dashboards"
    dashboards.mkdir()

    (provisioning / "datasources" / "prometheus.yml").write_text(
        """apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://heartbeat-prometheus:9090
    isDefault: true
    editable: false
""",
        encoding="utf-8",
    )
    (provisioning / "dashboards" / "dashboard.yml").write_text(
        """apiVersion: 1
providers:
  - name: Heartbeat
    orgId: 1
    folder: Heartbeat
    type: file
    disableDeletion: false
    updateIntervalSeconds: 2
    options:
      path: /var/lib/grafana/dashboards
""",
        encoding="utf-8",
    )

    dashboard = {
        "annotations": {"list": []},
        "editable": True,
        "graphTooltip": 1,
        "panels": [
            {
                "type": "timeseries",
                "title": "Agent heartbeat",
                "gridPos": {"h": 10, "w": 24, "x": 0, "y": 0},
                "targets": [{"expr": "sum by (agent_type) (codex_heartbeat_agent_amplitude)", "legendFormat": "{{agent_type}}"}],
                "fieldConfig": {
                    "defaults": {
                        "min": -1.5,
                        "max": 1.6,
                        "custom": {"lineWidth": 2, "fillOpacity": 8, "drawStyle": "line"},
                    },
                    "overrides": [],
                },
                "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
            },
            {
                "type": "timeseries",
                "title": "Token usage records",
                "gridPos": {"h": 8, "w": 18, "x": 0, "y": 10},
                "targets": [{"expr": "codex_heartbeat_token_latest", "legendFormat": "{{kind}}"}],
                "fieldConfig": {"defaults": {"custom": {"lineWidth": 2, "fillOpacity": 15}}},
            },
            {
                "type": "stat",
                "title": "Turns",
                "gridPos": {"h": 4, "w": 3, "x": 18, "y": 10},
                "targets": [{"expr": "codex_heartbeat_turns"}],
            },
            {
                "type": "stat",
                "title": "Agents",
                "gridPos": {"h": 4, "w": 3, "x": 21, "y": 10},
                "targets": [{"expr": "codex_heartbeat_agents"}],
            },
            {
                "type": "stat",
                "title": "Token records",
                "gridPos": {"h": 4, "w": 3, "x": 18, "y": 14},
                "targets": [{"expr": "codex_heartbeat_token_records"}],
            },
            {
                "type": "stat",
                "title": "Completions",
                "gridPos": {"h": 4, "w": 3, "x": 21, "y": 14},
                "targets": [{"expr": "codex_heartbeat_task_completions"}],
            },
        ],
        "refresh": "1s",
        "schemaVersion": 39,
        "tags": ["codex", "heartbeat"],
        "time": {"from": "now-5m", "to": "now"},
        "timepicker": {"refresh_intervals": ["1s", "2s", "5s"]},
        "timezone": "browser",
        "title": "Codex Heartbeat",
        "uid": "codex-heartbeat",
        "version": 1,
    }
    (dashboards / "heartbeat.json").write_text(json.dumps(dashboard), encoding="utf-8")

    return provisioning, dashboards


def _docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True)


def run(selector: str = "current", grafana_port: int = 3000, metrics_port: int = 9464) -> None:
    if not shutil.which("docker"):
        raise SystemExit("heartbeat --grafana requires Docker / Docker Desktop.")

    start_http_server(metrics_port)
    stop = threading.Event()

    def collector() -> None:
        while not stop.is_set():
            try:
                _update_metrics(selector)
            except Exception:
                pass
            stop.wait(0.4)

    thread = threading.Thread(target=collector, daemon=True)
    thread.start()

    with tempfile.TemporaryDirectory(prefix="codex-heartbeat-grafana-") as tmp:
        root = Path(tmp)
        provisioning, dashboards = _write_stack(root, grafana_port, metrics_port)

        # Clean up stale containers from a prior interrupted run.
        subprocess.run(["docker", "rm", "-f", "heartbeat-grafana", "heartbeat-prometheus"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        _docker(
            "run", "-d", "--name", "heartbeat-prometheus",
            "-p", "127.0.0.1:9090:9090",
            "-v", f"{root / 'prometheus.yml'}:/etc/prometheus/prometheus.yml:ro",
            "prom/prometheus:latest",
            "--config.file=/etc/prometheus/prometheus.yml",
        )
        _docker(
            "run", "-d", "--name", "heartbeat-grafana",
            "--link", "heartbeat-prometheus:heartbeat-prometheus",
            "-p", f"127.0.0.1:{grafana_port}:3000",
            "-e", "GF_SECURITY_ADMIN_USER=admin",
            "-e", "GF_SECURITY_ADMIN_PASSWORD=heartbeat",
            "-e", "GF_AUTH_ANONYMOUS_ENABLED=true",
            "-e", "GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer",
            "-v", f"{provisioning}:/etc/grafana/provisioning:ro",
            "-v", f"{dashboards}:/var/lib/grafana/dashboards:ro",
            "grafana/grafana-oss:latest",
        )

        url = f"http://127.0.0.1:{grafana_port}/d/codex-heartbeat/codex-heartbeat?orgId=1&from=now-5m&to=now"
        print(f"Heartbeat Grafana: {url}")
        print("Grafana admin login if needed: admin / heartbeat")
        threading.Timer(3.0, lambda: webbrowser.open(url)).start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            subprocess.run(["docker", "rm", "-f", "heartbeat-grafana", "heartbeat-prometheus"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
