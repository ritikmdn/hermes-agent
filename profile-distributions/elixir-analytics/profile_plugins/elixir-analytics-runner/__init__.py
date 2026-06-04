"""Profile-owned Hermes tools for Elixir analytics runner calls."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any


TOOLSET = "elixir-analytics-runner"
ANALYTICS_REPO_ENV = "ELIXIR_ANALYTICS_REPO"
DEFAULT_ANALYTICS_REPO = "/Users/ritik/Coding/claude-analytics"
DEFAULT_ANALYTICS_BASE_URL = "https://analytics.joinelixir.club"
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 900
DEFAULT_MAX_ROWS = 500
DEFAULT_QUESTION_MAX_ROWS = 100
MAX_ROWS = 5000
MAX_COMPACT_SLACK_TEXT_CHARS = 2200
MAX_DIRECT_FINAL_SLACK_TEXT_CHARS = 500
LOGGER = logging.getLogger("hermes.elixir_analytics_runner")
COMPACT_ELIXIR_ANALYTICS_SKILL = """# Elixir Analytics Runtime Brief

## Mandatory Slack Fast Path

For every plain Slack analytics data question, call `elixir_analytics_runner`
with mode='answer_question' and the exact raw Slack question before planning,
querying manually, inspecting files, or editing source.

If the completed result includes `payload.slackText`, use that text as the
Slack-facing answer. Add at most one short caveat sentence. Do not expose hidden
SQL/HogQL, raw rows, or source-maintenance work before replying.

Use `max_rows: 25` for user lists, merchant lists, rankings, and breakdowns
unless the user explicitly asks for a larger export.

## Routing

- Saved business metrics: prefer `answer_question`; it promotes known topics
  such as `show GTV last 30 days by week` to saved query dashboards.
- Supabase business questions: use `answer_question` first, then
  `supabase_ad_hoc` only if the runner asks for a model-built request.
- PostHog app questions: use `answer_question` first, then `posthog_ad_hoc`
  only if needed. Keep app active users separate from card active users unless
  the user asks to combine definitions.
- Ambiguous "active users": ask whether the user means card active, app active,
  or combined active before querying.
- Definition/glossary/query/dashboard change requests: use
  `source_change_plan`, then `source_change_scope_check` before committing.
- Self-improvement reviews: use `self_improvement_check`, then
  `self_improvement_plan` only when due or explicitly requested.

## Answer Rules

Include rows or a compact summary, date window, freshness, assumptions/caveats,
and a direct dashboard link when the runner returns one. Never mutate analytics
source tables. Keep generic Hermes tools available for debugging, source
changes, repo edits, and runner gaps.
"""


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _profile_env() -> dict[str, str]:
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


def _analytics_repo(env: dict[str, str]) -> Path:
    return Path(env.get(ANALYTICS_REPO_ENV) or DEFAULT_ANALYTICS_REPO).expanduser()


def _parse_json_stdout(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return stdout


def _bounded_tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _request_json(args: dict[str, Any]) -> str | None:
    request = args.get("request")
    if request is None:
        return None
    if isinstance(request, str):
        return request
    return json.dumps(request, ensure_ascii=False)


def _changed_files_json(args: dict[str, Any]) -> str:
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
            "changed_files must be a list of file paths or a newline/comma separated string."
        )
    return json.dumps(changed_files, ensure_ascii=False)


def _safe_payload_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payloadType": type(payload).__name__}

    summary: dict[str, Any] = {}
    route = payload.get("route")
    shortcut = payload.get("shortcut")
    if route:
        summary["route"] = route
    if shortcut:
        summary["shortcut"] = shortcut

    nested_payload = payload.get("payload")
    row_payload = nested_payload if isinstance(nested_payload, dict) else payload

    topic_id = row_payload.get("topicId")
    result_type = row_payload.get("resultType")
    if not result_type and isinstance(row_payload.get("metadata"), dict):
        result_type = row_payload["metadata"].get("resultType")

    row_count = row_payload.get("rowCount")
    if row_count is None and isinstance(row_payload.get("rows"), list):
        row_count = len(row_payload["rows"])

    fields = {
        "topicId": topic_id,
        "kind": row_payload.get("kind"),
        "requiresClarification": row_payload.get("requiresClarification"),
        "prRequired": row_payload.get("prRequired"),
        "entriesReviewed": row_payload.get("entriesReviewed"),
        "latestQueryNumber": row_payload.get("latestQueryNumber"),
        "reviewDue": row_payload.get("reviewDue"),
        "status": row_payload.get("status"),
        "suggestionCount": (
            row_payload.get("suggestionCount")
            if row_payload.get("suggestionCount") is not None
            else len(row_payload["suggestions"])
            if isinstance(row_payload.get("suggestions"), list)
            else None
        ),
        "resultType": result_type,
        "rowCount": row_count,
        "truncated": row_payload.get("truncated"),
        "dryRun": row_payload.get("dryRun"),
        "dashboard": bool(
            row_payload.get("dashboardUrl") or row_payload.get("dashboardUrlPath")
        ),
    }
    summary.update({key: value for key, value in fields.items() if value is not None})
    return summary


def _compact_slack_text(text: str, *, limit: int = MAX_COMPACT_SLACK_TEXT_CHARS) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not line.strip().lower().startswith("dashboard:")
    ]
    compact_lines: list[str] = []
    length = 0
    truncated = False
    for line in lines:
        next_length = length + len(line) + (1 if compact_lines else 0)
        if next_length > limit:
            truncated = True
            break
        compact_lines.append(line)
        length = next_length

    compact_text = "\n".join(compact_lines).strip()
    if truncated:
        compact_text = (
            f"{compact_text}\n"
            "Slack handoff truncated; use payload.dashboardUrl for the full visualization."
        ).strip()
    return compact_text or text[:limit]


def _compact_direct_final_slack_text(
    text: str,
    *,
    limit: int = MAX_DIRECT_FINAL_SLACK_TEXT_CHARS,
) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not line.strip().lower().startswith("dashboard:")
    ]
    compact_lines: list[str] = []
    length = 0
    truncated = False
    for line in lines:
        next_length = length + len(line) + (1 if compact_lines else 0)
        if next_length > limit:
            truncated = True
            break
        compact_lines.append(line)
        length = next_length

    compact_text = "\n".join(compact_lines).strip()
    if truncated:
        suffix = "More rows in the dashboard."
        suffix_length = len(suffix) + (1 if compact_text else 0)
        compact_text = compact_text[: max(0, limit - suffix_length)].rstrip()
        compact_text = f"{compact_text}\n{suffix}".strip()
    return compact_text or text[:limit].strip()


def _compact_answer_question_payload(payload: Any) -> Any:
    """Keep Slack answer handoff compact without hiding route evidence."""
    if not isinstance(payload, dict):
        return payload

    nested_payload = payload.get("payload")
    answer_payload = nested_payload if isinstance(nested_payload, dict) else payload
    metadata = answer_payload.get("metadata")
    safe_metadata_keys = {
        "interpretedDefinition",
        "metricIds",
        "resultType",
        "sources",
        "dateWindow",
        "timezone",
        "assumptions",
        "caveats",
        "freshness",
        "resultSummary",
        "friction",
        "conventionAdded",
        "repeatPromoteCandidate",
    }

    compact: dict[str, Any] = {}
    for key in ("ok", "route", "shortcut", "error", "errorType", "message"):
        if key in payload:
            compact[key] = payload[key]

    compact_payload: dict[str, Any] = {}
    for key in (
        "ok",
        "topicId",
        "title",
        "kind",
        "resultType",
        "requiresClarification",
        "clarificationQuestion",
        "rowCount",
        "truncated",
        "dryRun",
        "dashboardUrl",
        "dateWindow",
    ):
        if key in answer_payload:
            compact_payload[key] = answer_payload[key]

    if "dashboardUrl" not in compact_payload and "dashboardUrlPath" in answer_payload:
        compact_payload["dashboardUrlPath"] = answer_payload["dashboardUrlPath"]

    slack_text = answer_payload.get("slackText")
    if isinstance(slack_text, str) and slack_text.strip():
        compact_payload["slackText"] = _compact_slack_text(slack_text)

    if isinstance(metadata, dict):
        safe_metadata = {
            key: value
            for key, value in metadata.items()
            if key in safe_metadata_keys and value is not None
        }
        if safe_metadata:
            compact_payload["metadata"] = safe_metadata

    if compact_payload:
        compact["payload"] = compact_payload

    return compact


def _direct_dashboard_link(answer_payload: dict[str, Any]) -> str | None:
    dashboard_url = answer_payload.get("dashboardUrl")
    if isinstance(dashboard_url, str) and dashboard_url.strip():
        return dashboard_url.strip()

    dashboard_path = answer_payload.get("dashboardUrlPath")
    if isinstance(dashboard_path, str) and dashboard_path.strip():
        if dashboard_path.startswith("http://") or dashboard_path.startswith("https://"):
            return dashboard_path.strip()
        return f"{DEFAULT_ANALYTICS_BASE_URL}{dashboard_path}"
    return None


def _direct_final_response_for_answer_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    nested_payload = payload.get("payload")
    answer_payload = nested_payload if isinstance(nested_payload, dict) else payload
    slack_text = answer_payload.get("slackText")
    if not isinstance(slack_text, str) or not slack_text.strip():
        clarification = answer_payload.get("clarificationQuestion")
        if isinstance(clarification, str) and clarification.strip():
            return clarification.strip()
        return None

    final_text = _compact_direct_final_slack_text(slack_text)
    dashboard_link = _direct_dashboard_link(answer_payload)
    if dashboard_link and "dashboard:" not in final_text.lower():
        final_text = f"{final_text}\n\nDashboard: {dashboard_link}"
    return final_text


def _is_elixir_analytics_skill_request(args: dict[str, Any]) -> bool:
    name = str(args.get("name") or "").strip().lower()
    if str(args.get("file_path") or args.get("filePath") or "").strip():
        return False
    return name in {"elixir-analytics", "analytics/elixir-analytics"}


def _compact_skill_view_result(args: dict[str, Any], result: str) -> str | None:
    if not _is_elixir_analytics_skill_request(args):
        return None

    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None

    result_name = str(payload.get("name") or "").strip().lower()
    if result_name not in {"elixir-analytics", "analytics/elixir-analytics"}:
        return None

    compact_payload = dict(payload)
    compact_payload["content"] = COMPACT_ELIXIR_ANALYTICS_SKILL
    compact_payload["contentCompacted"] = True
    return _json_dumps(compact_payload)


def _transform_tool_result(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: str,
    **_: Any,
) -> str | None:
    if tool_name == "skill_view" and isinstance(args, dict) and isinstance(result, str):
        return _compact_skill_view_result(args, result)
    return None


def _log_runner_result(result: dict[str, Any]) -> None:
    fields = {
        "mode": result.get("mode"),
        "ok": result.get("ok"),
        "elapsedSeconds": result.get("elapsedSeconds"),
        "errorType": result.get("errorType"),
    }
    if result.get("ok"):
        fields.update(_safe_payload_summary(result.get("payload")))
        LOGGER.info(
            "analytics runner completed %s",
            " ".join(f"{key}={value}" for key, value in fields.items() if value is not None),
        )
    else:
        LOGGER.warning(
            "analytics runner failed %s",
            " ".join(f"{key}={value}" for key, value in fields.items() if value is not None),
        )


def _runner_command(args: dict[str, Any]) -> tuple[list[str], str | None]:
    mode = str(args.get("mode") or "").strip()
    dry_run = _coerce_bool(args.get("dry_run"))
    max_rows = _coerce_int(args.get("max_rows"), DEFAULT_MAX_ROWS, minimum=1, maximum=MAX_ROWS)

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
        max_rows = _coerce_int(
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
        request_json = _request_json(args)
        if not request_json:
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
        return command, request_json

    if mode == "posthog_ad_hoc":
        request_json = _request_json(args)
        if not request_json:
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
        return command, request_json

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
            _changed_files_json(args),
        ]
        if _coerce_bool(args.get("allow_unexpected_files") or args.get("allowUnexpectedFiles")):
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
        "mode must be one of plan, answer_question, saved_topic, supabase_ad_hoc, posthog_ad_hoc, source_change_plan, source_change_scope_check, self_improvement_check, self_improvement_plan."
    )


def run_elixir_analytics_runner(args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode") or "").strip()
    if not mode and str(args.get("question") or "").strip():
        mode = "answer_question"
    started = time.monotonic()
    timeout_seconds = _coerce_int(
        args.get("timeout_seconds"),
        DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=MAX_TIMEOUT_SECONDS,
    )

    try:
        command, stdin = _runner_command(args)
        env = _profile_env()
        repo = _analytics_repo(env)
        if not repo.is_dir():
            result = {
                "ok": False,
                "mode": mode,
                "errorType": "missing_repo",
                "message": f"Analytics repo not found at {repo}",
            }
            _log_runner_result(result)
            return result

        completed = subprocess.run(
            command,
            cwd=str(repo),
            env=env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = {
            "ok": False,
            "mode": mode,
            "errorType": "timeout",
            "timeoutSeconds": timeout_seconds,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "stdout": _bounded_tail(str(exc.output or "")),
            "stderr": _bounded_tail(str(exc.stderr or "")),
        }
        _log_runner_result(result)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "mode": mode,
            "errorType": exc.__class__.__name__,
            "message": str(exc),
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }
        _log_runner_result(result)
        return result

    payload = _parse_json_stdout(completed.stdout)
    if completed.returncode == 0 and mode == "answer_question":
        payload = _compact_answer_question_payload(payload)

    if completed.returncode != 0:
        result = {
            "ok": False,
            "mode": mode,
            "errorType": "runner_failed",
            "exitCode": completed.returncode,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "stdout": _bounded_tail(completed.stdout),
            "stderr": _bounded_tail(completed.stderr),
            "payload": payload,
        }
        _log_runner_result(result)
        return result

    result = {
        "ok": True,
        "mode": mode,
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "payload": payload,
    }
    if mode == "answer_question":
        direct_final_response = _direct_final_response_for_answer_payload(payload)
        if direct_final_response:
            result["hermes_direct_final_response"] = direct_final_response
    _log_runner_result(result)
    return result


def _handler(args: dict[str, Any], **_: Any) -> str:
    return _json_dumps(run_elixir_analytics_runner(args))


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_tool(
        name="elixir_analytics_runner",
        toolset=TOOLSET,
        schema={
            "name": "elixir_analytics_runner",
            "description": (
                "Run Elixir analytics common-question shortcuts, planner, "
                "saved-topic, Supabase ad hoc, PostHog ad hoc, source-change, "
                "or self-improvement runners without shell/code-execution setup. "
                "Use mode='answer_question' first for plain Slack analytics "
                "questions. If a completed answer_question payload includes "
                "payload.slackText, use it as the Slack-facing final answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "description": (
                            "Runner mode. For plain Slack analytics questions, "
                            "use answer_question first with the exact raw question."
                        ),
                        "type": "string",
                        "enum": [
                            "plan",
                            "answer_question",
                            "saved_topic",
                            "supabase_ad_hoc",
                            "posthog_ad_hoc",
                            "source_change_plan",
                            "source_change_scope_check",
                            "self_improvement_check",
                            "self_improvement_plan",
                        ],
                    },
                    "question": {"type": "string"},
                    "topic_id": {"type": "string"},
                    "range": {
                        "type": "string",
                        "enum": ["7d", "30d", "90d"],
                        "default": "30d",
                    },
                    "request": {
                        "description": (
                            "AdHocQueryRequest/PostHogQueryRequest object for "
                            "ad hoc modes, or source-change request text for "
                            "source_change_plan/source_change_scope_check mode."
                        ),
                        "anyOf": [
                            {"type": "object"},
                            {"type": "string"},
                        ],
                    },
                    "changed_files": {
                        "description": (
                            "Changed repo file paths for source_change_scope_check."
                        ),
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "allow_unexpected_files": {
                        "description": (
                            "For source_change_scope_check: downgrade files outside "
                            "the source-change plan from blockers to warnings."
                        ),
                        "type": "boolean",
                        "default": False,
                    },
                    "query_log": {
                        "description": (
                            "Query log path for self_improvement_check or "
                            "self_improvement_plan. Defaults to QUERY_LOG.md "
                            "in the analytics repo."
                        ),
                        "type": "string",
                    },
                    "max_rows": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_ROWS,
                        "default": DEFAULT_MAX_ROWS,
                    },
                    "dry_run": {"type": "boolean", "default": False},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "default": DEFAULT_TIMEOUT_SECONDS,
                    },
                },
                "required": ["mode"],
                "additionalProperties": False,
            },
        },
        handler=_handler,
        description="Run Elixir analytics deterministic runners.",
    )
