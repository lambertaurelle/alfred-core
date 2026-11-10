# 🎩 Master Implementation Plan Flash-Ready (V7.1 Lean) — Alfred : Personal Chief of Staff

> **Interface Unique** : **100 % Antigravity Web UI** (mono-utilisateur, zéro Google Chat, zéro WebApp externe)  
> **Pattern Universel d'Exécution (3 Étapes)** :
> 1. **Analyse & Recherche Adaptative en Mode Turbo (`Read-Only`)** : La séquence s'adapte à la question posée (**0 recherche** si non pertinent, **recherche rapide** dans les Frontmatters YAML des Fiches de Référence `.md`, ou **recherche « Spread » approfondie** par plusieurs sous-agents parallèles dans Drive, Gmail, Calendar et Web).
> 2. **Synthèse & Gate Systématique (`implementation_plan.md`)** : Un agent synthétiseur résume les recherches et génère `implementation_plan.md` (`RequestFeedback: true`). **Gate bloquante obligatoire avant toute exécution.**
> 3. **Exécution Post-Validation** : Un dernier agent exécuteur applique le plan validé (et génère les **Pull Requests sur les 2 dépôts Git** dans le cas d'`alfred-evolve`).  
> **Architecture de Connaissance Hybride (3-Tiers)** :
> - **Tier 1 & 2 (`instance/data/knowledge/*.md` avec Frontmatter YAML)** : Fiches de Référence *Up-to-Date* rassemblant les infos synthétiques au même endroit (scan du Frontmatter YAML à ~50 tokens comme les `skills/`, sauvegardées en `.md` sur Google Drive).
> - **Tier 3 (Google Drive Direct + Index First-Run `init-drive`)** : Analyse exhaustive de ton Google Drive au premier lancement pour définir tes règles de rangement (`00_drive_taxonomy_and_filing_rules.md`) et alimenter `drive_metadata_index.json`.  
> **Standards Déterministes** : **TDD Strict** (`pytest --cov=core --cov-fail-under=90`), **Clean Architecture OOP**, **Fichiers $\le 500$ lignes & Méthodes $\le 60$ lignes (hors docstring)**, **100 % Docstrings Google-style**, **Zéro Donnée Personnelle dans Git**.

---

## 1. Arborescence Complète V7.1 Lean sous `/root/Alfred`

```text
/root/Alfred/                                      # 🌟 DÉPÔT GIT #1 (GÉNÉRIQUE : alfred-core)
├── .git/                                          # Historique propre sans aucune donnée personnelle ni IP privée
├── .gitignore                                     # Ignore /instance/, /worktrees/, .venv/, __pycache__/
├── .pre-commit-config.yaml & .githooks/pre-commit # Gardiens bloquants (AST OOP, Docstrings, Ruff, Mypy, Pytest, Leak)
├── pyproject.toml                                 # Configuration Ruff, Mypy (--strict), Pytest (--cov-fail-under=90)
├── AGENTS.md & README.md                          # Règles d'orchestration suprêmes V7.1 Lean
├── .agents/agents/                                # Sous-agents du Pattern Universel en 3 Étapes
│   ├── research-drive.md                          # Étape 1 (Turbo Read-Only) : Fiches Référence YAML + Google Drive API
│   ├── research-gmail-cal.md                      # Étape 1 (Turbo Read-Only) : exploration Gmail & Calendar API
│   ├── research-web.md                            # Étape 1 (Turbo Read-Only) : recherche documentation Internet
│   ├── plan-synthesizer.md                        # Étape 2 (Gate) : synthèse des recherches -> implementation_plan.md
│   └── plan-executor.md                           # Étape 3 (Write Post-Gate) : exécution du plan validé + PR Git
├── core/
│   ├── config/settings.example.yaml               # Exemple de configuration neutre
│   ├── templates/knowledge/                       # Templates de Fiches de Référence .md avec Frontmatter YAML
│   ├── scripts/
│   │   ├── check_code_architecture.py             # Linter AST (<= 500L/fichier, <= 60L/méthode hors docstring, 4 couches)
│   │   └── check-upstream-leak.sh                 # Gardien pré-commit 0 fuite de données personnelles
│   ├── alfred/                                    # Package Python Clean Architecture OOP (16 fichiers <= 500L)
│   │   ├── main.py                                # Point d'entrée CLI & Bootstrap DI (< 80 lignes)
│   │   ├── domain/
│   │   │   └── models.py                          # FrontmatterSchema, InboxTriageDecision, NounouContract, PajemploiReport
│   │   ├── repositories/
│   │   │   ├── json_repository.py                 # JsonStateRepository[T] & JsonlEventLog[T] (.lock + os.replace)
│   │   │   └── state_store.py                     # Accès typé aux fichiers d'état locaux
│   │   ├── services/
│   │   │   ├── knowledge_service.py               # Chargement Progressif 3-Tiers (Frontmatter YAML -> Fiche .md Up-to-Date)
│   │   │   ├── drive_service.py                   # API Google Drive v3 + init-drive (analyse exhaustive First-Run) + cache
│   │   │   ├── gmail_service.py                   # API Gmail v1 (history.list, drafts.create/update/send, STARRED)
│   │   │   ├── calendar_service.py                # API Google Calendar v3 (Agenda principal & calendrier Garde Nounou)
│   │   │   ├── agy_session_manager.py             # Ouverture de conversation Antigravity Web & auto-archivage SILENT_NO_ACTION
│   │   │   ├── inbox_triage_service.py            # Routine nouveaux messages à 3 sorties (SILENT / DRAFT / ACTION)
│   │   │   ├── nounou_service.py                  # Initialisation contrat signé, planning le 20 du mois, vacances & préavis
│   │   │   ├── pajemploi_calculator.py            # Calcul déterministe 0 token à last_day - 3 (février inclus)
│   │   │   └── evolve_worktree_service.py         # Git worktree isolé, validation TDD & génération PR sur les 2 repos Git
│   │   └── controllers/
│   │       └── cli_controller.py                  # Commandes : init-drive, poll-inbox, init-nounou, seed-nounou, close-pajemploi, evolve
│   ├── workspaces/                                # Manifests des 4 Workspaces Antigravity Web
│   │   ├── alfred-life/AGENTS.md                  # Hub User-Initiated (Recherche adaptative -> Plan Gate -> Exécuteur)
│   │   ├── alfred-inbox/AGENTS.md                 # Agent-Spawned Only (Nouveaux mails DRAFT_REPLY & USER_ACTION_REQUIRED)
│   │   ├── alfred-calendar/AGENTS.md              # Agent-Spawned Only (Assistante Maternelle Garde Nounou & Agenda)
│   │   └── alfred-evolve/AGENTS.md                # Évolutivité (Analyse -> Plan Gate -> Worktree TDD + PR sur les 2 repos)
│   └── tests/                                     # Tests unitaires TDD (>= 90% coverage)
└── instance/                                      # 🔒 DÉPÔT GIT #2 (PERSO : alfred-instance — structure & extensions, 0 data)
    ├── .git/ & .gitignore                         # Ignore secrets/ et data/
    ├── config/settings.yaml                       # Paramètres structurels de l'instance
    ├── extensions/                                # Évolutions personnelles créées via alfred-evolve
    ├── secrets/                                   # [IGNORÉ PAR GIT] OAuth2 client_secrets.json & token.json
    └── data/                                      # [IGNORÉ PAR GIT — Sauvegardé sur Google Drive]
        ├── knowledge/                             # 📘 FICHES DE RÉFÉRENCE UP-TO-DATE (.md avec Frontmatter YAML obligatoire)
        │   ├── 00_drive_taxonomy_and_filing_rules.md # Règles de rangement et cartographie du Google Drive (créé au First-Run)
        │   ├── 01_profil_et_contacts_cles.md      # Informations clés à jour, contacts fréquents & style relationnel
        │   ├── 02_assistante_maternelle.md        # Synthèse à jour du contrat Nounou, horaires, congés + file_id Drive
        │   └── 03_dossiers_actifs_et_administratif.md # Synthèse à jour des dossiers récurrents et infos importantes
        ├── cache/drive_metadata_index.json        # Index local rapide de toute l'arborescence Google Drive
        └── state/                                 # Les 5 fichiers d'état JSON/JSONL atomiques
```

---

## 2. Découpage Détaillé des 5 Work Packages (`WP0` à `WP4`)

### `WP0` — Garde-Fous Pré-Commit, Hygiène Dual-Git & Squelette Clean Architecture
- **Livrables** :
  - `check_code_architecture.py` ($\le 500\text{L}$/fichier, $\le 60\text{L}$/méthode hors docstring, 100 % docstrings Google, matrice AST 4 couches) et `check-upstream-leak.sh`.
  - Nettoyage de l'historique Git de `/root/Alfred/.git` et initialisation de `/root/Alfred/instance/.git` (avec `data/` et `secrets/` exclus par `.gitignore`).

---

### `WP1` — Fiches de Référence à Frontmatter YAML (`knowledge_service.py`), Scaffolding Exhaustif du Drive au First-Run (`init-drive`) & Connecteurs Google API
- **Livrables** :
  1. **`knowledge_service.py` (Chargement Progressif en 3 Étages par Frontmatter YAML)** :
     - Parse uniquement les Frontmatters YAML (`id`, `title`, `domain`, `summary`, `keywords`, `drive_source_ids`, `updated_at`) de tous les fichiers `.md` de `instance/data/knowledge/` (~50 tokens) pour sélectionner la fiche de référence *up-to-date* pertinente avant d'en charger le corps ($\le 150\text{L}$).
     - Sauvegarde automatiquement toute mise à jour d'une fiche de référence `.md` vers le dossier `_ALFRED_REFERENTIEL_` sur Google Drive (en conservant le fichier `.md` et son Frontmatter YAML intacts, 0 conversion complexe).
  2. **`drive_service.py` & Commande First-Run `init-drive` (Scaffolding Exhaustif du Drive)** :
     - **Au First-Run (`init-drive`)** : Scanne l'exhaustivité des dossiers et fichiers de ton Google Drive, construit `drive_metadata_index.json`, analyse l'organisation existante et génère un **`implementation_plan.md`** proposant la fiche maîtresse **`00_drive_taxonomy_and_filing_rules.md`** (qui définit où sont tes données, comment elles sont rangées, et quelles infos clés doivent être maintenues *up-to-date* dans `knowledge/*.md`).
     - **Au quotidien** : Maintient `drive_metadata_index.json` à jour par delta via `drive.changes().list` (0 token) et lit les fichiers bruts sur Drive (`files.export`, `spreadsheets.values.get`, `pypdf` ou Vision `3.8 Flash` sur scans).
  3. **`gmail_service.py`, `calendar_service.py` et `agy_session_manager.py`** (avec auto-archivage des conversations `SILENT_NO_ACTION`).

---

### `WP2` — Cas d'Usage 1 : `alfred-inbox` (Routine Nouveaux Messages à 3 Sorties Exclusives)
- **Livrables** :
  - Prise de la boîte Gmail à l'état actuel (`historyId` courant) sans toucher au passé.
  - Traitement de chaque **nouveau message entrant** selon les 3 sorties :
    1. **`SILENT_NO_ACTION`** : Bruit et notifications passives de portails (*« Nouveaux documents réglementaires sur votre espace Generali »*, factures d'abonnement courantes sans anomalie) $\rightarrow$ 0 action Gmail + **auto-archivage immédiat de la conversation Antigravity**.
    2. **`DRAFT_REPLY`** : Crée le brouillon dans Gmail, garde la conversation Antigravity Web **allumée**, et écrit dans **`implementation_plan.md`** le contenu du brouillon pour correction via commentaires inline.
    3. **`USER_ACTION_REQUIRED`** : **Starre automatiquement (`STARRED`)** l'e-mail dans Gmail, garde la conversation Antigravity Web **allumée**, et réalise un **défrichage complet** (consultation des fiches de référence `.md`, recherche dans Drive, mails et Internet) restitué dans **`implementation_plan.md`** (avec proposition de mettre à jour une fiche de référence `.md` si une nouvelle info clé est apparue).

---

### `WP3` — Cas d'Usage 2 : `alfred-calendar` (Assistante Maternelle & Calendrier `Garde Nounou`)
- **Livrables** :
  - **Initialisation (`init-nounou`)** : Analyse du **contrat signé** $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ création du calendrier Google **`Garde Nounou`**, de `nounou_contract.json` et de la fiche de référence *up-to-date* **`02_assistante_maternelle.md`** (avec Frontmatter YAML).
  - **Routine du 20 du mois $M$ (`seed-nounou`)** : Pré-remplissage du mois $M+1$ aux horaires exacts du contrat.
  - **Vacances & Préavis (~1 mois)** : Détection des vacances et alerte de préavis via `implementation_plan.md`.
  - **Bilan Pajemploi à `last_day - 3` (`close-pajemploi`, février inclus)** : Calcul déterministe des heures et indemnités du mois présenté dans `implementation_plan.md`.

---

### `WP4` — Cas d'Usage 3 : Workspaces `alfred-life` & `alfred-evolve` (Recherche Adaptative & Génération de PR sur les 2 Dépôts Git)
- **Livrables** :
  - Manifests `.agents/agents/` et `workspaces/{alfred-life,alfred-inbox,alfred-calendar,alfred-evolve}/AGENTS.md` implémentant le **Pattern 3 Étapes avec Recherche Adaptative** (0 sous-agent si question directe, 1 sous-agent sur les Frontmatters YAML / Fiches de Référence pour une vérif rapide, ou *spread* multi-agents complet pour une recherche approfondie $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ Agent Exécuteur Unique).
  - **`evolve_worktree_service.py`** : Une fois le plan d'évolution validé dans `alfred-evolve`, l'agent exécuteur développe et valide l'évolution en TDD dans un `git worktree` isolé, met à jour les fiches de référence si nécessaire, et **génère automatiquement les Pull Requests (PR) sur les 2 dépôts Git** :
    1. Le dépôt générique (`/root/Alfred/.git` $\rightarrow$ `alfred-core`, après validation par `check-upstream-leak.sh`),
    2. Le dépôt personnel (`/root/Alfred/instance/.git` $\rightarrow$ `alfred-instance`).
