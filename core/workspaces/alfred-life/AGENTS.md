# 🎩 Workspace `alfred-life` — Hub Principal d'Interaction (User-Initiated)

> **Mode d'Ouverture** : **User-Initiated** (point d'entrée libre dans Antigravity Web UI)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **Pattern Universel en 3 Étapes (Recherche Adaptative $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ Exécuteur Unique)**

## 1. Étape 1 — Analyse & Recherche Adaptative en Mode Turbo (`Read-Only`)
Adapte immédiatement le niveau de recherche à la demande :
- **0 sous-agent** si la question ou consigne est auto-suffisante $\rightarrow$ passe directement à l'Étape 2.
- **1 sous-agent ciblé (`research-drive` ou `research-gmail-cal`)** s'il suffit de scanner les Frontmatters YAML (`instance/data/knowledge/*.md`), lire une fiche *up-to-date* (`<= 150L`) ou vérifier un créneau agenda / allowlist `[HOLD]` (`alfred hold-allowlist --list`).
- **Recherche « Spread » parallèle (`research-drive` + `research-gmail-cal` + `research-web`)** en mode Turbo sans confirmation si la question croise documents Drive, historique mails et règles externes.
- Pour l'initialisation First-Run du Drive, exécute `alfred init-drive` afin de construire `drive_metadata_index.json` et proposer `00_drive_taxonomy_and_filing_rules.md`.

## 2. Étape 2 — Synthèse & Gate Systématique (`implementation_plan.md`)
- L'agent `plan-synthesizer` consolide la réponse et génère **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
- **Gate bloquante obligatoire** : attends la validation (`Proceed`) et les éventuels commentaires inline de l'utilisateur avant toute écriture Gmail, Google Calendar (`create-event --hold` / `confirm-event --send-invites`), ou référentiel.

## 3. Étape 3 — Exécution Post-Validation (`plan-executor`)
- Applique uniquement les actions validées (brouillons Gmail, blocages `[HOLD]` allowlistés ou invitations Google Calendar validées, mise à jour de `hold-allowlist`, et Fiches de Référence `.md` sauvegardées automatiquement sur Google Drive).
- **Règle Inbox-Zero par défaut** : lorsqu'un e-mail a été traité de manière interactive avec l'utilisateur, **archive par défaut ce mail dans Gmail à la fin** (`send-draft` par défaut ou `alfred archive-email --thread-id ...`), sauf si l'utilisateur demande l'inverse (`--keep-in-inbox`).
