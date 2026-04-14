"""Unit coverage for #28 step 2 — wrapper/proxy-side stale-session recovery.

Covers:
  - MCP-proxy sentinel detection is strict (only structured tool-result text,
    no false positives on arbitrary chat content or unrelated bodies).
  - MCP-proxy retry policy: only strictly read-only tools are retried; the
    stale-session hook is still fired for mutating tools (e.g. chat_send) so
    the wrapper re-registers, but no transparent retry happens.
  - Tool-name extraction from JSON-RPC bodies.
  - End-to-end POST /mcp through a real McpIdentityProxy against a stub
    upstream: first response carries the sentinel, hook flips token, retry
    succeeds — the client sees only the clean second response.
"""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.request import Request, urlopen


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server — required for parallel stub tests where a fan-in
    barrier holds N concurrent requests before any responds."""

    daemon_threads = True

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_proxy
from mcp_proxy import (
    McpIdentityProxy,
    _STALE_RETRY_TOOLS,
    _STALE_SESSION_ERROR_SIGNATURE,
    _extract_tool_name,
    _response_has_stale_sentinel,
)


def _tools_call(tool_name: str, args: dict | None = None, req_id: int = 1) -> bytes:
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args or {}},
    }
    return json.dumps(body).encode("utf-8")


def _tool_result(text: str, req_id: int = 1, is_error: bool = False) -> bytes:
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }
    return json.dumps(body).encode("utf-8")


class ToolNameExtractionTests(unittest.TestCase):
    def test_extracts_name_from_tools_call(self):
        self.assertEqual(_extract_tool_name(_tools_call("chat_read")), "chat_read")

    def test_returns_empty_for_non_tools_call(self):
        other = json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}).encode()
        self.assertEqual(_extract_tool_name(other), "")

    def test_returns_empty_for_empty_or_bad_body(self):
        self.assertEqual(_extract_tool_name(b""), "")
        self.assertEqual(_extract_tool_name(b"not-json"), "")


class StaleSentinelDetectionTests(unittest.TestCase):
    def test_matches_exact_error_signature_with_is_error(self):
        body = _tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=True)
        self.assertTrue(_response_has_stale_sentinel(body))

    def test_requires_is_error_flag(self):
        # Same text, but result is not flagged as an error (e.g. a normal
        # tool that happens to surface the sentence). Must NOT match — this
        # guards chat_read outputs that can echo any user-authored string.
        body = _tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=False)
        self.assertFalse(_response_has_stale_sentinel(body))

    def test_rejects_substring_match_in_chat_content(self):
        # codex2 blocker 1: even if a user posts the exact phrase, it
        # surfaces inside a larger chat_read payload string — never at the
        # start of the tool-result text. Substring detection was the bug;
        # strict detection must refuse this body.
        payload = json.dumps([
            {"sender": "user", "text": _STALE_SESSION_ERROR_SIGNATURE},
            {"sender": "codex", "text": "ack"},
        ])
        body = _tool_result(payload, is_error=False)
        self.assertFalse(_response_has_stale_sentinel(body))

        # And even if chat_read ever surfaced this via an isError envelope
        # (it does not), an embedded occurrence that isn't the signature's
        # own full text still must not trip detection.
        embedded = f"preamble {_STALE_SESSION_ERROR_SIGNATURE} trailing"
        self.assertFalse(_response_has_stale_sentinel(
            _tool_result(embedded, is_error=True)
        ))

    def test_ignores_sentinel_phrase_in_non_result_body(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"summary": f"user posted: {_STALE_SESSION_ERROR_SIGNATURE}"},
        }).encode()
        self.assertFalse(_response_has_stale_sentinel(body))

    def test_ignores_clean_tool_result(self):
        body = _tool_result("ok")
        self.assertFalse(_response_has_stale_sentinel(body))

    def test_ignores_malformed_body(self):
        self.assertFalse(_response_has_stale_sentinel(b""))
        self.assertFalse(_response_has_stale_sentinel(b"not-json"))

    def test_matches_in_batch_response(self):
        body = json.dumps([
            {"jsonrpc": "2.0", "id": 1,
             "result": {"content": [{"type": "text", "text": "ok"}], "isError": False}},
            {"jsonrpc": "2.0", "id": 2,
             "result": {"content": [{"type": "text", "text": _STALE_SESSION_ERROR_SIGNATURE}],
                        "isError": True}},
        ]).encode()
        self.assertTrue(_response_has_stale_sentinel(body))


class StaleRetryWhitelistTests(unittest.TestCase):
    def test_read_only_tools_are_retryable(self):
        for name in ("chat_read", "chat_who"):
            self.assertIn(name, _STALE_RETRY_TOOLS,
                          f"{name} should be on the retry whitelist")

    def test_mutating_tools_are_not_retryable(self):
        # chat_rules covered explicitly: action="propose" mutates rule
        # state + posts a timeline message, so it cannot be retried
        # transparently (codex2 blocker 2).
        for name in ("chat_send", "chat_claim", "chat_join",
                     "chat_decision", "chat_set_hat", "chat_propose_job",
                     "chat_rules"):
            self.assertNotIn(name, _STALE_RETRY_TOOLS,
                             f"{name} must NOT be on the retry whitelist "
                             "(risk of duplicate state mutation on retry)")


# ---------------------------------------------------------------------------
# Integration: stub upstream + real proxy. Verifies the retry path end-to-end.
# ---------------------------------------------------------------------------


class _StubUpstream:
    """Tiny HTTP server that serves /mcp responses.

    Two modes:
      - FIFO scripted list (`responses=[...]`): pops one body per request.
        Used by single-request tests where order of arrival is deterministic.
      - Token-aware (`token_responder=callable`): body is chosen by the
        request's `Authorization` header. Used by the parallel test so that
        retries always land on the "fresh" branch regardless of arrival
        order, eliminating the timing flakiness codex2 flagged.

    An optional `barrier` holds incoming requests until N threads have
    arrived, giving tests a hard guarantee that all parallel calls reach
    upstream with the pre-flip token before any response flips the shared
    state.
    """

    def __init__(
        self,
        responses: list[bytes] | None = None,
        token_responder=None,
        barrier: threading.Barrier | None = None,
    ):
        if responses is None and token_responder is None:
            raise ValueError("_StubUpstream requires responses or token_responder")
        self._responses = list(responses or [])
        self._token_responder = token_responder
        self._barrier = barrier
        self._tokens_seen: list[str] = []
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def tokens_seen(self) -> list[str]:
        with self._lock:
            return list(self._tokens_seen)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                _ = self.rfile.read(length) if length else b""
                auth = self.headers.get("Authorization", "")
                with stub._lock:
                    stub._tokens_seen.append(auth)
                # Barrier forces a deterministic fan-in: all N callers must
                # have arrived (with whatever token they were carrying at
                # send-time) before any response flows, so retries under
                # the refreshed token can't race back to the stub before
                # the pre-flip wave has landed.
                if stub._barrier is not None:
                    try:
                        stub._barrier.wait(timeout=5)
                    except threading.BrokenBarrierError:
                        pass
                if stub._token_responder is not None:
                    body = stub._token_responder(auth)
                else:
                    with stub._lock:
                        body = (
                            stub._responses.pop(0) if stub._responses
                            else _tool_result("ok")
                        )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                # Provide a fake MCP-Session-Id so streamable-http stays happy.
                self.send_header("Mcp-Session-Id", "stub-session")
                self.end_headers()
                self.wfile.write(body)

        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class ProxyStaleSessionRetryTests(unittest.TestCase):
    def setUp(self):
        self.upstream: _StubUpstream | None = None
        self.proxy: McpIdentityProxy | None = None

    def tearDown(self):
        if self.proxy:
            self.proxy.stop()
        if self.upstream:
            self.upstream.stop()

    def _setup(self, responses: list[bytes]):
        self.upstream = _StubUpstream(responses)
        self.upstream.start()
        self.proxy = McpIdentityProxy(
            upstream_base=f"http://127.0.0.1:{self.upstream.port}",
            upstream_path="/mcp",
            agent_name="claude",
            instance_token="old-token",
        )
        self.assertTrue(self.proxy.start())

    def _post_tool_call(self, tool: str, req_id: int = 1) -> bytes:
        req = Request(
            f"{self.proxy.url}/mcp",
            data=_tools_call(tool, req_id=req_id),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=5) as resp:
            return resp.read()

    def test_chat_read_stale_sentinel_triggers_hook_and_retry(self):
        stale = _tool_result(_STALE_SESSION_ERROR_SIGNATURE,
                             is_error=True)
        fresh = _tool_result("messages: []")
        self._setup([stale, fresh])

        hook_calls = []

        def _hook():
            hook_calls.append(True)
            self.proxy.token = "new-token"
            return True

        self.proxy.on_stale_session = _hook

        body = self._post_tool_call("chat_read")

        self.assertEqual(len(hook_calls), 1, "hook must fire exactly once on stale sentinel")
        self.assertIn(b"messages: []", body, "client must see the fresh retry response")
        self.assertNotIn(_STALE_SESSION_ERROR_SIGNATURE.encode(), body,
                         "stale sentinel must not leak to the client after successful retry")
        tokens = self.upstream.tokens_seen
        self.assertEqual(len(tokens), 2, "upstream must have been hit twice (original + retry)")
        self.assertEqual(tokens[0], "Bearer old-token")
        self.assertEqual(tokens[1], "Bearer new-token",
                         "retry must use the refreshed token from the hook")

    def test_chat_send_stale_sentinel_fires_hook_but_no_retry(self):
        stale = _tool_result(_STALE_SESSION_ERROR_SIGNATURE,
                             is_error=True)
        self._setup([stale])

        hook_calls = []

        def _hook():
            hook_calls.append(True)
            self.proxy.token = "new-token"
            return True

        self.proxy.on_stale_session = _hook

        body = self._post_tool_call("chat_send")

        self.assertEqual(len(hook_calls), 1,
                         "hook fires for chat_send so wrapper re-registers")
        self.assertIn(_STALE_SESSION_ERROR_SIGNATURE.encode(), body,
                      "mutating tools must surface the sentinel to the caller — "
                      "no transparent retry that could double-post")
        self.assertEqual(len(self.upstream.tokens_seen), 1,
                         "chat_send must not be retried against upstream")

    def test_retry_skipped_when_hook_reports_failure(self):
        stale = _tool_result(_STALE_SESSION_ERROR_SIGNATURE,
                             is_error=True)
        self._setup([stale])

        self.proxy.on_stale_session = lambda: False  # re-register attempted, failed

        body = self._post_tool_call("chat_read")

        self.assertIn(_STALE_SESSION_ERROR_SIGNATURE.encode(), body,
                      "failed hook => no retry, sentinel flows to client")
        self.assertEqual(len(self.upstream.tokens_seen), 1,
                         "no retry when hook says token is not fresh")

    def test_clean_response_is_untouched(self):
        self._setup([_tool_result("ok")])
        called = []
        self.proxy.on_stale_session = lambda: called.append(True) or True

        body = self._post_tool_call("chat_read")

        self.assertEqual(called, [], "hook must not fire on clean responses")
        self.assertIn(b"ok", body)
        self.assertEqual(len(self.upstream.tokens_seen), 1)

    def test_parallel_chat_reads_single_hook_fire(self):
        # PM guardrail 5 / reviewer point 1: single throttle is the only
        # guard against cascading re-registers. This test proves that
        # parallel stale responses converge on a single token flip without
        # sentinel leaks — deterministically, not timing-dependent.
        #
        # Determinism setup (codex2 herreview fix):
        #   - Upstream chooses response by Authorization header:
        #       old-token -> stale sentinel
        #       new-token -> fresh payload
        #     so the stub cannot run out of "fresh" responses regardless of
        #     how many times retries fire.
        #   - A threading.Barrier holds the first N initial requests until
        #     they have all arrived at upstream carrying old-token. Only
        #     then are their stale responses released, guaranteeing every
        #     worker enters the recovery path with the pre-flip token.
        #   - The hook is a one-shot flip guarded by a lock — the invariant
        #     under test is "exactly one flip no matter how many parallel
        #     workers hit the sentinel".
        stale = _tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=True)
        fresh = _tool_result("messages: []")
        N = 3
        barrier = threading.Barrier(N)

        def _responder(auth: str) -> bytes:
            return stale if auth == "Bearer old-token" else fresh

        # Fan-in barrier applies only to the first N pre-flip requests.
        # Retries carry new-token and must not be delayed or held, so we
        # install a dedicated stub with the barrier releasing after N hits.
        # After the barrier is passed, subsequent calls skip the wait via
        # BrokenBarrierError handling in _StubUpstream.
        self.upstream = _StubUpstream(token_responder=_responder, barrier=barrier)
        self.upstream.start()
        self.proxy = McpIdentityProxy(
            upstream_base=f"http://127.0.0.1:{self.upstream.port}",
            upstream_path="/mcp",
            agent_name="claude",
            instance_token="old-token",
        )
        self.assertTrue(self.proxy.start())

        hook_lock = threading.Lock()
        hook_calls = []
        token_flipped = {"done": False}

        def _hook():
            # Simulates the wrapper's shared 30s throttle: first call flips
            # the proxy token, concurrent callers find it already flipped
            # and return True (meaning: token is fresh, proceed to retry).
            with hook_lock:
                if not token_flipped["done"]:
                    hook_calls.append(True)
                    token_flipped["done"] = True
                    self.proxy.token = "new-token"
            return True

        self.proxy.on_stale_session = _hook

        results: list[bytes | None] = [None] * N
        errors: list[BaseException] = []

        def _worker(i: int):
            try:
                results[i] = self._post_tool_call("chat_read", req_id=i + 1)
            except BaseException as exc:  # noqa: BLE001 — test harness
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"worker(s) raised: {errors}")
        for i, body in enumerate(results):
            self.assertIsNotNone(body, f"worker {i} produced no body")
            self.assertIn(b"messages: []", body,
                          f"worker {i} did not see fresh response: {body!r}")
            self.assertNotIn(_STALE_SESSION_ERROR_SIGNATURE.encode(), body,
                             f"worker {i} saw stale sentinel leak: {body!r}")

        self.assertEqual(
            len(hook_calls), 1,
            "shared throttle must collapse concurrent recoveries to exactly "
            "one re-register, regardless of how many parallel workers hit "
            "the stale sentinel in the same window",
        )

        # Token histogram: every worker's first-pass carried old-token (the
        # barrier forced this), and every retry carried new-token.
        tokens = self.upstream.tokens_seen
        self.assertEqual(tokens.count("Bearer old-token"), N,
                         "every initial request must have used old-token")
        self.assertEqual(tokens.count("Bearer new-token"), N,
                         "every retry must have used new-token")
        self.assertEqual(len(tokens), 2 * N,
                         "exactly N initial calls + N retries — no extra or "
                         "missing upstream hits")


# ---------------------------------------------------------------------------
# SSE regression: the retry path is strictly POST /mcp. The SSE handler
# (do_GET) must never inspect event payloads for the sentinel or call the
# stale-session hook. This test locks in that scope boundary.
# ---------------------------------------------------------------------------


class _StubSseUpstream:
    """Upstream that serves an SSE stream on GET /sse, including an event
    whose payload contains the stale-session phrase verbatim."""

    def __init__(self, sse_body: bytes):
        self._sse_body = sse_body
        self._lock = threading.Lock()
        self._get_count = 0
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def get_count(self) -> int:
        with self._lock:
            return self._get_count

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):
                pass

            def do_GET(self):
                with stub._lock:
                    stub._get_count += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(stub._sse_body)))
                self.end_headers()
                self.wfile.write(stub._sse_body)

        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class SseStaleSentinelIgnoredTests(unittest.TestCase):
    def test_sse_stream_never_triggers_stale_hook_or_retry(self):
        # An SSE event whose text carries the exact stale-session phrase.
        # Even under this worst case the proxy must stream it through
        # unchanged; the hook must never fire on the streaming path.
        sse_body = (
            f"event: message\n"
            f"data: {_STALE_SESSION_ERROR_SIGNATURE}\n\n"
        ).encode("utf-8")

        upstream = _StubSseUpstream(sse_body)
        upstream.start()
        try:
            proxy = McpIdentityProxy(
                upstream_base=f"http://127.0.0.1:{upstream.port}",
                upstream_path="/sse",
                agent_name="claude",
                instance_token="t",
            )
            self.assertTrue(proxy.start())
            hook_calls = []
            proxy.on_stale_session = lambda: hook_calls.append(True) or True
            try:
                req = Request(f"{proxy.url}/sse", method="GET")
                with urlopen(req, timeout=5) as resp:
                    body = resp.read()
                self.assertIn(_STALE_SESSION_ERROR_SIGNATURE.encode(), body,
                              "SSE payload must pass through untouched — "
                              "proxy does not rewrite event content")
                self.assertEqual(hook_calls, [],
                                 "SSE path must never trigger the stale-session hook")
                self.assertEqual(upstream.get_count, 1,
                                 "SSE path must never retry — out of scope "
                                 "for the #28 step 2 recovery pattern")
            finally:
                proxy.stop()
        finally:
            upstream.stop()


if __name__ == "__main__":
    unittest.main()
