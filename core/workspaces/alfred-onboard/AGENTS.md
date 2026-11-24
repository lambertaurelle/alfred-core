# 🚀 Workspace `alfred-onboard` — Interactive Onboarding & Modular Setup Assistant

> **Model Routing (Brain)**: `gemini-pro-orchestrator` (User-Initiated)  
> **Alfred Root & CLI**: `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Mandatory Pipeline**: **1. `Read-Only` Diagnostic $\rightarrow$ 2. `implementation_plan.md` Gate $\rightarrow$ 3. Lightweight `Hydrogen` Executor (`plan-executor`)**

Interactive onboarding, modular feature flags, automated GCP OAuth2 setup, Drive taxonomy scaffolding, and workspace provisioning.

## Bilingual FR/EN Runtime
- Converse with the user and present onboarding guidance, `implementation_plan.md` summaries, and Drive taxonomy notes in the user's active language (French or English).

## 1. Stage 1 — Installation Diagnostic & Modular Mapping (`Read-Only`)
- Inspect current installation state via `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred status-onboarding --json` (including `gcloud_installed`, `gcloud_account`, `gcp_project_id`) and `oauth-guide`.
- For end-to-end guided GCP onboarding (`gcloud` SDK installation, `--no-launch-browser` auth, project creation, API enablement per `features.*`, Desktop OAuth JSON ingestion, and `token.json` minting), propose `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred bootstrap-gcp` (or `./scripts/bootstrap.sh --onboard`).
- Confirm with the user which feature modules to enable in `instance/config/settings.yaml` (`drive_knowledge_sync`, `inbox_auto_triage`, `gmail_drafts_and_send`, `calendar_management`, `tasks_management`, `contacts_lookup`, `google_docs_suite`, `drive_watcher`, `signature_delegation`, `mcp_gemini_bridge`), owner `identity.default_send_as_email` / `never_invite_emails` aliases (e.g., `contact@my-domain.example`), and the minimal GCP APIs + OAuth2 scopes required.
- Once OAuth2 is active (`auth-google`), inspect Google Drive read-only via `discover-drive --json` and `init-drive`.

## 2. Stage 2 — Synthesis & Systematic Gate (`implementation_plan.md`)
- `plan-synthesizer` authors **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`) summarizing:
  - enabled feature flags in `instance/config/settings.yaml`,
  - proposed Google Drive taxonomy (`00_drive_taxonomy_and_filing_rules.md`),
  - provisioning of the 7 Antigravity projects (`init-workspaces --prune-legacy`), `systemd --user` timers, and (when enabled) the MCP bridge (`setup-mcp`).
- **Mandatory blocking gate**: zero final writes before user approval (`Proceed`).

## 3. Stage 3 — Post-Validation Execution (`plan-executor` — Lightweight `Hydrogen`)
- Single lightweight `plan-executor` applies the approved plan (`init-workspaces --prune-legacy`, `init-drive --execute`, `setup-mcp`, `install-routines`) without ever exposing plaintext secrets.
