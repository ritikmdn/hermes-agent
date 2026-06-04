# Elixir Analytics Agent Roadmap

This roadmap is the operating runbook for the `elixir-analytics` Hermes
profile. Keep it aligned after each execution pass so Slack behavior, the
analytics repo, and the executive Next.js dashboard move through the same
product milestones.

Last updated: 2026-06-04 19:25 IST.

## Product Shape

```text
Slack app: macros
  -> Hermes profile: elixir-analytics
  -> claude-analytics deterministic runner or planner
  -> saved topic, Supabase ad hoc, PostHog ad hoc, clarification, or source-change workflow
  -> Slack answer with assumptions, metadata, and optional dashboard link
  -> executive Next.js dashboard or no-persistence query visualization
  -> query log and self-improvement review
```

Hermes owns AI reasoning, Slack conversation, approvals, profile routing,
source-change orchestration, and self-improvement. `claude-analytics` owns
metric contracts, deterministic query runners, read-only validation, dashboard
rendering, release checks, and production deployment.

## Current Status Snapshot

| Area | Status | Evidence |
|---|---|---|
| Slack profile routing | Working | Slack app `macros` reaches the launchd-managed `elixir-analytics` gateway. Socket Mode connected in `logs/gateway.log`. |
| Hermes provider | Working | Live profile uses `openai-codex` / `gpt-5.5`; Slack turns complete. |
| Generic tools | Enabled | Smart approvals and generic tools remain available for debugging and source changes. |
| Supabase saved topic | Working | `show GTV last 30 days by week` routed to `card-gtv-weekly` through `answer_question`, returned a dashboard link, 2 model calls, 30.0s gateway time. |
| Supabase ad hoc | Working and fast for the key shortcut | `which users spent on Swiggy this week?` routed to `supabase_ad_hoc` shortcut `swiggy_users_this_week`, returned 15 rows, dashboard link, 2 model calls, 9.8s gateway time. |
| Production dashboard | Working for GTV link | `https://analytics.joinelixir.club/query?topic=card-gtv-weekly&range=30d` was rechecked after the Vercel env fix and now loads data. |
| Active-user ambiguity | Working for first step | `active users this week` asks whether to use card, app, or combined active users before querying. |
| Write-SQL rejection | Working | `delete from profiles` was refused in Slack with read-only alternatives, 3 model calls, 17.4s gateway time. |
| Row cap/truncation | Covered by runner smoke suite | `row_cap_truncation` in `scripts/run-analytics-smoke-suite.ts` proves large ad hoc output is bounded and returns `truncated: true`. |
| PostHog | Built, but fresh fast Slack evidence pending | Earlier live app-active run was functionally correct but too slow and used generic tooling; needs a fresh post-direct-answer Slack pass. |
| Source-change workflow | Built | Runner modes `source_change_plan` and `source_change_scope_check` exist and have live direct checks. |
| Self-improvement loop | Built | Runner mode `self_improvement_plan` exists; query-log cadence tooling exists. |
| Hermes PR | Open draft | `NousResearch/hermes-agent#39041`, branch `ritikmdn:codex/elixir-analytics-profile`. Current local direct-answer updates still need commit/push. |
| Analytics PR | Merged/deployed | Runtime/dashboard fixes are deployed to `analytics.joinelixir.club`; current work here is Hermes-side. |

## Fresh Slack Evidence

| Prompt | Expected product behavior | Current evidence | Status |
|---|---|---|---|
| `show GTV last 30 days by week` | Saved topic `card-gtv-weekly`, dashboard link with data | 2026-06-04 19:11 IST: runner route `saved_topic`, shortcut `card_gtv_weekly_30d`, 2 calls, 30.0s, 497 chars | Pass |
| `which users spent on Swiggy this week?` | Supabase ad hoc shortcut, India week-to-date, user rows, dashboard link | 2026-06-04 19:10 IST: runner route `supabase_ad_hoc`, shortcut `swiggy_users_this_week`, 15 rows, 2 calls, 9.8s, 4845 chars | Pass |
| `active users this week` | Clarify before querying | 2026-06-04 19:12 IST: Slack reply asked card/app/combined active users before executing a metric query | Pass for clarification |
| `delete from profiles` | Refuse destructive SQL and offer safe alternatives | 2026-06-04 19:16 IST: Slack reply refused `DELETE FROM profiles`, 3 calls, 17.4s | Pass |
| `how many app active users this week?` | PostHog ad hoc shortcut, no generic shell path | Fresh post-direct-answer Slack pass pending | Pending |
| Large ad hoc result | Bound rows and mark `truncated: true` | Covered in smoke suite via `row_cap_truncation` | Pass by automated runner evidence |

## Current Gaps

- PostHog needs a fresh live Slack recheck after the direct-answer and compact
  skill-view changes. The old evidence is functionally correct but too slow.
- Clarified follow-up ranking questions can still drift into generic
  file/search/tool work. Example: a separate "Who spent the most in last 30
  days?" thread completed but took 189.1s and 19 model calls. This belongs in
  answer-polish and deterministic shortcut expansion.
- `scripts/check-slack-e2e-logs.ts` in `claude-analytics` is stale relative to
  the current runner-first rule. It still expects no runner call for
  clarification/rejection scenarios, but Hermes now intentionally calls
  `answer_question` first for plain analytics questions. The checker should be
  updated in the analytics repo.
- The Hermes direct-final response update is local and still needs full focused
  verification, commit, and push to PR #39041.
- The gateway is locally supervised, not yet hosted in an always-on production
  environment.

## Milestones

| Phase | Milestone | Product outcome | Status |
|---|---|---|---|
| 1 | Profile foundation | Slack `macros` uses the isolated analytics profile. | Done |
| 2 | Saved query vertical slice | Known questions such as weekly GTV use saved topics and dashboard links. | Done |
| 3 | Supabase ad hoc runner | Arbitrary Supabase analytics questions use a read-only JSON contract. | Done |
| 4 | Query-specific visualization handoff | Ad hoc results can link to temporary no-persistence dashboard payloads. | Built; core production saved-topic link verified |
| 5 | PostHog runner | Product analytics route to read-only HogQL with separate definitions. | Built; fresh fast Slack pass pending |
| 6 | Source-of-truth workflow | Definition/glossary/query changes become scoped repo changes and PRs. | Built; first real source-change PR still pending |
| 7 | Self-improvement loop | Repeated questions become saved-topic, glossary, and dashboard candidates. | Built; cadence/live usage pending |
| 8 | Ops readiness | Deployment blockers, gateway health, and env readiness are inspectable. | Local checks built; hosted gateway pending |
| 9 | Slack smoke suite | Core scenarios are covered by deterministic tests/smoke tooling. | Built; log checker needs rule alignment |
| 10 | Production deploy | Analytics app is merged, deployed, and env-backed. | Done for current GTV dashboard path |
| 11 | Slack end-to-end validation | Real Slack proves saved topic, Supabase ad hoc, clarification, rejection, and PostHog. | Mostly done; PostHog fresh pass pending |
| 12 | Hosted gateway | Slack gateway is reliable beyond the local machine. | Pending product/infra decision |
| 13 | Hermes upstream sync | Profile distribution and runtime changes are pushed upstream. | PR open; current patch pending push |
| 14 | Answer polish | Common questions respond fast, compactly, and with useful links. | Supabase improved; PostHog and ranking shortcuts pending |
| 15 | Operating cadence | Query-log and self-improvement review run regularly. | Tooling built; cadence not operationalized |

## Immediate Goal

Finish the current Hermes PR update.

Done means:

1. The roadmap reflects the current product state.
2. Hermes focused tests pass for profile distribution, runner plugin, direct
   final-response behavior, and release packaging.
3. The current direct-answer/profile/test changes are committed and pushed to
   `ritikmdn:codex/elixir-analytics-profile`.
4. The live gateway remains connected after any restart needed for sync.

## Next Product Milestone

Milestone 11B: finish fresh Slack acceptance.

Done means:

1. `how many app active users this week?` uses the PostHog shortcut path from
   Slack with low call count and no generic shell path.
2. `scripts/check-slack-e2e-logs.ts` is updated so current runner-first
   behavior counts as valid for clarification and destructive-query rejection.
3. The log checker passes on fresh Slack evidence or reports only intentionally
   deferred scenarios.

Milestone 14E: add deterministic ranking shortcuts.

Done means:

1. "Who spent the most in last 30 days?" maps to a bounded Supabase ad hoc
   request without broad file/search exploration.
2. The Slack answer includes top users, spend, count, date window, and optional
   dashboard link within the agreed latency target.

Milestone 12A: choose and implement hosted gateway.

Done means:

1. The gateway is running on a stable host instead of relying on the local Mac.
2. Secrets are scoped to the `macros` analytics Slack app/profile.
3. Restart health, logs, and rollback are documented.

## Standard Verification

From `/Users/ritik/.hermes/hermes-agent`:

```bash
scripts/run_tests.sh tests/hermes_cli/test_elixir_analytics_profile_distribution.py tests/hermes_cli/test_elixir_analytics_runner_plugin.py tests/hermes_cli/test_elixir_analytics_release_packaging.py tests/gateway/test_slack.py tests/plugins/test_disk_cleanup_plugin.py tests/test_toolsets.py -- -q
HERMES_HOME=/private/tmp/hermes-agent-test venv/bin/python -m pytest tests/run_agent/test_run_agent.py -q -k direct_final_response
venv/bin/python scripts/check_elixir_analytics_release_packaging.py --strict
```

From `/Users/ritik/Coding/claude-analytics` after analytics changes:

```bash
npm run lint
npm test
npm run build
node --import tsx scripts/run-analytics-smoke-suite.ts --query-log QUERY_LOG.md --current-branch main --env-file /Users/ritik/.hermes/profiles/elixir-analytics/.env --provider-authenticated openai-codex --smart-approvals --generic-tools
node --import tsx scripts/check-slack-e2e-logs.ts --gateway-log /Users/ritik/.hermes/profiles/elixir-analytics/logs/gateway.log --agent-log /Users/ritik/.hermes/profiles/elixir-analytics/logs/agent.log
```

## Open Decisions

- Hosted gateway target: Vercel service, small VPS, Render/Fly, or another
  always-on host.
- Product default for ambiguous active users: always clarify, or choose a
  default such as card active users.
- Slack latency targets. Current suggested target: under 15s for deterministic
  shortcuts, under 45s for broader ad hoc questions.
