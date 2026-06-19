import importlib.util
import json
import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PATH = (
    REPO_ROOT
    / "profile-distributions"
    / "elixir-analytics"
    / "plugins"
    / "elixir-analytics-runner"
    / "__init__.py"
)


def _load_plugin_module():
    module_name = "test_elixir_analytics_runner_plugin_pkg"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_PATH,
        submodule_search_locations=[str(PLUGIN_PATH.parent)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(PLUGIN_PATH.parent)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _gateway_session_vars(*, platform: str, user_id: str, user_name: str):
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform=platform,
        chat_id="C_ANALYTICS",
        chat_name="analytics",
        user_id=user_id,
        user_name=user_name,
        session_key="agent:main:slack:channel:C_ANALYTICS",
    )
    try:
        yield
    finally:
        clear_session_vars(tokens)


class _Completed:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _Ctx:
    def __init__(self):
        self.tools = []
        self.hooks = {}

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)


def _slack_event(text: str, *, user_id: str = "ritik", user_name: str = "Ritik Madan"):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    return MessageEvent(
        text=text,
        message_id="1780654834.000000",
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="D0B7CHZFGA3",
            chat_type="dm",
            user_id=user_id,
            user_name=user_name,
            thread_id="1780654833.865679",
        ),
    )


class _FakeSessionStore:
    def __init__(self, session_key: str, transcript: list[dict]):
        self._entries = {session_key: SimpleNamespace(session_id="session_1")}
        self.transcript = transcript

    def _generate_session_key(self, source):
        return f"agent:main:slack:dm:{source.chat_id}:{source.thread_id}"

    def _ensure_loaded(self):
        return None

    def load_transcript(self, session_id):
        assert session_id == "session_1"
        return self.transcript


def test_registers_pre_gateway_agent_handoff_hook():
    module = _load_plugin_module()
    ctx = _Ctx()

    module.register(ctx)

    assert "pre_gateway_dispatch" in ctx.hooks


def test_pre_gateway_agent_handoff_lives_in_slack_handoff_module():
    module = _load_plugin_module()
    ctx = _Ctx()

    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    assert hook.__module__.endswith(".slack_handoff")


def test_runner_command_lives_in_runner_modes_module():
    module = _load_plugin_module()

    assert module._runner_command.__module__.endswith(".runner_modes")


def test_answer_payload_helpers_live_in_answer_payloads_module():
    module = _load_plugin_module()

    assert module._compact_answer_question_payload.__module__.endswith(".answer_payloads")
    assert module._direct_final_response_for_answer_payload.__module__.endswith(
        ".answer_payloads"
    )
    assert module._safe_payload_summary.__module__.endswith(".answer_payloads")


def test_source_control_guard_lives_in_source_control_guard_module():
    module = _load_plugin_module()

    assert module._permission_denied_result.__module__.endswith(".source_control_guard")
    assert module._block_non_ritik_source_control_tool.__module__.endswith(
        ".source_control_guard"
    )


def test_shortcut_request_builders_live_in_shortcut_requests_module():
    module = _load_plugin_module()

    assert module._profile_answer_question_shortcut.__module__.endswith(
        ".shortcut_requests"
    )
    assert module._relative_period_label.__module__.endswith(".shortcut_requests")


def test_card_shortcut_requests_live_in_card_module():
    module = _load_plugin_module()

    assert module._card_gtv_7d_request.__module__.endswith(".card_shortcut_requests")
    assert module._card_gtv_daily_request.__module__.endswith(".card_shortcut_requests")
    assert module._top_card_spender_7d_spend_breakdown_request.__module__.endswith(
        ".card_shortcut_requests"
    )


def test_commerce_shortcut_requests_live_in_commerce_module():
    module = _load_plugin_module()

    assert module._merchant_card_spend_7d_request.__module__.endswith(
        ".commerce_shortcut_requests"
    )
    assert module._swiggy_spend_trend_10d_request.__module__.endswith(
        ".commerce_shortcut_requests"
    )
    assert module._gym_milestone_avg_monthly_spend_3mo_request.__module__.endswith(
        ".commerce_shortcut_requests"
    )


def test_slack_formatters_live_in_slack_formatters_module():
    module = _load_plugin_module()

    assert module._card_gtv_7d_slack_text.__module__.endswith(".slack_formatters")
    assert module._format_inr.__module__.endswith(".slack_formatters")


def test_commerce_slack_formatters_live_in_commerce_module():
    module = _load_plugin_module()

    assert module._merchant_card_spend_7d_slack_text.__module__.endswith(
        ".commerce_slack_formatters"
    )
    assert module._swiggy_spend_trend_10d_slack_text.__module__.endswith(
        ".commerce_slack_formatters"
    )
    assert module._gym_milestone_avg_monthly_spend_3mo_slack_text.__module__.endswith(
        ".commerce_slack_formatters"
    )


def test_slack_payload_router_lives_in_payload_router_module():
    module = _load_plugin_module()

    assert module._profile_answer_question_payload.__module__.endswith(
        ".slack_payload_router"
    )


def test_pre_gateway_agent_handoff_strips_slack_context(monkeypatch):
    module = _load_plugin_module()
    ctx = _Ctx()
    calls = []

    def fake_runner(args):
        calls.append(args)
        return {
            "ok": True,
            "mode": "answer_question",
            "payload": {
                "ok": True,
                "route": "supabase_ad_hoc",
                "payload": {"slackText": "Swiggy answer"},
            },
            "hermes_direct_final_response": "Swiggy answer",
        }

    monkeypatch.setattr(module, "run_elixir_analytics_runner", fake_runner)
    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    result = hook(
        event=_slack_event(
            "[Thread context - prior messages in this thread (not yet in conversation history):]\n"
            "Ritik Madan: old question\n"
            "[End of thread context]\n\n"
            "which users spent on Swiggy this week?\n"
            "*Sent using* ChatGPT\n\n"
            "[Slack Block Kit payload for this message omitted]"
        ),
        gateway=SimpleNamespace(_is_user_authorized=lambda source: True),
        session_store=None,
    )

    assert result is not None
    assert result["action"] == "annotate"
    assert result["reason"] == "elixir_analytics_agent_runtime_handoff"
    assert "which users spent on Swiggy this week?" in result["text"]
    assert "Ritik Madan: old question" in result["context"]
    assert "*Sent using*" not in result["text"]
    assert calls == []


def test_pre_gateway_agent_handoff_allows_unauthorized_slack_user(monkeypatch):
    module = _load_plugin_module()
    ctx = _Ctx()
    calls = []

    monkeypatch.setattr(
        module,
        "run_elixir_analytics_runner",
        lambda args: calls.append(args) or {"ok": True},
    )
    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    result = hook(
        event=_slack_event("which users spent on Swiggy this week?", user_id="someone_else"),
        gateway=SimpleNamespace(_is_user_authorized=lambda source: False),
        session_store=None,
    )

    assert result == {"action": "allow"}
    assert calls == []


def test_pre_gateway_agent_handoff_keeps_model_required_questions_in_agent(monkeypatch):
    module = _load_plugin_module()
    ctx = _Ctx()

    monkeypatch.setattr(
        module,
        "run_elixir_analytics_runner",
        lambda args: {
            "ok": True,
            "mode": "answer_question",
            "payload": {"ok": False, "route": "requires_model_request"},
        },
    )
    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    result = hook(
        event=_slack_event("how have spends on swiggy evolved over last 10 days"),
        gateway=SimpleNamespace(_is_user_authorized=lambda source: True),
        session_store=None,
    )

    assert result is not None
    assert result["action"] == "annotate"
    assert result["reason"] == "elixir_analytics_agent_runtime_handoff"
    assert "how have spends on swiggy evolved over last 10 days" in result["text"]
    assert "analytics-capable Hermes profile" in result["context"]
    assert "This Slack analytics request" not in result["context"]
    assert "Call `elixir_analytics_runner`" in result["context"]
    assert "You do not need `skill_view`" in result["context"]


def test_pre_gateway_agent_handoff_annotates_ambiguous_active_users(monkeypatch):
    module = _load_plugin_module()
    ctx = _Ctx()
    calls = []

    monkeypatch.setattr(
        module,
        "run_elixir_analytics_runner",
        lambda args: calls.append(args) or {"ok": True},
    )
    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    result = hook(
        event=_slack_event("who was our most active user in last 7 days"),
        gateway=SimpleNamespace(_is_user_authorized=lambda source: True),
        session_store=None,
    )

    assert result is not None
    assert result["action"] == "annotate"
    assert result["reason"] == "elixir_analytics_agent_runtime_handoff"
    assert "who was our most active user in last 7 days" in result["text"]
    assert "active user definition is ambiguous" in result["context"]
    assert "Most card-active user" in result["context"]
    assert "Highest card spender" in result["context"]
    assert calls == []


def test_pre_gateway_agent_handoff_preserves_active_users_clarification_reply(monkeypatch):
    module = _load_plugin_module()
    ctx = _Ctx()
    calls = []

    def fake_runner(args):
        calls.append(args)
        return {
            "ok": True,
            "mode": "answer_question",
            "payload": {
                "ok": True,
                "route": "supabase_ad_hoc",
                "payload": {"slackText": "Highest spender answer"},
            },
            "hermes_direct_final_response": "Highest spender answer",
        }

    monkeypatch.setattr(module, "run_elixir_analytics_runner", fake_runner)
    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    result = hook(
        event=_slack_event(
            "[Thread context - prior messages in this thread (not yet in conversation history):]\n"
            "Ritik Madan: who was our most active user in last 7 days\n"
            "chandler: Which active user definition should I use for the last 7 days?\n"
            "[End of thread context]\n\n"
            "highest spender sorry"
        ),
        gateway=SimpleNamespace(_is_user_authorized=lambda source: True),
        session_store=None,
    )

    assert result is not None
    assert result["action"] == "annotate"
    assert result["reason"] == "elixir_analytics_agent_runtime_handoff"
    assert result["text"] == "highest spender sorry"
    assert "Call `elixir_analytics_runner`" in result["context"]
    assert "who was our most active user in last 7 days" in result["context"]
    assert calls == []


def test_pre_gateway_agent_handoff_preserves_spend_breakdown_followup(monkeypatch):
    module = _load_plugin_module()
    ctx = _Ctx()
    calls = []

    def fake_runner(args):
        calls.append(args)
        return {
            "ok": True,
            "mode": "answer_question",
            "payload": {
                "ok": True,
                "route": "supabase_ad_hoc",
                "payload": {"slackText": "Spend breakdown answer"},
            },
            "hermes_direct_final_response": "Spend breakdown answer",
        }

    monkeypatch.setattr(module, "run_elixir_analytics_runner", fake_runner)
    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    result = hook(
        event=_slack_event(
            "[Thread context - prior messages in this thread (not yet in conversation history):]\n"
            "Ritik Madan: who was our most active user in last 7 days\n"
            "chandler: Which active user definition should I use for the last 7 days?\n"
            "Ritik Madan: highest spender sorry\n"
            "chandler: Highest spender in the last 7 IST days was *Nagendra G*.\n"
            "[End of thread context]\n\n"
            "what did he spend on?"
        ),
        gateway=SimpleNamespace(_is_user_authorized=lambda source: True),
        session_store=None,
    )

    assert result is not None
    assert result["action"] == "annotate"
    assert result["reason"] == "elixir_analytics_agent_runtime_handoff"
    assert result["text"] == "what did he spend on?"
    assert "Highest spender in the last 7 IST days was *Nagendra G*." in result["context"]
    assert calls == []


def test_pre_gateway_agent_handoff_preserves_session_history_followup(monkeypatch):
    module = _load_plugin_module()
    ctx = _Ctx()
    calls = []

    def fake_runner(args):
        calls.append(args)
        return {
            "ok": True,
            "mode": "answer_question",
            "payload": {
                "ok": True,
                "route": "supabase_ad_hoc",
                "payload": {"slackText": "Spend breakdown answer"},
            },
            "hermes_direct_final_response": "Spend breakdown answer",
        }

    monkeypatch.setattr(module, "run_elixir_analytics_runner", fake_runner)
    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    result = hook(
        event=_slack_event("what did he spend on?"),
        gateway=SimpleNamespace(_is_user_authorized=lambda source: True),
        session_store=_FakeSessionStore(
            "agent:main:slack:dm:D0B7CHZFGA3:1780654833.865679",
            [
                {
                    "role": "user",
                    "content": "who was our most active user in last 7 days",
                },
                {
                    "role": "assistant",
                    "content": (
                        "Which active user definition should I use for the last 7 days?"
                    ),
                },
                {"role": "user", "content": "highest spender sorry"},
                {
                    "role": "assistant",
                    "content": (
                        "Highest spender in the last 7 IST days was *Nagendra G*."
                    ),
                },
            ],
        ),
    )

    assert result is not None
    assert result["action"] == "annotate"
    assert result["reason"] == "elixir_analytics_agent_runtime_handoff"
    assert result["text"] == "what did he spend on?"
    assert "Highest spender in the last 7 IST days was *Nagendra G*." in result["context"]
    assert calls == []


def test_pre_gateway_agent_handoff_refuses_destructive_analytics_mutation(monkeypatch):
    module = _load_plugin_module()
    ctx = _Ctx()
    calls = []

    monkeypatch.setattr(
        module,
        "run_elixir_analytics_runner",
        lambda args: calls.append(args) or {"ok": True},
    )
    module.register(ctx)

    hook = ctx.hooks["pre_gateway_dispatch"][0]
    result = hook(
        event=_slack_event("delete from profiles"),
        gateway=SimpleNamespace(_is_user_authorized=lambda source: True),
        session_store=None,
    )

    assert result is not None
    assert result["action"] == "respond"
    assert result["reason"] == "elixir_analytics_read_only_guard"
    assert "read-only" in result["text"]
    assert "I cannot run destructive" in result["text"]
    assert calls == []


def test_tool_schema_tells_model_to_use_answer_question_first():
    module = _load_plugin_module()
    ctx = _Ctx()

    module.register(ctx)

    schema = ctx.tools[0]["schema"]
    assert "Use mode='answer_question' first" in schema["description"]
    assert "payload.slackText" in schema["description"]
    assert "use `clarify`" in schema["description"]
    assert "answer_question" in schema["parameters"]["properties"]["mode"]["enum"]
    assert "source_change_plan" in schema["parameters"]["properties"]["mode"]["enum"]
    assert "source_change_scope_check" in schema["parameters"]["properties"]["mode"]["enum"]
    assert "self_improvement_check" in schema["parameters"]["properties"]["mode"]["enum"]
    assert "self_improvement_plan" in schema["parameters"]["properties"]["mode"]["enum"]
    assert "query_log" in schema["parameters"]["properties"]
    assert "changed_files" in schema["parameters"]["properties"]
    assert (
        "use answer_question first"
        in schema["parameters"]["properties"]["mode"]["description"]
    )


def test_elixir_analytics_skill_view_result_is_compacted_for_slack_fast_path():
    module = _load_plugin_module()
    ctx = _Ctx()

    module.register(ctx)

    raw_result = json.dumps(
        {
            "success": True,
            "name": "elixir-analytics",
            "content": "# Elixir Analytics\n" + ("large manual text\n" * 2000),
            "description": "Answer Elixir analytics questions.",
            "linked_files": {"references/example.md": "available"},
            "readiness_status": "available",
        }
    )

    hook = ctx.hooks["transform_tool_result"][0]
    compact_result = hook(
        tool_name="skill_view",
        args={"name": "elixir-analytics"},
        result=raw_result,
        task_id="",
        session_id="",
        tool_call_id="",
        duration_ms=1,
    )

    assert compact_result is not None
    payload = json.loads(compact_result)
    assert payload["success"] is True
    assert payload["name"] == "elixir-analytics"
    assert len(payload["content"]) < 3500
    assert "large manual text" not in payload["content"]
    assert "Mandatory Slack Fast Path" in payload["content"]
    assert "mode='answer_question'" in payload["content"]
    assert "what can Chandler do?" in payload["content"]
    assert "Ritik-only" in payload["content"]
    assert "source_change_plan" in payload["content"]
    assert "self_improvement_check" in payload["content"]
    assert payload["linked_files"] == {"references/example.md": "available"}


def test_skill_view_compaction_ignores_other_skills_and_linked_files():
    module = _load_plugin_module()

    assert (
        module._compact_skill_view_result(
            {"name": "github"},
            json.dumps({"success": True, "name": "github", "content": "unchanged"}),
        )
        is None
    )
    assert (
        module._compact_skill_view_result(
            {"name": "elixir-analytics", "file_path": "references/example.md"},
            json.dumps(
                {
                    "success": True,
                    "name": "elixir-analytics",
                    "content": "linked file content",
                }
            ),
        )
        is None
    )


def test_plan_mode_invokes_question_planner_on_stdin(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "route": "saved_topic"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "plan", "question": "show GTV last 30 days by week"}
    )

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/plan-analytics-question.ts",
    ]
    assert kwargs["input"] == "show GTV last 30 days by week"


def test_ad_hoc_mode_sends_request_json_on_stdin_and_defaults_base_url(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "rows": [{"user": "Ada"}]}))

    monkeypatch.delenv("ANALYTICS_BASE_URL", raising=False)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "supabase_ad_hoc",
            "request": {"question": "which users spent on Swiggy this week?"},
            "max_rows": 25,
        }
    )

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert json.loads(kwargs["input"]) == {
        "question": "which users spent on Swiggy this week?"
    }
    assert kwargs["env"]["ANALYTICS_BASE_URL"] == "https://analytics.joinelixir.club"


def test_answer_question_mode_invokes_shortcut_runner_on_stdin(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "route": "supabase_ad_hoc"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "which users had failed onboarding attempts this week?",
            "max_rows": 25,
        }
    )

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-analytics-question.ts",
        "--max-rows",
        "25",
    ]
    assert kwargs["input"] == "which users had failed onboarding attempts this week?"


def test_answer_question_mode_returns_compact_slack_handoff(monkeypatch):
    module = _load_plugin_module()
    payload = {
        "ok": True,
        "route": "supabase_ad_hoc",
        "shortcut": "swiggy_users_this_week",
        "payload": {
            "ok": True,
            "title": "Users who spent on Swiggy this week",
            "resultType": "users",
            "rowCount": 15,
            "truncated": False,
            "dashboardUrlPath": "/query?payload=compact",
            "dashboardUrl": "https://analytics.joinelixir.club/query?payload=compact",
            "slackText": (
                "Users who spent on Swiggy this week\n"
                "Rows: 15\n"
                + "\n".join(
                    f"{index}. user_name: User {index}, spend: {index * 100}"
                    for index in range(1, 80)
                )
                + "\nDashboard: <https://analytics.joinelixir.club/query?payload=compact|View full table>"
            ),
            "metadata": {
                "resultType": "users",
                "sql": "select * from transactions",
                "assumptions": "This week means India business week-to-date.",
                "caveats": "Includes successful card spend only.",
            },
            "rows": [
                {
                    "email": "ada@example.com",
                    "phone": "+910000000000",
                    "gross_spend_inr": 420,
                }
            ],
            "logEntry": "## Query #99\nSQL: select * from transactions",
        },
    }

    def fake_run(command, **kwargs):
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "which users had failed onboarding attempts this week?",
        }
    )

    assert result["ok"] is True
    assert result["payload"]["route"] == "supabase_ad_hoc"
    assert result["hermes_direct_final_response"].startswith(
        "Users who spent on Swiggy this week"
    )
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?payload=compact|"
        "View full table>"
        in result["hermes_direct_final_response"]
    )
    assert "More rows in the dashboard." in result["hermes_direct_final_response"]
    assert len(result["hermes_direct_final_response"]) < 5000
    compact_payload = result["payload"]["payload"]
    assert compact_payload["slackText"].startswith("Users who spent on Swiggy")
    assert compact_payload["dashboardUrl"].startswith("https://analytics.joinelixir.club")
    assert "dashboardUrlPath" not in compact_payload
    assert compact_payload["rowCount"] == 15
    assert len(compact_payload["slackText"]) < 2400
    assert "Dashboard:" not in compact_payload["slackText"]
    assert compact_payload["slackDashboardLine"] == (
        "Dashboard: <https://analytics.joinelixir.club/query?payload=compact|"
        "View full table>"
    )
    assert "Slack handoff truncated" in compact_payload["slackText"]
    assert compact_payload["metadata"] == {
        "resultType": "users",
        "assumptions": "This week means India business week-to-date.",
        "caveats": "Includes successful card spend only.",
    }

    serialized = json.dumps(result, ensure_ascii=False)
    assert "select * from transactions" not in serialized
    assert "ada@example.com" not in serialized
    assert "+910000000000" not in serialized
    assert "logEntry" not in serialized


def test_answer_question_mode_clarification_requires_clarify_tool(monkeypatch):
    module = _load_plugin_module()
    payload = {
        "ok": True,
        "route": "clarify",
        "payload": {
            "ok": False,
            "requiresClarification": True,
            "clarificationQuestion": (
                "Which active user definition should I use?"
            ),
            "choices": [
                "card active",
                "app active",
                "combined active",
            ],
        },
    }

    def fake_run(command, **kwargs):
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "active users this week",
        }
    )

    assert result["ok"] is True
    assert "hermes_direct_final_response" not in result
    assert result["hermes_agent_instruction"].startswith("Call `clarify`")
    assert result["payload"]["payload"]["clarificationQuestion"] == (
        "Which active user definition should I use?"
    )
    assert result["payload"]["payload"]["choices"] == [
        "card active",
        "app active",
        "combined active",
    ]


def test_answer_question_mode_handles_highest_spender_last_7_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "user_name": "Nagendra G",
                "user_id": "user-1",
                "txn_count": 32,
                "gross_spend_inr": 130427.41,
                "avg_txn_value_inr": 4075.856,
                "first_card_txn_at": "2026-05-30 09:55:38",
                "last_card_txn_at": "2026-06-05 15:24:26",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=top_7d",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=top_7d",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "highest spender in last 7 days"}
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "top_card_spender_7d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["question"] == "highest spender in last 7 days"
    assert request["resultType"] == "users"
    assert request["metricIds"] == ["gtv", "active_spender"]
    assert "classified_transactions" in request["sql"]
    assert "is_card_spend = true" in request["sql"]
    assert "is_reward_reconciliation = false" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Highest spender in the last 7 IST days was *Nagendra G*."
    )
    assert "GTV:* ₹130,427" in result["hermes_direct_final_response"]
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?result=top_7d|"
        "Open visualization>"
        in result["hermes_direct_final_response"]
    )


def test_answer_question_mode_handles_top_card_spenders_last_7_days_as_ranking(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "user_name": "Nagendra G",
                "user_id": "user-1",
                "txn_count": 32,
                "gross_spend_inr": 130427.41,
                "avg_txn_value_inr": 4075.856,
            },
            {
                "user_name": "Ada Lovelace",
                "user_id": "user-2",
                "txn_count": 12,
                "gross_spend_inr": 62420.0,
                "avg_txn_value_inr": 5201.667,
            },
        ],
        "rowCount": 2,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=top_spenders_7d",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=top_spenders_7d",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "show top users by card spend last 7 days",
        }
    )

    request = json.loads(calls[0][1]["input"])
    direct = result["hermes_direct_final_response"]
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "top_card_spenders_7d"
    assert request["resultType"] == "users"
    assert request["metricIds"] == ["gtv", "active_spender"]
    assert "order by gross_spend_inr desc" in request["sql"]
    assert direct.startswith("Top card spenders in the last 7 IST days:")
    assert "| # | User | GTV | Txns | Avg txn |" in direct
    assert "| 1 | Nagendra G | ₹130,427 | 32 | ₹4,076 |" in direct
    assert "| 2 | Ada Lovelace | ₹62,420 | 12 | ₹5,202 |" in direct
    assert "Highest spender in the last 7 IST days" not in direct


def test_answer_question_mode_handles_top_card_spender_today(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "user_name": "Nagendra G",
                "user_id": "user-1",
                "txn_count": 8,
                "gross_spend_inr": 18420.55,
                "avg_txn_value_inr": 2302.569,
                "first_card_txn_at": "2026-06-05 09:55:38",
                "last_card_txn_at": "2026-06-05 15:24:26",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "who spent the most today?"}
    )

    request = json.loads(calls[0][1]["input"])
    direct = result["hermes_direct_final_response"]
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "top_card_spender_today"
    assert request["resultType"] == "users"
    assert "including today-to-date" in request["assumptions"]
    assert "is_card_spend = true" in request["sql"]
    assert direct.startswith("Highest card spender today in IST is *Nagendra G*.")
    assert "GTV:* ₹18,421" in direct


def test_answer_question_mode_handles_top_card_spenders_this_week(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "user_name": "Nagendra G",
                "user_id": "user-1",
                "txn_count": 36,
                "gross_spend_inr": 160400.0,
                "avg_txn_value_inr": 4455.556,
            },
            {
                "user_name": "Ada Lovelace",
                "user_id": "user-2",
                "txn_count": 22,
                "gross_spend_inr": 88200.0,
                "avg_txn_value_inr": 4009.091,
            },
        ],
        "rowCount": 2,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "which users spent the most this week"}
    )

    request = json.loads(calls[0][1]["input"])
    direct = result["hermes_direct_final_response"]
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "top_card_spenders_this_week"
    assert request["resultType"] == "users"
    assert "week-to-date" in request["assumptions"]
    assert direct.startswith("Top card spenders this week in IST:")
    assert "| 1 | Nagendra G | ₹160,400 | 36 | ₹4,456 |" in direct
    assert "| 2 | Ada Lovelace | ₹88,200 | 22 | ₹4,009 |" in direct


def test_answer_question_mode_handles_highest_spender_spend_breakdown(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "top_user_name": "Nagendra G",
                "merchant_name": "AIR INDIA",
                "description": "AIR INDIA BOOKING",
                "txn_count": 2,
                "gross_spend_inr": 85750.25,
                "latest_card_txn_at": "2026-06-05 15:24:26",
            },
            {
                "top_user_name": "Nagendra G",
                "merchant_name": "SWIGGY",
                "description": "SWIGGY INSTAMART",
                "txn_count": 3,
                "gross_spend_inr": 4260.2,
                "latest_card_txn_at": "2026-06-04 20:04:11",
            },
        ],
        "rowCount": 2,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=top_spender_breakdown",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=top_spender_breakdown",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "what did the highest spender spend on in last 7 days",
        }
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "top_card_spender_7d_spend_breakdown"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["question"] == "what did the highest spender spend on in last 7 days"
    assert request["resultType"] == "breakdown"
    assert request["metricIds"] == ["gtv", "active_spender"]
    assert "ranked_spenders" in request["sql"]
    assert "top_spender" in request["sql"]
    assert "merchant_name" in request["sql"]
    assert "description" in request["sql"]
    assert "is_card_spend = true" in request["sql"]
    assert "is_reward_reconciliation = false" in request["sql"]
    assert "email" not in request["sql"].lower()
    assert "phone" not in request["sql"].lower()
    assert result["hermes_direct_final_response"].startswith(
        "Nagendra G's card spend in the last 7 IST days was concentrated in:"
    )
    assert "| Merchant | Description | GTV | Txns | Latest |" in result[
        "hermes_direct_final_response"
    ]
    assert "| AIR INDIA | AIR INDIA BOOKING | ₹85,750 | 2 | 2026-06-05 15:24:26 |" in result[
        "hermes_direct_final_response"
    ]
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?result=top_spender_breakdown|"
        "Open visualization>"
        in result["hermes_direct_final_response"]
    )


def test_answer_question_mode_handles_top_merchants_card_spend_last_7_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "merchant_name": "PHONEPE PRIVATE LTD",
                "txn_count": 42,
                "user_count": 18,
                "gross_spend_inr": 95500.8,
                "latest_card_txn_at": "2026-06-05 15:24:26",
            },
            {
                "merchant_name": "SWIGGY",
                "txn_count": 12,
                "user_count": 9,
                "gross_spend_inr": 8400.2,
                "latest_card_txn_at": "2026-06-04 20:04:11",
            },
        ],
        "rowCount": 2,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=top_merchants_7d",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=top_merchants_7d",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "show top merchants by card spend last 7 days",
        }
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "top_merchants_card_spend_7d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["question"] == "show top merchants by card spend last 7 days"
    assert request["resultType"] == "merchants"
    assert request["metricIds"] == ["gtv"]
    assert "merchant_name" in request["sql"]
    assert "count(distinct ct.user_id)" in request["sql"]
    assert "is_card_spend = true" in request["sql"]
    assert "is_reward_reconciliation = false" in request["sql"]
    assert "email" not in request["sql"].lower()
    assert "phone" not in request["sql"].lower()
    assert result["hermes_direct_final_response"].startswith(
        "Top merchants by card spend in the last 7 IST days:"
    )
    assert "| Merchant | GTV | Txns | Users | Latest |" in result[
        "hermes_direct_final_response"
    ]
    assert "| PHONEPE PRIVATE LTD | ₹95,501 | 42 | 18 | 2026-06-05 15:24:26 |" in result[
        "hermes_direct_final_response"
    ]
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?result=top_merchants_7d|"
        "Open visualization>"
        in result["hermes_direct_final_response"]
    )


def test_answer_question_mode_handles_swiggy_spend_trend_last_10_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "business_date": "2026-05-27",
                "swiggy_gtv_inr": 2046.2,
                "txn_count": 7,
                "user_count": 7,
            },
            {
                "business_date": "2026-05-28",
                "swiggy_gtv_inr": 1640.4,
                "txn_count": 7,
                "user_count": 5,
            },
            {
                "business_date": "2026-05-29",
                "swiggy_gtv_inr": 7106.4,
                "txn_count": 8,
                "user_count": 6,
            },
        ],
        "rowCount": 3,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=swiggy_trend",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=swiggy_trend",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "how have spends on swiggy evolved over last 10 days",
        }
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "swiggy_spend_trend_10d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["question"] == "how have spends on swiggy evolved over last 10 days"
    assert request["resultType"] == "trend"
    assert request["metricIds"] == ["gtv"]
    assert "generate_series" in request["sql"]
    assert "merchant_name" in request["sql"]
    assert "description" in request["sql"]
    assert "ilike '%swiggy%'" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Swiggy card spend over the last 10 IST days"
    )
    assert "Peak day was 2026-05-29 at ₹7,106" in result[
        "hermes_direct_final_response"
    ]
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?result=swiggy_trend|"
        "Open visualization>"
        in result["hermes_direct_final_response"]
    )


def test_answer_question_mode_handles_merchant_card_spend_last_7_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "merchant_query": "zepto",
                "gtv": 46821.25,
                "transactions": 19,
                "users": 11,
                "source_freshness": "2026-06-05 15:24:26",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=zepto_spend",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=zepto_spend",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "how much did users spend on zepto last 7 days?",
        }
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "merchant_card_spend_7d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["question"] == "how much did users spend on zepto last 7 days?"
    assert request["resultType"] == "kpi"
    assert request["metricIds"] == ["gtv"]
    assert request["merchantQuery"] == "zepto"
    assert "merchant_name" in request["sql"]
    assert "description" in request["sql"]
    assert "ilike '%zepto%'" in request["sql"]
    assert "is_card_spend = true" in request["sql"]
    assert "is_reward_reconciliation = false" in request["sql"]
    assert "email" not in request["sql"].lower()
    assert "phone" not in request["sql"].lower()
    assert result["hermes_direct_final_response"].startswith(
        "Zepto card spend in the last 7 IST days was *₹46,821*."
    )
    assert "Transactions:* 19" in result["hermes_direct_final_response"]
    assert "Card users:* 11" in result["hermes_direct_final_response"]
    assert "matched merchant_name or description containing `zepto`" in result[
        "hermes_direct_final_response"
    ]
    assert "Dashboard:" not in result["hermes_direct_final_response"]


def test_answer_question_mode_handles_merchant_users_this_week(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "merchant_query": "zepto",
                "user_name": "Asha K",
                "user_id": "user-1",
                "txn_count": 4,
                "gross_spend_inr": 8120.5,
                "last_card_txn_at": "2026-06-05 11:03:44",
            },
            {
                "merchant_query": "zepto",
                "user_name": "Dev P",
                "user_id": "user-2",
                "txn_count": 2,
                "gross_spend_inr": 2399.1,
                "last_card_txn_at": "2026-06-04 20:15:01",
            },
        ],
        "rowCount": 2,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=zepto_users",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=zepto_users",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "which users spent on zepto this week?",
        }
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "merchant_users_this_week"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["question"] == "which users spent on zepto this week?"
    assert request["resultType"] == "users"
    assert request["metricIds"] == ["gtv"]
    assert request["merchantQuery"] == "zepto"
    assert "merchant_name" in request["sql"]
    assert "description" in request["sql"]
    assert "ilike '%zepto%'" in request["sql"]
    assert "group by ct.user_id" in request["sql"]
    assert "email" not in request["sql"].lower()
    assert "phone" not in request["sql"].lower()
    assert "week-to-date" in request["assumptions"]
    assert result["hermes_direct_final_response"].startswith(
        "Users who spent on Zepto this week:"
    )
    assert "| User | GTV | Txns | Latest |" in result["hermes_direct_final_response"]
    assert "| Asha K | ₹8,120 | 4 | 2026-06-05 11:03:44 |" in result[
        "hermes_direct_final_response"
    ]
    assert "matched merchant_name or description containing `zepto`" in result[
        "hermes_direct_final_response"
    ]
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?result=zepto_users|"
        "Open visualization>"
        in result["hermes_direct_final_response"]
    )


def test_answer_question_mode_handles_top_merchants_card_spend_today(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "merchant_name": "SWIGGY",
                "txn_count": 18,
                "user_count": 14,
                "gross_spend_inr": 55210.75,
                "latest_card_txn_at": "2026-06-05 15:24:26",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=top_merchants_today",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=top_merchants_today",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "top merchants by card spend today",
        }
    )

    request = json.loads(calls[0][1]["input"])
    direct = result["hermes_direct_final_response"]
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "top_merchants_card_spend_today"
    assert request["resultType"] == "merchants"
    assert request["metricIds"] == ["gtv"]
    assert "including today-to-date" in request["assumptions"]
    assert "group by coalesce(nullif(ct.merchant_name" in request["sql"]
    assert "count(distinct ct.user_id)" in request["sql"]
    assert direct.startswith("Top merchants by card spend today in IST:")
    assert "| SWIGGY | ₹55,211 | 18 | 14 | 2026-06-05 15:24:26 |" in direct


def test_answer_question_mode_handles_merchant_card_spend_this_week(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "merchant_query": "swiggy",
                "gtv": 81220.4,
                "transactions": 42,
                "users": 31,
                "source_freshness": "2026-06-05 15:24:26",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "Swiggy GTV this week"}
    )

    request = json.loads(calls[0][1]["input"])
    direct = result["hermes_direct_final_response"]
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "merchant_card_spend_this_week"
    assert request["resultType"] == "kpi"
    assert request["merchantQuery"] == "swiggy"
    assert "ilike '%swiggy%'" in request["sql"]
    assert "week-to-date" in request["assumptions"]
    assert direct.startswith("Swiggy card spend this week in IST is *₹81,220*.")


def test_answer_question_mode_handles_swiggy_users_this_week(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "merchant_query": "swiggy",
                "user_name": "Asha K",
                "user_id": "user-1",
                "txn_count": 4,
                "gross_spend_inr": 8120.5,
                "last_card_txn_at": "2026-06-05 11:03:44",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "which users spent on Swiggy this week?"}
    )

    request = json.loads(calls[0][1]["input"])
    direct = result["hermes_direct_final_response"]
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "merchant_users_this_week"
    assert request["merchantQuery"] == "swiggy"
    assert "ilike '%swiggy%'" in request["sql"]
    assert direct.startswith("Users who spent on Swiggy this week:")
    assert "| Asha K | ₹8,120 | 4 | 2026-06-05 11:03:44 |" in direct


def test_answer_question_mode_handles_merchant_users_today(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "merchant_query": "swiggy",
                "user_name": "Asha K",
                "user_id": "user-1",
                "txn_count": 3,
                "gross_spend_inr": 5120.5,
                "last_card_txn_at": "2026-06-05 11:03:44",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "which users spent on Swiggy today?"}
    )

    request = json.loads(calls[0][1]["input"])
    direct = result["hermes_direct_final_response"]
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "merchant_users_today"
    assert request["merchantQuery"] == "swiggy"
    assert "including today-to-date" in request["assumptions"]
    assert "ilike '%swiggy%'" in request["sql"]
    assert direct.startswith("Users who spent on Swiggy today in IST:")
    assert "| Asha K | ₹5,120 | 3 | 2026-06-05 11:03:44 |" in direct


def test_answer_question_mode_handles_card_gtv_last_7_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "gtv": 928183.22,
                "transactions": 421,
                "users": 144,
                "source_freshness": "2026-06-05 03:42:49",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "what was GTV for last 7 days?"}
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_gtv_7d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["resultType"] == "kpi"
    assert request["metricIds"] == ["gtv"]
    assert "classified_transactions" in request["sql"]
    assert "is_card_spend = true" in request["sql"]
    assert "is_reward_reconciliation = false" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Card GTV for the last 7 completed IST days was *₹928,183*."
    )
    assert "Dashboard:" not in result["hermes_direct_final_response"]


def test_answer_question_mode_handles_card_gtv_last_12_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "gtv": 2242224.39,
                "transactions": 1014,
                "users": 243,
                "source_freshness": "2026-06-08 18:10:13",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "gtv over last 12 days"}
    )

    request = json.loads(calls[0][1]["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_gtv_last_12d"
    assert request["resultType"] == "kpi"
    assert request["metricIds"] == ["gtv"]
    assert "last 12 completed" in request["interpretedDefinition"]
    assert "classified_transactions" in request["sql"]
    assert "is_card_spend = true" in request["sql"]
    assert "is_reward_reconciliation = false" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Card GTV for the last 12 completed IST days was *₹2,242,224*."
    )


def test_answer_question_mode_handles_gym_milestone_average_monthly_spend(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "period": "overall_3mo_avg",
                "month_start": None,
                "gym_users": 103,
                "total_gtv": 5267234.61,
                "transactions": 1866,
                "spending_users": None,
                "avg_monthly_spend_per_gym_user": 17046.0666990291,
                "avg_monthly_spend_per_spending_user": 36567.0114424392,
                "source_freshness": "2026-05-31 17:38:06.159+00",
            },
            {
                "period": "2026-03",
                "month_start": "2026-03-01T00:00:00.000Z",
                "gym_users": 103,
                "total_gtv": 1542908.78,
                "transactions": 573,
                "spending_users": 46,
                "avg_monthly_spend_per_gym_user": 14979.6968932039,
                "avg_monthly_spend_per_spending_user": 33541.4952173913,
                "source_freshness": "2026-03-31 18:06:45.915+00",
            },
            {
                "period": "2026-04",
                "month_start": "2026-04-01T00:00:00.000Z",
                "gym_users": 103,
                "total_gtv": 2051138.31,
                "transactions": 660,
                "spending_users": 46,
                "avg_monthly_spend_per_gym_user": 19913.9641747573,
                "avg_monthly_spend_per_spending_user": 44589.9632608696,
                "source_freshness": "2026-04-30 18:10:13.032+00",
            },
            {
                "period": "2026-05",
                "month_start": "2026-05-01T00:00:00.000Z",
                "gym_users": 103,
                "total_gtv": 1673187.52,
                "transactions": 633,
                "spending_users": 53,
                "avg_monthly_spend_per_gym_user": 16244.5390291262,
                "avg_monthly_spend_per_spending_user": 31569.5758490566,
                "source_freshness": "2026-05-31 17:38:06.159+00",
            },
        ],
        "rowCount": 4,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "what is the average monthly spend of gym milestone users over last 3 months?",
        }
    )

    request = json.loads(calls[0][1]["input"])
    direct = result["hermes_direct_final_response"]
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "gym_milestone_avg_monthly_spend_3mo"
    assert request["resultType"] == "timeseries"
    assert request["metricIds"] == ["gym_milestone_users", "gtv"]
    assert request["sources"] == [
        "milestone_program_instances",
        "customer_vouchers",
        "profiles",
        "transactions",
        "marketplace_order",
    ]
    assert "generate_series" in request["sql"]
    assert "mpi.status = 'active'" in request["sql"]
    assert "avg_monthly_spend_per_gym_user" in request["sql"]
    assert direct.startswith(
        "For current active gym milestone users, average monthly card spend "
        "over the last 3 completed months was:"
    )
    assert "*₹17,046.07* per gym milestone user / month" in direct
    assert "| 2026-03 | 103 | 46 | ₹1,542,909 | 573 | ₹14,979.70 | ₹33,541.50 |" in direct


def test_answer_question_mode_handles_card_gtv_today(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "gtv": 145507.2,
                "transactions": 73,
                "users": 31,
                "source_freshness": "2026-06-05 15:24:26",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "what was GTV today?"}
    )

    request = json.loads(calls[0][1]["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_gtv_today"
    assert request["resultType"] == "kpi"
    assert request["metricIds"] == ["gtv"]
    assert "including today-to-date" in request["assumptions"]
    assert "is_card_spend = true" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Card GTV today in IST is *₹145,507*."
    )


def test_answer_question_mode_handles_card_spend_yesterday(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "gtv": 118002.9,
                "transactions": 61,
                "users": 28,
                "source_freshness": "2026-06-04 23:59:59",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "what was card spend yesterday?"}
    )

    request = json.loads(calls[0][1]["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_gtv_yesterday"
    assert request["resultType"] == "kpi"
    assert request["metricIds"] == ["gtv"]
    assert "completed yesterday" in request["assumptions"]
    assert result["hermes_direct_final_response"].startswith(
        "Card GTV yesterday in IST was *₹118,003*."
    )


def test_answer_question_mode_handles_card_gtv_this_week(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "gtv": 702001,
                "transactions": 320,
                "users": 99,
                "source_freshness": "2026-06-05 15:24:26",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "card GTV this week"}
    )

    request = json.loads(calls[0][1]["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_gtv_this_week"
    assert request["resultType"] == "kpi"
    assert request["metricIds"] == ["gtv"]
    assert "week-to-date" in request["assumptions"]
    assert result["hermes_direct_final_response"].startswith(
        "Card GTV this week in IST is *₹702,001*."
    )


def test_answer_question_mode_handles_daily_card_gtv_last_7_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "business_date": "2026-05-30",
                "gtv": 120000.4,
                "transactions": 51,
                "users": 24,
                "source_freshness": "2026-05-30 23:59:59",
            },
            {
                "business_date": "2026-05-31",
                "gtv": 99000.2,
                "transactions": 44,
                "users": 21,
                "source_freshness": "2026-05-31 23:59:59",
            },
        ],
        "rowCount": 2,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=daily_gtv_7d",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=daily_gtv_7d",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "show GTV by day last 7 days"}
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_gtv_daily_7d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["question"] == "show GTV by day last 7 days"
    assert request["resultType"] == "trend"
    assert request["metricIds"] == ["gtv"]
    assert "generate_series" in request["sql"]
    assert "business_date" in request["sql"]
    assert "is_card_spend = true" in request["sql"]
    assert "is_reward_reconciliation = false" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Daily card GTV over the last 7 IST days"
    )
    assert "| Date | GTV | Txns | Users | DoD |" in result[
        "hermes_direct_final_response"
    ]
    assert "| 2026-05-30 | ₹120,000 | 51 | 24 | - |" in result[
        "hermes_direct_final_response"
    ]
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?result=daily_gtv_7d|"
        "Open visualization>"
        in result["hermes_direct_final_response"]
    )


def test_answer_question_mode_handles_daily_card_gtv_last_30_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "business_date": "2026-05-07",
                "gtv": 50000,
                "transactions": 20,
                "users": 12,
                "source_freshness": "2026-05-07 23:59:59",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=daily_gtv_30d",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=daily_gtv_30d",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "show daily GTV last 30 days"}
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_gtv_daily_30d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "30",
    ]
    assert request["resultType"] == "trend"
    assert request["metricIds"] == ["gtv"]
    assert "generate_series" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Daily card GTV over the last 30 IST days"
    )


def test_answer_question_mode_handles_card_transaction_count_last_7_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "transactions": 421,
                "gtv": 928183.22,
                "users": 144,
                "source_freshness": "2026-06-05 03:42:49",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=card_txn_count_7d",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=card_txn_count_7d",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "what were card transaction counts last 7 days?",
        }
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_transaction_count_7d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["resultType"] == "kpi"
    assert request["metricIds"] == ["card_transactions", "gtv"]
    assert "count(*)::int as transactions" in request["sql"]
    assert "classified_transactions" in request["sql"]
    assert "is_card_spend = true" in request["sql"]
    assert "is_reward_reconciliation = false" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Card transaction count for the last 7 completed IST days was *421*."
    )
    assert "GTV:* ₹928,183" in result["hermes_direct_final_response"]
    assert "Dashboard:" not in result["hermes_direct_final_response"]


def test_answer_question_mode_handles_card_transaction_count_today(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "transactions": 73,
                "gtv": 145507.2,
                "users": 31,
                "source_freshness": "2026-06-05 15:24:26",
            }
        ],
        "rowCount": 1,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": None,
        "dashboardUrl": None,
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "how many card transactions today?"}
    )

    request = json.loads(calls[0][1]["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_transaction_count_today"
    assert request["resultType"] == "kpi"
    assert request["metricIds"] == ["card_transactions", "gtv"]
    assert "including today-to-date" in request["assumptions"]
    assert result["hermes_direct_final_response"].startswith(
        "Card transaction count today in IST is *73*."
    )


def test_answer_question_mode_handles_weekly_card_gtv_last_30_days(monkeypatch):
    module = _load_plugin_module()
    calls = []
    payload = {
        "ok": True,
        "dryRun": False,
        "rows": [
            {
                "week_start": "2026-05-04",
                "gtv": 102030.4,
                "transactions": 45,
                "users": 28,
                "source_freshness": "2026-05-10 23:59:59",
            },
            {
                "week_start": "2026-05-11",
                "gtv": 203040.5,
                "transactions": 67,
                "users": 33,
                "source_freshness": "2026-05-17 23:59:59",
            },
        ],
        "rowCount": 2,
        "truncated": False,
        "maxRows": 25,
        "metadata": {},
        "dashboardUrlPath": "/query?result=weekly_gtv",
        "dashboardUrl": "https://analytics.joinelixir.club/query?result=weekly_gtv",
        "logEntry": "## Query #0",
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "answer_question", "question": "show GTV last 30 days by week"}
    )

    command, kwargs = calls[0]
    request = json.loads(kwargs["input"])
    assert result["ok"] is True
    assert result["payload"]["shortcut"] == "card_gtv_weekly_30d"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
    ]
    assert request["resultType"] == "trend"
    assert request["metricIds"] == ["gtv"]
    assert "date_trunc('week'" in request["sql"]
    assert result["hermes_direct_final_response"].startswith(
        "Weekly card GTV over the last 30 completed IST days"
    )
    assert "| Week | GTV | Txns | Users |" in result["hermes_direct_final_response"]
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?result=weekly_gtv|"
        "Open visualization>"
        in result["hermes_direct_final_response"]
    )


def test_answer_question_mode_labels_saved_topic_dashboard_links(monkeypatch):
    module = _load_plugin_module()
    payload = {
        "ok": True,
        "route": "saved_topic",
        "shortcut": "card_gtv_weekly_30d",
        "payload": {
            "ok": True,
            "topicId": "card-gtv-weekly",
            "title": "Weekly Card GTV",
            "rowCount": 5,
            "dashboardUrlPath": "/query?topic=card-gtv-weekly&range=30d",
            "dashboardUrl": (
                "https://analytics.joinelixir.club/query?"
                "topic=card-gtv-weekly&range=30d"
            ),
            "slackText": (
                "Weekly Card GTV\n"
                "Rows: 5\n"
                "1. week_start: 2026-05-04, gtv: 557638.14\n"
                "Dashboard: <https://analytics.joinelixir.club/query?"
                "topic=card-gtv-weekly&range=30d|View trend>"
            ),
        },
    }

    compact_payload = module._compact_answer_question_payload(payload)
    direct_final = module._direct_final_response_for_answer_payload(compact_payload)

    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?"
        "topic=card-gtv-weekly&range=30d|View trend>"
        in direct_final
    )
    assert (
        "Dashboard: https://analytics.joinelixir.club/query?"
        "topic=card-gtv-weekly&range=30d"
        not in direct_final
    )


def test_direct_final_compactor_keeps_whole_table_lines_when_truncated():
    module = _load_plugin_module()
    text = (
        "Nagendra G's card spend in the last 7 IST days was concentrated in:\n"
        "Total shown: ₹130,427 across 32 txns.\n\n"
        "| Merchant | Description | GTV | Txns | Latest |\n"
        "|---|---|---:|---:|---|\n"
        "| PHONEPE PRIVATE LTD MUMBAI IN | ECOM transaction at PHONEPE PRIVATE LTD MUMBAI IN | ₹63,997 | 20 | 2026-06-04 16:20:03 |\n"
        "| Freecharge Payment Techno122002 IN | ECOM transaction at Freecharge Payment Techno122002 IN | ₹27,408 | 3 | 2026-05-31 06:45:17 |\n"
    )

    compact = module._compact_direct_final_slack_text(text, limit=320)

    assert compact.endswith("More rows in the dashboard.")
    table_lines = [line for line in compact.splitlines() if line.startswith("|")]
    assert all(line.endswith("|") for line in table_lines)

    default_compact = module._compact_direct_final_slack_text(text)
    assert "PHONEPE PRIVATE LTD" in default_compact
    assert "Freecharge Payment" in default_compact
    default_table_lines = [
        line for line in default_compact.splitlines() if line.startswith("|")
    ]
    assert all(line.endswith("|") for line in default_table_lines)


def test_answer_question_mode_uses_runner_dashboard_label_when_present(monkeypatch):
    module = _load_plugin_module()
    payload = {
        "ok": True,
        "route": "supabase_ad_hoc",
        "shortcut": "swiggy_users_this_week",
        "payload": {
            "ok": True,
            "rowCount": 1,
            "truncated": False,
            "dashboardUrlPath": "/query?result=result_12345678",
            "dashboardUrl": (
                "https://analytics.joinelixir.club/query?result=result_12345678"
            ),
            "slackText": (
                "Swiggy users this week\n"
                "Rows: 1\n"
                "1. user_name: Ada Lovelace, gross_spend_inr: 420\n"
                "Dashboard: <https://analytics.joinelixir.club/query?"
                "result=result_12345678|View full table>"
            ),
            "metadata": {
                "resultType": "users",
                "assumptions": "This week means India business week-to-date.",
                "caveats": "Includes successful card spend only.",
            },
            "rows": [{"user_name": "Ada Lovelace", "gross_spend_inr": 420}],
        },
    }

    def fake_run(command, **kwargs):
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "which users had failed onboarding attempts this week?",
        }
    )

    assert result["ok"] is True
    assert (
        "Dashboard: <https://analytics.joinelixir.club/query?"
        "result=result_12345678|View full table>"
        in result["hermes_direct_final_response"]
    )
    assert "result=result_12345678|Open dashboard>" not in result[
        "hermes_direct_final_response"
    ]


def test_answer_question_mode_does_not_append_dashboard_when_slack_text_omits_it(monkeypatch):
    module = _load_plugin_module()
    payload = {
        "ok": True,
        "route": "saved_topic",
        "shortcut": "card_gtv_7d",
        "payload": {
            "ok": True,
            "rowCount": 1,
            "truncated": False,
            "dashboardUrlPath": "/query?topic=card-gtv&range=7d",
            "dashboardUrl": (
                "https://analytics.joinelixir.club/query?"
                "topic=card-gtv&range=7d"
            ),
            "slackText": (
                "*Card GTV*\n"
                "Rows: 1\n"
                "1. gtv: 120000, transactions: 42, users: 19\n"
                "Window: 2026-05-28 to 2026-06-04 (Asia/Kolkata)\n"
                "Working assumptions: Completed India business window.\n"
                "Fine print: Wallet loads excluded."
            ),
            "metadata": {
                "resultType": "kpi",
                "assumptions": "Completed India business window.",
                "caveats": "Wallet loads excluded.",
            },
            "rows": [{"gtv": 120000, "transactions": 42, "users": 19}],
        },
    }

    def fake_run(command, **kwargs):
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "what was GTV for last 7 days?",
        }
    )

    assert result["ok"] is True
    assert "Dashboard:" not in result["hermes_direct_final_response"]


def test_runner_logs_safe_route_summary_without_rows_or_sql(monkeypatch, caplog):
    module = _load_plugin_module()
    row = {"email": "ada@example.com", "gross_spend_inr": 420}
    payload = {
        "ok": True,
        "route": "supabase_ad_hoc",
        "shortcut": "swiggy_users_this_week",
        "payload": {
            "ok": True,
            "rowCount": 1,
            "truncated": False,
            "dryRun": False,
            "dashboardUrlPath": "/query?payload=compact",
            "metadata": {
                "resultType": "users",
                "sql": "select * from transactions",
            },
            "rows": [row],
        },
    }

    def fake_run(command, **kwargs):
        return _Completed(json.dumps(payload))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with caplog.at_level(logging.INFO, logger="hermes.elixir_analytics_runner"):
        result = module.run_elixir_analytics_runner(
            {
                "mode": "answer_question",
                "question": "which users had failed onboarding attempts this week?",
            }
        )

    assert result["ok"] is True
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "mode=answer_question" in log_text
    assert "route=supabase_ad_hoc" in log_text
    assert "shortcut=swiggy_users_this_week" in log_text
    assert "rowCount=1" in log_text
    assert "resultType=users" in log_text
    assert "dashboard=True" in log_text
    assert "ada@example.com" not in log_text
    assert "select * from transactions" not in log_text
    assert "which users spent on Swiggy this week" not in log_text


def test_answer_question_mode_defaults_to_compact_row_cap(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "route": "posthog_ad_hoc"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "answer_question",
            "question": "how many app active users this week?",
        }
    )

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-analytics-question.ts",
        "--max-rows",
        "100",
    ]
    assert kwargs["input"] == "how many app active users this week?"


def test_question_without_mode_defaults_to_answer_question(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(
            json.dumps(
                {
                    "ok": True,
                    "dryRun": True,
                    "rows": [],
                    "rowCount": 0,
                    "truncated": False,
                    "maxRows": 25,
                    "metadata": {},
                    "dashboardUrlPath": None,
                    "dashboardUrl": None,
                    "logEntry": "## Query #0",
                }
            )
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"question": "show GTV last 30 days by week", "dry_run": True}
    )

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert result["mode"] == "answer_question"
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/run-ad-hoc-query.ts",
        "--max-rows",
        "25",
        "--dry-run",
    ]
    assert json.loads(kwargs["input"])["question"] == "show GTV last 30 days by week"


def test_source_change_plan_mode_invokes_source_change_planner_on_stdin(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "kind": "metric_definition"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "source_change_plan",
            "request": "GTV definition is wrong, wallet loads should be included",
        }
    )

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/plan-source-change.ts",
    ]
    assert kwargs["input"] == "GTV definition is wrong, wallet loads should be included"


def test_source_change_modes_reject_non_ritik_slack_users(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "kind": "metric_definition"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with _gateway_session_vars(platform="slack", user_id="U_ALEX", user_name="Alex"):
        result = module.run_elixir_analytics_runner(
            {
                "mode": "source_change_plan",
                "request": "GTV definition is wrong, wallet loads should be included",
            }
        )

    assert result["ok"] is False
    assert result["errorType"] == "permission_denied"
    assert "Ritik-only" in result["message"]
    assert calls == []


def test_source_change_modes_allow_ritik_slack_user(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "kind": "metric_definition"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with _gateway_session_vars(
        platform="slack",
        user_id="U_RITIK",
        user_name="Ritik Madan",
    ):
        result = module.run_elixir_analytics_runner(
            {
                "mode": "source_change_plan",
                "request": "GTV definition is wrong, wallet loads should be included",
            }
        )

    assert result["ok"] is True
    assert len(calls) == 1


def test_source_change_modes_allow_configured_slack_user_id(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "kind": "metric_definition"}))

    monkeypatch.setenv("ELIXIR_ANALYTICS_SOURCE_CHANGE_ALLOWED_USERS", "U_APPROVED")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with _gateway_session_vars(
        platform="slack",
        user_id="U_APPROVED",
        user_name="Alex",
    ):
        result = module.run_elixir_analytics_runner(
            {
                "mode": "source_change_plan",
                "request": "GTV definition is wrong, wallet loads should be included",
            }
        )

    assert result["ok"] is True
    assert len(calls) == 1


def test_pre_tool_hook_blocks_non_ritik_source_control_tools():
    module = _load_plugin_module()
    ctx = _Ctx()

    module.register(ctx)

    hook = ctx.hooks["pre_tool_call"][0]
    with _gateway_session_vars(platform="slack", user_id="U_ALEX", user_name="Alex"):
        block = hook(
            tool_name="execute_code",
            args={
                "code": "import subprocess\nsubprocess.run(['git', 'commit', '-m', 'x'])"
            },
            task_id="",
            session_id="",
            tool_call_id="",
        )

    assert block == {
        "action": "block",
        "message": "Elixir analytics source-control actions are Ritik-only in Slack.",
    }


def test_source_change_scope_check_mode_invokes_scope_checker(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "status": "ready"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "source_change_scope_check",
            "request": "GTV definition is wrong",
            "changed_files": [
                "GLOSSARY.md",
                "tests/metric-contracts.test.ts",
            ],
        }
    )

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/check-source-change-scope.ts",
        "--changed-files-json",
        '["GLOSSARY.md", "tests/metric-contracts.test.ts"]',
    ]
    assert kwargs["input"] == "GTV definition is wrong"


def test_source_change_scope_check_accepts_string_changed_files(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "status": "ready"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {
            "mode": "source_change_scope_check",
            "request": "add a glossary term",
            "changed_files": "GLOSSARY.md, tests/agent-instructions.test.ts",
            "allow_unexpected_files": True,
        }
    )

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/check-source-change-scope.ts",
        "--changed-files-json",
        '["GLOSSARY.md", "tests/agent-instructions.test.ts"]',
        "--allow-unexpected-files",
    ]
    assert kwargs["input"] == "add a glossary term"


def test_self_improvement_plan_mode_invokes_self_improvement_planner_with_query_log(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "entriesReviewed": 5}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner({"mode": "self_improvement_plan"})

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/plan-self-improvement.ts",
        "--query-log",
        "QUERY_LOG.md",
    ]
    assert kwargs["input"] is None


def test_self_improvement_check_mode_invokes_cadence_checker_with_query_log(monkeypatch):
    module = _load_plugin_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(json.dumps({"ok": True, "status": "not_due"}))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner({"mode": "self_improvement_check"})

    command, kwargs = calls[0]
    assert result["ok"] is True
    assert command == [
        "node",
        "--import",
        "tsx",
        "scripts/check-self-improvement-cadence.ts",
        "--query-log",
        "QUERY_LOG.md",
    ]
    assert kwargs["input"] is None


def test_runner_failure_returns_structured_error(monkeypatch):
    module = _load_plugin_module()

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout=3, output="partial")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.run_elixir_analytics_runner(
        {"mode": "plan", "question": "show GTV", "timeout_seconds": 3}
    )

    assert result["ok"] is False
    assert result["errorType"] == "timeout"
    assert result["timeoutSeconds"] == 3
