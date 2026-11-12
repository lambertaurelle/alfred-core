"""WP8 TDD suite: idle-safe Antigravity remote-control daemon reconciliation for CLI sessions."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    EmailMessage,
)
from alfred.domain.routine_models import RoutinesConfig
from alfred.main import build_controller
from alfred.repositories.state_store import StateStore
from alfred.services.agy_daemon_sync import AgyDaemonSyncService, DaemonSyncStatus
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.nounou_service import NounouService
from alfred.services.pajemploi_calculator import PajemploiCalculator
from alfred.services.routine_service import RoutineService


def _init_agy_summary_db(agy_dir: Path) -> Path:
    """Create a realistic Antigravity conversation_summaries.db schema."""
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    db_path = agy_dir / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, "
            "title TEXT NOT NULL DEFAULT '', "
            "project_id TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'CASCADE_RUN_STATUS_IDLE', "
            "not_fully_idle INTEGER NOT NULL DEFAULT 0)"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _upsert_summary_row(
    db_path: Path,
    conv_id: str,
    status: str = "CASCADE_RUN_STATUS_IDLE",
    not_fully_idle: int = 0,
    project_id: str = "proj-inbox",
) -> None:
    """Insert or update a conversation row in conversation_summaries.db."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO conversation_summaries("
            "conversation_id, title, project_id, status, not_fully_idle"
            ") VALUES (?, '', ?, ?, ?)",
            (conv_id, project_id, status, not_fully_idle),
        )
        conn.commit()
    finally:
        conn.close()


def test_daemon_sync_no_op_when_not_dirty(tmp_path: Path) -> None:
    """sync_if_needed emits zero systemctl calls when no CLI conversation changes are pending."""
    calls: list[list[str]] = []
    svc = AgyDaemonSyncService(
        state_dir=tmp_path / "state",
        agy_data_dir=tmp_path / "agy",
        systemctl_runner=lambda cmd: (
            calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "active\n", "")
        ),
    )
    res = svc.sync_if_needed()
    assert res.status == DaemonSyncStatus.NO_PENDING_CHANGES
    assert calls == []


def test_daemon_sync_defers_on_running_turn_then_flushes_when_idle(tmp_path: Path) -> None:
    """Active Web turn defers restart; subsequent idle tick restarts daemon and clears flag."""
    agy_dir = tmp_path / "agy"
    db_path = _init_agy_summary_db(agy_dir)
    web_conv = "3df46f70-5fe6-495f-959f-c2290ad878a5"
    inbox_conv = "2720d13e-bc7f-438c-934a-61e9decdea4a"
    (agy_dir / "conversations" / f"{inbox_conv}.db").write_bytes(b"sqlite")
    _upsert_summary_row(db_path, web_conv, status="CASCADE_RUN_STATUS_RUNNING", not_fully_idle=1)
    _upsert_summary_row(db_path, inbox_conv, status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=0)

    calls: list[list[str]] = []
    svc = AgyDaemonSyncService(
        state_dir=tmp_path / "state",
        agy_data_dir=agy_dir,
        systemctl_runner=lambda cmd: (
            calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "active\n", "")
        ),
    )
    svc.mark_dirty(inbox_conv, reason="silent_archive")
    assert svc.load_state().pending is True

    deferred = svc.sync_if_needed()
    assert deferred.status == DaemonSyncStatus.DEFERRED_ACTIVE_TURN
    assert not any("restart" in c for c in calls)
    assert svc.load_state().pending is True

    _upsert_summary_row(db_path, web_conv, status="CASCADE_RUN_STATUS_IDLE", not_fully_idle=0)
    synced = svc.sync_if_needed()
    assert synced.status == DaemonSyncStatus.SYNCED
    assert synced.synced_ids == [inbox_conv]
    assert ["systemctl", "--user", "restart", "antigravity-cli-daemon.service"] in calls
    assert svc.load_state().pending is False


def test_daemon_sync_wal_grace_excludes_own_batch_but_protects_foreign_wal(
    tmp_path: Path,
) -> None:
    """Fresh .db-wal for pending CLI conv does not block sync; fresh foreign .db-wal defers."""
    agy_dir = tmp_path / "agy"
    _init_agy_summary_db(agy_dir)
    own_conv = "85930777-aaf4-4338-bed5-9be5f5a15f3d"
    foreign_conv = "9c89748d-fcab-4cbd-83fb-378297bbcacd"
    own_wal = agy_dir / "conversations" / f"{own_conv}.db-wal"
    own_wal.write_bytes(b"wal")
    foreign_wal = agy_dir / "conversations" / f"{foreign_conv}.db-wal"
    foreign_wal.write_bytes(b"wal")

    svc = AgyDaemonSyncService(
        state_dir=tmp_path / "state",
        agy_data_dir=agy_dir,
        idle_wal_grace_sec=20,
        systemctl_runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "active\n", ""),
    )
    svc.mark_dirty(own_conv, reason="trace")

    assert svc.sync_if_needed().status == DaemonSyncStatus.DEFERRED_ACTIVE_TURN

    old_ns = int((time.time() - 60) * 1_000_000_000)
    os.utime(foreign_wal, ns=(old_ns, old_ns))
    res = svc.sync_if_needed()
    assert res.status == DaemonSyncStatus.SYNCED
    assert res.synced_ids == [own_conv]


def test_daemon_sync_handles_disabled_inactive_and_restart_failure(tmp_path: Path) -> None:
    """Disabled config, inactive service, and restart errors preserve safety invariants."""
    agy_dir = tmp_path / "agy"
    _init_agy_summary_db(agy_dir)
    cid = "e61ac095-39b6-4b4e-8f1e-05fd843c1ca1"

    disabled_svc = AgyDaemonSyncService(
        state_dir=tmp_path / "state_dis",
        agy_data_dir=agy_dir,
        enabled=False,
        systemctl_runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "active\n", ""),
    )
    disabled_svc.mark_dirty(cid, reason="gate")
    assert disabled_svc.sync_if_needed().status == DaemonSyncStatus.DISABLED

    inactive_svc = AgyDaemonSyncService(
        state_dir=tmp_path / "state_inact",
        agy_data_dir=agy_dir,
        systemctl_runner=lambda cmd: subprocess.CompletedProcess(cmd, 3, "inactive\n", ""),
    )
    inactive_svc.mark_dirty(cid, reason="gate")
    assert inactive_svc.sync_if_needed().status == DaemonSyncStatus.SKIPPED_DAEMON_INACTIVE

    def failing_restart(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return active on is-active, but fail on restart."""
        if "restart" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "restart failed")
        return subprocess.CompletedProcess(cmd, 0, "active\n", "")

    fail_svc = AgyDaemonSyncService(
        state_dir=tmp_path / "state_fail",
        agy_data_dir=agy_dir,
        systemctl_runner=failing_restart,
    )
    fail_svc.mark_dirty(cid, reason="gate")
    res_fail = fail_svc.sync_if_needed()
    assert res_fail.status == DaemonSyncStatus.RESTART_FAILED
    assert fail_svc.load_state().pending is True


def test_session_manager_marks_dirty_for_real_uuids_and_ignores_synthetic(tmp_path: Path) -> None:
    """AgySessionManager marks daemon_sync dirty for real UUIDs (Gate + trace), not agy_* IDs."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    _init_agy_summary_db(agy_dir)
    gate_uuid = "11111111-aaaa-bbbb-cccc-222222222222"
    trace_uuid = "33333333-dddd-eeee-ffff-444444444444"
    (agy_dir / "conversations" / f"{gate_uuid}.db").write_bytes(b"db")
    (agy_dir / "conversations" / f"{trace_uuid}.db").write_bytes(b"db")

    seq = iter([gate_uuid, trace_uuid])
    agy = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=lambda cmd: subprocess.CompletedProcess(
            cmd, 0, json.dumps({"conversation_id": next(seq)}), ""
        ),
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )

    offline = agy.create_session("alfred-inbox", "m0", "# Plan", spawn_live=False)
    assert offline.session_id.startswith("agy_")
    assert agy.daemon_sync.load_state().pending is False

    live_gate = agy.create_session("alfred-calendar", "m1", "# Cal Gate", spawn_live=True)
    assert live_gate.session_id == gate_uuid
    live_trace = agy.create_and_archive_trace(
        "alfred-inbox", "[SILENT] Facture", "Trace", source_message_id="m2", spawn_live=True
    )
    assert live_trace.session_id == trace_uuid
    st = agy.daemon_sync.load_state()
    assert st.pending is True
    assert st.conversation_ids == [gate_uuid, trace_uuid]


def _build_live_routine_fixture(
    tmp_path: Path,
    loaded: AlfredSettings,
    trace_id: str,
    sys_calls: list[list[str]],
) -> tuple[RoutineService, AgySessionManager]:
    """Assemble a wired RoutineService and AgySessionManager over an isolated agy_data dir."""
    paths = AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    store.bookmarks.update(lambda b: b.model_copy(update={"gmail_history_id": "100"}))
    agy_dir = tmp_path / "agy_data"
    _init_agy_summary_db(agy_dir)
    (agy_dir / "conversations" / f"{trace_id}.db").write_bytes(b"db")

    def systemctl_fn(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record systemctl invocation and return active status."""
        sys_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "active\n", "")

    agy = AgySessionManager(
        paths,
        loaded,
        store,
        runner_fn=lambda cmd: subprocess.CompletedProcess(
            cmd, 0, json.dumps({"conversation_id": trace_id}), ""
        ),
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
        systemctl_runner=systemctl_fn,
    )
    gmail = GmailService(paths, loaded)
    gmail.list_new_messages_since = lambda _hid: (  # type: ignore[method-assign]
        [
            EmailMessage(
                message_id="m_night_1",
                thread_id="th_n1",
                sender="no-reply@news.test",
                subject="Newsletter de la nuit",
                headers={"List-Unsubscribe": "<mailto:u@news.test>"},
            )
        ],
        "101",
    )
    gmail.get_authenticated_addresses = lambda: frozenset()  # type: ignore[method-assign]
    know = KnowledgeService(paths, loaded, store)
    drv = DriveService(paths, loaded, store)
    cal = CalendarService(paths, loaded, state_store=store)
    inbox = InboxTriageService(store, gmail, know, drv, agy, spawn_live_agy=True, settings=loaded)
    routines = RoutineService(
        paths,
        loaded.model_copy(update={"routines": RoutinesConfig(spawn_agy=True, cognitive_agy=True)}),
        store,
        inbox,
        drv,
        NounouService(store, cal, know, loaded),
        PajemploiCalculator(),
        cal,
        agy,
        systemctl_runner=systemctl_fn,
    )
    return routines, agy


def test_routine_and_cli_sync_agy_ui_end_to_end(tmp_path: Path, capsys: Any) -> None:
    """RoutineService flushes pending CLI conversations and CLI exposes sync-agy-ui."""
    cfg_dir = tmp_path / "instance" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.yaml").write_text(
        "antigravity:\n"
        '  binary_path: "agy"\n'
        '  remote_control_service: "custom-agy-daemon.service"\n'
        "  sync_daemon_on_changes: true\n"
        "  idle_wal_grace_sec: 12\n",
        encoding="utf-8",
    )
    loaded = AlfredSettings.load(AlfredPaths.from_env(tmp_path))
    assert loaded.agy_remote_control_service == "custom-agy-daemon.service"
    assert loaded.agy_sync_daemon_on_changes is True
    assert loaded.agy_idle_wal_grace_sec == 12

    sys_calls: list[list[str]] = []
    routines, agy = _build_live_routine_fixture(
        tmp_path, loaded, "f0590df9-ffb2-4e6c-8e31-52a5be64d6a4", sys_calls
    )
    report = routines.run_routines(scope="inbox")
    by_job = {j.job_name: j for j in report.jobs}
    assert by_job["inbox_poll"].status == "OK"
    assert by_job["agy_ui_sync"].status == "OK"
    assert ["systemctl", "--user", "restart", "custom-agy-daemon.service"] in sys_calls
    assert agy.daemon_sync.load_state().pending is False

    ctrl = build_controller(str(tmp_path))
    ctrl._agy = agy
    assert ctrl.run(["sync-agy-ui", "--force"]) == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["status"] == "SYNCED"
