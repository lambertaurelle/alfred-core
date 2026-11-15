"""Google People API v1 connector for Family Office contact lookup and post-Gate creation."""

from __future__ import annotations

import contextlib
from typing import Any

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.domain.tasks_models import ContactSummary
from alfred.services.google_auth import GoogleOAuthService

PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations"
OTHER_CONTACT_FIELDS = "names,emailAddresses,phoneNumbers"


class ContactsFeatureDisabledError(RuntimeError):
    """Raised when Google Contacts operations are invoked while contacts_lookup is False."""


class ContactsService:
    """Google People v1 adapter supporting search, get, and post-Gate contact creation."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        people_client: Any | None = None,
        oauth_service: GoogleOAuthService | None = None,
    ) -> None:
        """Initialize the Google Contacts service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including features.contacts_lookup.
            people_client: Optional injected Google People v1 discovery resource.
            oauth_service: Optional shared GoogleOAuthService instance.
        """
        self._paths = paths
        self._settings = settings
        self._people_client = people_client
        self._oauth = oauth_service or GoogleOAuthService(paths, settings)

    def is_enabled(self) -> bool:
        """Return True when contacts_lookup is enabled in settings.features."""
        return bool(self._settings.features.contacts_lookup)

    def search_contacts(
        self,
        query: str,
        page_size: int = 10,
        include_other_contacts: bool = True,
    ) -> list[ContactSummary]:
        """Search personal Google Contacts and optional Other Contacts matching query.

        Args:
            query: Name, email, phone, or organization search string.
            page_size: Maximum number of contacts to retrieve per directory source.
            include_other_contacts: When True, also query otherContacts.search read-only.

        Returns:
            Deduplicated list of normalized ContactSummary items.

        Raises:
            ContactsFeatureDisabledError: When contacts_lookup is False.
        """
        client = self._require_enabled_client()
        limit = max(1, min(30, int(page_size)))
        seen: set[str] = set()
        results: list[ContactSummary] = []

        resp = (
            client.people()
            .searchContacts(query=query, pageSize=limit, readMask=PERSON_FIELDS)
            .execute()
        )
        for item in (resp.get("results", []) if isinstance(resp, dict) else []):
            person = item.get("person", item) if isinstance(item, dict) else None
            if isinstance(person, dict):
                parsed = self._parse_person(person)
                if parsed.resource_name and parsed.resource_name not in seen:
                    seen.add(parsed.resource_name)
                    results.append(parsed)

        if include_other_contacts:
            with contextlib.suppress(Exception):
                other_resp = (
                    client.otherContacts()
                    .search(query=query, pageSize=limit, readMask=OTHER_CONTACT_FIELDS)
                    .execute()
                )
                for item in (other_resp.get("results", []) if isinstance(other_resp, dict) else []):
                    person = item.get("person", item) if isinstance(item, dict) else None
                    if isinstance(person, dict):
                        parsed = self._parse_person(person)
                        if parsed.resource_name and parsed.resource_name not in seen:
                            seen.add(parsed.resource_name)
                            results.append(parsed)

        return results[:limit]

    def get_contact(self, resource_name: str) -> ContactSummary:
        """Fetch a single Google Contact by resourceName (including etag).

        Args:
            resource_name: People API resourceName (e.g. 'people/c101').

        Returns:
            Normalized ContactSummary record.

        Raises:
            ContactsFeatureDisabledError: When contacts_lookup is False.
        """
        client = self._require_enabled_client()
        norm_name = (
            resource_name
            if resource_name.startswith(("people/", "otherContacts/"))
            else f"people/{resource_name}"
        )
        raw = client.people().get(resourceName=norm_name, personFields=PERSON_FIELDS).execute()
        return self._parse_person(raw if isinstance(raw, dict) else {})

    def create_contact(
        self,
        given_name: str,
        family_name: str = "",
        email: str = "",
        phone: str = "",
        organization: str = "",
    ) -> ContactSummary:
        """Create a new Google Contact strictly post-Gate validation.

        Args:
            given_name: Contact first/given name.
            family_name: Optional contact family/last name.
            email: Optional primary email address.
            phone: Optional primary phone number.
            organization: Optional organization or firm name.

        Returns:
            Created ContactSummary including server-assigned resource_name and etag.

        Raises:
            ContactsFeatureDisabledError: When contacts_lookup is False.
        """
        client = self._require_enabled_client()
        name_entry: dict[str, str] = {"givenName": given_name}
        if family_name:
            name_entry["familyName"] = family_name
        body: dict[str, Any] = {"names": [name_entry]}
        if email:
            body["emailAddresses"] = [{"value": email}]
        if phone:
            body["phoneNumbers"] = [{"value": phone}]
        if organization:
            body["organizations"] = [{"name": organization}]
        created = (
            client.people()
            .createContact(body=body, personFields=PERSON_FIELDS)
            .execute()
        )
        return self._parse_person(created if isinstance(created, dict) else {})

    def _require_enabled_client(self) -> Any:
        """Verify contacts_lookup is enabled and return an initialized People v1 client."""
        if not self.is_enabled():
            raise ContactsFeatureDisabledError(
                "Google Contacts feature ('contacts_lookup') is disabled in settings.yaml."
            )
        if self._people_client is None:
            self._people_client = self._oauth.build_api_client("people", "v1")
        return self._people_client

    def _parse_person(self, raw: dict[str, Any]) -> ContactSummary:
        """Normalize a raw Google People v1 Person dictionary into a ContactSummary."""
        resource_name = str(raw.get("resourceName", ""))
        etag = str(raw.get("etag", ""))
        emails = [
            str(entry.get("value", "")).strip()
            for entry in raw.get("emailAddresses", [])
            if isinstance(entry, dict) and str(entry.get("value", "")).strip()
        ]
        phones = [
            str(entry.get("value", "")).strip()
            for entry in raw.get("phoneNumbers", [])
            if isinstance(entry, dict) and str(entry.get("value", "")).strip()
        ]
        display_name = ""
        names = raw.get("names", [])
        if isinstance(names, list) and names and isinstance(names[0], dict):
            first = names[0]
            display_name = str(first.get("displayName", "")).strip()
            if not display_name:
                combined = f"{first.get('givenName', '')} {first.get('familyName', '')}".strip()
                display_name = combined
        if not display_name:
            display_name = emails[0] if emails else resource_name

        organization = ""
        orgs = raw.get("organizations", [])
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
            organization = str(orgs[0].get("name", "")).strip()

        return ContactSummary(
            resource_name=resource_name,
            display_name=display_name,
            emails=emails,
            phones=phones,
            organization=organization,
            etag=etag,
        )
