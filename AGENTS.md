# 🎩 AGENTS.md — Guide Suprême d'Orchestration `/root/Alfred` (V7.1 Lean)
## Directives Universelles, Pattern 3 Étapes Adaptatif & Chargement Progressif 3-Tiers (Frontmatter YAML + Google Drive)

> **Périmètre** : Racine du projet `/root/Alfred` (`ALFRED_ROOT_DIR`)  
> **Interface Unique** : **100 % Antigravity Web UI** (mono-utilisateur, zéro Google Chat, zéro WebApp externe)  
> **Architecture de Connaissance en 3 Étages (Progressive Disclosure)** :
> 1. **Étage 1 — Frontmatters YAML des Fichiers de Référence (`instance/data/knowledge/*.md`)** : Lecture à ~50 tokens des headers YAML pour identifier instantanément quelle fiche de synthèse *up-to-date* ou quelle règle de rangement Drive charger.
> 2. **Étage 2 — Corps des Fichiers de Référence `.md` (*Up-to-Date*) + `drive_metadata_index.json`** : Chargement ciblé de la fiche de synthèse ($\le 150\text{L}$) qui regroupe les informations à jour et pointe vers les `file_id` des documents bruts sur Google Drive.
> 3. **Étage 3 — Documents Bruts sur Google Drive (`drive_read`)** : Ouverture à la demande des PDF/Docs/Sheets originaux sur Google Drive uniquement lorsqu'un détail de pièce justificative est nécessaire.  
> **Standards de Développement** : TDD Strict (`>= 90%`), Clean Architecture OOP (`<= 500L/fichier`, `<= 60L/méthode` hors docstring) & Zéro Donnée Personnelle dans Git.

---

## 1. Le Pattern Universel en 3 Étapes (Avec Recherche Adaptative & Gate Systématique)

Tous les agents d'Alfred (`alfred-inbox`, `alfred-calendar`, `alfred-life`, `alfred-evolve`) fonctionnent **systématiquement** selon ce pipeline en 3 étapes :

1. **Étape 1 — Analyse & Recherche Adaptative en Mode Turbo (`Read-Only`)** :
   - **L'ampleur de la recherche s'adapte à la question posée** :
     - *Aucune recherche (0 sous-agent)* si la demande est auto-suffisante $\rightarrow$ passage direct à l'Étape 2.
     - *Recherche ciblée (1 sous-agent)* s'il suffit de vérifier un fichier de référence ou un événement calendrier.
     - *Recherche « Spread » approfondie (plusieurs sous-agents en parallèle : `research-drive`, `research-gmail-cal`, `research-web`)* en mode Turbo sans confirmation pour fouiller les fichiers de référence, Google Drive, Gmail, Calendar et Internet.
2. **Étape 2 — Synthèse & Gate Systématique (`implementation_plan.md`)** :
   - L'agent synthétiseur consolide l'analyse et génère l'artefact **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`).
   - **Ce plan d'implémentation est une Gate bloquante obligatoire** avant toute exécution : l'utilisateur peut l'annoter avec des commentaires inline dans Antigravity Web, puis cliquer sur **`Proceed`**.
3. **Étape 3 — Exécution Post-Validation par un Agent Exécuteur Unique** :
   - Une fois le plan validé par l'utilisateur, un dernier agent exécuteur applique les modifications :
     - Mise à jour / envoi de brouillon Gmail ou marquage `STARRED`,
     - Mise à jour du calendrier Google (`Garde Nounou`, agenda principal),
     - Mise à jour des **Fichiers de Référence *Up-to-Date* (`.md` avec Frontmatter YAML)** et sauvegarde sur Google Drive,
     - Pour **`alfred-evolve`** : développement en TDD dans un `git worktree` isolé, puis **génération automatique des Pull Requests (PR) sur les 2 dépôts Git** (le dépôt générique `alfred-core` et le dépôt personnel `alfred-instance`).

---

## 2. Les 4 Cas d'Usage Cœurs de la V7.1 Lean

### 2.1. Scaffolding Initial de Google Drive au First-Run (`init-drive`) & Fichiers de Référence *Up-to-Date*
- **Analyse Exhaustive du Drive au First-Run** : Lors de l'initialisation du projet, Alfred scanne en lecture seule **l'exhaustivité de l'arborescence Google Drive** pour comprendre comment les données sont organisées et rangées par l'utilisateur.
- **Validation des Règles de Rangement** : Il soumet dans un `implementation_plan.md` la cartographie et les règles de rangement détectées (`00_drive_taxonomy_and_filing_rules.md`), et construit le `drive_metadata_index.json`.
- **Maintien des Fichiers de Référence *Up-to-Date* (`instance/data/knowledge/*.md`)** :
  - Chaque fichier de référence commence par un **Frontmatter YAML obligatoire** (`id`, `title`, `domain`, `summary`, `keywords`, `drive_source_ids`, `updated_at`) suivi d'une synthèse toujours à jour (avec liens vers les `file_id` Drive sources).
  - Ces fichiers `.md` sont stockés localement pour un scan instantané des frontmatters et sauvegardés automatiquement sur Google Drive (`_ALFRED_REFERENTIEL_`).

### 2.2. `alfred-inbox` — Routine sur les Nouveaux E-mails à 3 Sorties Exclusives
La boîte mail est prise telle quelle au démarrage (`historyId` courant). Chaque **nouveau message entrant** produit l'une des **3 sorties suivantes** :
1. **`SILENT_NO_ACTION`** :
   - Bruit, newsletters, et notifications passives de portails (*« Vous avez de nouveaux documents sur votre espace Generali »*, factures d'abonnement sans anomalie).
   - **Action** : 0 modification dans Gmail + **auto-archivage immédiat de la conversation Antigravity**.
2. **`DRAFT_REPLY` (Brouillon de réponse à écrire)** :
   - **Action** : Création du brouillon dans Gmail, maintien de la conversation Antigravity Web **allumée**, et écriture du brouillon dans **`implementation_plan.md`** pour correction par commentaires inline avant validation.
3. **`USER_ACTION_REQUIRED` (Action à réaliser par l'utilisateur)** :
   - **Action** : L'e-mail est automatiquement **étoilé (`STARRED`)** dans Gmail, la conversation Antigravity Web reste **allumée**, et les sous-agents réalisent un **défrichage complet** (recherche doc Internet, pièces Drive, contexte mails) restitué dans **`implementation_plan.md`**.

### 2.3. `alfred-calendar` — Assistante Maternelle & Calendrier `Garde Nounou`
- Initialisation à partir du **contrat signé** (création du calendrier Google `Garde Nounou` + fiche de référence `02_assistante_maternelle_contrat.md` avec Frontmatter YAML et `nounou_contract.json`).
- **Le 20 du mois $M$** : pré-remplissage du mois $M+1$ avec les horaires exacts du contrat.
- **Vacances & Préavis** : détection des vacances et alerte de préavis (~1 mois avant selon contrat) via `implementation_plan.md`.
- **À J-3 / J-4 de la fin du mois (`last_day - 3`, février inclus)** : calcul déterministe du bilan Pajemploi présenté dans `implementation_plan.md`.

### 2.4. `alfred-life` & `alfred-evolve` (Avec Génération de PR sur les 2 Dépôts Git)
- **`alfred-life`** : Point d'entrée libre fonctionnant selon le Pattern 3 Étapes (Recherche adaptative $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ Exécuteur).
- **`alfred-evolve`** : Compréhension du besoin et captation de signaux $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ Exécution TDD dans un `git worktree` isolé et **création des Pull Requests (PR) sur les 2 dépôts Git (`alfred-core` générique et `alfred-instance` personnel)**.
