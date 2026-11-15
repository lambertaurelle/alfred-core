"""Opt-in Google Workspace Suite connectors (Docs v1, Sheets v4, Slides v1, Forms v1)."""

from __future__ import annotations

from typing import Any

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.services.google_auth import GoogleOAuthService


class WorkspaceSuiteDisabledError(RuntimeError):
    """Raised when Docs/Sheets/Slides/Forms operations run while google_docs_suite is False."""


class GoogleWorkspaceSuiteService:
    """Opt-in Google Docs, Sheets, Slides, and Forms adapter gated by google_docs_suite."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        docs_client: Any | None = None,
        sheets_client: Any | None = None,
        slides_client: Any | None = None,
        forms_client: Any | None = None,
        oauth_service: GoogleOAuthService | None = None,
    ) -> None:
        """Initialize the opt-in Google Workspace Suite service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including features.google_docs_suite.
            docs_client: Optional injected Google Docs v1 discovery resource.
            sheets_client: Optional injected Google Sheets v4 discovery resource.
            slides_client: Optional injected Google Slides v1 discovery resource.
            forms_client: Optional injected Google Forms v1 discovery resource.
            oauth_service: Optional shared GoogleOAuthService instance.
        """
        self._paths = paths
        self._settings = settings
        self._docs_client = docs_client
        self._sheets_client = sheets_client
        self._slides_client = slides_client
        self._forms_client = forms_client
        self._oauth = oauth_service or GoogleOAuthService(paths, settings)

    def is_enabled(self) -> bool:
        """Return True when google_docs_suite is enabled in settings.features."""
        return bool(self._settings.features.google_docs_suite)

    def create_doc(self, title: str, initial_text: str = "") -> str:
        """Create a Google Doc and optionally insert initial plain text.

        Args:
            title: Title of the new Google Document.
            initial_text: Optional initial text appended right after document creation.

        Returns:
            Created Google Docs documentId string.

        Raises:
            WorkspaceSuiteDisabledError: When google_docs_suite is False.
        """
        client = self._require_docs_client()
        created = client.documents().create(body={"title": title}).execute()
        doc_id = str(created["documentId"])
        if initial_text:
            self.append_doc_text(doc_id, initial_text)
        return doc_id

    def append_doc_text(self, document_id: str, text: str) -> None:
        """Append plain text at the end of an existing Google Document.

        Args:
            document_id: Target Google Docs documentId.
            text: UTF-8 plain text to insert before the trailing section newline.

        Raises:
            WorkspaceSuiteDisabledError: When google_docs_suite is False.
        """
        client = self._require_docs_client()
        doc = client.documents().get(documentId=document_id).execute()
        content = doc.get("body", {}).get("content", []) if isinstance(doc, dict) else []
        end_index = 1
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            end_index = int(content[-1].get("endIndex", 1))
        insert_index = max(1, end_index - 1)
        client.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": insert_index},
                            "text": text,
                        }
                    }
                ]
            },
        ).execute()

    def read_sheet_values(self, spreadsheet_id: str, range_a1: str) -> list[list[Any]]:
        """Read cell values from a Google Sheet A1 range.

        Args:
            spreadsheet_id: Target Google Spreadsheet ID.
            range_a1: A1 notation range (e.g. 'Budget!A1:B20').

        Returns:
            2D list of cell values.

        Raises:
            WorkspaceSuiteDisabledError: When google_docs_suite is False.
        """
        client = self._require_sheets_client()
        resp = (
            client.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_a1)
            .execute()
        )
        raw_values = resp.get("values", []) if isinstance(resp, dict) else []
        return [list(row) for row in raw_values if isinstance(row, list)]

    def append_sheet_row(
        self,
        spreadsheet_id: str,
        range_a1: str,
        row_values: list[Any],
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        """Append a single row to a Google Sheet range.

        Args:
            spreadsheet_id: Target Google Spreadsheet ID.
            range_a1: Target table A1 range (e.g. 'Budget!A:B').
            row_values: Ordered cell values for the appended row.
            value_input_option: Google Sheets input mode ('USER_ENTERED' or 'RAW').

        Returns:
            Raw Google Sheets append response dictionary.

        Raises:
            WorkspaceSuiteDisabledError: When google_docs_suite is False.
        """
        client = self._require_sheets_client()
        resp = (
            client.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption=value_input_option,
                body={"values": [list(row_values)]},
            )
            .execute()
        )
        return dict(resp) if isinstance(resp, dict) else {}

    def update_sheet_values(
        self,
        spreadsheet_id: str,
        range_a1: str,
        values: list[list[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        """Overwrite cell values in a specific Google Sheet A1 range.

        Args:
            spreadsheet_id: Target Google Spreadsheet ID.
            range_a1: Target A1 range (e.g. 'Budget!B2:B2').
            values: 2D matrix of replacement cell values.
            value_input_option: Google Sheets input mode ('USER_ENTERED' or 'RAW').

        Returns:
            Raw Google Sheets update response dictionary.

        Raises:
            WorkspaceSuiteDisabledError: When google_docs_suite is False.
        """
        client = self._require_sheets_client()
        resp = (
            client.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption=value_input_option,
                body={"values": [list(row) for row in values]},
            )
            .execute()
        )
        return dict(resp) if isinstance(resp, dict) else {}

    def create_presentation(self, title: str) -> str:
        """Create a Google Slides presentation and return its presentationId.

        Args:
            title: Presentation title.

        Returns:
            Created Google Slides presentationId string.

        Raises:
            WorkspaceSuiteDisabledError: When google_docs_suite is False.
        """
        client = self._require_slides_client()
        created = client.presentations().create(body={"title": title}).execute()
        return str(created["presentationId"])

    def create_form(self, title: str, document_title: str = "") -> str:
        """Create a Google Form and return its formId.

        Args:
            title: Public form title.
            document_title: Optional Drive file title (defaults to title).

        Returns:
            Created Google Forms formId string.

        Raises:
            WorkspaceSuiteDisabledError: When google_docs_suite is False.
        """
        client = self._require_forms_client()
        info = {"title": title, "documentTitle": document_title or title}
        created = client.forms().create(body={"info": info}).execute()
        return str(created["formId"])

    def list_form_responses(self, form_id: str) -> list[dict[str, Any]]:
        """List submitted responses on a Google Form read-only.

        Args:
            form_id: Target Google Forms formId.

        Returns:
            List of response dictionaries.

        Raises:
            WorkspaceSuiteDisabledError: When google_docs_suite is False.
        """
        client = self._require_forms_client()
        resp = client.forms().responses().list(formId=form_id).execute()
        raw_list = resp.get("responses", []) if isinstance(resp, dict) else []
        return [dict(item) for item in raw_list if isinstance(item, dict)]

    def _ensure_enabled(self) -> None:
        """Raise WorkspaceSuiteDisabledError when google_docs_suite is False."""
        if not self.is_enabled():
            raise WorkspaceSuiteDisabledError(
                "Google Workspace Suite ('google_docs_suite') is disabled in settings.yaml."
            )

    def _require_docs_client(self) -> Any:
        """Verify suite is enabled and return an initialized Google Docs v1 client."""
        self._ensure_enabled()
        if self._docs_client is None:
            self._docs_client = self._oauth.build_api_client("docs", "v1")
        return self._docs_client

    def _require_sheets_client(self) -> Any:
        """Verify suite is enabled and return an initialized Google Sheets v4 client."""
        self._ensure_enabled()
        if self._sheets_client is None:
            self._sheets_client = self._oauth.build_api_client("sheets", "v4")
        return self._sheets_client

    def _require_slides_client(self) -> Any:
        """Verify suite is enabled and return an initialized Google Slides v1 client."""
        self._ensure_enabled()
        if self._slides_client is None:
            self._slides_client = self._oauth.build_api_client("slides", "v1")
        return self._slides_client

    def _require_forms_client(self) -> Any:
        """Verify suite is enabled and return an initialized Google Forms v1 client."""
        self._ensure_enabled()
        if self._forms_client is None:
            self._forms_client = self._oauth.build_api_client("forms", "v1")
        return self._forms_client
