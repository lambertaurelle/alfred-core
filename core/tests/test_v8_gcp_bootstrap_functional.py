"""Functional tests for GcpBootstrapService pipeline, degraded states, and onboarding."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.state_store import StateStore
from alfred.services.onboarding_service import OnboardingService


def _write_sdk_tarball(dest: Path) -> None:
    """Create a synthetic google-cloud-sdk tarball for offline functional tests."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tf:
        for rel, body, mode in (
            ("google-cloud-sdk/install.sh", b"#!/bin/sh\nexit 0\n", 0o755),
            ("google-cloud-sdk/bin/gcloud", b"#!/bin/sh\nexit 0\n", 0o755),
        ):
            info = tarfile.TarInfo(name=rel)
            info.size = len(body)
            info.mode = mode
            tf.addfile(info, io.BytesIO(body))


def _build_happy_runner_and_mocks(
    paths: AlfredPaths,
) -> tuple[Any, MagicMock, MagicMock, MagicMock]:
    """Create simulated gcloud runner and OAuth/Workspace/Routine mocks."""
    state = {"authed": False, "proj": False}

    def _runner(cmd: list[str], *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Simulate gcloud auth, project creation, and API enablement."""
        joined = " ".join(cmd)
        if "auth login" in joined:
            state["authed"] = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "get-value account" in joined:
            out = "alice@example.com\n" if state["authed"] else "(unset)\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if "projects describe" in joined:
            rc = 0 if state["proj"] else 1
            return subprocess.CompletedProcess(cmd, rc, "", "" if rc == 0 else "NOT_FOUND")
        if "projects create" in joined:
            state["proj"] = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _fake_token(**_kwargs: Any) -> Path:
        """Create synthetic token.json when headless OAuth runs."""
        tok = paths.secrets_dir / "token.json"
        tok.parent.mkdir(parents=True, exist_ok=True)
        tok.write_text('{"token": "tok_ok"}', encoding="utf-8")
        return tok

    oauth_mock = MagicMock()
    oauth_mock.bootstrap_interactive_token.side_effect = _fake_token
    ws_mock = MagicMock()
    ws_mock.init_workspaces.return_value = []
    routine_mock = MagicMock()
    routine_mock.install_systemd_timers.return_value = []
    return _runner, oauth_mock, ws_mock, routine_mock


def test_run_full_bootstrap_happy_path_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify full pipeline from gcloud install through OAuth, workspaces, and routines."""
    from alfred.domain.onboarding_models import GcpBootstrapReport
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    paths = AlfredPaths.from_env(tmp_path / "repo")
    settings = AlfredSettings()
    runner, oauth_mock, ws_mock, routine_mock = _build_happy_runner_and_mocks(paths)
    oauth_json = json.dumps(
        {"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec_val"}}
    )
    svc = GcpBootstrapService(
        paths,
        settings,
        runner_fn=runner,
        downloader_fn=lambda _url, dest: _write_sdk_tarball(dest),
        input_fn=lambda _p: oauth_json,
        install_root=fake_home / ".local",
        machine_arch="x86_64",
    )
    report = svc.run_full_bootstrap(
        project_id="alfred-home-001",
        project_name="Alfred Family",
        auto_install_gcloud=True,
        interactive=True,
        run_oauth_token=True,
        finish_setup=True,
        oauth_service=oauth_mock,
        workspace_service=ws_mock,
        routine_service=routine_mock,
    )

    assert isinstance(report, GcpBootstrapReport)
    assert report.status == "READY"
    assert report.gcloud_installed and report.gcloud_installed_now
    assert report.gcloud_account == "alice@example.com"
    assert report.project_id == "alfred-home-001" and report.project_created
    assert report.apis_enabled == settings.features.compute_gcp_apis()
    assert report.client_secrets_present and report.token_present
    assert report.workspaces_initialized and report.routines_installed
    assert "?project=alfred-home-001" in report.audience_url
    assert "?project=alfred-home-001" in report.clients_url
    oauth_mock.bootstrap_interactive_token.assert_called_once()
    ws_mock.init_workspaces.assert_called_once()
    routine_mock.install_systemd_timers.assert_called_once()


def _create_stub_gcloud(fake_home: Path) -> Path:
    """Create an executable stub gcloud binary under fake_home/.local/google-cloud-sdk/bin."""
    gcloud_bin = fake_home / ".local" / "google-cloud-sdk" / "bin" / "gcloud"
    gcloud_bin.parent.mkdir(parents=True, exist_ok=True)
    gcloud_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gcloud_bin.chmod(0o755)
    return gcloud_bin


def test_run_full_bootstrap_degraded_modes_with_targeted_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify graceful GCLOUD_MISSING, NEEDS_GCLOUD_AUTH, and NEEDS_OAUTH_CLIENT_JSON reports."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    paths, settings = AlfredPaths.from_env(tmp_path / "repo"), AlfredSettings()

    svc_missing = GcpBootstrapService(paths, settings, install_root=fake_home / ".local")
    rep_missing = svc_missing.run_full_bootstrap(
        auto_install_gcloud=False, interactive=False, run_oauth_token=False
    )
    assert rep_missing.status == "GCLOUD_MISSING"
    assert rep_missing.gcloud_installed is False and rep_missing.next_actions

    _create_stub_gcloud(fake_home)
    svc_no_auth = GcpBootstrapService(
        paths,
        settings,
        runner_fn=lambda cmd, *_a, **_k: subprocess.CompletedProcess(cmd, 0, "(unset)\n", ""),
    )
    rep_no_auth = svc_no_auth.run_full_bootstrap(interactive=False, run_oauth_token=False)
    assert rep_no_auth.status == "NEEDS_GCLOUD_AUTH"
    assert rep_no_auth.gcloud_installed is True and rep_no_auth.gcloud_account == ""
    assert rep_no_auth.next_actions

    def _authed_runner(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Return active account and allow project/APIs."""
        if "get-value account" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, "alice@example.com\n", "")
        if "projects describe" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, "projectId: alfred-home-002\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    rep_no_json = GcpBootstrapService(paths, settings, runner_fn=_authed_runner).run_full_bootstrap(
        project_id="alfred-home-002", interactive=False, run_oauth_token=False
    )
    assert rep_no_json.status == "NEEDS_OAUTH_CLIENT_JSON"
    assert rep_no_json.client_secrets_present is False
    assert rep_no_json.project_id == "alfred-home-002"
    assert "?project=alfred-home-002" in rep_no_json.audience_url
    assert "?project=alfred-home-002" in rep_no_json.clients_url
    assert rep_no_json.next_actions


def test_onboarding_inspect_live_status_includes_gcloud_fields_without_breaking_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify OnboardingService.inspect_live_status adds gcloud metadata cleanly."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    paths = AlfredPaths.from_env(tmp_path / "repo")
    svc = OnboardingService(paths, AlfredSettings.load(paths), StateStore(paths))
    absent_status = svc.inspect_live_status()

    legacy_keys = {
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
    assert legacy_keys.issubset(absent_status.keys())
    assert absent_status["gcloud_installed"] is False
    assert absent_status["gcloud_account"] == ""
    assert absent_status["gcp_project_id"] == ""

    gcloud_bin = fake_home / ".local" / "google-cloud-sdk" / "bin" / "gcloud"
    gcloud_bin.parent.mkdir(parents=True)
    gcloud_bin.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"get-value account"*) echo "alice@example.com" ;;\n'
        '  *"auth list"*) echo "alice@example.com" ;;\n'
        '  *"get-value project"*) echo "alfred-live-01" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gcloud_bin.chmod(0o755)

    present_status = svc.inspect_live_status()
    assert present_status["gcloud_installed"] is True
    assert present_status["gcloud_account"] == "alice@example.com"
    assert present_status["gcp_project_id"] == "alfred-live-01"
