"""Provision and verify the 4 distinct Antigravity projects for Alfred V7.1 Lean."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from alfred.domain.models import AlfredPaths, AlfredSettings, WorkspaceProjectStatus
from alfred.repositories.json_repository import atomic_write_text
from alfred.services.agy_session_manager import AgySessionManager

CANONICAL_WORKSPACE_ROLES: tuple[str, ...] = ("life", "inbox", "calendar", "evolve")
LEGACY_PRUNABLE_NAMES: frozenset[str] = frozenset({"Alfred", "CLI Project"})


class WorkspaceService:
    """Idempotently provision, link, and verify Antigravity workspace projects."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        agy_manager: AgySessionManager,
        projects_dir: Path | None = None,
        conversations_db: Path | None = None,
        runner_fn: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        """Initialize the Antigravity workspace provisioning service.

        Args:
            paths: Resolved AlfredPaths layout.
            settings: Runtime structural settings including workspace names.
            agy_manager: Session manager providing executable agy locator and archive store.
            projects_dir: Optional override for ~/.gemini/config/projects.
            conversations_db: Optional override for conversation_summaries.db.
            runner_fn: Optional subprocess runner override taking (cmd, cwd).
        """
        self._paths = paths
        self._settings = settings
        self._agy = agy_manager
        home = Path.home()
        self._projects_dir = projects_dir or (home / ".gemini" / "config" / "projects")
        self._conversations_db = conversations_db or (
            home / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
        )
        self._runner_fn = runner_fn

    def init_workspaces(self, prune_legacy: bool = False) -> list[WorkspaceProjectStatus]:
        """Provision the 4 Antigravity workspace projects and optionally prune empty legacy ones.

        Args:
            prune_legacy: When True, remove empty 'Alfred' / 'CLI Project' entries with 0 sessions.

        Returns:
            Ordered list of WorkspaceProjectStatus descriptors for the 4 workspaces.
        """
        self._projects_dir.mkdir(parents=True, exist_ok=True)
        statuses: list[WorkspaceProjectStatus] = []
        keep_ids: set[str] = set()

        for role in CANONICAL_WORKSPACE_ROLES:
            project_name = self._settings.resolve_workspace_project(role)
            ws_dir = self._resolve_workspace_dir(role, project_name)
            self._ensure_agents_symlink(ws_dir)
            project_id, created = self._ensure_antigravity_project(project_name, ws_dir)
            keep_ids.add(project_id)
            agents = self._verify_project_agents(project_name, ws_dir)
            statuses.append(
                WorkspaceProjectStatus(
                    role=role,
                    project_name=project_name,
                    project_id=project_id,
                    workspace_dir=str(ws_dir),
                    agents_discovered=agents,
                    created=created,
                )
            )

        if prune_legacy:
            self._agy.archive_store.prune_orphan_default_cli_project(self._projects_dir)
            self._prune_unused_legacy_projects(keep_ids)
        return statuses

    def _resolve_workspace_dir(self, role: str, project_name: str) -> Path:
        """Resolve and create the physical workspace directory for a role."""
        direct = self._paths.workspaces_dir / project_name
        default_dir = self._paths.workspaces_dir / f"alfred-{role}"
        ws_dir = direct if direct.exists() or not default_dir.exists() else default_dir
        ws_dir.mkdir(parents=True, exist_ok=True)
        return ws_dir

    def _ensure_agents_symlink(self, ws_dir: Path) -> None:
        """Create a relative .agents symlink inside ws_dir pointing to root .agents."""
        root_agents = self._paths.root_dir / ".agents"
        if not root_agents.is_dir():
            return
        link_path = ws_dir / ".agents"
        rel_target = Path(os.path.relpath(root_agents, ws_dir))
        if link_path.is_symlink():
            if link_path.resolve() == root_agents.resolve():
                return
            link_path.unlink()
        elif link_path.exists():
            return
        link_path.symlink_to(rel_target)

    def _scan_projects(self) -> list[tuple[Path, dict[str, object]]]:
        """Read all valid JSON project manifests in the Antigravity projects directory."""
        entries: list[tuple[Path, dict[str, object]]] = []
        if not self._projects_dir.is_dir():
            return entries
        for path in sorted(self._projects_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                entries.append((path, {str(k): v for k, v in raw.items()}))
        return entries

    def _find_matching_project(
        self,
        project_name: str,
        target_uri: str,
    ) -> tuple[Path, dict[str, object]] | None:
        """Locate an existing project manifest by exact name or primary folderUri."""
        for path, payload in self._scan_projects():
            if payload.get("name") == project_name:
                return (path, payload)
            res_block = payload.get("projectResources")
            resources = res_block.get("resources") if isinstance(res_block, dict) else None
            if isinstance(resources, list):
                for item in resources:
                    if isinstance(item, dict) and item.get("folderUri") == target_uri:
                        return (path, payload)
        return None

    def _ensure_antigravity_project(self, project_name: str, ws_dir: Path) -> tuple[str, bool]:
        """Register or synchronize a single Antigravity project and return (project_id, created)."""
        target_uri = ws_dir.resolve().as_uri()
        existing = self._find_matching_project(project_name, target_uri)
        if existing is not None:
            path, payload = existing
            return (self._sync_project_manifest(path, payload, project_name, target_uri), False)

        agy_bin = self._agy.locate_agy_binary()
        if agy_bin is not None:
            self._run_cmd([agy_bin, "--new-project", "agents"], ws_dir)
            created_hit = self._find_matching_project(project_name, target_uri)
            if created_hit is not None:
                path, payload = created_hit
                return (self._sync_project_manifest(path, payload, project_name, target_uri), True)

        proj_id = str(uuid.uuid4())
        manifest = {
            "id": proj_id,
            "name": project_name,
            "projectResources": {"resources": [{"folderUri": target_uri}]},
        }
        atomic_write_text(
            self._projects_dir / f"{proj_id}.json",
            json.dumps(manifest, indent=2) + "\n",
        )
        return (proj_id, True)

    def _sync_project_manifest(
        self,
        path: Path,
        payload: dict[str, object],
        project_name: str,
        target_uri: str,
    ) -> str:
        """Ensure project name and primary folderUri match configuration and return project ID."""
        proj_id = str(payload.get("id") or path.stem)
        updated = dict(payload)
        dirty = False
        if updated.get("name") != project_name:
            updated["name"] = project_name
            dirty = True
        res_block = updated.get("projectResources")
        resources = res_block.get("resources") if isinstance(res_block, dict) else None
        if not isinstance(resources, list) or not resources:
            updated["projectResources"] = {"resources": [{"folderUri": target_uri}]}
            dirty = True
        if dirty:
            atomic_write_text(path, json.dumps(updated, indent=2) + "\n")
        return proj_id

    def _verify_project_agents(self, project_name: str, ws_dir: Path) -> list[str]:
        """Verify subagent discovery for project_name via agy CLI when available."""
        agy_bin = self._agy.locate_agy_binary()
        if agy_bin is None:
            return []
        res = self._run_cmd([agy_bin, "--project", project_name, "agents"], ws_dir)
        if res is None or res.returncode != 0:
            return []
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        return [ln for ln in lines if not ln.lower().startswith("available agents")]

    def _load_busy_project_ids(self) -> set[str]:
        """Query conversation_summaries.db read-only to protect projects with live trajectories."""
        if not self._conversations_db.is_file():
            return set()
        conv_dir = self._conversations_db.parent / "conversations"
        try:
            conn = sqlite3.connect(f"file:{self._conversations_db}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT conversation_id, project_id FROM conversation_summaries"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return set()
        busy: set[str] = set()
        for conv_id, proj_id in rows:
            if not proj_id:
                continue
            if not conv_dir.is_dir() or (conv_dir / f"{conv_id}.db").is_file():
                busy.add(str(proj_id))
        return busy

    def _prune_unused_legacy_projects(self, keep_ids: set[str]) -> None:
        """Remove empty legacy 'Alfred' or 'CLI Project' manifests without conversations."""
        busy_ids = self._load_busy_project_ids()
        for path, payload in self._scan_projects():
            proj_id = str(payload.get("id") or path.stem)
            name = str(payload.get("name") or "")
            if proj_id in keep_ids or proj_id in busy_ids:
                continue
            if name in LEGACY_PRUNABLE_NAMES or proj_id == "default-cli-project":
                path.unlink(missing_ok=True)

    def _run_cmd(
        self,
        cmd: list[str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str] | None:
        """Execute an agy CLI command in cwd using injected runner or subprocess."""
        try:
            if self._runner_fn is not None:
                return self._runner_fn(cmd, cwd)
            return subprocess.run(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None
