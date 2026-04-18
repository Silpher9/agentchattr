"""Per-instance MCP identity proxy.

Sits between an agent CLI and the real agentchattr MCP server.
Intercepts tool calls and stamps the `sender`/`name` argument
from the agent's registered identity while forwarding the
server-issued bearer token, so agents never need to know
their own name or auth material.

Supports both transports:
  - streamable-http (Claude, Codex, Qwen): POST /mcp, GET /mcp, DELETE /mcp
  - SSE (Gemini): GET /sse → event stream, POST /messages/ → tool calls

Usage (from wrapper.py):
    proxy = McpIdentityProxy(
        upstream_base="http://127.0.0.1:8200",
        upstream_path="/mcp",
        agent_name="claude-prime",
        instance_token="abc123...",
    )
    proxy.start()          # non-blocking — runs in a daemon thread
    proxy_url = proxy.url  # e.g. "http://127.0.0.1:54321"
    ...
    proxy.stop()
"""

import json
import re
import threading
import logging
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

log = logging.getLogger(__name__)

# MCP tools and which parameter carries the agent identity
_SENDER_PARAMS = {
    "chat_send": "sender",
    "chat_read": "sender",
    "chat_resync": "sender",
    "chat_join": "name",
    "chat_who": None,          # no sender param
    "chat_decision": "sender",
    "chat_channels": None,
    "chat_set_hat": "sender",
    "chat_claim": "sender",
}

# Exact error signature returned by mcp_bridge when the bearer token is stale
# after a server restart (see mcp_bridge._resolve_tool_identity). Detected in
# JSON-RPC tool-result content so the proxy can trigger wrapper-side
# re-register and (where safe) retry the call transparently.
#
# We match the FULL error string, not a substring, so a chat message whose
# body happens to contain the phrase cannot trip the sentinel when surfaced
# by chat_read. mcp_bridge wraps this error via FastMCP, which emits it as
# an isError tool-result.
_STALE_SESSION_ERROR_SIGNATURE = (
    "Error: stale or unknown authenticated agent session. Re-register and retry."
)

# Tools that are safe to retry transparently after a stale-session re-register.
# Strict read-only whitelist — anything that mutates server state is left to
# the caller to retry with the refreshed token (avoids e.g. duplicate
# chat_send posts on unlucky timing). chat_rules is NOT on this list because
# action="propose" mutates rule state + posts a timeline message.
_STALE_RETRY_TOOLS = {"chat_read", "chat_who", "chat_channels", "chat_resync"}


def _extract_tool_name(raw: bytes) -> str:
    """Return the tool name for a `tools/call` JSON-RPC request, else ''."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    messages = data if isinstance(data, list) else [data]
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("method") != "tools/call":
            continue
        params = msg.get("params") or {}
        name = params.get("name")
        if isinstance(name, str) and name:
            return name
    return ""


def _iter_jsonrpc_payloads(resp_body: bytes):
    """Yield parsed JSON-RPC payloads from an MCP streamable-http response body.

    Supports both response shapes FastMCP uses for `tools/call`:
      - plain JSON (when `is_json_response_enabled=True`): one JSON object or array
      - SSE event-stream (library default): `event: ...\\ndata: <json>\\n\\n` frames

    Unrecognized lines and malformed JSON frames are silently skipped so callers
    can treat the iterator as a best-effort payload source.
    """
    if not resp_body:
        return
    # Fast path: plain JSON body (identical to the pre-SSE behaviour).
    try:
        yield json.loads(resp_body)
        return
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    # Slow path: SSE frames. Per spec, events are blank-line separated and
    # `data:` lines carry the payload. FastMCP emits one JSON payload per
    # `data:` line, so we parse each such line independently.
    try:
        text = resp_body.decode("utf-8", "replace")
    except Exception:
        return
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].lstrip()
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue


def _response_has_stale_sentinel(resp_body: bytes) -> bool:
    """Parse a JSON-RPC response and return True iff a tool-result carries the
    exact stale-session error signature.

    Strict detection (codex2 guardrail 1):
      - tool-result must be flagged as an error (`isError: true`) — a normal
        `chat_read` response carrying user chat text is never flagged this
        way by the MCP bridge, so even chat content that literally contains
        the phrase cannot trip the sentinel;
      - the matching text fragment must equal the full error signature, not
        just contain the phrase as a substring. Tool-result text fragments
        may be chunked, so we permit a fragment that strictly *equals* or is
        prefixed by the signature, but never a loose substring anywhere in
        arbitrary content.

    Accepts both plain-JSON and SSE-wrapped response bodies so the detector
    works regardless of which streamable-http response shape FastMCP emits
    (SSE is the library default; see #30).
    """
    if not resp_body:
        return False
    for data in _iter_jsonrpc_payloads(resp_body):
        messages = data if isinstance(data, list) else [data]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            result = msg.get("result")
            if not isinstance(result, dict):
                continue
            if result.get("isError") is not True:
                continue
            content = result.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                # Accept exact match or the signature as a full prefix of a
                # larger error blob, but reject arbitrary substring hits.
                stripped = text.strip()
                if stripped == _STALE_SESSION_ERROR_SIGNATURE:
                    return True
                if stripped.startswith(_STALE_SESSION_ERROR_SIGNATURE):
                    return True
    return False


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread.
    Required for SSE: GET holds the stream open while POSTs arrive concurrently."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if _is_benign_client_disconnect(exc):
            return
        super().handle_error(request, client_address)


def _is_benign_client_disconnect(exc: BaseException | None) -> bool:
    """Return True for normal client disconnects that should not spam stderr."""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
        return True
    if isinstance(exc, OSError):
        return getattr(exc, "winerror", None) in {64, 995, 10053, 10054}
    return False


class McpIdentityProxy:
    """Local HTTP proxy that stamps agent identity on MCP tool calls.

    Args:
        upstream_base: Base URL without path, e.g. "http://127.0.0.1:8200"
        upstream_path: Path prefix for the transport, e.g. "/mcp" or "/sse"
        agent_name: Current canonical name for this instance
        instance_token: Server-issued token (forwarded as Authorization: Bearer)
    """

    def __init__(self, upstream_base: str, upstream_path: str,
                 agent_name: str, instance_token: str, port: int = 0):
        self._upstream_base = upstream_base.rstrip("/")
        self._upstream_path = upstream_path
        self._agent_name = agent_name
        self._token = instance_token
        self._port = port  # 0 = OS-assigned (legacy), >0 = fixed
        self._lock = threading.RLock()
        self._recover_cond = threading.Condition(self._lock)
        self._upstream_session_id: str | None = None
        self._init_request_body: bytes | None = None
        self._recovering = False
        self._last_recovery_ok = False
        self._server: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Optional hook invoked when upstream returns the stale-session sentinel
        # in a tool-result. Callable should trigger a throttled wrapper-side
        # re-register (which updates `proxy.token`) and return True when the
        # current token is fresh, False when re-register was attempted and
        # failed. See wrapper.py `_attempt_stale_reregister`.
        self.on_stale_session: "callable | None" = None

    @property
    def port(self) -> int:
        if self._server:
            return self._server.server_address[1]
        return 0

    @property
    def url(self) -> str:
        """Base URL of the proxy (no path — clients add /mcp or /sse themselves)."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def agent_name(self) -> str:
        with self._lock:
            return self._agent_name

    @agent_name.setter
    def agent_name(self, name: str):
        with self._lock:
            self._agent_name = name

    @property
    def token(self) -> str:
        with self._lock:
            return self._token

    @token.setter
    def token(self, value: str):
        with self._lock:
            self._token = value

    def _get_upstream_session_id(self) -> str | None:
        with self._lock:
            return self._upstream_session_id

    def _set_upstream_session_id(self, session_id: str | None):
        with self._recover_cond:
            self._upstream_session_id = session_id

    def _remember_initialize(self, raw: bytes):
        with self._recover_cond:
            self._init_request_body = raw

    def _prepare_recovery(self) -> tuple[str, bytes | None]:
        """Return ('leader'|'waited'|'missing', init_body)."""
        with self._recover_cond:
            if self._recovering:
                while self._recovering:
                    self._recover_cond.wait()
                return "waited", None
            if not self._init_request_body:
                return "missing", None
            self._recovering = True
            self._last_recovery_ok = False
            return "leader", self._init_request_body

    def _finish_recovery(self, ok: bool, session_id: str | None = None):
        with self._recover_cond:
            if ok:
                self._upstream_session_id = session_id
            else:
                self._upstream_session_id = None
            self._last_recovery_ok = ok
            self._recovering = False
            self._recover_cond.notify_all()

    def _recovery_succeeded(self) -> bool:
        with self._recover_cond:
            return self._last_recovery_ok and bool(self._upstream_session_id)

    def start(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # silence request logs

            def _upstream_url(self, path: str | None = None) -> str:
                """Build upstream URL, preserving the request path."""
                p = path if path else self.path
                return f"{proxy._upstream_base}{p}"

            def _send_response_headers(self, headers, *, fallback_session_id: str | None = None):
                sent_session_header = False
                for key in (
                    "Content-Type",
                    "Mcp-Session-Id",
                    "mcp-session-id",
                    "Cache-Control",
                    "X-Accel-Buffering",
                    "Connection",
                ):
                    val = headers.get(key)
                    if val:
                        if key.lower() == "mcp-session-id":
                            sent_session_header = True
                        self.send_header(key, val)
                if fallback_session_id and not sent_session_header:
                    self.send_header("Mcp-Session-Id", fallback_session_id)

            @staticmethod
            def _is_session_not_found(status: int, body: bytes) -> bool:
                return status == 404 and b"session not found" in body.lower()

            def _is_streamable_http(self) -> bool:
                path = urlsplit(self.path).path
                return proxy._upstream_path == "/mcp" and path == "/mcp"

            def _jsonrpc_method(self, raw: bytes) -> str:
                if not raw:
                    return ""
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return ""
                if isinstance(data, dict):
                    return str(data.get("method", "") or "")
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("method"):
                            return str(item.get("method") or "")
                return ""

            def _jsonrpc_tool_name(self, raw: bytes) -> str:
                return _extract_tool_name(raw)

            @staticmethod
            def _response_has_stale_sentinel(resp_body: bytes) -> bool:
                return _response_has_stale_sentinel(resp_body)

            def _build_upstream_request(
                self,
                method: str,
                *,
                data: bytes | None = None,
                path: str | None = None,
                use_proxy_session: bool = False,
                explicit_session_id: str | None = None,
                extra_headers: dict[str, str] | None = None,
            ) -> Request:
                req = Request(self._upstream_url(path), data=data, method=method)
                extra_header_names = {k.lower() for k in (extra_headers or {})}
                for hdr, val in self.headers.items():
                    lower = hdr.lower()
                    if lower in ("content-length", "host", "mcp-session-id") or lower in extra_header_names:
                        continue
                    req.add_header(hdr, val)

                session_id = explicit_session_id
                if use_proxy_session and session_id is None:
                    session_id = proxy._get_upstream_session_id()
                if session_id:
                    req.add_header("Mcp-Session-Id", session_id)

                if extra_headers:
                    for hdr, val in extra_headers.items():
                        req.add_header(hdr, val)

                req.add_header("Authorization", f"Bearer {proxy.token}")
                req.add_header("X-Agent-Token", proxy.token)
                return req

            def _send_upstream_request(
                self,
                method: str,
                *,
                data: bytes | None = None,
                path: str | None = None,
                use_proxy_session: bool = False,
                explicit_session_id: str | None = None,
                extra_headers: dict[str, str] | None = None,
                timeout: int = 30,
            ) -> tuple[int, bytes, object]:
                req = self._build_upstream_request(
                    method,
                    data=data,
                    path=path,
                    use_proxy_session=use_proxy_session,
                    explicit_session_id=explicit_session_id,
                    extra_headers=extra_headers,
                )
                try:
                    resp = urlopen(req, timeout=timeout)
                    return resp.status, resp.read(), resp.headers
                except HTTPError as e:
                    return e.code, e.read(), e.headers

            def _open_upstream_stream(
                self,
                *,
                path: str | None = None,
                use_proxy_session: bool = False,
                timeout: int = 300,
            ):
                req = self._build_upstream_request(
                    "GET",
                    path=path,
                    use_proxy_session=use_proxy_session,
                )
                return urlopen(req, timeout=timeout)

            @staticmethod
            def _extract_session_id(headers) -> str:
                return headers.get("Mcp-Session-Id") or headers.get("mcp-session-id") or ""

            def _update_session_from_headers(self, headers):
                session_id = self._extract_session_id(headers)
                if session_id:
                    proxy._set_upstream_session_id(session_id)
                return session_id

            def _reinitialize_upstream_session(self) -> bool:
                state, init_body = proxy._prepare_recovery()
                if state == "missing":
                    log.warning(
                        "MCP session lost for %s, but no initialize payload was stored; cannot recover",
                        proxy.agent_name,
                    )
                    return False
                if state == "waited":
                    return proxy._recovery_succeeded()

                assert init_body is not None
                log.warning("MCP session lost for %s — starting streamable-http recovery", proxy.agent_name)
                ok = False
                new_session_id = None
                try:
                    init_status, init_resp_body, init_headers = self._send_upstream_request(
                        "POST",
                        data=init_body,
                        path=proxy._upstream_path,
                        use_proxy_session=False,
                        extra_headers={"Content-Type": "application/json"},
                        timeout=30,
                    )
                    if init_status >= 400:
                        log.warning(
                            "MCP session recovery initialize failed for %s: HTTP %s %s",
                            proxy.agent_name,
                            init_status,
                            init_resp_body.decode("utf-8", "replace")[:200],
                        )
                    else:
                        new_session_id = self._extract_session_id(init_headers)
                        if not new_session_id:
                            log.warning(
                                "MCP session recovery initialize returned no session id for %s",
                                proxy.agent_name,
                            )
                        else:
                            notif_body = json.dumps({
                                "jsonrpc": "2.0",
                                "method": "notifications/initialized",
                            }).encode("utf-8")
                            notif_status, notif_resp_body, _ = self._send_upstream_request(
                                "POST",
                                data=notif_body,
                                path=proxy._upstream_path,
                                explicit_session_id=new_session_id,
                                extra_headers={"Content-Type": "application/json"},
                                timeout=30,
                            )
                            if notif_status >= 400:
                                log.warning(
                                    "MCP session recovery initialized notification failed for %s: HTTP %s %s",
                                    proxy.agent_name,
                                    notif_status,
                                    notif_resp_body.decode("utf-8", "replace")[:200],
                                )
                            else:
                                ok = True
                except (URLError, OSError) as exc:
                    log.warning("MCP session recovery failed for %s: %s", proxy.agent_name, exc)
                finally:
                    proxy._finish_recovery(ok, new_session_id)

                if ok:
                    log.info("MCP session recovered for %s", proxy.agent_name)
                else:
                    log.warning("MCP session recovery failed for %s", proxy.agent_name)
                return ok

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""

                # Inject sender into MCP tool calls
                body = self._maybe_inject_sender(raw)
                method_name = self._jsonrpc_method(raw)
                is_streamable = self._is_streamable_http()
                if is_streamable and method_name == "initialize":
                    proxy._remember_initialize(raw)
                    proxy._set_upstream_session_id(None)

                try:
                    status, resp_body, resp_headers = self._send_upstream_request(
                        "POST",
                        data=body,
                        use_proxy_session=is_streamable and method_name != "initialize",
                        extra_headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
                        timeout=30,
                    )
                except (URLError, OSError) as e:
                    self.send_error(502, f"Upstream error: {e}")
                    return

                if is_streamable and self._is_session_not_found(status, resp_body):
                    log.warning("MCP session not found from upstream for %s", proxy.agent_name)
                    if self._reinitialize_upstream_session():
                        try:
                            status, resp_body, resp_headers = self._send_upstream_request(
                                "POST",
                                data=body,
                                use_proxy_session=True,
                                extra_headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
                                timeout=30,
                            )
                        except (URLError, OSError) as e:
                            self.send_error(502, f"Upstream error: {e}")
                            return
                elif not is_streamable and self._is_session_not_found(status, resp_body):
                    log.warning(
                        "SSE MCP session lost for %s — automatic recovery is not implemented yet; client must reconnect",
                        proxy.agent_name,
                    )

                # #28 step 2: upstream may return a tool-result with the
                # stale-session sentinel (HTTP 200 body) when our bearer token
                # was invalidated by a server restart. Detect it, trigger the
                # wrapper-side re-register hook, and retry once for strictly
                # read-only tools. POST /mcp only — SSE streams are never
                # inspected or retried here (out of scope for this step).
                stale_token_refreshed = False
                if (
                    is_streamable
                    and status < 400
                    and method_name == "tools/call"
                    and self._response_has_stale_sentinel(resp_body)
                ):
                    tool_name = self._jsonrpc_tool_name(raw)
                    log.warning(
                        "MCP stale-session sentinel detected for %s (tool=%s)",
                        proxy.agent_name, tool_name or "?",
                    )
                    hook = proxy.on_stale_session
                    hook_fresh = False
                    if hook is not None:
                        try:
                            hook_fresh = bool(hook())
                        except Exception as exc:
                            log.warning(
                                "on_stale_session hook raised for %s: %s",
                                proxy.agent_name, exc,
                            )
                    stale_token_refreshed = hook_fresh
                    # Only retry idempotent reads; let state-mutating tools
                    # (chat_send, chat_claim, chat_decision, ...) surface the
                    # sentinel to the caller, who can retry with the refreshed
                    # token if they choose. This avoids double-posts on races.
                    if hook_fresh and tool_name in _STALE_RETRY_TOOLS:
                        # Re-inject sender on a fresh copy of the request body
                        # because proxy.agent_name may have changed.
                        retry_body = self._maybe_inject_sender(raw)
                        try:
                            status, resp_body, resp_headers = self._send_upstream_request(
                                "POST",
                                data=retry_body,
                                use_proxy_session=is_streamable and method_name != "initialize",
                                extra_headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
                                timeout=30,
                            )
                        except (URLError, OSError) as e:
                            self.send_error(502, f"Upstream error: {e}")
                            return

                fallback_session_id = ""
                if is_streamable and status < 400 and method_name != "notifications/initialized":
                    fallback_session_id = self._update_session_from_headers(resp_headers)
                    if not fallback_session_id:
                        fallback_session_id = proxy._get_upstream_session_id() or ""

                # #30: after a successful token refresh, invalidate the
                # cached upstream session so the NEXT MCP call re-runs
                # initialize with the fresh token. Runs AFTER the
                # fallback_session_id update above so the retry response's
                # own session header doesn't immediately re-prime the cache
                # with the old-server session. This is atomic against other
                # recovery operations because `_set_upstream_session_id`
                # serialises on `_recover_cond`.
                if stale_token_refreshed and is_streamable:
                    proxy._set_upstream_session_id(None)

                self.send_response(status)
                self._send_response_headers(
                    resp_headers,
                    fallback_session_id=fallback_session_id or None,
                )
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)

            def do_GET(self):
                """Forward GET — handles both streamable-http and SSE streams."""
                is_streamable = self._is_streamable_http()
                try:
                    resp = self._open_upstream_stream(
                        use_proxy_session=is_streamable,
                        timeout=300,
                    )
                except HTTPError as e:
                    status = e.code
                    resp_body = e.read()
                    resp_headers = e.headers
                    if is_streamable and self._is_session_not_found(status, resp_body):
                        log.warning("MCP stream GET session not found for %s", proxy.agent_name)
                        if self._reinitialize_upstream_session():
                            try:
                                resp = self._open_upstream_stream(
                                    use_proxy_session=True,
                                    timeout=300,
                                )
                            except HTTPError as retry_err:
                                status = retry_err.code
                                resp_body = retry_err.read()
                                resp_headers = retry_err.headers
                            except (URLError, OSError) as err:
                                self.send_error(502, f"Upstream error: {err}")
                                return
                            else:
                                fallback_session_id = self._update_session_from_headers(resp.headers)
                                self.send_response(resp.status)
                                self._send_response_headers(
                                    resp.headers,
                                    fallback_session_id=fallback_session_id or None,
                                )
                                self.end_headers()
                                try:
                                    for line in resp:
                                        if line.startswith(b"data:"):
                                            line = self._rewrite_sse_endpoint(line)
                                        self.wfile.write(line)
                                        self.wfile.flush()
                                except BrokenPipeError:
                                    pass
                                return
                    elif not is_streamable and self._is_session_not_found(status, resp_body):
                        log.warning(
                            "SSE MCP stream session lost for %s — automatic recovery is not implemented yet; client must reconnect",
                            proxy.agent_name,
                        )
                    self.send_response(status)
                    self._send_response_headers(resp_headers)
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    if resp_body:
                        self.wfile.write(resp_body)
                    return
                except BrokenPipeError:
                    return
                except (URLError, OSError) as e:
                    self.send_error(502, f"Upstream error: {e}")
                    return

                fallback_session_id = None
                if is_streamable:
                    fallback_session_id = self._update_session_from_headers(resp.headers) or proxy._get_upstream_session_id()

                self.send_response(resp.status)
                self._send_response_headers(resp.headers, fallback_session_id=fallback_session_id)
                self.end_headers()

                try:
                    # Stream line-by-line for SSE (events are line-delimited)
                    for line in resp:
                        # Rewrite endpoint URLs in SSE events so the client
                        # POSTs back through the proxy, not directly to upstream
                        if line.startswith(b"data:"):
                            line = self._rewrite_sse_endpoint(line)
                        self.wfile.write(line)
                        self.wfile.flush()
                except BrokenPipeError:
                    pass

            def do_DELETE(self):
                try:
                    status, resp_body, resp_headers = self._send_upstream_request(
                        "DELETE",
                        use_proxy_session=self._is_streamable_http(),
                        timeout=10,
                    )
                except (URLError, OSError):
                    self.send_error(502)
                    return

                if self._is_streamable_http():
                    if self._is_session_not_found(status, resp_body):
                        log.info("MCP session already gone for %s during DELETE; clearing proxy state", proxy.agent_name)
                        proxy._set_upstream_session_id(None)
                        self.send_response(204)
                        self.end_headers()
                        return
                    if status < 400:
                        proxy._set_upstream_session_id(None)

                self.send_response(status)
                self._send_response_headers(resp_headers)
                if resp_body:
                    self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                if resp_body:
                    self.wfile.write(resp_body)

            def _rewrite_sse_endpoint(self, line: bytes) -> bytes:
                """Rewrite upstream endpoint URLs in SSE data lines.

                FastMCP SSE sends: data: http://127.0.0.1:8201/messages/?session_id=xxx
                We rewrite to:     data: http://127.0.0.1:{proxy_port}/messages/?session_id=xxx
                so the client routes tool call POSTs through our proxy.
                """
                try:
                    text = line.decode("utf-8")
                    # Match "data: http://host:port/path..."
                    rewritten = re.sub(
                        r'data:\s*http://127\.0\.0\.1:\d+/',
                        f'data: {proxy.url}/',
                        text,
                    )
                    return rewritten.encode("utf-8")
                except Exception:
                    return line

            def _maybe_inject_sender(self, raw: bytes) -> bytes:
                """Parse JSON-RPC, inject sender for tools/call if missing."""
                if not raw:
                    return raw
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return raw

                # Handle both single requests and batches
                messages = data if isinstance(data, list) else [data]
                modified = False

                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("method") != "tools/call":
                        continue

                    params = msg.get("params", {})
                    tool_name = params.get("name", "")
                    args = params.get("arguments", {})

                    sender_key = _SENDER_PARAMS.get(tool_name)
                    if sender_key is None:
                        continue

                    current = args.get(sender_key, "")
                    if current != proxy.agent_name:
                        args[sender_key] = proxy.agent_name
                        params["arguments"] = args
                        modified = True

                if modified:
                    return json.dumps(data).encode("utf-8")
                return raw

        try:
            self._server = _ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        except OSError as e:
            if self._port > 0:
                # Fixed port in use — another wrapper instance owns the proxy
                log.info(f"Proxy port {self._port} in use, skipping (another instance owns it)")
                print(f"  MCP proxy: port {self._port} in use (shared with another instance)")
                self._server = None
                return False
            raise
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info(f"MCP proxy for {self._agent_name} on port {self.port}")
        print(f"  MCP proxy: port {self.port}")
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
