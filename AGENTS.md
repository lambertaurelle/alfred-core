# 🎩 AGENTS.md — Guide Suprême d'Orchestration `/root/Alfred` (V8.0 Family Office Chief of Staff)

> **Périmètre** : Mono-Repo Zéro-Fuite `/root/Alfred` (`ALFRED_ROOT_DIR`)  
> **Interfaces** : **Antigravity Web UI** + **Pont MCP OAuth 2.1 PKCE vers Gemini App** (`/mcp`)  
> **Standards** : TDD Isolé (`>= 90%`), Clean Architecture OOP 4 Couches (`<= 500L/fichier`, `<= 60L/méthode` hors docstring, `main.py <= 79L`) & Zéro Donnée Personnelle dans Git (`check-upstream-leak.sh`).

---

## 1. Les 6 Workspaces Spécialisés & Routage Modèles (Brain vs Muscle)

| Workspace | Rôle | Modèle Principal | Exécution Étape 3 |
| :--- | :--- | :--- | :--- |
| **`alfred-onboard`** | Onboarding interactif, flags modulaires, OAuth2 & taxonomie Drive | `gemini-pro-orchestrator` | `plan-executor` (`Hydrogen`) |
| **`alfred-life`** | Hub Family Office libre, études transverses & `alfred_life_brain_dump` MCP | `gemini-pro-orchestrator` | `plan-executor` (`Hydrogen`) |
| **`alfred-inbox`** | Triage nouveaux e-mails (pré-filtre 0-token + pare-feu SPF/DKIM + 3 sorties) | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| **`alfred-calendar`** | Multi-calendriers, `[HOLD]` allowlistés & exclusion d'auto-invitation | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| **`alfred-tasks`** | Google Tasks, échéances RFC 3339 & annuaire Google Contacts | `gemini-4-hydrogen-medium` | `plan-executor` (`Hydrogen`) |
| **`alfred-evolve`** | Évolutivité Mono-Repo en `git worktree` isolé & extensions `instance/` | `gemini-pro-orchestrator` | Triade TDD `tdd-red` $\rightarrow$ `tdd-green` $\rightarrow$ `tdd-verifier` |

*Note CLI `agy` : ne jamais passer `--effort` lorsque le modèle sélectionné contient `orchestrator`. Les traces d'audit auto-archivées utilisent `gemini-4-hydrogen-low`.*

---

## 2. Le Pattern Universel en 3 Étapes (Avec Exécution Asymétrique)

1. **Étape 1 — Analyse & Recherche Adaptative en Mode Turbo (`Read-Only`)** :
   - **0 sous-agent** si la demande est auto-suffisante $\rightarrow$ passage direct à l'Étape 2.
   - **Recherche ciblée ou « Spread » parallèle** : `research-drive`, `research-gmail-cal`, `research-web`, et escalade **`study-orchestrator`** pour les études patrimoniales, fiscales ou juridiques multi-sources.
2. **Étape 2 — Synthèse & Gate Systématique Bloquante (`implementation_plan.md`)** :
   - `plan-synthesizer` génère **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
   - **STOP BLOQUANT** : aucune mutation Gmail, Calendar, Tasks, Drive ou code n'est exécutée avant clic utilisateur sur **`Proceed`**.
3. **Étape 3 — Exécution Post-Validation Asymétrique** :
   - **Workspaces opérationnels (`onboard`, `life`, `inbox`, `calendar`, `tasks`)** : exécution rapide par un sous-agent léger **`Hydrogen`** unique (**`plan-executor`**) appelant exclusivement `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`. Par défaut, tout fil e-mail traité en interactif est archivé dans Gmail (`INBOX` retiré, sauf `--keep-in-inbox`).
   - **Workspace d'ingénierie (`alfred-evolve`)** : exécution obligatoire dans un `git worktree` isolé (`worktrees/<proposal_id>`) via la **triade TDD à contextes séparés** :
     1. **`tdd-red`** : rédige uniquement les tests en échec sous `core/tests/` (zéro accès écriture à `core/alfred/`).
     2. **`tdd-green`** : implémente uniquement le code de production (interdiction absolue d'éditer, affaiblir ou ignorer `core/tests/`).
     3. **`tdd-verifier`** : auditeur `Read-Only` indépendant validant les 5 gardiens déterministes (`check_code_architecture.py`, `check-upstream-leak.sh`, `ruff check`, `mypy --strict`, `pytest --cov=core --cov-fail-under=90`) avant ouverture de la Pull Request Mono-Repo sur `alfred-core`.

---

## 3. Architecture de Connaissance en 3 Étages (Progressive Disclosure)

1. **Étage 1 — Frontmatters YAML (`instance/data/knowledge/*.md`)** : scan à ~50 tokens (`alfred scan-knowledge`) des métadonnées (`id`, `title`, `domain`, `summary`, `keywords`, `drive_source_ids`, `updated_at`).
2. **Étage 2 — Fiches de Référence *Up-to-Date* (`<= 150L`) & `drive_metadata_index.json`** : chargement ciblé (`alfred load-knowledge`, `alfred search-drive`) et sauvegarde automatique vers `_ALFRED_REFERENTIEL_` sur Google Drive.
3. **Étage 3 — Documents Bruts Google Drive (`alfred read-drive`)** : lecture native à 0 OCR uniquement lorsqu'un détail de pièce justificative est requis.

---

## 4. Garde-Fous de Sécurité & Identité (Defense-in-Depth)

1. **Hook Déterministe `PreToolUse` (`.agents/hooks.json` $\rightarrow$ `security_pre_tool_hook.py`)** : bloque l'accès direct des agents à `signature_vault/`, `mcp_oauth.json`, `token.json`, `client_secret*.json` et aux IDs Drive protégés.
2. **Signature Manuscrite 100 % Hors-Bande Humaine** : `prepare-signature` prépare le dossier PDF pour signature par l'utilisateur ; Alfred n'appose **jamais** de signature manuscrite de manière autonome.
3. **Pare-Feu Inbox & SPF/DKIM** : détection d'injection de prompt et interdiction formelle de déclencher `alfred-evolve` depuis un e-mail non authentifié propriétaire SPF/DKIM.
4. **Identité `sendAs` vs Exclusion Calendar** : `identity.default_send_as_email` signe les brouillons/envois Gmail, tandis que les adresses personnelles et alias `sendAs` (`never_invite_emails`) sont systématiquement filtrés des `attendees` Google Calendar.
