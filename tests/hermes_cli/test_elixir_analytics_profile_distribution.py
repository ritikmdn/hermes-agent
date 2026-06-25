import re
from pathlib import Path

import yaml

from hermes_cli.profile_distribution import DistributionManifest
from hermes_cli.tools_config import _get_platform_tools


REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "profile-distributions" / "elixir-analytics"
SKILL_PATH = DIST_DIR / "skills" / "elixir-analytics" / "SKILL.md"
SOUL_PATH = DIST_DIR / "SOUL.md"


def _load_yaml(name: str):
    return yaml.safe_load((DIST_DIR / name).read_text(encoding="utf-8"))


def test_elixir_analytics_distribution_manifest_is_installable():
    manifest = DistributionManifest.from_dict(_load_yaml("distribution.yaml"))

    assert manifest.name == "elixir-analytics"
    assert manifest.description == "Slack-first Elixir analytics agent."
    assert "SOUL.md" in manifest.owned_paths()
    assert "ROADMAP.md" in manifest.owned_paths()
    assert "RELEASE_PACKAGING.md" in manifest.owned_paths()
    assert "HOSTED_GATEWAY.md" in manifest.owned_paths()
    assert "config.yaml" in manifest.owned_paths()
    assert "metric-contracts" in manifest.owned_paths()
    assert "profile_plugins" in manifest.owned_paths()
    assert "deploy" in manifest.owned_paths()

    required_env = {req.name for req in manifest.env_requires if req.required}
    assert {"SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"}.issubset(required_env)


def test_elixir_analytics_profile_keeps_slack_self_improvement_tools():
    config = _load_yaml("config.yaml")
    slack_toolsets = _get_platform_tools(config, "slack")

    assert {
        "elixir-analytics-runner",
        "web",
        "terminal",
        "file",
        "skills",
        "todo",
        "memory",
        "session_search",
        "clarify",
        "code_execution",
        "cronjob",
        "messaging",
    }.issubset(slack_toolsets)


def test_elixir_analytics_profile_configures_inference_provider():
    config = _load_yaml("config.yaml")

    assert config["model"]["provider"] == "openai-codex"
    assert config["model"]["default"] == "gpt-5.5"


def test_elixir_analytics_profile_uses_smart_approvals_without_restricting_slack_code():
    config = _load_yaml("config.yaml")
    slack_toolsets = _get_platform_tools(config, "slack")

    assert config["approvals"]["mode"] == "smart"
    assert "code_execution" in slack_toolsets


def test_elixir_analytics_profile_enables_runner_plugin():
    config = _load_yaml("config.yaml")

    assert "elixir-analytics-runner" in config["plugins"]["enabled"]


def test_elixir_analytics_profile_excludes_unrelated_fluff():
    config = _load_yaml("config.yaml")
    slack_toolsets = _get_platform_tools(config, "slack")

    assert {
        "vision",
        "image_gen",
        "video",
        "video_gen",
        "tts",
        "browser",
        "homeassistant",
        "spotify",
        "discord",
        "discord_admin",
        "computer_use",
        "delegation",
    }.isdisjoint(slack_toolsets)


def test_elixir_analytics_distribution_ships_analytics_skill():
    body = SKILL_PATH.read_text(encoding="utf-8")
    frontmatter = body.split("---", 2)[1]

    assert "name: elixir-analytics" in frontmatter
    assert "description: Answer Elixir analytics questions." in frontmatter
    assert "## Slack Voice" in body
    assert "The Slack bot may be named Chandler" in body
    assert "Chandler is Ritik's sharp business analyst" in body
    assert "Do not\nimitate or quote Chandler Bing" in body
    assert "proof/source detail available in the artifact" in body
    assert "Do not dump metric contracts, source tables, freshness timestamps" in body
    assert "Stay sober for sensitive user-level data" in body
    assert "Read-only analytics questions are self-serve in Slack channels and DMs" in body
    assert 'questions like "what can Chandler do?"' in body
    assert "`analytics_answer`" in body
    assert "`AnswerArtifact`" in body
    assert "SLACK_RESPONSE_SLA_SECONDS = 10" in body
    assert "Source-of-truth changes, source-control actions, commits, pushes, and PRs are" in body
    assert "Ritik-only" in body
    assert "ELIXIR_ANALYTICS_SOURCE_CHANGE_ALLOWED_USERS" in body
    assert "/Users/ritik/Coding/claude-analytics/GLOSSARY.md" in body
    assert "/Users/ritik/Coding/claude-analytics/src/lib/analytics/metric-contracts.ts" in body
    assert "../../metric-contracts/wearable_identified_users.yaml" in body
    assert "/Users/ritik/Coding/claude-analytics/docs/ad-hoc-query-protocol.md" in body
    assert "/Users/ritik/Coding/claude-analytics/docs/source-change-workflow.md" in body
    assert "/Users/ritik/Coding/claude-analytics/docs/self-improvement-loop.md" in body
    assert "/Users/ritik/Coding/claude-analytics/docs/ops-readiness.md" in body
    assert "/Users/ritik/Coding/claude-analytics/docs/slack-smoke-suite.md" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/plan-analytics-question.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/plan-source-change.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/check-source-change-scope.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/check-self-improvement-cadence.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/plan-self-improvement.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/check-ops-readiness.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/run-analytics-smoke-suite.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/check-slack-e2e-logs.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/run-analytics-answer.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/run-analytics-question.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/run-saved-query-topic.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/run-ad-hoc-query.ts" in body
    assert "/Users/ritik/Coding/claude-analytics/scripts/run-posthog-query.ts" in body
    assert (
        "the first tool call must be\n"
        "   `analytics_answer`"
    ) in body
    assert "Plan remaining Slack analytics questions" in body
    assert "Plan every definition/glossary/schema/dashboard source-change request" in body
    assert "`elixir_analytics_runner` mode `source_change_plan`" in body
    assert 'mode: "source_change_scope_check"' in body
    assert "`scripts/check-source-change-scope.ts`" in body
    assert "Check self-improvement cadence with `elixir_analytics_runner` mode" in body
    assert "`self_improvement_check`" in body
    assert "Plan self-improvement reviews with `elixir_analytics_runner` mode" in body
    assert "`self_improvement_plan`" in body
    assert "Check production readiness" in body
    assert "Run `scripts/run-analytics-smoke-suite.ts`" in body
    assert "Run `scripts/check-slack-e2e-logs.ts` after live Slack acceptance prompts" in body
    assert "`saved_topic`: run `recommendedCommand`" in body
    assert "`dashboard_candidate`" in body
    assert "`sourceChangeRequest`" in body
    assert "`blockers`" in body
    assert "`ops_readiness_contract`" in body
    assert "`posthog_ad_hoc`" in body
    assert "`requiresClarification`" in body
    assert "`requiredFiles`" in body
    assert "`prTitle`" in body
    assert "Mandatory First Call" in body
    assert "the first tool call must be\n`analytics_answer`" in body
    assert "exact raw Slack question for standalone requests" in body
    assert "For follow-ups whose referents are present in prior `AnswerArtifact`s" in body
    assert "resolved follow-up question that names the artifact handle or IDs" in body
    assert "Do not plan, inspect files, write\nSQL, run `execute_code`" in body
    assert "edit the repo, or manually query before this first\ncall" in body
    assert "Only if it returns `needs_model_plan`" in body
    assert "Fast Slack Answer Path" in body
    assert "Prefer the `analytics_answer` tool" in body
    assert "`answerText` plus `answerArtifact`" in body
    assert "exact raw Slack question" in body
    assert "resolve that phrase from the artifact" in body
    assert "answer only from\nthe new `answerText`" in body
    assert "Use a compact Slack row cap such as `max_rows: 25`" in body
    assert "runs `scripts/run-analytics-answer.ts`" in body
    assert "If `analytics_answer` returns `needs_model_plan`" in body
    assert "Call\n   `scripts/plan-analytics-question.ts` once" in body
    assert "Do not use `patch`, edit files, append `QUERY_LOG.md`" in body
    assert "Do not block a plain Slack data answer on source edits" in body
    assert "Do not add a dashboard link merely because `dashboardUrl`" in body
    assert "single-number KPI answers" in body
    assert "Keep source-control and PR work Ritik-only in Slack" in body
    assert "Do not use generic `execute_code` for the first pass" in body
    assert "Only after `analytics_answer` returns `needs_model_plan`, call the\n" in body
    assert "deterministic planner" in body
    assert "card-gtv-weekly" in body
    assert "which users spent on Swiggy this week" in body
    assert "`analytics_answer` -> Supabase ad hoc source result -> `AnswerArtifact`" in body
    assert "how many app active users this week" in body
    assert "`analytics_answer` -> PostHog source result -> `AnswerArtifact`" in body
    assert "Do not combine Supabase and" in body
    assert "PostHog active-user definitions" in body
    assert "`active_app_user`" in body
    assert "loadDotenv({ path: \".env.local\"" in body
    assert "dynamic import" in body
    assert "`logEntry` in stdout" in body
    assert "do not create a duplicate query number" in body
    assert "`dashboardUrl`" in body
    assert "answer Slack with the bounded rows or\nconcise summary in Chandler's voice" in body
    assert "defaulting to `https://analytics.joinelixir.club`" in body
    assert "Ask clarification" in body
    assert "--env-file /Users/ritik/.hermes/profiles/elixir-analytics/.env" in body
    assert (
        "scripts/check-ops-readiness.ts --current-branch '<branch>' --profile-home "
        "/Users/ritik/.hermes/profiles/elixir-analytics"
    ) in body
    assert "--gateway-hosted --slack-connected" not in body
    assert "--provider-authenticated openai-codex" in body
    assert "auth status openai-codex" in body
    assert "scripts/check-slack-e2e-logs.ts --gateway-log" in body
    assert "route, timing, call-count, and safe runner telemetry" in body
    assert "`source_change_workflow` for definition\nchanges that must become PR work" in body
    assert 'mode: "source_change_plan"' in body
    assert 'mode: "self_improvement_check"' in body
    assert 'mode: "self_improvement_plan"' in body
    assert "short redirect is acceptable" not in body


def test_elixir_analytics_skill_uses_durable_clarify_for_ambiguity():
    body = SKILL_PATH.read_text(encoding="utf-8")
    soul = SOUL_PATH.read_text(encoding="utf-8")

    assert "use the `clarify` tool" in body
    assert "Do not ask the clarification as an ordinary final answer" in body
    assert "the next Slack reply is captured as the answer" in body
    assert "If the planner returns `clarify`, call `clarify`" in body
    assert "If the user replies by changing the definition" in body
    assert "use `clarify` to ask whether they mean card active" in body
    assert "If it returns `requiresClarification`, use `clarify` before editing" in body
    assert "Use `clarify` for ambiguous business terms" in soul


def test_elixir_analytics_wearable_definition_requires_identified_hardware():
    body = SKILL_PATH.read_text(encoding="utf-8")
    metric_contract = yaml.safe_load(
        (
            DIST_DIR / "metric-contracts" / "wearable_identified_users.yaml"
        ).read_text(encoding="utf-8")
    )
    wearable_reference = (
        DIST_DIR
        / "skills"
        / "elixir-analytics"
        / "references"
        / "wearable-provider-customer-counts.md"
    ).read_text(encoding="utf-8")
    health_sync_reference = (
        DIST_DIR
        / "skills"
        / "elixir-analytics"
        / "references"
        / "user-demographics-health-sync.md"
    ).read_text(encoding="utf-8")
    plugin_init = (
        DIST_DIR / "profile_plugins" / "elixir-analytics-runner" / "__init__.py"
    ).read_text(encoding="utf-8")

    for text in (body, wearable_reference, health_sync_reference, plugin_init):
        assert "wearable-identified" in text
        assert "generic" in text
        assert "GOOGLE" in text
        assert "health-sync" in text
        assert "provider" in text

    assert "Do not roll generic aggregators into wearable totals" in wearable_reference
    assert "TOTAL_WEARABLE_IDENTIFIED_USERS" in wearable_reference
    assert "TOTAL_HEALTH_SYNC_USERS" in wearable_reference
    assert "TOTAL_APPLE_HEALTH_SYNC_USERS" in wearable_reference
    assert "TOTAL_TERRA_SYNC_USERS" in wearable_reference
    assert "APPLE_WATCH_VIA_APPLE_HEALTH" in wearable_reference
    assert "WHOOP_VIA_APPLE_HEALTH" in wearable_reference
    assert "GARMIN_VIA_TERRA" in wearable_reference
    assert "SAMSUNG_GALAXY_WATCH_VIA_GOOGLE_TERRA" in wearable_reference
    assert "SAMSUNG_GALAXY_WATCH_VIA_TERRA" in wearable_reference
    assert "HUAWEI_WATCH_VIA_GOOGLE_TERRA" in wearable_reference
    assert "NOTHING_WATCH_VIA_GOOGLE_TERRA" in wearable_reference
    assert "CULTSPORT_WATCH_VIA_GOOGLE_TERRA" in wearable_reference
    assert "ANTPLUS_SENSOR_VIA_GOOGLE_TERRA" in wearable_reference
    assert "ANTPLUS_SENSOR_VIA_TERRA" in wearable_reference
    assert "DAFIT_CRREPA_VIA_TERRA" in wearable_reference
    assert "FOSSIL_VIA_TERRA" in wearable_reference
    assert "q explorist" in wearable_reference
    assert "SM-R" in wearable_reference
    assert "forerunner" in wearable_reference
    assert "IPHONE_HEALTH_ONLY" in wearable_reference
    assert "GOOGLE_FIT_ANDROID_HEALTH_ONLY" in wearable_reference
    assert "HEALTH_DATA_EXISTS_SOURCE_UNKNOWN" in wearable_reference
    assert "Apple Watch users" in wearable_reference
    assert "brand rollup" in wearable_reference
    assert "profiles.reward_rate" in wearable_reference
    assert "card-issued" in wearable_reference
    assert "active synced `APPLE_HEALTH`" not in wearable_reference
    assert "Apple Health user membership comes from `health_data_providers.provider" not in (
        wearable_reference
    )
    assert "profiles.onboardstatus.data.metadata.healthDeviceManufacturer" in (
        wearable_reference
    )
    assert "WatchN,M" in wearable_reference
    assert "`apple watch` / `watchos` are QA-only" in wearable_reference
    assert "health_data_* .data.device_data.name" in wearable_reference
    assert "com.google.android.gms" in wearable_reference
    assert "Dedicated wearable ecosystems" in health_sync_reference
    assert "Apple Health wearable evidence does not require an Apple Health provider row" in (
        health_sync_reference
    )
    assert metric_contract["id"] == "wearable_identified_users"
    assert metric_contract["displayName"] == "Wearable Identified Users"
    assert metric_contract["sourceSystem"] == "supabase"
    assert "public.profiles" in metric_contract["sourceTables"]
    assert "public.health_data_providers" in metric_contract["sourceTables"]
    assert "profiles.onboardstatus" in metric_contract["sourceTables"]
    assert metric_contract["aggregation"] == "count_distinct_user_id"
    assert metric_contract["grain"] == "user_wearable_brand_evidence"
    assert metric_contract["defaultFilters"]["population"] == "card_issued_users"
    assert metric_contract["defaultFilters"]["appleWatchEvidence"] == "exact WatchN,M"
    assert "profiles.reward_rate" in metric_contract["validBreakdowns"]
    assert "IPHONE_HEALTH_ONLY" in metric_contract["nonWearableBuckets"]
    assert "GOOGLE_FIT_ANDROID_HEALTH_ONLY" in metric_contract["nonWearableBuckets"]
    assert "HEALTH_DATA_EXISTS_SOURCE_UNKNOWN" in metric_contract["nonWearableBuckets"]
    assert metric_contract["ambiguityPolicy"]["activeUsers"] == (
        "default_to_combined_active_users_for_wearable_questions"
    )


def test_elixir_analytics_distribution_ships_product_roadmap():
    body = (DIST_DIR / "ROADMAP.md").read_text(encoding="utf-8")

    assert "Slack app: macros" in body
    assert "Current Status Snapshot" in body
    assert "Production dashboard" in body
    assert "Analytics PR #6" in body
    assert "Analytics PR #7" in body
    assert "Analytics PR #9" in body
    assert "Open visualization" in body
    assert "/query?result=..." in body
    assert "analytics-agent-ogg92mopr" in body
    assert "HTTP 429 usage-limit" in body
    assert "ops-readiness status from analytics `main` is `ready`" in body
    assert 'overallStatus: "ready"' in body
    assert "signed-in dashboard proof passed" in body
    assert "INTERNAL_LAUNCH_RUNBOOK.md" in body
    assert "Slack E2E Checklist" in body
    assert "Milestone 12A: operate internal-team launch v1." in body
    assert "show GTV last 30 days by week" in body
    assert "which users spent on Swiggy this week?" in body
    assert "top_card_spenders_30d" in body
    assert "2 API calls, 15 rows" in body
    assert "2 API calls, dashboard link present" in body
    assert "row_cap_truncation" in body
    assert "Milestone 12B: choose and implement hosted gateway." in body
    assert "Milestone 13A: choose Hermes upstream/private sync path." in body
    assert "Milestone 14A: compact Slack answer links." in body
    assert "active users this week" in body
    assert "how many app active users this week?" in body
    assert "delete from profiles" in body
    assert "--provider-authenticated openai-codex" in body


def test_elixir_analytics_distribution_ships_hosted_gateway_runbook():
    readme = (DIST_DIR / "README.md").read_text(encoding="utf-8")
    internal = (DIST_DIR / "INTERNAL_LAUNCH_RUNBOOK.md").read_text(encoding="utf-8")
    body = (DIST_DIR / "HOSTED_GATEWAY.md").read_text(encoding="utf-8")
    compose = (DIST_DIR / "deploy" / "docker-compose.gateway.yml").read_text(encoding="utf-8")
    compose_env = (DIST_DIR / "deploy" / ".env.hosted-gateway.example").read_text(encoding="utf-8")
    systemd_unit = (
        DIST_DIR / "deploy" / "systemd" / "hermes-elixir-analytics-gateway.service"
    ).read_text(encoding="utf-8")
    systemd_env = (
        DIST_DIR / "deploy" / "systemd" / "elixir-analytics-gateway.env.example"
    ).read_text(encoding="utf-8")

    assert "INTERNAL_LAUNCH_RUNBOOK.md" in readme
    assert "ai.hermes.gateway-elixir-analytics" in internal
    assert "launchctl print gui/501/ai.hermes.gateway-elixir-analytics" in internal
    assert "scripts/check-slack-e2e-logs.ts" in internal
    assert "Internal launch is ready when" in internal
    assert "HOSTED_GATEWAY.md" in readme
    assert "deploy/` directory includes Docker Compose and systemd" in readme
    assert "Slack `macros` independent of the\nlocal laptop" in body
    assert "hermes -p elixir-analytics gateway run" in body
    assert "deploy/docker-compose.gateway.yml" in body
    assert "deploy/systemd/hermes-elixir-analytics-gateway.service" in body
    assert "SLACK_APP_TOKEN" in body
    assert "SLACK_BOT_TOKEN" in body
    assert "ANALYTICS_DATABASE_URL" in body
    assert "Socket Mode connected" in body
    assert "Do not leave hosted and local gateways connected" in body
    assert 'command: ["--profile", "elixir-analytics", "gateway", "run"]' in compose
    assert "Socket Mode connected" in compose
    assert "SLACK_APP_TOKEN=" in compose_env
    assert "ExecStart=/srv/hermes-agent/venv/bin/python -m hermes_cli.main --profile elixir-analytics gateway run" in systemd_unit
    assert "Restart=on-failure" in systemd_unit
    assert "HERMES_HOME=/var/lib/hermes" in systemd_env


def test_elixir_analytics_soul_uses_selective_visualization_and_permissions():
    body = (DIST_DIR / "SOUL.md").read_text(encoding="utf-8")

    assert "the bot may appear as Chandler" in body
    assert "professional, concise, and data-first" in body
    assert "Never imitate or quote Chandler Bing" in body
    assert "preserve the metric contract id, source tables" in body
    assert "answer first and show proof/source details only when the user asks" in body
    assert "dashboard or temporary visualization links only when they materially help" in body
    assert "Single KPI\n  answers usually stay in Slack" in body
    assert "read-only analytics questions as self-serve in Slack channels and DMs" in body
    assert "Source-of-truth changes, source-control actions, commits, pushes, and PRs are\n  Ritik-only" in body
    assert "Do not use third-party URL shorteners" in body
    assert re.search(
        r"Do not finalize a Slack ad hoc data answer from manual `execute_code` output\s+alone",
        body,
    )


def test_elixir_analytics_distribution_ships_runner_plugin():
    plugin_dir = DIST_DIR / "profile_plugins" / "elixir-analytics-runner"
    manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text(encoding="utf-8"))
    init_body = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    answer_payloads_body = (plugin_dir / "answer_payloads.py").read_text(encoding="utf-8")
    commerce_requests_body = (plugin_dir / "commerce_shortcut_requests.py").read_text(
        encoding="utf-8"
    )
    commerce_formatters_body = (plugin_dir / "commerce_slack_formatters.py").read_text(
        encoding="utf-8"
    )
    runner_modes_body = (plugin_dir / "runner_modes.py").read_text(encoding="utf-8")
    shortcut_requests_body = (plugin_dir / "shortcut_requests.py").read_text(
        encoding="utf-8"
    )
    slack_formatters_body = (plugin_dir / "slack_formatters.py").read_text(
        encoding="utf-8"
    )
    slack_payload_router_body = (plugin_dir / "slack_payload_router.py").read_text(
        encoding="utf-8"
    )
    source_control_body = (plugin_dir / "source_control_guard.py").read_text(
        encoding="utf-8"
    )

    assert manifest["name"] == "elixir-analytics-runner"
    assert "analytics_answer" in manifest["provides_tools"]
    assert "elixir_analytics_runner" in manifest["provides_tools"]
    assert "from . import answer_payloads" in init_body
    assert "from . import card_shortcut_requests" in init_body
    assert "from . import commerce_shortcut_requests" in init_body
    assert "from . import commerce_slack_formatters" in init_body
    assert "from . import runner_modes" in init_body
    assert "from . import shortcut_requests" in init_body
    assert "from . import slack_formatters" in init_body
    assert "from . import slack_payload_router" in init_body
    assert "from . import source_control_guard" in init_body
    assert "def compact_answer_question_payload" in answer_payloads_body
    assert "def direct_final_response_for_answer_payload" in answer_payloads_body
    card_requests_body = (plugin_dir / "card_shortcut_requests.py").read_text(
        encoding="utf-8"
    )
    assert "def _card_gtv_7d_request" in card_requests_body
    assert "def _top_card_spender_7d_spend_breakdown_request" in card_requests_body
    assert "def _merchant_card_spend_7d_request" in commerce_requests_body
    assert "def _swiggy_spend_trend_10d_request" in commerce_requests_body
    assert "def _merchant_card_spend_7d_slack_text" in commerce_formatters_body
    assert "def _swiggy_spend_trend_10d_slack_text" in commerce_formatters_body
    assert "def _profile_answer_question_shortcut" in shortcut_requests_body
    assert "def _card_gtv_7d_slack_text" in slack_formatters_body
    assert "def _profile_answer_question_payload" in slack_payload_router_body
    assert "scripts/plan-analytics-question.ts" in runner_modes_body
    assert "scripts/run-analytics-question.ts" in runner_modes_body
    assert "scripts/run-saved-query-topic.ts" in runner_modes_body
    assert "scripts/run-ad-hoc-query.ts" in runner_modes_body
    assert "scripts/run-posthog-query.ts" in runner_modes_body
    assert "scripts/plan-source-change.ts" in runner_modes_body
    assert "scripts/check-self-improvement-cadence.ts" in runner_modes_body
    assert "scripts/plan-self-improvement.ts" in runner_modes_body
    assert "def permission_denied_result" in source_control_body
    assert "def block_non_ritik_source_control_tool" in source_control_body
