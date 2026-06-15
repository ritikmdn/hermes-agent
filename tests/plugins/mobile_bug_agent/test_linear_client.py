from __future__ import annotations

import json

import httpx
import pytest

from plugins.mobile_bug_agent.linear_client import (
    LinearAttachmentPayload,
    LinearClient,
    LinearClientError,
    LinearCommentPayload,
    LinearIssuePayload,
)


def test_linear_client_creates_issue_with_project():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        assert request.headers["Authorization"] == "lin_api_key"
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "issue-id",
                            "identifier": "MOB-123",
                            "url": "https://linear.app/acme/issue/MOB-123",
                        },
                    }
                }
            },
        )

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    issue = client.create_or_update_issue(
        LinearIssuePayload(
            team_id="team-id",
            project_id="project-id",
            title="[Mobile] Checkout crash",
            description="## Summary\nAndroid checkout crashes.",
        )
    )

    assert issue.identifier == "MOB-123"
    assert issue.url == "https://linear.app/acme/issue/MOB-123"
    assert requests[0]["variables"]["input"] == {
        "teamId": "team-id",
        "projectId": "project-id",
        "title": "[Mobile] Checkout crash",
        "description": "## Summary\nAndroid checkout crashes.",
    }


def test_linear_client_lists_workspace_metadata():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        assert request.headers["Authorization"] == "lin_api_key"
        return httpx.Response(
            200,
            json={
                "data": {
                    "teams": {
                        "nodes": [
                            {"id": "team-mobile", "key": "MOB", "name": "Mobile"},
                            {"id": "team-web", "key": "WEB", "name": "Web"},
                        ]
                    },
                    "projects": {
                        "nodes": [
                            {"id": "project-app", "name": "Mobile App", "state": "started"},
                        ]
                    },
                    "issueLabels": {
                        "nodes": [
                            {"id": "label-bug", "name": "Bug", "color": "#e5484d"},
                            {"id": "label-mobile", "name": "Mobile", "color": "#3b82f6"},
                        ]
                    },
                }
            },
        )

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    metadata = client.list_workspace_metadata()

    assert metadata.teams[0].id == "team-mobile"
    assert metadata.teams[0].key == "MOB"
    assert metadata.teams[0].name == "Mobile"
    assert metadata.projects[0].id == "project-app"
    assert metadata.projects[0].name == "Mobile App"
    assert metadata.projects[0].state == "started"
    assert metadata.labels[1].id == "label-mobile"
    assert metadata.labels[1].name == "Mobile"
    assert metadata.labels[1].color == "#3b82f6"
    assert "teams" in requests[0]["query"]
    assert "projects" in requests[0]["query"]
    assert "issueLabels" in requests[0]["query"]


def test_linear_client_lists_workspace_metadata_requires_api_key():
    client = LinearClient(api_key="")

    with pytest.raises(LinearClientError) as exc_info:
        client.list_workspace_metadata()

    assert str(exc_info.value) == "LINEAR_API_KEY is not configured."


def test_linear_client_surfaces_graphql_metadata_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Invalid token"}]})

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    with pytest.raises(LinearClientError) as exc_info:
        client.list_workspace_metadata()

    assert str(exc_info.value) == "Linear GraphQL error: Invalid token"


def test_linear_client_updates_existing_issue():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueUpdate": {
                        "success": True,
                        "issue": {
                            "id": "issue-id",
                            "identifier": "MOB-123",
                            "url": "https://linear.app/acme/issue/MOB-123",
                        },
                    }
                }
            },
        )

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    issue = client.create_or_update_issue(
        LinearIssuePayload(
            team_id="team-id",
            title="[Mobile] Checkout crash",
            description="Updated description",
        ),
        existing_issue_id="issue-id",
    )

    assert issue.id == "issue-id"
    assert requests[0]["variables"] == {
        "id": "issue-id",
        "input": {
            "title": "[Mobile] Checkout crash",
            "description": "Updated description",
        },
    }


def test_linear_client_sends_label_ids_on_create():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "issue-id",
                            "identifier": "MOB-123",
                            "url": "https://linear.app/acme/issue/MOB-123",
                        },
                    }
                }
            },
        )

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    client.create_or_update_issue(
        LinearIssuePayload(
            team_id="team-id",
            project_id="project-id",
            label_ids=("bug-label", "mobile-label"),
            title="[Mobile] Checkout crash",
            description="## Summary\nAndroid checkout crashes.",
        )
    )

    assert requests[0]["variables"]["input"]["labelIds"] == ["bug-label", "mobile-label"]


def test_linear_client_surfaces_graphql_issue_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Invalid Linear API key"}]})

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    with pytest.raises(LinearClientError) as exc_info:
        client.create_or_update_issue(
            LinearIssuePayload(
                team_id="team-id",
                title="[Mobile] Checkout crash",
                description="## Summary\nAndroid checkout crashes.",
            )
        )

    assert str(exc_info.value) == "Linear GraphQL error: Invalid Linear API key"


def test_linear_client_refuses_unsuccessful_issue_mutation_with_partial_issue():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": False,
                        "issue": {
                            "id": "partial-issue-id",
                            "identifier": "MOB-123",
                            "url": "https://linear.app/acme/issue/MOB-123",
                        },
                    }
                }
            },
        )

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    with pytest.raises(LinearClientError) as exc_info:
        client.create_or_update_issue(
            LinearIssuePayload(
                team_id="team-id",
                title="[Mobile] Checkout crash",
                description="## Summary\nAndroid checkout crashes.",
            )
        )

    assert str(exc_info.value) == "Linear issue mutation did not report success."


def test_linear_client_surfaces_http_issue_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token", request=request)

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    with pytest.raises(LinearClientError) as exc_info:
        client.create_or_update_issue(
            LinearIssuePayload(
                team_id="team-id",
                title="[Mobile] Checkout crash",
                description="## Summary\nAndroid checkout crashes.",
            )
        )

    assert str(exc_info.value) == "Linear HTTP error: 401 Unauthorized - bad token"


def test_linear_client_surfaces_non_json_issue_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>", request=request)

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    with pytest.raises(LinearClientError) as exc_info:
        client.create_or_update_issue(
            LinearIssuePayload(
                team_id="team-id",
                title="[Mobile] Checkout crash",
                description="## Summary\nAndroid checkout crashes.",
            )
        )

    assert str(exc_info.value) == "Linear returned invalid JSON: <html>maintenance</html>"


def test_linear_client_creates_attachment():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "data": {
                    "attachmentCreate": {
                        "success": True,
                        "attachment": {
                            "id": "attachment-id",
                            "title": "crash.png",
                            "url": "https://files/crash.png",
                        },
                    }
                }
            },
        )

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    attachment = client.create_attachment(
        LinearAttachmentPayload(
            issue_id="issue-id",
            title="crash.png",
            url="https://files/crash.png",
            subtitle="image/png",
        )
    )

    assert attachment.id == "attachment-id"
    assert requests[0]["variables"]["input"] == {
        "issueId": "issue-id",
        "title": "crash.png",
        "url": "https://files/crash.png",
        "subtitle": "image/png",
    }


def test_linear_client_creates_comment():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        assert request.headers["Authorization"] == "lin_api_key"
        return httpx.Response(
            200,
            json={
                "data": {
                    "commentCreate": {
                        "success": True,
                        "comment": {
                            "id": "comment-id",
                            "url": "https://linear.app/acme/issue/MOB-123#comment-id",
                        },
                    }
                }
            },
        )

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    comment = client.create_comment(
        LinearCommentPayload(
            issue_id="issue-id",
            body="Base: origin/dev @ abc123base",
        )
    )

    assert comment.id == "comment-id"
    assert comment.url == "https://linear.app/acme/issue/MOB-123#comment-id"
    assert "commentCreate" in requests[0]["query"]
    assert requests[0]["variables"]["input"] == {
        "issueId": "issue-id",
        "body": "Base: origin/dev @ abc123base",
    }


def test_linear_client_surfaces_graphql_attachment_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Attachment URL is invalid"}]})

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    with pytest.raises(LinearClientError) as exc_info:
        client.create_attachment(
            LinearAttachmentPayload(
                issue_id="issue-id",
                title="crash.png",
                url="https://files/crash.png",
            )
        )

    assert str(exc_info.value) == "Linear GraphQL error: Attachment URL is invalid"


def test_linear_client_refuses_unsuccessful_attachment_mutation_with_partial_attachment():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "attachmentCreate": {
                        "success": False,
                        "attachment": {
                            "id": "partial-attachment-id",
                            "title": "crash.png",
                            "url": "https://files/crash.png",
                        },
                    }
                }
            },
        )

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    with pytest.raises(LinearClientError) as exc_info:
        client.create_attachment(
            LinearAttachmentPayload(
                issue_id="issue-id",
                title="crash.png",
                url="https://files/crash.png",
            )
        )

    assert str(exc_info.value) == "Linear attachment mutation did not report success."


def test_linear_client_surfaces_network_attachment_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    client = LinearClient(api_key="lin_api_key", transport=httpx.MockTransport(handler))

    with pytest.raises(LinearClientError) as exc_info:
        client.create_attachment(
            LinearAttachmentPayload(
                issue_id="issue-id",
                title="crash.png",
                url="https://files/crash.png",
            )
        )

    assert str(exc_info.value) == "Linear request failed: network down"
