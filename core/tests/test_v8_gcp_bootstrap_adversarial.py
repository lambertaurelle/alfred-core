"""Adversarial Red-Team tests for GcpBootstrapService injection, tarball, URL, and leaks."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.main import main


def _write_malicious_tarball(dest: Path, *, mode: str) -> None:
    """Create a malicious .tar.gz archive containing path traversal or escaping symlinks."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tf:
        if mode == "traversal":
            info = tarfile.TarInfo(name="../../etc/cron.d/pwn")
            body = b"* * * * * root id\n"
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        elif mode == "absolute":
            info = tarfile.TarInfo(name="/etc/passwd")
            body = b"pwn\n"
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        elif mode == "symlink":
            sym = tarfile.TarInfo(name="google-cloud-sdk/bad_link")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "../../etc/shadow"
            tf.addfile(sym)


@pytest.mark.parametrize(
    "bad_project_id",
    [
        "; rm -rf /",
        "UPPERCASE_ID",
        "../etc",
        "-leading-hyphen",
        "ab",
        "$(touch /tmp/pwn)",
        "bad space id",
    ],
)
def test_adversarial_project_id_injection_rejected(
    tmp_path: Path, bad_project_id: str
) -> None:
    """Verify invalid or shell-injected GCP project_id values raise ValueError before subprocess."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    cmds: list[list[str]] = []

    def _runner(cmd: list[str], *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record any unexpected subprocess invocation."""
        cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "alice@example.com\n", "")

    svc = GcpBootstrapService(AlfredPaths.from_env(tmp_path), AlfredSettings(), runner_fn=_runner)
    with pytest.raises(ValueError):
        svc.ensure_project_and_enable_apis(tmp_path / "gcloud", project_id=bad_project_id)
    assert cmds == []

    with pytest.raises(ValueError):
        svc.run_full_bootstrap(project_id=bad_project_id, interactive=False, run_oauth_token=False)


@pytest.mark.parametrize("tar_mode", ["traversal", "absolute", "symlink"])
def test_adversarial_tarball_traversal_and_symlink_blocked_before_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tar_mode: str
) -> None:
    """Verify path-traversal and escaping-symlink archives raise ValueError before disk write."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    install_root = fake_home / ".local"
    install_root.mkdir()
    svc = GcpBootstrapService(
        AlfredPaths.from_env(tmp_path / "repo"),
        AlfredSettings(),
        downloader_fn=lambda _u, dest: _write_malicious_tarball(dest, mode=tar_mode),
        install_root=install_root,
        machine_arch="x86_64",
    )
    with pytest.raises(ValueError):
        svc.ensure_gcloud_installed(auto_install=True)
    assert not (install_root / "google-cloud-sdk").exists()


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.example.com/google-cloud-cli-linux-x86_64.tar.gz",
        "https://dl.google.com.evil.example/cloudsdk/channels/rapid/downloads/sdk.tar.gz",
        "http://dl.google.com/cloudsdk/channels/rapid/downloads/sdk.tar.gz",
        "file:///tmp/google-cloud-cli-linux-x86_64.tar.gz",
    ],
)
def test_adversarial_untrusted_download_url_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """Verify non-official gcloud download URLs raise ValueError without invoking downloader."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    called: list[str] = []
    svc = GcpBootstrapService(
        AlfredPaths.from_env(tmp_path / "repo"),
        AlfredSettings(),
        downloader_fn=lambda u, _d: called.append(u),
        install_root=fake_home / ".local",
    )
    with pytest.raises(ValueError):
        svc.ensure_gcloud_installed(auto_install=True, download_url_override=bad_url)
    assert called == []


@pytest.mark.parametrize(
    "bad_payload",
    [
        "{broken json",
        "{}",
        json.dumps({"installed": {"client_id": "only_cid"}}),
        json.dumps({"installed": {"client_secret": "only_sec"}}),
        json.dumps({"installed": {"client_id": "", "client_secret": ""}}),
    ],
)
def test_adversarial_malformed_oauth_json_rejected_without_corrupting_existing(
    tmp_path: Path, bad_payload: str
) -> None:
    """Verify malformed/incomplete OAuth JSON raises ValueError and keeps valid file intact."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    paths = AlfredPaths.from_env(tmp_path / "fresh")
    svc = GcpBootstrapService(paths, AlfredSettings(), input_fn=lambda _p: bad_payload)
    with pytest.raises(ValueError):
        svc.maybe_ingest_oauth_client_json("alfred-proj-01", interactive=True)
    assert not (paths.secrets_dir / "client_secrets.json").exists()

    paths_existing = AlfredPaths.from_env(tmp_path / "existing")
    paths_existing.secrets_dir.mkdir(parents=True, exist_ok=True)
    existing_file = paths_existing.secrets_dir / "client_secrets.json"
    original_bytes = json.dumps(
        {"installed": {"client_id": "valid_cid", "client_secret": "valid_sec_orig"}}
    ).encode("utf-8")
    existing_file.write_bytes(original_bytes)

    svc_existing = GcpBootstrapService(
        paths_existing, AlfredSettings(), input_fn=lambda _p: bad_payload
    )
    assert svc_existing.maybe_ingest_oauth_client_json("alfred-proj-01", interactive=True) is True
    assert existing_file.read_bytes() == original_bytes


def _seed_secrets_and_gcloud(fake_home: Path, paths: AlfredPaths) -> None:
    """Create stub gcloud binary and synthetic secret files for leak assertion tests."""
    gcloud_bin = fake_home / ".local" / "google-cloud-sdk" / "bin" / "gcloud"
    gcloud_bin.parent.mkdir(parents=True, exist_ok=True)
    gcloud_bin.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"get-value account"*) echo "alice@example.com" ;;\n'
        '  *"auth list"*) echo "alice@example.com" ;;\n'
        '  *"get-value project"*) echo "alfred-sec-01" ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gcloud_bin.chmod(0o755)
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    (paths.secrets_dir / "client_secrets.json").write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid.apps.googleusercontent.com",
                    "client_secret": "fake_secret_xyz",
                }
            }
        ),
        encoding="utf-8",
    )
    (paths.secrets_dir / "token.json").write_text(
        json.dumps({"token": "fake_access_xyz", "refresh_token": "fake_refresh_xyz"}),
        encoding="utf-8",
    )


def test_adversarial_no_raw_secrets_leaked_in_report_or_cli_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify client_secret and refresh tokens never leak into GcpBootstrapReport or stdout."""
    from alfred.services.gcp_bootstrap_service import GcpBootstrapService

    fake_home = tmp_path / "home"
    paths = AlfredPaths.from_env(tmp_path / "repo")
    _seed_secrets_and_gcloud(fake_home, paths)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    def _runner(cmd: list[str], *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return active account and project ok."""
        if "get-value account" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, "alice@example.com\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    svc = GcpBootstrapService(paths, AlfredSettings(), runner_fn=_runner)
    report = svc.run_full_bootstrap(
        project_id="alfred-sec-01", interactive=False, run_oauth_token=False
    )
    dumped = json.dumps(report.model_dump(mode="json"))

    monkeypatch.setenv("ALFRED_ROOT_DIR", str(paths.root_dir))
    assert (
        main(
            [
                "bootstrap-gcp",
                "--project-id",
                "alfred-sec-01",
                "--non-interactive",
                "--skip-oauth-token",
            ]
        )
        == 0
    )
    stdout = capsys.readouterr().out
    for secret_tok in ("fake_secret_xyz", "fake_access_xyz", "fake_refresh_xyz", "client_secret"):
        assert secret_tok not in dumped
        assert secret_tok not in stdout
