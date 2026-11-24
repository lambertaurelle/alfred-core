# 🧬 Workspace `alfred-evolve` — Isolated Worktree Mono-Repo Evolution & Conflict-Free TDD Pipeline

> **Model Routing (Brain)**: `gemini-pro-orchestrator` (User-Initiated or SPF/DKIM-Authenticated Owner Delegation)  
> **Alfred Root & CLI**: `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Mandatory Pipeline**: **1. `evolve-qualifier` (`Read-Only`) $\rightarrow$ 2. `implementation_plan.md` Gate $\rightarrow$ 3. Isolated Pipeline (`tdd-red` $\rightarrow$ `tdd-green` $\rightarrow$ `doc-maintainer` $\rightarrow$ `tdd-verifier`) per `--mode` (`instance` | `upstream-pr` | `local-fork`)**

Isolated git worktree engineering workflow with architectural qualification, 4-stage TDD + doc-maintainer triad, and zero-leak PRs.

## 1. Stage 1 — Architectural Qualification & Security (`evolve-qualifier`, `Read-Only`)
- **`evolve-qualifier`** inspects `docs/SPEC.md`, `core/`, and `instance/` to select one of 3 modes:
  - **`--mode instance`**: strictly personal logic confined to `instance/` (zero PR, `docs/SPEC.md` unchanged; deterministic failure if anything outside `instance/` is modified).
  - **`--mode upstream-pr`**: generic capability in `core/` (or anonymized examples under `instance/examples/`). When `--hybrid`, the `core/` slice must be **100% standalone** (`--core-complete`) without `instance/` dependencies. Automatically enforces English-only repo rules (`check_english_only.py`), syncs `docs/SPEC.md` (`sync-spec`), flags `[BREAKING]` changes (`--breaking`), and supports GitHub fork fallback if `origin` denies push.
  - **`--mode local-fork`**: local `core/` modification committed on `evolve/<id>` without opening an upstream PR.
- If triggered from an email, verify it originates from an SPF/DKIM-authenticated owner address (`EmailSecurityService`).
- Preserve **100% English repository code/docs/tests/templates** while maintaining the **Bilingual FR/EN Runtime Invariant** (`BILINGUAL_RUNTIME_STRING_ALLOWLIST`).

## 2. Stage 2 — Systematic Gate (`implementation_plan.md`)
- `plan-synthesizer` authors **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`) specifying the chosen mode (`instance` / `upstream-pr` / `local-fork`), 4-layer OOP design (`domain <- repositories <- services <- controllers <- main`), **regression** / non-regression impact analysis, the 3 test tiers (**unit**, **functional**, **e2e**), and the **`swarm` adversarial testing** campaign.
- Zero branches or code modifications are made before user **`Proceed`**.

## 3. Stage 3 — Isolated `git worktree` Execution (`EvolveWorktreeService`)
Unlike operational workspaces handled by `plan-executor`, every `core/` evolution in `alfred-evolve` executes in an isolated `git worktree` (`worktrees/<proposal_id>`, branch `evolve/<proposal_id>`) via **4 separated-context subagents**:
1. **`tdd-red`** *(Tests only)*: writes initially failing tests under `core/tests/` covering all 3 tiers (**unit**, **functional**, and **e2e**), **regression** checks, and **`swarm` adversarial** scenarios with English docstrings/comments (strictly forbidden from editing `core/alfred/`).
2. **`tdd-green`** *(Production only)*: implements clean OOP production code respecting `<= 500L/file`, `<= 60L/method` excluding docstrings, 100% English Google docstrings, and bilingual FR/EN runtime detectors (strictly forbidden from editing, weakening, or skipping `core/tests/`).
3. **`doc-maintainer`** *(Docs, Manifests & SPEC only)*: updates `README.md` (`<= 195` lines), `AGENTS.md`, `.agents/agents/*.md`, templates, and `docs/SPEC.md` (`sync-spec`) in 100% English while preserving bilingual FR/EN runtime guidance (strictly forbidden from editing `core/tests/`, `core/alfred/`, or `core/scripts/`).
4. **`tdd-verifier`** *(Independent Read-Only Audit)*: verifies test integrity (unit, functional, e2e, regression, `swarm` adversarial) and enforces all **6 blocking deterministic guardians** + `sync-spec --check`:
   - `check_code_architecture.py --repo-root <worktree>` (including zero `Instance Coupling`)
   - `check-upstream-leak.sh <worktree>` (zero personal data on `HEAD`)
   - `check_english_only.py --repo-root <worktree>` (100% English repo compliance)
   - `ruff check core` & `mypy --strict core/alfred core/scripts core/tests`
   - `pytest --cov=core --cov-fail-under=90` & `alfred sync-spec --check --repo-root <worktree>`
5. Finalization: `alfred evolve --id "<slug>" --title "<title>" --description "<spec>" --mode <mode> --execute` (adding `--hybrid --core-complete` or `--breaking` as applicable).
