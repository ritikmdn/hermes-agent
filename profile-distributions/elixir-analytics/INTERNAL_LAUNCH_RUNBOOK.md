# Elixir Analytics Internal Launch Runbook

This runbook is for the internal-team launch of Slack `macros` while the
gateway is still supervised on Ritik's Mac with `launchd`.

Use this as the v1 operating model. It is launchable for internal usage as long
as the Mac stays online, awake, and connected. Use `HOSTED_GATEWAY.md` for the
v2 cutover when Slack analytics must be independent of the laptop.

## Product Boundary

Internal launch means:

- the team asks analytics questions in Slack through the `macros` app;
- Hermes runs the `elixir-analytics` profile as the AI brain;
- analytics answers use deterministic runners in `claude-analytics`;
- visual answers link to `https://analytics.joinelixir.club`;
- source-of-truth changes become tested repo changes and PRs;
- local supervision is acceptable for uptime during the pilot.

Internal launch does not mean:

- 24/7 production-grade uptime;
- multiple gateway hosts connected to the same Slack app token;
- unreviewed source-of-truth changes;
- durable saving of arbitrary ad hoc result payloads.

## Runtime

Current local service:

```text
launchd label: ai.hermes.gateway-elixir-analytics
profile: elixir-analytics
working directory: /Users/ritik/.hermes/profiles/elixir-analytics
stdout log: /Users/ritik/.hermes/profiles/elixir-analytics/logs/gateway.log
stderr log: /Users/ritik/.hermes/profiles/elixir-analytics/logs/gateway.error.log
```

Expected process command:

```bash
/Users/ritik/.hermes/hermes-agent/venv/bin/python \
  -m hermes_cli.main \
  --profile elixir-analytics \
  gateway run \
  --replace
```

Do not run another gateway with the same `SLACK_APP_TOKEN`/`SLACK_BOT_TOKEN`.
That includes a default-profile Hermes gateway and any hosted candidate process.

## Pre-Launch Check

Run from the Hermes repo:

```bash
launchctl print gui/501/ai.hermes.gateway-elixir-analytics
tail -n 120 /Users/ritik/.hermes/profiles/elixir-analytics/logs/gateway.log
venv/bin/python -m hermes_cli.main --profile elixir-analytics auth status openai-codex
```

The launchd output should show `state = running`. The gateway log should show:

```text
Active profile: elixir-analytics
[Slack] Socket Mode connected
Gateway running with 1 platform(s)
```

Run from the analytics repo:

```bash
node --import tsx scripts/check-ops-readiness.ts \
  --current-branch main \
  --profile-home /Users/ritik/.hermes/profiles/elixir-analytics \
  --env-file .env.production.local \
  --provider-authenticated openai-codex \
  --smart-approvals \
  --generic-tools
```

Expected result: `overallStatus: "ready"` and no blockers.

## Slack Acceptance Prompts

Send these to `macros` in Slack:

```text
show GTV last 30 days by week
what was GTV for last 7 days?
which users spent on Swiggy this week?
who spent the most in last 30 days?
how many app active users this week?
active users this week
delete from profiles
```

Expected behavior:

- weekly GTV uses saved topic `card-gtv-weekly`;
- 7-day GTV uses saved topic `card-gtv`;
- Swiggy users uses Supabase ad hoc route with a temporary visualization link;
- top spenders uses Supabase ad hoc route with bounded/truncated rows;
- app active users uses PostHog route;
- ambiguous active users asks a clarification before querying;
- write SQL is rejected.

Verify from the analytics repo:

```bash
node --import tsx scripts/check-slack-e2e-logs.ts \
  --gateway-log /Users/ritik/.hermes/profiles/elixir-analytics/logs/gateway.log \
  --agent-log /Users/ritik/.hermes/profiles/elixir-analytics/logs/agent.log
```

Expected result: `overallStatus: "pass"`.

## Dashboard Checks

Saved-topic visual:

```text
https://analytics.joinelixir.club/query?topic=card-gtv-weekly&range=30d
```

Expected signed-in result:

- title: `Ad hoc analytics`;
- content includes `Weekly Card GTV`;
- table has 5 weekly rows;
- query metadata is visible;
- no browser console errors.

Ad hoc visual:

- Open the latest `Open visualization` link from the Swiggy Slack answer.
- Expected signed-in result: the page shows the Slack question, bounded rows,
  metadata, and no browser console errors.

## Restart

Use a targeted restart:

```bash
launchctl kickstart -k gui/501/ai.hermes.gateway-elixir-analytics
```

Then confirm the log shows a fresh start and Slack reconnect:

```bash
tail -n 120 /Users/ritik/.hermes/profiles/elixir-analytics/logs/gateway.log
```

If restart happens while a Slack clarification is pending, the gateway may
auto-resume an old session. Verify the final product state with
`scripts/check-slack-e2e-logs.ts`; the checker treats a delivered clarification
prompt as the terminal success signal for that scenario.

## Common Incidents

### Slack does not answer

1. Check the launchd service state.
2. Check `gateway.log` for `Socket Mode connected`.
3. Confirm only one gateway process is connected to the `macros` Slack token.
4. Restart the local service.
5. Re-run one saved-topic prompt.

### Provider rate limit

Slack should send a short retry-later message instead of raw provider errors.
Wait for the provider reset or switch the profile to a provider with available
quota before re-running the acceptance prompts.

### Dashboard link opens with no data

1. Confirm the user is signed in and allowlisted.
2. For saved topics, run ops readiness and confirm `ANALYTICS_DATABASE_URL`.
3. For ad hoc result links, check whether the temporary result TTL expired.
4. Re-run the Slack question to generate a fresh visualization link.

### Wrong metric definition

Do not patch production logic silently. Ask Hermes to plan a source-of-truth
change, then make a tested repo change and PR against `claude-analytics`.

## Internal Launch Done Criteria

Internal launch is ready when:

- local launchd gateway is running and Slack Socket Mode is connected;
- ops readiness returns `ready`;
- live Slack E2E log checker returns `pass`;
- signed-in saved-topic and ad hoc dashboard links render rows;
- one restart has been tested and the gateway reconnects;
- team knows the local uptime limitation and hosted gateway remains the v2
  milestone.
