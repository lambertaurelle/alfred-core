# 🎩 Workspace `alfred-life` — Hub Principal d'Interaction (User-Initiated)

> **Mode d'Ouverture** : **User-Initiated** (point d'entrée libre dans Antigravity Web UI)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **Pattern Universel en 3 Étapes (Recherche Adaptative $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ Exécuteur Unique)**

## 1. Étape 1 — Analyse & Recherche Adaptative en Mode Turbo (`Read-Only`)
Adapte immédiatement le niveau de recherche à la demande :
- **0 sous-agent** si la question ou consigne est auto-suffisante $\rightarrow$ passe directement à l'Étape 2.
- **1 sous-agent ciblé (`research-drive` ou `research-gmail-cal`)** s'il suffit de scanner les Frontmatters YAML (`instance/data/knowledge/*.md`), lire une fiche *up-to-date* (`<= 150L`) ou vérifier un créneau agenda.
- **Recherche « Spread » parallèle (`research-drive` + `research-gmail-cal` + `research-web`)** en mode Turbo sans confirmation si la question croise documents Drive, historique mails et règles externes.
- Pour l'initialisation First-Run du Drive, exécute `alfred init-drive` afin de construire `drive_metadata_index.json` et proposer `00_drive_taxonomy_and_filing_rules.md`.

## 2. Étape 2 — Synthèse & Gate Systématique (`implementation_plan.md`)
- L'agent `plan-synthesizer` consolide la réponse et génère **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
- **Gate bloquante obligatoire** : attends la validation (`Proceed`) et les éventuels commentaires inline de l'utilisateur avant toute écriture.

## 3. Étape 3 — Exécution Post-Validation (`plan-executor`)
- Applique uniquement les actions validées et maintient à jour les Fiches de Référence `.md` (`KnowledgeService.upsert_document`, sauvegardées automatiquement dans `_ALFRED_REFERENTIEL_` sur Google Drive).
