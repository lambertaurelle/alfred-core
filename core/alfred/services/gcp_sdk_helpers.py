"""Pure validation and archive extraction helpers for Google Cloud SDK bootstrap."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from alfred.repositories.json_repository import atomic_write_text

_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_SDK_BASE_URL = "https://dl.google.com/cloudsdk/channels/rapid/downloads/"


def validate_project_id(project_id: str) -> str:
    """Validate GCP project_id against official rules to block shell injection.

    Args:
        project_id: Candidate Google Cloud project identifier.

    Returns:
        Validated project_id string.

    Raises:
        ValueError: If project_id violates GCP naming rules or contains unsafe chars.
    """
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError(
            "Invalid GCP project_id: must be 6-30 lowercase letters, digits, or hyphens, "
            "starting with a letter and ending with an alphanumeric character."
        )
    return project_id


def is_valid_project_id(project_id: str) -> bool:
    """Return True when project_id matches GCP naming constraints.

    Args:
        project_id: Candidate Google Cloud project identifier.

    Returns:
        Boolean validity flag.
    """
    return bool(isinstance(project_id, str) and _PROJECT_ID_RE.fullmatch(project_id))


def validate_sdk_download_url(url: str) -> str:
    """Verify the SDK archive URL strictly uses HTTPS on official dl.google.com path.

    Args:
        url: Candidate download URL string.

    Returns:
        Stripped validated HTTPS URL.

    Raises:
        ValueError: If scheme, host, or path deviates from official Google Cloud SDK URLs.
    """
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme != "https"
        or parsed.netloc != "dl.google.com"
        or not parsed.path.startswith("/cloudsdk/channels/rapid/downloads/")
    ):
        raise ValueError(f"Untrusted gcloud SDK download URL rejected: {url}")
    return url.strip()


def select_sdk_url(machine_arch: str) -> str:
    """Return official Google Cloud CLI Linux tarball URL matching CPU architecture.

    Args:
        machine_arch: Machine architecture token (e.g. 'x86_64', 'aarch64').

    Returns:
        Official HTTPS download URL.

    Raises:
        ValueError: If architecture is unsupported.
    """
    norm = machine_arch.strip().lower()
    if norm in {"x86_64", "amd64"}:
        return f"{_SDK_BASE_URL}google-cloud-cli-linux-x86_64.tar.gz"
    if norm in {"aarch64", "arm64"}:
        return f"{_SDK_BASE_URL}google-cloud-cli-linux-arm.tar.gz"
    raise ValueError(f"Unsupported CPU architecture for gcloud SDK auto-install: {machine_arch}")


def validate_tar_members(tf: tarfile.TarFile, dest_root: Path) -> list[tarfile.TarInfo]:
    """Validate all archive members against traversal, absolute paths, and escaping links.

    Args:
        tf: Opened TarFile archive to inspect before extraction.
        dest_root: Target local installation directory.

    Returns:
        List of validated TarInfo members safe to extract into dest_root.

    Raises:
        ValueError: If any member attempts path traversal or symlink escape.
    """
    resolved_root = dest_root.resolve()
    members = tf.getmembers()
    for member in members:
        if member.name.startswith(("/", "\\")):
            raise ValueError(f"Unsafe absolute path in tarball: {member.name}")
        rel_path = Path(member.name)
        if ".." in rel_path.parts:
            raise ValueError(f"Unsafe path traversal in tarball: {member.name}")
        target_path = (resolved_root / rel_path).resolve()
        if not target_path.is_relative_to(resolved_root):
            raise ValueError(f"Tarball entry escapes destination directory: {member.name}")
        if member.issym() or member.islnk():
            link_path = Path(member.linkname)
            if link_path.is_absolute():
                raise ValueError(f"Unsafe absolute link target in tarball: {member.linkname}")
            resolved_link = (target_path.parent / link_path).resolve()
            if not resolved_link.is_relative_to(resolved_root):
                raise ValueError(f"Tarball symlink escapes destination directory: {member.name}")
    return members


def validate_and_save_client_json(secrets_dir: Path, raw_json: str) -> Path:
    """Validate Desktop OAuth client JSON payload and save atomically with 0600 permissions.

    Args:
        secrets_dir: Target secrets directory (instance/secrets).
        raw_json: Raw pasted JSON string from Google Cloud Console.

    Returns:
        Resolved Path to the written client_secrets.json file.

    Raises:
        ValueError: If JSON is malformed or missing non-empty client credentials.
    """
    try:
        parsed = json.loads(raw_json)
    except ValueError as exc:
        raise ValueError("Malformed OAuth JSON payload.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("OAuth JSON payload must be a JSON object.")
    block = parsed.get("installed") or parsed.get("web")
    if not isinstance(block, dict):
        raise ValueError("OAuth JSON missing 'installed' or 'web' credentials object.")
    cid, csec = block.get("client_id"), block.get("client_secret")
    valid_creds = (
        isinstance(cid, str) and bool(cid.strip())
        and isinstance(csec, str) and bool(csec.strip())
    )
    if not valid_creds:
        raise ValueError("OAuth JSON credentials require non-empty client_id and secret.")
    target = secrets_dir / "client_secrets.json"
    atomic_write_text(target, json.dumps(parsed, indent=2) + "\n")
    os.chmod(target, 0o600)
    return target


def default_subprocess_runner(
    cmd: list[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Execute a subprocess command capturing text output without shell interpolation.

    Args:
        cmd: Argument vector to execute.
        **kwargs: Additional subprocess.run keyword arguments.

    Returns:
        CompletedProcess result with text stdout/stderr.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    return subprocess.run(cmd, **kwargs)


def default_sdk_downloader(url: str, dest: Path) -> None:
    """Download an official HTTPS Google Cloud SDK archive into dest.

    Args:
        url: Official HTTPS Google Cloud SDK download URL.
        dest: Local archive destination file path.
    """
    validated = validate_sdk_download_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(validated, timeout=120) as resp:  # noqa: S310
        dest.write_bytes(resp.read())
