"""Google Tasks v1 connector supporting tasklists, subtasks, due dates, and feature flags."""

from __future__ import annotations

from typing import Any

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.domain.tasks_models import GoogleTaskItem, GoogleTaskListItem
from alfred.services.google_auth import GoogleOAuthService


class TasksFeatureDisabledError(RuntimeError):
    """Raised when Google Tasks operations are invoked while tasks_management is False."""


def _normalize_due_rfc3339(due_iso: str) -> str:
    """Normalize a YYYY-MM-DD or ISO-8601 string into an RFC 3339 timestamp for Google Tasks."""
    cleaned = due_iso.strip()
    if not cleaned:
        return ""
    if "T" not in cleaned:
        return f"{cleaned}T00:00:00Z"
    return cleaned


class TasksService:
    """Google Tasks v1 adapter for Family Office action tracking and subtask hierarchies."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        tasks_client: Any | None = None,
        oauth_service: GoogleOAuthService | None = None,
    ) -> None:
        """Initialize the Google Tasks service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including features.tasks_management.
            tasks_client: Optional injected Google Tasks v1 discovery resource.
            oauth_service: Optional shared GoogleOAuthService instance.
        """
        self._paths = paths
        self._settings = settings
        self._tasks_client = tasks_client
        self._oauth = oauth_service or GoogleOAuthService(paths, settings)

    def is_enabled(self) -> bool:
        """Return True when tasks_management is enabled in settings.features."""
        return bool(self._settings.features.tasks_management)

    def list_tasklists(self, max_results: int = 100) -> list[GoogleTaskListItem]:
        """List all Google Task lists accessible to the authenticated user.

        Args:
            max_results: Maximum number of task lists to fetch.

        Returns:
            Ordered list of GoogleTaskListItem descriptors.

        Raises:
            TasksFeatureDisabledError: When tasks_management is False.
        """
        client = self._require_enabled_client()
        resp = client.tasklists().list(maxResults=max(1, int(max_results))).execute()
        items = resp.get("items", []) if isinstance(resp, dict) else []
        return [
            GoogleTaskListItem(
                tasklist_id=str(raw.get("id", "")),
                title=str(raw.get("title", "")),
                updated=str(raw.get("updated", "")),
            )
            for raw in items
            if isinstance(raw, dict) and raw.get("id")
        ]

    def list_tasks(
        self,
        tasklist_id: str | None = None,
        include_completed: bool = False,
        max_results: int = 100,
    ) -> list[GoogleTaskItem]:
        """List tasks in a Google Task list, filtering out completed tasks unless requested.

        Args:
            tasklist_id: Optional Google Task list ID (defaults to settings.default_tasklist_id).
            include_completed: When True, include completed and hidden tasks.
            max_results: Maximum number of tasks to return.

        Returns:
            Ordered list of normalized GoogleTaskItem entities.

        Raises:
            TasksFeatureDisabledError: When tasks_management is False.
        """
        client = self._require_enabled_client()
        resolved_tl = tasklist_id or self._settings.default_tasklist_id
        resp = (
            client.tasks()
            .list(
                tasklist=resolved_tl,
                showCompleted=bool(include_completed),
                showHidden=bool(include_completed),
                maxResults=max(1, int(max_results)),
            )
            .execute()
        )
        raw_items = resp.get("items", []) if isinstance(resp, dict) else []
        parsed = [
            self._parse_task_item(raw, resolved_tl)
            for raw in raw_items
            if isinstance(raw, dict) and raw.get("id")
        ]
        if not include_completed:
            return [item for item in parsed if item.status != "completed"]
        return parsed

    def create_task(
        self,
        title: str,
        notes: str = "",
        due_iso: str = "",
        tasklist_id: str | None = None,
        parent_id: str = "",
    ) -> GoogleTaskItem:
        """Create a top-level task or subtask in Google Tasks.

        Args:
            title: Actionable task title.
            notes: Optional notes body (including Gmail/Drive reference permalinks).
            due_iso: Optional due date ('YYYY-MM-DD' or RFC 3339 timestamp).
            tasklist_id: Optional target tasklist ID (defaults to settings.default_tasklist_id).
            parent_id: Optional parent task ID when creating a subtask.

        Returns:
            Newly created GoogleTaskItem.

        Raises:
            TasksFeatureDisabledError: When tasks_management is False.
        """
        client = self._require_enabled_client()
        resolved_tl = tasklist_id or self._settings.default_tasklist_id
        body: dict[str, str] = {"title": title}
        if notes:
            body["notes"] = notes
        normalized_due = _normalize_due_rfc3339(due_iso)
        if normalized_due:
            body["due"] = normalized_due
        kwargs: dict[str, Any] = {"tasklist": resolved_tl, "body": body}
        if parent_id:
            kwargs["parent"] = parent_id
        created = client.tasks().insert(**kwargs).execute()
        return self._parse_task_item(created if isinstance(created, dict) else {}, resolved_tl)

    def complete_task(self, task_id: str, tasklist_id: str | None = None) -> GoogleTaskItem:
        """Mark an existing Google Task as completed.

        Args:
            task_id: Target Google Task identifier.
            tasklist_id: Optional Google Task list ID (defaults to settings.default_tasklist_id).

        Returns:
            Updated GoogleTaskItem with status='completed'.

        Raises:
            TasksFeatureDisabledError: When tasks_management is False.
        """
        client = self._require_enabled_client()
        resolved_tl = tasklist_id or self._settings.default_tasklist_id
        patched = (
            client.tasks()
            .patch(tasklist=resolved_tl, task=task_id, body={"status": "completed"})
            .execute()
        )
        return self._parse_task_item(patched if isinstance(patched, dict) else {}, resolved_tl)

    def update_task(
        self,
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        due_iso: str | None = None,
        status: str | None = None,
        tasklist_id: str | None = None,
    ) -> GoogleTaskItem:
        """Patch mutable fields (title, notes, due, status) on an existing Google Task.

        Args:
            task_id: Target Google Task identifier.
            title: Optional updated task title.
            notes: Optional updated notes string.
            due_iso: Optional updated due date ('YYYY-MM-DD' or RFC 3339 timestamp).
            status: Optional updated status ('needsAction' or 'completed').
            tasklist_id: Optional Google Task list ID (defaults to settings.default_tasklist_id).

        Returns:
            Updated GoogleTaskItem.

        Raises:
            TasksFeatureDisabledError: When tasks_management is False.
        """
        client = self._require_enabled_client()
        resolved_tl = tasklist_id or self._settings.default_tasklist_id
        body: dict[str, str] = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if due_iso is not None:
            body["due"] = _normalize_due_rfc3339(due_iso)
        if status is not None:
            body["status"] = status
        patched = client.tasks().patch(tasklist=resolved_tl, task=task_id, body=body).execute()
        return self._parse_task_item(patched if isinstance(patched, dict) else {}, resolved_tl)

    def delete_task(self, task_id: str, tasklist_id: str | None = None) -> None:
        """Delete a Google Task permanently from its task list.

        Args:
            task_id: Target Google Task identifier.
            tasklist_id: Optional Google Task list ID (defaults to settings.default_tasklist_id).

        Raises:
            TasksFeatureDisabledError: When tasks_management is False.
        """
        client = self._require_enabled_client()
        resolved_tl = tasklist_id or self._settings.default_tasklist_id
        client.tasks().delete(tasklist=resolved_tl, task=task_id).execute()

    def _require_enabled_client(self) -> Any:
        """Verify tasks_management is enabled and return an initialized Tasks v1 client."""
        if not self.is_enabled():
            raise TasksFeatureDisabledError(
                "Google Tasks feature ('tasks_management') is disabled in settings.yaml."
            )
        if self._tasks_client is None:
            self._tasks_client = self._oauth.build_api_client("tasks", "v1")
        return self._tasks_client

    def _parse_task_item(self, raw: dict[str, Any], tasklist_id: str) -> GoogleTaskItem:
        """Normalize a raw Google Tasks v1 resource dictionary into a GoogleTaskItem."""
        return GoogleTaskItem(
            task_id=str(raw.get("id", "")),
            tasklist_id=tasklist_id,
            title=str(raw.get("title", "")),
            notes=str(raw.get("notes", "")),
            due_iso=str(raw.get("due", "")),
            status=str(raw.get("status", "needsAction")),
            parent_id=str(raw.get("parent", "")),
            completed_at=str(raw.get("completed", "")),
        )
