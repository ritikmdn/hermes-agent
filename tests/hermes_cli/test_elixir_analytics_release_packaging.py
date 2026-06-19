import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_elixir_analytics_release_packaging.py"


def _load_packaging_module():
    module_name = "test_elixir_analytics_release_packaging_script"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_release_verification_files_are_separate_from_runtime_changes():
    module = _load_packaging_module()

    result = module.classify_release_files([
        "profile-distributions/elixir-analytics/config.yaml",
        "scripts/check_elixir_analytics_release_packaging.py",
        "tests/hermes_cli/test_elixir_analytics_profile_distribution.py",
        "tests/hermes_cli/test_elixir_analytics_runner_plugin.py",
        "agent/conversation_loop.py",
    ])

    packages = {package["id"]: package["files"] for package in result["packages"]}
    assert packages["profile-distribution"] == [
        "profile-distributions/elixir-analytics/config.yaml",
    ]
    assert packages["release-verification"] == [
        "scripts/check_elixir_analytics_release_packaging.py",
        "tests/hermes_cli/test_elixir_analytics_profile_distribution.py",
        "tests/hermes_cli/test_elixir_analytics_runner_plugin.py",
    ]
    assert packages["hermes-runtime"] == ["agent/conversation_loop.py"]
