"""AST-driven living specification generator and zero-leak validator for docs/SPEC.md."""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Sequence
from pathlib import Path

from alfred.domain.evolve_models import (
    CliCommandSpec,
    CoreModuleEntry,
    CoreSpecSnapshot,
    FeatureSpecEntry,
    WorkspaceSpecEntry,
)
from alfred.domain.models import AlfredPaths
from alfred.repositories.json_repository import atomic_write_text
from alfred.services.core_spec_ast import (
    extract_cli_commands_from_source,
    extract_extension_tokens_ast,
    extract_features_from_source,
)

__all__ = [
    "BEGIN_GENERATED",
    "CoreSpecService",
    "END_GENERATED",
    "extract_cli_commands_from_source",
    "extract_features_from_source",
]

BEGIN_GENERATED = "<!-- BEGIN ALFRED_CORE_GENERATED_SPEC -->"
END_GENERATED = "<!-- END ALFRED_CORE_GENERATED_SPEC -->"

_LEGACY_V7_DOCS: tuple[str, ...] = (
    "docs/vision/alfred_vision_and_architecture.md",
    "docs/architecture/implementation_plan.md",
)
_BANNED_V7_TERMS: tuple[str, ...] = (
    "nou" + "nou",
    "pajem" + "ploi",
    "version " + "7.1",
    "v7.1 " + "lean",
    "instance/" + ".git",
    "dual-" + "git",
)
_ARCHITECTURE_LAYERS: tuple[str, ...] = ("domain", "repositories", "services", "controllers")


def _first_docstring_line(tree: ast.Module) -> str:
    """Return the first non-empty line of a parsed module docstring, if any."""
    doc = ast.get_docstring(tree) or ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


class CoreSpecService:
    """AST-only living specification builder, synchronizer, and leak validator."""

    def __init__(self, paths: AlfredPaths | None = None) -> None:
        """Initialize CoreSpecService with optional AlfredPaths context.

        Args:
            paths: Optional resolved AlfredPaths instance.
        """
        self._paths = paths

    def inspect_core_snapshot(self, repo_root: Path) -> CoreSpecSnapshot:
        """Build a deterministic CoreSpecSnapshot via pure AST and static file inspection.

        Args:
            repo_root: Target mono-repo root path.

        Returns:
            Populated CoreSpecSnapshot for alfred-core.
        """
        root = repo_root.resolve()
        return CoreSpecSnapshot(
            version=self._read_project_version(root),
            workspaces=self._inspect_workspaces(root),
            features=self._inspect_features(root),
            modules=self._inspect_core_modules(root),
            cli_commands=self._inspect_cli_commands(root),
        )

    def render_generated_section(self, snapshot: CoreSpecSnapshot) -> str:
        """Render the deterministic Markdown section bounded by SPEC markers.

        Args:
            snapshot: Extracted CoreSpecSnapshot to format.

        Returns:
            Generated Markdown block including BEGIN_GENERATED and END_GENERATED markers.
        """
        lines: list[str] = [
            BEGIN_GENERATED,
            f"## Inventaire Généré (`alfred-core` v{snapshot.version})",
            "",
            "### Workspaces Spécialisés (`core/workspaces/`)",
            "| Workspace | Description |",
            "| :--- | :--- |",
        ]
        for ws in snapshot.workspaces:
            lines.append(f"| `{ws.name}` | {ws.summary} |")
        lines.extend(
            [
                "",
                "### Feature Flags, APIs GCP & Scopes OAuth2",
                "| Feature Flag | Défaut | APIs GCP | Scopes OAuth2 |",
                "| :--- | :---: | :--- | :--- |",
            ]
        )
        for feat in snapshot.features:
            apis = ", ".join(f"`{a}`" for a in feat.gcp_apis) or "—"
            scopes = ", ".join(f"`{s.rsplit('/', 1)[-1]}`" for s in feat.oauth_scopes) or "—"
            flag_def = str(feat.default_enabled).lower()
            lines.append(f"| `{feat.flag_name}` | `{flag_def}` | {apis} | {scopes} |")
        lines.extend(
            [
                "",
                "### Modules Clean Architecture (`core/alfred/`)",
                "| Couche | Module | Rôle |",
                "| :--- | :--- | :--- |",
            ]
        )
        for mod in snapshot.modules:
            lines.append(f"| `{mod.layer}` | `{mod.module_path}` | {mod.summary} |")
        lines.extend(
            [
                "",
                "### Commandes CLI (`alfred <subcommand>`)",
                "| Commande | Options | Source |",
                "| :--- | :--- | :--- |",
            ]
        )
        for cmd in snapshot.cli_commands:
            opts = ", ".join(f"`{o}`" for o in cmd.options) or "—"
            lines.append(f"| `{cmd.name}` | {opts} | `{cmd.source_module}` |")
        lines.append(END_GENERATED)
        return "\n".join(lines)

    def sync_spec_file(self, repo_root: Path) -> Path:
        """Create or update the generated section of docs/SPEC.md while preserving curated prose.

        Args:
            repo_root: Target mono-repo root path.

        Returns:
            Resolved Path to docs/SPEC.md.

        Raises:
            ValueError: If an existing docs/SPEC.md lacks BEGIN_GENERATED/END_GENERATED markers.
        """
        root = repo_root.resolve()
        spec_path = root / "docs" / "SPEC.md"
        snapshot = self.inspect_core_snapshot(root)
        generated_block = self.render_generated_section(snapshot)
        if spec_path.is_file():
            existing = spec_path.read_text(encoding="utf-8")
            if BEGIN_GENERATED not in existing or END_GENERATED not in existing:
                raise ValueError(f"Missing generated SPEC markers in {spec_path}.")
            b_idx = existing.index(BEGIN_GENERATED)
            e_idx = existing.index(END_GENERATED) + len(END_GENERATED)
            if b_idx > e_idx:
                raise ValueError(f"Malformed generated SPEC marker order in {spec_path}.")
            new_content = f"{existing[:b_idx]}{generated_block}{existing[e_idx:]}"
        else:
            new_content = (
                f"# Alfred V{snapshot.version} — Spécification Technique Vivante\n\n"
                f"Document de référence mono-repo (`core/` générique open-source vs "
                f"`instance/` privé gitignoré).\n\n{generated_block}\n"
            )
        atomic_write_text(spec_path, new_content)
        Path(f"{spec_path.resolve()}.lock").unlink(missing_ok=True)
        return spec_path

    def validate_spec_file(
        self,
        repo_root: Path,
        instance_dirs: Sequence[Path] = (),
    ) -> list[str]:
        """Validate legacy doc cleanup, SPEC freshness, zero banned terms, and zero leaks.

        Args:
            repo_root: Target mono-repo root path.
            instance_dirs: Optional live external instance directories to scan by AST.

        Returns:
            List of human-readable validation errors (empty when compliant).
        """
        root = repo_root.resolve()
        errors = [
            f"Legacy V7.1 documentation must be removed: {rel_legacy}"
            for rel_legacy in _LEGACY_V7_DOCS
            if (root / rel_legacy).exists()
        ]
        spec_path = root / "docs" / "SPEC.md"
        if not spec_path.is_file():
            errors.append(f"Missing living specification file: {spec_path}")
            return errors
        raw = spec_path.read_text(encoding="utf-8")
        if BEGIN_GENERATED not in raw or END_GENERATED not in raw:
            errors.append("docs/SPEC.md is missing BEGIN/END generated spec markers.")
        else:
            b_idx = raw.index(BEGIN_GENERATED)
            e_idx = raw.index(END_GENERATED) + len(END_GENERATED)
            if b_idx >= e_idx:
                errors.append("docs/SPEC.md has inverted BEGIN/END markers.")
            else:
                expected = self.render_generated_section(self.inspect_core_snapshot(root))
                if raw[b_idx:e_idx] != expected:
                    errors.append("docs/SPEC.md generated section is out of sync (run sync-spec).")
        lowered = raw.lower()
        for banned in _BANNED_V7_TERMS:
            if banned in lowered:
                errors.append(f"Banned legacy term found in docs/SPEC.md: '{banned}'")
        errors.extend(self._detect_extension_leaks(root, raw, instance_dirs))
        return errors

    def _read_project_version(self, root: Path) -> str:
        """Read project.version from pyproject.toml without importing code."""
        pyproject = root / "pyproject.toml"
        if not pyproject.is_file():
            return "0.0.0"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data.get("project")
        if isinstance(project, dict):
            ver = project.get("version")
            if isinstance(ver, str):
                return ver
        return "0.0.0"

    def _inspect_workspaces(self, root: Path) -> list[WorkspaceSpecEntry]:
        """Extract specialist workspace names and summaries from core/workspaces/*/AGENTS.md."""
        ws_root = root / "core" / "workspaces"
        if not ws_root.is_dir():
            return []
        entries: list[WorkspaceSpecEntry] = []
        for ws_dir in sorted(ws_root.iterdir()):
            agents_md = ws_dir / "AGENTS.md"
            if not ws_dir.is_dir() or not agents_md.is_file():
                continue
            entries.append(
                WorkspaceSpecEntry(
                    name=ws_dir.name,
                    summary=self._extract_markdown_summary(agents_md),
                )
            )
        return entries

    def _extract_markdown_summary(self, md_path: Path) -> str:
        """Pick the first descriptive prose line or fallback heading from a workspace AGENTS.md."""
        heading = ""
        for line in md_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped == "---" or stripped.startswith(">"):
                continue
            if stripped.startswith("#"):
                if not heading:
                    heading = stripped.lstrip("#").strip()
                continue
            return stripped
        return heading

    def _inspect_features(self, root: Path) -> list[FeatureSpecEntry]:
        """Extract FeatureFlagsConfig inventory from domain/feature_models.py."""
        feat_file = root / "core" / "alfred" / "domain" / "feature_models.py"
        if not feat_file.is_file():
            return []
        return extract_features_from_source(feat_file.read_text(encoding="utf-8"))

    def _inspect_core_modules(self, root: Path) -> list[CoreModuleEntry]:
        """Extract 4-layer modules and main.py summaries using pure AST."""
        alfred_dir = root / "core" / "alfred"
        if not alfred_dir.is_dir():
            return []
        modules: list[CoreModuleEntry] = []
        for layer in _ARCHITECTURE_LAYERS:
            layer_dir = alfred_dir / layer
            if not layer_dir.is_dir():
                continue
            for py_file in sorted(layer_dir.glob("*.py")):
                if py_file.name == "__init__.py":
                    continue
                modules.append(self._parse_module_entry(layer, py_file, root))
        main_py = alfred_dir / "main.py"
        if main_py.is_file():
            modules.append(self._parse_module_entry("main", main_py, root))
        return modules

    def _parse_module_entry(self, layer: str, py_file: Path, root: Path) -> CoreModuleEntry:
        """Parse one module AST safely to extract its first docstring line."""
        rel = py_file.relative_to(root).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            summary = _first_docstring_line(tree)
        except (OSError, SyntaxError):
            summary = ""
        return CoreModuleEntry(layer=layer, module_path=rel, summary=summary)

    def _inspect_cli_commands(self, root: Path) -> list[CliCommandSpec]:
        """Inspect all controller modules via AST and return sorted CliCommandSpec list."""
        ctl_dir = root / "core" / "alfred" / "controllers"
        if not ctl_dir.is_dir():
            return []
        specs: dict[str, CliCommandSpec] = {}
        for py_file in sorted(ctl_dir.glob("*.py")):
            if py_file.name == "__init__.py":
                continue
            rel = py_file.relative_to(root).as_posix()
            for cmd in extract_cli_commands_from_source(
                py_file.read_text(encoding="utf-8"), source_module=rel
            ):
                if cmd.name in specs:
                    merged_opts = list(dict.fromkeys([*specs[cmd.name].options, *cmd.options]))
                    specs[cmd.name] = CliCommandSpec(
                        name=cmd.name,
                        options=merged_opts,
                        source_module=specs[cmd.name].source_module,
                    )
                else:
                    specs[cmd.name] = cmd
        return [specs[k] for k in sorted(specs)]

    def _detect_extension_leaks(
        self,
        root: Path,
        spec_text: str,
        instance_dirs: Sequence[Path],
    ) -> list[str]:
        """Verify no private extension stem or AST extension_id leaks into docs/SPEC.md."""
        candidate_dirs: list[Path] = [root / "instance" / "extensions"]
        for inst_dir in instance_dirs:
            resolved = inst_dir.resolve()
            candidate_dirs.append(resolved)
            ext_sub = resolved / "extensions"
            if ext_sub.is_dir():
                candidate_dirs.append(ext_sub)
        tokens: list[str] = []
        seen_files: set[Path] = set()
        for folder in candidate_dirs:
            if not folder.is_dir():
                continue
            for py_file in sorted(folder.glob("*.py")):
                rp = py_file.resolve()
                is_ignored = py_file.name.startswith("_") or py_file.name.endswith(".example.py")
                if rp in seen_files or is_ignored:
                    continue
                seen_files.add(rp)
                tokens.extend(extract_extension_tokens_ast(py_file))
        errors: list[str] = []
        for tok in dict.fromkeys(t for t in tokens if len(t) >= 3):
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])"
            if re.search(pattern, spec_text, flags=re.IGNORECASE):
                errors.append(f"Private extension identifier leaked in docs/SPEC.md: '{tok}'")
        return errors
