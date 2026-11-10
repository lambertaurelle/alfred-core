# 📬 Workspace `alfred-inbox` — Routine Nouveaux E-mails à 3 Sorties Exclusives

> **Mode d'Ouverture** : **Agent-Spawned Only** (déclenché par `alfred poll-inbox` à partir du `historyId` courant, sans toucher au passé)  
> **Pipeline Obligatoire** : **Pattern Universel en 3 Étapes + 3 Sorties Exclusives**

## 1. Les 3 Sorties Exclusives (`InboxTriageService`)
1. **`SILENT_NO_ACTION`** :
   - Bruit, newsletters et notifications passives de portails (*« Vous avez de nouveaux documents sur votre espace Generali »*, factures d'abonnement courantes sans anomalie).
   - **Action** : **0 modification dans Gmail** + **auto-archivage immédiat de la conversation Antigravity** (`AgySessionManager.auto_archive_silent_session`).
2. **`DRAFT_REPLY` (Brouillon de réponse)** :
   - **Étape 1 (`Read-Only`)** : Consultation progressive des Frontmatters YAML (`01_profil_et_contacts_cles.md` et fiche métier pertinente).
   - **Étape 2 (Gate `implementation_plan.md`)** : Création du brouillon Gmail (`users.drafts.create`), maintien de la conversation Antigravity Web **active**, et affichage intégral du brouillon dans `implementation_plan.md` (`RequestFeedback: true`) pour correction par commentaires inline.
   - **Étape 3 (`plan-executor`)** : Après `Proceed`, mise à jour (`update_draft`) et/ou envoi (`send_draft`) selon la consigne validée.
3. **`USER_ACTION_REQUIRED` (Action utilisateur requise)** :
   - **Étape 1 (`Read-Only` Spread)** : Marquage automatique **`STARRED`** dans Gmail + défrichage parallèle (`research-drive`, `research-gmail-cal`, `research-web`).
   - **Étape 2 (Gate `implementation_plan.md`)** : Conversation Antigravity Web maintenue **active**, restitution du défrichage complet et proposition éventuelle de mise à jour d'une fiche `instance/data/knowledge/*.md`.
   - **Étape 3 (`plan-executor`)** : Mise à jour de la fiche de référence `.md` validée.
