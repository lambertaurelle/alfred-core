# Alfred V8.0 — Family Office Chief of Staff

**Free, open-source** personal Chief of Staff combining **Antigravity Web**, local Python automation over Google Workspace, and an **OAuth 2.1 PKCE MCP bridge for Gemini App**.

![Alfred V8.0 — End-to-End Workflow & Out-of-the-Box Use Cases](docs/assets/alfred-workflow.jpg)

---

## Why Alfred (Compared to Standalone Gemini App)?

1. **Consistent Workflows Without Re-Prompting**: Alfred enforces a deterministic *read-only research $\rightarrow$ approved plan $\rightarrow$ execution* cycle across every task, without manually repeating instructions or permissions.
2. **Background API Polling (Near-Push)**: local periodic routines poll Gmail, Google Calendar, and Google Drive at regular intervals — incoming emails and Drive deltas are triaged, filed, and drafted before you even open a chat.
3. **English-Only Repo + Bilingual FR/EN Runtime**: all repository code, docs, templates, and agent manifests are 100% English for global open-source contributors, while Alfred natively operates in **both French and English** at runtime (answering in your language and drafting replies matching incoming emails).

The mono-repo strictly separates **`core/`** (generic open-source engine) from **`instance/`** (gitignored personal data, OAuth secrets, routines, and settings). Full technical reference: [docs/SPEC.md](docs/SPEC.md).

---

## One-Command Install

```bash
git clone https://github.com/<owner>/alfred-core.git ~/Alfred && cd ~/Alfred && ./scripts/bootstrap.sh --onboard
```

`./scripts/bootstrap.sh --onboard` initializes gitignored `instance/`, builds `.venv`, activates `.githooks/`, and runs `alfred bootstrap-gcp --finish-setup`.

---

## Step-by-Step Google Cloud Setup

**What `alfred bootstrap-gcp` automates 100%:**
- Installs the `gcloud` SDK locally under `~/.local/google-cloud-sdk` (no `sudo`, Linux `x86_64` / `aarch64`);
- Authenticates via `gcloud auth login --no-launch-browser` (SSH-friendly);
- Creates or selects your GCP project and enables only the APIs required by your active flags (`gcloud services enable`).

**2 quick browser steps guided by the `?project=<project_id>` URLs printed in your terminal:**
1. **Google Auth Platform $\rightarrow$ Audience**: select *External* (or *Testing* if prompted), click **`+ Add users`** under **Test users**, add your Gmail address, and click **`Save`**.
2. **Clients $\rightarrow$ `+ Create client`**: pick **`Desktop app`**, name it `Alfred`, click **`Create`**, then **`Download JSON`**.
3. **Direct SSH Paste**: paste the downloaded JSON directly into the terminal (no `scp` needed; saved `0600` in `instance/secrets/`), then approve the OAuth URL from `alfred auth-google --no-browser`.

| Module (`features.*`) | Default | Enabled GCP APIs | OAuth2 Scopes (`https://www.googleapis.com/auth/...`) |
| :--- | :---: | :--- | :--- |
| `drive_knowledge_sync` | `true` | `drive`, `sheets` | `drive` |
| `inbox_auto_triage` | `true` | `gmail` | `gmail.modify`, `gmail.compose`, `gmail.settings.basic` |
| `gmail_drafts_and_send` | `true` | `gmail` | `gmail.modify`, `gmail.compose`, `gmail.settings.basic` |
| `calendar_management` | `true` | `calendar-json` | `calendar` |
| `tasks_management` | `true` | `tasks` | `tasks` |
| `contacts_lookup` | `true` | `people` | `contacts`, `contacts.other.readonly` |
| `google_docs_suite` | `false` | `docs`, `sheets`, `slides`, `forms` | `documents`, `spreadsheets`, `presentations`, `forms.body`, `forms.responses.readonly` |
| `drive_watcher` | `false` | `drive`, `sheets` | `drive` |
| `signature_delegation` | `false` | Local (PDF preview only) | None (out-of-band human signing only) |
| `mcp_gemini_bridge` | `false` | Local (`127.0.0.1:8765`) | None (OAuth 2.1 PKCE `S256` + PBKDF2 PIN) |

---

## The 7 Specialist Workspaces & Daily CLI

| Workspace | Role | Model | Execution After `Proceed` |
| :--- | :--- | :--- | :--- |
| `alfred-onboard` | Diagnostics, feature flags, OAuth2 & Drive taxonomy | `gemini-pro-orchestrator` | `plan-executor` (`Hydrogen`) |
| `alfred-life` | Chief of Staff hub, multi-source studies & MCP bridge | `gemini-pro-orchestrator` | `plan-executor` (`Hydrogen`) |
| `alfred-inbox` | Bilingual email triage, Drive attachments & `sendAs` drafts | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| `alfred-calendar` | Multi-calendar `[HOLD]`s, bookings & FreeBusy lookup | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| `alfred-tasks` | Google Tasks (RFC 3339) & Google Contacts | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| `alfred-drive` | Non-destructive Drive governance & delta watcher (`watch-drive`) | `gemini-4-hydrogen-medium` | `plan-executor` / `drive-custodian` (`Hydrogen`) |
| `alfred-evolve` | `instance/`, `local-fork`, or `upstream-pr` PRs | `gemini-pro-orchestrator` | `tdd-red` -> `tdd-green` -> `doc-maintainer` -> `tdd-verifier` |

```bash
.venv/bin/alfred status-onboarding --json
.venv/bin/alfred scan-knowledge && .venv/bin/alfred poll-inbox --spawn-agy --cognitive-agy
.venv/bin/alfred list-calendars && .venv/bin/alfred list-tasks
.venv/bin/alfred install-routines && .venv/bin/alfred status-routines
```

---

## Evolution Modes (`--mode instance` vs `upstream-pr` vs `local-fork`)

1. **`--mode instance` (default for personal logic)**: routines and settings isolated under `instance/` (`instance/extensions/*.py`, `settings.yaml`). Zero PR and `docs/SPEC.md` untouched.
2. **`--mode upstream-pr`**: generic `core/` features (with anonymized examples under `instance/examples/`). Runs in an isolated `git worktree` via **`tdd-red` -> `tdd-green` -> `doc-maintainer` -> `tdd-verifier`**, auto-syncing [docs/SPEC.md](docs/SPEC.md) (`sync-spec`), enforcing *zero half-measures* (`--hybrid --core-complete`), detecting `[BREAKING]`, and falling back to a GitHub fork when needed.
3. **`--mode local-fork`**: local `core/` changes committed to `evolve/<id>` without an upstream PR.

---

## External MCP Bridge to Gemini App (HTTPS / TLS)

```bash
.venv/bin/alfred apply-onboarding --enable mcp_gemini_bridge --mcp-public-url "https://alfred.my-domain.example"
.venv/bin/alfred setup-mcp --pin "<PIN>" && .venv/bin/alfred install-routines
```

- **Option A — Cloudflare Tunnel (`cloudflared`)**: expose `https://alfred.my-domain.example` to `http://127.0.0.1:8765` (or `--mcp-host 0.0.0.0` on LAN), disable Cloudflare Access OTP, and set a WAF *Skip* rule on `/.well-known/*`, `/oauth/*`, and `/mcp`.
- **Option B — TLS Reverse Proxy (Caddy / Nginx, port `443`)**: forward HTTPS TLS traffic to `127.0.0.1:8765` without buffering (`X-Forwarded-Proto`, `Host`). Connect `https://alfred.my-domain.example/mcp` in Gemini App using your `<PIN>`.

---

## Deterministic Guardians

```bash
.venv/bin/python core/scripts/check_code_architecture.py --repo-root .
bash core/scripts/check-upstream-leak.sh .
.venv/bin/python core/scripts/check_english_only.py --repo-root .
.venv/bin/ruff check core && .venv/bin/mypy --strict core/alfred core/scripts core/tests
.venv/bin/pytest --cov=core --cov-fail-under=90 && .venv/bin/alfred sync-spec --check --repo-root .
```
