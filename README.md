# 🎩 Alfred — Personal Chief of Staff (Version 7.1 Lean & Antigravity-Native)

**Alfred** est un assistant personnel souverain propulsé **100 % par Google Antigravity Web** et directement connecté aux APIs natives **Google Workspace** (Gmail, Google Drive, Google Calendar) à **0,00 € de coût marginal**.

---

## ✨ Philosophie V7.1 Lean : Zéro Usine à Gaz & Efficacité Maximale en Tokens

1. **100 % Antigravity Web UI** : Pas de bot Google Chat externe, pas de WebApp superflue à maintenir — toutes les interactions, revues et validations passent par l'interface native d'Antigravity Web et ses artefacts **`implementation_plan.md`** annotables par commentaires inline.
2. **Pattern Universel en 3 Étapes avec Recherche Adaptative** :
   - **1. Analyse & Recherche Adaptative en Mode Turbo (`Read-Only`)** : S'adapte à la question (0 sous-agent si auto-suffisant, 1 sous-agent sur les Frontmatters YAML / Fiches de Référence, ou *spread* parallèle multi-agents sur Drive, Gmail, Calendar et le Web).
   - **2. Gate Systématique (`implementation_plan.md`)** : Un agent synthétiseur rédige le plan d'implémentation (`RequestFeedback: true`). Aucune action d'écriture ne s'exécute tant que le plan n'est pas validé (`Proceed`).
   - **3. Agent Exécuteur Unique** : Applique les changements validés, maintient à jour les Fiches de Référence `.md`, et génère les Pull Requests sur les 2 dépôts Git dans `alfred-evolve`.
3. **Architecture de Connaissance Hybride en 3 Étages (Progressive Disclosure)** :
   - **Étage 1 — Frontmatters YAML (`instance/data/knowledge/*.md`)** : Scan instantané à ~50 tokens des métadonnées YAML.
   - **Étage 2 — Fiches de Référence *Up-to-Date* (`<= 150 lignes`) + `drive_metadata_index.json`** : Synthèse à jour sauvegardée automatiquement dans `_ALFRED_REFERENTIEL_` sur Google Drive, complétée par la taxonomie construite au First-Run (`init-drive`).
   - **Étage 3 — Documents Bruts Google Drive (`drive_read`)** : Extraction directe à 0 OCR sur les Docs/Sheets/PDF natifs uniquement lorsqu'un détail de pièce justificative est nécessaire.

---

## 🎯 Les 4 Cas d'Usage Cœurs

1. **`init-drive` — Scaffolding Exhaustif du Google Drive au First-Run** :
   - Analyse l'arborescence complète de votre Google Drive, construit `drive_metadata_index.json` et propose dans `implementation_plan.md` la fiche maîtresse `00_drive_taxonomy_and_filing_rules.md`.
2. **`alfred-inbox` — Routine des Nouveaux E-mails à 3 Sorties Exclusives** :
   - **`SILENT_NO_ACTION`** : Ignore le bruit et les notifications passives de portails (*« Vous avez de nouveaux documents sur votre espace Generali »*, factures d'abonnement courantes sans anomalie) et **archive automatiquement** la conversation Antigravity.
   - **`DRAFT_REPLY`** : Crée un brouillon dans Gmail, garde la conversation Antigravity Web allumée et présente le brouillon dans `implementation_plan.md` pour correction par commentaires inline.
   - **`USER_ACTION_REQUIRED`** : Étoile automatiquement l'e-mail (`STARRED`) dans Gmail, garde la conversation allumée et livre dans `implementation_plan.md` un **défrichage complet** de l'action à réaliser.
3. **`alfred-calendar` — Suivi de l'Assistante Maternelle (`Garde Nounou`)** :
   - Initialisé à partir du **contrat signé** (`init-nounou`), il gère le calendrier dédié **`Garde Nounou`** (`seed-nounou` le 20 du mois $M$ pour $M+1$, vacances et préavis ~1 mois, et clôture Pajemploi déterministe `close-pajemploi` à `last_day - 3` y compris en février).
4. **`alfred-life` & `alfred-evolve` — Requêtes Libres & Évolutivité Dual-Git** :
   - Posez toute question dans `alfred-life` ou demandez une évolution dans `alfred-evolve` (développée en TDD strict dans un `git worktree` isolé avec génération automatique des **Pull Requests sur les 2 dépôts Git** : `alfred-core` et `alfred-instance`).

---

## 🚀 Onboarding & CLI Quickstart

```bash
# 1. Authentification OAuth2 Google Workspace & Initialisation des 4 Projets Antigravity
.venv/bin/alfred auth-google
.venv/bin/alfred init-workspaces --prune-legacy

# 2. First-Run Google Drive (Étape 2 Gate -> Étape 3 Persistance Taxonomie)
.venv/bin/alfred init-drive
.venv/bin/alfred init-drive --execute

# 3. Référentiel 3-Étages & Google Drive (0-OCR natif)
.venv/bin/alfred scan-knowledge --query "nounou"
.venv/bin/alfred load-knowledge --doc "02_assistante_maternelle.md"
.venv/bin/alfred upsert-knowledge --file "03_dossiers_actifs_et_administratif.md" \
  --id "dossiers_actifs_et_administratif" --title "Dossiers Actifs" \
  --domain "admin" --summary "Suivi administratif à jour" --body-file /tmp/body.md
.venv/bin/alfred search-drive --query "contrat"
.venv/bin/alfred sync-drive
.venv/bin/alfred read-drive --file-id "<drive_file_id>"

# 4. Routine Inbox (3 sorties exclusives ; archivage Gmail automatique en fin de traitement interactif)
.venv/bin/alfred poll-inbox --spawn-agy --cognitive-agy
.venv/bin/alfred send-draft --draft-id "<draft_id>" --thread-id "<thread_id>" --to "contact@example.com" --body "Confirmé."
.venv/bin/alfred archive-email --thread-id "<thread_id>" --unstar

# 5. Calendriers & Assistante Maternelle (Garde Nounou + Pajemploi)
.venv/bin/alfred list-events --calendar primary --from "2026-05-01T00:00:00Z" --to "2026-05-31T23:59:59Z"
.venv/bin/alfred list-events --calendar nounou --from "2026-05-01T00:00:00Z" --to "2026-05-31T23:59:59Z"
.venv/bin/alfred init-nounou --contract-json instance/data/state/nounou_contract.json --execute
.venv/bin/alfred seed-nounou --date 2026-05-20 --apply
.venv/bin/alfred check-vacation --start 2026-08-01 --end 2026-08-15 --persist
.venv/bin/alfred close-pajemploi --year 2026 --month 5

# 6. Évolutivité Dual-Git & 5 Gardiens Déterministes
.venv/bin/alfred evolve --id "feat_01" --title "Nouvelle règle" --description "Détail" --execute
python3 core/scripts/check_code_architecture.py --root core/
.venv/bin/pytest --cov=core --cov-fail-under=90
.venv/bin/ruff check core
.venv/bin/mypy --strict core/alfred core/scripts core/tests
bash core/scripts/check-upstream-leak.sh
```
