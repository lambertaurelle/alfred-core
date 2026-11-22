# 🎩 Workspace `alfred-life` — Hub Family Office Chief of Staff (User-Initiated & Pont MCP)

> **Routage Modèle (Brain)** : `gemini-pro-orchestrator` (User-Initiated ou `alfred_life_brain_dump` via MCP Gemini)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **1. Recherche Adaptative `Read-Only` $\rightarrow$ 2. Gate `implementation_plan.md` $\rightarrow$ 3. Exécuteur Léger `Hydrogen` (`plan-executor`)**

## 1. Étape 1 — Analyse & Recherche Adaptative en Mode Turbo (`Read-Only`)
Adapte immédiatement l'ampleur de l'investigation à la demande :
- **0 sous-agent** si la consigne est auto-suffisante $\rightarrow$ passage direct à l'Étape 2.
- **1 sous-agent ciblé (`research-drive` ou `research-gmail-cal`)** pour scanner les Frontmatters YAML (`instance/data/knowledge/*.md`), lire une fiche *up-to-date* (`<= 150L`) ou vérifier agendas, tâches et contacts.
- **Recherche « Spread » parallèle (`research-drive` + `research-gmail-cal` + `research-web`)** ou étude approfondie via **`study-orchestrator`** lorsque le dossier croise patrimoine, fiscalité, pièces Google Drive, historique Gmail et règles officielles.

## 2. Étape 2 — Synthèse & Gate Systématique (`implementation_plan.md`)
- `plan-synthesizer` génère **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
- Toute écriture Gmail, Google Calendar, Google Tasks, Contacts, délégation inter-workspaces (`alfred delegate --to <role>`), préparation de dossier à signer (`prepare-signature` — signature humaine hors-bande exclusivement, zéro signature autonome) ou mise à jour du référentiel est bloquée jusqu'au **`Proceed`** utilisateur.

## 3. Étape 3 — Exécution Post-Validation (`plan-executor` — Modèle Léger `Hydrogen`)
- Un agent unique léger `Hydrogen` (`plan-executor`) exécute les commandes CLI validées et sauvegarde automatiquement les fiches `.md` modifiées sur Google Drive (`_ALFRED_REFERENTIEL_`).
- **Surfaçage Automatique Inter-Workspaces** : toute mutation Calendar (`create-event`, `update-event`, `confirm-event`, `delete-event`, `hold-allowlist --add/--remove`) ou Tasks/Contacts/Suite (`create-task`, `complete-task`, `update-task`, `delete-task`, `create-contact`, `create-doc`, `append-sheet`) lancée depuis `alfred-life` surface automatiquement une trace visible (`EXECUTED`) dans `alfred-calendar` ou `alfred-tasks` (désactivable via `--no-archive-trace`).
- **Règle Inbox-Zero par défaut** : tout e-mail traité de manière interactive est archivé dans Gmail à la fin (`send-draft` ou `archive-email --thread-id ...`), sauf option explicite `--keep-in-inbox`.
