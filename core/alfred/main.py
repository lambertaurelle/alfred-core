"""Alfred V8.0 Family Office CLI entrypoint and dependency injection bootstrap (< 80 lines)."""

from __future__ import annotations

import sys

from alfred.controllers.app_factory import build_cli_controller
from alfred.controllers.cli_controller import CliController


def build_controller(root_override: str | None = None) -> CliController:
    """Wire Clean Architecture repositories, services, and V8 CLI controller.

    Args:
        root_override: Optional override for ALFRED_ROOT_DIR.

    Returns:
        Fully wired CliController instance.
    """
    return build_cli_controller(root_override=root_override)


def main(argv: list[str] | None = None) -> int:
    """Execute the Alfred V8.0 CLI entrypoint.

    Args:
        argv: Optional argument list; defaults to sys.argv[1:].

    Returns:
        Exit status code.
    """
    return build_controller().run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
