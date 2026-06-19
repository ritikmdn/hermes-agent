"""Runner mode command construction for the Elixir analytics profile plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


ANALYTICS_REPO_ENV = "ELIXIR_ANALYTICS_REPO"
DEFAULT_ANALYTICS_REPO = "/Users/ritik/Coding/claude-analytics"
DEFAULT_ANALYTICS_BASE_URL = "https://analytics.joinelixir.club"
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 900
DEFAULT_MAX_ROWS = 500
DEFAULT_QUESTION_MAX_ROWS = 100
MAX_ROWS = 5000


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def profile_env() -> dict[str, str]:
    env = dict(os.environ)
    hermes_home = env.get("HERMES_HOME")
    if hermes_home:
        env_file = Path(hermes_home) / ".env"
        if env_file.is_file():
            for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key or key in env:
                    continue
                value = value.strip().strip("'\"")
                env[key] = value
    env.setdefault("ANALYTICS_BASE_URL", DEFAULT_ANALYTICS_BASE_URL)
    return env


def analytics_repo(env: dict[str, str]) -> Path:
    return Path(env.get(ANALYTICS_REPO_ENV) or DEFAULT_ANALYTICS_REPO).expanduser()


def parse_json_stdout(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return stdout


def bounded_tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def request_json(args: dict[str, Any]) -> str | None:
    request = args.get("request")
    if request is None:
        return None
    if isinstance(request, str):
        return request
    return json.dumps(request, ensure_ascii=False)


def changed_files_json(args: dict[str, Any]) -> str:
    changed_files = args.get("changed_files") or args.get("changedFiles") or []
    if isinstance(changed_files, str):
        changed_files = [
            line.strip()
            for line in changed_files.replace(",", "\n").splitlines()
            if line.strip()
        ]
    if not isinstance(changed_files, list) or not all(
        isinstance(item, str) for item in changed_files
    ):
        raise ValueError(
            "changed_files must be a list of file paths or a newline/comma "
            "separated string."
        )
    return json.dumps(changed_files, ensure_ascii=False)


def runner_command(args: dict[str, Any]) -> tuple[list[str], str | None]:
    mode = str(args.get("mode") or "").strip()
    dry_run = coerce_bool(args.get("dry_run"))
    max_rows = coerce_int(
        args.get("max_rows"),
        DEFAULT_MAX_ROWS,
        minimum=1,
        maximum=MAX_ROWS,
    )

    if not mode and str(args.get("question") or "").strip():
        mode = "answer_question"

    if mode == "plan":
        question = str(args.get("question") or "").strip()
        if not question:
            raise ValueError("mode='plan' requires question.")
        return [
            "node",
            "--import",
            "tsx",
            "scripts/plan-analytics-question.ts",
        ], question

    if mode == "answer_question":
        question = str(args.get("question") or "").strip()
        if not question:
            raise ValueError("mode='answer_question' requires question.")
        max_rows = coerce_int(
            args.get("max_rows"),
            DEFAULT_QUESTION_MAX_ROWS,
            minimum=1,
            maximum=MAX_ROWS,
        )
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/run-analytics-question.ts",
            "--max-rows",
            str(max_rows),
        ]
        if dry_run:
            command.append("--dry-run")
        return command, question

    if mode == "saved_topic":
        topic_id = str(args.get("topic_id") or args.get("topicId") or "").strip()
        if not topic_id:
            raise ValueError("mode='saved_topic' requires topic_id.")
        range_key = str(args.get("range") or "30d").strip()
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/run-saved-query-topic.ts",
            topic_id,
            "--range",
            range_key,
        ]
        if dry_run:
            command.append("--dry-run")
        return command, None

    if mode == "supabase_ad_hoc":
        stdin = request_json(args)
        if not stdin:
            raise ValueError("mode='supabase_ad_hoc' requires request JSON.")
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/run-ad-hoc-query.ts",
            "--max-rows",
            str(max_rows),
        ]
        if dry_run:
            command.append("--dry-run")
        return command, stdin

    if mode == "posthog_ad_hoc":
        stdin = request_json(args)
        if not stdin:
            raise ValueError("mode='posthog_ad_hoc' requires request JSON.")
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/run-posthog-query.ts",
            "--max-rows",
            str(max_rows),
        ]
        if dry_run:
            command.append("--dry-run")
        return command, stdin

    if mode == "source_change_plan":
        request = str(args.get("request") or args.get("question") or "").strip()
        if not request:
            raise ValueError("mode='source_change_plan' requires request.")
        return [
            "node",
            "--import",
            "tsx",
            "scripts/plan-source-change.ts",
        ], request

    if mode == "source_change_scope_check":
        request = str(args.get("request") or args.get("question") or "").strip()
        if not request:
            raise ValueError("mode='source_change_scope_check' requires request.")
        command = [
            "node",
            "--import",
            "tsx",
            "scripts/check-source-change-scope.ts",
            "--changed-files-json",
            changed_files_json(args),
        ]
        if coerce_bool(args.get("allow_unexpected_files") or args.get("allowUnexpectedFiles")):
            command.append("--allow-unexpected-files")
        return command, request

    if mode in {"self_improvement_check", "self_improvement_plan"}:
        query_log = str(args.get("query_log") or args.get("queryLog") or "QUERY_LOG.md").strip()
        if not query_log:
            raise ValueError(f"mode='{mode}' requires query_log.")
        script = (
            "scripts/check-self-improvement-cadence.ts"
            if mode == "self_improvement_check"
            else "scripts/plan-self-improvement.ts"
        )
        return [
            "node",
            "--import",
            "tsx",
            script,
            "--query-log",
            query_log,
        ], None

    raise ValueError(
        "mode must be one of plan, answer_question, saved_topic, supabase_ad_hoc, "
        "posthog_ad_hoc, source_change_plan, source_change_scope_check, "
        "self_improvement_check, self_improvement_plan."
    )
