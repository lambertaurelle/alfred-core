"""Render systemd --user service/timer units and compute stale unit pruning targets."""

from __future__ import annotations

from pathlib import Path

from alfred.domain.models import AlfredPaths, AlfredSettings


def render_alfred_systemd_units(paths: AlfredPaths, settings: AlfredSettings) -> dict[str, str]:
    """Render systemd --user unit files according to enabled modular feature flags.

    Args:
        paths: Resolved AlfredPaths filesystem layout.
        settings: Structural runtime configuration including features and routines blocks.

    Returns:
        Mapping from unit file name to UTF-8 systemd unit text.
    """
    cfg = settings.routines
    alfred_bin = paths.root_dir / ".venv" / "bin" / "alfred"
    local_bin = Path.home() / ".local" / "bin"
    env_path = f"{local_bin}:/usr/local/bin:/usr/bin:/bin"
    common_env = (
        f"WorkingDirectory={paths.root_dir}\n"
        f'Environment="ALFRED_ROOT_DIR={paths.root_dir}"\n'
        f'Environment="PATH={env_path}"\n'
    )
    common_svc = f"Type=oneshot\n{common_env}"
    units: dict[str, str] = {}

    if settings.features.inbox_auto_triage:
        units["alfred-inbox.service"] = (
            "[Unit]\nDescription=Alfred V8.0 — Incremental Gmail Inbox Routine\n\n"
            f"[Service]\n{common_svc}"
            f"ExecStart={alfred_bin} run-routines --only inbox\n"
        )
        units["alfred-inbox.timer"] = (
            "[Unit]\nDescription=Alfred V8.0 — Poll Gmail Inbox Timer\n\n"
            f"[Timer]\nOnBootSec=45s\nOnUnitActiveSec={cfg.inbox_interval_sec}s\n"
            "Persistent=true\nUnit=alfred-inbox.service\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )

    if settings.features.drive_watcher:
        units["alfred-drive.service"] = (
            "[Unit]\nDescription=Alfred V8.0 — Incremental Google Drive Watcher Routine\n\n"
            f"[Service]\n{common_svc}"
            f"ExecStart={alfred_bin} run-routines --only drive\n"
        )
        units["alfred-drive.timer"] = (
            "[Unit]\nDescription=Alfred V8.0 — Poll Google Drive Watcher Timer\n\n"
            f"[Timer]\nOnBootSec=75s\nOnUnitActiveSec={cfg.drive_watcher_interval_sec}s\n"
            "Persistent=true\nUnit=alfred-drive.service\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )

    units["alfred-periodic.service"] = (
        "[Unit]\nDescription=Alfred V8.0 — Periodic Drive & Extensions Routine\n\n"
        f"[Service]\n{common_svc}"
        f"ExecStart={alfred_bin} run-routines --only periodic\n"
    )
    units["alfred-periodic.timer"] = (
        "[Unit]\nDescription=Alfred V8.0 — Periodic Drive & Extensions Timer\n\n"
        f"[Timer]\nOnBootSec=90s\nOnUnitActiveSec={cfg.periodic_interval_sec}s\n"
        "Persistent=true\nUnit=alfred-periodic.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )

    if settings.features.mcp_gemini_bridge:
        units["alfred-mcp.service"] = _render_mcp_service_unit(common_env, alfred_bin)
    return units


def compute_stale_systemd_specs(settings: AlfredSettings) -> list[tuple[str, tuple[str, ...]]]:
    """Return (disable_unit, filenames_to_remove) pairs for legacy or disabled features.

    Args:
        settings: Structural runtime configuration with feature activation flags.

    Returns:
        List of (systemctl_disable_target, unit_filenames) tuples.
    """
    stale: list[tuple[str, tuple[str, ...]]] = [
        ("alfred-routines.timer", ("alfred-routines.service", "alfred-routines.timer")),
    ]
    if not settings.features.inbox_auto_triage:
        stale.append(("alfred-inbox.timer", ("alfred-inbox.service", "alfred-inbox.timer")))
    if not settings.features.drive_watcher:
        stale.append(("alfred-drive.timer", ("alfred-drive.service", "alfred-drive.timer")))
    if not settings.features.mcp_gemini_bridge:
        stale.append(("alfred-mcp.service", ("alfred-mcp.service",)))
    return stale


def _render_mcp_service_unit(common_env: str, alfred_bin: Path) -> str:
    """Render the permanent systemd --user service unit for the MCP OAuth 2.1 bridge."""
    return (
        "[Unit]\n"
        "Description=Alfred V8.0 — MCP OAuth 2.1 Bridge for Gemini App\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{common_env}"
        f"ExecStart={alfred_bin} serve-mcp\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
