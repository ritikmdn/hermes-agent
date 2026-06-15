from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


_MENTION_RE = re.compile(r"<@([A-Z0-9_]+)(?:\|[^>]+)?>")
_SLACK_BLOCK_KIT_PAYLOAD_MARKER = "[Slack Block Kit payload for this message]"
_CHATGPT_FOOTER_RE = re.compile(r"\s*\*Sent using\*\s+ChatGPT\s*$", re.I)


class SlackClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlackMessage:
    user_id: str
    text: str
    ts: str
    permalink: str = ""


@dataclass(frozen=True)
class SlackFile:
    id: str
    name: str
    mimetype: str
    url_private: str
    permalink: str = ""
    local_path: str = ""
    error: str = ""
    requires_auth: bool = False


@dataclass(frozen=True)
class SlackUploadedFile:
    id: str
    name: str
    permalink: str


@dataclass(frozen=True)
class SlackThreadContext:
    channel_id: str
    thread_ts: str
    permalink: str
    messages: list[SlackMessage]
    files: list[SlackFile]

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
            "permalink": self.permalink,
            "messages": [f"{msg.user_id}: {msg.text}" if msg.user_id else msg.text for msg in self.messages],
            "message_details": [asdict(message) for message in self.messages],
            "files": [asdict(file) for file in self.files],
            "attachments": [file.local_path or file.name for file in self.files],
        }


@dataclass(frozen=True)
class SlackAuthInfo:
    bot_user_id: str
    bot_id: str
    team_id: str
    team_name: str
    team_url: str


@dataclass(frozen=True)
class SlackChannelInfo:
    id: str
    name: str
    is_private: bool
    is_member: bool
    is_archived: bool


@dataclass(frozen=True)
class SlackWorkspaceMetadata:
    auth: SlackAuthInfo
    channels: tuple[SlackChannelInfo, ...]


class SlackThreadClient:
    def __init__(
        self,
        *,
        client: Any,
        token: str = "",
        monica_user_ids: tuple[str, ...] = (),
        download_dir: str | Path | None = None,
        download_attachments: bool = True,
        max_attachment_bytes: int = 15_000_000,
    ) -> None:
        self.client = client
        self.token = token.strip()
        self.monica_user_ids = set(monica_user_ids)
        self.download_dir = Path(download_dir) if download_dir else None
        self.download_attachments = download_attachments
        self.max_attachment_bytes = max_attachment_bytes

    @classmethod
    def from_token(
        cls,
        *,
        token: str,
        monica_user_ids: tuple[str, ...] = (),
        download_dir: str | Path | None = None,
        download_attachments: bool = True,
        max_attachment_bytes: int = 15_000_000,
    ) -> "SlackThreadClient":
        try:
            from slack_sdk import WebClient
        except Exception as exc:  # pragma: no cover - depends on optional install
            raise SlackClientError("slack_sdk is not installed.") from exc
        return cls(
            client=WebClient(token=token),
            token=token,
            monica_user_ids=monica_user_ids,
            download_dir=download_dir,
            download_attachments=download_attachments,
            max_attachment_bytes=max_attachment_bytes,
        )

    def read_thread(self, *, channel_id: str, thread_ts: str, limit: int = 40) -> SlackThreadContext:
        messages: list[SlackMessage] = []
        files: list[SlackFile] = []
        cursor = ""
        seen_cursors: set[str] = set()
        max_messages = max(1, limit)

        while len(messages) < max_messages:
            response = self._conversations_replies(
                channel_id=channel_id,
                thread_ts=thread_ts,
                limit=max_messages - len(messages),
                cursor=cursor,
            )
            if _response_get(response, "ok") is False:
                error = str(_response_get(response, "error") or "unknown_error")
                raise SlackClientError(f"Slack conversations_replies failed: {error}")
            messages_payload = _response_get(response, "messages") or []
            for payload in messages_payload:
                if len(messages) >= max_messages:
                    break
                if not isinstance(payload, dict):
                    continue
                text = self._normalize_text(str(payload.get("text") or ""))
                message_ts = str(payload.get("ts") or "")
                messages.append(
                    SlackMessage(
                        user_id=str(payload.get("user") or payload.get("bot_id") or ""),
                        text=text,
                        ts=message_ts,
                        permalink=self._message_permalink(
                            channel_id=channel_id,
                            message_ts=message_ts,
                        ),
                    )
                )
                for raw_file in payload.get("files") or []:
                    if isinstance(raw_file, dict):
                        files.append(self._file_from_payload(raw_file))
                for attachment_file in self._files_from_message_attachments(
                    payload.get("attachments") or [],
                    message_ts=message_ts,
                ):
                    files.append(attachment_file)

            cursor = _response_next_cursor(response)
            if not cursor or not _response_get(response, "has_more"):
                break
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)

        return SlackThreadContext(
            channel_id=channel_id,
            thread_ts=thread_ts,
            permalink=messages[0].permalink if messages else "",
            messages=messages,
            files=files,
        )

    def post_thread_reply(self, *, channel_id: str, thread_ts: str, text: str) -> None:
        clean_text = str(text or "").strip()
        if not clean_text:
            raise SlackClientError("Slack reply text is required.")
        response = self.client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=clean_text,
        )
        if _response_get(response, "ok") is False:
            error = str(_response_get(response, "error") or "unknown_error")
            raise SlackClientError(f"Slack chat_postMessage failed: {error}")

    def upload_thread_file(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        file_path: str,
        title: str,
        initial_comment: str = "",
    ) -> SlackUploadedFile:
        path = Path(file_path)
        if not path.is_file():
            raise SlackClientError(f"Slack upload file does not exist: {path}")
        clean_title = str(title or path.name).strip() or path.name
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "file": str(path),
            "title": clean_title,
        }
        comment = str(initial_comment or "").strip()
        if comment:
            kwargs["initial_comment"] = comment
        uploader = getattr(self.client, "files_upload_v2", None)
        if callable(uploader):
            response = uploader(**kwargs)
        else:
            legacy_uploader = getattr(self.client, "files_upload", None)
            if not callable(legacy_uploader):
                raise SlackClientError("Slack client does not support file uploads.")
            legacy_kwargs = dict(kwargs)
            legacy_kwargs["channels"] = legacy_kwargs.pop("channel")
            response = legacy_uploader(**legacy_kwargs)
        if _response_get(response, "ok") is False:
            error = str(_response_get(response, "error") or "unknown_error")
            raise SlackClientError(f"Slack file upload failed: {error}")
        payload = _uploaded_file_payload(response)
        if not payload:
            raise SlackClientError("Slack file upload did not return file metadata.")
        permalink = str(payload.get("permalink_public") or payload.get("permalink") or "")
        if not permalink:
            raise SlackClientError("Slack file upload did not return a permalink.")
        return SlackUploadedFile(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or payload.get("title") or path.name),
            permalink=permalink,
        )

    def list_workspace_metadata(self, *, limit: int = 200) -> SlackWorkspaceMetadata:
        auth_response = self.client.auth_test()
        if _response_get(auth_response, "ok") is False:
            error = str(_response_get(auth_response, "error") or "unknown_error")
            raise SlackClientError(f"Slack auth_test failed: {error}")
        auth = SlackAuthInfo(
            bot_user_id=str(_response_get(auth_response, "user_id") or ""),
            bot_id=str(_response_get(auth_response, "bot_id") or ""),
            team_id=str(_response_get(auth_response, "team_id") or ""),
            team_name=str(_response_get(auth_response, "team") or ""),
            team_url=str(_response_get(auth_response, "url") or ""),
        )
        channels: list[SlackChannelInfo] = []
        cursor = ""
        seen_cursors: set[str] = set()
        page_limit = max(1, min(int(limit), 1000))
        while True:
            kwargs: dict[str, Any] = {
                "types": "public_channel,private_channel",
                "exclude_archived": True,
                "limit": page_limit,
            }
            if cursor:
                kwargs["cursor"] = cursor
            response = self.client.conversations_list(**kwargs)
            if _response_get(response, "ok") is False:
                error = str(_response_get(response, "error") or "unknown_error")
                raise SlackClientError(f"Slack conversations_list failed: {error}")
            for payload in _response_get(response, "channels") or []:
                if not isinstance(payload, dict) or not payload.get("id"):
                    continue
                channels.append(
                    SlackChannelInfo(
                        id=str(payload.get("id") or ""),
                        name=str(payload.get("name") or ""),
                        is_private=bool(payload.get("is_private")),
                        is_member=bool(payload.get("is_member")),
                        is_archived=bool(payload.get("is_archived")),
                    )
                )
            cursor = _response_next_cursor(response)
            if not cursor or not _response_get(response, "has_more"):
                break
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return SlackWorkspaceMetadata(auth=auth, channels=tuple(channels))

    def _conversations_replies(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        limit: int,
        cursor: str = "",
    ) -> Any:
        kwargs: dict[str, Any] = {"channel": channel_id, "ts": thread_ts, "limit": limit}
        if cursor:
            kwargs["cursor"] = cursor
        return self.client.conversations_replies(**kwargs)

    def _file_from_payload(self, payload: dict[str, Any]) -> SlackFile:
        slack_file = SlackFile(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or payload.get("title") or "slack-file"),
            mimetype=str(payload.get("mimetype") or ""),
            url_private=str(payload.get("url_private_download") or payload.get("url_private") or ""),
            permalink=str(payload.get("permalink_public") or payload.get("permalink") or ""),
            requires_auth=True,
        )
        if not self.download_attachments or not self.download_dir or not slack_file.url_private:
            return slack_file
        return self._download_file(slack_file)

    def _files_from_message_attachments(
        self,
        attachments: Any,
        *,
        message_ts: str,
    ) -> list[SlackFile]:
        files: list[SlackFile] = []
        if not isinstance(attachments, list):
            return files
        for index, payload in enumerate(attachments, start=1):
            if not isinstance(payload, dict):
                continue
            image_url = str(
                payload.get("image_url")
                or payload.get("thumb_url")
                or payload.get("url")
                or ""
            ).strip()
            if not image_url:
                continue
            title = str(
                payload.get("title")
                or payload.get("fallback")
                or payload.get("text")
                or _safe_filename(image_url.rsplit("/", 1)[-1])
                or "Slack attachment"
            ).strip()
            slack_file = SlackFile(
                id=f"attachment-{message_ts or 'message'}-{index}",
                name=title or "Slack attachment",
                mimetype="image" if payload.get("image_url") or payload.get("thumb_url") else "",
                url_private=image_url,
                permalink=image_url,
                requires_auth=False,
            )
            if self.download_attachments and self.download_dir:
                slack_file = self._download_file(slack_file)
            files.append(slack_file)
        return files

    def _download_file(self, slack_file: SlackFile) -> SlackFile:
        scheme = urlparse(slack_file.url_private).scheme.lower()
        if scheme not in {"http", "https"}:
            return _download_error(
                slack_file,
                f"unsupported Slack attachment URL scheme: {scheme or 'missing'}",
            )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(self.download_dir, _safe_filename(slack_file.name))
        try:
            with httpx.stream(
                "GET",
                slack_file.url_private,
                headers=(
                    {"Authorization": f"Bearer {self.token}"}
                    if self.token and slack_file.requires_auth
                    else {}
                ),
                follow_redirects=True,
                timeout=30.0,
            ) as response:
                response.raise_for_status()
                total = 0
                with target.open("wb") as fh:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.max_attachment_bytes:
                            raise SlackClientError(
                                f"Slack attachment exceeds {self.max_attachment_bytes} bytes"
                            )
                        fh.write(chunk)
        except Exception as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return _download_error(slack_file, str(exc))
        return SlackFile(
            id=slack_file.id,
            name=slack_file.name,
            mimetype=slack_file.mimetype,
            url_private=slack_file.url_private,
            permalink=slack_file.permalink,
            local_path=str(target),
            requires_auth=slack_file.requires_auth,
        )

    def _normalize_text(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            user_id = match.group(1)
            return "@monica" if user_id in self.monica_user_ids else f"@{user_id}"

        clean = str(text or "").strip()
        marker_index = clean.find(_SLACK_BLOCK_KIT_PAYLOAD_MARKER)
        if marker_index >= 0:
            clean = clean[:marker_index].rstrip()
        clean = _CHATGPT_FOOTER_RE.sub("", clean).strip()
        return _MENTION_RE.sub(replace, clean).strip()

    def _message_permalink(self, *, channel_id: str, message_ts: str) -> str:
        if not message_ts:
            return ""
        try:
            link = self.client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
            return str(link.get("permalink") or "")
        except Exception:
            return ""


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    return safe or "slack-file"


def _response_get(response: Any, key: str, default: Any = None) -> Any:
    getter = getattr(response, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def _response_next_cursor(response: Any) -> str:
    metadata = _response_get(response, "response_metadata") or {}
    cursor = _response_get(metadata, "next_cursor", "")
    if cursor:
        return str(cursor).strip()
    return str(_response_get(response, "next_cursor", "") or "").strip()


def _uploaded_file_payload(response: Any) -> dict[str, Any]:
    file_payload = _response_get(response, "file")
    if isinstance(file_payload, dict):
        return file_payload
    files_payload = _response_get(response, "files") or []
    if isinstance(files_payload, list):
        for item in files_payload:
            if isinstance(item, dict):
                return item
    return {}


def _unique_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise SlackClientError(f"Could not choose a unique Slack attachment path for {filename}")


def _download_error(slack_file: SlackFile, error: str) -> SlackFile:
    return SlackFile(
        id=slack_file.id,
        name=slack_file.name,
        mimetype=slack_file.mimetype,
        url_private=slack_file.url_private,
        permalink=slack_file.permalink,
        error=error,
        requires_auth=slack_file.requires_auth,
    )
