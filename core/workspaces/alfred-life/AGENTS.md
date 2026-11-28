# 🎩 Workspace `alfred-life` — Family Office Chief of Staff Hub (User-Initiated & MCP Bridge)

> **Model Routing (Brain)**: `gemini-pro-orchestrator` (User-Initiated or `alfred_life_brain_dump` via Gemini MCP)  
> **Alfred Root & CLI**: `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Mandatory Pipeline**: **1. Adaptive `Read-Only` Research $\rightarrow$ 2. `implementation_plan.md` Gate $\rightarrow$ 3. `Orchestrator (Neon)` Executor (`plan-executor`)**

General Family Office Chief of Staff hub for cross-domain decisions, multi-source studies, and the Gemini App MCP bridge.

## Bilingual FR/EN Runtime
- Understand requests, search Gmail/Drive/knowledge bases, draft replies, and present `implementation_plan.md` natively in both French and English, matching the user's or correspondent's active language.

## 1. Stage 1 — Adaptive Turbo Analysis & Research (`Read-Only`)
Scale investigation depth immediately to match the request:
- **0 subagents** when instructions are self-contained $\rightarrow$ move straight to Stage 2.
- **1 targeted subagent (`research-drive` or `research-gmail-cal`)** to scan YAML frontmatters (`instance/data/knowledge/*.md`), load an up-to-date reference sheet (`<= 150L`), or inspect calendars, tasks, and contacts.
- **Parallel "Spread" research (`research-drive` + `research-gmail-cal` + `research-web`)** or deep analysis via **`study-orchestrator`** when a case spans estate/patrimony, taxes, Google Drive records, Gmail history, and official regulations.

## 2. Stage 2 — Synthesis & Systematic Gate (`implementation_plan.md`)
- `plan-synthesizer` generates **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
- Any Gmail, Google Calendar, Google Tasks, Contacts, cross-workspace delegation (`alfred delegate --to <role>`), signature preparation (`prepare-signature` — out-of-band human signing only, zero autonomous signing), or knowledge base write is blocked until user **`Proceed`**.

## 3. Stage 3 — Post-Validation Execution (`plan-executor` — `Orchestrator (Neon)`)
- A single lightweight `Orchestrator (Neon)` agent (`plan-executor`, `gemini-pro-orchestrator-neon`) runs approved CLI commands and backs up updated `.md` knowledge sheets to Google Drive (`_ALFRED_REFERENTIEL_`) — or delegates Drive writes to `alfred-drive` (`alfred delegate --to drive --mode execute-approved`) when `features.drive_watcher: true` is enabled.
- **Automatic Cross-Workspace Surfacing**: any Calendar mutation (`create-event`, `update-event`, `confirm-event`, `delete-event`, `hold-allowlist --add/--remove`) or Tasks/Contacts/Suite mutation (`create-task`, `complete-task`, `update-task`, `delete-task`, `create-contact`, `create-doc`, `append-sheet`) invoked from `alfred-life` automatically surfaces a visible `EXECUTED` trace in `alfred-calendar` or `alfred-tasks` (suppressible via `--no-archive-trace`).
- **Default Inbox-Zero Policy**: every interactively handled email thread is archived in Gmail upon completion (`send-draft` or `archive-email --thread-id ...`), unless `--keep-in-inbox` is explicitly specified.
