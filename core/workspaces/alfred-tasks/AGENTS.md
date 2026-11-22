# ✅ Workspace `alfred-tasks` — Gestion Google Tasks, Échéances & Annuaire Google Contacts

> **Routage Modèle (Muscle)** : `gemini-4-hydrogen-medium` (User-Initiated ou Délégation `alfred delegate --to tasks`)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **1. Inspection `Read-Only` $\rightarrow$ 2. Gate `implementation_plan.md` $\rightarrow$ 3. Exécuteur Léger `Hydrogen` (`plan-executor`)**

## 1. Étape 1 — Revue des Tâches, Échéances & Contacts (`Read-Only`)
- Consulte les listes et tâches actives via `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred list-tasklists` et `list-tasks [--tasklist "<id>"]`.
- Résout les coordonnées exactes des interlocuteurs via `search-contacts --query "<nom_ou_email>"` et `get-contact --resource-name "<id>"` (Google People API + `otherContacts`) et recoupe si besoin avec `research-drive` ou `research-gmail-cal`.

## 2. Étape 2 — Synthèse & Gate Systématique (`implementation_plan.md`)
- `plan-synthesizer` structure les créations, mises à jour, suppressions, échéances RFC 3339, notes contextuelles (références `threadId` Gmail ou `file_id` Drive), créations de contacts et clôtures de tâches dans **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
- **Gate bloquante obligatoire** : aucune création, modification ou clôture de tâche/contact n'est appliquée avant clic utilisateur sur **`Proceed`**.

## 3. Étape 3 — Exécution Post-Validation (`plan-executor` — Modèle Léger `Hydrogen`)
- Après `Proceed`, l'agent unique léger `Hydrogen` (`plan-executor`) exécute selon le plan validé :
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred create-task --title "<titre>" [--tasklist "<id>"] [--notes "<notes>"] [--due "<RFC3339>"]`
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred complete-task --task-id "<id>" [--tasklist "<id>"]`
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred update-task --task-id "<id>" [--title "<titre>"] [--notes "<notes>"] [--due "<RFC3339>"] [--tasklist "<id>"]`
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred delete-task --task-id "<id>" [--tasklist "<id>"]`
  - `${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred create-contact --given-name "<prénom>" [--family-name "<nom>"] [--email "<email>"]`
- Toute mutation exécutée depuis un autre workspace surface automatiquement une trace visible (`EXECUTED`) dans `alfred-tasks` (dédupliquée lorsqu'exécutée déjà depuis `alfred-tasks`, sauf `--archive-trace` ; désactivable via `--no-archive-trace`).
