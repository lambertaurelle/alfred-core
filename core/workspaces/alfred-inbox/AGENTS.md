# 📬 Workspace `alfred-inbox` — Triage Sécurisé des Nouveaux E-mails à 3 Sorties Exclusives

> **Routage Modèle (Muscle)** : `gemini-4-hydrogen-medium` (Agent-Spawned via `alfred poll-inbox`, traces archivées en `gemini-4-hydrogen-low`)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **Pré-filtre 0-Token & Pare-Feu Injection/SPF-DKIM $\rightarrow$ 3 Sorties Exclusives $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ `plan-executor` (`Hydrogen`)**

## 1. Pré-Filtrage Déterministe & Pare-Feu de Sécurité
- Pré-filtrage à 0 token des invitations `.ics` automatiques, promotions/forums et expéditeurs denylistés avant tout appel cognitif — tout en laissant passer les **confirmations datées de billets, réservations, hôtels, transports ou rendez-vous** (`has_dated_booking_signal`) pour inscription sous Gate à l'agenda (sans limite d'horizon).
- Pare-feu anti-injection de prompt sur `subject`/`body`/`HTML` et sur le texte/HTML des pièces jointes (`EmailSanitizer`) ; vérification `Authentication-Results` (SPF/DKIM) interdisant à tout e-mail non authentifié de déclencher `alfred-evolve`.
- Exploration sécurisée en mémoire des pièces jointes Gmail (`list-attachments`, `read-attachment`) avec neutralisation des traversées de chemin et des archives ZIP/XML malveillantes.

## 2. Les 3 Sorties Exclusives (`InboxTriageService`)
1. **`SILENT_NO_ACTION`** :
   - Bruit, newsletters et notifications passives sans anomalie $\rightarrow$ **0 modification dans Gmail** (et trace optionnelle auto-archivée selon `silent_creates_agy_session`).
   - **Coordination Agenda (`CONFIRM_EVENT`)** : si un e-mail valide un créneau ou confirme un billet/réservation datée sans nécessiter de réponse écrite, ouverture d'une Gate dans `alfred-calendar` (`confirm-event --location ... --description ... --no-send-invites --archive-trace` avec `attendees: []`, `send_invites: false` pour les marchands).
2. **`DRAFT_REPLY` (Brouillon Gmail + `[HOLD]` éventuel)** :
   - Création du brouillon Gmail (avec en-tête `From:` `default_send_as_email` si configuré), maintien de la session active et présentation dans **`implementation_plan.md`** (`RequestFeedback: true`).
   - Après `Proceed`, **`plan-executor`** (`Hydrogen`) envoie le brouillon (`send-draft`, archivage Gmail par défaut sauf `--keep-in-inbox`) et applique l'action calendrier associée (surfaçage automatique dans `alfred-calendar`).
3. **`USER_ACTION_REQUIRED` (Action utilisateur + escalade éventuelle `study-orchestrator`)** :
   - Marquage automatique **`STARRED`** dans Gmail, défrichage complet (`research-drive`, `research-gmail-cal`, `research-web`, ou `study-orchestrator` si `needs_orchestrator_study=true`) incluant l'analyse des pièces jointes et la section Coordination Agenda éventuelle dans **`implementation_plan.md`**.
   - Après `Proceed`, **`plan-executor`** transfère au besoin les pièces jointes validées (`save-attachment-to-drive --message-id <id> --parent-id <folder_id>`), exécute les actions Tasks/Contacts (`create-task`, `complete-task`, `update-task`, `delete-task`, `create-contact`) ou Calendar (`confirm-event` / `create-event`) avec surfaçage automatique inter-workspace, met à jour le référentiel `.md` et archive le fil (`archive-email --thread-id ... --unstar`, sauf `--keep-in-inbox`).
