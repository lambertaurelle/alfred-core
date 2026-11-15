# 🎩 Alfred V8.0 — Family Office Chief of Staff

Assistant personnel souverain sur **Antigravity Web** + **Google Workspace** + **Pont MCP OAuth 2.1 pour Gemini App**, structuré en mono-repo zéro-fuite (`core/` générique open-source, `instance/` privé gitignoré).

---

## 1. Les 6 Workspaces Spécialisés & Routage Modèles

| Workspace | Rôle Métier | Modèle Principal | Exécution Post-Gate (`Proceed`) |
| :--- | :--- | :--- | :--- |
| `alfred-onboard` | Onboarding interactif, flags modulaires, OAuth2 & taxonomie Drive | `gemini-pro-orchestrator` | `plan-executor` (`Hydrogen`) |
| `alfred-life` | Hub Family Office libre, études (`study-orchestrator`) & MCP | `gemini-pro-orchestrator` | `plan-executor` (`Hydrogen`) |
| `alfred-inbox` | Triage e-mails (pré-filtre 0-token, pare-feu SPF/DKIM, 3 sorties) | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| `alfred-calendar` | Multi-calendriers, `[HOLD]` allowlistés & disponibilités | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| `alfred-tasks` | Google Tasks, échéances RFC 3339 & Google Contacts | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| `alfred-evolve` | Évolutivité Mono-Repo en `git worktree` isolé & extensions | `gemini-pro-orchestrator` | Triade `tdd-red` $\rightarrow$ `tdd-green` $\rightarrow$ `tdd-verifier` |

---

## 2. Quickstart (`bootstrap.sh` $\rightarrow$ `alfred-onboard`)

```bash
# 1. Bootstrap de l'environnement, instance/ gitignoré et hooks Zéro-Fuite
./scripts/bootstrap.sh

# 2. Diagnostic modulaire, guide GCP OAuth2 minimal et consentement initial
.venv/bin/alfred status-onboarding --json
.venv/bin/alfred oauth-guide
.venv/bin/alfred auth-google                    # poste local avec navigateur
# Serveur distant (SSH headless) :
# .venv/bin/alfred auth-google --no-browser
# (ou en 2 étapes : --print-url puis --redirect-url "<url_localhost>")

# 3. Provisionnement des 6 projets Antigravity et découverte Google Drive
.venv/bin/alfred init-workspaces --prune-legacy
.venv/bin/alfred discover-drive --json
# Ouvrir ensuite le projet 'alfred-onboard' dans Antigravity Web pour valider la configuration.
```

---

## 3. Modules Fonctionnels, APIs GCP & Scopes OAuth2

Seules les APIs et permissions correspondant aux modules activés dans `instance/config/settings.yaml` (`features.*`) sont requises (`SKIPPED_DISABLED` propre si désactivé) :

| Feature Flag (`settings.yaml`) | Défaut | API(s) Google Cloud | Scopes OAuth2 (`https://www.googleapis.com/auth/...`) |
| :--- | :---: | :--- | :--- |
| `drive_knowledge_sync` | `true` | `drive.googleapis.com`, `sheets.googleapis.com` | `drive` |
| `inbox_auto_triage` | `true` | `gmail.googleapis.com` | `gmail.modify`, `gmail.compose`, `gmail.settings.basic` |
| `gmail_drafts_and_send` | `true` | `gmail.googleapis.com` | `gmail.modify`, `gmail.compose`, `gmail.settings.basic` |
| `calendar_management` | `true` | `calendar-json.googleapis.com` | `calendar` |
| `tasks_management` | `true` | `tasks.googleapis.com` | `tasks` |
| `contacts_lookup` | `true` | `people.googleapis.com` | `contacts`, `contacts.other.readonly` |
| `google_docs_suite` | `false` | `docs`, `sheets`, `slides`, `forms.googleapis.com` | `documents`, `spreadsheets`, `presentations`, `forms.body`, `forms.responses.readonly` |
| `signature_delegation` | `false` | Local (`signature_vault/` protégé par `PreToolUse`) | Aucun scope additionnel (préparation PDF ; signature humaine hors-bande) |
| `mcp_gemini_bridge` | `false` | Local (`127.0.0.1:8765` OAuth 2.1 PKCE `S256`) | Aucun scope GCP (authentification dédiée PBKDF2 `mcp_oauth.json`) |

---

## 4. Identité `sendAs` vs Exclusion d'Invités Google Calendar

| Clé `identity.*` (`settings.yaml`) | Exemple | Effet Déterministe |
| :--- | :--- | :--- |
| `default_send_as_email` | `contact@domaine-perso.example` | En-tête `From:` appliqué aux brouillons et envois Gmail (vérifié via `gmail.settings.basic`). |
| `default_send_as_display_name` | `Cabinet Exemple` | Nom d'affichage RFC 2822 combiné à `default_send_as_email`. |
| `calendar_organizer_email` | `agenda@domaine-perso.example` | Adresse organisatrice exclue d'office de toute liste `attendees`. |
| `never_invite_emails` | `["archive@example.com"]` | Adresses personnelles/alias **jamais** ajoutées en invités Google Calendar (zéro auto-invitation). |

---

## 5. Exposition Externe MCP pour Gemini App (OAuth 2.1 PKCE + HTTPS TLS)

Le pont MCP (`/.well-known/*`, `/oauth/{register,authorize,token}` PKCE `S256`, `POST /mcp` : `alfred_life_brain_dump`, `alfred_session_status`, `alfred_approve_plan`, `alfred_quick_context`) se déploie en 3 étapes :

### 1. Côté Serveur Alfred (`apply-onboarding` + `setup-mcp` + `install-routines`)
```bash
# Local cloudflared (sur la même machine, host 127.0.0.1 par défaut) :
.venv/bin/alfred apply-onboarding --enable mcp_gemini_bridge --mcp-public-url "https://alfred.domaine-perso.example"
# Tunnel cloudflared déporté sur un autre conteneur/hôte du LAN (ajouter --mcp-host "0.0.0.0") :
.venv/bin/alfred apply-onboarding --enable mcp_gemini_bridge --mcp-host "0.0.0.0" --mcp-public-url "https://alfred.domaine-perso.example"

# Initialiser le PIN PBKDF2-HMAC-SHA256 puis installer et démarrer alfred-mcp.service :
.venv/bin/alfred setup-mcp --pin "<PIN>"
.venv/bin/alfred install-routines
```

### 2. Côté Réseau (`Option A — Cloudflare Zero Trust Tunnel` vs `Option B — Port 443 Direct Caddy/Nginx`)
- **Option A — Cloudflare Zero Trust Tunnel (`cloudflared`)** : Public Hostname `alfred.domaine-perso.example` $\rightarrow$ Service `HTTP` `127.0.0.1:8765` (local) ou `<IP_SERVEUR_ALFRED>:8765` (tunnel LAN déporté). **Aucune application Cloudflare Access (OTP/Login)** devant ce sous-domaine (l'OAuth 2.1 + PIN d'Alfred gère l'authentification) + règle WAF **Skip** sur `/.well-known/*`, `/oauth/*`, `/mcp` :
  - Expression WAF : `(http.host eq "alfred.domaine-perso.example" and (starts_with(http.request.uri.path, "/.well-known/") or starts_with(http.request.uri.path, "/oauth/") or http.request.uri.path eq "/mcp"))`
- **Option B — Port 443 Direct (Caddy / Nginx)** : pointer le DNS `A` `alfred.domaine-perso.example` (ex. `203.0.113.10`, port `443`) vers `reverse_proxy 127.0.0.1:8765` (`flush_interval -1`, `X-Forwarded-Proto`/`Host`) avec TLS automatique.

### 3. Côté Gemini App
Connecter `https://alfred.domaine-perso.example/mcp` dans Gemini App et valider le `<PIN>` sur l'écran OAuth 2.1 d'Alfred.

---

## 6. Commandes CLI & Gardiens Déterministes

```bash
.venv/bin/alfred scan-knowledge && .venv/bin/alfred poll-inbox --spawn-agy --cognitive-agy
.venv/bin/alfred list-calendars && .venv/bin/alfred query-freebusy --from "2026-11-16T08:00:00Z" --to "2026-11-16T18:00:00Z"
.venv/bin/alfred list-tasks && .venv/bin/alfred search-contacts --query "Alice"
.venv/bin/alfred delegate --to tasks --title "Relance" --brief "Vérifier échéance"
.venv/bin/python core/scripts/check_code_architecture.py --root core/
bash core/scripts/check-upstream-leak.sh
.venv/bin/ruff check core && .venv/bin/mypy --strict core/alfred core/scripts core/tests
.venv/bin/pytest --cov=core --cov-fail-under=90
```
