from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import json
import subprocess
import sys
from typing import Any

from .config import ROOT
from .state import StateStore


DEFAULT_HERMES_REPO = ROOT / "external" / "hermes-agent"
DEFAULT_HERMES_PYTHON = ROOT / ".venv-hermes" / "Scripts" / "python.exe"
DEFAULT_HERMES_HOME = ROOT / "data" / "hermes" / "home"
DEFAULT_CONTEXT_PATH = ROOT / "data" / "hermes" / "trading_context.md"


def hermes_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = (config or {}).get("hermes", {}) if config else {}
    repo = Path(cfg.get("repo_path") or DEFAULT_HERMES_REPO)
    launcher = repo / "hermes"
    cli = repo / "cli.py"
    python_path = Path(cfg.get("python_path") or DEFAULT_HERMES_PYTHON)
    home_path = Path(cfg.get("home_path") or DEFAULT_HERMES_HOME)
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "repo_path": str(repo),
        "python_path": str(python_path),
        "home_path": str(home_path),
        "config_exists": (home_path / "config.yaml").exists(),
        "repo_exists": repo.exists(),
        "python_exists": python_path.exists(),
        "home_exists": home_path.exists(),
        "launcher_exists": launcher.exists(),
        "cli_exists": cli.exists(),
        "context_path": str(Path(cfg.get("context_path") or DEFAULT_CONTEXT_PATH)),
        "read_only": bool(cfg.get("read_only", True)),
    }


def export_hermes_context(store: StateStore, path: Path = DEFAULT_CONTEXT_PATH) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    trades = store.query(
        """
        select id, opened_at, closed_at, symbol, side, strategy, setup_tier,
               entry, initial_stop_loss, stop_loss, take_profit, rr, score,
               execution_mode, status, exit_price, pnl, exit_reason,
               hybrid_mt5_status, hybrid_mt5_pnl, hybrid_mt5_message,
               reason
        from trades
        order by opened_at desc
        limit 120
        """
    )
    snapshots = store.query(
        """
        select id, created_at, candidate_id, trade_id, symbol, side, strategy,
               decision, entry, stop_loss, take_profit, rr, score,
               outcome_status, outcome_pnl, outcome_exit_reason,
               order_book_json, order_flow_json
        from decision_snapshots
        order by created_at desc
        limit 120
        """
    )
    options = store.latest_options_positioning("GC", limit=80)

    context = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": "Read-only trading app context for external NousResearch/hermes-agent.",
        "rules": [
            "Do not place trades.",
            "Do not modify config or code.",
            "Only analyze saved trades, snapshots, options positioning, and execution logs.",
            "Return observations and suggestions only.",
        ],
        "trades": trades,
        "decision_snapshots": snapshots,
        "gc_options_positioning": options,
    }
    markdown = [
        "# Trading Agent Context for Hermes",
        "",
        f"Generated: {context['generated_at']}",
        "",
        "## Rules",
        *[f"- {rule}" for rule in context["rules"]],
        "",
        "## JSON Context",
        "```json",
        json.dumps(context, indent=2, default=str),
        "```",
        "",
    ]
    path.write_text("\n".join(markdown), encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "trades": len(trades),
        "snapshots": len(snapshots),
        "options_rows": len(options.get("rows") or []),
    }


def ask_hermes(
    store: StateStore,
    message: str,
    config: dict[str, Any] | None = None,
    *,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    cfg = (config or {}).get("hermes", {}) if config else {}
    status = hermes_status(config)
    context_path = Path(cfg.get("context_path") or DEFAULT_CONTEXT_PATH)
    export_result = export_hermes_context(store, context_path)
    if not status["enabled"]:
        return {"ok": False, "error": "Hermes integration is disabled.", "export": export_result, "status": status}
    if not status["repo_exists"]:
        return {"ok": False, "error": "Hermes repo is not cloned.", "export": export_result, "status": status}
    if not status.get("config_exists"):
        return {
            "ok": False,
            "error": "Hermes is cloned and installed, but Hermes setup/model config is not complete yet. Run Hermes setup in the local venv before chat.",
            "export": export_result,
            "status": status,
        }

    repo = Path(status["repo_path"])
    prompt = (
        "You are external Hermes Agent connected to a trading app in read-only mode.\n"
        f"Read this context file first: {context_path}\n"
        "Do not modify files, code, configs, database rows, or trades. Suggest only.\n\n"
        f"User question: {message}"
    )
    command = [str(repo / "hermes"), "-q", prompt]
    if sys.platform == "win32":
        command = [status["python_path"] if status.get("python_exists") else sys.executable, str(repo / "cli.py"), "-q", prompt]

    try:
        hermes_home = Path(status["home_path"])
        hermes_home.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "HERMES_HOME": str(hermes_home),
        }
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "error": f"Hermes launch failed: {exc}",
            "export": export_result,
            "status": status,
        }

    ok = completed.returncode == 0
    return {
        "ok": ok,
        "answer": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "returncode": completed.returncode,
        "export": export_result,
        "status": status,
    }
