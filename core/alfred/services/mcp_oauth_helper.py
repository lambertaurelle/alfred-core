"""OAuth 2.1 PKCE S256, PBKDF2 PIN storage, and tool schemas for McpBridgeService."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from alfred.domain.models import utc_now_iso

KDF_NAME = "pbkdf2_hmac_sha256"
PBKDF2_ITERATIONS = 210_000
AUTH_CODE_TTL_SECONDS = 300.0
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 86400 * 30
GOOGLE_CUSTOM_MCP_REDIRECT_PREFIX = (
    "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp"
)

MCP_TOOLS_SPEC: tuple[dict[str, object], ...] = (
    {
        "name": "alfred_life_brain_dump",
        "description": "Soumettre une demande ou un brain dump au workspace alfred-life.",
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}, "title": {"type": "string"}},
            "required": ["prompt"],
        },
    },
    {
        "name": "alfred_session_status",
        "description": "Consulter le statut d'une session Antigravity active ou archivée.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}},
    },
    {
        "name": "alfred_approve_plan",
        "description": "Valider ou annoter le plan Gate d'une session Antigravity.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "feedback": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "alfred_quick_context",
        "description": "Interroger le référentiel de connaissances 3-tiers d'Alfred.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
)


@dataclass(frozen=True)
class RegisteredOAuthClient:
    """Dynamically registered OAuth 2.1 public client metadata."""

    client_id: str
    redirect_uris: tuple[str, ...]
    client_name: str = "Gemini Custom MCP"


@dataclass(frozen=True)
class AuthorizationCodeGrant:
    """Single-use OAuth 2.1 authorization code bound to a PKCE S256 challenge."""

    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    expires_at: float


@dataclass(frozen=True)
class TokenRecord:
    """Issued OAuth 2.1 bearer or refresh token with monotonic expiration."""

    token: str
    client_id: str
    expires_at: float


def write_pbkdf2_pin_secret(secret_path: Path, admin_pin: str) -> Path:
    """Persist PBKDF2-HMAC-SHA256 admin PIN credentials with 0600 permissions.

    Args:
        secret_path: Target path for instance/secrets/mcp_oauth.json.
        admin_pin: Non-empty administrator PIN to hash and never store in plaintext.
    """
    cleaned_pin = admin_pin.strip()
    if not cleaned_pin:
        raise ValueError("MCP admin PIN must not be empty.")
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", cleaned_pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    payload = {
        "kdf": KDF_NAME,
        "iterations": PBKDF2_ITERATIONS,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "pin_hash_b64": base64.b64encode(derived).decode("ascii"),
        "updated_at": utc_now_iso(),
    }
    serialized = json.dumps(payload, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(secret_path.parent), prefix=".mcp_oauth_")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(tmp_path, secret_path)
        os.chmod(secret_path, 0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return secret_path


def verify_pbkdf2_pin_secret(secret_path: Path, candidate_pin: str) -> bool:
    """Verify a submitted admin PIN against PBKDF2-HMAC-SHA256 credentials in constant time.

    Args:
        secret_path: Path to mcp_oauth.json.
        candidate_pin: Submitted PIN string from POST /oauth/authorize.
    """
    if not candidate_pin or not secret_path.is_file():
        return False
    try:
        raw = json.loads(secret_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("kdf") != KDF_NAME:
            return False
        iterations = int(raw["iterations"])
        salt = base64.b64decode(str(raw["salt_b64"]))
        expected = base64.b64decode(str(raw["pin_hash_b64"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    computed = hashlib.pbkdf2_hmac("sha256", candidate_pin.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(computed, expected)


def is_allowed_redirect_uri(uri: str) -> bool:
    """Validate an OAuth 2.1 redirect URI against the Gemini and localhost allowlist.

    Args:
        uri: Candidate redirect URI string.
    """
    cleaned = uri.strip()
    if not cleaned:
        return False
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme == "https":
        if cleaned.startswith(GOOGLE_CUSTOM_MCP_REDIRECT_PREFIX):
            return parsed.hostname == "oauth-redirect.googleusercontent.com"
        return parsed.hostname == "gemini.google.com"
    return bool(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"})


def verify_pkce_s256(code_verifier: str, expected_challenge: str) -> bool:
    """Verify an RFC 7636 S256 PKCE code_verifier against its stored code_challenge.

    Args:
        code_verifier: Raw ASCII PKCE verifier submitted to POST /oauth/token.
        expected_challenge: Stored URL-safe base64 SHA-256 challenge without padding.
    """
    if not code_verifier or not expected_challenge:
        return False
    try:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    except UnicodeEncodeError:
        return False
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, expected_challenge)


def build_redirect_with_params(redirect_uri: str, params: dict[str, str]) -> str:
    """Append URL-encoded OAuth parameters to redirect_uri while preserving verbatim state.

    Args:
        redirect_uri: Validated target redirect URI.
        params: Query key-value pairs (e.g. code and verbatim state).
    """
    parsed = urllib.parse.urlparse(redirect_uri)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    merged = [*existing, *[(k, v) for k, v in params.items() if v != "" or k == "state"]]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(merged)))


def render_authorize_html_form(params: dict[str, str], error_message: str = "") -> bytes:
    """Render a minimal HTML PIN consent form for GET /oauth/authorize.

    Args:
        params: OAuth query parameters echoed as hidden form fields.
        error_message: Optional human-readable error banner.
    """
    hidden_fields = "\n".join(
        f'<input type="hidden" name="{html.escape(k, quote=True)}" '
        f'value="{html.escape(v, quote=True)}" />'
        for k, v in params.items()
    )
    err_html = f"<p style='color:#b00020'>{html.escape(error_message)}</p>" if error_message else ""
    doc = (
        "<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>"
        "<title>Alfred MCP — Autorisation OAuth 2.1</title></head><body>"
        f"<h1>Alfred Chief of Staff — Pont MCP</h1>{err_html}"
        f"<form method='post' action='/oauth/authorize'>{hidden_fields}"
        "<label for='pin'>Code PIN Administrateur :</label> "
        "<input id='pin' name='pin' type='password' autocomplete='off' required /> "
        "<button type='submit'>Autoriser Gemini</button></form></body></html>"
    )
    return doc.encode("utf-8")


def json_http_reply(
    status: int,
    payload: dict[str, object],
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Encode a JSON HTTP response tuple with UTF-8 Content-Type and optional headers.

    Args:
        status: HTTP status code.
        payload: JSON-serializable response dictionary.
        extra_headers: Optional extra HTTP headers (e.g. WWW-Authenticate).
    """
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    return (status, headers, raw)


def parse_form_or_json_body(raw_body: bytes, content_type: str) -> dict[str, str]:
    """Decode application/x-www-form-urlencoded or flat JSON request bodies.

    Args:
        raw_body: Raw HTTP request body bytes.
        content_type: Request Content-Type header string.
    """
    text = raw_body.decode("utf-8", errors="replace")
    if "application/json" in content_type.lower():
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v is not None}
        return {}
    qs = urllib.parse.parse_qs(text, keep_blank_values=True)
    return {k: vals[0] for k, vals in qs.items() if vals}


def build_discovery_payload(base_url: str, *, auth_server: bool) -> dict[str, object]:
    """Build RFC 9728 protected-resource or RFC 8414 authorization-server JSON metadata.

    Args:
        base_url: Canonical public scheme://authority base URL without trailing slash.
        auth_server: True for RFC 8414 authorization server metadata, False for RFC 9728.
    """
    if not auth_server:
        return {
            "resource": f"{base_url}/mcp",
            "authorization_servers": [base_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp:tools"],
        }
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp:tools"],
    }


class McpOAuthState:
    """Thread-safe OAuth 2.1 registry for DCR clients, one-shot PKCE codes, and tokens."""

    def __init__(self) -> None:
        """Initialize empty thread-safe OAuth 2.1 state containers."""
        self._lock = threading.Lock()
        self._clients: dict[str, RegisteredOAuthClient] = {}
        self._codes: dict[str, AuthorizationCodeGrant] = {}
        self._access_tokens: dict[str, TokenRecord] = {}
        self._refresh_tokens: dict[str, TokenRecord] = {}

    def register_client(
        self, redirect_uris: list[str], client_name: str = "Gemini Custom MCP"
    ) -> dict[str, object] | None:
        """Validate redirect URIs and register a public OAuth 2.1 client.

        Args:
            redirect_uris: Candidate redirect URI strings from POST /oauth/register.
            client_name: Optional human-readable client name.
        """
        if not redirect_uris or not all(is_allowed_redirect_uri(u) for u in redirect_uris):
            return None
        client_id = f"alfred-mcp-{secrets.token_urlsafe(12)}"
        clean_uris = tuple(u.strip() for u in redirect_uris)
        record = RegisteredOAuthClient(client_id, clean_uris, client_name or "Gemini Custom MCP")
        with self._lock:
            self._clients[client_id] = record
        return {
            "client_id": client_id,
            "client_name": record.client_name,
            "redirect_uris": list(clean_uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

    def validate_client_redirect(self, client_id: str, redirect_uri: str) -> bool:
        """Check client_id and redirect_uri against allowlist and registered URIs.

        Args:
            client_id: OAuth client identifier.
            redirect_uri: Requested redirect URI.
        """
        if not client_id.strip() or not is_allowed_redirect_uri(redirect_uri):
            return False
        with self._lock:
            registered = self._clients.get(client_id)
        return redirect_uri.strip() in registered.redirect_uris if registered else True

    def issue_authorization_code(
        self, client_id: str, redirect_uri: str, code_challenge: str
    ) -> str:
        """Mint and store a single-use PKCE S256 authorization code.

        Args:
            client_id: Validated OAuth client identifier.
            redirect_uri: Validated OAuth redirect URI.
            code_challenge: S256 PKCE code challenge string.
        """
        code = secrets.token_urlsafe(32)
        grant = AuthorizationCodeGrant(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri.strip(),
            code_challenge=code_challenge.strip(),
            expires_at=time.monotonic() + AUTH_CODE_TTL_SECONDS,
        )
        with self._lock:
            self._codes[code] = grant
        return code

    def exchange_authorization_code(
        self, code: str, client_id: str, redirect_uri: str, code_verifier: str
    ) -> dict[str, object] | None:
        """Burn a one-shot authorization code, verify PKCE S256, and mint token pair.

        Args:
            code: Single-use authorization code.
            client_id: Presenting OAuth client identifier.
            redirect_uri: Redirect URI matching the authorization request.
            code_verifier: Raw ASCII PKCE verifier.
        """
        now = time.monotonic()
        with self._lock:
            grant = self._codes.pop(code, None)
        if grant is None or now > grant.expires_at:
            return None
        if grant.client_id != client_id or grant.redirect_uri != redirect_uri.strip():
            return None
        if not verify_pkce_s256(code_verifier, grant.code_challenge):
            return None
        return self._mint_token_pair(client_id)

    def exchange_refresh_token(
        self, refresh_token: str, client_id: str = ""
    ) -> dict[str, object] | None:
        """Rotate a valid refresh token and issue a fresh access/refresh token pair.

        Args:
            refresh_token: Previously issued refresh token (invalidated on use).
            client_id: Optional client identifier checked when provided.
        """
        now = time.monotonic()
        with self._lock:
            record = self._refresh_tokens.pop(refresh_token, None)
        if record is None or now > record.expires_at:
            return None
        if client_id and record.client_id != client_id:
            return None
        return self._mint_token_pair(record.client_id)

    def validate_bearer_token(self, auth_header: str) -> bool:
        """Verify an HTTP Authorization: Bearer <token> header against active tokens.

        Args:
            auth_header: Raw Authorization request header string.
        """
        parts = auth_header.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            return False
        token = parts[1].strip()
        now = time.monotonic()
        with self._lock:
            record = self._access_tokens.get(token)
            if record is None:
                return False
            if now > record.expires_at:
                self._access_tokens.pop(token, None)
                return False
        return True

    def _mint_token_pair(self, client_id: str) -> dict[str, object]:
        """Generate and store a fresh access_token + refresh_token pair."""
        now = time.monotonic()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            self._access_tokens[access] = TokenRecord(
                access, client_id, now + ACCESS_TOKEN_TTL_SECONDS
            )
            self._refresh_tokens[refresh] = TokenRecord(
                refresh, client_id, now + float(REFRESH_TOKEN_TTL_SECONDS)
            )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh,
            "scope": "mcp:tools",
        }
