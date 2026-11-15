# 🚀 Workspace `alfred-onboard` — Assistant Interactif d'Onboarding & Configuration Modulaire

> **Routage Modèle (Brain)** : `gemini-pro-orchestrator` (User-Initiated)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **1. Diagnostic `Read-Only` $\rightarrow$ 2. Gate `implementation_plan.md` $\rightarrow$ 3. Exécuteur Léger `Hydrogen` (`plan-executor`)**

## 1. Étape 1 — Diagnostic d'Installation & Cartographie Modulaire (`Read-Only`)
- Inspecte l'état courant via `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred status-onboarding --json` et `oauth-guide`.
- Identifie avec l'utilisateur les briques fonctionnelles souhaitées dans `instance/config/settings.yaml` (`drive_knowledge_sync`, `inbox_auto_triage`, `gmail_drafts_and_send`, `calendar_management`, `tasks_management`, `contacts_lookup`, `google_docs_suite`, `signature_delegation`, `mcp_gemini_bridge`), ses alias `identity.default_send_as_email` / `never_invite_emails` (ex. `contact@domaine-perso.example`) et les APIs GCP + scopes OAuth2 strictement nécessaires.
- Une fois le jeton OAuth2 initialisé (`auth-google`), explore l'arborescence Google Drive en lecture seule via `discover-drive --json` et `init-drive`.

## 2. Étape 2 — Synthèse & Gate Systématique (`implementation_plan.md`)
- `plan-synthesizer` rédige **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`) récapitulant :
  - les flags activés dans `instance/config/settings.yaml`,
  - la taxonomie Google Drive proposée (`00_drive_taxonomy_and_filing_rules.md`),
  - le provisionnement des 6 projets Antigravity (`init-workspaces --prune-legacy`), des routines `systemd --user` et, si activé, du pont MCP (`setup-mcp`).
- **Gate bloquante obligatoire** : aucune écriture finale avant clic utilisateur sur **`Proceed`**.

## 3. Étape 3 — Exécution Post-Validation (`plan-executor` — Modèle Léger `Hydrogen`)
- L'agent léger `plan-executor` applique le plan validé (`init-workspaces --prune-legacy`, `init-drive --execute`, `setup-mcp`, `install-routines`) sans jamais exposer de secret en clair.
