from __future__ import annotations

import argparse

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="heartbeat",
        description="Live observability for local Codex and Claude Code sessions",
    )
    parser.add_argument(
        "session",
        nargs="?",
        default="current",
        help="current, a session ID, or a session JSONL path",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--flask", action="store_true", help="Run the local Flask dashboard")
    mode.add_argument("--grafana", action="store_true", help="Run the Grafana + Prometheus dashboard (Docker required)")
    parser.add_argument("--provider", choices=("auto", "codex", "claude"), default="auto",
                        help="Session source (default: latest active Codex or Claude Code session)")
    parser.add_argument("--port", type=int, default=None, help="UI port (Flask default 8765, Grafana default 3000)")
    args = parser.parse_args()

    if args.flask:
        from codex_heartbeat.flask_app import run
        run(args.session, port=args.port or 8765, provider=args.provider)
        return

    if args.grafana:
        if args.provider == "claude":
            parser.error("Claude Code is currently supported by the Flask dashboard only")
        from codex_heartbeat.grafana_mode import run
        run(args.session, grafana_port=args.port or 3000)
        return

    from codex_heartbeat.flask_app import run
    run(args.session, port=args.port or 8765, provider=args.provider)
