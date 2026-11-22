"""Unit tests for GcpBootstrapService gcloud detection, install, auth, APIs, and OAuth."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from alfred.domain.feature_models import FeatureFlagsConfig
from alfred.domain.models import AlfredPaths, AlfredSettings


def _build_valid_gcloud_tarball(dest: Path) -> None:
    """Write a safe synthetic google-cloud-sdk.tar.gz archive to dest."""
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


def test_inspect_gcloud_absent_vs_installed_in_local_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify inspect_gcloud detects missing binary vs ~/.local/google-cloud-sdk/bin/gcloud."""
    from alfred.domain.onboarding_models import GcloudStatus
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    paths = AlfredPaths.from_env(tmp_path / "repo")
    svc_absent = GcpBootstrapService(paths, AlfredSettings())
    st_absent = svc_absent.inspect_gcloud()
    assert isinstance(st_absent, GcloudStatus)
    assert st_absent.installed is False
    assert st_absent.binary_path == ""

    gcloud_bin = fake_home / ".local" / "google-cloud-sdk" / "bin" / "gcloud"
    gcloud_bin.parent.mkdir(parents=True)
    gcloud_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gcloud_bin.chmod(0o755)

    def _runner(cmd: list[str], *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return configured account and project from fake gcloud config calls."""
        joined = " ".join(cmd)
        if "get-value account" in joined:
            return subprocess.CompletedProcess(cmd, 0, "alice@example.com\n", "")
        if "get-value project" in joined:
            return subprocess.CompletedProcess(cmd, 0, "alfred-proj-01\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    svc_present = GcpBootstrapService(paths, AlfredSettings(), runner_fn=_runner)
    st_present = svc_present.inspect_gcloud()
    assert st_present.installed is True
    assert Path(st_present.binary_path).resolve() == gcloud_bin.resolve()
    assert st_present.active_account == "alice@example.com"
    assert st_present.current_project == "alfred-proj-01"


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_ensure_gcloud_installed_multi_arch_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arch: str
) -> None:
    """Verify multi-arch SDK download, install.sh --disable-prompts, and idempotence."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    paths = AlfredPaths.from_env(tmp_path / "repo")
    install_root = fake_home / ".local"
    urls: list[str] = []
    cmds: list[list[str]] = []

    def _download(url: str, dest: Path) -> None:
        """Record official SDK URL and write a safe synthetic tarball."""
        urls.append(url)
        _build_valid_gcloud_tarball(dest)

    def _runner(cmd: list[str], *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record install.sh invocation."""
        cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    svc = GcpBootstrapService(
        paths,
        AlfredSettings(),
        runner_fn=_runner,
        downloader_fn=_download,
        install_root=install_root,
        machine_arch=arch,
    )
    bin_path, installed_now = svc.ensure_gcloud_installed(auto_install=True)
    assert installed_now is True
    assert bin_path is not None and bin_path.is_file()
    assert len(urls) == 1
    assert urls[0].startswith("https://dl.google.com/cloudsdk/channels/rapid/downloads/")
    if arch == "x86_64":
        assert "x86_64" in urls[0]
    else:
        assert "arm" in urls[0] or "aarch64" in urls[0]
    assert any("install.sh" in " ".join(c) and "--disable-prompts" in c for c in cmds)

    bin_again, installed_again = svc.ensure_gcloud_installed(auto_install=True)
    assert installed_again is False
    assert bin_again == bin_path
    assert len(urls) == 1


def test_ensure_gcloud_installed_no_auto_install_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ensure_gcloud_installed(auto_install=False) skips install when absent."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    svc = GcpBootstrapService(AlfredPaths.from_env(tmp_path / "repo"), AlfredSettings())
    bin_path, installed_now = svc.ensure_gcloud_installed(auto_install=False)
    assert bin_path is None
    assert installed_now is False


def test_authenticate_gcloud_reuses_active_login_interactive_and_blocks_noninteractive(
    tmp_path: Path,
) -> None:
    """Verify authenticate_gcloud reuses active account, runs --no-launch-browser, or fails."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    paths = AlfredPaths.from_env(tmp_path)
    gcloud_bin = tmp_path / "gcloud"

    def _active_runner(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Return existing active account immediately."""
        return subprocess.CompletedProcess(cmd, 0, "alice@example.com\n", "")

    svc_ok = GcpBootstrapService(paths, AlfredSettings(), runner_fn=_active_runner)
    assert svc_ok.authenticate_gcloud(gcloud_bin, interactive=False) == "alice@example.com"

    state = {"authed": False}
    login_cmds: list[list[str]] = []

    def _login_runner(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Simulate unauthenticated state until auth login --no-launch-browser runs."""
        login_cmds.append(list(cmd))
        joined = " ".join(cmd)
        if "auth login" in joined:
            state["authed"] = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "get-value account" in joined:
            out = "alice@example.com\n" if state["authed"] else "(unset)\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    svc_non_int = GcpBootstrapService(paths, AlfredSettings(), runner_fn=_login_runner)
    with pytest.raises(RuntimeError):
        svc_non_int.authenticate_gcloud(gcloud_bin, interactive=False)

    svc_int = GcpBootstrapService(paths, AlfredSettings(), runner_fn=_login_runner)
    assert svc_int.authenticate_gcloud(gcloud_bin, interactive=True) == "alice@example.com"
    assert any("auth" in c and "login" in c and "--no-launch-browser" in c for c in login_cmds)


def test_ensure_project_and_enable_apis_creates_or_reuses_and_computes_flags(
    tmp_path: Path,
) -> None:
    """Verify project creation vs reuse and dynamic API enablement from FeatureFlagsConfig."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    paths = AlfredPaths.from_env(tmp_path)
    gcloud_bin = tmp_path / "gcloud"
    minimal_features = FeatureFlagsConfig(
        drive_knowledge_sync=True,
        inbox_auto_triage=False,
        gmail_drafts_and_send=False,
        calendar_management=False,
        tasks_management=True,
        contacts_lookup=False,
    )
    settings = AlfredSettings(features=minimal_features)
    expected_apis = minimal_features.compute_gcp_apis()
    cmds_create: list[list[str]] = []

    def _create_runner(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Simulate missing project on describe, then project creation and services enable."""
        cmds_create.append(list(cmd))
        if "projects" in cmd and "describe" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "NOT_FOUND")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    svc_new = GcpBootstrapService(paths, settings, runner_fn=_create_runner)
    pid, created, apis = svc_new.ensure_project_and_enable_apis(
        gcloud_bin, project_id="alfred-new-01", project_name="Alfred Home"
    )
    assert pid == "alfred-new-01"
    assert created is True
    assert apis == expected_apis
    assert any("projects" in c and "create" in c and "alfred-new-01" in c for c in cmds_create)
    enable_cmd = next(c for c in cmds_create if "services" in c and "enable" in c)
    for api in expected_apis:
        assert api in enable_cmd
    assert "calendar-json.googleapis.com" not in enable_cmd
    assert "people.googleapis.com" not in enable_cmd

    cmds_reuse: list[list[str]] = []

    def _reuse_runner(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Simulate existing project on describe."""
        cmds_reuse.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "projectId: alfred-existing-01\n", "")

    svc_reuse = GcpBootstrapService(paths, settings, runner_fn=_reuse_runner)
    pid2, created2, apis2 = svc_reuse.ensure_project_and_enable_apis(
        gcloud_bin, project_id="alfred-existing-01"
    )
    assert pid2 == "alfred-existing-01"
    assert created2 is False
    assert apis2 == expected_apis
    assert not any("projects" in c and "create" in c for c in cmds_reuse)


def test_maybe_ingest_oauth_client_json_atomic_0600_and_no_secret_return(tmp_path: Path) -> None:
    """Verify JSON paste writes client_secrets.json atomically at 0600 and returns bool."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    paths = AlfredPaths.from_env(tmp_path)
    raw_json = json.dumps(
        {
            "installed": {
                "client_id": "12345.apps.googleusercontent.com",
                "client_secret": "fake_secret_value",
                "redirect_uris": ["http://localhost"],
            }
        }
    )

    svc_non_int = GcpBootstrapService(paths, AlfredSettings(), input_fn=lambda _p: raw_json)
    assert svc_non_int.maybe_ingest_oauth_client_json("alfred-01", interactive=False) is False

    svc_int = GcpBootstrapService(paths, AlfredSettings(), input_fn=lambda _p: raw_json)
    result = svc_int.maybe_ingest_oauth_client_json("alfred-01", interactive=True)
    assert result is True
    dest = paths.secrets_dir / "client_secrets.json"
    assert dest.is_file()
    assert (dest.stat().st_mode & 0o777) == 0o600
    loaded_secret = json.loads(dest.read_text(encoding="utf-8"))
    assert loaded_secret["installed"]["client_id"].startswith("12345")

    called: list[str] = []

    def _record_prompt(prompt: str) -> str:
        """Record unexpected prompt and return OAuth JSON."""
        called.append(prompt)
        return raw_json

    svc_existing = GcpBootstrapService(paths, AlfredSettings(), input_fn=_record_prompt)
    assert svc_existing.maybe_ingest_oauth_client_json("alfred-01", interactive=True) is True
    assert called == []
