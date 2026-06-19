# Elixir Analytics Hermes Release Packaging

Use this guide before staging, committing, or opening a PR from the Hermes
worktree for the analytics agent.

## Package Lanes

### profile-distribution

Includes the user-facing Elixir analytics profile:

- `profile-distributions/elixir-analytics/config.yaml`
- `profile-distributions/elixir-analytics/SOUL.md`
- `profile-distributions/elixir-analytics/ROADMAP.md`
- `profile-distributions/elixir-analytics/skills/**`
- `profile-distributions/elixir-analytics/plugins/**`

This package changes what the installed `elixir-analytics` profile knows and
which deterministic analytics tools it can call.

### hermes-runtime

Includes Hermes core changes that affect the profile boundary:

- profile distribution installation support
- plugin loading and toolset exposure for `elixir-analytics-runner`
- tests that prove the profile distribution can be installed and the runner
  plugin is exposed

This package is only needed when the existing Hermes interface is insufficient.
The normal Chandler path uses profile-local `plugins/` and avoids Hermes-core
changes.

### slack-gateway

Includes Slack transport health changes:

- Slack Socket Mode reconnect/recovery logging
- Slack clarification prompt telemetry and provider-error hygiene
- tests that prove the gateway logs a recovered connection after transport
  self-healing and suppresses noisy provider retry messages

This package can ship with runtime changes, but it should remain reviewable as
gateway reliability work rather than analytics prompt behavior.

## Checker

Run from the Hermes repo:

```bash
venv/bin/python scripts/check_elixir_analytics_release_packaging.py --strict
```

Use JSON output when preparing package-specific staging:

```bash
venv/bin/python scripts/check_elixir_analytics_release_packaging.py --json
```

`--strict` fails when changed files are unclassified. Local artifacts such as
`.hermes-bootstrap-complete` are reported separately and should not be staged.

## Release Rule

Do not stage the full Hermes dirty worktree as one commit.

Package in this order unless the user chooses otherwise:

1. `profile-distribution`
2. `hermes-runtime` only when an approved Hermes interface change is required
3. `slack-gateway`

After profile distribution changes, sync the installed profile before Slack E2E:

```bash
cp profile-distributions/elixir-analytics/skills/elixir-analytics/SKILL.md /Users/ritik/.hermes/profiles/elixir-analytics/skills/elixir-analytics/SKILL.md
cp profile-distributions/elixir-analytics/ROADMAP.md /Users/ritik/.hermes/profiles/elixir-analytics/ROADMAP.md
cp -R profile-distributions/elixir-analytics/plugins/elixir-analytics-runner /Users/ritik/.hermes/profiles/elixir-analytics/plugins/
```

Then restart the profile gateway and verify Slack Socket Mode reconnects.
