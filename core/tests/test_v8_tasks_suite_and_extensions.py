"""V8 Slice 2 TDD tests for Tasks, Contacts, Google Workspace Suite, and ExtensionLoader."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from alfred.domain.extension_protocol import (
    AlfredExtension,
    ExtensionContext,
    ExtensionJobOutcome,
)
from alfred.domain.feature_models import FeatureFlagsConfig
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.services.extension_loader import ExtensionLoaderService

_SYNTHETIC_EXTENSION_SRC = '''"""Synthetic personal extension for ExtensionLoaderService."""

from __future__ import annotations

from alfred.domain.extension_protocol import (
    ExtensionContext,
    ExtensionJobOutcome,
    ExtensionTaskDraft,
)


class LeaseReminderExtension:
    """Emit a monthly rent verification task on a configurable day of month."""

    @property
    def extension_id(self) -> str:
        """Return unique identifier."""
        return "lease_reminder"

    @property
    def description(self) -> str:
        """Return short description."""
        return "Vérification mensuelle des quittances."

    def validate_config(self, raw_config: dict[str, object]) -> list[str]:
        """Require day_of_month between 1 and 28."""
        day = raw_config.get("day_of_month", 20)
        if not isinstance(day, int) or not (1 <= day <= 28):
            return ["day_of_month must be an integer between 1 and 28"]
        return []

    def run_periodic(self, context: ExtensionContext) -> list[ExtensionJobOutcome]:
        """Return an actionable task draft when today matches day_of_month."""
        day = int(str(context.extension_settings.get("day_of_month", 20)))
        if context.today_iso.endswith(f"-{day:02d}"):
            return [
                ExtensionJobOutcome(
                    extension_id=self.extension_id,
                    job_name="monthly_lease_check",
                    status="ACTION_REQUIRED",
                    summary="Générer quittance mensuelle",
                    tasks_to_create=[
                        ExtensionTaskDraft(
                            title="Vérifier virement loyer",
                            notes="Contrôle automatique mensuel",
                            due_date_iso=context.today_iso,
                        )
                    ],
                )
            ]
        return [
            ExtensionJobOutcome(
                extension_id=self.extension_id,
                job_name="monthly_lease_check",
                status="NO_CHANGES",
            )
        ]


def build_extension() -> LeaseReminderExtension:
    """Instantiate synthetic personal extension."""
    return LeaseReminderExtension()


EXTENSION = build_extension()
'''


def _build_tasks_mock() -> MagicMock:
    """Build a mocked Google Tasks v1 client supporting list, insert, patch, update, and delete."""
    client = MagicMock()
    client.tasklists.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "tl_fam", "title": "Famille", "updated": "2026-11-15T08:00:00Z"}]
    }
    open_raw = {
        "id": "t_open",
        "title": "Payer cantine",
        "notes": "Fil th_12",
        "due": "2026-11-22T00:00:00.000Z",
        "status": "needsAction",
    }
    done_raw = {
        "id": "t_done",
        "title": "Archivé",
        "status": "completed",
        "completed": "2026-11-10T09:00:00Z",
    }

    def _list_tasks(**kwargs: Any) -> MagicMock:
        """Return open tasks by default and include completed tasks when requested."""
        items = [open_raw, done_raw] if kwargs.get("showCompleted") else [open_raw]
        return MagicMock(execute=lambda: {"items": items})

    tasks_res = client.tasks.return_value
    tasks_res.list.side_effect = _list_tasks
    tasks_res.insert.return_value.execute.return_value = {
        "id": "t_sub",
        "title": "Joindre RIB",
        "notes": "https://drive.example/f1",
        "due": "2026-11-25T00:00:00Z",
        "status": "needsAction",
        "parent": "t_open",
    }
    completed_payload = {
        **open_raw,
        "status": "completed",
        "completed": "2026-11-15T18:00:00Z",
    }
    tasks_res.get.return_value.execute.return_value = dict(open_raw)
    tasks_res.patch.return_value.execute.return_value = completed_payload
    tasks_res.update.return_value.execute.return_value = completed_payload
    tasks_res.delete.return_value.execute.return_value = {}
    return client


def test_tasks_service_crud_subtasks_and_completion(tmp_path: Path) -> None:
    """Verify TasksService tasklists, filtering, subtask creation, completion, update, & delete."""
    from alfred.services.tasks_service import TasksService

    paths = AlfredPaths.from_env(tmp_path)
    client = _build_tasks_mock()
    svc = TasksService(paths=paths, settings=AlfredSettings(), tasks_client=client)

    assert svc.is_enabled() is True
    lists = svc.list_tasklists()
    assert len(lists) == 1 and lists[0].tasklist_id == "tl_fam" and lists[0].title == "Famille"

    open_only = svc.list_tasks(tasklist_id="@default", include_completed=False)
    assert [t.task_id for t in open_only] == ["t_open"]
    with_done = svc.list_tasks(tasklist_id="@default", include_completed=True)
    assert [t.task_id for t in with_done] == ["t_open", "t_done"]

    created = svc.create_task(
        title="Joindre RIB",
        notes="https://drive.example/f1",
        due_iso="2026-11-25T00:00:00Z",
        tasklist_id="@default",
        parent_id="t_open",
    )
    assert created.task_id == "t_sub" and created.parent_id == "t_open"

    completed = svc.complete_task("t_open", tasklist_id="@default")
    assert completed.status == "completed"

    updated_payload = {
        "id": "t_open",
        "title": "Payer cantine novembre",
        "notes": "Note maj",
        "due": "2026-11-28T00:00:00Z",
        "status": "needsAction",
    }
    client.tasks.return_value.patch.return_value.execute.return_value = updated_payload
    client.tasks.return_value.update.return_value.execute.return_value = updated_payload
    updated = svc.update_task(
        "t_open",
        title="Payer cantine novembre",
        notes="Note maj",
        due_iso="2026-11-28T00:00:00Z",
        tasklist_id="@default",
    )
    assert updated.title == "Payer cantine novembre"

    svc.delete_task("t_open", tasklist_id="@default")
    client.tasks.return_value.delete.assert_called_once()


def test_tasks_service_disabled_raises_error(tmp_path: Path) -> None:
    """Verify TasksFeatureDisabledError on all operations when tasks_management is False."""
    from alfred.services.tasks_service import TasksFeatureDisabledError, TasksService

    paths = AlfredPaths.from_env(tmp_path)
    cfg = AlfredSettings(features=FeatureFlagsConfig(tasks_management=False))
    client = MagicMock()
    svc = TasksService(paths=paths, settings=cfg, tasks_client=client)

    assert svc.is_enabled() is False
    with pytest.raises(TasksFeatureDisabledError):
        svc.list_tasklists()
    with pytest.raises(TasksFeatureDisabledError):
        svc.list_tasks()
    with pytest.raises(TasksFeatureDisabledError):
        svc.create_task("Tâche bloquée")
    with pytest.raises(TasksFeatureDisabledError):
        svc.complete_task("t_1")
    with pytest.raises(TasksFeatureDisabledError):
        svc.update_task("t_1", title="Nouveau")
    with pytest.raises(TasksFeatureDisabledError):
        svc.delete_task("t_1")
    client.tasklists.assert_not_called()
    client.tasks.assert_not_called()


def test_contacts_service_search_get_and_create(tmp_path: Path) -> None:
    """Verify ContactsService People v1 search_contacts, get_contact, and create_contact."""
    from alfred.services.contacts_service import ContactsService

    paths = AlfredPaths.from_env(tmp_path)
    people_client = MagicMock()
    person_raw = {
        "resourceName": "people/c101",
        "etag": "etag_v1",
        "names": [{"displayName": "Camille Martin"}],
        "emailAddresses": [{"value": "camille.martin@example.com"}],
        "phoneNumbers": [{"value": "+33600000001"}],
        "organizations": [{"name": "Cabinet Exemple"}],
    }
    people_api = people_client.people.return_value
    people_api.searchContacts.return_value.execute.return_value = {
        "results": [{"person": person_raw}]
    }
    people_client.otherContacts.return_value.search.return_value.execute.return_value = {
        "results": []
    }
    people_api.get.return_value.execute.return_value = person_raw
    people_api.createContact.return_value.execute.return_value = {
        "resourceName": "people/c202",
        "etag": "etag_new",
        "names": [{"displayName": "Claire Dupont"}],
        "emailAddresses": [{"value": "claire.dupont@example.com"}],
        "phoneNumbers": [{"value": "+33600000002"}],
        "organizations": [{"name": "Étude Exemple"}],
    }

    svc = ContactsService(paths=paths, settings=AlfredSettings(), people_client=people_client)
    assert svc.is_enabled() is True
    hits = svc.search_contacts("Camille", page_size=5)
    assert len(hits) == 1 and hits[0].resource_name == "people/c101"
    assert hits[0].emails == ["camille.martin@example.com"]
    assert hits[0].organization == "Cabinet Exemple"

    fetched = svc.get_contact("people/c101")
    assert fetched.display_name == "Camille Martin" and fetched.etag == "etag_v1"

    created = svc.create_contact(
        given_name="Claire",
        family_name="Dupont",
        email="claire.dupont@example.com",
        phone="+33600000002",
        organization="Étude Exemple",
    )
    assert created.resource_name == "people/c202"
    assert created.emails == ["claire.dupont@example.com"]


def test_contacts_service_disabled_raises_error(tmp_path: Path) -> None:
    """Verify ContactsFeatureDisabledError on all methods when contacts_lookup is False."""
    from alfred.services.contacts_service import ContactsFeatureDisabledError, ContactsService

    paths = AlfredPaths.from_env(tmp_path)
    people_mock = MagicMock()
    disabled_svc = ContactsService(
        paths=paths,
        settings=AlfredSettings(features=FeatureFlagsConfig(contacts_lookup=False)),
        people_client=people_mock,
    )
    assert disabled_svc.is_enabled() is False
    with pytest.raises(ContactsFeatureDisabledError):
        disabled_svc.search_contacts("Camille")
    with pytest.raises(ContactsFeatureDisabledError):
        disabled_svc.get_contact("people/c101")
    with pytest.raises(ContactsFeatureDisabledError):
        disabled_svc.create_contact("Jean", email="jean@example.com")
    people_mock.people.assert_not_called()


def test_google_workspace_suite_disabled_by_default(tmp_path: Path) -> None:
    """Verify GoogleWorkspaceSuiteService is disabled by default and raises on every method."""
    from alfred.services.google_workspace_suite import (
        GoogleWorkspaceSuiteService,
        WorkspaceSuiteDisabledError,
    )

    paths = AlfredPaths.from_env(tmp_path)
    svc = GoogleWorkspaceSuiteService(paths=paths, settings=AlfredSettings())

    assert svc.is_enabled() is False
    with pytest.raises(WorkspaceSuiteDisabledError):
        svc.create_doc("Note")
    with pytest.raises(WorkspaceSuiteDisabledError):
        svc.append_doc_text("doc_1", "Texte")
    with pytest.raises(WorkspaceSuiteDisabledError):
        svc.read_sheet_values("sh_1", "A1:B2")
    with pytest.raises(WorkspaceSuiteDisabledError):
        svc.append_sheet_row("sh_1", "A:B", ["x", 1])
    with pytest.raises(WorkspaceSuiteDisabledError):
        svc.update_sheet_values("sh_1", "A1:A1", [["y"]])
    with pytest.raises(WorkspaceSuiteDisabledError):
        svc.create_presentation("Slides")
    with pytest.raises(WorkspaceSuiteDisabledError):
        svc.create_form("Formulaire")
    with pytest.raises(WorkspaceSuiteDisabledError):
        svc.list_form_responses("form_1")


def test_google_workspace_suite_docs_sheets_slides_and_forms_when_enabled(tmp_path: Path) -> None:
    """Verify Docs, Sheets, Slides, and Forms v1 connectors when google_docs_suite is True."""
    from alfred.services.google_workspace_suite import GoogleWorkspaceSuiteService

    paths = AlfredPaths.from_env(tmp_path)
    cfg = AlfredSettings(features=FeatureFlagsConfig(google_docs_suite=True))
    docs_client, sheets_client = MagicMock(), MagicMock()
    slides_client, forms_client = MagicMock(), MagicMock()

    docs_api = docs_client.documents.return_value
    docs_api.create.return_value.execute.return_value = {"documentId": "doc_101"}
    docs_api.get.return_value.execute.return_value = {
        "documentId": "doc_101",
        "body": {"content": [{"endIndex": 1}, {"endIndex": 10}]},
    }
    docs_api.batchUpdate.return_value.execute.return_value = {"documentId": "doc_101"}

    vals_api = sheets_client.spreadsheets.return_value.values.return_value
    vals_api.get.return_value.execute.return_value = {
        "values": [["Poste", "Montant"], ["Eau", "45"]]
    }
    vals_api.append.return_value.execute.return_value = {"updates": {"updatedRows": 1}}
    vals_api.update.return_value.execute.return_value = {"updatedCells": 1}

    slides_client.presentations.return_value.create.return_value.execute.return_value = {
        "presentationId": "pres_101"
    }
    forms_api = forms_client.forms.return_value
    forms_api.create.return_value.execute.return_value = {"formId": "form_101"}
    forms_api.responses.return_value.list.return_value.execute.return_value = {
        "responses": [{"responseId": "resp_1", "answers": {"q1": "Oui"}}]
    }

    svc = GoogleWorkspaceSuiteService(
        paths=paths,
        settings=cfg,
        docs_client=docs_client,
        sheets_client=sheets_client,
        slides_client=slides_client,
        forms_client=forms_client,
    )
    assert svc.is_enabled() is True
    assert svc.create_doc("Synthèse Patrimoine") == "doc_101"
    svc.append_doc_text("doc_101", "Paragraphe complémentaire\n")
    docs_api.batchUpdate.assert_called_once()

    rows = svc.read_sheet_values("sh_101", "Budget!A1:B2")
    assert rows == [["Poste", "Montant"], ["Eau", "45"]]
    svc.append_sheet_row("sh_101", "Budget!A:B", ["Électricité", 90])
    vals_api.append.assert_called_once()
    svc.update_sheet_values("sh_101", "Budget!B2:B2", [[50]])
    vals_api.update.assert_called_once()

    assert svc.create_presentation("Bilan Annuel") == "pres_101"
    assert svc.create_form("Sondage Réunion") == "form_101"
    responses = svc.list_form_responses("form_101")
    assert len(responses) == 1 and responses[0]["responseId"] == "resp_1"


def test_extension_loader_discovers_repo_examples_and_checks_compliance() -> None:
    """Verify ExtensionLoaderService loads /root/Alfred/instance/examples as AlfredExtension."""
    repo_root = Path(__file__).resolve().parents[2]
    repo_paths = AlfredPaths.from_env(repo_root)
    loader = ExtensionLoaderService(paths=repo_paths, settings=AlfredSettings.load(repo_paths))

    examples = loader.discover_active_extensions(override_dir=repo_paths.examples_dir)
    assert len(examples) >= 2
    for ext in examples:
        assert isinstance(ext, AlfredExtension)
        assert ext.extension_id and ext.description
        errors = ext.validate_config({})
        assert isinstance(errors, list)
        outcomes = ext.run_periodic(ExtensionContext(today_iso="2026-11-20"))
        assert all(isinstance(o, ExtensionJobOutcome) for o in outcomes)


def test_extension_loader_filters_out_non_compliant_plugin(tmp_path: Path) -> None:
    """Verify discover_active_extensions ignores modules violating AlfredExtension."""
    paths = AlfredPaths.from_env(tmp_path)
    paths.extensions_dir.mkdir(parents=True, exist_ok=True)
    (paths.extensions_dir / "broken_ext.py").write_text(
        '"""Non-compliant plugin."""\n\nEXTENSION = object()\n',
        encoding="utf-8",
    )
    loader = ExtensionLoaderService(paths=paths, settings=AlfredSettings())
    assert loader.discover_active_extensions() == []


def test_extension_loader_config_validation_periodic_and_private_backup(tmp_path: Path) -> None:
    """Verify AlfredExtension config validation, periodic outcomes, and private file backup."""
    paths = AlfredPaths.from_env(tmp_path)
    paths.extensions_dir.mkdir(parents=True, exist_ok=True)
    (paths.extensions_dir / "lease_reminder.py").write_text(
        _SYNTHETIC_EXTENSION_SRC, encoding="utf-8"
    )
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text("project_name: Test Backup\n", encoding="utf-8")
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    (paths.secrets_dir / "token.json").write_text('{"token": "NEVER_UPLOAD"}', encoding="utf-8")

    backed_up_calls: list[tuple[str, str]] = []

    def _record_backup(name: str, content: str) -> str:
        """Record backed-up private file name and payload."""
        backed_up_calls.append((name, content))
        return f"fid_{name}"

    cfg = AlfredSettings(extensions_config={"lease_reminder": {"day_of_month": 20}})
    loader = ExtensionLoaderService(paths=paths, settings=cfg, backup_fn=_record_backup)
    discovered = loader.discover_active_extensions()
    assert len(discovered) == 1 and isinstance(discovered[0], AlfredExtension)
    ext = discovered[0]

    assert ext.validate_config({"day_of_month": 99}) != []
    assert ext.validate_config({"day_of_month": 20}) == []

    outcomes = ext.run_periodic(
        ExtensionContext(today_iso="2026-11-20", extension_settings={"day_of_month": 20})
    )
    assert len(outcomes) == 1 and outcomes[0].status == "ACTION_REQUIRED"
    assert outcomes[0].tasks_to_create[0].title == "Vérifier virement loyer"

    idle_outcomes = ext.run_periodic(
        ExtensionContext(today_iso="2026-11-19", extension_settings={"day_of_month": 20})
    )
    assert len(idle_outcomes) == 1 and idle_outcomes[0].status == "NO_CHANGES"

    backup_result = loader.backup_private_instance_files()
    assert len(backup_result) >= 2
    backed_names = [name for name, _ in backed_up_calls]
    assert any("settings.yaml" in n for n in backed_names)
    assert any("lease_reminder.py" in n for n in backed_names)
    assert not any("token.json" in n for n in backed_names)
    assert not any("NEVER_UPLOAD" in content for _, content in backed_up_calls)
