from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class LinearClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class LinearIssuePayload:
    team_id: str
    title: str
    description: str
    project_id: str = ""
    label_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinearIssue:
    id: str
    identifier: str
    url: str


@dataclass(frozen=True)
class LinearTeam:
    id: str
    key: str
    name: str


@dataclass(frozen=True)
class LinearProject:
    id: str
    name: str
    state: str


@dataclass(frozen=True)
class LinearLabel:
    id: str
    name: str
    color: str


@dataclass(frozen=True)
class LinearWorkspaceMetadata:
    teams: tuple[LinearTeam, ...]
    projects: tuple[LinearProject, ...]
    labels: tuple[LinearLabel, ...]


@dataclass(frozen=True)
class LinearAttachmentPayload:
    issue_id: str
    title: str
    url: str
    subtitle: str = ""


@dataclass(frozen=True)
class LinearAttachment:
    id: str
    title: str
    url: str


@dataclass(frozen=True)
class LinearCommentPayload:
    issue_id: str
    body: str


@dataclass(frozen=True)
class LinearComment:
    id: str
    url: str


class LinearClient:
    API_URL = "https://api.linear.app/graphql"

    def __init__(
        self,
        *,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key.strip()
        self._transport = transport
        self._timeout = timeout

    def create_or_update_issue(
        self,
        payload: LinearIssuePayload,
        *,
        existing_issue_id: str = "",
    ) -> LinearIssue:
        if not self.api_key:
            raise LinearClientError("LINEAR_API_KEY is not configured.")
        if not existing_issue_id and not payload.team_id:
            raise LinearClientError("Linear team_id is required when creating an issue.")

        body = self._update_body(payload, existing_issue_id) if existing_issue_id else self._create_body(payload)
        data = self._post_graphql(body)

        if error_summary := _graphql_error_summary(data):
            raise LinearClientError(f"Linear GraphQL error: {error_summary}")
        if _mutation_success(data, "issueCreate", "issueUpdate") is False:
            raise LinearClientError("Linear issue mutation did not report success.")

        issue = self._extract_issue(data)
        if issue is None:
            raise LinearClientError(f"Linear write failed: {data}")
        return issue

    def create_attachment(self, payload: LinearAttachmentPayload) -> LinearAttachment:
        if not self.api_key:
            raise LinearClientError("LINEAR_API_KEY is not configured.")
        if not payload.issue_id:
            raise LinearClientError("Linear issue_id is required when creating an attachment.")
        if not payload.url:
            raise LinearClientError("Attachment url is required.")

        data = self._post_graphql(self._attachment_body(payload))

        if error_summary := _graphql_error_summary(data):
            raise LinearClientError(f"Linear GraphQL error: {error_summary}")
        if _mutation_success(data, "attachmentCreate") is False:
            raise LinearClientError("Linear attachment mutation did not report success.")

        attachment = self._extract_attachment(data)
        if attachment is None:
            raise LinearClientError(f"Linear attachment write failed: {data}")
        return attachment

    def create_comment(self, payload: LinearCommentPayload) -> LinearComment:
        if not self.api_key:
            raise LinearClientError("LINEAR_API_KEY is not configured.")
        if not payload.issue_id:
            raise LinearClientError("Linear issue_id is required when creating a comment.")
        if not payload.body.strip():
            raise LinearClientError("Comment body is required.")

        data = self._post_graphql(self._comment_body(payload))

        if error_summary := _graphql_error_summary(data):
            raise LinearClientError(f"Linear GraphQL error: {error_summary}")
        if _mutation_success(data, "commentCreate") is False:
            raise LinearClientError("Linear comment mutation did not report success.")

        comment = self._extract_comment(data)
        if comment is None:
            raise LinearClientError(f"Linear comment write failed: {data}")
        return comment

    def list_workspace_metadata(self) -> LinearWorkspaceMetadata:
        if not self.api_key:
            raise LinearClientError("LINEAR_API_KEY is not configured.")
        data = self._post_graphql(self._metadata_body())

        if error_summary := _graphql_error_summary(data):
            raise LinearClientError(f"Linear GraphQL error: {error_summary}")
        body = data.get("data")
        if not isinstance(body, dict):
            raise LinearClientError(f"Linear metadata query failed: {data}")
        return LinearWorkspaceMetadata(
            teams=tuple(_extract_teams(body.get("teams"))),
            projects=tuple(_extract_projects(body.get("projects"))),
            labels=tuple(_extract_labels(body.get("issueLabels"))),
        )

    @staticmethod
    def _create_body(payload: LinearIssuePayload) -> dict[str, Any]:
        issue_input = {
            "teamId": payload.team_id,
            "title": payload.title,
            "description": payload.description,
        }
        if payload.project_id:
            issue_input["projectId"] = payload.project_id
        if payload.label_ids:
            issue_input["labelIds"] = list(payload.label_ids)
        return {
            "query": (
                "mutation($input: IssueCreateInput!) "
                "{ issueCreate(input: $input) { success issue { id identifier url } } }"
            ),
            "variables": {"input": issue_input},
        }

    @staticmethod
    def _update_body(payload: LinearIssuePayload, existing_issue_id: str) -> dict[str, Any]:
        return {
            "query": (
                "mutation($id: String!, $input: IssueUpdateInput!) "
                "{ issueUpdate(id: $id, input: $input) { success issue { id identifier url } } }"
            ),
            "variables": {
                "id": existing_issue_id,
                "input": LinearClient._update_input(payload),
            },
        }

    @staticmethod
    def _update_input(payload: LinearIssuePayload) -> dict[str, Any]:
        issue_input: dict[str, Any] = {
            "title": payload.title,
            "description": payload.description,
        }
        if payload.project_id:
            issue_input["projectId"] = payload.project_id
        if payload.label_ids:
            issue_input["labelIds"] = list(payload.label_ids)
        return issue_input

    @staticmethod
    def _attachment_body(payload: LinearAttachmentPayload) -> dict[str, Any]:
        attachment_input = {
            "issueId": payload.issue_id,
            "title": payload.title,
            "url": payload.url,
        }
        if payload.subtitle:
            attachment_input["subtitle"] = payload.subtitle
        return {
            "query": (
                "mutation($input: AttachmentCreateInput!) "
                "{ attachmentCreate(input: $input) { success attachment { id title url } } }"
            ),
            "variables": {"input": attachment_input},
        }

    @staticmethod
    def _comment_body(payload: LinearCommentPayload) -> dict[str, Any]:
        return {
            "query": (
                "mutation($input: CommentCreateInput!) "
                "{ commentCreate(input: $input) { success comment { id url } } }"
            ),
            "variables": {
                "input": {
                    "issueId": payload.issue_id,
                    "body": payload.body,
                },
            },
        }

    @staticmethod
    def _metadata_body() -> dict[str, Any]:
        return {
            "query": (
                "query MonicaLinearMetadata { "
                "teams(first: 100) { nodes { id key name } } "
                "projects(first: 100) { nodes { id name state } } "
                "issueLabels(first: 100) { nodes { id name color } } "
                "}"
            ),
            "variables": {},
        }

    def _post_graphql(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(
                    self.API_URL,
                    headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                    json=body,
                )
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as exc:
                    detail = _truncate_detail(response.text.strip())
                    message = "Linear returned invalid JSON"
                    if detail:
                        message += f": {detail}"
                    raise LinearClientError(message) from exc
        except httpx.HTTPStatusError as exc:
            response = exc.response
            detail = response.text.strip()
            message = f"Linear HTTP error: {response.status_code} {response.reason_phrase}"
            if detail:
                message += f" - {detail}"
            raise LinearClientError(message) from exc
        except httpx.HTTPError as exc:
            raise LinearClientError(f"Linear request failed: {exc}") from exc
        if not isinstance(data, dict):
            raise LinearClientError(f"Linear returned non-object response: {data}")
        return data

    @staticmethod
    def _extract_issue(data: dict[str, Any]) -> LinearIssue | None:
        issue = (
            (((data.get("data") or {}).get("issueCreate") or {}).get("issue"))
            or (((data.get("data") or {}).get("issueUpdate") or {}).get("issue"))
        )
        if not isinstance(issue, dict) or not issue.get("id"):
            return None
        return LinearIssue(
            id=str(issue.get("id") or ""),
            identifier=str(issue.get("identifier") or ""),
            url=str(issue.get("url") or ""),
        )

    @staticmethod
    def _extract_attachment(data: dict[str, Any]) -> LinearAttachment | None:
        attachment = (((data.get("data") or {}).get("attachmentCreate") or {}).get("attachment"))
        if not isinstance(attachment, dict) or not attachment.get("id"):
            return None
        return LinearAttachment(
            id=str(attachment.get("id") or ""),
            title=str(attachment.get("title") or ""),
            url=str(attachment.get("url") or ""),
        )

    @staticmethod
    def _extract_comment(data: dict[str, Any]) -> LinearComment | None:
        comment = (((data.get("data") or {}).get("commentCreate") or {}).get("comment"))
        if not isinstance(comment, dict) or not comment.get("id"):
            return None
        return LinearComment(
            id=str(comment.get("id") or ""),
            url=str(comment.get("url") or ""),
        )


def _graphql_error_summary(data: dict[str, Any]) -> str:
    errors = data.get("errors")
    if not isinstance(errors, list):
        return ""
    messages: list[str] = []
    for item in errors:
        if isinstance(item, dict):
            message = str(item.get("message") or "").strip()
        else:
            message = str(item).strip()
        if message:
            messages.append(message)
    return "; ".join(messages)


def _mutation_success(data: dict[str, Any], *mutation_names: str) -> bool | None:
    body = data.get("data")
    if not isinstance(body, dict):
        return None
    for name in mutation_names:
        result = body.get(name)
        if isinstance(result, dict) and "success" in result:
            return bool(result.get("success"))
    return None


def _extract_teams(connection: Any) -> list[LinearTeam]:
    teams: list[LinearTeam] = []
    for node in _connection_nodes(connection):
        if not isinstance(node, dict) or not node.get("id"):
            continue
        teams.append(
            LinearTeam(
                id=str(node.get("id") or ""),
                key=str(node.get("key") or ""),
                name=str(node.get("name") or ""),
            )
        )
    return teams


def _extract_projects(connection: Any) -> list[LinearProject]:
    projects: list[LinearProject] = []
    for node in _connection_nodes(connection):
        if not isinstance(node, dict) or not node.get("id"):
            continue
        projects.append(
            LinearProject(
                id=str(node.get("id") or ""),
                name=str(node.get("name") or ""),
                state=str(node.get("state") or ""),
            )
        )
    return projects


def _extract_labels(connection: Any) -> list[LinearLabel]:
    labels: list[LinearLabel] = []
    for node in _connection_nodes(connection):
        if not isinstance(node, dict) or not node.get("id"):
            continue
        labels.append(
            LinearLabel(
                id=str(node.get("id") or ""),
                name=str(node.get("name") or ""),
                color=str(node.get("color") or ""),
            )
        )
    return labels


def _connection_nodes(connection: Any) -> list[Any]:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return []
    return nodes


def _truncate_detail(detail: str, *, limit: int = 2000) -> str:
    if len(detail) <= limit:
        return detail
    return f"{detail[:limit]}..."
