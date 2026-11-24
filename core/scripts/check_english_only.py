#!/usr/bin/env python3
"""CLI verifier enforcing the English-only repository policy with bilingual runtime exceptions."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def resolve_default_root() -> Path:
    """Resolve the Alfred repository root directory from environment or script path.

    Returns:
        Resolved repository root directory path.
    """
    env_root = os.environ.get("ALFRED_ROOT_DIR")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[2]


def _ensure_import_path(repo_root: Path) -> None:
    """Prepend the core package directory containing english_policy_service to sys.path."""
    candidates = [
        repo_root / "core",
        Path(__file__).resolve().parents[1],
    ]
    for core_dir in candidates:
        policy_file = core_dir / "alfred" / "services" / "english_policy_service.py"
        if policy_file.is_file():
            core_str = str(core_dir.resolve())
            if core_str not in sys.path:
                sys.path.insert(0, core_str)
            return


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse optional repository root flags for English-only policy checks."""
    parser = argparse.ArgumentParser(description="English-only repository policy verifier")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--root", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the English-only repository scan and return process exit code.

    Args:
        argv: Optional CLI argument list overriding sys.argv[1:].

    Returns:
        Exit code 0 when compliant, 1 when violations are detected.
    """
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    raw_root = ns.repo_root or ns.root
    repo_root = Path(raw_root).resolve() if raw_root else resolve_default_root()
    _ensure_import_path(repo_root)

    from alfred.services.english_policy_service import inspect_repository_english

    violations = inspect_repository_english(repo_root)
    if violations:
        for item in violations:
            print(f"ERROR [English Policy]: {item}", file=sys.stderr)
        print(
            f"\nEnglish-only policy check failed with {len(violations)} violation(s).",
            file=sys.stderr,
        )
        return 1

    print("English-only repository policy check passed (0 violations).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
