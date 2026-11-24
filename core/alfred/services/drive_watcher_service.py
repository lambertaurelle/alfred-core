"""2-speed Google Drive watcher service with 0-token pre-filters, coalescing, and anti-loop."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from alfred.domain.drive_watcher_models import (
    DriveDeltaItem,
    DriveDeltaReport,
    DriveGovernanceActionType,
    DriveGovernanceOp,
    DriveMutationResult,
    DriveWatcherBatchDecision,
    DriveWatcherOutputType,
    ProcessedDriveEvent,
    SanitizedDriveTextResult,
)
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    DriveEntry,
    DriveMetadataIndex,
    FrontmatterSchema,
    KnowledgeDocument,
    utc_now_iso,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_governance_service import DriveGovernanceService
from alfred.services.drive_security_sanitizer import DriveSecuritySanitizer
from alfred.services.drive_service import DriveService
from alfred.services.drive_speed1_filter import (
    apply_coalesced_changes_to_entries,
    filter_speed1_delta_items,
)
from alfred.services.drive_watcher_helpers import (
    classify_drive_batch_with_agy,
    render_drive_gate_plan,
    render_drive_silent_trace,
)
from alfred.services.knowledge_service import KnowledgeService

DELTA_CHANGE_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,file(id,name,mimeType,parents,modifiedTime,"
    "md5Checksum,headRevisionId,ownedByMe,shared,trashed))"
)

CognitiveDriveWatcherFn = Callable[
    [list[DriveDeltaItem], dict[str, SanitizedDriveTextResult], list[KnowledgeDocument]],
    tuple[DriveWatcherOutputType, str, list[DriveGovernanceOp]],
]


class DriveWatcherService:
    """Poll incremental Google Drive changes with 0-token Speed-1 guards and Speed-2 governance."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        drive_service: DriveService,
        governance_service: DriveGovernanceService,
        knowledge_service: KnowledgeService,
        agy_manager: AgySessionManager,
        cognitive_fn: CognitiveDriveWatcherFn | None = None,
        spawn_live_agy: bool = False,
        sanitizer: DriveSecuritySanitizer | None = None,
    ) -> None:
        """Initialize the 2-speed Google Drive watcher service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including features and drive_watcher config.
            state_store: Atomic local state store for index, bookmarks, and watcher ledger.
            drive_service: Google Drive v3 index and 0-OCR native text reader service.
            governance_service: Non-destructive DriveGovernanceService for moves/shortcuts.
            knowledge_service: 3-tier YAML frontmatter knowledge service.
            agy_manager: Antigravity Web session manager for [SILENT] traces and Gates.
            cognitive_fn: Optional injected Speed-2 classifier callback.
            spawn_live_agy: When True, spawn live Antigravity CLI conversations.
            sanitizer: Optional DriveSecuritySanitizer instance.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store
        self._drive = drive_service
        self._governance = governance_service
        self._knowledge = knowledge_service
        self._agy = agy_manager
        self._cognitive_fn = cognitive_fn
        self._spawn_live_agy = spawn_live_agy
        self._sanitizer = sanitizer or DriveSecuritySanitizer()

    def collect_delta_report(self) -> DriveDeltaReport:
        """Execute Speed-1 zero-token delta scan, update DriveMetadataIndex, and filter items.

        Returns:
            DriveDeltaReport containing eligible DriveDeltaItems and deterministic skip reasons.
        """
        index = self._state_store.drive_index.load()
        start_token = index.start_page_token or (
            self._state_store.bookmarks.load().drive_page_token or ""
        )
        if not start_token:
            synced, _ = self._drive.init_drive()
            return DriveDeltaReport(
                start_page_token_before="",
                new_start_page_token=synced.start_page_token,
            )

        raw_changes, new_start_token = self._fetch_all_delta_changes(start_token)
        coalesced: dict[str, dict[str, Any]] = {}
        for change in raw_changes:
            fid = str(change.get("fileId", "")).strip()
            if fid:
                coalesced[fid] = change

        prev_entries = dict(index.entries)
        entries = apply_coalesced_changes_to_entries(prev_entries, coalesced)
        eligible, skipped = filter_speed1_delta_items(
            coalesced=coalesced,
            prev_entries=prev_entries,
            entries=entries,
            ledger=self._state_store.drive_watcher_ledger.load(),
            watcher_cfg=self._settings.drive_watcher,
            protected_ids=set(self._settings.security.protected_signature_drive_ids),
            ref_id=self._governance.resolve_referentiel_folder_id(),
            ref_name=self._settings.referentiel_drive_folder_name,
        )
        self._save_updated_index(entries, new_start_token)
        return DriveDeltaReport(
            start_page_token_before=start_token,
            new_start_page_token=new_start_token,
            total_raw_changes=len(raw_changes),
            eligible_items=eligible,
            skipped_reasons=skipped,
        )

    def poll_and_process(
        self,
        spawn_live_agy: bool | None = None,
        use_cognitive_agy: bool = False,
    ) -> list[DriveWatcherBatchDecision]:
        """Run Speed-1 0-token filter followed by coalesced Speed-2 governance and archival.

        Args:
            spawn_live_agy: Optional override to spawn live Antigravity Web sessions.
            use_cognitive_agy: When True, invoke agy CLI classifier when cognitive_fn is None.

        Returns:
            Ordered list of DriveWatcherBatchDecision records (empty when 0 eligible items).
        """
        if not self._settings.features.drive_watcher:
            return []
        report = self.collect_delta_report()
        if not report.eligible_items:
            return []

        eff_spawn = self._spawn_live_agy if spawn_live_agy is None else spawn_live_agy
        cap = self._settings.drive_watcher.coalesce_max_files_per_batch
        items = report.eligible_items
        batches = [items[idx : idx + cap] for idx in range(0, len(items), cap)]
        return [
            self._process_batch(batch, eff_spawn=eff_spawn, use_cognitive_agy=use_cognitive_agy)
            for batch in batches
        ]

    def _fetch_all_delta_changes(self, start_token: str) -> tuple[list[dict[str, Any]], str]:
        """Fetch all paginated changes from Google Drive before mutating local bookmarks."""
        client = self._drive.get_raw_client()
        collected: list[dict[str, Any]] = []
        next_token = start_token
        new_start_token = start_token
        while next_token:
            resp = (
                client.changes()
                .list(pageToken=next_token, fields=DELTA_CHANGE_FIELDS)
                .execute()
            )
            collected.extend(resp.get("changes", []))
            new_start_token = str(resp.get("newStartPageToken") or new_start_token)
            next_token = str(resp.get("nextPageToken") or "")
        return collected, new_start_token

    def _process_batch(
        self,
        batch: list[DriveDeltaItem],
        *,
        eff_spawn: bool,
        use_cognitive_agy: bool,
    ) -> DriveWatcherBatchDecision:
        """Execute Speed-2 extraction, sanitization, governance firewall, and archival."""
        batch_id = f"drv_batch_{uuid.uuid4().hex[:10]}"
        max_chars = self._settings.drive_watcher.max_extracted_chars_per_file
        sanitized_map: dict[str, SanitizedDriveTextResult] = {}
        for item in batch:
            raw_text = self._drive.read_file_text(item.file_id)
            sanitized_map[item.file_id] = self._sanitizer.sanitize_item(
                item.name, item.path, raw_text, max_chars=max_chars
            )

        suspicious = any(s.suspicious_injection for s in sanitized_map.values())
        docs = self._load_context_documents(batch, sanitized_map)
        out_type, reason, ops = self._invoke_classifier(
            batch, sanitized_map, docs, use_cognitive_agy=use_cognitive_agy
        )

        violations = self._collect_silent_violations(out_type, ops)
        if suspicious or violations:
            esc_reason = reason if not violations else f"{reason} ({violations[0]})"
            return self._open_gate_decision(
                batch_id, batch, sanitized_map, esc_reason, ops, violations, suspicious, eff_spawn
            )
        if out_type == DriveWatcherOutputType.GATE_REQUIRED:
            return self._open_gate_decision(
                batch_id, batch, sanitized_map, reason, ops, [], False, eff_spawn
            )
        if out_type == DriveWatcherOutputType.SILENT_AUTO_FILED:
            return self._apply_silent_auto_filed(batch_id, batch, reason, ops, eff_spawn)
        return self._complete_silent_no_action(batch_id, batch, reason, eff_spawn)

    def _invoke_classifier(
        self,
        batch: list[DriveDeltaItem],
        sanitized_map: dict[str, SanitizedDriveTextResult],
        docs: list[KnowledgeDocument],
        *,
        use_cognitive_agy: bool,
    ) -> tuple[DriveWatcherOutputType, str, list[DriveGovernanceOp]]:
        """Dispatch Speed-2 cognitive classification via callback, agy CLI, or safe default."""
        if self._cognitive_fn is not None:
            raw_type, reason, raw_ops = self._cognitive_fn(batch, sanitized_map, docs)
            return (raw_type, reason, list(raw_ops))
        if use_cognitive_agy:
            return classify_drive_batch_with_agy(
                self._agy, self._settings, self._sanitizer, batch, sanitized_map, docs
            )
        return (DriveWatcherOutputType.SILENT_NO_ACTION, "Indexé sans anomalie détectée", [])

    def _collect_silent_violations(
        self, out_type: DriveWatcherOutputType, ops: Sequence[DriveGovernanceOp]
    ) -> list[str]:
        """Validate proposed silent operations against static bounds and ancestor chains."""
        if out_type != DriveWatcherOutputType.SILENT_AUTO_FILED:
            return []
        protected = set(self._settings.security.protected_signature_drive_ids)
        ref_id = self._governance.resolve_referentiel_folder_id()
        violations = self._sanitizer.validate_silent_operations(
            ops, protected, ref_id, self._settings.max_knowledge_body_lines
        )
        for op in ops:
            violations.extend(self._governance.validate_op_ancestors(op))
        return violations

    def _apply_silent_auto_filed(
        self,
        batch_id: str,
        batch: Sequence[DriveDeltaItem],
        reason: str,
        ops: Sequence[DriveGovernanceOp],
        eff_spawn: bool,
    ) -> DriveWatcherBatchDecision:
        """Execute validated non-destructive mutations, record backups, and archive [SILENT]."""
        applied: list[DriveMutationResult] = []
        updated_md: list[str] = []
        source_ids = [it.file_id for it in batch]
        for op in ops:
            if op.action == DriveGovernanceActionType.MOVE:
                applied.append(self._governance.move_file(op.file_id, op.target_parent_id))
            elif op.action == DriveGovernanceActionType.RENAME:
                applied.append(self._governance.rename_file(op.file_id, op.new_name))
            elif op.action == DriveGovernanceActionType.CREATE_FOLDER:
                applied.append(
                    self._governance.create_folder(op.new_name, parent_id=op.target_parent_id)
                )
            elif op.action == DriveGovernanceActionType.CREATE_SHORTCUT:
                applied.append(
                    self._governance.create_shortcut(
                        op.shortcut_target_id or op.file_id,
                        parent_id=op.target_parent_id,
                        name=op.new_name,
                    )
                )
            elif op.action == DriveGovernanceActionType.UPSERT_KNOWLEDGE:
                self._execute_knowledge_op(op, source_ids)
                updated_md.append(op.knowledge_file)

        self._record_batch_in_ledger(batch, outcome="SILENT_AUTO_FILED", preserve_mutated=True)
        ws = self._settings.resolve_workspace_project("alfred-drive")
        trace_md = render_drive_silent_trace(
            batch, DriveWatcherOutputType.SILENT_AUTO_FILED, reason, applied, updated_md
        )
        sess = self._agy.create_and_archive_trace(
            workspace=ws,
            title=f"[SILENT] Drive: {reason[:60]}",
            trace_markdown=trace_md,
            source_message_id=batch[0].file_id,
            outcome=f"SILENT_AUTO_FILED: {reason}",
            spawn_live=eff_spawn,
        )
        return DriveWatcherBatchDecision(
            batch_id=batch_id,
            file_ids=source_ids,
            output_type=DriveWatcherOutputType.SILENT_AUTO_FILED,
            reason=reason,
            applied_mutations=applied,
            updated_knowledge_files=updated_md,
            session_id=sess.session_id,
            session_archived=True,
        )

    def _execute_knowledge_op(self, op: DriveGovernanceOp, source_ids: list[str]) -> None:
        """Upsert a concise Tier-2 .md reference document and register its Drive backup ID."""
        fm = FrontmatterSchema(
            id=op.knowledge_id,
            title=op.knowledge_title,
            domain=op.knowledge_domain,
            summary=op.knowledge_summary,
            keywords=list(op.knowledge_keywords),
            drive_source_ids=source_ids,
        )
        self._knowledge.upsert_document(op.knowledge_file, fm, op.knowledge_body)
        audits = self._state_store.knowledge_audit.read_all()
        if audits and audits[-1].drive_backup_file_id:
            self._governance.record_self_mutation(
                audits[-1].drive_backup_file_id, action="KNOWLEDGE_BACKUP"
            )

    def _complete_silent_no_action(
        self, batch_id: str, batch: Sequence[DriveDeltaItem], reason: str, eff_spawn: bool
    ) -> DriveWatcherBatchDecision:
        """Record batch in ledger and materialize mandatory archived [SILENT] session."""
        self._record_batch_in_ledger(batch, outcome="SILENT_NO_ACTION", preserve_mutated=False)
        ws = self._settings.resolve_workspace_project("alfred-drive")
        trace_md = render_drive_silent_trace(
            batch, DriveWatcherOutputType.SILENT_NO_ACTION, reason, [], []
        )
        sess = self._agy.create_and_archive_trace(
            workspace=ws,
            title=f"[SILENT] Drive: {reason[:60]}",
            trace_markdown=trace_md,
            source_message_id=batch[0].file_id,
            outcome=f"SILENT_NO_ACTION: {reason}",
            spawn_live=eff_spawn,
        )
        return DriveWatcherBatchDecision(
            batch_id=batch_id,
            file_ids=[it.file_id for it in batch],
            output_type=DriveWatcherOutputType.SILENT_NO_ACTION,
            reason=reason,
            session_id=sess.session_id,
            session_archived=True,
        )

    def _open_gate_decision(
        self,
        batch_id: str,
        batch: Sequence[DriveDeltaItem],
        sanitized_map: Mapping[str, SanitizedDriveTextResult],
        reason: str,
        ops: Sequence[DriveGovernanceOp],
        violations: list[str],
        suspicious: bool,
        eff_spawn: bool,
    ) -> DriveWatcherBatchDecision:
        """Open an active Gate session in alfred-drive with zero Drive/Knowledge mutations."""
        self._record_batch_in_ledger(batch, outcome="GATE_REQUIRED", preserve_mutated=False)
        ws = self._settings.resolve_workspace_project("alfred-drive")
        plan_md = render_drive_gate_plan(batch, sanitized_map, reason, ops, violations, suspicious)
        sess = self._agy.create_session(
            workspace=ws,
            source_message_id=batch[0].file_id,
            implementation_plan_md=plan_md,
            spawn_live=eff_spawn,
            title=f"[Drive Gate] {reason[:60]}",
        )
        return DriveWatcherBatchDecision(
            batch_id=batch_id,
            file_ids=[it.file_id for it in batch],
            output_type=DriveWatcherOutputType.GATE_REQUIRED,
            reason=reason,
            session_id=sess.session_id,
            session_archived=False,
            implementation_plan_markdown=plan_md,
            suspicious_injection=suspicious,
            validation_errors=violations,
        )

    def _record_batch_in_ledger(
        self, batch: Sequence[DriveDeltaItem], *, outcome: str, preserve_mutated: bool
    ) -> None:
        """Persist processed DriveDeltaItem fingerprints without clobbering self-mutations."""

        def _apply(cur: Any) -> Any:
            """Merge batch fingerprints into DriveWatcherLedger state."""
            events = dict(cur.events)
            for item in batch:
                existing = events.get(item.file_id)
                if preserve_mutated and existing and existing.origin == "alfred_self_mutation":
                    hist = list(existing.recent_fingerprints)
                    if item.version_fingerprint not in hist:
                        hist.append(item.version_fingerprint)
                    events[item.file_id] = existing.model_copy(
                        update={"recent_fingerprints": hist[-20:]}
                    )
                    continue
                events[item.file_id] = ProcessedDriveEvent(
                    file_id=item.file_id,
                    version_fingerprint=item.version_fingerprint,
                    parent_ids=list(item.parents),
                    origin="watcher",
                    outcome=outcome,
                )
            return cur.model_copy(update={"events": events})

        self._state_store.drive_watcher_ledger.update(_apply)

    def _load_context_documents(
        self,
        batch: Sequence[DriveDeltaItem],
        sanitized_map: Mapping[str, SanitizedDriveTextResult],
    ) -> list[KnowledgeDocument]:
        """Load taxonomy rules plus top relevant Tier-2 .md documents for Speed-2 context."""
        docs: list[KnowledgeDocument] = []
        seen: set[str] = set()
        tax_name = "00_drive_taxonomy_and_filing_rules.md"
        if (self._paths.knowledge_dir / tax_name).is_file():
            docs.append(self._knowledge.load_document(tax_name))
            seen.add(tax_name)
        query = " ".join(
            f"{sanitized_map[it.file_id].clean_name} {sanitized_map[it.file_id].clean_path}"
            for it in batch
        )
        for fname, _ in self._knowledge.select_relevant_frontmatters(query)[:3]:
            if fname not in seen:
                docs.append(self._knowledge.load_document(fname))
                seen.add(fname)
        return docs

    def _save_updated_index(self, entries: dict[str, DriveEntry], token: str) -> None:
        """Persist updated DriveMetadataIndex and GoogleSyncBookmarks atomically."""
        folders = [e for e in entries.values() if e.is_folder]
        files = [e for e in entries.values() if not e.is_folder]
        top_folders = sorted(
            {
                e.path.strip("/").split("/")[0]
                for e in folders
                if e.path and len(e.path.strip("/").split("/")) == 1
            }
        )
        now = utc_now_iso()
        updated = DriveMetadataIndex(
            start_page_token=token,
            last_synced_at=now,
            folder_count=len(folders),
            file_count=len(files),
            top_level_folders=top_folders,
            entries=entries,
        )
        self._state_store.drive_index.save(updated)
        self._state_store.bookmarks.update(
            lambda b: b.model_copy(
                update={"drive_page_token": token, "drive_last_synced_at": now}
            )
        )
