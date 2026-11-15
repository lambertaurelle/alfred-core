"""OAuth 2.1 PKCE + Streamable HTTP /mcp bridge service for Gemini App integration."""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from alfred.domain.models import (
    ActiveSessionRecord,
    AlfredPaths,
    AlfredSettings,
    KnowledgeDocument,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.mcp_oauth_helper import (
    MCP_TOOLS_SPEC,
    McpOAuthState,
    build_discovery_payload,
    build_redirect_with_params,
    json_http_reply,
    parse_form_or_json_body,
    render_authorize_html_form,
    verify_pbkdf2_pin_secret,
    write_pbkdf2_pin_secret,
)

MCP_PROTOCOL_VERSION = "2025-03-26"
OAUTH_SECRET_FILENAME = "mcp_oauth.json"
PROTECTED_RESOURCE_ROUTES = frozenset(
    {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}
)
AUTH_SERVER_ROUTES = frozenset(
    {
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
        "/.well-known/openid-configuration",
    }
)


class _McpHttpServer(ThreadingHTTPServer):
    """ThreadingHTTPServer bound to a McpBridgeService instance."""

    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], bridge: McpBridgeService) -> None:
        """Initialize the multithreaded HTTP server with its owning bridge service."""
        self.bridge = bridge
        super().__init__(server_address, _McpRequestHandler)


class _McpRequestHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 request handler delegating OAuth 2.1 and Streamable MCP routes."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Handle GET discovery and OAuth authorization form requests."""
        srv = self.server
        assert isinstance(srv, _McpHttpServer)
        req_headers = {k.lower(): v for k, v in self.headers.items()}
        addr = (str(srv.server_address[0]), int(srv.server_address[1]))
        status, headers, body = srv.bridge.handle_http_get(self.path, req_headers, addr)
        self._send_framed_reply(status, headers, body)

    def do_POST(self) -> None:
        """Handle POST OAuth registration/authorize/token and /mcp JSON-RPC requests."""
        srv = self.server
        assert isinstance(srv, _McpHttpServer)
        req_headers = {k.lower(): v for k, v in self.headers.items()}
        length = max(0, int(req_headers.get("content-length", "0") or "0"))
        raw_body = self.rfile.read(length) if length > 0 else b""
        addr = (str(srv.server_address[0]), int(srv.server_address[1]))
        status, headers, body = srv.bridge.handle_http_post(self.path, req_headers, raw_body, addr)
        self._send_framed_reply(status, headers, body)

    def _send_framed_reply(self, status: int, headers: dict[str, str], body: bytes) -> None:
        """Write status, explicit Content-Length, headers, and raw payload bytes."""
        self.send_response(status)
        for key, val in headers.items():
            if key.lower() != "content-length":
                self.send_header(key, val)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default stderr HTTP logging to avoid leaking OAuth codes or tokens.

        Args:
            format: Standard printf-style log format string.
            *args: Positional interpolation arguments.
        """
        _ = (format, args)


class McpBridgeService:
    """OAuth 2.1 PKCE S256 + MCP 2025-03-26 Streamable HTTP bridge for Gemini App."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore | None = None,
        agy_manager: object = None,
        knowledge_service: object = None,
    ) -> None:
        """Initialize the MCP bridge service with local state, session, and knowledge deps.

        Args:
            paths: Resolved AlfredPaths layout.
            settings: Runtime settings including McpServerConfig and ModelRoutingConfig.
            state_store: Optional local atomic StateStore (created from paths when None).
            agy_manager: AgySessionManager or compatible session dispatcher.
            knowledge_service: KnowledgeService or compatible 3-tier knowledge provider.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store or StateStore(paths)
        self._agy = agy_manager
        self._knowledge = knowledge_service
        self._oauth = McpOAuthState()

    @property
    def secret_path(self) -> Path:
        """Return the PBKDF2 OAuth PIN secret JSON path inside instance/secrets."""
        return self._paths.secrets_dir / OAUTH_SECRET_FILENAME

    def setup_credentials(self, admin_pin: str = "", *, pin: str = "") -> Path:
        """Write PBKDF2-HMAC-SHA256 admin PIN credentials to instance/secrets/mcp_oauth.json.

        Args:
            admin_pin: Administrator PIN string used to authorize OAuth 2.1 consent.
            pin: Optional keyword alias for admin_pin.
        """
        return write_pbkdf2_pin_secret(self.secret_path, (admin_pin or pin).strip())

    def resolve_base_url(
        self,
        headers: Mapping[str, str],
        fallback_host: str = "127.0.0.1",
        fallback_port: int = 8765,
    ) -> str:
        """Resolve canonical public origin from settings.mcp.public_url or proxy headers.

        Args:
            headers: Lowercase HTTP request headers mapping.
            fallback_host: Bound TCP host fallback when Host header is absent.
            fallback_port: Bound TCP port fallback when Host header is absent.
        """
        configured = self._settings.mcp.public_url.strip().rstrip("/")
        if configured:
            return configured
        proto = headers.get("x-forwarded-proto", "http").split(",")[0].strip() or "http"
        host_raw = headers.get("x-forwarded-host") or headers.get("host") or ""
        host = host_raw.split(",")[0].strip() or f"{fallback_host}:{fallback_port}"
        return f"{proto}://{host}".rstrip("/")

    def create_http_server(
        self, host: str | None = None, port: int | None = None
    ) -> ThreadingHTTPServer:
        """Create a bound ThreadingHTTPServer exposing OAuth 2.1 and POST /mcp routes.

        Args:
            host: Optional bind address override (defaults to settings.mcp.host).
            port: Optional TCP port override (defaults to settings.mcp.port; 0 for ephemeral).
        """
        bind_host = self._settings.mcp.host if host is None else host
        bind_port = self._settings.mcp.port if port is None else port
        return _McpHttpServer((bind_host, bind_port), self)

    def serve_forever(self, host: str | None = None, port: int | None = None) -> None:
        """Run the MCP HTTP server synchronously until interrupted.

        Args:
            host: Optional bind address override.
            port: Optional bind TCP port override.
        """
        server = self.create_http_server(host=host, port=port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()

    def handle_http_get(
        self, raw_path: str, headers: Mapping[str, str], server_addr: tuple[str, int]
    ) -> tuple[int, dict[str, str], bytes]:
        """Dispatch HTTP GET routes for RFC 9728/8414 discovery and PIN consent form.

        Args:
            raw_path: Raw HTTP request target including optional query string.
            headers: Lowercase HTTP request headers.
            server_addr: Bound (host, port) tuple.
        """
        parsed = urllib.parse.urlparse(raw_path)
        route = parsed.path.rstrip("/") or "/"
        base = self.resolve_base_url(headers, server_addr[0], server_addr[1])
        if route in PROTECTED_RESOURCE_ROUTES:
            return json_http_reply(200, build_discovery_payload(base, auth_server=False))
        if route in AUTH_SERVER_ROUTES:
            return json_http_reply(200, build_discovery_payload(base, auth_server=True))
        if route == "/oauth/authorize":
            return self._handle_authorize_get(parsed.query)
        return json_http_reply(404, {"error": "not_found"})

    def handle_http_post(
        self,
        raw_path: str,
        headers: Mapping[str, str],
        raw_body: bytes,
        server_addr: tuple[str, int],
    ) -> tuple[int, dict[str, str], bytes]:
        """Dispatch HTTP POST routes for OAuth 2.1 endpoints and Streamable /mcp.

        Args:
            raw_path: Raw HTTP request target.
            headers: Lowercase HTTP request headers.
            raw_body: Raw request payload bytes.
            server_addr: Bound (host, port) tuple.
        """
        route = urllib.parse.urlparse(raw_path).path.rstrip("/") or "/"
        if route == "/oauth/register":
            return self._handle_register_post(raw_body)
        if route == "/oauth/authorize":
            return self._handle_authorize_post(raw_body, headers.get("content-type", ""))
        if route == "/oauth/token":
            return self._handle_token_post(raw_body, headers.get("content-type", ""))
        if route == "/mcp":
            base = self.resolve_base_url(headers, server_addr[0], server_addr[1])
            return self._handle_mcp_post(headers, raw_body, base)
        return json_http_reply(404, {"error": "not_found"})

    def _handle_authorize_get(self, query_string: str) -> tuple[int, dict[str, str], bytes]:
        """Validate OAuth authorize query parameters and render the HTML PIN form."""
        raw_qs = urllib.parse.parse_qs(query_string, keep_blank_values=True)
        qs = {k: vals[0] for k, vals in raw_qs.items() if vals}
        client_id, redirect_uri = qs.get("client_id", ""), qs.get("redirect_uri", "")
        if (
            qs.get("response_type") != "code"
            or qs.get("code_challenge_method") != "S256"
            or not qs.get("code_challenge", "").strip()
            or not self._oauth.validate_client_redirect(client_id, redirect_uri)
        ):
            return json_http_reply(400, {"error": "invalid_request"})
        resp_headers = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}
        return (200, resp_headers, render_authorize_html_form(qs))

    def _handle_register_post(self, raw_body: bytes) -> tuple[int, dict[str, str], bytes]:
        """Handle RFC 7591 dynamic client registration with redirect URI allowlist."""
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return json_http_reply(400, {"error": "invalid_client_metadata"})
        if not isinstance(parsed, dict):
            return json_http_reply(400, {"error": "invalid_client_metadata"})
        raw_uris = parsed.get("redirect_uris")
        is_str_list = isinstance(raw_uris, list) and bool(raw_uris) and all(
            isinstance(u, str) for u in raw_uris
        )
        if not is_str_list or not isinstance(raw_uris, list):
            return json_http_reply(400, {"error": "invalid_redirect_uri"})
        client_name = str(parsed.get("client_name") or "Gemini Custom MCP")
        registered = self._oauth.register_client([str(u) for u in raw_uris], client_name)
        if registered is None:
            return json_http_reply(400, {"error": "invalid_redirect_uri"})
        return json_http_reply(201, registered)

    def _handle_authorize_post(
        self, raw_body: bytes, content_type: str
    ) -> tuple[int, dict[str, str], bytes]:
        """Verify PBKDF2 PIN and redirect with 1-shot code + verbatim state."""
        form = parse_form_or_json_body(raw_body, content_type)
        client_id, redirect_uri = form.get("client_id", ""), form.get("redirect_uri", "")
        challenge = form.get("code_challenge", "")
        if (
            form.get("response_type") != "code"
            or form.get("code_challenge_method") != "S256"
            or not challenge.strip()
            or not self._oauth.validate_client_redirect(client_id, redirect_uri)
        ):
            return json_http_reply(400, {"error": "invalid_request"})
        if not verify_pbkdf2_pin_secret(self.secret_path, form.get("pin", "")):
            return json_http_reply(403, {"error": "access_denied"})
        code = self._oauth.issue_authorization_code(client_id, redirect_uri, challenge)
        redirect_params: dict[str, str] = {"code": code}
        if "state" in form:
            redirect_params["state"] = form["state"]
        location = build_redirect_with_params(redirect_uri, redirect_params)
        return (302, {"Location": location, "Cache-Control": "no-store"}, b"")

    def _handle_token_post(
        self, raw_body: bytes, content_type: str
    ) -> tuple[int, dict[str, str], bytes]:
        """Exchange an authorization_code (PKCE S256) or rotate a refresh_token."""
        form = parse_form_or_json_body(raw_body, content_type)
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            tokens = self._oauth.exchange_authorization_code(
                form.get("code", ""),
                form.get("client_id", ""),
                form.get("redirect_uri", ""),
                form.get("code_verifier", ""),
            )
        elif grant_type == "refresh_token":
            tokens = self._oauth.exchange_refresh_token(
                form.get("refresh_token", ""), form.get("client_id", "")
            )
        else:
            return json_http_reply(400, {"error": "unsupported_grant_type"})
        if tokens is None:
            return json_http_reply(400, {"error": "invalid_grant"})
        return json_http_reply(200, tokens)

    def _handle_mcp_post(
        self, headers: Mapping[str, str], raw_body: bytes, base_url: str
    ) -> tuple[int, dict[str, str], bytes]:
        """Authenticate bearer token and dispatch synchronous JSON-RPC 2.0 MCP calls."""
        if not self._oauth.validate_bearer_token(headers.get("authorization", "")):
            www_auth = f'Bearer resource_metadata="{base_url}/.well-known/oauth-protected-resource"'
            return json_http_reply(401, {"error": "unauthorized"}, {"WWW-Authenticate": www_auth})
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            err = {"code": -32700, "message": "Parse error"}
            return json_http_reply(400, {"jsonrpc": "2.0", "id": None, "error": err})
        if not isinstance(payload, dict):
            err = {"code": -32600, "message": "Invalid Request"}
            return json_http_reply(400, {"jsonrpc": "2.0", "id": None, "error": err})
        return json_http_reply(200, self._dispatch_jsonrpc(payload))

    def _dispatch_jsonrpc(self, payload: dict[str, object]) -> dict[str, object]:
        """Route a parsed JSON-RPC 2.0 MCP envelope to initialize, list, or call."""
        rpc_id = payload.get("id")
        method = str(payload.get("method") or "")
        raw_params = payload.get("params")
        params = dict(raw_params) if isinstance(raw_params, dict) else {}
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "alfred-mcp-bridge", "version": "8.0.0"},
            }
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        if method in {"notifications/initialized", "ping"}:
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": list(MCP_TOOLS_SPEC)}}
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": rpc_id, "result": self._dispatch_tool_call(params)}
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def _dispatch_tool_call(self, params: dict[str, object]) -> dict[str, object]:
        """Execute one of the 4 registered Alfred MCP tools and wrap text content."""
        name = str(params.get("name") or "")
        raw_args = params.get("arguments")
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        handlers = {
            "alfred_life_brain_dump": self._tool_life_brain_dump,
            "alfred_session_status": self._tool_session_status,
            "alfred_approve_plan": self._tool_approve_plan,
            "alfred_quick_context": self._tool_quick_context,
        }
        fn = handlers.get(name)
        if fn is None:
            err_block = [{"type": "text", "text": f"Outil inconnu : {name}"}]
            return {"content": err_block, "isError": True}
        return {"content": [{"type": "text", "text": fn(args)}], "isError": False}

    def _tool_life_brain_dump(self, args: dict[str, object]) -> str:
        """Spawn an alfred-life orchestrator session guarded by sync_wait_seconds."""
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return "Prompt vide : aucune session ouverte."
        title = str(args.get("title") or f"[MCP] {prompt[:60]}").strip()
        workspace = self._settings.resolve_workspace_project("life")
        model_id, _ = self._settings.models.resolve_for_workspace("life")
        wait_sec = max(0.5, float(self._settings.mcp.sync_wait_seconds) - 0.5)
        holder: dict[str, ActiveSessionRecord] = {}

        def _worker() -> None:
            """Invoke real AgySessionManager or test session dispatcher in background."""
            if isinstance(self._agy, AgySessionManager):
                plan_md = f"# {title}\n\n## Brain Dump (Gemini MCP)\n\n{prompt}\n"
                holder["record"] = self._agy.create_session(
                    workspace=workspace,
                    implementation_plan_md=plan_md,
                    spawn_live=True,
                    title=title,
                )
            elif self._agy is not None:
                spawn_fn: Any = getattr(self._agy, "spawn_session", None)
                if callable(spawn_fn):
                    rec = spawn_fn(workspace=workspace, model=model_id, title=title, prompt=prompt)
                    if isinstance(rec, ActiveSessionRecord):
                        holder["record"] = rec

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(timeout=wait_sec)
        if "record" in holder:
            rec = holder["record"]
            return f"Session ouverte : {rec.session_id} ({rec.workspace}) — {rec.title}"
        return (
            f"Session {workspace} ({model_id}) lancée en arrière-plan pour '{title}'. "
            "Interrogez alfred_session_status pour suivre l'avancement."
        )

    def _tool_session_status(self, args: dict[str, object]) -> str:
        """Read session status from StateStore."""
        session_id = str(args.get("session_id") or "").strip()
        sessions = self._state_store.conversations.load().sessions
        if session_id:
            rec = sessions.get(session_id)
            if rec is None:
                return f"Session '{session_id}' introuvable dans l'état local."
            return json.dumps(rec.model_dump(mode="json"), ensure_ascii=False)
        summary = [r.model_dump(mode="json") for r in list(sessions.values())[-10:]]
        return json.dumps({"count": len(sessions), "sessions": summary}, ensure_ascii=False)

    def _tool_approve_plan(self, args: dict[str, object]) -> str:
        """Record Gate approval/feedback on an active Antigravity session."""
        session_id = str(args.get("session_id") or "").strip()
        feedback = str(args.get("feedback") or "Proceed").strip()
        if not session_id:
            return "Paramètre session_id requis."
        wait_sec = max(0.5, float(self._settings.mcp.sync_wait_seconds) - 0.5)
        if self._agy is not None and not isinstance(self._agy, AgySessionManager):
            approve_fn: Any = getattr(self._agy, "approve_session", None)
            if callable(approve_fn):
                worker = threading.Thread(
                    target=lambda: approve_fn(session_id=session_id, feedback=feedback),
                    daemon=True,
                )
                worker.start()
                worker.join(timeout=wait_sec)
        self._state_store.conversations.update(
            lambda st: (
                st.model_copy(
                    update={
                        "sessions": {
                            **st.sessions,
                            session_id: st.sessions[session_id].model_copy(
                                update={"status": "APPROVED", "outcome": f"Approved: {feedback}"}
                            ),
                        }
                    }
                )
                if session_id in st.sessions
                else st
            )
        )
        return f"Session {session_id} approuvée ({feedback})."

    def _tool_quick_context(self, args: dict[str, object]) -> str:
        """Query Tier-1/Tier-2 reference knowledge documents matching query."""
        query = str(args.get("query") or "").strip()
        docs: list[KnowledgeDocument] = []
        if isinstance(self._knowledge, KnowledgeService):
            hits = (
                self._knowledge.select_relevant_frontmatters(query)
                if query
                else self._knowledge.scan_frontmatters()
            )
            if not hits:
                hits = self._knowledge.scan_frontmatters()
            for fname, _fm in hits[:3]:
                docs.append(self._knowledge.load_document(fname))
        elif self._knowledge is not None:
            search_fn: Any = getattr(self._knowledge, "search", None)
            if callable(search_fn):
                raw_docs = search_fn(query)
                if isinstance(raw_docs, list):
                    docs = [d for d in raw_docs if isinstance(d, KnowledgeDocument)]
        if not docs:
            return f"Aucune fiche de référence trouvée pour '{query}'."
        blocks = [
            f"### {d.frontmatter.title} (`{d.frontmatter.id}`)\n"
            f"{d.frontmatter.summary}\n\n{d.body.strip()}"
            for d in docs
        ]
        return "\n\n---\n\n".join(blocks)
