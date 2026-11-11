"""Gmail API v1 connector for incremental history polling, drafts, and STARRED labels."""

from __future__ import annotations

import base64
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
        """Fetch the current mailbox historyId without touching past messages.

        Returns:
            Current Gmail historyId string.
        """
        client = self._require_client()
        profile = client.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_authenticated_addresses(self) -> frozenset[str]:
        """Return lowercase primary and sendAs alias email addresses for the mailbox owner.

        Returns:
            Cached frozenset of lowercase email addresses owned by the authenticated user.
        """
        if self._cached_addresses is not None:
            return self._cached_addresses
        found: set[str] = set()
        client = self._require_client()
        try:
            profile = client.users().getProfile(userId="me").execute()
            primary = profile.get("emailAddress") if isinstance(profile, dict) else None
            if isinstance(primary, str) and "@" in primary:
                found.add(primary.strip().lower())
        except Exception:
            pass
        try:
            resp = client.users().settings().sendAs().list(userId="me").execute()
            items = resp.get("sendAs", []) if isinstance(resp, dict) else []
            for entry in items:
                alias = entry.get("sendAsEmail") if isinstance(entry, dict) else None
                if isinstance(alias, str) and "@" in alias:
                    found.add(alias.strip().lower())
        except Exception:
            pass
        self._cached_addresses = frozenset(found)
        return self._cached_addresses

    def list_new_messages_since(
        self,
        start_history_id: str,
    ) -> tuple[list[EmailMessage], str]:
        """List newly added incoming messages across all history pages since start_history_id.

        Args:
            start_history_id: Bookmark Gmail historyId from previous poll.

        Returns:
            Tuple of (ordered list of new incoming EmailMessage items, latest historyId).

        Raises:
            GmailHistoryExpiredError: If Gmail reports HTTP 404 on expired start_history_id.
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

        Returns:
            Ordered list of matching normalized EmailMessage records.
        """
        client = self._require_client()
        resp = (
            client.users()
            .messages()
            .list(userId="me", q=query, maxResults=max(1, int(max_results)))
            .execute()
        )
        results: list[EmailMessage] = []
        for stub in resp.get("messages", []):
            msg_id = str(stub.get("id", ""))
            if not msg_id:
                continue
            fetched = self._fetch_message_or_none(client, msg_id)
            if fetched is not None:
                results.append(fetched)
        return results

    def get_message(self, message_id: str) -> EmailMessage | None:
        """Fetch a single Gmail message by ID read-only, or None when unavailable.

        Args:
            message_id: Target Gmail message identifier.

        Returns:
            Normalized EmailMessage or None on HTTP 404 / missing client.
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

        Returns:
            List of normalized EmailMessage records belonging to thread_id.
        """
        client = self._require_client()
        req = client.users().threads().get(userId="me", id=thread_id, format="full")
        raw_thread = req.execute()
        return [self._parse_raw_message(raw_m) for raw_m in raw_thread.get("messages", [])]

    def restore_inbox_label(self, message_id: str) -> None:
        """Re-apply the INBOX label to an archived Gmail message.

        Args:
            message_id: Target Gmail message identifier.
        """
        client = self._require_client()
        client.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": ["INBOX"]},
        ).execute()

    def create_draft(
        self,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str = "",
        references: str = "",
    ) -> str:
        """Create a Gmail draft reply in the target thread without sending it.

        Args:
            thread_id: Gmail threadId to attach the draft to.
            to_addr: Recipient email address.
            subject: Reply subject line.
            body_text: Plain-text draft body.
            in_reply_to: Optional RFC 2822 Message-ID of the parent email.
            references: Optional RFC 2822 References header chain.

        Returns:
            Created Gmail draft_id.
        """
        client = self._require_client()
        raw = self._encode_mime(to_addr, subject, body_text, in_reply_to, references)
        created = (
            client.users()
            .drafts()
            .create(userId="me", body={"message": {"threadId": thread_id, "raw": raw}})
            .execute()
        )
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

        Returns:
            Updated Gmail draft_id.
        """
        client = self._require_client()
        raw = self._encode_mime(to_addr, subject, body_text, in_reply_to, references)
        updated = (
            client.users()
            .drafts()
            .update(
                userId="me",
                id=draft_id,
                body={"message": {"threadId": thread_id, "raw": raw}},
            )
            .execute()
        )
        return str(updated["id"])

    def send_draft(self, draft_id: str) -> str:
        """Send an existing Gmail draft strictly post-Gate approval.

        Args:
            draft_id: Validated Gmail draft identifier.

        Returns:
            Sent Gmail message identifier.
        """
        client = self._require_client()
        sent = client.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        return str(sent["id"])

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
        body_text = self._extract_body_text(payload)
        return EmailMessage(
            message_id=str(raw["id"]),
            thread_id=str(raw.get("threadId", "")),
            sender=headers.get("from", ""),
            recipient=headers.get("to", ""),
            subject=headers.get("subject", ""),
            snippet=str(raw.get("snippet", "")),
            body_text=body_text,
            headers=headers,
            label_ids=[str(lbl) for lbl in raw.get("labelIds", [])],
        )

    def _extract_body_text(self, payload: dict[str, Any]) -> str:
        """Extract text/plain first across MIME tree, falling back to stripped text/html."""
        plain = self._find_mime_part_data(payload, "text/plain")
        if plain:
            return _decode_base64url(plain)
        raw_html = self._find_mime_part_data(payload, "text/html")
        if raw_html:
            return _html_to_plain_text(_decode_base64url(raw_html))
        return ""

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
    ) -> str:
        """Encode an RFC 2822 UTF-8 email message into URL-safe base64."""
        mime = MimeEmailMessage()
        mime["To"] = to_addr
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = references or in_reply_to
        elif references:
            mime["References"] = references
        mime.set_content(body_text)
        return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
