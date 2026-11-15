"""TDD tests for Alfred V8.0 McpBridgeService (OAuth 2.1 PKCE + Streamable HTTP /mcp)."""

from __future__ import annotations

import base64
import functools
import hashlib
import http.client
import importlib
import json
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alfred.domain.feature_models import FeatureFlagsConfig, McpServerConfig
from alfred.domain.models import (
    ActiveSessionRecord,
    AlfredPaths,
    AlfredSettings,
    FrontmatterSchema,
    KnowledgeDocument,
)
from alfred.repositories.state_store import StateStore

GEMINI_REDIRECT_URI = (
    "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-test123"
)


@dataclass(frozen=True)
class HttpReply:
    """Captured raw HTTP response from the ephemeral localhost MCP bridge server."""

    status: int
    headers: dict[str, str]
    raw_body: bytes

    @property
    def text(self) -> str:
        """Decode the HTTP response payload as UTF-8 text."""
        return self.raw_body.decode("utf-8")

    def json_dict(self) -> dict[str, Any]:
        """Parse the HTTP response payload into a JSON object dictionary."""
        parsed: Any = json.loads(self.text)
        assert isinstance(parsed, dict)
        return parsed


def _pkce_s256(code_verifier: str) -> str:
    """Compute an RFC 7636 S256 code_challenge from an ASCII code_verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _http_request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
) -> HttpReply:
    """Send a raw HTTP request to 127.0.0.1:port without auto-following redirects."""
    req_headers: dict[str, str] = dict(headers or {})
    payload: bytes | None = None
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        payload = urllib.parse.urlencode(form_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if payload is not None:
        req_headers["Content-Length"] = str(len(payload))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10.0)
    try:
        conn.request(method, path, body=payload, headers=req_headers)
        resp = conn.getresponse()
        return HttpReply(resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read())
    finally:
        conn.close()


class StubAgyManager:
    """Test double for AntigravitySessionManager recording workspace/model routing and delays."""

    def __init__(self, state_store: StateStore) -> None:
        """Initialize stub session manager backed by a real temporary StateStore."""
        self._state_store = state_store
        self.calls: list[dict[str, Any]] = []
        self.delay_seconds: float = 0.0
        self._counter = 0

    def _dispatch(self, action: str, *args: object, **kwargs: object) -> ActiveSessionRecord:
        """Record an invocation, optionally sleep for slow simulation, and update state."""
        self._counter += 1
        self.calls.append({"action": action, "args": list(args), "kwargs": dict(kwargs)})
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        session_id = str(kwargs.get("session_id") or f"sess-mcp-{self._counter:03d}")
        workspace = str(kwargs.get("workspace") or (args[0] if args else "alfred-life"))
        status = "APPROVED" if "approve" in action or "continue" in action else "ACTIVE"
        session = ActiveSessionRecord(
            session_id=session_id,
            workspace=workspace,
            status=status,
            title=str(kwargs.get("title") or "Brain Dump MCP"),
            outcome=f"{action}:ok",
        )
        self._state_store.conversations.update(
            lambda st: st.model_copy(update={"sessions": {**st.sessions, session_id: session}})
        )
        return session

    def spawn_session(self, *args: object, **kwargs: object) -> ActiveSessionRecord:
        """Simulate spawning an Antigravity specialist session."""
        return self._dispatch("spawn_session", *args, **kwargs)

    def __getattr__(self, name: str) -> Callable[..., ActiveSessionRecord]:
        """Fallback dispatcher for continue/approve/resume session method names."""
        return functools.partial(self._dispatch, name)


class StubKnowledgeService:
    """Test double for KnowledgeService providing deterministic Tier-1/Tier-2 reference data."""

    def __init__(self, paths: AlfredPaths) -> None:
        """Seed a sample reference Markdown file on disk and in memory."""
        self._doc = KnowledgeDocument(
            frontmatter=FrontmatterSchema(
                id="01_foyer_fiscal",
                title="Synthèse Foyer Fiscal",
                domain="patrimoine",
                summary="Échéances fiscales et organisation patrimoniale de référence.",
                keywords=["fiscalite", "impots"],
                drive_source_ids=["drv-fiscal-001"],
                updated_at="2026-11-15T18:00:00Z",
            ),
            body="## Synthèse\nDéclaration annuelle vérifiée.\n",
            file_name="01_foyer_fiscal.md",
            body_line_count=2,
        )
        paths.knowledge_dir.mkdir(parents=True, exist_ok=True)
        md_text = (
            "---\nid: 01_foyer_fiscal\ntitle: Synthèse Foyer Fiscal\ndomain: patrimoine\n"
            "summary: Échéances fiscales et organisation patrimoniale de référence.\n"
            "keywords: [fiscalite, impots]\ndrive_source_ids: [drv-fiscal-001]\n"
            "updated_at: '2026-11-15T18:00:00Z'\n---\n## Synthèse\nDéclaration vérifiée.\n"
        )
        (paths.knowledge_dir / "01_foyer_fiscal.md").write_text(md_text, encoding="utf-8")

    def list_frontmatters(self, *args: object, **kwargs: object) -> list[FrontmatterSchema]:
        """Return all Tier-1 YAML frontmatter headers."""
        _ = (args, kwargs)
        return [self._doc.frontmatter]

    def scan_frontmatters(self, *args: object, **kwargs: object) -> list[FrontmatterSchema]:
        """Alias returning Tier-1 YAML frontmatter headers."""
        _ = (args, kwargs)
        return [self._doc.frontmatter]

    def load_document(self, *args: object, **kwargs: object) -> KnowledgeDocument:
        """Return the seeded Tier-2 KnowledgeDocument."""
        _ = (args, kwargs)
        return self._doc

    def search(self, query: str = "", *args: object, **kwargs: object) -> list[KnowledgeDocument]:
        """Search reference documents matching query string."""
        _ = (query, args, kwargs)
        return [self._doc]


def _make_bridge(
    tmp_path: Path,
    *,
    public_url: str = "https://alfred.domaine-perso.example",
    sync_wait_seconds: int = 25,
) -> tuple[Any, AlfredPaths, StateStore, StubAgyManager]:
    """Instantiate McpBridgeService with isolated paths, settings, store, and stubs."""
    bridge_cls: Any = importlib.import_module("alfred.services.mcp_bridge_service").McpBridgeService
    paths = AlfredPaths(root_dir=tmp_path)
    settings = AlfredSettings(
        features=FeatureFlagsConfig(mcp_gemini_bridge=True),
        mcp=McpServerConfig(
            host="127.0.0.1", port=8765, public_url=public_url, sync_wait_seconds=sync_wait_seconds
        ),
    )
    store = StateStore(paths)
    agy = StubAgyManager(store)
    return bridge_cls(paths, settings, store, agy, StubKnowledgeService(paths)), paths, store, agy


@contextmanager
def _running_bridge(service: Any) -> Iterator[int]:
    """Bind an ephemeral localhost HTTP server in a daemon thread and yield its TCP port."""
    server = service.create_http_server(host="127.0.0.1", port=0)
    bound_port = int(server.server_address[1])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield bound_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5.0)


def _issue_auth_code(
    port: int, client_id: str, verifier: str, pin: str = "246810", state: str = "st-ok"
) -> HttpReply:
    """Submit POST /oauth/authorize with PKCE S256 challenge and PIN."""
    form = {
        "response_type": "code", "client_id": client_id, "redirect_uri": GEMINI_REDIRECT_URI,
        "state": state, "code_challenge": _pkce_s256(verifier),
        "code_challenge_method": "S256", "pin": pin,
    }
    return _http_request(port, "POST", "/oauth/authorize", form_body=form)


def _extract_redirect_code(reply: HttpReply) -> str:
    """Extract the authorization code query parameter from a 302 Location header."""
    assert reply.status == 302
    return urllib.parse.parse_qs(urllib.parse.urlparse(reply.headers["location"]).query)["code"][0]


def _complete_oauth_flow(port: int, admin_pin: str = "246810") -> tuple[str, str, str]:
    """Register a Gemini redirect client and complete PKCE S256 exchange returning tokens."""
    reg = _http_request(
        port, "POST", "/oauth/register", json_body={"redirect_uris": [GEMINI_REDIRECT_URI]}
    )
    assert reg.status in (200, 201)
    client_id = str(reg.json_dict()["client_id"])
    verifier = "v8-pkce-code-verifier-entropy-0123456789abcdef0123456789"
    code = _extract_redirect_code(_issue_auth_code(port, client_id, verifier, pin=admin_pin))
    form = {
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": GEMINI_REDIRECT_URI, "code_verifier": verifier,
    }
    tok = _http_request(port, "POST", "/oauth/token", form_body=form)
    assert tok.status == 200
    body = tok.json_dict()
    return client_id, str(body["access_token"]), str(body["refresh_token"])


def _call_mcp(port: int, token: str, rpc_id: int, method: str, params: dict[str, Any]) -> HttpReply:
    """Send an authenticated JSON-RPC 2.0 POST /mcp call and assert framing invariants."""
    reply = _http_request(
        port,
        "POST",
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json_body={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
    )
    assert reply.status == 200
    assert "application/json" in reply.headers.get("content-type", "")
    assert int(reply.headers["content-length"]) == len(reply.raw_body)
    return reply


def test_setup_credentials_writes_pbkdf2_secret_with_0600_mode(tmp_path: Path) -> None:
    """Verify setup_credentials writes 0600 mcp_oauth.json with PBKDF2 hash and zero plaintext."""
    service, paths, _, _ = _make_bridge(tmp_path)
    service.setup_credentials(admin_pin="246810")

    secret_file = paths.secrets_dir / "mcp_oauth.json"
    assert secret_file.is_file()
    assert (secret_file.stat().st_mode & 0o777) == 0o600
    raw_text = secret_file.read_text(encoding="utf-8")
    assert "246810" not in raw_text
    assert isinstance(json.loads(raw_text), dict)
    assert "pbkdf2" in raw_text.lower()


def test_discovery_endpoints_honor_public_url_and_forwarded_headers(tmp_path: Path) -> None:
    """Verify RFC 9728 and RFC 8414 discovery with public_url and Cloudflare Tunnel headers."""
    service, _, _, _ = _make_bridge(tmp_path)
    service.setup_credentials(admin_pin="246810")

    with _running_bridge(service) as port:
        res_meta = _http_request(port, "GET", "/.well-known/oauth-protected-resource")
        assert res_meta.status == 200
        assert "https://alfred.domaine-perso.example" in json.dumps(res_meta.json_dict())
        as_json = _http_request(port, "GET", "/.well-known/oauth-authorization-server").json_dict()
        assert as_json["authorization_endpoint"] == (
            "https://alfred.domaine-perso.example/oauth/authorize"
        )
        assert as_json["token_endpoint"] == "https://alfred.domaine-perso.example/oauth/token"
        assert as_json["registration_endpoint"] == (
            "https://alfred.domaine-perso.example/oauth/register"
        )
        assert "S256" in as_json["code_challenge_methods_supported"]

    cf_service, _, _, _ = _make_bridge(tmp_path / "cf", public_url="")
    cf_service.setup_credentials(admin_pin="246810")
    cf_headers = {"Host": "tunnel.domaine-perso.example", "X-Forwarded-Proto": "https"}
    with _running_bridge(cf_service) as cf_port:
        cf_res = _http_request(
            cf_port, "GET", "/.well-known/oauth-protected-resource", headers=cf_headers
        )
        assert cf_res.status == 200
        assert "https://tunnel.domaine-perso.example" in json.dumps(cf_res.json_dict())
        cf_as = _http_request(
            cf_port, "GET", "/.well-known/oauth-authorization-server", headers=cf_headers
        )
        assert cf_as.json_dict()["token_endpoint"] == (
            "https://tunnel.domaine-perso.example/oauth/token"
        )


def test_oauth_register_accepts_google_and_loopback_and_rejects_evil(tmp_path: Path) -> None:
    """Verify POST /oauth/register redirect URI allowlist enforcement."""
    service, _, _, _ = _make_bridge(tmp_path)
    service.setup_credentials(admin_pin="246810")

    with _running_bridge(service) as port:
        google_reg = _http_request(
            port, "POST", "/oauth/register", json_body={"redirect_uris": [GEMINI_REDIRECT_URI]}
        )
        assert google_reg.status in (200, 201) and google_reg.json_dict().get("client_id")
        local_reg = _http_request(
            port, "POST", "/oauth/register", json_body={"redirect_uris": ["http://127.0.0.1/cb"]}
        )
        assert local_reg.status in (200, 201) and local_reg.json_dict().get("client_id")
        evil_reg = _http_request(
            port, "POST", "/oauth/register", json_body={"redirect_uris": ["http://evil.example/cb"]}
        )
        assert evil_reg.status == 400


def test_oauth_authorize_pin_validation_and_verbatim_state_echo(tmp_path: Path) -> None:
    """Verify GET/POST /oauth/authorize rejects bad PIN (403) and echoes state on 302."""
    service, _, _, _ = _make_bridge(tmp_path)
    service.setup_credentials(admin_pin="246810")
    verifier = "pkce-verifier-high-entropy-value-abcdef0123456789abcdef01234567"
    exact_state = "state-verbatim:check_42+ok"

    with _running_bridge(service) as port:
        reg = _http_request(
            port, "POST", "/oauth/register", json_body={"redirect_uris": [GEMINI_REDIRECT_URI]}
        )
        client_id = str(reg.json_dict()["client_id"])
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": GEMINI_REDIRECT_URI,
            "state": exact_state,
            "code_challenge": _pkce_s256(verifier),
            "code_challenge_method": "S256",
        })
        get_form = _http_request(port, "GET", f"/oauth/authorize?{query}")
        assert get_form.status == 200 and "<form" in get_form.text.lower()
        assert _issue_auth_code(port, client_id, verifier, pin="000000").status == 403
        good = _issue_auth_code(port, client_id, verifier, pin="246810", state=exact_state)
        assert good.status == 302
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(good.headers["location"]).query)
        assert qs["state"][0] == exact_state and qs["code"][0]


def test_oauth_token_pkce_s256_replay_guard_and_refresh_grant(tmp_path: Path) -> None:
    """Verify POST /oauth/token enforces PKCE S256, rejects code replay, and rotates tokens."""
    service, _, _, _ = _make_bridge(tmp_path)
    service.setup_credentials(admin_pin="246810")
    verifier = "pkce-verifier-high-entropy-value-abcdef0123456789abcdef01234567"

    with _running_bridge(service) as port:
        client_id, first_access, first_refresh = _complete_oauth_flow(port)
        bad_code = _extract_redirect_code(_issue_auth_code(port, client_id, verifier))
        wrong_ver = _http_request(
            port,
            "POST",
            "/oauth/token",
            form_body={
                "grant_type": "authorization_code",
                "code": bad_code,
                "client_id": client_id,
                "redirect_uri": GEMINI_REDIRECT_URI,
                "code_verifier": "wrong-verifier-000000000000000000000000000000000000",
            },
        )
        assert wrong_ver.status == 400

        ok_code = _extract_redirect_code(_issue_auth_code(port, client_id, verifier))
        token_payload = {
            "grant_type": "authorization_code",
            "code": ok_code,
            "client_id": client_id,
            "redirect_uri": GEMINI_REDIRECT_URI,
            "code_verifier": verifier,
        }
        assert _http_request(port, "POST", "/oauth/token", form_body=token_payload).status == 200
        assert _http_request(port, "POST", "/oauth/token", form_body=token_payload).status == 400

        refreshed = _http_request(
            port,
            "POST",
            "/oauth/token",
            form_body={"grant_type": "refresh_token", "refresh_token": first_refresh},
        )
        assert refreshed.status == 200
        ref_json = refreshed.json_dict()
        assert ref_json["access_token"] != first_access and ref_json.get("refresh_token")


def test_mcp_unauthenticated_and_initialize_tools_list(tmp_path: Path) -> None:
    """Verify 401 WWW-Authenticate when unauthenticated, and MCP 2025-03-26 initialize/list."""
    service, _, _, _ = _make_bridge(tmp_path)
    service.setup_credentials(admin_pin="246810")

    with _running_bridge(service) as port:
        unauth = _http_request(
            port, "POST", "/mcp", json_body={"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
        assert unauth.status == 401
        www_auth = unauth.headers.get("www-authenticate", "")
        assert "resource_metadata" in www_auth
        assert "/.well-known/oauth-protected-resource" in www_auth

        _, token, _ = _complete_oauth_flow(port)
        init_res = _call_mcp(
            port, token, 1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}
        ).json_dict()["result"]
        assert init_res["protocolVersion"] == "2025-03-26"
        tools_res = _call_mcp(port, token, 2, "tools/list", {}).json_dict()["result"]
        tool_names = {t["name"] for t in tools_res["tools"]}
        assert {
            "alfred_life_brain_dump",
            "alfred_session_status",
            "alfred_approve_plan",
            "alfred_quick_context",
        }.issubset(tool_names)


def test_mcp_four_tools_call_and_slow_timeout_guard(tmp_path: Path) -> None:
    """Verify tools/call on all 4 MCP tools plus sync_wait_seconds timeout guard."""
    service, _, store, agy = _make_bridge(tmp_path, sync_wait_seconds=3)
    seeded = ActiveSessionRecord(
        session_id="sess-life-001", workspace="alfred-life", status="ACTIVE", title="Patrimoine Q4"
    )
    store.conversations.save(
        store.conversations.load().model_copy(update={"sessions": {"sess-life-001": seeded}})
    )
    service.setup_credentials(admin_pin="246810")

    with _running_bridge(service) as port:
        _, token, _ = _complete_oauth_flow(port)
        dump_res = _call_mcp(
            port,
            token,
            3,
            "tools/call",
            {"name": "alfred_life_brain_dump", "arguments": {"prompt": "Bilan fiscal annuel"}},
        ).json_dict()["result"]
        assert dump_res["content"][0]["text"]
        serialized = json.dumps(agy.calls[-1])
        assert "alfred-life" in serialized or '"life"' in serialized
        assert "gemini-pro-orchestrator" in serialized

        st_res = _call_mcp(
            port, token, 4, "tools/call",
            {"name": "alfred_session_status", "arguments": {"session_id": "sess-life-001"}},
        ).json_dict()["result"]
        assert "sess-life-001" in st_res["content"][0]["text"]

        ap_res = _call_mcp(
            port, token, 5, "tools/call",
            {
                "name": "alfred_approve_plan",
                "arguments": {"session_id": "sess-life-001", "feedback": "Proceed"},
            },
        ).json_dict()["result"]
        assert ap_res["content"][0]["text"]

        ctx_res = _call_mcp(
            port, token, 6, "tools/call",
            {"name": "alfred_quick_context", "arguments": {"query": "fiscalite"}},
        ).json_dict()["result"]
        assert "Synthèse Foyer Fiscal" in ctx_res["content"][0]["text"]

        agy.delay_seconds = 4.2
        started = time.monotonic()
        slow_res = _call_mcp(
            port, token, 7, "tools/call",
            {"name": "alfred_life_brain_dump", "arguments": {"prompt": "Audit assurances"}},
        ).json_dict()["result"]
        assert (time.monotonic() - started) < 4.0 and not slow_res.get("isError", False)
        assert slow_res["content"][0]["text"]
