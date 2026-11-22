"""End-to-end CLI surfacing, deduplication, opt-out, manifest, and guardian tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from alfred.controllers.app_factory import build_cli_controller
from alfred.controllers.cli_controller import CliController
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.contacts_service import ContactsService
from alfred.services.core_spec_service import CoreSpecService
from alfred.services.gmail_service import GmailService
from alfred.services.tasks_service import TasksService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


class _Exec:
    """Minimal Google API execute() wrapper."""

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store static response payload."""
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        """Return stored payload."""
        return self._payload


class _FakeCalClient:
    """In-memory fake Google Calendar v3 client for E2E CLI commands."""

    def __init__(self) -> None:
        """Seed one event and initialize sequence counter."""
        self._seq = 1
        self._items: dict[str, dict[str, Any]] = {
            "ev_1": {
                "id": "ev_1",
                "summary": "Initial",
                "start": {"dateTime": "2026-12-10T10:00:00+01:00"},
                "end": {"dateTime": "2026-12-10T11:00:00+01:00"},
            }
        }
        self._res = _FakeCalSubResource(self)

    def calendarList(self) -> Any:  # noqa: N802
        """Return fake calendarList resource."""
        return self._res

    def freebusy(self) -> Any:
        """Return fake freebusy resource."""
        return self._res

    def events(self) -> Any:
        """Return fake events resource."""
        return self._res


class _FakeCalSubResource:
    """Shared stub for calendarList, freebusy, and events methods."""

    def __init__(self, parent: _FakeCalClient) -> None:
        """Bind parent in-memory store."""
        self._p = parent

    def list(self, **kwargs: Any) -> _Exec:
        """Handle both calendarList.list and events.list."""
        if "calendarId" not in kwargs:
            return _Exec(
                {
                    "items": [
                        {
                            "id": "primary",
                            "summary": "Principal",
                            "primary": True,
                            "accessRole": "owner",
                            "timeZone": "Europe/Paris",
                        }
                    ]
                }
            )
        return _Exec({"items": list(self._p._items.values())})

    def query(self, **_kwargs: Any) -> _Exec:
        """Handle freebusy.query."""
        return _Exec({"calendars": {"primary": {"busy": []}}})

    def insert(self, **kwargs: Any) -> _Exec:
        """Insert event and return assigned id."""
        self._p._seq += 1
        eid = f"ev_{self._p._seq}"
        rec = {"id": eid, **dict(kwargs["body"])}
        self._p._items[eid] = rec
        return _Exec(rec)

    def patch(self, **kwargs: Any) -> _Exec:
        """Patch event and return updated record."""
        eid = str(kwargs["eventId"])
        rec = {**self._p._items.get(eid, {"id": eid}), **dict(kwargs["body"]), "id": eid}
        self._p._items[eid] = rec
        return _Exec(rec)

    def update(self, **kwargs: Any) -> _Exec:
        """Update event and return updated record."""
        return self.patch(**kwargs)

    def delete(self, **kwargs: Any) -> _Exec:
        """Delete event and return empty dict."""
        self._p._items.pop(str(kwargs["eventId"]), None)
        return _Exec({})


class _FakeTasksClient:
    """In-memory fake Google Tasks v1 client."""

    def __init__(self) -> None:
        """Initialize shared tasks/tasklists stub."""
        self._res = _FakeTasksRes()

    def tasklists(self) -> Any:
        """Return tasklists resource."""
        return self._res

    def tasks(self) -> Any:
        """Return tasks resource."""
        return self._res


class _FakeTasksRes:
    """Stub methods for Google Tasks tasklists and tasks resources."""

    def list(self, **kwargs: Any) -> _Exec:
        """Return tasklists or tasks payload based on tasklist argument."""
        if "tasklist" not in kwargs:
            return _Exec({"items": [{"id": "@default", "title": "Tâches", "updated": ""}]})
        return _Exec({"items": [{"id": "t_1", "title": "Initiale", "status": "needsAction"}]})

    def insert(self, **kwargs: Any) -> _Exec:
        """Create task and return payload."""
        return _Exec({"id": "t_new", "title": kwargs["body"]["title"], "status": "needsAction"})

    def patch(self, **kwargs: Any) -> _Exec:
        """Patch task and return payload."""
        body = dict(kwargs["body"])
        return _Exec(
            {
                "id": str(kwargs["task"]),
                "title": body.get("title", "Tâche"),
                "status": body.get("status", "needsAction"),
            }
        )

    def delete(self, **_kwargs: Any) -> _Exec:
        """Delete task."""
        return _Exec({})


class _FakePeopleClient:
    """In-memory fake Google People v1 client."""

    def __init__(self) -> None:
        """Initialize people and otherContacts stubs."""
        self._res = _FakePeopleRes()

    def people(self) -> Any:
        """Return people resource."""
        return self._res

    def otherContacts(self) -> Any:  # noqa: N802
        """Return otherContacts resource."""
        return self._res


class _FakePeopleRes:
    """Stub methods for Google People v1."""

    _PERSON: dict[str, Any] = {
        "resourceName": "people/c1",
        "etag": "etag_1",
        "names": [{"displayName": "Jean Dupont"}],
        "emailAddresses": [{"value": "jean.dupont@example.test"}],
    }

    def searchContacts(self, **_kwargs: Any) -> _Exec:  # noqa: N802
        """Return search hit."""
        return _Exec({"results": [{"person": dict(self._PERSON)}]})

    def search(self, **_kwargs: Any) -> _Exec:
        """Return empty otherContacts hit."""
        return _Exec({"results": []})

    def get(self, **_kwargs: Any) -> _Exec:
        """Return contact by resourceName."""
        return _Exec(dict(self._PERSON))

    def createContact(self, **_kwargs: Any) -> _Exec:  # noqa: N802
        """Return created contact."""
        return _Exec({**dict(self._PERSON), "resourceName": "people/c_new"})


class _FakeGmailClient:
    """Minimal fake Gmail v1 client."""

    def users(self) -> Any:
        """Return users resource."""
        return self

    def getProfile(self, **_kwargs: Any) -> _Exec:  # noqa: N802
        """Return mailbox profile."""
        return _Exec({"emailAddress": "owner@example.com", "historyId": "100"})

    def settings(self) -> Any:
        """Return settings resource."""
        return self

    def sendAs(self) -> Any:  # noqa: N802
        """Return sendAs resource."""
        return self

    def list(self, **_kwargs: Any) -> _Exec:
        """Return primary sendAs identity."""
        return _Exec(
            {"sendAs": [{"sendAsEmail": "owner@example.com", "isPrimary": True, "isDefault": True}]}
        )

    def threads(self) -> Any:
        """Return threads resource."""
        return self

    def modify(self, **_kwargs: Any) -> _Exec:
        """Archive thread no-op."""
        return _Exec({})


def _build_e2e_rig(
    tmp_path: Path,
) -> tuple[CliController, StateStore, Path, list[list[str]]]:
    """Assemble CliController with isolated AgySessionManager and in-memory Google doubles."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    agy_cmds: list[list[str]] = []

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Simulate agy creating <conv_id>.db and returning a unique UUID."""
        agy_cmds.append(list(cmd))
        cid = str(uuid.uuid4())
        (agy_dir / "conversations" / f"{cid}.db").write_bytes(b"sqlite")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": cid}), stderr=""
        )

    agy_mgr = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=_runner,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    ctrl = build_cli_controller(
        paths=paths,
        settings=settings,
        state_store=store,
        calendar_service=CalendarService(
            paths, settings, calendar_client=_FakeCalClient(), state_store=store
        ),
        tasks_service=TasksService(paths, settings, tasks_client=_FakeTasksClient()),
        contacts_service=ContactsService(paths, settings, people_client=_FakePeopleClient()),
        gmail_service=GmailService(paths, settings, gmail_client=_FakeGmailClient()),
        agy_manager=agy_mgr,
    )
    return ctrl, store, agy_dir, agy_cmds


def _assert_visible_session(
    store: StateStore,
    agy_dir: Path,
    new_ids: set[str],
    expected_workspace: str,
) -> None:
    """Assert exactly one new EXECUTED, non-archived session exists in expected_workspace."""
    assert len(new_ids) == 1
    cid = next(iter(new_ids))
    rec = store.conversations.load().sessions[cid]
    assert rec.status == "EXECUTED"
    assert rec.workspace == expected_workspace
    pbtxt = agy_dir / "annotations" / f"{cid}.pbtxt"
    assert pbtxt.is_file()
    text = pbtxt.read_text(encoding="utf-8")
    assert "archived:false" in text
    assert "archived:true" not in text


@pytest.mark.parametrize(
    ("argv", "expected_ws"),
    [
        (
            [
                "create-event",
                "--summary",
                "Diner Famille",
                "--from",
                "2026-12-10T20:00:00+01:00",
                "--to",
                "2026-12-10T22:00:00+01:00",
            ],
            "alfred-calendar",
        ),
        (["update-event", "--event-id", "ev_1", "--summary", "Diner Modifié"], "alfred-calendar"),
        (
            [
                "confirm-event",
                "--summary",
                "Diner Famille",
                "--from",
                "2026-12-10T20:00:00+01:00",
                "--to",
                "2026-12-10T22:00:00+01:00",
                "--no-send-invites",
            ],
            "alfred-calendar",
        ),
        (["delete-event", "--event-id", "ev_1"], "alfred-calendar"),
        (["hold-allowlist", "--add", "partner@example.test"], "alfred-calendar"),
        (["create-task", "--title", "Renouveler passeport"], "alfred-tasks"),
        (["complete-task", "--task-id", "t_1"], "alfred-tasks"),
        (["update-task", "--task-id", "t_1", "--title", "Passeport urgent"], "alfred-tasks"),
        (["delete-task", "--task-id", "t_1"], "alfred-tasks"),
        (
            [
                "create-contact",
                "--given-name",
                "Jean",
                "--family-name",
                "Dupont",
                "--email",
                "jean.dupont@example.test",
            ],
            "alfred-tasks",
        ),
    ],
)
def test_e2e_cli_mutations_from_life_surface_visible_specialist_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_ws: str,
) -> None:
    """Every Calendar, Tasks, or Contacts mutation from alfred-life surfaces a visible session."""
    monkeypatch.chdir(WORKTREE_ROOT / "core" / "workspaces" / "alfred-life")
    ctrl, store, agy_dir, agy_cmds = _build_e2e_rig(tmp_path)
    before_ids = set(store.conversations.load().sessions.keys())

    rc = ctrl.run(argv)
    assert rc == 0
    after_ids = set(store.conversations.load().sessions.keys())
    _assert_visible_session(store, agy_dir, after_ids - before_ids, expected_ws)
    assert len(agy_cmds) == 1
    assert agy_cmds[0][agy_cmds[0].index("--project") + 1] == expected_ws


@pytest.mark.parametrize(
    "argv",
    [
        ["list-calendars"],
        ["list-events", "--from", "2026-12-01", "--to", "2026-12-31"],
        ["query-freebusy", "--from", "2026-12-01", "--to", "2026-12-02"],
        ["hold-allowlist", "--list"],
        ["list-tasklists"],
        ["list-tasks"],
        ["search-contacts", "--query", "Dupont"],
        ["get-contact", "--resource-name", "people/c1"],
    ],
)
def test_e2e_cli_readonly_commands_create_zero_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    """Read-only Calendar, Tasks, and Contacts commands create zero Antigravity sessions."""
    monkeypatch.chdir(WORKTREE_ROOT / "core" / "workspaces" / "alfred-life")
    ctrl, store, _, agy_cmds = _build_e2e_rig(tmp_path)

    assert ctrl.run(argv) == 0
    assert store.conversations.load().sessions == {}
    assert agy_cmds == []


def test_e2e_cli_deduplication_in_calendar_and_no_archive_trace_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify same-workspace deduplication, --archive-trace override, and --no-archive-trace."""
    ctrl, store, agy_dir, agy_cmds = _build_e2e_rig(tmp_path)

    monkeypatch.chdir(WORKTREE_ROOT / "core" / "workspaces" / "alfred-calendar")
    base_ev = [
        "create-event",
        "--summary",
        "Point Interne",
        "--from",
        "2026-12-11T10:00:00+01:00",
        "--to",
        "2026-12-11T11:00:00+01:00",
    ]
    assert ctrl.run(base_ev) == 0
    assert store.conversations.load().sessions == {}
    assert agy_cmds == []

    assert ctrl.run([*base_ev, "--archive-trace"]) == 0
    _assert_visible_session(
        store, agy_dir, set(store.conversations.load().sessions.keys()), "alfred-calendar"
    )
    assert len(agy_cmds) == 1

    monkeypatch.chdir(WORKTREE_ROOT / "core" / "workspaces" / "alfred-life")
    before_optout = set(store.conversations.load().sessions.keys())
    assert ctrl.run([*base_ev, "--no-archive-trace"]) == 0
    assert ctrl.run(["create-task", "--title", "Tâche silencieuse", "--no-archive-trace"]) == 0
    assert set(store.conversations.load().sessions.keys()) == before_optout
    assert len(agy_cmds) == 1


def test_e2e_worktree_manifests_pretool_hook_and_spec_invariants() -> None:
    """Verify plan-executor mainAgent, inbox PreToolUse hook allowlist, and synced SPEC.md."""
    executor_path = WORKTREE_ROOT / ".agents" / "agents" / "plan-executor.md"
    raw = executor_path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    parts = raw.split("\n---\n", maxsplit=1)
    assert len(parts) == 2
    frontmatter: Any = yaml.safe_load(parts[0][4:])
    assert isinstance(frontmatter, dict)
    assert frontmatter.get("mainAgent") is True

    hook_path = WORKTREE_ROOT / "core" / "scripts" / "security_pre_tool_hook.py"
    spec = importlib.util.spec_from_file_location("wt_security_pre_tool_hook", hook_path)
    assert spec is not None and spec.loader is not None
    hook_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook_mod)

    inbox_cwd = str(WORKTREE_ROOT / "core" / "workspaces" / "alfred-inbox")
    for cmd_line in (
        "alfred complete-task --task-id t_1",
        "alfred update-task --task-id t_1 --title Maj",
        "alfred delete-task --task-id t_1",
        "alfred create-contact --given-name Jean --email jean@example.test",
    ):
        verdict = hook_mod.evaluate_pre_tool_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "run_command",
                "cwd": inbox_cwd,
                "tool_input": {"CommandLine": cmd_line},
            }
        )
        assert verdict["decision"] == "allow", f"Expected allow for '{cmd_line}', got {verdict}"

    assert CoreSpecService().validate_spec_file(WORKTREE_ROOT) == []
