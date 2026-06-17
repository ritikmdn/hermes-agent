from __future__ import annotations

import os
from collections.abc import Mapping

MONICA_SLACK_BOT_TOKEN = "MONICA_SLACK_BOT_TOKEN"
MONICA_SLACK_APP_TOKEN = "MONICA_SLACK_APP_TOKEN"
MONICA_LINEAR_API_KEY = "MONICA_LINEAR_API_KEY"
MONICA_GITHUB_TOKEN = "MONICA_GITHUB_TOKEN"


def secret_value(env: Mapping[str, str] | None, key: str) -> str:
    source = env if env is not None else os.environ
    return str(source.get(key) or "").strip()


def monica_slack_bot_token(env: Mapping[str, str] | None = None) -> str:
    return secret_value(env, MONICA_SLACK_BOT_TOKEN)


def monica_slack_app_token(env: Mapping[str, str] | None = None) -> str:
    return secret_value(env, MONICA_SLACK_APP_TOKEN)
