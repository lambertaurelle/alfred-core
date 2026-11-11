# 📬 Workspace `alfred-inbox` — Routine Nouveaux E-mails à 3 Sorties Exclusives

> **Mode d'Ouverture** : **Agent-Spawned Only** (déclenché par `alfred poll-inbox` à partir du `historyId` courant, sans toucher au passé)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **Pattern Universel en 3 Étapes + 3 Sorties Exclusives + Pont `alfred-calendar`**

## 1. Les 3 Sorties Exclusives (`InboxTriageService`)
1. **`SILENT_NO_ACTION`** :
   - Bruit, newsletters et notifications passives de portails (*« Vous avez de nouveaux documents sur votre espace Generali »*, factures d'abonnement courantes sans anomalie).
   - **Action** : **0 modification dans Gmail** + **création et auto-archivage natif immédiat d'une trace Antigravity dans `alfred-inbox`** (`annotations/<conv_id>.pbtxt` avec `archived:true` et `title:"[SILENT] <Sujet>"`, consultable dans Antigravity Web via **`Archived Only`** sans polluer la vue active).
   - **Exception Agenda (`CONFIRM_EVENT`)** : si un e-mail valide un créneau/événement sans nécessiter de réponse mail, `InboxTriageService` ouvre en parallèle une **Gate bloquante obligatoire dans `alfred-calendar`** pour valider la conversion du `[HOLD]` et l'envoi de l'invitation Google Calendar (`confirm-event --send-invites`).
2. **`DRAFT_REPLY` (Brouillon de réponse + `[HOLD]` éventuel)** :
   - **Étape 1 (`Read-Only`)** : Consultation progressive des Frontmatters YAML (`01_profil_et_contacts_cles.md`, fiche métier pertinente) et de l'agenda principal sur 14 jours.
   - **Étape 2 (Gate `implementation_plan.md`)** : Création du brouillon Gmail (`users.drafts.create`), maintien de la conversation Antigravity Web **active**, et affichage intégral du brouillon dans `implementation_plan.md` (`RequestFeedback: true`). Si le brouillon propose ou valide un événement, inclut le bloc `Coordination Agenda (alfred-calendar)` distinguant invités allowlistés immédiats et invités externes différés.
   - **Étape 3 (`plan-executor`)** : Après `Proceed`, mise à jour/envoi du brouillon (`send-draft`) et exécution éventuelle de `create-event --hold --archive-trace` ou `confirm-event --send-invites --archive-trace`.
3. **`USER_ACTION_REQUIRED` (Action utilisateur requise)** :
   - **Étape 1 (`Read-Only` Spread)** : Marquage automatique **`STARRED`** dans Gmail + défrichage parallèle (`research-drive`, `research-gmail-cal`, `research-web`).
   - **Étape 2 (Gate `implementation_plan.md`)** : Conversation Antigravity Web maintenue **active**, restitution du défrichage complet et proposition éventuelle de mise à jour d'une fiche `instance/data/knowledge/*.md`.
   - **Étape 3 (`plan-executor`)** : Mise à jour de la fiche de référence `.md` validée.
