"""Speed-1 zero-token Google Drive delta index updater and eligibility filter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from alfred.domain.drive_watcher_models import (
    DriveChangeKind,
    DriveDeltaItem,
    DriveWatcherConfig,
    DriveWatcherLedger,
)
from alfred.domain.models import DriveEntry
from alfred.services.drive_governance_service import SHORTCUT_MIME
from alfred.services.drive_service import FOLDER_MIME


def populate_drive_paths(entries: dict[str, DriveEntry]) -> None:
    """Resolve hierarchical '/Folder/Subfolder/File' paths in-place across entries.

    Args:
        entries: Mutable map of file_id to DriveEntry records.
    """
    for fid, entry in list(entries.items()):
        segments = [entry.name]
        seen = {fid}
        pid = entry.parents[0] if entry.parents else ""
        while pid and pid in entries and pid not in seen:
            seen.add(pid)
            parent = entries[pid]
            segments.append(parent.name)
            pid = parent.parents[0] if parent.parents else ""
        segments.reverse()
        entries[fid] = entry.model_copy(update={"path": "/" + "/".join(segments)})


def apply_coalesced_changes_to_entries(
    prev_entries: Mapping[str, DriveEntry],
    coalesced: Mapping[str, dict[str, Any]],
) -> dict[str, DriveEntry]:
    """Update local DriveMetadataIndex entries for owned or mounted non-trashed items.

    Args:
        prev_entries: Previous snapshot of indexed DriveEntry records.
        coalesced: Latest Drive change dictionary per fileId.

    Returns:
        Updated dictionary of file_id to path-resolved DriveEntry records.
    """
    entries = dict(prev_entries)
    batch_owned_ids = {
        fid
        for fid, ch in coalesced.items()
        if not ch.get("removed")
        and not (ch.get("file") or {}).get("trashed")
        and bool((ch.get("file") or {}).get("ownedByMe", True))
    }
    known_ids = set(entries.keys()) | batch_owned_ids
    for fid, change in coalesced.items():
        file_obj = change.get("file") or {}
        if change.get("removed") or file_obj.get("trashed"):
            entries.pop(fid, None)
            continue
        if not file_obj:
            continue
        owned = bool(file_obj.get("ownedByMe", True))
        parents = [str(p) for p in file_obj.get("parents", []) if p]
        if not owned and not any(p in known_ids for p in parents):
            continue
        mime = str(file_obj.get("mimeType", ""))
        entries[fid] = DriveEntry(
            file_id=fid,
            name=str(file_obj.get("name", "")),
            mime_type=mime,
            parents=parents,
            modified_time=str(file_obj.get("modifiedTime", "")),
            is_folder=(mime == FOLDER_MIME),
            trashed=False,
        )
    populate_drive_paths(entries)
    return entries


def filter_speed1_delta_items(
    coalesced: Mapping[str, dict[str, Any]],
    prev_entries: Mapping[str, DriveEntry],
    entries: Mapping[str, DriveEntry],
    ledger: DriveWatcherLedger,
    watcher_cfg: DriveWatcherConfig,
    protected_ids: set[str],
    ref_id: str,
    ref_name: str,
) -> tuple[list[DriveDeltaItem], dict[str, str]]:
    """Evaluate all 0-token Speed-1 guards without reading any Drive file bytes.

    Args:
        coalesced: Latest Drive change dictionary per fileId.
        prev_entries: Snapshot of DriveEntry map prior to this delta tick.
        entries: Path-resolved DriveEntry map after applying coalesced changes.
        ledger: Deduplication ledger of processed Drive fingerprints.
        watcher_cfg: DriveWatcherConfig holding excluded MIME and path prefixes.
        protected_ids: Drive IDs protected by signature_vault policy.
        ref_id: Resolved folder ID of _ALFRED_REFERENTIEL_ when known.
        ref_name: Configured folder name of _ALFRED_REFERENTIEL_.

    Returns:
        Tuple of (eligible DriveDeltaItem list, skipped_reasons mapping).
    """
    eligible: list[DriveDeltaItem] = []
    skipped: dict[str, str] = {}
    for fid, change in coalesced.items():
        file_obj = change.get("file") or {}
        if change.get("removed") or file_obj.get("trashed") or not file_obj:
            skipped[fid] = "SKIP_TRASHED_OR_REMOVED"
            continue
        reason = _check_item_skip_reason(
            fid, file_obj, entries, watcher_cfg, protected_ids, ref_id, ref_name
        )
        if reason:
            skipped[fid] = reason
            continue
        item = _build_delta_item(fid, file_obj, prev_entries, entries)
        if ledger.has_processed(fid, item.version_fingerprint):
            skipped[fid] = "SKIP_ALREADY_PROCESSED"
            continue
        eligible.append(item)
    return eligible, skipped


def _check_item_skip_reason(
    fid: str,
    file_obj: Mapping[str, Any],
    entries: Mapping[str, DriveEntry],
    watcher_cfg: DriveWatcherConfig,
    protected_ids: set[str],
    ref_id: str,
    ref_name: str,
) -> str:
    """Return non-empty Speed-1 skip code when file_obj must be bypassed at 0 tokens."""
    mime = str(file_obj.get("mimeType", ""))
    if mime == FOLDER_MIME:
        return "SKIP_FOLDER"
    if mime == SHORTCUT_MIME:
        return "SKIP_SHORTCUT"
    owned = bool(file_obj.get("ownedByMe", True))
    parents = [str(p) for p in file_obj.get("parents", []) if p]
    if not owned and (fid not in entries or not any(p in entries for p in parents)):
        return "SKIP_ORPHAN_SHARED_WITH_ME"
    ancestors = _ancestor_ids_in_index(fid, parents, entries)
    if ancestors & protected_ids:
        return "SKIP_PROTECTED_SIGNATURE_ID"
    path = entries[fid].path if fid in entries else f"/{file_obj.get('name', '')}"
    if (
        (ref_id and ref_id in ancestors)
        or path.startswith(f"/{ref_name}/")
        or path == f"/{ref_name}"
    ):
        return "SKIP_REFERENTIEL_LOOP"
    if _is_excluded_path_or_mime(path, mime, watcher_cfg):
        return "SKIP_EXCLUDED_PATH_OR_MIME"
    return ""


def _is_excluded_path_or_mime(path: str, mime: str, cfg: DriveWatcherConfig) -> bool:
    """Check DriveWatcherConfig excluded MIME prefixes and folder path prefixes."""
    lower_mime = mime.lower()
    if any(lower_mime.startswith(pref.lower()) for pref in cfg.excluded_mime_prefixes):
        return True
    lower_path = path.lower()
    for raw_pref in cfg.excluded_path_prefixes:
        norm = "/" + raw_pref.strip().strip("/").lower()
        if norm != "/" and (lower_path == norm or lower_path.startswith(norm + "/")):
            return True
    return False


def _ancestor_ids_in_index(
    fid: str, direct_parents: Sequence[str], entries: Mapping[str, DriveEntry]
) -> set[str]:
    """Collect fid and all ancestor folder IDs known in the local Drive index."""
    chain = {fid}
    queue = list(direct_parents)
    while queue:
        cur = queue.pop()
        if cur and cur not in chain:
            chain.add(cur)
            if cur in entries:
                queue.extend(entries[cur].parents)
    return chain


def _build_delta_item(
    fid: str,
    file_obj: Mapping[str, Any],
    prev_entries: Mapping[str, DriveEntry],
    entries: Mapping[str, DriveEntry],
) -> DriveDeltaItem:
    """Construct a typed DriveDeltaItem from a raw Drive change dictionary."""
    parents = [str(p) for p in file_obj.get("parents", []) if p]
    if fid not in prev_entries:
        kind = DriveChangeKind.ADDED
    elif prev_entries[fid].parents != parents:
        kind = DriveChangeKind.MOVED
    else:
        kind = DriveChangeKind.MODIFIED
    return DriveDeltaItem(
        file_id=fid,
        name=str(file_obj.get("name", "")),
        mime_type=str(file_obj.get("mimeType", "")),
        parents=parents,
        path=entries[fid].path if fid in entries else "",
        modified_time=str(file_obj.get("modifiedTime", "")),
        md5_checksum=str(file_obj.get("md5Checksum", "")),
        head_revision_id=str(file_obj.get("headRevisionId", "")),
        owned_by_me=bool(file_obj.get("ownedByMe", True)),
        shared=bool(file_obj.get("shared", False)),
        change_kind=kind,
    )
