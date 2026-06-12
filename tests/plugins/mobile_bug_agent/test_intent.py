from __future__ import annotations

from pathlib import Path

from plugins.mobile_bug_agent.config import MonicaConfig, RuntimeConfig, WorkerConfig
from plugins.mobile_bug_agent.intent import IntentClassifier, IntentResult


class FakeAgent:
    def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
        assert "You are Monica" in system_message
        assert "Tagged request:" in user_message
        return {
            "final_response": """{
              "is_mobile_bug": true,
              "wants_linear": true,
              "wants_fix": true,
              "confidence": 0.92,
              "summary": "Android checkout crashes after applying promo code",
              "missing_questions": [],
              "reason": "The tagged thread describes a reproducible Android app crash."
            }"""
        }


class BadAgent:
    def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
        return {"final_response": "not json"}


class StringBooleanAgent:
    def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
        return {
            "final_response": """{
              "is_mobile_bug": "false",
              "wants_linear": "false",
              "wants_fix": "false",
              "confidence": "0.81",
              "summary": "Backend invoice sync is slow",
              "missing_questions": [],
              "reason": "The tagged thread is not about the mobile app."
            }"""
        }


def test_classifier_parses_agent_json():
    result = IntentClassifier(agent_factory=lambda: FakeAgent()).classify(
        request_text="@monica checkout crashes on Android after promo",
        thread_text="U1: checkout crashes on Android after promo\nU2: Pixel 7, latest build",
    )

    assert result.is_mobile_bug is True
    assert result.wants_linear is True
    assert result.wants_fix is True
    assert result.summary.startswith("Android checkout")


def test_classifier_treats_string_false_booleans_as_false():
    result = IntentClassifier(agent_factory=lambda: StringBooleanAgent()).classify(
        request_text="@monica invoice sync is slow",
        thread_text="U1: backend invoice sync queue is behind",
    )

    assert result.is_mobile_bug is False
    assert result.wants_linear is False
    assert result.wants_fix is False
    assert result.confidence == 0.81


class RichAgent:
    def run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None):
        assert "observed_behavior" in system_message
        return {
            "final_response": """{
              "is_mobile_bug": true,
              "wants_linear": true,
              "wants_fix": false,
              "confidence": 0.87,
              "summary": "Android checkout crashes after applying promo code",
              "observed_behavior": "Checkout crashes after applying a promo code.",
              "expected_behavior": "Checkout should keep the cart open and apply the discount.",
              "reproduction_steps": ["Open checkout", "Apply a promo code", "Tap pay"],
              "platforms": ["Android"],
              "device_context": "Pixel 7 on latest build",
              "build_context": "2.14.0 beta",
              "missing_questions": [],
              "reason": "Thread includes crash, platform, device, and reproduction context."
            }"""
        }


def test_classifier_parses_rich_bug_context():
    result = IntentClassifier(agent_factory=lambda: RichAgent()).classify(
        request_text="@monica checkout crashes on Android after promo",
        thread_text="U1: checkout crashes after promo\nU2: Pixel 7, latest build 2.14.0 beta",
    )

    assert result.observed_behavior == "Checkout crashes after applying a promo code."
    assert result.expected_behavior == "Checkout should keep the cart open and apply the discount."
    assert result.reproduction_steps == ("Open checkout", "Apply a promo code", "Tap pay")
    assert result.platforms == ("Android",)
    assert result.device_context == "Pixel 7 on latest build"
    assert result.build_context == "2.14.0 beta"


def test_classifier_uses_codex_cli_backend_when_configured(tmp_path):
    calls = []

    def fake_run(command, cwd, prompt, timeout):
        calls.append((command, cwd, prompt, timeout))
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(
            """{
              "is_mobile_bug": true,
              "wants_linear": true,
              "wants_fix": true,
              "confidence": 0.9,
              "summary": "iOS PDP tags are hard coded",
              "missing_questions": [],
              "reason": "The request describes native app PDP copy that needs cleanup."
            }""",
            encoding="utf-8",
        )
        return "stdout fallback"

    config = MonicaConfig(
        runtime=RuntimeConfig(home_subdir=str(tmp_path / "runtime")),
        worker=WorkerConfig(
            backend="codex_cli",
            codex_model="gpt-5-codex",
            codex_profile="monica",
        ),
    )

    result = IntentClassifier(config=config, codex_run_command=fake_run).classify(
        request_text="@monica iOS PDP tags are hard coded, please fix it",
        thread_text="U1: screenshot attached from the native app",
    )

    command, cwd, prompt, timeout = calls[0]
    assert result.is_mobile_bug is True
    assert result.wants_fix is True
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("-c") + 1] == 'approval_policy="never"'
    assert command[command.index("--cd") + 1] == str(tmp_path / "runtime")
    assert "--skip-git-repo-check" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5-codex"
    assert command[command.index("--profile") + 1] == "monica"
    assert command[-1] == "-"
    assert cwd == tmp_path / "runtime"
    assert "Tagged request:" in prompt
    assert "Return strict JSON" in prompt
    assert timeout == 180


def test_classifier_falls_back_safely_on_bad_json():
    result = IntentClassifier(agent_factory=lambda: BadAgent()).classify(
        request_text="@monica hello",
        thread_text="U1: hello",
    )

    assert isinstance(result, IntentResult)
    assert result.is_mobile_bug is False
    assert result.needs_clarification is True
    assert result.confidence <= 0.5


def test_fallback_does_not_classify_non_mobile_fix_request_as_mobile_bug():
    result = IntentClassifier(agent_factory=lambda: BadAgent()).classify(
        request_text="@monica backend API error is returning 500s, please fix it",
        thread_text="U1: started after the billing deploy",
    )

    assert result.is_mobile_bug is False
    assert result.wants_fix is True
    assert result.needs_clarification is True
    assert "mobile" in result.missing_questions[0].lower()


def test_fallback_does_not_classify_web_frontend_bug_as_mobile_bug():
    result = IntentClassifier(agent_factory=lambda: BadAgent()).classify(
        request_text="@monica web frontend checkout regression after the header deploy, please fix it",
        thread_text="U1: reproduces in Chrome desktop, not the native app",
    )

    assert result.is_mobile_bug is False
    assert result.wants_fix is True
    assert result.needs_clarification is True
    assert "mobile" in result.missing_questions[0].lower()


def test_fallback_does_not_classify_android_web_bug_as_native_mobile_bug():
    result = IntentClassifier(agent_factory=lambda: BadAgent()).classify(
        request_text="@monica checkout is broken on Android Chrome, please fix it",
        thread_text="U1: this is the mobile web flow, not the native app",
    )

    assert result.is_mobile_bug is False
    assert result.wants_fix is True
    assert result.needs_clarification is True
    assert "mobile" in result.missing_questions[0].lower()


def test_fallback_keeps_question_only_mobile_bug_tag_actionless():
    result = IntentClassifier(agent_factory=lambda: BadAgent()).classify(
        request_text="@monica any thoughts on this Android checkout crash?",
        thread_text="U1: Pixel 7 on 2.14.0 beta",
    )

    assert result.is_mobile_bug is True
    assert result.wants_linear is False
    assert result.wants_fix is False


def test_fallback_classifies_clear_android_fix_request_as_mobile_bug():
    result = IntentClassifier(agent_factory=lambda: BadAgent()).classify(
        request_text="@monica Android checkout error after promo, please fix it",
        thread_text="U1: Pixel 7 on 2.14.0 beta",
    )

    assert result.is_mobile_bug is True
    assert result.wants_linear is True
    assert result.wants_fix is True
    assert result.platforms == ("Android",)
    assert result.needs_clarification is False
