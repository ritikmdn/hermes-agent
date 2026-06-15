# Monica Agent

Monica is a mention-gated Slack agent for mobile frontend bug cleanup.

## Behavior

- Monica only starts when tagged in Slack.
- `approved_pr` code fixes are limited to marketplace copy/design bugs in the
  React Native mobile app.
- Clear mobile or frontend app bugs create or update Linear.
- Linear issues include observed behavior, expected behavior when inferable,
  reproduction context, platform/device/build clues, Slack context, and
  evidence links.
- Slack context preserves message authors, timestamps, per-message permalinks,
  thread permalink, file links, and attachment diagnostics.
- Ambiguous tagged messages ask for clarification.
- Code changes require explicit tagged approval in the same Slack thread.
- Draft PR creation happens only after configured verification commands pass
  and Monica has committed a real diff against the configured base branch.
- Draft PR bodies include the Linear link, Slack context, evidence links,
  Monica's worker summary, and captured verification output. PR publishing
  fails closed before git or GitHub side effects if Linear, Slack, or
  verification context is missing.

## Required Slack Scopes

- `app_mentions:read`
- `channels:history`
- `groups:history`
- `chat:write`
- `files:read`
- `files:write`

Generate a Slack App Manifest that matches these requirements with:

```bash
hermes mobile-bug-agent slack-manifest
```

Optional names are supported:

```bash
hermes mobile-bug-agent slack-manifest \
  --app-name "Monica" \
  --bot-display-name "monica"
```

Import that JSON in Slack's app manifest flow, install the app, invite Monica
to the allowed channels, then capture the bot user ID from a `<@U...>` mention
token and the channel IDs from Slack. If attachment downloads are disabled with
`mobile_bug_agent.slack.download_attachments: false`, the generated manifest
omits `files:read`; `files:write` remains required so Monica can upload proof
artifacts to the Slack thread in `approved_pr` mode.

After `MONICA_SLACK_BOT_TOKEN` is in the Monica profile `.env`, Monica can list
the bot user ID and visible channel IDs for you:

```bash
hermes mobile-bug-agent slack-metadata
```

For scripts or setup dashboards:

```bash
hermes mobile-bug-agent slack-metadata --json
```

Use the returned `auth.bot_user_id` value for `slack.bot_user_ids`; do not use
the `bot_id` value. Use selected channel IDs, such as `C...` or `G...`, for
`slack.allowed_channels`.

## Required Tokens

For Chandler separation, run Monica from her own Hermes profile/home and keep
her Slack tokens out of Chandler's `.env`. In a profile layout, store Monica
secrets in:

```text
~/.hermes/profiles/monica/.env
```

Use Monica-specific Slack names there:

```text
MONICA_SLACK_BOT_TOKEN=xoxb-...
MONICA_SLACK_APP_TOKEN=xapp-...
LINEAR_API_KEY=lin_api_...
GITHUB_TOKEN=github_pat_...
```

When `mobile_bug_agent.enabled: true`, the Monica profile gateway bridges
`MONICA_SLACK_BOT_TOKEN` and `MONICA_SLACK_APP_TOKEN` into the Slack adapter for
that process only. Chandler can keep using `SLACK_BOT_TOKEN` and
`SLACK_APP_TOKEN` in its own profile.

For the live dry run, the safe shape is:

```bash
mkdir -p ~/.hermes/profiles/monica
$EDITOR ~/.hermes/profiles/monica/.env
```

Then add Monica's two Slack tokens to that file:

```text
MONICA_SLACK_BOT_TOKEN=xoxb-monica...
MONICA_SLACK_APP_TOKEN=xapp-monica...
```

Run Monica setup and gateway commands with that profile home:

```bash
HERMES_HOME=~/.hermes/profiles/monica hermes mobile-bug-agent configure-dry-run \
  --bot-user-id U012ABCDEF \
  --channel-id C012ABCDEF
HERMES_HOME=~/.hermes/profiles/monica hermes mobile-bug-agent doctor --rollout-mode dry_run
HERMES_HOME=~/.hermes/profiles/monica hermes gateway
```

For Slack DMs, enable the App Home Messages tab, subscribe to the `message.im`
bot event, and grant `im:history` plus `im:read`. Monica treats 1:1 DMs as
explicit intent, but channel/private-channel messages still require `@monica`.

`GITHUB_TOKEN` is optional if `gh` is already authenticated in the environment that runs Hermes.

## Configuration

Enable the plugin first:

```bash
hermes plugins enable mobile-bug-agent
```

Or edit the profile config directly:

```yaml
plugins:
  enabled:
    - mobile-bug-agent
```

Then add this under `mobile_bug_agent` in `~/.hermes/config.yaml`:

```yaml
mobile_bug_agent:
  enabled: true
  rollout_mode: dry_run
  dry_run: true

  slack:
    allowed_channels: []
    bot_user_ids: []
    approver_user_ids: []
    download_attachments: true

  loop:
    create_linear: true
    require_fix_approval: true
    max_thread_messages: 40
    max_attachment_bytes: 15000000

  linear:
    team_id: ""
    project_id: ""
    label_ids: []

  repo:
    url: ""
    local_name: "mobile-app"
    default_branch: "main"
    branch_prefix: "monica"

  verification:
    commands:
      - "npm test"
      - "npm run lint"

  proof:
    enabled: false
    required_for_done: false
    platform_order:
      - ios
      - android
    artifact_dir: "proof"
    setup_commands: []
    commands: []
    required_env_keys: []
    deep_link: ""
    dev_client_scheme: ""
    ios_simulator_udid: ""
    ios_bundle_id: ""
    android_serial: ""
    android_avd: ""
    android_package: ""
    timeout_minutes: 10

  runtime:
    home_subdir: "agents/monica"
    worker_session_prefix: "monica"
    skip_memory: true

  worker:
    backend: "codex_cli"
    codex_command: "codex"
    codex_model: ""
    codex_profile: ""
    codex_sandbox: "workspace-write"
    codex_approval_policy: "never"
    timeout_minutes: 45
```

`slack.bot_user_ids` should contain Monica's Slack mention user ID, for example
`U012ABCDEF` from the `<@U012ABCDEF>` mention token. Do not use Slack's
`bot_id` value, which usually starts with `B`, handles like `@monica`, or
free-form names like `monica`. If `bot_user_ids` is empty, Monica only wakes
on Slack's explicit
`app_mention` event and ignores generic channel messages.
In `linear_only`, `local_fix_only`, and `approved_pr` modes, `bot_user_ids` is
required and enforced: an `app_mention` whose mention token does not match
Monica's configured user ID is ignored, and Slack `bot_id` values such as
`B012...` are rejected. Leave `bot_user_ids` empty only during dry-run setup
when relying on a dedicated Monica Slack app's `app_mention` events as the
temporary identity boundary.

`slack.allowed_channels` can stay empty during dry-run setup. Before enabling
`linear_only`, `local_fix_only`, or `approved_pr`, configure the exact Slack
channel IDs Monica may act in; the doctor command and runtime Slack hook fail
closed without that allowlist.

Monica stores her state, attachments, cloned repo, and worktrees under
`<active HERMES_HOME>/agents/monica` by default. In approved draft PR mode,
her default code worker is `codex exec`, rooted at the isolated mobile app
worktree with `--sandbox workspace-write` and `approval_policy="never"`.
The optional `internal_agent` backend uses `platform=monica` sessions with the
`monica-` prefix and skips Hermes memory by default, so it does not share
Chandler's conversation memory or ordinary chat sessions.

When `proof.required_for_done: true`, Monica treats simulator or device proof
as a hard completion gate. After code verification passes, she runs the
configured `proof.commands` inside the mobile worktree with
`MONICA_PROOF_DIR` pointing at her profile-owned proof directory. At least one
file must be written there, such as a screenshot or screen recording. If proof
is unavailable, the run stops at `proof_blocked`: Monica does not mark the run
done and does not open a PR.

For Stage D/E simulator proof, use Monica's bundled helper as the proof
command. It runs from the exact `$MONICA_WORKTREE`, uses the app's real
`npm run ios` / `npm run android` scripts, optionally opens
`MONICA_DEEP_LINK` for deterministic navigation, and captures platform
screenshots into `$MONICA_PROOF_DIR`:

```yaml
proof:
  enabled: true
  required_for_done: true
  platform_order: [ios, android]
  setup_commands:
    - 'npm run monica:seed-auth'
  required_env_keys:
    - MONICA_TEST_LOGIN_TOKEN
  commands:
    - 'uv run --project "$MONICA_HERMES_AGENT_ROOT" python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600'
```

`proof.setup_commands` run before screenshot capture and are the right place
for the mobile team's test-login, OTP bypass, or session seed command. Monica
passes `MONICA_WORKTREE`, `MONICA_PROOF_DIR`, `MONICA_LINEAR_IDENTIFIER`,
`MONICA_LINEAR_URL`, `MONICA_BRANCH_NAME`, `MONICA_BASE_REF`,
`MONICA_BASE_COMMIT`, `MONICA_DEEP_LINK`, `MONICA_PROOF_EXPECTED_TEXT`,
`MONICA_PROOF_SCREEN`, simulator IDs/packages, and credentials loaded from the
Monica profile `.env`.
No-op setup or proof commands such as `true` or `exit 0` do not satisfy these
gates.
For exact-screen proof, the worker summary must provide
`Monica proof deep link: <url>` and
`Monica proof expected text: <text visible on the fixed screen>`. If the
affected React Native route or screen name is known, it may also provide
`Monica proof screen: <route or screen name>`; Monica then carries that marker
through proof capture, the manifest, Linear context, and the draft PR gate.
List only operator-provided test-auth secret names in `proof.required_env_keys`;
Monica's built-in context variables such as `MONICA_WORKTREE` and
`MONICA_PROOF_DIR` do not count as proof credentials.
Keep secrets such as test login tokens in the profile `.env`, not in
`config.yaml`.

The proof runner writes `monica-proof-manifest.json` after screenshot/video
artifacts exist. The manifest records the Monica run ID, Linear issue and URL,
branch, base ref/commit, worktree path, selected platforms, setup/proof
commands, proof target, and proof artifact paths so reviewers can confirm the
proof came from Monica's fixed branch.

## Rollout Modes

1. Dry run: set `rollout_mode: dry_run` and keep `dry_run: true`. Monica captures context and shows what she would file.
2. Linear only: set `rollout_mode: linear_only` and `dry_run: false`. Monica creates/updates Linear and stops there; she does not ask for code approval in this mode.
3. Local fix only: set `rollout_mode: local_fix_only`, configure `repo.url`, verification commands, and Slack approvers. Monica writes and verifies a local worktree branch after approval, but never pushes or opens a PR.
4. Approved draft PRs: set `rollout_mode: approved_pr`, configure `repo.url`, verification commands, proof setup/proof commands, `gh` auth, and Slack approvers. The non-secret parts can be saved with `hermes mobile-bug-agent configure-approved-pr --proof-setup-command '<auth/session seed command>' --proof-required-env-key '<test-auth env key>' --proof-command '<simulator proof command>'`; keep credentials in the Monica profile `.env`. If only the test-auth/session seed command changes later, update just that piece with `hermes mobile-bug-agent configure-proof-setup --proof-setup-command '<auth/session seed command>' --proof-required-env-key '<test-auth env key>'`.

In code-writing modes, Monica prepares a separate worktree under the active
Hermes profile, asks Codex CLI to make the smallest safe React Native fix, runs
the configured verification commands, and refuses no-op branches. In
`local_fix_only`, she stops with the verified local branch. In `approved_pr`,
she first refuses requests outside Monica's marketplace copy/design scope, then
commits any dirty worktree changes with the PR title, pushes, and opens a draft
PR. When proof is required, both modes must capture proof before they can
complete.

In `approved_pr`, the draft PR body is also proof-gated: it must include the
Linear link, Slack context, Monica summary, verification output, and proof
artifacts. The PR publisher refuses to push/create a draft PR if that evidence
is absent.

`slack.approver_user_ids` must also use Slack user IDs such as `U012ABCDEF`.
Do not use handles or display names there; Monica treats that list as the
explicit allowlist for starting code-writing work.
In `approved_pr`, Monica re-checks that allowlist before code work and before
retrying proof from an existing proof-blocked branch.
Approved-PR code work also re-checks that the stored Slack event came from an
`app_mention`, a 1:1 DM, or a text mention of Monica's configured Slack bot user
ID before the worker can touch the mobile repo. Configure `slack.bot_user_ids`
for side-effect rollouts so doctor can prove generic Slack message events are
mention-gated; explicit `app_mention` and 1:1 DM events are still accepted by
the runtime as direct Monica intake.

## Live Rollout Worksheet

Collect these values before moving Monica beyond local simulation:

- Monica Slack app token for Socket Mode: `MONICA_SLACK_APP_TOKEN`
- Monica Slack bot token: `MONICA_SLACK_BOT_TOKEN`
- Monica Slack mention user ID from `slack-metadata --json`: `auth.bot_user_id`
- Private dry-run Slack channel ID, and later allowed live-action channel IDs:
  `C...` or `G...`
- Linear API token: `LINEAR_API_KEY`
- Linear team ID, and optionally project and label IDs
- React Native repo Git URL and default branch
- Verification commands that must pass before draft PR creation
- Proof command that captures simulator/device evidence into `MONICA_PROOF_DIR`
- Proof setup/auth command that seeds simulator login before capture
- Slack user IDs allowed to approve code work
- Attachment policy: download Slack files locally, or only reference Slack URLs

The rollout should move in phases:

1. Dry-run Slack: Monica can read tagged Slack threads and reply, but no Linear,
   code, git, or GitHub side effects occur.
2. Linear-only: Monica files or updates Linear issues in explicitly allowed
   channels and stops before code work.
3. Local fix only: Monica writes code only after tagged approval from an
   allowed approver, runs verification, requires proof when configured, and
   stops before push or PR creation.
4. Approved draft PR: Monica writes code only after tagged approval from an
   allowed approver, runs verification, requires proof when configured, pushes,
   and opens a draft PR.

Run the readiness check before each rollout step:

```bash
hermes mobile-bug-agent doctor
```

For the first private Slack dry run, persist the non-secret bootstrap settings.
The Slack scope flags are optional, but using them keeps even dry-run traffic
limited to Monica's real mention user ID and the private test channel. When
provided, Monica ignores other app mentions and refuses tagged messages outside
that channel. `doctor` rejects handles, Slack `bot_id` values, malformed user
IDs, and channel names before the gateway starts:

```bash
hermes mobile-bug-agent configure-dry-run \
  --bot-user-id U012ABCDEF \
  --channel-id C012ABCDEF
```

To turn readiness failures into ordered setup actions, use:

```bash
hermes mobile-bug-agent setup-plan --rollout-mode dry_run
```

Then check the next gate:

```bash
hermes mobile-bug-agent setup-plan --rollout-mode linear_only
```

For automation:

```bash
hermes mobile-bug-agent setup-plan --rollout-mode dry_run --json
hermes mobile-bug-agent setup-plan --rollout-mode linear_only --json
```

To preview the next rollout gate before editing config, pass the target mode:

```bash
hermes mobile-bug-agent doctor --rollout-mode linear_only
```

For automation, add `--json`. The JSON form includes stable readiness codes
and keeps the same nonzero exit code when Monica is not ready:

```bash
hermes mobile-bug-agent doctor --rollout-mode approved_pr --json
```

For `approved_pr`, the gateway is part of readiness. A stopped gateway, missing
gateway status, stale heartbeat, missing heartbeat, incomplete runtime metadata,
or manually running gateway process means Monica cannot prove live Slack tag/DM
intake and approvals are running under a durable supervisor on the current
runtime, so `doctor` reports a blocking gateway failure for approved draft PR
rollout. `setup-plan --rollout-mode approved_pr` turns stopped or manual
gateway failures into a supervised `hermes -p monica gateway install` step, and
stale or incomplete heartbeat failures into `hermes -p monica gateway restart`
steps.
It also maps missing, placeholder, no-op, or inline-secret proof setup/capture
commands to the relevant proof configuration step instead of telling the
operator to rerun doctor without a repair.

Once the Slack and Linear IDs are known, persist the non-secret Linear-only
settings with:

```bash
hermes mobile-bug-agent configure-linear-only \
  --bot-user-id U012ABCDEF \
  --channel-id C012ABCDEF \
  --linear-team-id <linear-team-id>
```

Repeat `--channel-id` for each allowed Slack channel. Optional
`--linear-project-id` and repeated `--linear-label-id` flags are supported.
Secrets such as `MONICA_SLACK_BOT_TOKEN` and `LINEAR_API_KEY` stay in the
Monica profile `.env`.

To discover Linear team, project, and label IDs from the configured
`LINEAR_API_KEY`, run:

```bash
hermes mobile-bug-agent linear-metadata
```

For scripts or setup dashboards:

```bash
hermes mobile-bug-agent linear-metadata --json
```

When the React Native repo, approvers, and verification commands are known,
persist the non-secret local-fix settings with:

```bash
hermes mobile-bug-agent configure-local-fix-only \
  --approver-user-id U012ABCDEF \
  --repo-url git@github.com:acme/mobile-app.git \
  --verification-command "npm test" \
  --verification-command "npm run lint"
```

To require proof in this mode, set `proof.enabled: true`,
`proof.required_for_done: true`, and add at least one `proof.commands` entry in
the Monica profile config. Until the simulator command is available and writes
an artifact, Monica will verify the code and then stop at `proof_blocked`.

When GitHub push/PR tooling is ready, persist the non-secret approved-PR
settings with:

```bash
hermes mobile-bug-agent configure-approved-pr \
  --approver-user-id U012ABCDEF \
  --repo-url git@github.com:acme/mobile-app.git \
  --verification-command "npm test" \
  --verification-command "npm run lint" \
  --proof-setup-command "npm run monica:seed-auth" \
  --proof-command 'uv run --project "$MONICA_HERMES_AGENT_ROOT" python -m plugins.mobile_bug_agent.simulator_proof --timeout-seconds 600' \
  --proof-required-env-key MONICA_TEST_LOGIN_TOKEN \
  --proof-dev-client-scheme elixir-card \
  --proof-ios-bundle-id com.elixir.card \
  --proof-android-package com.joinelixir.elixirclub \
  --proof-android-avd MonicaPixel
```

If doctor later reports only `proof.setup_commands` as missing, placeholder,
no-op, or inline-secret, repair that setting without restating the whole
rollout:

```bash
hermes mobile-bug-agent configure-proof-setup \
  --proof-setup-command "npm run monica:seed-auth" \
  --proof-required-env-key MONICA_TEST_LOGIN_TOKEN
```

Once `proof.required_env_keys` already contains valid key names, the same
command can replace just the setup command; Monica preserves the existing
required env-key list unless new `--proof-required-env-key` values are passed.

Optional `--repo-local-name`, `--default-branch`, and `--branch-prefix` flags
are supported. Optional proof environment flags include
`--proof-required-env-key`, `--proof-ios-simulator-udid`,
`--proof-android-serial`, `--proof-timeout-minutes`, and
`--proof-artifact-dir`. Set
`proof.required_env_keys` for any test-auth secrets the simulator proof must
receive from the Monica profile `.env`; Monica exports them to the built-in
simulator harness through `MONICA_REQUIRED_ENV_KEYS` and fails readiness if a
configured key is missing or names Monica's built-in proof context instead.
Monica refuses handles,
unsafe repo names, unsafe branch names, and Chandler-like branch prefixes before
saving. GitHub credentials and test-login/session secrets still stay outside
config, either through `gh` auth, `GITHUB_TOKEN`, or the Monica profile `.env`.
This command also restores Monica-owned runtime defaults and the
non-interactive Codex guardrails required for approved PR work:
`worker_session_prefix: monica`, `skip_memory: true`,
`codex_sandbox: workspace-write`, and `codex_approval_policy: never`.

Before inviting Monica into Slack channels, run a local simulation against the
same Monica state machine. Simulation is safe by default: in `linear_only` or
`approved_pr` modes it refuses to run unless side effects are explicitly allowed.

```bash
hermes mobile-bug-agent simulate "checkout crashes on Android after applying a promo code"
```

When `--thread-ts` is omitted, each simulation gets a unique local Slack-shaped
thread ID. Pass `--thread-ts` only when you intentionally want to reuse or test
a specific simulated thread.

To deliberately exercise real Linear or approved-PR side effects from a local
simulation, pass the explicit operator gate:

```bash
hermes mobile-bug-agent simulate "checkout crashes on Android" --allow-side-effects
```

Side-effect simulations run the same readiness checks as `doctor` before
creating a Monica run, so missing Linear/GitHub/Codex/Slack setup stops before
state or external services are touched.

Code-writing simulations additionally require configured
`slack.approver_user_ids`; otherwise the simulation stops before creating a run.
Slack approvals use the same shared fail-closed readiness policy: Monica replies without
approving the run when code rollout configuration, runtime secrets, or worker
tools are not ready, or when the worker session is not clearly Monica-owned.

## Slack Examples

```text
@monica checkout crashes on Android after applying a promo code. Screenshot above.
@monica can you turn this thread into a mobile bug?
@monica approved, fix it
@monica yes, take the fix
@monica go ahead
@monica cancel
```

Untagged messages are ignored.

## Operator CLI

```bash
hermes mobile-bug-agent status
hermes mobile-bug-agent status --json
hermes mobile-bug-agent doctor
hermes mobile-bug-agent configure-dry-run
hermes mobile-bug-agent configure-dry-run --bot-user-id U012ABCDEF --channel-id C012ABCDEF
hermes mobile-bug-agent configure-linear-only --bot-user-id U012ABCDEF --channel-id C012ABCDEF --linear-team-id <team-id>
hermes mobile-bug-agent configure-local-fix-only --approver-user-id U_APPROVER --repo-url <repo-url> --verification-command "npm test"
hermes mobile-bug-agent configure-approved-pr --approver-user-id U_APPROVER --repo-url <repo-url> --verification-command "npm test" --proof-setup-command "<auth/session seed command>" --proof-required-env-key "<test-auth env key>" --proof-command "<simulator proof command>"
hermes mobile-bug-agent configure-proof-setup --proof-setup-command "<auth/session seed command>" --proof-required-env-key "<test-auth env key>"
hermes mobile-bug-agent show <run_id>
hermes mobile-bug-agent show <run_id> --json
hermes mobile-bug-agent retry <run_id>
hermes mobile-bug-agent retry <run_id> --json
hermes mobile-bug-agent approve <run_id> --user-id U_APPROVER
hermes mobile-bug-agent approve <run_id> --user-id U_APPROVER --json
hermes mobile-bug-agent simulate "checkout crashes on Android"
hermes mobile-bug-agent simulate "checkout crashes on Android" --json
hermes mobile-bug-agent simulate "checkout crashes on Android" --allow-side-effects
```

`approve` is an operator escape hatch for the same approval gate used in
Slack. The `--user-id` value must be one of
`mobile_bug_agent.slack.approver_user_ids`; otherwise Monica refuses to start
the code-writing loop. In approved-PR mode, local approval also runs the
readiness checks before changing the run to approved. Retrying a run that still
has stored approval also runs readiness checks before relaunching code work.

`retry` can resume blocked, failed, or clarification-needed runs from the last
safe gate. Runs cancelled from Slack stay cancelled; tag Monica again in the
Slack thread with new context to start a fresh pass.

`status` prints compact rollout context for recent runs: run ID, status, Slack
channel/thread, Linear identifier, branch name, PR or Linear URL, and request
text. `status --json` and `show <run_id> --json` provide machine-readable run
payloads for dashboards and rollout scripts. `retry --json` and
`approve --json` return `{ok, action, run}` on success and `{ok, action, error}`
with a stable error code on refusal; when a run exists, the refusal payload also
includes the current run. `simulate --json` returns the created run payload so
dry-run rehearsals can be chained into `show` or dashboard checks; if the
simulated thread already exists, it returns
`created: false`, a `duplicate_thread` error code, and the existing run payload
while preserving the nonzero exit code. Early simulation failures, such as
disabled config, missing simulation text, missing `--allow-side-effects`,
missing allowed channels, missing Monica Slack user IDs, disallowed channels,
missing approved-PR approvers, readiness failures, or readiness exceptions,
also return structured JSON errors when `--json` is used.

`status` also reports Hermes runtime-sync readiness. Monica blocks Hermes
self-update while any non-terminal run exists, including queued intake,
awaiting approval, approved work, fixing, verifying, proofing, proof-blocked
retry state, or opening a PR. Only terminal/resting runs such as `done`,
`blocked`, `failed`, and `needs_clarification` are considered idle for runtime
sync.

`proof_blocked` means the code worker and verification completed, but Monica
could not capture the required screenshot or recording artifact. Fix the local
simulator/tooling or `proof.commands`, then retry the run.

Monica loop logs use the `plugins.mobile_bug_agent.loop` logger and include
the run ID, Slack channel/thread, Linear issue, branch, PR URL, status, stage,
and failure reason when available.
