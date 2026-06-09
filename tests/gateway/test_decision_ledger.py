import json

from gateway.decision_ledger import GatewayDecisionLedger
from hermes_state import SessionDB


def test_decision_ledger_records_hook_annotation_without_mutating_intent():
    ledger = GatewayDecisionLedger(
        platform="slack",
        chat_id="C1",
        user_id="U1",
        message_id="m1",
        original_text="What changed most last week?",
    )

    ledger.record_hook_result(
        "elixir-analytics",
        {
            "action": "annotate",
            "reason": "elixir_analytics_agent_runtime_handoff",
            "context": "Use analytics tools as needed.",
        },
    )
    ledger.set_final_action("continue", reason="agent_dispatch")

    summary = ledger.summary()

    assert summary["final_action"] == "continue"
    assert summary["final_reason"] == "agent_dispatch"
    assert summary["decisions"][0]["actor"] == "elixir-analytics"
    assert summary["decisions"][0]["capability"] == "annotate"
    assert summary["decisions"][0]["reason"] == "elixir_analytics_agent_runtime_handoff"


def test_decision_ledger_bounds_and_redacts_text_fields():
    ledger = GatewayDecisionLedger(
        platform="slack",
        chat_id="C1",
        user_id="U1",
        message_id="m1",
        original_text="x" * 2000,
        max_text_chars=64,
    )

    ledger.record_hook_result(
        "unsafe-plugin",
        {
            "action": "respond",
            "reason": "leaked",
            "text": "secret sk-test_1234567890abcdef " + ("y" * 2000),
        },
    )

    summary = ledger.summary()
    assert len(summary["original_text_preview"]) <= 96
    serialized = json.dumps(summary)
    assert "sk-test_1234567890abcdef" not in serialized
    assert "sk-test_1234567890abcdef" not in summary["decisions"][0]["payload"]["text"]
    assert "truncated" in serialized


def test_session_db_persists_gateway_turn_records(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("s1", "slack")
        ledger = GatewayDecisionLedger(
            platform="slack",
            chat_id="C1",
            user_id="U1",
            message_id="m1",
            original_text="hello",
        )
        ledger.record_hook_result("profile", {"action": "allow", "reason": "not_analytics"})
        ledger.set_final_action("continue", reason="agent_dispatch", session_id="s1")

        row_id = db.append_gateway_turn("s1", ledger.summary())
        rows = db.get_gateway_turns("s1")

        assert row_id > 0
        assert len(rows) == 1
        assert rows[0]["session_id"] == "s1"
        assert rows[0]["turn_id"] == ledger.turn_id
        assert rows[0]["final_action"] == "continue"
        assert rows[0]["decision_count"] == 1
        assert rows[0]["summary"]["decisions"][0]["actor"] == "profile"
    finally:
        db.close()
