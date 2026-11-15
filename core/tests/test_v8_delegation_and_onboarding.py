"""V8 Slice 2 TDD tests for Cross-Project DelegationService and Family Office OnboardingService."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from alfred.domain.feature_models import FeatureFlagsConfig, IdentityConfig, McpServerConfig
from alfred.domain.models import AlfredPaths, AlfredSettings, DriveMetadataIndex, EmailMessage
from alfred.domain.security_models import DelegationMode, DelegationTarget
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager


def _prepare_delegation_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: AlfredSettings | None = None,
    owner_addresses: frozenset[str] = frozenset({"owner@domaine-perso.example"}),
) -> tuple[Any, StateStore, AlfredPaths, list[tuple[list[str], Path | None]]]:
    """Wire DelegationService with isolated workspace dirs and captured agy subprocess calls."""
    from alfred.services.delegation_service import DelegationService

    paths = AlfredPaths.from_env(tmp_path)
    for role in ("onboard", "life", "inbox", "calendar", "tasks", "evolve"):
        ws_dir = paths.workspaces_dir / f"alfred-{role}"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENTS.md").write_text(f"# Workspace alfred-{role}\n", encoding="utf-8")

    agy_dir = tmp_path / "agy_data"
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    cfg = settings or AlfredSettings.load(paths)
    store = StateStore(paths)
    agy_calls: list[tuple[list[str], Path | None]] = []

    def _fake_subprocess_run(
        cmd: list[str], *_args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Capture agy CLI invocations and synthesize a live UUID conversation response."""
        if cmd and "agy" in Path(cmd[0]).name:
            conv_id = f"11111111-2222-3333-4444-{len(agy_calls) + 1:012d}"
            (agy_dir / "conversations" / f"{conv_id}.db").write_bytes(b"db")
            raw_cwd = kwargs.get("cwd")
            agy_calls.append((list(cmd), Path(raw_cwd).resolve() if raw_cwd else None))
            out = json.dumps({"conversation_id": conv_id, "status": "ok"})
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=out, stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=3, stdout="inactive\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)
    agy = AgySessionManager(
        paths=paths,
        settings=cfg,
        state_store=store,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
        systemctl_runner=lambda c: subprocess.CompletedProcess(c, 3, "inactive\n", ""),
    )
    gmail_mock = MagicMock()
    gmail_mock.get_authenticated_addresses.return_value = owner_addresses
    svc = DelegationService(
        paths=paths,
        settings=cfg,
        state_store=store,
        agy_manager=agy,
        gmail_service=gmail_mock,
    )
    return svc, store, paths, agy_calls


def test_delegation_cross_project_spawning_cwd_title_and_parent_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify --project alfred-<target>, workspace cwd, sidebar title, and parent_session_id."""
    svc, store, paths, agy_calls = _prepare_delegation_stack(tmp_path, monkeypatch)

    rec = svc.delegate(
        target=DelegationTarget.CALENDAR,
        title="Planifier RDV Pédiatre",
        brief="Proposer un créneau mardi à 14h00.",
        mode=DelegationMode.OPEN_GATE,
        parent_session_id="parent-life-conv-01",
        source_workspace="alfred-life",
    )

    assert len(agy_calls) == 1
    cmd, used_cwd = agy_calls[0]
    assert cmd[cmd.index("--project") + 1] == "alfred-calendar"
    assert used_cwd == (paths.workspaces_dir / "alfred-calendar").resolve()
    assert rec.target_project == "alfred-calendar"
    assert rec.session_id == "11111111-2222-3333-4444-000000000001"
    assert "Planifier RDV Pédiatre" in rec.title
    assert rec.parent_session_id == "parent-life-conv-01"

    persisted = store.conversations.load().sessions[rec.session_id]
    assert persisted.workspace == "alfred-calendar"
    assert "Planifier RDV Pédiatre" in persisted.title
    assert persisted.parent_session_id == "parent-life-conv-01"


def test_delegation_model_routing_orchestrator_vs_hydrogen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify orchestrator targets omit --effort while light targets pass hydrogen + medium."""
    svc, _, _, agy_calls = _prepare_delegation_stack(tmp_path, monkeypatch)

    for brain_role in ("evolve", "life", "onboard"):
        idx = len(agy_calls)
        rec = svc.delegate(
            target=brain_role,
            title=f"Étude {brain_role}",
            brief="Analyse approfondie",
            mode=DelegationMode.OPEN_GATE,
        )
        cmd, _ = agy_calls[idx]
        assert cmd[cmd.index("--model") + 1] == "gemini-pro-orchestrator"
        assert "--effort" not in cmd
        assert rec.model_used == "gemini-pro-orchestrator"

    for muscle_role in ("calendar", "tasks", "inbox"):
        idx = len(agy_calls)
        rec = svc.delegate(
            target=muscle_role,
            title=f"Exécution {muscle_role}",
            brief="Action ciblée",
            mode=DelegationMode.OPEN_GATE,
        )
        cmd, _ = agy_calls[idx]
        assert cmd[cmd.index("--model") + 1] == "gemini-4-hydrogen-medium"
        assert cmd[cmd.index("--effort") + 1] == "medium"
        assert rec.model_used == "gemini-4-hydrogen-medium"


def test_delegation_evolve_always_forces_open_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify target='evolve' forces OPEN_GATE and status='GATE_OPENED' even on EXECUTE_APPROVED."""
    svc, _, _, agy_calls = _prepare_delegation_stack(tmp_path, monkeypatch)

    rec = svc.delegate(
        target="evolve",
        title="Support export fiscal",
        brief="Étudier l'ajout d'un rapport annuel.",
        mode=DelegationMode.EXECUTE_APPROVED,
        source_workspace="alfred-life",
    )

    assert rec.target_project == "alfred-evolve"
    assert rec.mode == DelegationMode.OPEN_GATE
    assert rec.status == "GATE_OPENED"
    cmd, _ = agy_calls[0]
    assert cmd[cmd.index("--project") + 1] == "alfred-evolve"
    assert cmd[cmd.index("--agent") + 1] == "plan-synthesizer"


def test_delegation_execute_approved_runs_plan_executor_on_calendar_and_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify EXECUTE_APPROVED on calendar and tasks runs --agent plan-executor and EXECUTED."""
    svc, _, _, agy_calls = _prepare_delegation_stack(tmp_path, monkeypatch)

    cal_rec = svc.delegate(
        target="calendar",
        title="Confirmer réunion école",
        brief="Insérer événement validé jeudi 18h.",
        mode=DelegationMode.EXECUTE_APPROVED,
        parent_session_id="life-gate-77",
    )
    task_rec = svc.delegate(
        target="tasks",
        title="Envoyer attestation assurance",
        brief="Créer tâche avec échéance vendredi.",
        mode=DelegationMode.EXECUTE_APPROVED,
        parent_session_id="life-gate-77",
    )

    assert cal_rec.status == "EXECUTED" and cal_rec.mode == DelegationMode.EXECUTE_APPROVED
    assert task_rec.status == "EXECUTED" and task_rec.mode == DelegationMode.EXECUTE_APPROVED
    assert agy_calls[0][0][agy_calls[0][0].index("--agent") + 1] == "plan-executor"
    assert agy_calls[0][0][agy_calls[0][0].index("--project") + 1] == "alfred-calendar"
    assert agy_calls[1][0][agy_calls[1][0].index("--agent") + 1] == "plan-executor"
    assert agy_calls[1][0][agy_calls[1][0].index("--project") + 1] == "alfred-tasks"


def test_delegation_graceful_skip_when_target_features_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify SKIPPED_DISABLED and zero agy calls when tasks, calendar, or inbox are disabled."""
    disabled_cfg = AlfredSettings(
        features=FeatureFlagsConfig(
            tasks_management=False,
            calendar_management=False,
            inbox_auto_triage=False,
            gmail_drafts_and_send=False,
        )
    )
    svc, _, _, agy_calls = _prepare_delegation_stack(tmp_path, monkeypatch, settings=disabled_cfg)

    for disabled_target in ("tasks", "calendar", "inbox"):
        rec = svc.delegate(
            target=disabled_target,
            title=f"Skip {disabled_target}",
            brief="Ne doit pas lancer agy",
            mode=DelegationMode.EXECUTE_APPROVED,
        )
        assert rec.status == "SKIPPED_DISABLED" and rec.session_id == ""
    assert agy_calls == []


def test_delegation_inbox_to_evolve_firewall_blocks_unauthenticated_and_allows_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify inbox -> evolve raises PermissionError unless owner email passes SPF and DKIM."""
    cfg = AlfredSettings(
        identity=IdentityConfig(default_send_as_email="owner@domaine-perso.example")
    )
    svc, _, _, agy_calls = _prepare_delegation_stack(
        tmp_path,
        monkeypatch,
        settings=cfg,
        owner_addresses=frozenset({"owner@domaine-perso.example"}),
    )

    with pytest.raises(PermissionError):
        svc.delegate(target="evolve", title="Sans mail", brief="B", source_workspace="alfred-inbox")

    for bad_sender, bad_auth in (
        ("Intruder <intruder@example.com>", "mx.google.com; spf=pass; dkim=pass"),
        ("Owner <owner@domaine-perso.example>", "mx.google.com; spf=pass; dkim=fail"),
        ("Owner <owner@domaine-perso.example>", "mx.google.com; spf=fail; dkim=pass"),
    ):
        msg = EmailMessage(
            message_id="m_bad",
            thread_id="t_bad",
            sender=bad_sender,
            subject="Evo",
            headers={"Authentication-Results": bad_auth},
        )
        with pytest.raises(PermissionError):
            svc.delegate(
                target="evolve",
                title="Rejet",
                brief="B",
                source_workspace="alfred-inbox",
                source_email=msg,
            )
    assert agy_calls == []

    verified = EmailMessage(
        message_id="m_ok",
        thread_id="t_ok",
        sender="Owner <owner@domaine-perso.example>",
        subject="Idée évolution",
        headers={"Authentication-Results": "mx.google.com; spf=pass; dkim=pass"},
    )
    ok_rec = svc.delegate(
        target="evolve",
        title="Idée évolution",
        brief="Créer proposition sous Gate",
        mode=DelegationMode.EXECUTE_APPROVED,
        source_workspace="alfred-inbox",
        source_email=verified,
    )
    assert ok_rec.status == "GATE_OPENED" and ok_rec.mode == DelegationMode.OPEN_GATE
    assert len(agy_calls) == 1


def test_onboarding_inspect_live_status_first_run(tmp_path: Path) -> None:
    """Verify inspect_live_status reports FIRST_RUN and full missing_scopes on clean root."""
    from alfred.services.onboarding_service import OnboardingService

    paths = AlfredPaths.from_env(tmp_path)
    svc = OnboardingService(
        paths=paths, settings=AlfredSettings.load(paths), state_store=StateStore(paths)
    )
    first = svc.inspect_live_status()

    expected_keys = {
        "mode",
        "settings_exists",
        "client_secrets_exists",
        "token_exists",
        "granted_scopes",
        "required_scopes",
        "missing_scopes",
        "enabled_features",
        "identity",
        "knowledge_files",
        "drive_indexed",
        "mcp_configured",
    }
    assert expected_keys.issubset(first.keys())
    assert first["mode"] == "FIRST_RUN"
    assert not first["settings_exists"] and not first["client_secrets_exists"]
    assert not first["token_exists"] and not first["drive_indexed"]
    assert first["granted_scopes"] == [] and first["knowledge_files"] == []
    assert set(first["missing_scopes"]) == set(first["required_scopes"])


def test_onboarding_inspect_live_status_rerun_hides_raw_secrets(tmp_path: Path) -> None:
    """Verify inspect_live_status reports LIVE_RERUN, scope delta, and zero raw secrets."""
    from alfred.services.onboarding_service import OnboardingService

    paths = AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text("project_name: Live Test\n", encoding="utf-8")
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    (paths.secrets_dir / "client_secrets.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "RAW_CLIENT_SECRET_XYZ"}}),
        encoding="utf-8",
    )
    (paths.secrets_dir / "token.json").write_text(
        json.dumps(
            {
                "token": "RAW_ACCESS_TOKEN_SECRET_ABC",
                "refresh_token": "RAW_REFRESH_TOKEN_SECRET_DEF",
                "client_secret": "RAW_CLIENT_SECRET_XYZ",
                "scopes": ["https://www.googleapis.com/auth/drive"],
            }
        ),
        encoding="utf-8",
    )
    paths.knowledge_dir.mkdir(parents=True, exist_ok=True)
    (paths.knowledge_dir / "02_assistante_maternelle.md").write_text("# Fiche\n", encoding="utf-8")
    store.drive_index.save(
        DriveMetadataIndex(start_page_token="tok_live_9", folder_count=4, file_count=12)
    )

    svc = OnboardingService(paths=paths, settings=AlfredSettings.load(paths), state_store=store)
    live = svc.inspect_live_status()
    assert live["mode"] == "LIVE_RERUN"
    assert live["settings_exists"] and live["client_secrets_exists"] and live["token_exists"]
    assert live["drive_indexed"] is True
    assert "02_assistante_maternelle.md" in live["knowledge_files"]
    assert "https://www.googleapis.com/auth/drive" in live["granted_scopes"]
    assert "https://www.googleapis.com/auth/tasks" in live["missing_scopes"]

    dumped = json.dumps(live)
    for secret in (
        "RAW_ACCESS_TOKEN_SECRET_ABC",
        "RAW_REFRESH_TOKEN_SECRET_DEF",
        "RAW_CLIENT_SECRET_XYZ",
    ):
        assert secret not in dumped


def test_onboarding_render_oauth_guide_strictly_matches_enabled_features(tmp_path: Path) -> None:
    """Verify render_oauth_guide outputs clickable GCP links and scopes only for enabled flags."""
    from alfred.services.onboarding_service import OnboardingService

    paths = AlfredPaths.from_env(tmp_path)
    svc = OnboardingService(paths=paths, settings=AlfredSettings(), state_store=StateStore(paths))

    minimal = FeatureFlagsConfig(
        drive_knowledge_sync=True,
        tasks_management=True,
        inbox_auto_triage=False,
        gmail_drafts_and_send=False,
        calendar_management=False,
        contacts_lookup=False,
        google_docs_suite=False,
    )
    guide = svc.render_oauth_guide(features=minimal)
    for required in (
        "https://console.cloud.google.com/projectcreate",
        "https://console.cloud.google.com/apis/library/drive.googleapis.com",
        "https://console.cloud.google.com/apis/library/sheets.googleapis.com",
        "https://console.cloud.google.com/apis/library/tasks.googleapis.com",
        "https://console.cloud.google.com/auth/scopes",
        "https://console.cloud.google.com/auth/clients",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/tasks",
        "client_secrets.json",
        "alfred auth-google",
    ):
        assert required in guide
    for excluded in (
        "gmail.googleapis.com",
        "calendar-json.googleapis.com",
        "people.googleapis.com",
        "slides.googleapis.com",
    ):
        assert excluded not in guide

    suite_guide = svc.render_oauth_guide(
        features=minimal.model_copy(update={"google_docs_suite": True})
    )
    assert "https://console.cloud.google.com/apis/library/docs.googleapis.com" in suite_guide
    assert "https://console.cloud.google.com/apis/library/forms.googleapis.com" in suite_guide
    assert "https://www.googleapis.com/auth/presentations" in suite_guide


def test_onboarding_apply_settings_preserves_knowledge_files_byte_for_byte(tmp_path: Path) -> None:
    """Verify apply_onboarding_settings writes settings.yaml without touching knowledge/*.md."""
    from alfred.services.onboarding_service import OnboardingService

    paths = AlfredPaths.from_env(tmp_path)
    paths.knowledge_dir.mkdir(parents=True, exist_ok=True)
    (paths.knowledge_dir / "00_drive_taxonomy_and_filing_rules.md").write_bytes(
        b"---\nid: tax\n---\n# Taxonomie personnalisee intacte\n"
    )
    (paths.knowledge_dir / "02_assistante_maternelle.md").write_bytes(
        "---\nid: nounou\n---\n# Contrat privé accentué éàè\n".encode()
    )
    snapshot_before = {p.name: p.read_bytes() for p in sorted(paths.knowledge_dir.glob("*.md"))}

    svc = OnboardingService(paths=paths, settings=AlfredSettings(), state_store=StateStore(paths))
    out_path = svc.apply_onboarding_settings(
        features=FeatureFlagsConfig(
            contacts_lookup=False, google_docs_suite=True, mcp_gemini_bridge=True
        ),
        identity=IdentityConfig(
            default_send_as_email="contact@domaine-perso.example",
            default_send_as_display_name="Famille Exemple",
            calendar_organizer_email="organizer@example.com",
            never_invite_emails=["contact@domaine-perso.example", "organizer@example.com"],
        ),
        mcp=McpServerConfig(
            host="127.0.0.1", port=8788, public_url="https://alfred-mcp.domaine-perso.example"
        ),
    )

    assert out_path == paths.settings_path and out_path.is_file()
    reloaded = AlfredSettings.load(paths)
    assert not reloaded.features.contacts_lookup and reloaded.features.google_docs_suite
    assert reloaded.features.mcp_gemini_bridge is True
    assert reloaded.identity.default_send_as_email == "contact@domaine-perso.example"
    assert "organizer@example.com" in reloaded.identity.never_invite_emails
    assert reloaded.mcp.port == 8788
    assert reloaded.mcp.public_url == "https://alfred-mcp.domaine-perso.example"
    assert {
        p.name: p.read_bytes() for p in sorted(paths.knowledge_dir.glob("*.md"))
    } == snapshot_before
