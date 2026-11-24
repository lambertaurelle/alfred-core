# Alfred V8.0 — Spécification Technique Vivante (`docs/SPEC.md`)

Ce document constitue l'unique spécification technique de référence du mono-repo **`alfred-core`** (V8.0). Les sections narratives décrivent les invariants d'architecture, de sécurité et de gouvernance ; l'inventaire technique ci-dessous est généré de façon déterministe par analyse AST (`alfred sync-spec`).

---

## 1. Principes & Séparation Mono-Repo (`core/` vs `instance/`)

- **`core/` (générique open-source)** : code applicatif (`core/alfred/`), scripts gardiens (`core/scripts/`), templates (`core/templates/`), workspaces (`core/workspaces/`), suite TDD (`core/tests/`) et exemples fictifs (`instance/examples/`).
- **`instance/` (privé gitignoré)** : `instance/config/settings.yaml`, `instance/secrets/` (jetons OAuth2, `signature_vault/`), `instance/data/` (fiches de connaissances YAML 3 étages, index Drive, journaux d'état) et `instance/extensions/*.py` actifs.
- **Découplage strict** : aucun module sous `core/` ne peut importer `instance` (`check_code_architecture.py` : règle `Instance Coupling`). Toute capacité hybride destinée à `upstream-pr` doit fonctionner de manière 100 % autonome dans `core/` sans dépendre d'`instance/`.

---

## 2. Clean Architecture OOP à 4 Couches & Gardiens

```text
domain  <--  repositories  <--  services  <--  controllers  <--  main.py
```

1. **Invariants AST (`check_code_architecture.py`)** : `main.py <= 79L`, tout autre fichier `<= 500L`, toute méthode `<= 60L` hors docstring, docstrings Google sur 100 % des modules/classes/fonctions (`Args:` obligatoire sur toute fonction publique paramétrée hors `@property`), respect strict des dépendances entre couches et interdiction d'`import instance` sous `core/`.
2. **Zéro Fuite Amont (`check-upstream-leak.sh`)** : contrôle du working tree, de l'index et de l'historique actif `HEAD` (`git log HEAD`) afin d'éviter toute fuite sur les branches locales ou fork.
3. **Qualité & Couverture** : `ruff check core`, `mypy --strict core/alfred core/scripts core/tests`, `pytest --cov=core --cov-fail-under=90` et fraîcheur de `docs/SPEC.md` vérifiée en pre-commit (`alfred sync-spec --check`).

---

## 3. Sécurité, Identité & Onboarding GCP

- **Hook `PreToolUse` (`.agents/hooks.json` -> `security_pre_tool_hook.py`)** : interdit la lecture/écriture directe de `signature_vault/`, `client_secret*.json`, `token.json`, `mcp_oauth.json`, `oauth_pending.json` et `leak_terms.txt` ; impose une confirmation explicite (`force_ask`) sur `apply-signature` et bloque toute exécution hors-périmètre dans `alfred-inbox`.
- **Signature manuscrite hors-bande** : `prepare-signature` génère uniquement un aperçu sans tampon ; aucune signature n'est apposée sans consentement humain explicite récent.
- **Pare-feu Inbox & Identité `sendAs`** : assainissement HTML/Unicode (`EmailSanitizer`), encapsulation par nonce aléatoire, vérification propriétaire SPF/DKIM pour `alfred-evolve` et exclusion automatique des adresses personnelles (`never_invite_emails`) hors des invitations Calendar.
- **Bootstrap GCP & Pont MCP OAuth 2.1** : `alfred bootstrap-gcp` automatise l'installation multi-architecture de `gcloud`, la connexion `--no-launch-browser`, la création du projet, l'activation minimale des APIs selon `features.*` et l'ingestion `0600` du client OAuth Desktop. Le serveur MCP expose `/.well-known/*`, `/oauth/{register,authorize,token}` (PKCE `S256`) et `/mcp`.

---

## 4. Gouvernance `alfred-evolve` (`instance` | `upstream-pr` | `local-fork`)

- **Qualification (`evolve-qualifier`)** : classe chaque besoin en `PURE_INSTANCE`, `INSTANCE_WITH_COMMUNITY_OPTION`, `HYBRID_CORE_AND_INSTANCE` ou `CORE_EVOLUTION`.
- **Modes d'exécution** :
  - `instance` : écritures cantonnées à `instance/`, zéro PR, `docs/SPEC.md` inchangé.
  - `upstream-pr` : exécution en `git worktree` isolé via la triade `tdd-red` -> `tdd-green` -> `tdd-verifier`, synchronisation automatique de `docs/SPEC.md`, vérification d'autonomie (`--hybrid --core-complete`), marquage automatique `[BREAKING]` en cas de suppression de module/commande/flag et publication PR sur `origin` avec fallback fork GitHub.
  - `local-fork` : commit local sur `evolve/<id>` sans ouverture de PR amont.

---

<!-- BEGIN ALFRED_CORE_GENERATED_SPEC -->
## Inventaire Généré (`alfred-core` v8.0.0)

### Workspaces Spécialisés (`core/workspaces/`)
| Workspace | Description |
| :--- | :--- |
| `alfred-calendar` | - Inspecte les agendas et créneaux occupés sans limite d'horizon via `list-calendars`, `query-freebusy --from "<ISO>" --to "<ISO>"`, `list-events` et `hold-allowlist --list` (couvrant événements futurs lointains et séries récurrentes `RRULE`). |
| `alfred-drive` | Gouvernance documentaire non-destructive de Google Drive, classement automatique ou sous Gate, raccourcis multi-domaines et synthèse concise vers `_ALFRED_REFERENTIEL_`. |
| `alfred-evolve` | - **`evolve-qualifier`** inspecte `docs/SPEC.md`, `core/` et `instance/` pour trancher entre les 3 modes : |
| `alfred-inbox` | - Pré-filtrage à 0 token des invitations `.ics` automatiques, promotions/forums et expéditeurs denylistés avant tout appel cognitif — tout en laissant passer les **confirmations datées de billets, réservations, hôtels, transports ou rendez-vous** (`has_dated_booking_signal`) pour inscription sous Gate à l'agenda (sans limite d'horizon). |
| `alfred-life` | Adapte immédiatement l'ampleur de l'investigation à la demande : |
| `alfred-onboard` | - Inspecte l'état courant via `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred status-onboarding --json` (incluant `gcloud_installed`, `gcloud_account`, `gcp_project_id`) et `oauth-guide`. |
| `alfred-tasks` | - Consulte les listes et tâches actives via `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred list-tasklists` et `list-tasks [--tasklist "<id>"]`. |

### Feature Flags, APIs GCP & Scopes OAuth2
| Feature Flag | Défaut | APIs GCP | Scopes OAuth2 |
| :--- | :---: | :--- | :--- |
| `drive_knowledge_sync` | `true` | `drive.googleapis.com`, `sheets.googleapis.com` | `drive` |
| `inbox_auto_triage` | `true` | `gmail.googleapis.com` | `gmail.modify`, `gmail.compose`, `gmail.settings.basic` |
| `gmail_drafts_and_send` | `true` | `gmail.googleapis.com` | `gmail.modify`, `gmail.compose`, `gmail.settings.basic` |
| `calendar_management` | `true` | `calendar-json.googleapis.com` | `calendar` |
| `tasks_management` | `true` | `tasks.googleapis.com` | `tasks` |
| `contacts_lookup` | `true` | `people.googleapis.com` | `contacts`, `contacts.other.readonly` |
| `google_docs_suite` | `false` | `docs.googleapis.com`, `sheets.googleapis.com`, `slides.googleapis.com`, `forms.googleapis.com` | `documents`, `spreadsheets`, `presentations`, `forms.body`, `forms.responses.readonly` |
| `signature_delegation` | `false` | — | — |
| `mcp_gemini_bridge` | `false` | — | — |
| `drive_watcher` | `false` | `drive.googleapis.com`, `sheets.googleapis.com` | `drive` |

### Modules Clean Architecture (`core/alfred/`)
| Couche | Module | Rôle |
| :--- | :--- | :--- |
| `domain` | `core/alfred/domain/attachment_models.py` | Pure domain models for Gmail MIME attachments and Gmail-to-Drive transfers. |
| `domain` | `core/alfred/domain/calendar_models.py` | Domain models for generic multi-calendar HOLDs, FreeBusy windows, invitations, and PRs. |
| `domain` | `core/alfred/domain/drive_watcher_models.py` | Domain models and configuration schemas for Google Drive governance and 2-speed watcher. |
| `domain` | `core/alfred/domain/evolve_models.py` | Domain schemas for V8 evolution qualification, living SPEC snapshots, and breaking reports. |
| `domain` | `core/alfred/domain/extension_protocol.py` | Standardized protocol for user-specific instance/extensions plugins enforced by alfred-evolve. |
| `domain` | `core/alfred/domain/feature_models.py` | Modular feature flags, OAuth2 scope mapping, identity aliases, and model routing schemas. |
| `domain` | `core/alfred/domain/models.py` | Pure domain models and configuration schemas for Alfred V8.0 Chief of Staff. |
| `domain` | `core/alfred/domain/onboarding_models.py` | Domain schemas for Google Cloud SDK inspection and automated GCP OAuth2 bootstrap. |
| `domain` | `core/alfred/domain/routine_models.py` | Domain models for Alfred periodic routines, systemd timers, and workspace status. |
| `domain` | `core/alfred/domain/security_models.py` | Domain models for signature human consent, email sanitization, and specialist delegation. |
| `domain` | `core/alfred/domain/tasks_models.py` | Domain models for Google Tasks (alfred-tasks) and Google People Contacts. |
| `repositories` | `core/alfred/repositories/json_repository.py` | Atomic POSIX JSON and JSONL persistence repositories using .lock + os.replace. |
| `repositories` | `core/alfred/repositories/state_store.py` | Typed facade over all local atomic state and cache repositories. |
| `services` | `core/alfred/services/agy_archive_store.py` | Native Antigravity .pbtxt annotation writer, SQLite summary sync, and archive store. |
| `services` | `core/alfred/services/agy_daemon_sync.py` | Idle-safe Antigravity remote-control daemon reconciler for CLI-spawned conversations. |
| `services` | `core/alfred/services/agy_session_manager.py` | Antigravity Web UI session manager, model-tiered agy CLI bridge, and title sync. |
| `services` | `core/alfred/services/agy_triage_parser.py` | Pure cognitive triage JSON schema, nonce-spotlighted prompt builder, and CLI JSON parsers. |
| `services` | `core/alfred/services/attachment_extractor.py` | In-memory Gmail MIME traversal, filename sanitization, and secure attachment text extraction. |
| `services` | `core/alfred/services/calendar_helpers.py` | Pure helper functions for Google Calendar payload building, [HOLD] parsing, and matching. |
| `services` | `core/alfred/services/calendar_service.py` | Google Calendar API v3 generic multi-calendar service, [HOLD] lifecycle, and FreeBusy. |
| `services` | `core/alfred/services/contacts_service.py` | Google People API v1 connector for Family Office contact lookup and post-Gate creation. |
| `services` | `core/alfred/services/core_spec_ast.py` | Pure-AST extraction helpers for living specification generation and leak checks. |
| `services` | `core/alfred/services/core_spec_service.py` | AST-driven living specification generator and zero-leak validator for docs/SPEC.md. |
| `services` | `core/alfred/services/delegation_service.py` | Cross-project specialist delegation service with model routing and inbox-to-evolve firewall. |
| `services` | `core/alfred/services/drive_governance_service.py` | Non-destructive Google Drive governance service with protected ID and anti-loop guards. |
| `services` | `core/alfred/services/drive_security_sanitizer.py` | Anti-prompt-injection sanitizer, nonce spotlighting, and silent op validator for Drive. |
| `services` | `core/alfred/services/drive_service.py` | Google Drive v3 connector, First-Run exhaustive scaffolder (init-drive), and 0-OCR reader. |
| `services` | `core/alfred/services/drive_speed1_filter.py` | Speed-1 zero-token Google Drive delta index updater and eligibility filter. |
| `services` | `core/alfred/services/drive_watcher_helpers.py` | Cognitive prompt/schema helpers and Markdown plan/trace renderers for DriveWatcherService. |
| `services` | `core/alfred/services/drive_watcher_service.py` | 2-speed Google Drive watcher service with 0-token pre-filters, coalescing, and anti-loop. |
| `services` | `core/alfred/services/email_security_service.py` | Four-layer anti-prompt-injection sanitizer, nonce spotlighting, and inbox firewall. |
| `services` | `core/alfred/services/evolve_gate_runner.py` | Deterministic leak, AST architecture, ruff, mypy, pytest, and SPEC gate runners. |
| `services` | `core/alfred/services/evolve_governance_service.py` | Governance, qualification, isolation, and breaking-change detection for alfred-evolve. |
| `services` | `core/alfred/services/evolve_worktree_service.py` | alfred-evolve isolated git worktree TDD runner, governance gate, and Mono-Repo PR builder. |
| `services` | `core/alfred/services/extension_loader.py` | Dynamic loader, validator, and Drive backup helper for instance/extensions plugins. |
| `services` | `core/alfred/services/gcp_bootstrap_service.py` | Automated Google Cloud SDK bootstrap, project/API enablement, and OAuth2 onboarding. |
| `services` | `core/alfred/services/gcp_sdk_helpers.py` | Pure validation and archive extraction helpers for Google Cloud SDK bootstrap. |
| `services` | `core/alfred/services/gh_pr_publisher.py` | GitHub Pull Request publisher with automatic fork fallback on origin push 403. |
| `services` | `core/alfred/services/gmail_drive_bridge_service.py` | Service bridge connecting Gmail MIME attachments, EmailSanitizer, and Google Drive. |
| `services` | `core/alfred/services/gmail_service.py` | Gmail API v1 connector for incremental history polling, drafts, and STARRED labels. |
| `services` | `core/alfred/services/google_auth.py` | Google Workspace OAuth2 credential loader, auto-refresher, and interactive bootstrap. |
| `services` | `core/alfred/services/google_workspace_suite.py` | Opt-in Google Workspace Suite connectors (Docs v1, Sheets v4, Slides v1, Forms v1). |
| `services` | `core/alfred/services/inbox_booking_detector.py` | Generic FR/EN dated booking confirmation and automated correspondent detectors. |
| `services` | `core/alfred/services/inbox_calendar_bridge.py` | Bridge between alfred-inbox email triage, 2-speed Gate plans, and alfred-calendar. |
| `services` | `core/alfred/services/inbox_eligibility.py` | Deterministic 0-token pre-filtering and thread coalescence for alfred-inbox. |
| `services` | `core/alfred/services/inbox_triage_service.py` | alfred-inbox 2-speed routine with 0-token pre-filters, sanitizer, and calendar bridge. |
| `services` | `core/alfred/services/knowledge_service.py` | 3-Tier Progressive Knowledge loader (YAML Frontmatter -> Reference .md -> Drive backup). |
| `services` | `core/alfred/services/mcp_bridge_service.py` | OAuth 2.1 PKCE + Streamable HTTP /mcp bridge service for Gemini App integration. |
| `services` | `core/alfred/services/mcp_oauth_helper.py` | OAuth 2.1 PKCE S256, PBKDF2 PIN storage, and tool schemas for McpBridgeService. |
| `services` | `core/alfred/services/onboarding_service.py` | Family Office onboarding inspection, 1-click GCP OAuth guide, and non-destructive settings. |
| `services` | `core/alfred/services/prompt_loader.py` | Markdown prompt template loader with instance override and safe placeholder rendering. |
| `services` | `core/alfred/services/routine_service.py` | Unified V8 periodic routine runner and systemd --user timer installer (0-token idle). |
| `services` | `core/alfred/services/signature_vault_service.py` | Handwritten signature vault with SHA-256 pinning, one-shot tickets, and human consent. |
| `services` | `core/alfred/services/specialist_trace_service.py` | Cross-workspace specialist trace surfacing, sandbox guard, and deduplication. |
| `services` | `core/alfred/services/systemd_unit_renderer.py` | Render systemd --user service/timer units and compute stale unit pruning targets. |
| `services` | `core/alfred/services/tasks_service.py` | Google Tasks v1 connector supporting tasklists, subtasks, due dates, and feature flags. |
| `services` | `core/alfred/services/workspace_service.py` | Provision and verify the 7 distinct Antigravity specialist projects for Alfred V8.0. |
| `controllers` | `core/alfred/controllers/app_factory.py` | Dependency injection factory wiring Alfred V8.0 repositories, services, and CLI controller. |
| `controllers` | `core/alfred/controllers/attachment_cli.py` | CLI subcommand registration and handlers for Gmail MIME attachments and Drive transfer. |
| `controllers` | `core/alfred/controllers/calendar_cli.py` | CLI subcommand registration and handlers for alfred-calendar [HOLD]s, FreeBusy, and events. |
| `controllers` | `core/alfred/controllers/cli_controller.py` | Command-line controller exposing Alfred V8.0 Family Office Chief of Staff workflows. |
| `controllers` | `core/alfred/controllers/drive_cli.py` | CLI subcommands for non-destructive Google Drive governance and 2-speed watcher. |
| `controllers` | `core/alfred/controllers/evolve_cli.py` | CLI subcommand registration and handlers for alfred evolve and sync-spec. |
| `controllers` | `core/alfred/controllers/gcp_cli.py` | CLI subcommand registration and handler for automated Google Cloud SDK & OAuth bootstrap. |
| `controllers` | `core/alfred/controllers/onboarding_cli.py` | CLI subcommands for Family Office onboarding, delegation, signature vault, and MCP bridge. |
| `controllers` | `core/alfred/controllers/routine_cli.py` | CLI subcommand registration and handlers for periodic routines and Gmail actions. |
| `controllers` | `core/alfred/controllers/tasks_cli.py` | CLI subcommand registration and dispatcher for Google Tasks, Contacts, and Workspace Suite. |
| `main` | `core/alfred/main.py` | Alfred V8.0 Family Office CLI entrypoint and dependency injection bootstrap (< 80 lines). |

### Commandes CLI (`alfred <subcommand>`)
| Commande | Options | Source |
| :--- | :--- | :--- |
| `append-sheet` | `--spreadsheet-id`, `--range`, `--value`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/tasks_cli.py` |
| `apply-onboarding` | `--enable-all`, `--enable`, `--disable`, `--default-send-as`, `--send-as-name`, `--calendar-organizer`, `--never-invite`, `--mcp-host`, `--mcp-port`, `--mcp-public-url` | `core/alfred/controllers/onboarding_cli.py` |
| `apply-signature` | `--request-id`, `--workspace`, `--transcript` | `core/alfred/controllers/onboarding_cli.py` |
| `archive-email` | `--thread-id`, `--message-id`, `--unstar`, `--keep-unread` | `core/alfred/controllers/routine_cli.py` |
| `auth-google` | `--port`, `--no-browser`, `--print-url`, `--redirect-url` | `core/alfred/controllers/cli_controller.py` |
| `backfill-inbox-archives` | — | `core/alfred/controllers/routine_cli.py` |
| `bootstrap-gcp` | `--project-id`, `--project-name`, `--non-interactive`, `--no-install-gcloud`, `--skip-oauth-token`, `--finish-setup`, `--open-browser` | `core/alfred/controllers/gcp_cli.py` |
| `complete-task` | `--task-id`, `--tasklist`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/tasks_cli.py` |
| `confirm-event` | `--calendar`, `--summary`, `--from`, `--to`, `--attendee`, `--send-invites`, `--no-send-invites`, `--hold-event-id`, `--thread-id`, `--no-release-holds`, `--location`, `--description`, `--archive-trace`, `--no-archive-trace`, `--keep-in-inbox` | `core/alfred/controllers/calendar_cli.py` |
| `create-contact` | `--given-name`, `--family-name`, `--email`, `--phone`, `--organization`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/tasks_cli.py` |
| `create-doc` | `--title`, `--text`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/tasks_cli.py` |
| `create-drive-folder` | `--name`, `--parent-id` | `core/alfred/controllers/drive_cli.py` |
| `create-drive-shortcut` | `--target-id`, `--parent-id`, `--name` | `core/alfred/controllers/drive_cli.py` |
| `create-event` | `--calendar`, `--summary`, `--from`, `--to`, `--hold`, `--attendee`, `--rrule`, `--no-send-invites`, `--location`, `--description`, `--thread-id`, `--message-id`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/calendar_cli.py` |
| `create-task` | `--title`, `--notes`, `--due`, `--tasklist`, `--parent`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/tasks_cli.py` |
| `delegate` | `--to`, `--title`, `--brief`, `--mode`, `--parent-session`, `--source-workspace`, `--source-message-id` | `core/alfred/controllers/onboarding_cli.py` |
| `delete-event` | `--calendar`, `--event-id`, `--notify`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/calendar_cli.py` |
| `delete-task` | `--task-id`, `--tasklist`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/tasks_cli.py` |
| `discover-drive` | `--json` | `core/alfred/controllers/onboarding_cli.py` |
| `evolve` | `--id`, `--title`, `--description`, `--execute`, `--spawn-agy`, `--worktree`, `--mode`, `--breaking`, `--draft`, `--hybrid`, `--core-complete` | `core/alfred/controllers/evolve_cli.py` |
| `get-contact` | `--resource-name` | `core/alfred/controllers/tasks_cli.py` |
| `hold-allowlist` | `--list`, `--add`, `--remove`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/calendar_cli.py` |
| `init-drive` | `--execute` | `core/alfred/controllers/cli_controller.py` |
| `init-workspaces` | `--prune-legacy` | `core/alfred/controllers/cli_controller.py` |
| `install-routines` | `--systemd-dir`, `--no-enable` | `core/alfred/controllers/routine_cli.py` |
| `list-attachments` | `--message-id` | `core/alfred/controllers/attachment_cli.py` |
| `list-calendars` | `--refresh` | `core/alfred/controllers/calendar_cli.py` |
| `list-events` | `--calendar`, `--from`, `--to` | `core/alfred/controllers/calendar_cli.py` |
| `list-identities` | `--json` | `core/alfred/controllers/onboarding_cli.py` |
| `list-tasklists` | `--max-results` | `core/alfred/controllers/tasks_cli.py` |
| `list-tasks` | `--tasklist`, `--include-completed`, `--max-results` | `core/alfred/controllers/tasks_cli.py` |
| `load-knowledge` | `--doc` | `core/alfred/controllers/cli_controller.py` |
| `move-drive` | `--file-id`, `--target-parent-id`, `--parent-id` | `core/alfred/controllers/drive_cli.py` |
| `oauth-guide` | — | `core/alfred/controllers/onboarding_cli.py` |
| `poll-inbox` | `--spawn-agy`, `--cognitive-agy` | `core/alfred/controllers/cli_controller.py` |
| `prepare-signature` | `--pdf`, `--reason`, `--workspace`, `--page`, `--x`, `--y`, `--width`, `--height` | `core/alfred/controllers/onboarding_cli.py` |
| `query-freebusy` | `--from`, `--to`, `--calendar` | `core/alfred/controllers/calendar_cli.py` |
| `read-attachment` | `--message-id`, `--filename`, `--attachment`, `--index`, `--attachment-id`, `--max-chars` | `core/alfred/controllers/attachment_cli.py` |
| `read-drive` | `--file-id` | `core/alfred/controllers/cli_controller.py` |
| `read-sheet` | `--spreadsheet-id`, `--range` | `core/alfred/controllers/tasks_cli.py` |
| `read-thread` | `--thread-id` | `core/alfred/controllers/routine_cli.py` |
| `rename-drive` | `--file-id`, `--name` | `core/alfred/controllers/drive_cli.py` |
| `repair-session-titles` | — | `core/alfred/controllers/onboarding_cli.py` |
| `run-routines` | `--only`, `--today` | `core/alfred/controllers/routine_cli.py` |
| `save-attachment-to-drive` | `--message-id`, `--parent-id`, `--folder-id`, `--filename`, `--attachment`, `--index`, `--attachment-id`, `--name`, `--rename`, `--overwrite` | `core/alfred/controllers/attachment_cli.py` |
| `scan-knowledge` | `--query` | `core/alfred/controllers/cli_controller.py` |
| `search-contacts` | `--query`, `--page-size`, `--no-other-contacts` | `core/alfred/controllers/tasks_cli.py` |
| `search-drive` | `--query` | `core/alfred/controllers/cli_controller.py` |
| `search-gmail` | `--query`, `--max-results` | `core/alfred/controllers/routine_cli.py` |
| `send-draft` | `--draft-id`, `--thread-id`, `--to`, `--subject`, `--body`, `--keep-in-inbox`, `--unstar` | `core/alfred/controllers/cli_controller.py` |
| `serve-mcp` | `--host`, `--port` | `core/alfred/controllers/onboarding_cli.py` |
| `setup-mcp` | `--pin` | `core/alfred/controllers/onboarding_cli.py` |
| `status-onboarding` | `--json` | `core/alfred/controllers/onboarding_cli.py` |
| `status-routines` | `--systemd-dir` | `core/alfred/controllers/routine_cli.py` |
| `sync-agy-ui` | `--force`, `--wait-idle`, `--timeout`, `--poll-interval` | `core/alfred/controllers/routine_cli.py` |
| `sync-drive` | — | `core/alfred/controllers/cli_controller.py` |
| `sync-spec` | `--check`, `--repo-root` | `core/alfred/controllers/evolve_cli.py` |
| `update-event` | `--calendar`, `--event-id`, `--summary`, `--from`, `--to`, `--location`, `--description`, `--attendee`, `--rrule`, `--no-send-invites`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/calendar_cli.py` |
| `update-task` | `--task-id`, `--title`, `--notes`, `--due`, `--status`, `--tasklist`, `--archive-trace`, `--no-archive-trace` | `core/alfred/controllers/tasks_cli.py` |
| `upsert-knowledge` | `--file`, `--id`, `--title`, `--domain`, `--summary`, `--body-file`, `--keyword`, `--drive-source-id` | `core/alfred/controllers/cli_controller.py` |
| `watch-drive` | `--spawn-agy`, `--cognitive-agy` | `core/alfred/controllers/drive_cli.py` |
<!-- END ALFRED_CORE_GENERATED_SPEC -->
