# 📬 Workspace `alfred-inbox` — Secure 3-Output New Email Triage

> **Model Routing (Muscle)**: `gemini-pro-orchestrator-neon` (`Orchestrator (Neon)`, Agent-Spawned via `alfred poll-inbox`, silent traces archived in `gemini-pro-orchestrator-neon`)  
> **Alfred Root & CLI**: `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Mandatory Pipeline**: **0-Token Pre-Filter & Injection/SPF-DKIM Firewall $\rightarrow$ 3 Exclusive Outputs $\rightarrow$ `implementation_plan.md` Gate $\rightarrow$ `plan-executor` (`Orchestrator (Neon)`)**

Secure 2-speed email triage combining zero-token pre-filtering, SPF/DKIM prompt-injection firewalling, and three exclusive outputs.

## Bilingual FR/EN Runtime
- Process incoming French and English emails natively (action-required signals, dated booking confirmations, calendar RSVPs, prompt-injection firewalling, and knowledge lookup), and always draft `DRAFT_REPLY` bodies and `implementation_plan.md` responses in the active language matching the incoming email or user preference (French or English).

## 1. Deterministic Pre-Filtering & Security Firewall
- 0-token pre-filtering of automated `.ics` invites, promotions/forums, and denylisted senders prior to any cognitive call — while admitting priority allowlisted senders/domains (`allowlist_senders`, `allowlist_domains`), allowlisted automated correspondents (`allowlist_automated_senders`, `allowlist_automated_domains` when attachments are present), and **dated ticket, booking, hotel, transport, or appointment confirmations** (`has_dated_booking_signal`) for gated evaluation (across all future horizons).
- Anti-prompt-injection firewall across `subject`/`body`/`HTML` and extracted attachment text/HTML (`EmailSanitizer`), plus `Authentication-Results` (SPF/DKIM) verification forbidding any unauthenticated email from triggering `alfred-evolve`.
- In-memory Gmail attachment inspection (`list-attachments`, `read-attachment`) with layout-preserving PDF text extraction, path-traversal sanitization, and ZIP/XML hardening.

## 2. The 3 Exclusive Outputs (`InboxTriageService`)
1. **`SILENT_NO_ACTION`**:
   - Residual newsletters and benign passive notifications $\rightarrow$ **zero Gmail mutation** (optional auto-archived trace per `silent_creates_agy_session`).
   - **Calendar Coordination (`CONFIRM_EVENT`)**: when an email confirms a scheduled slot or dated ticket/booking requiring no written reply, open a Gate in `alfred-calendar` (`confirm-event --location ... --description ... --no-send-invites --archive-trace` with `attendees: []`, `send_invites: false` for merchants).
2. **`DRAFT_REPLY` (Gmail Draft + Optional `[HOLD]`)**:
   - Create the Gmail draft (with configured `default_send_as_email` `From:` header), keep the session active, and present full details in **`implementation_plan.md`** (`RequestFeedback: true`).
   - After `Proceed`, **`plan-executor`** (`Orchestrator (Neon)`) sends the draft (`send-draft`, archiving the thread by default unless `--keep-in-inbox`) and applies any paired calendar `[HOLD]` (`CREATE_HOLD`, automatically surfaced in `alfred-calendar`).
3. **`USER_ACTION_REQUIRED` (User Action + Optional `study-orchestrator` Escalation)**:
   - Auto-apply Gmail **`STARRED`** label, assemble full groundwork (`research-drive`, `research-gmail-cal`, `research-web`, or `study-orchestrator` when `needs_orchestrator_study=true`) including attachment inspection and optional Calendar coordination inside **`implementation_plan.md`**.
   - After `Proceed`, **`plan-executor`** files approved attachments (`save-attachment-to-drive --message-id <id> --parent-id <folder_id>`, or `alfred delegate --to drive --mode execute-approved` when `features.drive_watcher: true`), runs Tasks/Contacts (`create-task`, `complete-task`, `update-task`, `delete-task`, `create-contact`) or Calendar (`confirm-event` / `create-event`) actions with automatic cross-workspace surfacing, updates `.md` knowledge sheets, and archives the thread (`archive-email --thread-id ... --unstar`, unless `--keep-in-inbox`).
