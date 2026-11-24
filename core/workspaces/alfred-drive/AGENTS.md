# 🗂️ Workspace `alfred-drive` — Non-Destructive Google Drive Governance & 2-Speed Watcher

> **Model Routing (Muscle)**: `gemini-4-hydrogen-medium` (`watch-drive` or delegated via `alfred delegate --to drive`, `[SILENT]` traces in `gemini-4-hydrogen-low`)  
> **Alfred Root & CLI**: `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Mandatory Pipeline**: **1. 0-Token Filter (Speed 1) $\rightarrow$ 2. `drive-custodian` / `implementation_plan.md` Gate (Speed 2) $\rightarrow$ 3. Non-Destructive Execution (`plan-executor`)**

Non-destructive Google Drive document governance, automatic or gated filing, cross-domain shortcuts, and concise synthesis into `_ALFRED_REFERENTIEL_`.

## Bilingual FR/EN Runtime
- Read, sanitize, classify, and summarize French and English Drive files natively, keeping `<= 150`-line knowledge sheets aligned with the document's or user's active language.

## 1. Two-Speed Incremental Watcher (`DriveWatcherService`, opt-in `features.drive_watcher`)
1. **Speed 1 — Deterministic 0-Token Pre-Filter (`collect_delta_report`)**:
   - Skips folders, shortcuts, trashed items, internal writes under `_ALFRED_REFERENTIEL_` (`SKIP_REFERENTIEL_LOOP`), protected IDs (`protected_signature_drive_ids`), unmounted external `sharedWithMe` items, excluded media/backups (`image/`, `/Backups`, etc.), and already-processed fingerprints (`processed_drive_events.json`) at zero token cost.
2. **Speed 2 — Coalesced Batch Cognitive Governance (`drive-custodian`)**:
   - Native 0-OCR extraction (`read-drive`), anti-prompt-injection sanitization, and 128-bit nonce XML spotlighting (`DriveSecuritySanitizer`).
   - **3 exclusive outputs**:
     - **`SILENT_NO_ACTION`**: file already organized and synthesized $\rightarrow$ mandatory auto-archived `[SILENT]` conversation.
     - **`SILENT_AUTO_FILED`**: deterministic filing per `00_drive_taxonomy_and_filing_rules.md` (`move-drive`, `rename-drive`, `create-drive-folder`, `create-drive-shortcut`, `upsert-knowledge` `<= 150` lines backed up to `_ALFRED_REFERENTIEL_`) $\rightarrow$ mandatory auto-archived `[SILENT]` conversation.
     - **`GATE_REQUIRED`**: taxonomy ambiguity, `> 150`-line overflow, protected ID, or injection signal $\rightarrow$ **zero mutation executed**, opens an active conversation with **`implementation_plan.md`** (`RequestFeedback: true`).

## 2. Security Invariants & Zero Deletion
- **Zero destructive methods**: Alfred never deletes or trashes any Google Drive file or folder.
- **`PreToolUse` Sandbox**: invoking `prepare-signature`, `apply-signature`, `evolve`, `send-draft`, `delete-event`, or shell chaining from `alfred-drive` is strictly forbidden.
