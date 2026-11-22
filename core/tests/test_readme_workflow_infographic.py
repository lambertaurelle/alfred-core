"""Tests for README workflow infographic asset, placement, and binary leak guard."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from alfred.services.core_spec_service import CoreSpecService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
_IMAGE_REL_PATH = "docs/assets/alfred-workflow.jpg"
_NO_PAYLOAD_MARKERS = {0x00, 0x01, *range(0xD0, 0xD9)}
_FORBIDDEN_METADATA_MARKERS = {0xFE, *range(0xE1, 0xF0)}
_SOF_MARKERS = {0xC0, 0xC2}


def _parse_jpeg_segments(data: bytes) -> list[tuple[int, bytes]]:
    """Parse JPEG segments from SOI up to SOS or EOI using pure Python.

    Args:
        data: Raw JPEG file bytes.

    Returns:
        List of (marker_byte, payload_bytes) tuples before SOS or EOI.
    """
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("Missing JPEG SOI marker (0xFFD8).")

    segments: list[tuple[int, bytes]] = []
    pos = 2
    total = len(data)
    saw_terminal_marker = False

    while pos < total:
        if data[pos] != 0xFF:
            raise ValueError(
                f"Expected 0xFF JPEG marker prefix at byte {pos}, got 0x{data[pos]:02x}."
            )
        while pos < total and data[pos] == 0xFF:
            pos += 1
        if pos >= total:
            break

        marker = data[pos]
        pos += 1
        if marker in {0xDA, 0xD9}:
            saw_terminal_marker = True
            break
        if marker in _NO_PAYLOAD_MARKERS:
            continue
        if pos + 2 > total:
            raise ValueError(f"Truncated length field for JPEG marker 0xFF{marker:02X}.")

        seg_len = int.from_bytes(data[pos : pos + 2], "big")
        if seg_len < 2 or pos + seg_len > total:
            raise ValueError(f"Invalid length {seg_len} for JPEG marker 0xFF{marker:02X}.")

        segments.append((marker, data[pos + 2 : pos + seg_len]))
        pos += seg_len

    if not saw_terminal_marker:
        raise ValueError("JPEG stream ended before SOS (0xFFDA) or EOI (0xFFD9).")
    return segments


def _extract_sof_dimensions(segments: list[tuple[int, bytes]]) -> tuple[int, int]:
    """Extract (width, height) from the first SOF0 or SOF2 JPEG segment.

    Args:
        segments: Parsed JPEG marker-payload pairs.

    Returns:
        Image dimensions as (width, height) in pixels.
    """
    for marker, payload in segments:
        if marker in _SOF_MARKERS:
            assert len(payload) >= 5, f"Truncated SOF payload for marker 0xFF{marker:02X}."
            height = int.from_bytes(payload[1:3], "big")
            width = int.from_bytes(payload[3:5], "big")
            return width, height
    raise AssertionError("JPEG image lacks a SOF0 (0xFFC0) or SOF2 (0xFFC2) frame header.")


def _init_clean_git_repo(repo_dir: Path) -> None:
    """Initialize a minimal clean Git repository with an initial README commit.

    Args:
        repo_dir: Target directory for the temporary Git repository.
    """
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Alfred Maintainer"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "alfred@example.com"],
        cwd=repo_dir,
        check=True,
    )
    (repo_dir / "README.md").write_text("# Clean Repo\n\nSafe documentation.\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial clean commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


def _run_leak_guard(repo_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run check-upstream-leak.sh isolated against a target repository directory.

    Args:
        repo_dir: Target Git repository root passed to the leak guard script.

    Returns:
        CompletedProcess result with captured stdout and stderr.
    """
    script_path = WORKTREE_ROOT / "core" / "scripts" / "check-upstream-leak.sh"
    env = {**os.environ, "ALFRED_ROOT_DIR": str(repo_dir)}
    return subprocess.run(
        ["bash", str(script_path), str(repo_dir)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_workflow_infographic_asset_exists_and_valid_jpeg() -> None:
    """Verify the workflow infographic exists as a valid landscape JPEG asset."""
    asset = WORKTREE_ROOT / _IMAGE_REL_PATH
    assert asset.exists(), f"Missing infographic asset: {asset}"
    assert not asset.is_symlink(), f"Infographic asset must not be a symlink: {asset}"
    assert asset.is_file(), f"Infographic asset must be a regular file: {asset}"

    raw = asset.read_bytes()
    assert len(raw) > 50_000, f"Expected JPEG size > 50000 bytes, got {len(raw)}"
    assert raw.startswith(b"\xff\xd8"), "JPEG asset must start with SOI marker 0xFFD8."
    assert raw.endswith(b"\xff\xd9"), "JPEG asset must end with EOI marker 0xFFD9."

    segments = _parse_jpeg_segments(raw)
    width, height = _extract_sof_dimensions(segments)
    assert width >= 1200, f"Expected landscape width >= 1200px, got {width}"
    assert height >= 650, f"Expected landscape height >= 650px, got {height}"
    assert width > height, (
        f"Expected landscape orientation (width > height), got {width}x{height}"
    )


def test_readme_places_infographic_at_top_before_first_h2() -> None:
    """Verify README.md embeds the workflow infographic before the first H2 heading."""
    readme = (WORKTREE_ROOT / "README.md").read_text(encoding="utf-8")
    intro_section = readme.split("\n## ", maxsplit=1)[0]
    pattern = r"!\[[^\]]+\]\(docs/assets/alfred-workflow\.jpg\)"
    assert re.search(pattern, intro_section) is not None, (
        "README.md must include ![...](docs/assets/alfred-workflow.jpg) before the first '## '."
    )


def test_leak_guard_ignores_binary_image_bytes_without_stderr_noise(tmp_path: Path) -> None:
    """Verify check-upstream-leak.sh skips binary image files cleanly without grep noise.

    Args:
        tmp_path: Pytest temporary directory fixture.
    """
    _init_clean_git_repo(tmp_path)
    assets_dir = tmp_path / "docs" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    host_token = ".".join(["192", "168", "1", "42"])
    ct_token = "CT " + "703"
    sample_bytes = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
        + f"Host {host_token} {ct_token}".encode("ascii")
        + b"\xff\xd9"
    )
    (assets_dir / "sample.jpg").write_bytes(sample_bytes)
    subprocess.run(["git", "add", "docs/assets/sample.jpg"], cwd=tmp_path, check=True)

    res = _run_leak_guard(tmp_path)
    combined_output = f"{res.stderr}\n{res.stdout}"
    assert res.returncode == 0, f"Leak guard failed on binary image:\n{combined_output}"
    assert "binary file matches" not in combined_output.lower()


def test_leak_guard_still_blocks_sensitive_text_files(tmp_path: Path) -> None:
    """Verify check-upstream-leak.sh still rejects private IPs and webmail in text files.

    Args:
        tmp_path: Pytest temporary directory fixture.
    """
    _init_clean_git_repo(tmp_path)
    private_ip = ".".join(["192", "168", "1", "42"])
    webmail = "alice@" + "gmail" + ".com"
    (tmp_path / "notes.md").write_text(
        f"Server {private_ip} owner {webmail}\n",
        encoding="utf-8",
    )

    res_untracked = _run_leak_guard(tmp_path)
    assert res_untracked.returncode != 0

    subprocess.run(["git", "add", "notes.md"], cwd=tmp_path, check=True)
    res_staged = _run_leak_guard(tmp_path)
    assert res_staged.returncode != 0


def test_readme_e2e_line_budget_links_and_spec_integrity() -> None:
    """Verify README line budget, required keywords, safe relative image links, and SPEC sync."""
    readme = (WORKTREE_ROOT / "README.md").read_text(encoding="utf-8")
    lines = readme.splitlines()
    assert len(lines) <= 195, f"README.md exceeds 195 lines ({len(lines)})."

    exact_keywords = (
        "Gemini App",
        "--onboard",
        "|",
        "drive",
        "gmail.modify",
        "calendar",
        "tasks",
        "contacts",
        "https://github.com/<owner>/alfred-core.git",
    )
    for token in exact_keywords:
        assert token in readme, f"Missing required README keyword: {token}"

    lowered = readme.lower()
    for ci_token in ("cloudflared", "tls"):
        assert ci_token in lowered, (
            f"Missing required README keyword (case-insensitive): {ci_token}"
        )

    worktree_resolved = WORKTREE_ROOT.resolve()
    for raw_target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme):
        target = raw_target.strip().split()[0].removeprefix("<").removesuffix(">")
        if target.startswith(("http://", "https://", "data:")):
            continue
        rel_path = Path(target)
        assert not rel_path.is_absolute(), f"Image path must be relative: {target}"
        assert ".." not in rel_path.parts, f"Parent traversal forbidden in image path: {target}"
        cur = WORKTREE_ROOT
        for part in rel_path.parts:
            if part == ".":
                continue
            cur = cur / part
            assert not cur.is_symlink(), f"Symlink component forbidden in image path: {cur}"
        resolved = (WORKTREE_ROOT / rel_path).resolve()
        assert resolved.is_relative_to(worktree_resolved), f"Image escapes root: {resolved}"
        assert resolved.is_file(), f"Missing relative image target: {resolved}"

    assert CoreSpecService().validate_spec_file(WORKTREE_ROOT) == []


def test_infographic_jpeg_strips_exif_and_comment_metadata_segments() -> None:
    """Verify the infographic JPEG contains no COM or APP1..APP15 metadata and zero symlinks."""
    docs_dir = WORKTREE_ROOT / "docs"
    assets_dir = docs_dir / "assets"
    assert not docs_dir.is_symlink(), f"Forbidden symlink: {docs_dir}"
    assert assets_dir.is_dir() and not assets_dir.is_symlink(), (
        f"Assets directory must be a non-symlink directory: {assets_dir}"
    )
    for entry in assets_dir.rglob("*"):
        assert not entry.is_symlink(), f"Forbidden symlink inside assets: {entry}"

    asset = WORKTREE_ROOT / _IMAGE_REL_PATH
    assert asset.is_file() and not asset.is_symlink(), f"Missing regular file: {asset}"
    segments = _parse_jpeg_segments(asset.read_bytes())
    found_forbidden = [
        f"0xFF{marker:02X}" for marker, _ in segments if marker in _FORBIDDEN_METADATA_MARKERS
    ]
    assert not found_forbidden, (
        f"Infographic JPEG contains unstripped metadata segments: {found_forbidden}"
    )
