"""Functional tests for DriveWatcherService Speed-1/2, RoutineService, Delegation, & Workspaces."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from alfred.domain.feature_models import FeatureFlagsConfig
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    GoogleSyncBookmarks,
    KnowledgeDocument,
)
from alfred.domain.security_models import DelegationMode, DelegationTarget
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.delegation_service import DelegationService
from alfred.services.drive_service import FOLDER_MIME, DriveService
from alfred.services.evolve_governance_service import _sanitized_git_env
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.routine_service import RoutineService
from alfred.services.workspace_service import CANONICAL_WORKSPACE_ROLES, WorkspaceService
from tests.test_v8_drive_watcher_unit import FakeDriveApiClient


def _seed_agy_sqlite(agy_dir: Path, conv_ids: list[str]) -> None:
    """Create conversation_summaries.db and .db files for simulated live Antigravity sessions."""
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(agy_dir / "conversation_summaries.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', "
            "project_id TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversations_fts ("
            "conversation_id TEXT, archived INTEGER DEFAULT 0, title TEXT DEFAULT '')"
        )
        for cid in conv_ids:
            (agy_dir / "conversations" / f"{cid}.db").write_bytes(b"sqlite")
            conn.execute(
                "INSERT OR REPLACE INTO conversation_summaries VALUES (?, '', 'proj-drive')",
                (cid,),
            )
            conn.execute("INSERT INTO conversations_fts VALUES (?, 0, '')", (cid,))
        conn.commit()
    finally:
        conn.close()


def _seed_drive_folders() -> dict[str, dict[str, Any]]:
    """Return standard top-level Drive folders for functional watcher tests."""
    return {
        "fld_in": {"id": "fld_in", "name": "A_Trier", "mimeType": FOLDER_MIME, "parents": []},
        "fld_admin": {
            "id": "fld_admin",
            "name": "01_Administration",
            "mimeType": FOLDER_MIME,
            "parents": [],
        },
        "fld_pat": {
            "id": "fld_pat",
            "name": "02_Patrimoine",
            "mimeType": FOLDER_MIME,
            "parents": [],
        },
        "fld_ref": {
            "id": "fld_ref",
            "name": "_ALFRED_REFERENTIEL_",
            "mimeType": FOLDER_MIME,
            "parents": [],
        },
    }


def _build_watcher_stack(
    tmp_path: Path,
    cognitive_fn: Callable[..., Any] | None,
    conv_ids: list[str],
    spawn_live: bool = True,
) -> tuple[Any, FakeDriveApiClient, StateStore, Path, list[list[str]]]:
    """Wire DriveWatcherService with FakeDriveApiClient, Knowledge backup, and Agy SQLite."""
    from alfred.services.drive_governance_service import DriveGovernanceService
    from alfred.services.drive_watcher_service import DriveWatcherService

    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings(
        features=FeatureFlagsConfig(drive_watcher=True, drive_knowledge_sync=True),
        agy_sync_daemon_on_changes=False,
    )
    store = StateStore(paths)
    store.bookmarks.save(GoogleSyncBookmarks(referentiel_folder_id="fld_ref"))
    client = FakeDriveApiClient()
    client.items = _seed_drive_folders()
    drive_svc = DriveService(paths, settings, store, drive_client=client)
    drive_svc.init_drive()

    agy_dir = tmp_path / "agy_data"
    _seed_agy_sqlite(agy_dir, conv_ids)
    spawned: list[list[str]] = []

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return queued conversation UUIDs for each agy CLI invocation."""
        idx = min(len(spawned), len(conv_ids) - 1)
        spawned.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": conv_ids[idx]}), stderr=""
        )

    agy = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=_runner,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    watcher = DriveWatcherService(
        paths=paths,
        settings=settings,
        state_store=store,
        drive_service=drive_svc,
        governance_service=DriveGovernanceService(settings, store, drive_svc),
        knowledge_service=KnowledgeService(
            paths, settings, store, backup_fn=drive_svc.backup_markdown_file
        ),
        agy_manager=agy,
        cognitive_fn=cognitive_fn,
        spawn_live_agy=spawn_live,
    )
    return watcher, client, store, agy_dir, spawned


def _silent_auto_file_ops() -> list[Any]:
    """Build valid SILENT_AUTO_FILED operations for move, rename, shortcut, and knowledge."""
    from alfred.domain.drive_watcher_models import DriveGovernanceActionType, DriveGovernanceOp

    return [
        DriveGovernanceOp(
            action=DriveGovernanceActionType.MOVE, file_id="f_assur", target_parent_id="fld_admin"
        ),
        DriveGovernanceOp(
            action=DriveGovernanceActionType.RENAME,
            file_id="f_assur",
            new_name="2026_Contrat_Assurance_Habitation.pdf",
        ),
        DriveGovernanceOp(
            action=DriveGovernanceActionType.CREATE_SHORTCUT,
            shortcut_target_id="f_assur",
            target_parent_id="fld_pat",
            new_name="Assurance_Habitation_2026",
        ),
        DriveGovernanceOp(
            action=DriveGovernanceActionType.UPSERT_KNOWLEDGE,
            knowledge_file="02_patrimoine_et_contrats.md",
            knowledge_id="patrimoine_contrats",
            knowledge_title="Patrimoine & Contrats Clés",
            knowledge_domain="patrimoine",
            knowledge_summary="Assurance habitation 2026 enregistrée.",
            knowledge_keywords=["assurance", "habitation"],
            knowledge_body="# Contrats Clés\n\n- Police habitation 2026 active.\n",
        ),
    ]


def test_speed2_silent_auto_filed_applies_ops_and_archives_pbtxt(tmp_path: Path) -> None:
    """Verify SILENT_AUTO_FILED executes moves, shortcuts, knowledge, and archived .pbtxt."""
    from alfred.domain.drive_watcher_models import (
        DriveDeltaItem,
        DriveWatcherOutputType,
        SanitizedDriveTextResult,
    )

    conv_id = "11111111-aaaa-bbbb-cccc-222222222222"

    def _cognitive(
        items: list[DriveDeltaItem],
        sanitized: dict[str, SanitizedDriveTextResult],
        _docs: list[KnowledgeDocument],
    ) -> tuple[DriveWatcherOutputType, str, list[Any]]:
        """Propose deterministic filing, shortcut, and concise Tier-2 knowledge synthesis."""
        assert len(items) == 1 and "Contrat assurance" in sanitized["f_assur"].clean_text
        return (
            DriveWatcherOutputType.SILENT_AUTO_FILED,
            "Assurance classée",
            _silent_auto_file_ops(),
        )

    watcher, client, store, agy_dir, spawned = _build_watcher_stack(tmp_path, _cognitive, [conv_id])
    rec_in = {
        "id": "f_assur",
        "name": "scan001.pdf",
        "mimeType": "text/plain",
        "parents": ["fld_in"],
        "modifiedTime": "2026-11-24T10:00:00Z",
        "md5Checksum": "ma1",
        "headRevisionId": "ra1",
        "ownedByMe": True,
    }
    client.items["f_assur"] = rec_in
    client.contents["f_assur"] = "Contrat assurance habitation résidence principale 2026."
    client.changes_queue.append({"fileId": "f_assur", "removed": False, "file": dict(rec_in)})

    dec = watcher.poll_and_process(spawn_live_agy=True)[0]
    assert dec.output_type == DriveWatcherOutputType.SILENT_AUTO_FILED
    assert dec.session_id == conv_id and dec.session_archived is True
    assert dec.updated_knowledge_files == ["02_patrimoine_et_contrats.md"]
    assert len(dec.applied_mutations) == 3

    pbtxt = (agy_dir / "annotations" / f"{conv_id}.pbtxt").read_text(encoding="utf-8")
    assert "archived:true" in pbtxt and "[SILENT]" in pbtxt
    sess = store.conversations.load().sessions[conv_id]
    assert sess.workspace == "alfred-drive" and sess.status == "ARCHIVED"
    assert spawned[0][spawned[0].index("--project") + 1] == "alfred-drive"
    backup_audit = store.knowledge_audit.read_all()
    assert backup_audit[0].drive_backup_file_id in store.drive_watcher_ledger.load().events


def test_speed2_explicit_gate_required_opens_active_session(tmp_path: Path) -> None:
    """Verify explicit GATE_REQUIRED performs zero mutations and opens active session."""
    from alfred.domain.drive_watcher_models import DriveWatcherOutputType

    conv_id = "33333333-aaaa-bbbb-cccc-444444444444"
    watcher, client, store, agy_dir, _ = _build_watcher_stack(
        tmp_path,
        lambda _i, _s, _d: (DriveWatcherOutputType.GATE_REQUIRED, "Dossier fiscal ambigu", []),
        [conv_id],
    )
    rec = {
        "id": "f_ambig",
        "name": "doc_1.txt",
        "mimeType": "text/plain",
        "parents": ["fld_in"],
        "modifiedTime": "2026-11-24T11:01:00Z",
        "md5Checksum": "m_1",
        "headRevisionId": "r_1",
        "ownedByMe": True,
    }
    client.items["f_ambig"] = rec
    client.contents["f_ambig"] = "Pièce justificative standard."
    client.changes_queue.append({"fileId": "f_ambig", "removed": False, "file": dict(rec)})

    first = watcher.poll_and_process(spawn_live_agy=True)[0]
    assert first.output_type == DriveWatcherOutputType.GATE_REQUIRED
    assert first.session_archived is False and first.implementation_plan_markdown
    assert first.applied_mutations == [] and first.updated_knowledge_files == []
    assert store.conversations.load().sessions[conv_id].status == "ACTIVE"
    assert "archived:false" in (agy_dir / "annotations" / f"{conv_id}.pbtxt").read_text("utf-8")


def test_speed2_capability_violation_escalates_to_gate_zero_mutations(tmp_path: Path) -> None:
    """Verify >150-line silent knowledge upsert is blocked and escalated to GATE_REQUIRED."""
    from alfred.domain.drive_watcher_models import (
        DriveGovernanceActionType,
        DriveGovernanceOp,
        DriveWatcherOutputType,
    )

    conv_id = "55555555-aaaa-bbbb-cccc-666666666666"
    oversized = DriveGovernanceOp(
        action=DriveGovernanceActionType.UPSERT_KNOWLEDGE,
        knowledge_file="03_trop_long.md",
        knowledge_id="trop_long",
        knowledge_title="Trop Long",
        knowledge_domain="fiscal",
        knowledge_summary="Dépasse 150 lignes",
        knowledge_body="\n".join(f"Ligne {i}" for i in range(165)),
    )
    watcher, client, _, _, _ = _build_watcher_stack(
        tmp_path,
        lambda _i, _s, _d: (DriveWatcherOutputType.SILENT_AUTO_FILED, ">150L", [oversized]),
        [conv_id],
    )
    rec = {
        "id": "f_over",
        "name": "doc_2.txt",
        "mimeType": "text/plain",
        "parents": ["fld_in"],
        "modifiedTime": "2026-11-24T11:02:00Z",
        "md5Checksum": "m_2",
        "headRevisionId": "r_2",
        "ownedByMe": True,
    }
    client.items["f_over"] = rec
    client.contents["f_over"] = "Pièce justificative standard."
    client.changes_queue.append({"fileId": "f_over", "removed": False, "file": dict(rec)})

    second = watcher.poll_and_process(spawn_live_agy=True)[0]
    assert second.output_type == DriveWatcherOutputType.GATE_REQUIRED
    assert second.session_archived is False and second.applied_mutations == []
    assert not (AlfredPaths.from_env(tmp_path).knowledge_dir / "03_trop_long.md").exists()


def test_routine_service_drive_scope_systemd_900s_timer_and_pruning(tmp_path: Path) -> None:
    """Verify run_routines('drive'), 900s systemd timer generation, and pruning when disabled."""
    from alfred.domain.drive_watcher_models import DriveWatcherOutputType

    paths = AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    systemctl_cmds: list[list[str]] = []

    def _systemctl(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record systemctl calls and return active status."""
        systemctl_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "active\n", "")

    watcher, client, _, _, _ = _build_watcher_stack(
        tmp_path,
        lambda _i, _s, _d: (DriveWatcherOutputType.SILENT_NO_ACTION, "Déjà rangé", []),
        ["77777777-aaaa-bbbb-cccc-888888888888"],
    )
    en_cfg = AlfredSettings(
        features=FeatureFlagsConfig(drive_watcher=True), agy_sync_daemon_on_changes=False
    )
    drive_svc = DriveService(paths, en_cfg, store, drive_client=client)
    gmail_svc = GmailService(paths, en_cfg, gmail_client=MagicMock())
    agy = AgySessionManager(paths, en_cfg, store)
    know = KnowledgeService(paths, en_cfg, store)
    inbox = InboxTriageService(store, gmail_svc, know, drive_svc, agy, settings=en_cfg)

    routines_on = RoutineService(
        paths, en_cfg, store, inbox, drive_svc, agy, _systemctl, drive_watcher_service=watcher
    )
    unit_dir = tmp_path / "systemd_user"
    routines_on.install_systemd_timers(systemd_user_dir=unit_dir, enable_now=True)
    assert "run-routines --only drive" in (unit_dir / "alfred-drive.service").read_text("utf-8")
    tmr_txt = (unit_dir / "alfred-drive.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=900s" in tmr_txt and "Persistent=true" in tmr_txt

    tick = routines_on.run_routines(scope="drive")
    assert [j.job_name for j in tick.jobs] == ["drive_watcher", "agy_ui_sync"]
    with store.acquire_routine_lock("alfred_drive"):
        locked = routines_on.run_routines(scope="drive")
        assert locked.locked_skip is True and locked.jobs[0].status == "SKIPPED_LOCKED"

    off_cfg = AlfredSettings(features=FeatureFlagsConfig(drive_watcher=False))
    routines_off = RoutineService(
        paths, off_cfg, store, inbox, drive_svc, agy, _systemctl, drive_watcher_service=watcher
    )
    routines_off.install_systemd_timers(systemd_user_dir=unit_dir, enable_now=True)
    assert not (unit_dir / "alfred-drive.timer").exists()
    assert not (unit_dir / "alfred-drive.service").exists()
    assert ["systemctl", "--user", "disable", "--now", "alfred-drive.timer"] in systemctl_cmds
    assert routines_off.run_routines(scope="drive").jobs[0].status == "SKIPPED_DISABLED"


def test_workspace_seven_roles_and_delegation_to_drive(tmp_path: Path) -> None:
    """Verify WorkspaceService has 7 roles and DelegationService routes to alfred-drive."""
    assert CANONICAL_WORKSPACE_ROLES == (
        "onboard",
        "life",
        "inbox",
        "calendar",
        "tasks",
        "drive",
        "evolve",
    )
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings(agy_sync_daemon_on_changes=False)
    store = StateStore(paths)
    spawned_cmds: list[list[str]] = []

    def _agy_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Capture delegated agy CLI invocation."""
        spawned_cmds.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": "conv-drv-1"}), stderr=""
        )

    agy = AgySessionManager(
        paths, settings, store, runner_fn=_agy_runner, agy_locator_fn=lambda: "agy"
    )
    ws_statuses = WorkspaceService(
        paths, settings, agy, projects_dir=tmp_path / "projects"
    ).init_workspaces()
    assert len(ws_statuses) == 7 and any(s.project_name == "alfred-drive" for s in ws_statuses)

    dlg = DelegationService(paths, settings, store, agy)
    rec = dlg.delegate(
        target=DelegationTarget.DRIVE,
        title="Classer facture",
        brief="Déplacer dans /01_Administration",
        mode=DelegationMode.EXECUTE_APPROVED,
    )
    assert rec.target_project == "alfred-drive" and rec.status == "EXECUTED"
    assert rec.model_used == "gemini-pro-orchestrator-neon"
    assert "--effort" not in spawned_cmds[0]

    disabled_dlg = DelegationService(
        paths,
        AlfredSettings(features=FeatureFlagsConfig(drive_knowledge_sync=False)),
        store,
        agy,
    )
    assert disabled_dlg.delegate(target="drive", title="T", brief="B").status == "SKIPPED_DISABLED"


def _git_cmd(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Execute a deterministic Git command inside cwd with sanitized hook environment."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_sanitized_git_env(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def test_create_isolated_worktree_fetches_and_uses_ahead_origin_base(tmp_path: Path) -> None:
    """Verify create_isolated_worktree auto-fetches origin and bases checkout on origin/main."""
    remote_repo = tmp_path / "remote.git"
    _git_cmd(tmp_path, "init", "--bare", str(remote_repo))
    _git_cmd(remote_repo, "symbolic-ref", "HEAD", "refs/heads/main")

    peer_clone = tmp_path / "peer_clone"
    peer_clone.mkdir()
    _git_cmd(peer_clone, "init", "-b", "main")
    _git_cmd(peer_clone, "config", "user.name", "Alice Example")
    _git_cmd(peer_clone, "config", "user.email", "alice@example.com")
    _git_cmd(peer_clone, "config", "commit.gpgsign", "false")
    _git_cmd(peer_clone, "remote", "add", "origin", str(remote_repo))
    (peer_clone / "README.md").write_text("C1 initial base\n", encoding="utf-8")
    _git_cmd(peer_clone, "add", "README.md")
    _git_cmd(peer_clone, "commit", "-m", "chore: C1 initial base")
    _git_cmd(peer_clone, "push", "-u", "origin", "main")
    c1_sha = _git_cmd(peer_clone, "rev-parse", "HEAD").stdout.strip()

    local_root = tmp_path / "local_root"
    _git_cmd(tmp_path, "clone", str(remote_repo), str(local_root))
    assert _git_cmd(local_root, "rev-parse", "main").stdout.strip() == c1_sha

    (peer_clone / "upstream_pr8_marker.txt").write_text("merged upstream PR #8\n", encoding="utf-8")
    _git_cmd(peer_clone, "add", "upstream_pr8_marker.txt")
    _git_cmd(peer_clone, "commit", "-m", "feat: C2 upstream PR 8")
    _git_cmd(peer_clone, "push", "origin", "main")
    c2_sha = _git_cmd(peer_clone, "rev-parse", "HEAD").stdout.strip()
    assert _git_cmd(local_root, "rev-parse", "origin/main").stdout.strip() == c1_sha

    paths = AlfredPaths.from_env(local_root)
    evolve = EvolveWorktreeService(paths, AlfredSettings.load(paths), StateStore(paths))
    wt_dir = evolve.create_isolated_worktree("fix-stale-base")
    try:
        assert (wt_dir / "upstream_pr8_marker.txt").is_file()
        assert _git_cmd(wt_dir, "rev-parse", "HEAD").stdout.strip() == c2_sha
    finally:
        evolve.cleanup_worktree("fix-stale-base")
    assert not wt_dir.exists()


def test_create_isolated_worktree_falls_back_cleanly_without_origin_remote(tmp_path: Path) -> None:
    """Verify create_isolated_worktree succeeds offline using local main when origin is absent."""
    local_root = tmp_path / "offline_repo"
    local_root.mkdir()
    _git_cmd(local_root, "init", "-b", "main")
    _git_cmd(local_root, "config", "user.name", "Alice Example")
    _git_cmd(local_root, "config", "user.email", "alice@example.com")
    _git_cmd(local_root, "config", "commit.gpgsign", "false")
    (local_root / "local_c1.txt").write_text("offline C1\n", encoding="utf-8")
    _git_cmd(local_root, "add", "local_c1.txt")
    _git_cmd(local_root, "commit", "-m", "chore: C1 offline")
    c1_sha = _git_cmd(local_root, "rev-parse", "HEAD").stdout.strip()

    paths = AlfredPaths.from_env(local_root)
    evolve = EvolveWorktreeService(paths, AlfredSettings.load(paths), StateStore(paths))
    wt_dir = evolve.create_isolated_worktree("offline-wt")
    try:
        assert (wt_dir / "local_c1.txt").is_file()
        assert _git_cmd(wt_dir, "rev-parse", "HEAD").stdout.strip() == c1_sha
    finally:
        evolve.cleanup_worktree("offline-wt")
    assert not wt_dir.exists()

