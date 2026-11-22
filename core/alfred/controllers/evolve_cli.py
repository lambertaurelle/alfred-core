"""CLI subcommand registration and handlers for alfred evolve and sync-spec."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alfred.domain.evolve_models import EvolutionMode
from alfred.domain.models import AlfredPaths
from alfred.services.core_spec_service import CoreSpecService
from alfred.services.evolve_worktree_service import EvolveWorktreeService

EVOLVE_COMMANDS = frozenset({"evolve", "sync-spec"})


def register_evolve_parsers(sub: Any) -> None:
    """Register V8 mono-repo evolution and living-spec synchronization subparsers.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_ev = sub.add_parser("evolve")
    for flag in ("--id", "--title", "--description"):
        p_ev.add_argument(flag, required=True)
    p_ev.add_argument("--execute", action="store_true")
    p_ev.add_argument("--spawn-agy", action="store_true")
    p_ev.add_argument("--worktree", default="")
    p_ev.add_argument(
        "--mode",
        choices=[m.value for m in EvolutionMode],
        default=EvolutionMode.UPSTREAM_PR.value,
    )
    p_ev.add_argument("--breaking", action="store_true")
    p_ev.add_argument("--draft", action="store_true")
    p_ev.add_argument("--hybrid", action="store_true")
    p_ev.add_argument("--core-complete", action="store_true")

    p_sync = sub.add_parser("sync-spec")
    p_sync.add_argument("--check", action="store_true")
    p_sync.add_argument("--repo-root", default=None)


def dispatch_evolve_command(
    cmd: str,
    evolve_service: EvolveWorktreeService,
    paths: AlfredPaths | None,
    args: argparse.Namespace,
    maybe_spawn_gate_fn: Callable[[str, str, str, bool, str], None] | None = None,
) -> int:
    """Dispatch 'evolve' and 'sync-spec' CLI subcommands cleanly.

    Args:
        cmd: Subcommand name from EVOLVE_COMMANDS.
        evolve_service: Wired EvolveWorktreeService instance.
        paths: Optional resolved AlfredPaths layout.
        args: Parsed CLI namespace.
        maybe_spawn_gate_fn: Optional callback opening an Antigravity Gate session.

    Returns:
        POSIX exit status code (0 on success, 1 on spec drift).
    """
    eff_paths = paths or AlfredPaths.from_env()
    if cmd == "sync-spec":
        return _handle_sync_spec(eff_paths, args)
    return _handle_evolve(evolve_service, args, maybe_spawn_gate_fn)


def _handle_sync_spec(paths: AlfredPaths, args: argparse.Namespace) -> int:
    """Synchronize or validate docs/SPEC.md against AST core introspection."""
    repo_root = Path(str(args.repo_root)).resolve() if args.repo_root else paths.root_dir
    spec_svc = CoreSpecService(paths)
    if bool(args.check):
        errors = spec_svc.validate_spec_file(repo_root)
        payload = {
            "status": "DRIFT" if errors else "OK",
            "spec_path": str(repo_root / "docs" / "SPEC.md"),
            "errors": errors,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1 if errors else 0
    spec_path = spec_svc.sync_spec_file(repo_root)
    payload = {"status": "SYNCED", "spec_path": str(spec_path)}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _handle_evolve(
    evolve_service: EvolveWorktreeService,
    args: argparse.Namespace,
    maybe_spawn_gate_fn: Callable[[str, str, str, bool, str], None] | None,
) -> int:
    """Execute Stage-2 plan generation or Stage-3 instance/fork/upstream-pr workflow."""
    mode = str(args.mode)
    if not bool(args.execute):
        _, plan_md = evolve_service.prepare_evolution_plan(
            str(args.id),
            str(args.title),
            str(args.description),
            mode=mode,
            is_breaking=bool(args.breaking),
            is_hybrid=bool(args.hybrid),
            core_complete=bool(args.core_complete),
        )
        if maybe_spawn_gate_fn is not None:
            maybe_spawn_gate_fn(
                "alfred-evolve",
                f"evolve:{args.id}",
                plan_md,
                bool(args.spawn_agy),
                f"[Evolve] {args.title}",
            )
        print(plan_md)
        return 0

    if mode == EvolutionMode.INSTANCE.value:
        rec = evolve_service.execute_instance_only(
            str(args.id), str(args.title), str(args.description)
        )
        print(json.dumps(rec.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0

    wt = (
        Path(str(args.worktree))
        if str(args.worktree).strip()
        else evolve_service.create_isolated_worktree(str(args.id))
    )
    if mode == EvolutionMode.LOCAL_FORK.value:
        rec = evolve_service.execute_local_fork(
            str(args.id), str(args.title), wt, body=str(args.description)
        )
        print(json.dumps(rec.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0

    pr = evolve_service.execute_and_open_pr(
        proposal_id=str(args.id),
        title=str(args.title),
        body=str(args.description),
        worktree=wt,
        is_hybrid=bool(args.hybrid),
        core_complete=bool(args.core_complete) if bool(args.hybrid) else True,
        is_breaking=bool(args.breaking),
        draft=bool(args.draft),
    )
    print(json.dumps(pr.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0
