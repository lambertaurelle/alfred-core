"""Dynamic loader, validator, and Drive backup helper for instance/extensions plugins."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from alfred.domain.extension_protocol import (
    AlfredExtension,
    ExtensionContext,
    ExtensionJobOutcome,
)
from alfred.domain.models import AlfredPaths, AlfredSettings


class ExtensionLoaderService:
    """Discover, validate, and execute personal extensions + back up gitignored configs."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        backup_fn: Callable[[str, str], str] | None = None,
    ) -> None:
        """Initialize the extension loader service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including extensions_config.
            backup_fn: Optional callback uploading private config/extension files to Drive.
        """
        self._paths = paths
        self._settings = settings
        self._backup_fn = backup_fn

    def discover_active_extensions(
        self,
        override_dir: Path | None = None,
    ) -> list[AlfredExtension]:
        """Import and validate all active Python extensions in instance/extensions/.

        Args:
            override_dir: Optional directory override (e.g. for testing instance/examples/).

        Returns:
            Ordered list of validated AlfredExtension instances.
        """
        target_dir = override_dir or self._paths.extensions_dir
        if not target_dir.is_dir():
            return []
        loaded: list[AlfredExtension] = []
        include_examples = override_dir is not None
        for py_file in sorted(target_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            if not include_examples and py_file.name.endswith(".example.py"):
                continue
            ext = self._load_extension_module(py_file)
            if ext is not None:
                loaded.append(ext)
        return loaded

    def run_all_periodic(self, today_iso: str) -> list[ExtensionJobOutcome]:
        """Run periodic jobs across all active personal extensions.

        Args:
            today_iso: Reference ISO date ('YYYY-MM-DD').

        Returns:
            Aggregated list of ExtensionJobOutcome items.
        """
        outcomes: list[ExtensionJobOutcome] = []
        for ext in self.discover_active_extensions():
            ext_cfg = self._settings.extensions_config.get(ext.extension_id, {})
            errors = ext.validate_config(ext_cfg)
            if errors:
                outcomes.append(
                    ExtensionJobOutcome(
                        extension_id=ext.extension_id,
                        job_name="config_validation",
                        status="INVALID_CONFIG",
                        summary="; ".join(errors),
                    )
                )
                continue
            ctx = ExtensionContext(
                today_iso=today_iso,
                timezone=self._settings.timezone,
                extension_settings=ext_cfg,
            )
            outcomes.extend(ext.run_periodic(ctx))
        return outcomes

    def backup_private_instance_files(self) -> list[str]:
        """Back up gitignored instance/config/settings.yaml and active extensions to Drive.

        Returns:
            List of backed-up Drive file_id strings.
        """
        if self._backup_fn is None or not self._settings.features.drive_knowledge_sync:
            return []
        uploaded_ids: list[str] = []
        if self._paths.settings_path.is_file():
            raw_cfg = self._paths.settings_path.read_text(encoding="utf-8")
            fid = self._backup_fn("_backup_settings.yaml", raw_cfg)
            if fid:
                uploaded_ids.append(fid)
        if self._paths.extensions_dir.is_dir():
            for py_file in sorted(self._paths.extensions_dir.glob("*.py")):
                if py_file.name.startswith("_") or py_file.name.endswith(".example.py"):
                    continue
                fid = self._backup_fn(
                    f"_backup_ext_{py_file.name}",
                    py_file.read_text(encoding="utf-8"),
                )
                if fid:
                    uploaded_ids.append(fid)
        return uploaded_ids

    def _load_extension_module(self, py_file: Path) -> AlfredExtension | None:
        """Dynamically import a Python file and invoke build_extension() if compliant."""
        mod_name = f"alfred_ext_{py_file.stem.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, py_file)
        if spec is None or spec.loader is None:
            return None
        module: ModuleType = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        factory = getattr(module, "build_extension", None)
        if not callable(factory):
            return None
        candidate = factory()
        return candidate if isinstance(candidate, AlfredExtension) else None
