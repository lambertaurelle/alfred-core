"""Gmail API v1 connector for incremental history polling, drafts, and STARRED labels."""

from __future__ import annotations

import base64
import email
import email.policy
import html
import re
from email.message import EmailMessage as MimeEmailMessage
from typing import Any

from alfred.domain.models import AlfredPaths, AlfredSettings, EmailMessage
from alfred.services.google_auth import GoogleOAuthService

STRICT_IGNORED_GMAIL_LABELS = frozenset({"DRAFT", "SPAM", "TRASH"})
IGNORED_GMAIL_LABELS = STRICT_IGNORED_GMAIL_LABELS


class GmailHistoryExpiredError(RuntimeError):
    """Raised when Gmail API returns HTTP 404 for an expired startHistoryId."""


def _is_http_404(exc: BaseException) -> bool:
    """Return True when an exception represents a Google API HTTP 404 response."""
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) == 404 or getattr(exc, "status_code", None) == 404


def _is_incoming_candidate(labels: set[str], added_in_inbox: bool = False) -> bool:
    """Return True for incoming mail, keeping self-sent (SENT+INBOX) and dropping pure SENT."""
    if not STRICT_IGNORED_GMAIL_LABELS.isdisjoint(labels):
        return False
    return not ("SENT" in labels and "INBOX" not in labels and not added_in_inbox)


def _decode_base64url(data: str) -> str:
    """Decode an unpadded URL-safe base64 string into UTF-8 text."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _html_to_plain_text(raw_html: str) -> str:
    """Convert an HTML email body into clean readable plain text without external parsers."""
    cleaned = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", raw_html)
    cleaned = re.sub(r"(?i)<\s*(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", "\n", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    unescaped = html.unescape(cleaned)
    lines = [" ".join(line.split()) for line in unescaped.splitlines()]
    return "\n".join(line for line in lines if line).strip()


class GmailService:
    """Google Gmail v1 adapter handling historyId deltas, drafts, and message starring."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        gmail_client: Any | None = None,
        oauth_service: GoogleOAuthService | None = None,
    ) -> None:
        """Initialize the Gmail API service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings.
            gmail_client: Optional injected Gmail v1 client.
            oauth_service: Optional shared GoogleOAuthService instance.
        """
        self._paths = paths
        self._settings = settings
        self._gmail_client = gmail_client
        self._oauth = oauth_service or GoogleOAuthService(paths, settings)
        self._cached_addresses: frozenset[str] | None = None

    def get_current_history_id(self) -> str:
        """Fetch the current mailbox historyId without touching past messages."""
        profile = self._require_client().users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def list_send_as_identities(self) -> list[dict[str, Any]]:
        """Discover primary mailbox address and verified sendAs aliases via Gmail API."""
        identities: dict[str, dict[str, Any]] = {}
        client = self._require_client()
        try:
            profile = client.users().getProfile(userId="me").execute()
            raw_addr = profile.get("emailAddress", "") if isinstance(profile, dict) else ""
            primary = str(raw_addr).strip().lower()
            if "@" in primary:
                identities[primary] = {
                    "email": primary, "display_name": "", "is_primary": True,
                    "is_default": True, "verification_status": "accepted",
                }
        except Exception:
            pass
        try:
            resp = client.users().settings().sendAs().list(userId="me").execute()
            for entry in (resp.get("sendAs", []) if isinstance(resp, dict) else []):
                if not isinstance(entry, dict):
                    continue
                addr = str(entry.get("sendAsEmail", "")).strip().lower()
                if "@" not in addr:
                    continue
                identities[addr] = {
                    "email": addr,
                    "display_name": str(entry.get("displayName", "")).strip(),
                    "is_primary": bool(entry.get("isPrimary", False)),
                    "is_default": bool(entry.get("isDefault", False)),
                    "verification_status": str(entry.get("verificationStatus", "accepted")),
                }
        except Exception:
            pass
        return sorted(identities.values(), key=lambda item: (not item["is_primary"], item["email"]))

    def get_authenticated_addresses(self) -> frozenset[str]:
        """Return cached lowercase primary and sendAs alias addresses for the mailbox owner."""
        if self._cached_addresses is None:
            self._cached_addresses = frozenset(
                str(item["email"]) for item in self.list_send_as_identities()
            )
        return self._cached_addresses

    def list_new_messages_since(self, start_history_id: str) -> tuple[list[EmailMessage], str]:
        """List newly added incoming messages across all history pages since start_history_id.

        Args:
            start_history_id: Bookmark Gmail historyId from previous poll.
        """
        client = self._require_client()
        candidates, latest_history_id = self._collect_added_message_ids(client, start_history_id)
        messages: list[EmailMessage] = []
        for msg_id, added_in_inbox in candidates:
            fetched = self._fetch_message_or_none(client, msg_id)
            if fetched is not None and _is_incoming_candidate(
                set(fetched.label_ids), added_in_inbox=added_in_inbox
            ):
                messages.append(fetched)
        return messages, latest_history_id

    def search_messages(self, query: str, max_results: int = 10) -> list[EmailMessage]:
        """Search Gmail messages read-only using standard Gmail query syntax.

        Args:
            query: Gmail search query string (e.g. 'from:user@example.com').
            max_results: Maximum number of messages to retrieve.
        """
        client = self._require_client()
        q_max = max(1, int(max_results))
        resp = client.users().messages().list(userId="me", q=query, maxResults=q_max).execute()
        results: list[EmailMessage] = []
        for stub in resp.get("messages", []):
            msg_id = str(stub.get("id", ""))
            if msg_id and (fetched := self._fetch_message_or_none(client, msg_id)) is not None:
                results.append(fetched)
        return results

    def get_message(self, message_id: str) -> EmailMessage | None:
        """Fetch a single Gmail message by ID read-only, or None when unavailable.

        Args:
            message_id: Target Gmail message identifier.
        """
        try:
            client = self._require_client()
        except Exception:
            return None
        return self._fetch_message_or_none(client, message_id)

    def get_thread_messages(self, thread_id: str) -> list[EmailMessage]:
        """Fetch all messages in a Gmail thread read-only in chronological order.

        Args:
            thread_id: Target Gmail thread identifier.
        """
        req = self._require_client().users().threads().get(userId="me", id=thread_id, format="full")
        return [self._parse_raw_message(raw_m) for raw_m in req.execute().get("messages", [])]

    def restore_inbox_label(self, message_id: str) -> None:
        """Re-apply the INBOX label to an archived Gmail message.

        Args:
            message_id: Target Gmail message identifier.
        """
        self._require_client().users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": ["INBOX"]}
        ).execute()

    def create_draft(
        self,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str = "",
        references: str = "",
        from_addr: str = "",
    ) -> str:
        """Create a Gmail draft reply in the target thread without sending it.

        Args:
            thread_id: Gmail threadId to attach the draft to.
            to_addr: Recipient email address.
            subject: Reply subject line.
            body_text: Plain-text draft body.
            in_reply_to: Optional RFC 2822 Message-ID of the parent email.
            references: Optional RFC 2822 References header chain.
            from_addr: Optional explicit From header override (defaults to identity sendAs).
        """
        raw = self._encode_mime(to_addr, subject, body_text, in_reply_to, references, from_addr)
        body = {"message": {"threadId": thread_id, "raw": raw}}
        created = self._require_client().users().drafts().create(userId="me", body=body).execute()
        return str(created["id"])

    def update_draft(
        self,
        draft_id: str,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str = "",
        references: str = "",
        from_addr: str = "",
    ) -> str:
        """Update an existing Gmail draft after user inline comment corrections.

        Args:
            draft_id: Existing Gmail draft identifier.
            thread_id: Associated Gmail thread identifier.
            to_addr: Recipient email address.
            subject: Updated subject line.
            body_text: Revised plain-text body.
            in_reply_to: Optional RFC 2822 Message-ID of the parent email.
            references: Optional RFC 2822 References header chain.
            from_addr: Optional explicit From header override (defaults to identity sendAs).
        """
        raw = self._encode_mime(to_addr, subject, body_text, in_reply_to, references, from_addr)
        body = {"message": {"threadId": thread_id, "raw": raw}}
        res = self._require_client().users().drafts().update(userId="me", id=draft_id, body=body)
        return str(res.execute()["id"])

    def send_draft(
        self, draft_id: str, archive_inbox: bool = False, thread_id: str = "", unstar: bool = False
    ) -> str:
        """Send an existing Gmail draft strictly post-Gate approval.

        Args:
            draft_id: Validated Gmail draft identifier.
            archive_inbox: When True, remove INBOX and UNREAD from the sent thread.
            thread_id: Optional explicit threadId fallback when archiving.
            unstar: When True and archive_inbox is True, also remove STARRED.
        """
        return self.send_draft_with_archive(draft_id, archive_inbox, thread_id, unstar)[0]

    def send_draft_with_archive(
        self, draft_id: str, archive_inbox: bool = True, thread_id: str = "", unstar: bool = False
    ) -> tuple[str, bool]:
        """Send a Gmail draft and optionally archive its conversation thread in Gmail.

        Args:
            draft_id: Validated Gmail draft identifier.
            archive_inbox: When True, archive the thread after sending.
            thread_id: Optional explicit Gmail threadId fallback.
            unstar: When True and archiving, also remove STARRED.
        """
        client = self._require_client()
        eff_from = self._settings.identity.format_from_header()
        if eff_from:
            self._enforce_draft_from_header(client, draft_id, eff_from)
        sent = client.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        sent_id = str(sent["id"])
        raw_th = sent.get("threadId") if isinstance(sent, dict) else None
        resolved_th = thread_id.strip() or (raw_th.strip() if isinstance(raw_th, str) else "")
        if archive_inbox and resolved_th:
            self.archive_thread(resolved_th, mark_read=True, unstar=unstar)
            return sent_id, True
        return sent_id, False

    def _enforce_draft_from_header(self, client: Any, draft_id: str, eff_from: str) -> None:
        """Update draft MIME From header when identity.default_send_as_email is configured."""
        try:
            req = client.users().drafts().get(userId="me", id=draft_id, format="raw")
            draft_obj = req.execute()
        except Exception:
            return
        msg_obj = draft_obj.get("message") if isinstance(draft_obj, dict) else None
        if not isinstance(msg_obj, dict):
            return
        raw_b64 = msg_obj.get("raw")
        new_raw = ""
        if isinstance(raw_b64, str) and raw_b64:
            padded = raw_b64 + "=" * (-len(raw_b64) % 4)
            parsed = email.message_from_bytes(
                base64.urlsafe_b64decode(padded), policy=email.policy.default
            )
            if "From" in parsed:
                parsed.replace_header("From", eff_from)
            else:
                parsed["From"] = eff_from
            new_raw = base64.urlsafe_b64encode(parsed.as_bytes()).decode("ascii")
        elif isinstance(msg_obj.get("payload"), dict):
            pm = self._parse_raw_message(msg_obj)
            new_raw = self._encode_mime(pm.recipient, pm.subject, pm.body_text, from_addr=eff_from)
        if new_raw:
            upd_msg: dict[str, Any] = {"raw": new_raw}
            if msg_obj.get("threadId"):
                upd_msg["threadId"] = str(msg_obj["threadId"])
            body = {"id": draft_id, "message": upd_msg}
            client.users().drafts().update(userId="me", id=draft_id, body=body).execute()

    def archive_thread(self, thread_id: str, mark_read: bool = True, unstar: bool = False) -> None:
        """Archive an entire Gmail thread by removing INBOX (and optional UNREAD/STARRED).

        Args:
            thread_id: Target Gmail thread identifier.
            mark_read: When True (default), also remove UNREAD.
            unstar: When True, also remove STARRED.
        """
        remove_ids = ["INBOX", *(["UNREAD"] if mark_read else []), *(["STARRED"] if unstar else [])]
        self._require_client().users().threads().modify(
            userId="me", id=thread_id, body={"removeLabelIds": remove_ids}
        ).execute()

    def archive_message(
        self, message_id: str, mark_read: bool = True, unstar: bool = False
    ) -> None:
        """Archive a single Gmail message by removing INBOX (and optional UNREAD/STARRED).

        Args:
            message_id: Target Gmail message identifier.
            mark_read: When True (default), also remove UNREAD.
            unstar: When True, also remove STARRED.
        """
        remove_ids = ["INBOX", *(["UNREAD"] if mark_read else []), *(["STARRED"] if unstar else [])]
        self._require_client().users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": remove_ids}
        ).execute()

    def star_message(self, message_id: str) -> None:
        """Apply the STARRED label to a message requiring user action.

        Args:
            message_id: Target Gmail message identifier.
        """
        client = self._require_client()
        client.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": ["STARRED"]},
        ).execute()

    def _require_client(self) -> Any:
        """Return injected Gmail client or build one lazily via GoogleOAuthService."""
        if self._gmail_client is not None:
            return self._gmail_client
        self._gmail_client = self._oauth.build_api_client("gmail", "v1")
        return self._gmail_client

    def _collect_added_message_ids(
        self,
        client: Any,
        start_history_id: str,
    ) -> tuple[list[tuple[str, bool]], str]:
        """Paginate users.history.list and return deduplicated (msg_id, added_in_inbox) pairs."""
        seen_order: list[str] = []
        inbox_flags: dict[str, bool] = {}
        latest_history_id = start_history_id
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token
            try:
                resp = client.users().history().list(**kwargs).execute()
            except Exception as exc:
                if _is_http_404(exc):
                    raise GmailHistoryExpiredError(
                        f"Gmail startHistoryId '{start_history_id}' expired (HTTP 404)."
                    ) from exc
                raise
            latest_history_id = str(resp.get("historyId", latest_history_id))
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_stub = added.get("message", {})
                    labels = {str(lbl) for lbl in msg_stub.get("labelIds", [])}
                    msg_id = str(msg_stub.get("id", ""))
                    in_inbox = "INBOX" in labels
                    if msg_id and _is_incoming_candidate(labels, added_in_inbox=in_inbox):
                        if msg_id not in inbox_flags:
                            seen_order.append(msg_id)
                            inbox_flags[msg_id] = in_inbox
                        elif in_inbox:
                            inbox_flags[msg_id] = True
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return [(mid, inbox_flags[mid]) for mid in seen_order], latest_history_id

    def _fetch_message_or_none(self, client: Any, message_id: str) -> EmailMessage | None:
        """Fetch a Gmail message payload, returning None if deleted concurrently (HTTP 404)."""
        try:
            raw = client.users().messages().get(userId="me", id=message_id, format="full").execute()
            return self._parse_raw_message(raw)
        except Exception as exc:
            if _is_http_404(exc):
                return None
            raise

    def _parse_raw_message(self, raw: dict[str, Any]) -> EmailMessage:
        """Normalize a raw Gmail API message dictionary into an EmailMessage."""
        payload = raw.get("payload", {})
        headers = {
            str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in payload.get("headers", [])
        }
        html_b64 = self._find_mime_part_data(payload, "text/html")
        raw_html = _decode_base64url(html_b64) if html_b64 else ""
        plain_b64 = self._find_mime_part_data(payload, "text/plain")
        fallback_txt = _html_to_plain_text(raw_html) if raw_html else ""
        body_text = _decode_base64url(plain_b64) if plain_b64 else fallback_txt
        mimes, filenames = self._collect_mime_metadata(payload)
        return EmailMessage(
            message_id=str(raw["id"]),
            thread_id=str(raw.get("threadId", "")),
            sender=headers.get("from", ""),
            recipient=headers.get("to", ""),
            subject=headers.get("subject", ""),
            snippet=str(raw.get("snippet", "")),
            body_text=body_text,
            raw_html=raw_html,
            headers=headers,
            label_ids=[str(lbl) for lbl in raw.get("labelIds", [])],
            mime_types=mimes,
            attachment_filenames=filenames,
        )

    def _collect_mime_metadata(self, payload: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Collect all lowercase MIME types and attachment filenames across a Gmail MIME tree."""
        mimes: list[str] = []
        filenames: list[str] = []
        stack = [payload]
        while stack:
            part = stack.pop()
            mt = str(part.get("mimeType", "")).strip().lower()
            if mt and mt not in mimes:
                mimes.append(mt)
            fname = str(part.get("filename", "")).strip()
            if fname and fname not in filenames:
                filenames.append(fname)
            stack.extend(reversed(part.get("parts", [])))
        return mimes, filenames

    def _find_mime_part_data(self, payload: dict[str, Any], target_mime: str) -> str:
        """Recursively find base64url body data for target_mime in a Gmail MIME tree."""
        mime_type = str(payload.get("mimeType", "")).lower()
        body_data = payload.get("body", {}).get("data")
        if mime_type == target_mime and body_data:
            return str(body_data)
        for part in payload.get("parts", []):
            found = self._find_mime_part_data(part, target_mime)
            if found:
                return found
        return ""

    def _encode_mime(
        self,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str = "",
        references: str = "",
        from_addr: str = "",
    ) -> str:
        """Encode an RFC 2822 UTF-8 email message into URL-safe base64."""
        mime = MimeEmailMessage()
        eff_from = from_addr.strip() or self._settings.identity.format_from_header()
        if eff_from:
            mime["From"] = eff_from
        mime["To"] = to_addr
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = references or in_reply_to
        elif references:
            mime["References"] = references
        mime.set_content(body_text)
        return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
