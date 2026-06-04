# Elixir Analytics Hosted Gateway Runbook

This runbook is for Milestone 12A: make Slack `macros` independent of the
local laptop.

## Target Shape

Run one long-lived Hermes gateway process for the `elixir-analytics` profile:

```bash
hermes -p elixir-analytics gateway run
```

Equivalent module form:

```bash
venv/bin/python -m hermes_cli.main --profile elixir-analytics gateway run
```

The host must support:

- a persistent process, not request/response serverless execution
- restart-on-crash supervision
- persistent profile storage for sessions, auth, skills, logs, and plugin state
- outbound HTTPS/WebSocket access for Slack Socket Mode, model providers,
  Supabase/Postgres, PostHog, GitHub, and the analytics dashboard
- private environment variables or mounted secret files

## Required Secrets

Keep secrets scoped to this profile and Slack app.

Required:

- `SLACK_APP_TOKEN`: Socket Mode app token for the Slack app `macros`
- `SLACK_BOT_TOKEN`: bot token for the Slack app `macros`

Required by current analytics milestones:

- `ANALYTICS_DATABASE_URL`: read-only Supabase/Postgres DSN
- `POSTHOG_API_KEY`: read-only PostHog API key
- `POSTHOG_PROJECT_ID`: PostHog project id

Optional, depending on visualization handoff:

- `ELIXIR_ANALYTICS_ARTIFACT_API_URL`
- `ELIXIR_ANALYTICS_ARTIFACT_API_SECRET`

Model auth must be available through the profile's configured provider. Current
live profile evidence uses `openai-codex` provider auth; if the hosted process
cannot use that auth, configure a managed provider or provider API key before
cutover.

## Filesystem Layout

Use one profile home for the hosted process:

```text
$HERMES_HOME/
  config.yaml
  .env
  auth.json
  skills/
  profile_plugins/
  logs/
  sessions/
  state.db*
```

Install the distribution:

```bash
hermes profile install ./profile-distributions/elixir-analytics --name elixir-analytics
```

Then copy only the required profile secrets into the installed profile `.env`.
Do not copy default-profile Slack tokens into this profile.

## Process Supervision

The supervisor should:

- start the command from the Hermes checkout or installed package directory
- set the profile explicitly with `-p elixir-analytics`
- restart on non-zero exit with a small backoff
- preserve stdout/stderr logs, or ship them to the host log system
- expose a way to run `hermes -p elixir-analytics gateway status`
- avoid running a second gateway for the same Slack app token

Example unit shape:

```text
Command: hermes -p elixir-analytics gateway run
Working directory: /srv/hermes-agent
Restart policy: always / on-failure
Health signal: gateway log contains "Gateway running" and "Socket Mode connected"
Rollback: stop hosted process, restart local launchd gateway
```

## Cutover Checklist

1. Stop the local profile gateway or disable its launchd service.
2. Start the hosted `elixir-analytics` gateway.
3. Confirm only one process is connected to Slack Socket Mode for `macros`.
4. Run:

   ```bash
   hermes -p elixir-analytics gateway status
   ```

5. Confirm hosted logs show both:

   ```text
   Gateway running
   Socket Mode connected
   ```

6. Send the Slack smoke prompts:

   - `show GTV last 30 days by week`
   - `which users spent on Swiggy this week?`
   - `how many app active users this week?`
   - `active users this week`
   - `delete from profiles`

7. From the analytics repo, verify the acceptance log:

   ```bash
   node --import tsx scripts/check-slack-e2e-logs.ts \
     --gateway-log <hosted-gateway-log-path> \
     --agent-log <hosted-agent-log-path>
   ```

## Rollback

Rollback is simple because Slack Socket Mode is token-scoped:

1. Stop the hosted gateway process.
2. Start the local `ai.hermes.gateway-elixir-analytics` launchd service.
3. Confirm local `logs/gateway.log` shows `Socket Mode connected`.
4. Re-run one saved-topic prompt in Slack.

Do not leave hosted and local gateways connected to the same Slack app token at
the same time.

## Done Criteria

Milestone 12A is done when:

- Slack `macros` answers from the hosted process with the local gateway stopped
- restart-on-crash is configured and tested once
- hosted logs are accessible for Slack E2E verification
- rollback to the local launchd gateway is documented and tested
- ops readiness can be run with explicit hosted gateway evidence

