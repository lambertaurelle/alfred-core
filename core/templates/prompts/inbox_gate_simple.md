# Action Plan `alfred-inbox` — {{output_type}} (`Orchestrator (Neon)`)

## 1. Email Summary
- **From**: `{{sender}}`
- **Subject**: {{subject}}
- **Message ID**: `{{message_id}}` | **Thread ID**: `{{thread_id}}`
- **Classification**: `{{reason}}`
{{security_banner}}

## 2. Context & Preliminary Groundwork
{{research_section}}

## 3. Proposal Pending Approval (`Proceed`)
{{action_section}}

## 4. Post-Validation Execution (`Orchestrator (Neon)`)
Upon clicking **`Proceed`**, `plan-executor` (`gemini-pro-orchestrator-neon`, without `--effort`) will run only the approved commands above (automatically archiving the Gmail thread unless `--keep-in-inbox` is requested).
