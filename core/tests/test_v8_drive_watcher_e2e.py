"""End-to-end CLI, PreToolUse subprocess hook, and manifest tests for alfred-drive & watcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from alfred.domain.models import AlfredPaths, GoogleSyncBookmarks
from alfred.main import main
from alfred.repositories.state_store import StateStore
from alfred.services.drive_service import FOLDER_MIME, DriveService
from tests.test_v8_drive_watcher_unit import FakeDriveApiClient

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = WORKTREE_ROOT / "core" / "scripts" / "security_pre_tool_hook.py"


def _write_e2e_settings(
    root_dir: Path,
    *,
    drive_sync: bool = True,
    drive_watcher: bool = True,
    protected_ids: list[str] | None = None,
    agy_bin: str = "agy",
) -> AlfredPaths:
    """Write isolated settings.yaml and workspace folders under root_dir."""
    paths = AlfredPaths(root_dir=root_dir)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    for ws in (
        "alfred-onboard",
        "alfred-life",
        "alfred-inbox",
        "alfred-calendar",
        "alfred-tasks",
        "alfred-drive",
        "alfred-evolve",
    ):
        (paths.workspaces_dir / ws).mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "antigravity": {"binary_path": agy_bin, "sync_daemon_on_changes": False},
        "features": {
            "drive_knowledge_sync": drive_sync,
            "drive_watcher": drive_watcher,
            "inbox_auto_triage": False,
        },
        "security": {"protected_signature_drive_ids": protected_ids or []},
    }
    paths.settings_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return paths


def _run_hook_subprocess(
    root_dir: Path,
    workspace_slug: str,
    tool_name: str,
    tool_input: Mapping[str, object],
) -> dict[str, str]:
    """Invoke worktree security_pre_tool_hook.py as a real subprocess with ALFRED_ROOT_DIR."""
    cwd = str(root_dir / "core" / "workspaces" / workspace_slug)
    stdin_json = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "cwd": cwd,
            "tool_input": dict(tool_input),
        }
    )
    env = {**os.environ, "ALFRED_ROOT_DIR": str(root_dir)}
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin_json,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    verdict: dict[str, str] = json.loads(proc.stdout)
    return verdict


def test_e2e_cli_graceful_skip_and_delegate_to_drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify watch-drive graceful skip when disabled and delegate --to drive via main()."""
    _write_e2e_settings(tmp_path, drive_sync=True, drive_watcher=False)
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))

    assert main(["watch-drive"]) == 0
    assert "SKIPPED_DISABLED" in capsys.readouterr().out

    assert main(["run-routines", "--only", "drive"]) == 0
    routine_json = json.loads(capsys.readouterr().out)
    assert routine_json["scope"] == "drive"
    assert routine_json["jobs"][0]["status"] == "SKIPPED_DISABLED"

    agy_sh = tmp_path / "fake_agy.sh"
    agy_log = tmp_path / "agy.log"
    agy_sh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{agy_log}"\n'
        'echo \'{"conversation_id": "conv-e2e-drv-1"}\'\n',
        encoding="utf-8",
    )
    agy_sh.chmod(0o755)
    _write_e2e_settings(tmp_path, drive_sync=True, drive_watcher=True, agy_bin=str(agy_sh))

    rc = main(
        [
            "delegate",
            "--to",
            "drive",
            "--title",
            "Classer contrat",
            "--brief",
            "Ranger dans /01_Administration",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    logged = agy_log.read_text(encoding="utf-8")
    assert "alfred-drive" in out and "--project alfred-drive" in logged

    _write_e2e_settings(tmp_path, drive_sync=False, drive_watcher=False, agy_bin=str(agy_sh))
    assert main(["delegate", "--to", "drive", "--title", "T", "--brief", "B"]) == 0
    assert "SKIPPED_DISABLED" in capsys.readouterr().out


def test_e2e_cli_drive_governance_and_watch_subcommands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify create-drive-folder, move-drive, rename-drive, shortcut, and watch-drive CLI."""
    paths = _write_e2e_settings(tmp_path, drive_sync=True, drive_watcher=True)
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))

    client = FakeDriveApiClient()
    client.items = {
        "fld_in": {"id": "fld_in", "name": "A_Trier", "mimeType": FOLDER_MIME, "parents": []},
        "fld_adm": {"id": "fld_adm", "name": "01_Admin", "mimeType": FOLDER_MIME, "parents": []},
        "fld_ref": {
            "id": "fld_ref",
            "name": "_ALFRED_REFERENTIEL_",
            "mimeType": FOLDER_MIME,
            "parents": [],
        },
        "f_1": {
            "id": "f_1",
            "name": "brut.pdf",
            "mimeType": "application/pdf",
            "parents": ["fld_in"],
            "modifiedTime": "2026-11-24T08:00:00Z",
            "md5Checksum": "m1",
            "headRevisionId": "r1",
            "ownedByMe": True,
        },
    }
    monkeypatch.setattr(DriveService, "_require_drive_client", lambda _self: client)
    StateStore(paths).bookmarks.save(GoogleSyncBookmarks(referentiel_folder_id="fld_ref"))
    assert main(["init-drive"]) == 0
    _ = capsys.readouterr()

    assert main(["create-drive-folder", "--name", "2026_Quittances", "--parent-id", "fld_adm"]) == 0
    sub_id = str(json.loads(capsys.readouterr().out)["file_id"])

    assert main(["move-drive", "--file-id", "f_1", "--target-parent-id", sub_id]) == 0
    assert json.loads(capsys.readouterr().out)["parent_id"] == sub_id

    assert main(["move-drive", "--file-id", "f_1", "--parent-id", "fld_adm"]) == 0
    assert json.loads(capsys.readouterr().out)["parent_id"] == "fld_adm"

    assert main(["rename-drive", "--file-id", "f_1", "--name", "2026-11_Facture_Eau.pdf"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "2026-11_Facture_Eau.pdf"

    rc_sc = main(
        ["create-drive-shortcut", "--target-id", "f_1", "--parent-id", sub_id, "--name", "Eau"]
    )
    assert rc_sc == 0
    shortcut_out = json.loads(capsys.readouterr().out)
    assert shortcut_out["mime_type"] == "application/vnd.google-apps.shortcut"

    assert main(["watch-drive"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_e2e_pre_tool_hook_exclusive_drive_commands(tmp_path: Path) -> None:
    """Verify exclusive Drive mutation commands are allowed only inside alfred-drive."""
    _write_e2e_settings(tmp_path, drive_watcher=False, protected_ids=["sig_drv_99"])
    exclusive_cmds = (
        "alfred watch-drive",
        "alfred move-drive --file-id f1 --target-parent-id fld1",
        "alfred rename-drive --file-id f1 --name doc.pdf",
        "alfred create-drive-folder --name Fiscalite",
        "alfred create-drive-shortcut --target-id f1 --parent-id fld1",
    )
    other_workspaces = (
        "alfred-life",
        "alfred-inbox",
        "alfred-calendar",
        "alfred-tasks",
        "alfred-onboard",
    )
    for cmd in exclusive_cmds:
        verdict = _run_hook_subprocess(
            tmp_path, "alfred-drive", "run_command", {"CommandLine": cmd}
        )
        assert verdict["decision"] == "allow"
        for other_ws in other_workspaces:
            denied = _run_hook_subprocess(tmp_path, other_ws, "run_command", {"CommandLine": cmd})
            assert denied["decision"] == "deny"


def test_e2e_pre_tool_hook_opt_in_isolation_and_zero_token_reads(tmp_path: Path) -> None:
    """Verify opt-in write isolation on drive_watcher=true while keeping 0-token reads open."""
    _write_e2e_settings(tmp_path, drive_watcher=False)
    upsert_cmd = (
        "alfred upsert-knowledge --file 01_foyer.md --id foyer --title Foyer "
        "--domain foyer --summary S --body-file /tmp/body.md"
    )
    save_cmd = "alfred save-attachment-to-drive --message-id m1 --parent-id fld1"
    for ws in ("alfred-life", "alfred-inbox", "alfred-drive"):
        assert (
            _run_hook_subprocess(tmp_path, ws, "run_command", {"CommandLine": upsert_cmd})[
                "decision"
            ]
            == "allow"
        )
        assert (
            _run_hook_subprocess(tmp_path, ws, "run_command", {"CommandLine": save_cmd})["decision"]
            == "allow"
        )

    _write_e2e_settings(tmp_path, drive_watcher=True)
    for ws in ("alfred-life", "alfred-inbox"):
        assert (
            _run_hook_subprocess(tmp_path, ws, "run_command", {"CommandLine": upsert_cmd})[
                "decision"
            ]
            == "deny"
        )
        assert (
            _run_hook_subprocess(tmp_path, ws, "run_command", {"CommandLine": save_cmd})["decision"]
            == "deny"
        )
        dlg = 'alfred delegate --to drive --title "Ranger" --brief "Classer PJ"'
        assert (
            _run_hook_subprocess(tmp_path, ws, "run_command", {"CommandLine": dlg})["decision"]
            == "allow"
        )
    assert (
        _run_hook_subprocess(tmp_path, "alfred-drive", "run_command", {"CommandLine": upsert_cmd})[
            "decision"
        ]
        == "allow"
    )

    all_ops_ws = ("alfred-life", "alfred-inbox", "alfred-calendar", "alfred-tasks", "alfred-drive")
    for read_cmd in (
        "alfred scan-knowledge --query taxe",
        "alfred load-knowledge --doc foyer",
        "alfred search-drive --query quittance",
    ):
        for ws in all_ops_ws:
            assert (
                _run_hook_subprocess(tmp_path, ws, "run_command", {"CommandLine": read_cmd})[
                    "decision"
                ]
                == "allow"
            )


def test_e2e_pre_tool_hook_alfred_drive_sandbox_restrictions(tmp_path: Path) -> None:
    """Verify alfred-drive blocks signatures, evolve, outbound mail, calendar deletes, & chains."""
    _write_e2e_settings(tmp_path, drive_watcher=True, protected_ids=["sig_drv_99"])
    forbidden_in_drive = (
        "alfred apply-signature --request-id r1",
        "alfred prepare-signature --pdf /tmp/a.pdf",
        "alfred evolve --id x --title T --description D",
        "alfred delegate --to evolve --title T --brief B",
        "alfred send-draft --draft-id d1",
        "alfred delete-event --event-id ev1",
        "alfred watch-drive && whoami",
        "alfred search-drive --query a | cat",
        "alfred search-drive --query $(id)",
    )
    for bad_cmd in forbidden_in_drive:
        verdict = _run_hook_subprocess(
            tmp_path, "alfred-drive", "run_command", {"CommandLine": bad_cmd}
        )
        assert verdict["decision"] == "deny", f"Expected deny for: {bad_cmd}"


def test_e2e_alfred_drive_manifests_and_spec_alignment() -> None:
    """Verify alfred-drive/AGENTS.md, drive-custodian.md, root AGENTS.md, and docs/SPEC.md."""
    drive_ws_md = WORKTREE_ROOT / "core" / "workspaces" / "alfred-drive" / "AGENTS.md"
    custodian_md = WORKTREE_ROOT / ".agents" / "agents" / "drive-custodian.md"
    root_agents_md = WORKTREE_ROOT / "AGENTS.md"
    spec_md = WORKTREE_ROOT / "docs" / "SPEC.md"

    assert drive_ws_md.is_file()
    ws_text = drive_ws_md.read_text(encoding="utf-8")
    for token in ("implementation_plan.md", "drive-custodian", "_ALFRED_REFERENTIEL_"):
        assert token in ws_text

    assert custodian_md.is_file()
    cust_raw = custodian_md.read_text(encoding="utf-8")
    assert cust_raw.startswith("---\n") and "\n---\n" in cust_raw
    fm = yaml.safe_load(cust_raw.split("\n---\n", maxsplit=1)[0][4:])
    assert isinstance(fm, dict) and fm

    assert "alfred-drive" in root_agents_md.read_text(encoding="utf-8")
    spec_text = spec_md.read_text(encoding="utf-8")
    assert "alfred-drive" in spec_text and "drive_watcher" in spec_text
