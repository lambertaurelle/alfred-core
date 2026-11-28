# 🎩 AGENTS.md — Supreme Orchestration Guide `/root/Alfred` (V8.0 Family Office Chief of Staff)

> **Scope**: Zero-Leak Mono-Repo `/root/Alfred` (`ALFRED_ROOT_DIR`)  
> **Interfaces**: **Antigravity Web UI** + **OAuth 2.1 PKCE MCP Bridge to Gemini App** (`/mcp`)  
> **Standards**: Isolated TDD (`>= 90%`), 4-Layer OOP Clean Architecture (`<= 500L/file`, `<= 60L/method` excluding docstring, `main.py <= 79L`), 100% English-Only Repository (`check_english_only.py`), Bilingual FR/EN Runtime, & Zero Personal Data in Git (`check-upstream-leak.sh`).

---

## 1. The 7 Specialist Workspaces & Model Routing (Brain vs Muscle)

| Workspace | Role | Primary Model | Stage 3 Execution |
| :--- | :--- | :--- | :--- |
| **`alfred-onboard`** | Interactive onboarding, modular flags, GCP OAuth2 & Drive taxonomy | `gemini-pro-orchestrator` (`Orchestrator (Carbon)`) | `plan-executor` (`Orchestrator (Neon)`) |
| **`alfred-life`** | General Family Office hub, cross-domain studies & `alfred_life_brain_dump` MCP | `gemini-pro-orchestrator` (`Orchestrator (Carbon)`) | `plan-executor` (`Orchestrator (Neon)`) |
| **`alfred-inbox`** | New email triage (0-token pre-filter + SPF/DKIM firewall + 3 outputs) | `gemini-pro-orchestrator-neon` (`Orchestrator (Neon)`) | `plan-executor` (`Orchestrator (Neon)`) |
| **`alfred-calendar`** | Multi-calendars, allowlisted `[HOLD]`s & strict self-invite exclusion | `gemini-pro-orchestrator-neon` (`Orchestrator (Neon)`) | `plan-executor` (`Orchestrator (Neon)`) |
| **`alfred-tasks`** | Google Tasks, RFC 3339 due dates & Google People Contacts directory | `gemini-pro-orchestrator-neon` (`Orchestrator (Neon)`) | `plan-executor` (`Orchestrator (Neon)`) |
| **`alfred-drive`** | Non-destructive Drive governance, 2-speed delta watcher & `_ALFRED_REFERENTIEL_` | `gemini-pro-orchestrator-neon` (`Orchestrator (Neon)`) | `plan-executor` / `drive-custodian` (`Orchestrator (Neon)`) |
| **`alfred-evolve`** | Isolated `git worktree` Mono-Repo evolution & `instance/` extensions | `gemini-pro-orchestrator` (`Orchestrator (Carbon)`) | `tdd-red` $\rightarrow$ `tdd-green` $\rightarrow$ `doc-maintainer` $\rightarrow$ `tdd-verifier` |

*`agy` CLI note: never pass `--effort` when the selected model contains `orchestrator`. Auto-archived `[SILENT]` audit traces use `gemini-pro-orchestrator-neon`.*

---

## 2. Universal 3-Stage Pattern (With Asymmetric Execution)

1. **Stage 1 — Adaptive Turbo Analysis & Research (`Read-Only`)**:
   - **0 subagents** when the request is self-contained $\rightarrow$ proceed directly to Stage 2.
   - **Targeted lookup or parallel "Spread"**: `research-drive`, `research-gmail-cal`, `research-web`, escalate **`study-orchestrator`** for multi-source patrimonial, tax, or legal studies, and invoke **`evolve-qualifier`** in `alfred-evolve` to qualify `instance` vs `upstream-pr` vs `local-fork`.
2. **Stage 2 — Synthesis & Systematic Blocking Gate (`implementation_plan.md`)**:
   - `plan-synthesizer` generates **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
   - **BLOCKING STOP**: zero Gmail, Calendar, Tasks, Drive, or code mutations run before the user clicks **`Proceed`**.
3. **Stage 3 — Asymmetric Post-Validation Execution**:
   - **Operational workspaces (`onboard`, `life`, `inbox`, `calendar`, `tasks`, `drive`)**: fast execution by a single lightweight **`Orchestrator (Neon)`** subagent (**`plan-executor`** or **`drive-custodian`**) calling exclusively `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`. By default, every interactively processed email thread is archived in Gmail (`INBOX` removed unless `--keep-in-inbox`).
   - **Engineering workspace (`alfred-evolve`)**: routed per qualification (`--mode instance` for `instance/` without PR, `--mode local-fork` on a local branch without PR, or `--mode upstream-pr` to `alfred-core` with `docs/SPEC.md` synced via `alfred sync-spec`, `--hybrid --core-complete`, `--breaking`). All `core/` engineering runs inside an isolated `git worktree` (`worktrees/<proposal_id>`) through separated-context subagents enforcing the **3 mandatory quality pillars** (3 test tiers: **unit**, **functional**, and **e2e**; explicit **regression** analysis and tests; **adversarial testing** coordinated via **`swarm`**):
     1. **`tdd-red`**: writes only failing tests under `core/tests/` covering unit, functional, e2e, non-regression, and `swarm` adversarial checks (zero write access to `core/alfred/`).
     2. **`tdd-green`**: implements only production code (strictly forbidden from editing, weakening, or skipping `core/tests/`).
     3. **`doc-maintainer`**: keeps `README.md` (`<= 195` lines), `docs/SPEC.md` (`sync-spec`), manifests, and templates 100% English while preserving bilingual FR/EN runtime guidance (strictly forbidden from editing `core/tests/`, `core/alfred/`, or `core/scripts/`).
     4. **`tdd-verifier`**: independent `Read-Only` auditor validating all **6 deterministic guardians** (`check_code_architecture.py`, `check-upstream-leak.sh`, `check_english_only.py`, `ruff check`, `mypy --strict`, `pytest --cov=core --cov-fail-under=90`), `sync-spec --check`, and `swarm` adversarial coverage before opening the `alfred-core` Mono-Repo Pull Request.

---

## 3. Language Governance: 100% English Repo + Bilingual FR/EN Runtime Invariant

1. **Repository English-Only Policy (`check_english_only.py`)**: All source code docstrings, `# comments`, CLI messages, errors, `README.md`, `docs/SPEC.md`, `AGENTS.md`, `.agents/agents/*.md`, `.github/`, and `core/templates/**/*.md` MUST be written exclusively in English.
2. **Bilingual FR/EN Runtime Invariant**: Alfred operates natively in both **French and English** at runtime—responding to users in their active language, drafting Gmail replies in the incoming email's language (FR or EN), and supporting bilingual bookings, prompt-injection firewalling, email/Drive sanitization, and 3-tier knowledge search (`BILINGUAL_RUNTIME_STRING_ALLOWLIST`).

---

## 4. 3-Tier Progressive Knowledge Architecture

1. **Tier 1 — YAML Frontmatters (`instance/data/knowledge/*.md`)**: ~50-token metadata scan (`alfred scan-knowledge`) over `id`, `title`, `domain`, `summary`, `keywords`, `drive_source_ids`, `updated_at`.
2. **Tier 2 — Up-to-Date Reference Sheets (`<= 150L`) & `drive_metadata_index.json`**: targeted loading (`alfred load-knowledge`, `alfred search-drive`) with automatic one-way backup to `_ALFRED_REFERENTIEL_` on Google Drive.
3. **Tier 3 — Raw Google Drive Documents (`alfred read-drive`)**: native 0-OCR reading only when specific supporting contract or invoice details are required.

---

## 5. Defense-in-Depth Security & Identity Guardrails

1. **Deterministic `PreToolUse` Hook (`.agents/hooks.json` $\rightarrow$ `security_pre_tool_hook.py`)**: blocks direct agent access to `signature_vault/`, `mcp_oauth.json`, `token.json`, `client_secret*.json`, and protected Drive IDs.
2. **100% Out-of-Band Human Handwritten Signature**: `prepare-signature` prepares a preview PDF for human signing; Alfred **never** applies handwritten signatures autonomously.
3. **Inbox Firewall & SPF/DKIM**: bilingual FR/EN prompt-injection detection and strict ban on triggering `alfred-evolve` from any email lacking owner SPF/DKIM authentication.
4. **`sendAs` Identity vs Calendar Self-Invite Exclusion**: `identity.default_send_as_email` signs Gmail drafts/sends, while owner personal addresses and `sendAs` aliases (`never_invite_emails`) are systematically stripped from Google Calendar `attendees`.
