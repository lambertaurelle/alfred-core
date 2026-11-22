"""End-to-end tests for alfred bootstrap-gcp CLI and scripts/bootstrap.sh --onboard."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from alfred.controllers.app_factory import build_cli_controller
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.main import main
from alfred.repositories.state_store import StateStore

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def _install_fake_gcloud_script(fake_home: Path) -> Path:
    """Create an executable fake gcloud binary under fake_home/.local/google-cloud-sdk/bin."""
    gcloud_bin = fake_home / ".local" / "google-cloud-sdk" / "bin" / "gcloud"
    gcloud_bin.parent.mkdir(parents=True, exist_ok=True)
    gcloud_bin.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"get-value account"*) echo "alice@example.com" ;;\n'
        '  *"auth list"*) echo "alice@example.com" ;;\n'
        '  *"get-value project"*) echo "alfred-e2e-01" ;;\n'
        '  *"projects describe"*) exit 1 ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gcloud_bin.chmod(0o755)
    return gcloud_bin


def test_cli_bootstrap_gcp_non_interactive_missing_and_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify main(['bootstrap-gcp', ...]) reports GCLOUD_MISSING and READY JSON cleanly."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path / "repo"))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    rc_missing = main(["bootstrap-gcp", "--non-interactive", "--no-install-gcloud"])
    missing_out: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert rc_missing == 0
    assert missing_out["status"] == "GCLOUD_MISSING"
    assert missing_out["gcloud_installed"] is False

    _install_fake_gcloud_script(fake_home)
    paths = AlfredPaths.from_env(tmp_path / "repo")
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    (paths.secrets_dir / "client_secrets.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "sec_e2e"}}),
        encoding="utf-8",
    )
    (paths.secrets_dir / "token.json").write_text('{"token": "tok_e2e"}', encoding="utf-8")

    rc_ready = main(
        [
            "bootstrap-gcp",
            "--project-id",
            "alfred-e2e-01",
            "--project-name",
            "Alfred E2E",
            "--non-interactive",
            "--skip-oauth-token",
            "--finish-setup",
        ]
    )
    ready_out: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert rc_ready == 0
    assert ready_out["status"] == "READY"
    assert ready_out["project_id"] == "alfred-e2e-01"
    assert ready_out["workspaces_initialized"] is True
    assert ready_out["routines_installed"] is True


def test_cli_controller_bootstrap_gcp_wires_services_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify build_cli_controller supports bootstrap-gcp with mocked workspace/routine deps."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    _install_fake_gcloud_script(fake_home)

    paths = AlfredPaths.from_env(tmp_path / "repo")
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    (paths.secrets_dir / "client_secrets.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "sec_ctrl"}}),
        encoding="utf-8",
    )
    (paths.secrets_dir / "token.json").write_text('{"token": "tok_ctrl"}', encoding="utf-8")

    ws_mock = MagicMock()
    ws_mock.init_workspaces.return_value = []
    routine_mock = MagicMock()
    routine_mock.install_systemd_timers.return_value = []
    ctrl = build_cli_controller(
        paths=paths,
        settings=AlfredSettings(),
        state_store=StateStore(paths),
        workspace_service=ws_mock,
        routine_service=routine_mock,
    )
    rc = ctrl.run(
        [
            "bootstrap-gcp",
            "--project-id",
            "alfred-e2e-01",
            "--non-interactive",
            "--skip-oauth-token",
            "--finish-setup",
        ]
    )
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "READY"
    ws_mock.init_workspaces.assert_called_once()
    routine_mock.install_systemd_timers.assert_called_once()


def test_bootstrap_script_supports_onboard_and_preserves_offline_default(tmp_path: Path) -> None:
    """Verify scripts/bootstrap.sh runs bootstrap-gcp ONLY when --onboard is supplied."""
    script = WORKTREE_ROOT / "scripts" / "bootstrap.sh"
    assert script.is_file() and os.access(script, os.X_OK)
    content = script.read_text(encoding="utf-8")
    assert "--onboard" in content
    assert "bootstrap-gcp" in content

    sandbox = tmp_path / "sandbox"
    (sandbox / "core" / "config").mkdir(parents=True)
    (sandbox / "core" / "config" / "settings.example.yaml").write_text(
        "project_name: Sandbox\n", encoding="utf-8"
    )
    (sandbox / "core" / "workspaces" / "alfred-onboard").mkdir(parents=True)
    (sandbox / ".venv" / "bin").mkdir(parents=True)
    py_link = sandbox / ".venv" / "bin" / "python"
    py_link.symlink_to(Path(sys.executable))

    log_file = sandbox / "alfred_calls.log"
    alfred_stub = sandbox / ".venv" / "bin" / "alfred"
    alfred_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log_file}"\n',
        encoding="utf-8",
    )
    alfred_stub.chmod(0o755)
    pip_stub = sandbox / ".venv" / "bin" / "pip"
    pip_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    pip_stub.chmod(0o755)

    env = {**os.environ, "ALFRED_ROOT_DIR": str(sandbox)}
    res_offline = subprocess.run(
        ["bash", str(script), "--skip-pip"], env=env, capture_output=True, text=True, check=False
    )
    assert res_offline.returncode == 0, res_offline.stderr + res_offline.stdout
    assert (sandbox / "instance" / "config" / "settings.yaml").is_file()
    offline_calls = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    assert "bootstrap-gcp" not in offline_calls

    res_onboard = subprocess.run(
        ["bash", str(script), "--skip-pip", "--onboard"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_onboard.returncode == 0, res_onboard.stderr + res_onboard.stdout
    onboard_calls = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    assert "bootstrap-gcp" in onboard_calls
