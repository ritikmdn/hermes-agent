from __future__ import annotations

from collections.abc import Iterable

import pytest

from plugins.mobile_bug_agent import slack_client
from plugins.mobile_bug_agent.slack_client import SlackClientError, SlackThreadClient


class FakeWebClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, str]] = []
        self.permalink_calls: list[tuple[str, str]] = []

    def conversations_replies(self, channel, ts, limit):
        assert channel == "C123"
        assert ts == "1710000000.000100"
        assert limit == 40
        return {
            "ok": True,
            "messages": [
                {
                    "user": "U1",
                    "text": "<@U999> checkout crashes after promo",
                    "ts": "1710000000.000100",
                    "files": [
                        {
                            "id": "F1",
                            "name": "crash.png",
                            "mimetype": "image/png",
                            "url_private": "https://files/crash.png",
                            "permalink": "https://slack.example/file/F1",
                        }
                    ],
                },
                {"user": "U2", "text": "Android only, Pixel 7", "ts": "1710000001.000200"},
            ],
        }

    def chat_getPermalink(self, channel, message_ts):
        assert channel == "C123"
        self.permalink_calls.append((channel, message_ts))
        return {"ok": True, "permalink": f"https://slack.example/thread/{message_ts}"}

    def chat_postMessage(self, channel, thread_ts, text):
        self.posts.append({"channel": channel, "thread_ts": thread_ts, "text": text})
        return {"ok": True}


class DuplicateFileWebClient(FakeWebClient):
    def conversations_replies(self, channel, ts, limit):
        assert channel == "C123"
        assert ts == "1710000000.000100"
        assert limit == 40
        return {
            "ok": True,
            "messages": [
                {
                    "user": "U1",
                    "text": "<@U999> checkout crashes after promo",
                    "ts": "1710000000.000100",
                    "files": [
                        {
                            "id": "F1",
                            "name": "crash.png",
                            "mimetype": "image/png",
                            "url_private": "https://files/crash-one.png",
                            "permalink": "https://slack.example/file/F1",
                        },
                        {
                            "id": "F2",
                            "name": "crash.png",
                            "mimetype": "image/png",
                            "url_private": "https://files/crash-two.png",
                            "permalink": "https://slack.example/file/F2",
                        },
                    ],
                },
            ],
        }


class LabeledMentionWebClient(FakeWebClient):
    def conversations_replies(self, channel, ts, limit):
        assert channel == "C123"
        assert ts == "1710000000.000100"
        assert limit == 40
        return {
            "ok": True,
            "messages": [
                {
                    "user": "U1",
                    "text": "<@U999|monica> checkout crashes after promo",
                    "ts": "1710000000.000100",
                },
                {
                    "user": "U2",
                    "text": "<@U123|ritik> can repro on Android",
                    "ts": "1710000001.000200",
                },
            ],
        }


class ChatGptFooterWebClient(FakeWebClient):
    def conversations_replies(self, channel, ts, limit):
        assert channel == "C123"
        assert ts == "1710000000.000100"
        assert limit == 40
        return {
            "ok": True,
            "messages": [
                {
                    "user": "U1",
                    "text": (
                        "<@U999> checkout crashes after promo *Sent using* ChatGPT\n\n"
                        "[Slack Block Kit payload for this message]\n"
                        "```json\n[]\n```"
                    ),
                    "ts": "1710000000.000100",
                },
            ],
        }


class UploadWithoutPermalinkWebClient(FakeWebClient):
    def files_upload_v2(self, **_kwargs):
        return {"ok": True, "file": {"id": "F_PROOF", "name": "ios-proof.png"}}


class AttachmentImageWebClient(FakeWebClient):
    def conversations_replies(self, channel, ts, limit):
        assert channel == "C123"
        assert ts == "1710000000.000100"
        return {
            "ok": True,
            "messages": [
                {
                    "user": "U1",
                    "text": "<@U999> screenshot from checkout",
                    "ts": "1710000000.000100",
                    "attachments": [
                        {
                            "title": "Checkout crash screenshot",
                            "image_url": "https://files.example.com/screenshot.png",
                            "fallback": "checkout screenshot",
                        }
                    ],
                },
            ],
        }


class UnsafeAttachmentImageWebClient(FakeWebClient):
    def conversations_replies(self, channel, ts, limit):
        assert channel == "C123"
        assert ts == "1710000000.000100"
        return {
            "ok": True,
            "messages": [
                {
                    "user": "U1",
                    "text": "<@U999> screenshot from checkout",
                    "ts": "1710000000.000100",
                    "attachments": [
                        {
                            "title": "Local file attachment",
                            "image_url": "file:///etc/passwd",
                        }
                    ],
                },
            ],
        }


class PaginatedWebClient(FakeWebClient):
    def __init__(self) -> None:
        super().__init__()
        self.reply_calls: list[dict[str, str | int | None]] = []

    def conversations_replies(self, channel, ts, limit, cursor=None):
        self.reply_calls.append({"channel": channel, "ts": ts, "limit": limit, "cursor": cursor})
        if cursor is None:
            return {
                "ok": True,
                "has_more": True,
                "response_metadata": {"next_cursor": "cursor-2"},
                "messages": [
                    {
                        "user": "U1",
                        "text": "<@U999> checkout crashes after promo",
                        "ts": "1710000000.000100",
                    },
                ],
            }
        assert cursor == "cursor-2"
        return {
            "ok": True,
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
            "messages": [
                {
                    "user": "U2",
                    "text": "Pixel 7, latest beta. It started after 2.14.0.",
                    "ts": "1710000001.000200",
                },
            ],
        }


class RepeatedCursorWebClient(FakeWebClient):
    def __init__(self) -> None:
        super().__init__()
        self.reply_calls = 0

    def conversations_replies(self, channel, ts, limit, cursor=None):
        self.reply_calls += 1
        if self.reply_calls == 1:
            return {
                "ok": True,
                "has_more": True,
                "response_metadata": {"next_cursor": "cursor-repeat"},
                "messages": [
                    {
                        "user": "U1",
                        "text": "<@U999> checkout crashes after promo",
                        "ts": "1710000000.000100",
                    },
                ],
            }
        if self.reply_calls == 2:
            return {
                "ok": True,
                "has_more": True,
                "response_metadata": {"next_cursor": "cursor-repeat"},
                "messages": [],
            }
        raise AssertionError("repeated Slack cursors must not loop forever")


class ErrorWebClient(FakeWebClient):
    def conversations_replies(self, channel, ts, limit):
        return {"ok": False, "error": "missing_scope"}


class PostErrorWebClient(FakeWebClient):
    def chat_postMessage(self, channel, thread_ts, text):
        return {"ok": False, "error": "not_in_channel"}


class MetadataWebClient(FakeWebClient):
    def __init__(self) -> None:
        super().__init__()
        self.channel_calls: list[dict[str, object]] = []

    def auth_test(self):
        return {
            "ok": True,
            "user_id": "U_MONICA",
            "bot_id": "B_MONICA",
            "team_id": "T123",
            "team": "Acme",
            "url": "https://acme.slack.com/",
        }

    def conversations_list(self, **kwargs):
        self.channel_calls.append(kwargs)
        if not kwargs.get("cursor"):
            return {
                "ok": True,
                "has_more": True,
                "response_metadata": {"next_cursor": "next-page"},
                "channels": [
                    {
                        "id": "C_MOBILE",
                        "name": "mobile-bugs",
                        "is_private": False,
                        "is_member": True,
                        "is_archived": False,
                    }
                ],
            }
        assert kwargs["cursor"] == "next-page"
        return {
            "ok": True,
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
            "channels": [
                {
                    "id": "G_PRIVATE",
                    "name": "app-triage",
                    "is_private": True,
                    "is_member": False,
                    "is_archived": False,
                }
            ],
        }


class AuthErrorMetadataWebClient(MetadataWebClient):
    def auth_test(self):
        return {"ok": False, "error": "invalid_auth"}


class ChannelErrorMetadataWebClient(MetadataWebClient):
    def conversations_list(self, **kwargs):
        return {"ok": False, "error": "missing_scope"}


class ResponseLike:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)


class ResponseLikeErrorWebClient(FakeWebClient):
    def conversations_replies(self, channel, ts, limit):
        return ResponseLike({"ok": False, "error": "missing_scope"})

    def chat_postMessage(self, channel, thread_ts, text):
        return ResponseLike({"ok": False, "error": "not_in_channel"})


def test_reads_thread_messages_files_and_permalink():
    fake = FakeWebClient()
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_attachments=False,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    assert ctx.permalink == "https://slack.example/thread/1710000000.000100"
    assert [m.text for m in ctx.messages] == [
        "@monica checkout crashes after promo",
        "Android only, Pixel 7",
    ]
    assert [m.ts for m in ctx.messages] == ["1710000000.000100", "1710000001.000200"]
    assert [m.permalink for m in ctx.messages] == [
        "https://slack.example/thread/1710000000.000100",
        "https://slack.example/thread/1710000001.000200",
    ]
    assert ctx.files[0].name == "crash.png"
    assert ctx.files[0].permalink == "https://slack.example/file/F1"
    data = ctx.to_dict()
    assert data["attachments"] == ["crash.png"]
    assert data["message_details"] == [
        {
            "user_id": "U1",
            "text": "@monica checkout crashes after promo",
            "ts": "1710000000.000100",
            "permalink": "https://slack.example/thread/1710000000.000100",
        },
        {
            "user_id": "U2",
            "text": "Android only, Pixel 7",
            "ts": "1710000001.000200",
            "permalink": "https://slack.example/thread/1710000001.000200",
        },
    ]
    assert fake.permalink_calls == [
        ("C123", "1710000000.000100"),
        ("C123", "1710000001.000200"),
    ]


def test_upload_thread_file_requires_permalink(tmp_path):
    proof = tmp_path / "ios-proof.png"
    proof.write_bytes(b"ios proof")
    client = SlackThreadClient(client=UploadWithoutPermalinkWebClient())

    with pytest.raises(SlackClientError, match="permalink"):
        client.upload_thread_file(
            channel_id="C123",
            thread_ts="1710000000.000100",
            file_path=str(proof),
            title="Monica iOS proof",
        )


def test_lists_slack_workspace_metadata_with_channels():
    fake = MetadataWebClient()
    reader = SlackThreadClient(client=fake, token="xoxb-token")

    metadata = reader.list_workspace_metadata()

    assert metadata.auth.bot_user_id == "U_MONICA"
    assert metadata.auth.bot_id == "B_MONICA"
    assert metadata.auth.team_id == "T123"
    assert metadata.auth.team_name == "Acme"
    assert metadata.auth.team_url == "https://acme.slack.com/"
    assert [(channel.id, channel.name, channel.is_private, channel.is_member) for channel in metadata.channels] == [
        ("C_MOBILE", "mobile-bugs", False, True),
        ("G_PRIVATE", "app-triage", True, False),
    ]
    assert fake.channel_calls[0]["types"] == "public_channel,private_channel"
    assert fake.channel_calls[0]["exclude_archived"] is True
    assert fake.channel_calls[1]["cursor"] == "next-page"


def test_slack_workspace_metadata_surfaces_auth_errors():
    reader = SlackThreadClient(client=AuthErrorMetadataWebClient(), token="xoxb-token")

    with pytest.raises(SlackClientError) as exc_info:
        reader.list_workspace_metadata()

    assert str(exc_info.value) == "Slack auth_test failed: invalid_auth"


def test_slack_workspace_metadata_surfaces_channel_errors():
    reader = SlackThreadClient(client=ChannelErrorMetadataWebClient(), token="xoxb-token")

    with pytest.raises(SlackClientError) as exc_info:
        reader.list_workspace_metadata()

    assert str(exc_info.value) == "Slack conversations_list failed: missing_scope"


def test_normalizes_labeled_slack_mentions_in_thread_context():
    fake = LabeledMentionWebClient()
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_attachments=False,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    assert [message.text for message in ctx.messages] == [
        "@monica checkout crashes after promo",
        "@U123 can repro on Android",
    ]
    assert ctx.to_dict()["messages"] == [
        "U1: @monica checkout crashes after promo",
        "U2: @U123 can repro on Android",
    ]


def test_removes_chatgpt_footer_and_block_payload_from_thread_context():
    fake = ChatGptFooterWebClient()
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_attachments=False,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    assert [message.text for message in ctx.messages] == [
        "@monica checkout crashes after promo",
    ]
    assert ctx.to_dict()["messages"] == [
        "U1: @monica checkout crashes after promo",
    ]


def test_reads_slack_attachment_images_as_evidence():
    fake = AttachmentImageWebClient()
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_attachments=False,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    assert len(ctx.files) == 1
    assert ctx.files[0].id == "attachment-1710000000.000100-1"
    assert ctx.files[0].name == "Checkout crash screenshot"
    assert ctx.files[0].mimetype == "image"
    assert ctx.files[0].url_private == "https://files.example.com/screenshot.png"
    assert ctx.files[0].permalink == "https://files.example.com/screenshot.png"
    assert ctx.to_dict()["attachments"] == ["Checkout crash screenshot"]


def test_reads_paginated_thread_messages_until_limit():
    fake = PaginatedWebClient()
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_attachments=False,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=2)

    assert [message.text for message in ctx.messages] == [
        "@monica checkout crashes after promo",
        "Pixel 7, latest beta. It started after 2.14.0.",
    ]
    assert fake.reply_calls == [
        {"channel": "C123", "ts": "1710000000.000100", "limit": 2, "cursor": None},
        {"channel": "C123", "ts": "1710000000.000100", "limit": 1, "cursor": "cursor-2"},
    ]


def test_repeated_slack_cursor_stops_without_looping_forever():
    fake = RepeatedCursorWebClient()
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_attachments=False,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=5)

    assert [message.text for message in ctx.messages] == ["@monica checkout crashes after promo"]
    assert fake.reply_calls == 2


def test_thread_read_raises_typed_error_on_slack_api_error():
    reader = SlackThreadClient(client=ErrorWebClient(), token="xoxb-token")

    with pytest.raises(SlackClientError, match="missing_scope"):
        reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)


def test_thread_read_raises_typed_error_on_slack_response_like_error():
    reader = SlackThreadClient(client=ResponseLikeErrorWebClient(), token="xoxb-token")

    with pytest.raises(SlackClientError, match="missing_scope"):
        reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)


def test_posts_thread_reply():
    fake = FakeWebClient()
    reader = SlackThreadClient(client=fake, token="xoxb-token")

    reader.post_thread_reply(channel_id="C123", thread_ts="T1", text="Created Linear issue")

    assert fake.posts == [{"channel": "C123", "thread_ts": "T1", "text": "Created Linear issue"}]


def test_post_thread_reply_rejects_blank_text_before_calling_slack():
    fake = FakeWebClient()
    reader = SlackThreadClient(client=fake, token="xoxb-token")

    with pytest.raises(SlackClientError, match="reply text is required"):
        reader.post_thread_reply(channel_id="C123", thread_ts="T1", text="   ")

    assert fake.posts == []


def test_post_thread_reply_raises_typed_error_on_slack_api_error():
    reader = SlackThreadClient(client=PostErrorWebClient(), token="xoxb-token")

    with pytest.raises(SlackClientError, match="not_in_channel"):
        reader.post_thread_reply(channel_id="C123", thread_ts="T1", text="Created Linear issue")


def test_post_thread_reply_raises_typed_error_on_slack_response_like_error():
    reader = SlackThreadClient(client=ResponseLikeErrorWebClient(), token="xoxb-token")

    with pytest.raises(SlackClientError, match="not_in_channel"):
        reader.post_thread_reply(channel_id="C123", thread_ts="T1", text="Created Linear issue")


class FakeStreamResponse:
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        return iter(self.chunks)


def test_downloads_thread_files_to_attachment_directory(monkeypatch, tmp_path):
    fake = FakeWebClient()
    download_dir = tmp_path / "attachments"
    seen_headers: list[dict[str, str]] = []

    def fake_stream(*args, **kwargs):
        seen_headers.append(kwargs.get("headers") or {})
        return FakeStreamResponse([b"image-bytes"])

    monkeypatch.setattr(slack_client.httpx, "stream", fake_stream)
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_dir=download_dir,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    downloaded = download_dir / "crash.png"
    assert ctx.files[0].local_path == str(downloaded)
    assert ctx.files[0].error == ""
    assert downloaded.read_bytes() == b"image-bytes"
    assert ctx.to_dict()["attachments"] == [str(downloaded)]
    assert seen_headers == [{"Authorization": "Bearer xoxb-token"}]


def test_does_not_send_slack_token_when_downloading_public_attachment_image(
    monkeypatch,
    tmp_path,
):
    fake = AttachmentImageWebClient()
    download_dir = tmp_path / "attachments"
    seen_headers: list[dict[str, str]] = []

    def fake_stream(*args, **kwargs):
        seen_headers.append(kwargs.get("headers") or {})
        return FakeStreamResponse([b"image-bytes"])

    monkeypatch.setattr(slack_client.httpx, "stream", fake_stream)
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_dir=download_dir,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    assert ctx.files[0].local_path == str(download_dir / "Checkout-crash-screenshot")
    assert seen_headers == [{}]


def test_does_not_download_unsupported_attachment_url_schemes(monkeypatch, tmp_path):
    fake = UnsafeAttachmentImageWebClient()
    download_dir = tmp_path / "attachments"
    stream_calls = []

    def fake_stream(*args, **kwargs):
        stream_calls.append((args, kwargs))
        raise AssertionError("unsupported attachment URL must not be fetched")

    monkeypatch.setattr(slack_client.httpx, "stream", fake_stream)
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_dir=download_dir,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    assert stream_calls == []
    assert ctx.files[0].url_private == "file:///etc/passwd"
    assert ctx.files[0].local_path == ""
    assert "unsupported Slack attachment URL scheme: file" in ctx.files[0].error


def test_downloads_duplicate_file_names_without_overwriting(monkeypatch, tmp_path):
    fake = DuplicateFileWebClient()
    download_dir = tmp_path / "attachments"
    chunks_by_url = {
        "https://files/crash-one.png": b"first-image",
        "https://files/crash-two.png": b"second-image",
    }

    def fake_stream(method, url, **kwargs):
        return FakeStreamResponse([chunks_by_url[url]])

    monkeypatch.setattr(slack_client.httpx, "stream", fake_stream)
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        monica_user_ids=("U999",),
        download_dir=download_dir,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    paths = [file.local_path for file in ctx.files]
    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert (download_dir / "crash.png").read_bytes() == b"first-image"
    assert (download_dir / "crash-2.png").read_bytes() == b"second-image"
    assert paths == [str(download_dir / "crash.png"), str(download_dir / "crash-2.png")]


def test_download_failure_records_error_and_removes_partial_file(monkeypatch, tmp_path):
    fake = FakeWebClient()
    download_dir = tmp_path / "attachments"

    def fake_stream(*args, **kwargs):
        return FakeStreamResponse([b"abc", b"de"])

    monkeypatch.setattr(slack_client.httpx, "stream", fake_stream)
    reader = SlackThreadClient(
        client=fake,
        token="xoxb-token",
        download_dir=download_dir,
        max_attachment_bytes=4,
    )

    ctx = reader.read_thread(channel_id="C123", thread_ts="1710000000.000100", limit=40)

    assert "exceeds 4 bytes" in ctx.files[0].error
    assert ctx.files[0].local_path == ""
    assert not (download_dir / "crash.png").exists()
