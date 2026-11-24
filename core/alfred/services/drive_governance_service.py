"""Non-destructive Google Drive governance service with protected ID and anti-loop guards."""

from __future__ import annotations

import contextlib
import hashlib
from typing import Any

from alfred.domain.drive_watcher_models import (
    DriveGovernanceActionType,
    DriveGovernanceOp,
    DriveMutationResult,
    ProcessedDriveEvent,
)
from alfred.domain.models import (
    AlfredSettings,
    DriveEntry,
    DriveMetadataIndex,
    ProcessedItemEvent,
    utc_now_iso,
)
from alfred.repositories.state_store import StateStore
from alfred.services.drive_service import FOLDER_MIME, DriveService

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
MUTATION_FIELDS = "id,name,mimeType,parents,modifiedTime,md5Checksum,headRevisionId,trashed"


def _compute_fingerprint(raw: dict[str, Any]) -> str:
    """Build revision:md5:modifiedTime fingerprint from raw Google Drive file metadata."""
    rev = str(raw.get("headRevisionId", ""))
    md5 = str(raw.get("md5Checksum", ""))
    mtime = str(raw.get("modifiedTime", ""))
    return f"{rev}:{md5}:{mtime}"


class DriveGovernanceService:
    """Execute strictly non-destructive Drive filing mutations with ledger & audit tracking."""

    def __init__(
        self,
        settings: AlfredSettings,
        state_store: StateStore,
        drive_service: DriveService,
    ) -> None:
        """Initialize the non-destructive Google Drive governance service.

        Args:
            settings: Runtime structural settings including security.protected_signature_drive_ids.
            state_store: Atomic local state store for index, watcher ledger, and audit JSONL.
            drive_service: Underlying DriveService providing Google Drive v3 API access.
        """
        self._settings = settings
        self._state_store = state_store
        self._drive = drive_service

    def resolve_referentiel_folder_id(self) -> str:
        """Resolve the Drive folder ID of _ALFRED_REFERENTIEL_ from bookmarks or local index.

        Returns:
            Google Drive folder ID string, or empty string when not yet created.
        """
        bm_id = (self._state_store.bookmarks.load().referentiel_folder_id or "").strip()
        if bm_id:
            return bm_id
        ref_name = self._settings.referentiel_drive_folder_name
        for entry in self._state_store.drive_index.load().entries.values():
            if entry.is_folder and entry.name == ref_name and not entry.parents:
                return entry.file_id
        return ""

    def validate_op_ancestors(self, op: DriveGovernanceOp) -> list[str]:
        """Check ancestor chains for all IDs referenced by op without mutating Drive.

        Args:
            op: Proposed DriveGovernanceOp to inspect.

        Returns:
            List of violation strings when any referenced ID or ancestor is restricted.
        """
        errs: list[str] = []
        client = self._drive.get_raw_client()
        for cid in (op.file_id, op.target_parent_id, op.shortcut_target_id):
            if not cid:
                continue
            try:
                self._assert_governance_allowed(client, cid)
            except PermissionError as exc:
                errs.append(str(exc))
        if op.new_name.strip() == self._settings.referentiel_drive_folder_name:
            errs.append("Renaming or creating a folder named _ALFRED_REFERENTIEL_ is forbidden.")
        return errs

    def move_file(self, file_id: str, target_parent_id: str) -> DriveMutationResult:
        """Move a Drive file or folder into target_parent_id without deleting content.

        Args:
            file_id: Google Drive identifier of the item to move.
            target_parent_id: Destination Google Drive folder identifier.

        Returns:
            DriveMutationResult with updated parent_id, path, and version_fingerprint.

        Raises:
            PermissionError: If file_id or target_parent_id touches protected or referentiel trees.
        """
        client = self._drive.get_raw_client()
        self._assert_governance_allowed(client, file_id)
        self._assert_governance_allowed(client, target_parent_id)
        current = client.files().get(fileId=file_id, fields=MUTATION_FIELDS).execute()
        remove_parents = ",".join(str(p) for p in current.get("parents", []) if p)
        updated = (
            client.files()
            .update(
                fileId=file_id,
                addParents=target_parent_id,
                removeParents=remove_parents,
                fields=MUTATION_FIELDS,
            )
            .execute()
        )
        return self._finalize_mutation(
            DriveGovernanceActionType.MOVE, updated, fallback_parent=target_parent_id
        )

    def rename_file(self, file_id: str, new_name: str) -> DriveMutationResult:
        """Rename a Drive file or folder in place without altering its content or ID.

        Args:
            file_id: Google Drive identifier of the item to rename.
            new_name: New UTF-8 file or folder name.

        Returns:
            DriveMutationResult reflecting the updated name and version_fingerprint.

        Raises:
            PermissionError: If file_id touches protected signature IDs or _ALFRED_REFERENTIEL_.
            ValueError: If new_name is empty.
        """
        cleaned = new_name.strip()
        if not cleaned:
            raise ValueError("new_name cannot be empty.")
        if cleaned == self._settings.referentiel_drive_folder_name:
            raise PermissionError("Cannot rename an item to _ALFRED_REFERENTIEL_.")
        client = self._drive.get_raw_client()
        self._assert_governance_allowed(client, file_id)
        updated = (
            client.files()
            .update(fileId=file_id, body={"name": cleaned}, fields=MUTATION_FIELDS)
            .execute()
        )
        return self._finalize_mutation(DriveGovernanceActionType.RENAME, updated)

    def create_folder(self, name: str, parent_id: str = "") -> DriveMutationResult:
        """Create a new thematic folder on Google Drive.

        Args:
            name: Folder display name.
            parent_id: Optional parent Google Drive folder identifier.

        Returns:
            DriveMutationResult describing the newly created folder.

        Raises:
            PermissionError: If parent_id is inside protected signature or referentiel trees.
            ValueError: If name is empty.
        """
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Folder name cannot be empty.")
        if cleaned == self._settings.referentiel_drive_folder_name:
            raise PermissionError("Direct creation of _ALFRED_REFERENTIEL_ is reserved.")
        client = self._drive.get_raw_client()
        if parent_id:
            self._assert_governance_allowed(client, parent_id)
        body: dict[str, Any] = {"name": cleaned, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        created = client.files().create(body=body, fields=MUTATION_FIELDS).execute()
        return self._finalize_mutation(
            DriveGovernanceActionType.CREATE_FOLDER, created, fallback_parent=parent_id
        )

    def create_shortcut(
        self,
        target_file_id: str,
        parent_id: str,
        name: str = "",
    ) -> DriveMutationResult:
        """Create a non-duplicating Google Drive shortcut pointing to target_file_id.

        Args:
            target_file_id: Target Google Drive file identifier referenced by the shortcut.
            parent_id: Destination Google Drive folder identifier for the shortcut.
            name: Optional shortcut display name (defaults to target file's name).

        Returns:
            DriveMutationResult with mime_type 'application/vnd.google-apps.shortcut'.

        Raises:
            PermissionError: If target_file_id or parent_id touches protected or referentiel trees.
        """
        client = self._drive.get_raw_client()
        self._assert_governance_allowed(client, target_file_id)
        self._assert_governance_allowed(client, parent_id)
        resolved_name = name.strip()
        if not resolved_name:
            target_meta = client.files().get(fileId=target_file_id, fields="id,name").execute()
            resolved_name = str(target_meta.get("name") or target_file_id)
        body: dict[str, Any] = {
            "name": resolved_name,
            "mimeType": SHORTCUT_MIME,
            "parents": [parent_id],
            "shortcutDetails": {"targetId": target_file_id},
        }
        created = client.files().create(body=body, fields=MUTATION_FIELDS).execute()
        return self._finalize_mutation(
            DriveGovernanceActionType.CREATE_SHORTCUT,
            created,
            fallback_parent=parent_id,
            shortcut_target_id=target_file_id,
        )

    def record_self_mutation(
        self,
        file_id: str,
        raw_meta: dict[str, Any] | None = None,
        action: str = "SELF_MUTATION",
    ) -> str:
        """Record an Alfred-originated Drive write in drive_watcher_ledger to prevent loops.

        Args:
            file_id: Mutated or backed-up Google Drive file identifier.
            raw_meta: Optional Drive metadata dict returned by files().create/update.
            action: Governance action label recorded in ledger and audit log.

        Returns:
            Recorded version_fingerprint string.
        """
        meta = dict(raw_meta) if raw_meta is not None else {}
        if not meta and file_id:
            with contextlib.suppress(Exception):
                client = self._drive.get_raw_client()
                meta = dict(
                    client.files().get(fileId=file_id, fields=MUTATION_FIELDS).execute() or {}
                )
        fp = _compute_fingerprint(meta)
        parents = [str(p) for p in meta.get("parents", []) if p]

        def _update_ledger(cur: Any) -> Any:
            """Insert or refresh self-mutated Drive file fingerprint in ledger."""
            prev = cur.events.get(file_id)
            history = list(prev.recent_fingerprints) if prev is not None else []
            if prev is not None and prev.version_fingerprint and prev.version_fingerprint != fp:
                history.append(prev.version_fingerprint)
            ev = ProcessedDriveEvent(
                file_id=file_id,
                version_fingerprint=fp,
                recent_fingerprints=history[-20:],
                parent_ids=parents,
                origin="alfred_self_mutation",
                outcome=action,
            )
            return cur.model_copy(update={"events": {**cur.events, file_id: ev}})

        self._state_store.drive_watcher_ledger.update(_update_ledger)
        digest = hashlib.sha256(f"{file_id}:{fp}:{action}".encode()).hexdigest()
        self._state_store.drive_governance_audit.append(
            ProcessedItemEvent(
                item_type="drive_governance",
                item_id=file_id,
                sha256=digest,
                outcome=action,
                details=f"parents={parents} fp={fp}",
            )
        )
        return fp

    def _finalize_mutation(
        self,
        action: DriveGovernanceActionType,
        raw: dict[str, Any],
        fallback_parent: str = "",
        shortcut_target_id: str = "",
    ) -> DriveMutationResult:
        """Update local DriveMetadataIndex, record self-mutation ledger, and build result."""
        fid = str(raw["id"])
        name = str(raw.get("name", ""))
        mime = str(raw.get("mimeType", ""))
        parents = [str(p) for p in raw.get("parents", []) if p]
        if not parents and fallback_parent:
            parents = [fallback_parent]
        path = self._upsert_index_entry(fid, name, mime, parents, str(raw.get("modifiedTime", "")))
        fp = self.record_self_mutation(fid, raw_meta=raw, action=action.value)
        return DriveMutationResult(
            action=action,
            file_id=fid,
            name=name,
            mime_type=mime,
            parent_id=parents[0] if parents else "",
            path=path,
            version_fingerprint=fp,
            shortcut_target_id=shortcut_target_id,
        )

    def _upsert_index_entry(
        self, fid: str, name: str, mime: str, parents: list[str], modified_time: str
    ) -> str:
        """Insert or update a single DriveEntry in drive_metadata_index.json and resolve paths."""
        index = self._state_store.drive_index.load()
        entries = dict(index.entries)
        entries[fid] = DriveEntry(
            file_id=fid,
            name=name,
            mime_type=mime,
            parents=parents,
            modified_time=modified_time,
            is_folder=(mime == FOLDER_MIME),
            trashed=False,
        )
        self._populate_paths(entries)
        folders = [e for e in entries.values() if e.is_folder]
        files = [e for e in entries.values() if not e.is_folder]
        top_folders = sorted(
            {
                e.path.strip("/").split("/")[0]
                for e in folders
                if e.path and len(e.path.strip("/").split("/")) == 1
            }
        )
        self._state_store.drive_index.save(
            DriveMetadataIndex(
                start_page_token=index.start_page_token,
                last_synced_at=utc_now_iso(),
                folder_count=len(folders),
                file_count=len(files),
                top_level_folders=top_folders,
                entries=entries,
            )
        )
        return entries[fid].path

    def _populate_paths(self, entries: dict[str, DriveEntry]) -> None:
        """Recompute hierarchical '/Folder/Subfolder/Item' paths across entries."""
        for eid, entry in list(entries.items()):
            segments = [entry.name]
            seen = {eid}
            pid = entry.parents[0] if entry.parents else ""
            while pid and pid in entries and pid not in seen:
                seen.add(pid)
                parent = entries[pid]
                segments.append(parent.name)
                pid = parent.parents[0] if parent.parents else ""
            segments.reverse()
            entries[eid] = entry.model_copy(update={"path": "/" + "/".join(segments)})

    def _assert_governance_allowed(self, client: Any, item_id: str) -> None:
        """Raise PermissionError if item_id or any of its ancestors is protected or referentiel."""
        protected = set(self._settings.security.protected_signature_drive_ids)
        ref_id = self.resolve_referentiel_folder_id()
        ref_name = self._settings.referentiel_drive_folder_name
        entries = self._state_store.drive_index.load().entries

        chain, names = self._collect_ancestor_chain(client, item_id, entries)
        hit = chain & protected
        if hit:
            raise PermissionError(
                f"Governance denied: '{item_id}' touches protected Drive ID {sorted(hit)}."
            )
        if (ref_id and ref_id in chain) or ref_name in names:
            raise PermissionError(
                f"Governance denied: '{item_id}' touches reserved folder '{ref_name}'."
            )

    def _collect_ancestor_chain(
        self, client: Any, item_id: str, entries: dict[str, DriveEntry]
    ) -> tuple[set[str], set[str]]:
        """Walk parents from local index or Drive API to collect ancestor IDs and names."""
        ids: set[str] = set()
        names: set[str] = set()
        queue = [item_id]
        while queue:
            cur_id = queue.pop()
            if not cur_id or cur_id in ids:
                continue
            ids.add(cur_id)
            if cur_id in entries:
                names.add(entries[cur_id].name)
                queue.extend(p for p in entries[cur_id].parents if p not in ids)
                continue
            with contextlib.suppress(Exception):
                meta = client.files().get(fileId=cur_id, fields="id,name,parents").execute()
                if isinstance(meta, dict):
                    if meta.get("name"):
                        names.add(str(meta["name"]))
                    queue.extend(str(p) for p in meta.get("parents", []) if str(p) not in ids)
        return ids, names
