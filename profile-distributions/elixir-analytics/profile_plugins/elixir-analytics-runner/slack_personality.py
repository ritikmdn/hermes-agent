"""Chandler Slack voice and default answer composition."""

from __future__ import annotations

import re
from typing import Any


CHANDLER_PERSONALITY = """Chandler is Ritik's sharp business analyst in Slack.

He writes like a consultant briefing a founder: crisp bottom line first,
clean Slack formatting, one short readout, and only the scope caveat that
changes interpretation. Provenance, freshness, source tables, metric
contracts, and fine print stay available in the answer artifact, but they
are not dumped into Slack by default.
"""


_CARD_GTV_LAST_DAYS_RE = re.compile(r"^card_gtv_last_(\d+)d$")
_CARD_TRANSACTION_LAST_DAYS_RE = re.compile(r"^card_transaction_count_last_(\d+)d$")
_RUPEE_AMOUNT_RE = re.compile(r"₹\s*([0-9][0-9,]*(?:\.\d+)?)(cr|l|k)?", re.I)


def compose_chandler_slack_response(
    answer_payload: dict[str, Any],
    *,
    route: str | None = None,
    shortcut: str | None = None,
) -> str | None:
    """Return a human Slack answer for common quantitative artifacts."""
    del route

    shortcut = _shortcut_from_payload(answer_payload, shortcut)
    if not shortcut:
        return None

    if _is_card_gtv_shortcut(shortcut):
        return _compose_card_gtv(answer_payload, shortcut)

    if _is_card_transaction_shortcut(shortcut):
        return _compose_card_transactions(answer_payload, shortcut)

    if _is_merchant_card_spend_shortcut(shortcut):
        return _compose_merchant_card_spend(answer_payload, shortcut)

    return None


def sanitize_default_slack_text(text: str) -> str:
    """Remove report/audit lines from fallback Slack text."""
    clean_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if clean_lines and clean_lines[-1] != "":
                clean_lines.append("")
            continue
        if _is_default_hidden_audit_line(stripped):
            continue
        clean_lines.append(line.rstrip())

    while clean_lines and clean_lines[-1] == "":
        clean_lines.pop()
    while clean_lines and clean_lines[0] == "":
        clean_lines.pop(0)
    return "\n".join(clean_lines).strip()


def should_append_dashboard_link(
    answer_payload: dict[str, Any],
    *,
    route: str | None = None,
    shortcut: str | None = None,
) -> bool:
    """Single-number answers stay in Slack; exploratory answers can link out."""
    del route

    shortcut = _shortcut_from_payload(answer_payload, shortcut)
    result_type = _result_type(answer_payload)
    if result_type == "kpi":
        return False
    if shortcut and (
        _is_card_gtv_shortcut(shortcut)
        or _is_card_transaction_shortcut(shortcut)
        or _is_merchant_card_spend_shortcut(shortcut)
    ):
        return False
    return True


def _shortcut_from_payload(answer_payload: dict[str, Any], shortcut: str | None) -> str | None:
    if isinstance(shortcut, str) and shortcut.strip():
        return shortcut.strip()

    payload_shortcut = answer_payload.get("shortcut")
    if isinstance(payload_shortcut, str) and payload_shortcut.strip():
        return payload_shortcut.strip()

    artifact = answer_payload.get("answerArtifact")
    if isinstance(artifact, dict):
        source_runner = artifact.get("sourceRunner")
        if isinstance(source_runner, dict):
            artifact_shortcut = source_runner.get("shortcut")
            if isinstance(artifact_shortcut, str) and artifact_shortcut.strip():
                return artifact_shortcut.strip()

    return None


def _result_type(answer_payload: dict[str, Any]) -> str | None:
    result_type = answer_payload.get("resultType")
    if isinstance(result_type, str) and result_type.strip():
        return result_type.strip().lower()
    metadata = answer_payload.get("metadata")
    if isinstance(metadata, dict):
        metadata_result_type = metadata.get("resultType")
        if isinstance(metadata_result_type, str) and metadata_result_type.strip():
            return metadata_result_type.strip().lower()
    return None


def _rows(answer_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = answer_payload.get("rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _first_row(answer_payload: dict[str, Any]) -> dict[str, Any] | None:
    rows = _rows(answer_payload)
    return rows[0] if rows else None


def _is_card_gtv_shortcut(shortcut: str) -> bool:
    return shortcut in {
        "card_gtv_7d",
        "card_gtv_today",
        "card_gtv_yesterday",
        "card_gtv_this_week",
    } or bool(_CARD_GTV_LAST_DAYS_RE.match(shortcut))


def _is_card_transaction_shortcut(shortcut: str) -> bool:
    return shortcut in {
        "card_transaction_count_7d",
        "card_transaction_count_today",
        "card_transaction_count_yesterday",
        "card_transaction_count_this_week",
    } or bool(_CARD_TRANSACTION_LAST_DAYS_RE.match(shortcut))


def _is_merchant_card_spend_shortcut(shortcut: str) -> bool:
    return shortcut in {
        "merchant_card_spend_7d",
        "merchant_card_spend_today",
        "merchant_card_spend_yesterday",
        "merchant_card_spend_this_week",
    }


def _compose_card_gtv(answer_payload: dict[str, Any], shortcut: str) -> str | None:
    first_row = _first_row(answer_payload)
    amount = (
        _coerce_float(first_row.get("gtv"))
        if first_row
        else _amount_from_slack_text(answer_payload.get("slackText"))
    )
    if amount is None:
        return None

    period = _period_phrase(shortcut)
    first_line = f"*Bottom line:* Card GTV was *{_format_inr_human(amount)}* {period}."
    if not first_row:
        return first_line

    transactions = _coerce_int(first_row.get("transactions"))
    users = _coerce_int(first_row.get("users"))
    if transactions is None and users is None:
        return first_line

    readout = _transaction_user_readout(
        transactions=transactions,
        users=users,
        user_label="card users",
    )
    return _consultant_response(
        first_line,
        readout=readout,
        scope="Successful card spend only.",
    )


def _compose_card_transactions(answer_payload: dict[str, Any], shortcut: str) -> str | None:
    first_row = _first_row(answer_payload)
    if not first_row:
        return None

    transactions = _coerce_int(first_row.get("transactions"))
    if transactions is None:
        return None

    period = _period_phrase(shortcut)
    first_line = (
        f"*Bottom line:* Card transactions were "
        f"*{_format_number(transactions)}* {period}."
    )
    gtv = _coerce_float(first_row.get("gtv"))
    users = _coerce_int(first_row.get("users"))
    return _consultant_response(
        first_line,
        readout=_gtv_user_readout(gtv=gtv, users=users, user_label="card users"),
        scope="Successful card spend only.",
    )


def _compose_merchant_card_spend(answer_payload: dict[str, Any], shortcut: str) -> str | None:
    first_row = _first_row(answer_payload)
    if not first_row:
        return None

    amount = _coerce_float(first_row.get("gross_spend_inr") or first_row.get("gtv"))
    if amount is None:
        return None

    merchant = _merchant_label(answer_payload, first_row)
    period = _period_phrase(shortcut)
    first_line = (
        f"*Bottom line:* Card spend at {merchant} was "
        f"*{_format_inr_human(amount)}* {period}."
    )
    transactions = _coerce_int(first_row.get("txn_count") or first_row.get("transactions"))
    users = _coerce_int(first_row.get("user_count") or first_row.get("users"))
    readout = _transaction_user_readout(
        transactions=transactions,
        users=users,
        user_label="card users",
    )
    return _consultant_response(
        first_line,
        readout=readout,
        scope="Successful card spend only.",
    )


def _period_phrase(shortcut: str) -> str:
    if shortcut.endswith("_today"):
        return "today"
    if shortcut.endswith("_yesterday"):
        return "yesterday"
    if shortcut.endswith("_this_week"):
        return "this week"
    if shortcut.endswith("_7d"):
        return "in the last 7 completed days"

    match = _CARD_GTV_LAST_DAYS_RE.match(shortcut) or _CARD_TRANSACTION_LAST_DAYS_RE.match(
        shortcut
    )
    if match:
        return f"in the last {match.group(1)} completed days"

    return "for the requested window"


def _merchant_label(answer_payload: dict[str, Any], first_row: dict[str, Any]) -> str:
    for value in (
        first_row.get("merchant_query"),
        first_row.get("merchant_name"),
        _metadata_value(answer_payload, "merchantQuery"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip().title()
    return "the merchant"


def _metadata_value(answer_payload: dict[str, Any], key: str) -> Any:
    metadata = answer_payload.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get(key)
    return None


def _consultant_response(
    first_line: str,
    *,
    readout: str | None,
    scope: str,
) -> str:
    support_lines: list[str] = []
    if readout:
        support_lines.append(f"*Readout:* {readout}")
    support_lines.append(f"*Scope:* {scope}")
    return f"{first_line}\n\n" + "\n".join(support_lines)


def _transaction_user_readout(
    *,
    transactions: int | None,
    users: int | None,
    user_label: str,
) -> str | None:
    if transactions is not None and users is not None:
        return (
            f"{_format_number(transactions)} transactions across "
            f"{_format_number(users)} {user_label}."
        )
    if transactions is not None:
        return f"{_format_number(transactions)} transactions."
    if users is not None:
        return f"{_format_number(users)} {user_label}."
    return None


def _gtv_user_readout(
    *,
    gtv: float | None,
    users: int | None,
    user_label: str,
) -> str | None:
    if gtv is not None and users is not None:
        return f"{_format_inr_human(gtv)} GTV across {_format_number(users)} {user_label}."
    if gtv is not None:
        return f"{_format_inr_human(gtv)} GTV."
    if users is not None:
        return f"{_format_number(users)} {user_label}."
    return None


def _format_inr_human(value: float) -> str:
    amount = abs(value)
    sign = "-" if value < 0 else ""
    if amount >= 10_000_000:
        return f"{sign}₹{_trim_decimal(amount / 10_000_000)}Cr"
    if amount >= 100_000:
        return f"{sign}₹{_trim_decimal(amount / 100_000)}L"
    if amount >= 1_000:
        return f"{sign}₹{_trim_decimal(amount / 1_000)}K"
    return f"{sign}₹{amount:,.0f}"


def _trim_decimal(value: float) -> str:
    rounded = round(value, 1)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.1f}"


def _format_number(value: int) -> str:
    return f"{value:,}"


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _amount_from_slack_text(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    match = _RUPEE_AMOUNT_RE.search(value)
    if not match:
        return None
    amount = _coerce_float(match.group(1).replace(",", ""))
    if amount is None:
        return None
    unit = (match.group(2) or "").lower()
    if unit == "cr":
        return amount * 10_000_000
    if unit == "l":
        return amount * 100_000
    if unit == "k":
        return amount * 1_000
    return amount


def _is_default_hidden_audit_line(stripped: str) -> bool:
    normalized = stripped.lower().lstrip("-* ").strip()
    return normalized.startswith(
        (
            "fine print:",
            "freshness:",
            "source table:",
            "source tables:",
            "metric contract:",
            "metric contracts:",
            "working assumptions:",
            "assumptions:",
            "caveats:",
            "window:",
        )
    )
