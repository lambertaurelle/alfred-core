# 📅 Workspace `alfred-calendar` — Multi-Calendars, `[HOLD]` Lifecycle & Strict Self-Invite Exclusion

> **Model Routing (Muscle)**: `gemini-4-hydrogen-medium` (Agent-Spawned or Delegated via `alfred delegate --to calendar`)  
> **Alfred Root & CLI**: `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Mandatory Pipeline**: **1. `Read-Only` Inspection $\rightarrow$ 2. `implementation_plan.md` Gate $\rightarrow$ 3. Lightweight `Hydrogen` Executor (`plan-executor`)**

Generic multi-calendar availability, allowlisted `[HOLD]` scheduling, dated booking confirmations, and strict self-invite exclusion.

## Bilingual FR/EN Runtime
- Parse scheduling proposals and dated booking confirmations in both French and English, and present `implementation_plan.md` event summaries, locations, and invitations in the user's or email thread's language (French or English).

## 1. Stage 1 — Multi-Calendar Reading & Availability (`Read-Only`)
- Inspect calendars and busy windows across all future dates (no horizon limit, including recurring `RRULE` series) using `list-calendars`, `query-freebusy --from "<ISO>" --to "<ISO>"`, `list-events`, and `hold-allowlist --list`.
- Support optional personal rules registered in `instance/extensions/` with zero personal logic hardcoded in `core/`.

## 2. Allowlisted `[HOLD]` Lifecycle, Bookings & Self-Invite Exclusion
1. **Strict Rule — Zero Owner Self-Invites & Zero Merchant Invites**:
   - Owner personal addresses and `sendAs` aliases (`identity.default_send_as_email`, `identity.calendar_organizer_email`, `identity.never_invite_emails`, plus authenticated Gmail addresses) and automated addresses (`noreply`, `no-reply`, `feedback@`, `bounces+`) must **never** appear in Google Calendar `attendees`. For merchant ticket/booking/hotel confirmations, enforce `attendees: []` and `--no-send-invites`.
2. **Placing a `[HOLD]` (`create-event --hold`)**:
   - Blocks calendar availability as `opaque` (`[HOLD] <Subject>`). Only contacts explicitly listed in `calendar_hold_allowlist.json` are invited immediately; external interlocutors are stored in `alfred_candidate_attendees` (**zero external notification at `[HOLD]` stage**).
3. **Confirmation, Update, or Deletion Under Mandatory Gate (`implementation_plan.md`)**:
   - Every `[HOLD]` confirmation (`confirm-event --send-invites` or `--no-send-invites`, with `--location` / `--description`), update (`update-event`), or deletion (`delete-event`) must be approved in **`implementation_plan.md`** (`RequestFeedback: true`).

## 3. Stage 3 — Post-Validation Execution (`plan-executor` — Lightweight `Hydrogen`)
- After `Proceed`, `plan-executor` executes the approved calendar command, cleans up competing `[HOLD]` events on the same thread, and archives the source Gmail thread by default (unless `--keep-in-inbox`).
- Any mutation run from another workspace automatically surfaces a visible `EXECUTED` trace in `alfred-calendar` (deduplicated when run directly inside `alfred-calendar`, unless `--archive-trace` is explicitly set).
