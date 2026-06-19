"""Answer payload compaction and direct-response helpers for Chandler analytics."""

from __future__ import annotations

from typing import Any

from .runner_modes import DEFAULT_ANALYTICS_BASE_URL


MAX_COMPACT_SLACK_TEXT_CHARS = 2200
MAX_DIRECT_FINAL_SLACK_TEXT_CHARS = 1400


def safe_payload_summary(payload: Any) -> dict[str, Any]:
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


def compact_slack_text(text: str, *, limit: int = MAX_COMPACT_SLACK_TEXT_CHARS) -> str:
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


def dashboard_label_for_url(url: str) -> str:
    return "Open visualization" if "payload=" in url or "result=" in url else "Open dashboard"


def slack_dashboard_line_from_text(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("dashboard:"):
            continue

        value = stripped.split(":", 1)[1].strip()
        if not value:
            return None
        if value.startswith("<") and "|" in value and value.endswith(">"):
            return f"Dashboard: {value}"
        if value.startswith("http://") or value.startswith("https://"):
            return f"Dashboard: <{value}|{dashboard_label_for_url(value)}>"
        return stripped
    return None


def compact_direct_final_slack_text(
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
        while compact_lines:
            candidate = "\n".join(compact_lines).strip()
            suffix_length = len(suffix) + (1 if candidate else 0)
            if len(candidate) + suffix_length <= limit:
                compact_text = candidate
                break
            compact_lines.pop()
        else:
            compact_text = ""
        compact_text = f"{compact_text}\n{suffix}".strip()
    return compact_text or text[:limit].strip()


def compact_answer_question_payload(payload: Any) -> Any:
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
        "choices",
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
        slack_dashboard_line = slack_dashboard_line_from_text(slack_text)
        compact_payload["slackText"] = compact_slack_text(slack_text)
        if slack_dashboard_line:
            compact_payload["slackDashboardLine"] = slack_dashboard_line

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


def direct_dashboard_link(answer_payload: dict[str, Any]) -> str | None:
    dashboard_url = answer_payload.get("dashboardUrl")
    if isinstance(dashboard_url, str) and dashboard_url.strip():
        return dashboard_url.strip()

    dashboard_path = answer_payload.get("dashboardUrlPath")
    if isinstance(dashboard_path, str) and dashboard_path.strip():
        if dashboard_path.startswith("http://") or dashboard_path.startswith("https://"):
            return dashboard_path.strip()
        return f"{DEFAULT_ANALYTICS_BASE_URL}{dashboard_path}"
    return None


def slack_dashboard_link(answer_payload: dict[str, Any]) -> str | None:
    dashboard_link = direct_dashboard_link(answer_payload)
    if not dashboard_link:
        return None

    label = (
        "Open visualization"
        if "payload=" in dashboard_link or "result=" in dashboard_link
        else "Open dashboard"
    )
    return f"<{dashboard_link}|{label}>"


def direct_final_response_for_answer_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    nested_payload = payload.get("payload")
    answer_payload = nested_payload if isinstance(nested_payload, dict) else payload
    slack_text = answer_payload.get("slackText")
    if not isinstance(slack_text, str) or not slack_text.strip():
        return None

    final_text = compact_direct_final_slack_text(slack_text)
    dashboard_line = answer_payload.get("slackDashboardLine")
    if not isinstance(dashboard_line, str) or not dashboard_line.strip():
        dashboard_line = slack_dashboard_line_from_text(slack_text)
    if dashboard_line and "dashboard:" not in final_text.lower():
        final_text = f"{final_text}\n\n{dashboard_line.strip()}"
    return final_text


def clarify_instruction_for_answer_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    nested_payload = payload.get("payload")
    answer_payload = nested_payload if isinstance(nested_payload, dict) else payload
    clarification = answer_payload.get("clarificationQuestion")
    if not isinstance(clarification, str) or not clarification.strip():
        return None

    return (
        "Call `clarify` with `payload.payload.clarificationQuestion` before "
        "answering or querying. Do not send the clarification as a final "
        "assistant response."
    )
