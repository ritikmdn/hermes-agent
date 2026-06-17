from gateway.canary import (
    GATEWAY_AGENTIC_CONTRACT_VERSION,
    build_runtime_fingerprint,
    detect_stale_runtime,
    run_pre_gateway_contract_canary,
)


def test_runtime_fingerprint_contains_contract_version_and_digest():
    fingerprint = build_runtime_fingerprint()

    assert fingerprint["contract_version"] == GATEWAY_AGENTIC_CONTRACT_VERSION
    assert fingerprint["digest"]
    assert "gateway/run.py" in fingerprint["files"]


def test_detect_stale_runtime_reports_digest_mismatch():
    warning = detect_stale_runtime(
        expected={"digest": "expected", "source": "repo"},
        actual={"digest": "actual", "source": "runtime"},
    )

    assert warning is not None
    assert "expected" in warning
    assert "actual" in warning


def test_detect_stale_runtime_accepts_matching_digest():
    assert detect_stale_runtime(
        expected={"digest": "same"},
        actual={"digest": "same"},
    ) is None


def test_pre_gateway_contract_canary_blocks_conversational_respond():
    result = run_pre_gateway_contract_canary()

    assert result["ok"] is True
    assert result["route"] == "agent"
    assert result["blocked_reason"] == "conversational_respond_not_allowed"
