"""Domain models for Google Tasks (alfred-tasks) and Google People Contacts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GoogleTaskListItem(BaseModel):
    """Normalized Google Tasks list descriptor."""

    model_config = ConfigDict(frozen=True)

    tasklist_id: str
    title: str
    updated: str = ""


class GoogleTaskItem(BaseModel):
    """Normalized Google Task record supporting subtasks and Gmail/Drive notes permalinks."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    tasklist_id: str = "@default"
    title: str
    notes: str = ""
    due_iso: str = ""
    status: str = "needsAction"
    parent_id: str = ""
    completed_at: str = ""


class ContactSummary(BaseModel):
    """Normalized Google People v1 contact entry with etag for safe updates."""

    model_config = ConfigDict(frozen=True)

    resource_name: str
    display_name: str
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    organization: str = ""
    etag: str = ""
