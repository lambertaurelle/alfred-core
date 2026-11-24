# ✅ Workspace `alfred-tasks` — Google Tasks, Deadlines & Google Contacts Directory

> **Model Routing (Muscle)**: `gemini-4-hydrogen-medium` (User-Initiated or Delegated via `alfred delegate --to tasks`)  
> **Alfred Root & CLI**: `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Mandatory Pipeline**: **1. `Read-Only` Inspection $\rightarrow$ 2. `implementation_plan.md` Gate $\rightarrow$ 3. Lightweight `Hydrogen` Executor (`plan-executor`)**

Google Tasks management with RFC 3339 due dates, contextual notes, and Google People Contacts directory lookup.

## Bilingual FR/EN Runtime
- Understand, create, and update tasks, notes, and contacts in both French and English, matching the user's active language or source email thread.

## 1. Stage 1 — Tasks, Deadlines & Contacts Review (`Read-Only`)
- Inspect tasklists and active tasks via `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred list-tasklists` and `list-tasks [--tasklist "<id>"]`.
- Resolve exact correspondent details via `search-contacts --query "<name_or_email>"` and `get-contact --resource-name "<id>"` (Google People API + `otherContacts`), cross-referencing `research-drive` or `research-gmail-cal` when needed.

## 2. Stage 2 — Synthesis & Systematic Gate (`implementation_plan.md`)
- `plan-synthesizer` structures creations, updates, deletions, RFC 3339 due dates, contextual notes (Gmail `threadId` or Drive `file_id` pointers), contact creations, and completions inside **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
- **Mandatory blocking gate**: zero task or contact creation, modification, or completion runs before user **`Proceed`**.

## 3. Stage 3 — Post-Validation Execution (`plan-executor` — Lightweight `Hydrogen`)
- After `Proceed`, the single lightweight `Hydrogen` agent (`plan-executor`) runs the approved commands:
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred create-task --title "<title>" [--tasklist "<id>"] [--notes "<notes>"] [--due "<RFC3339>"]`
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred complete-task --task-id "<id>" [--tasklist "<id>"]`
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred update-task --task-id "<id>" [--title "<title>"] [--notes "<notes>"] [--due "<RFC3339>"] [--tasklist "<id>"]`
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred delete-task --task-id "<id>" [--tasklist "<id>"]`
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred create-contact --given-name "<given>" [--family-name "<family>"] [--email "<email>"]`
- Any mutation run from another workspace automatically surfaces a visible `EXECUTED` trace in `alfred-tasks` (deduplicated when run inside `alfred-tasks`, unless `--archive-trace` is passed; suppressible via `--no-archive-trace`).
