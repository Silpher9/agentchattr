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
    _iter_jsonrpc_payloads,
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


def _sse_frame(payload_json: bytes, *, event: str = "message", event_id: str | None = None) -> bytes:
    """Wrap a JSON-RPC payload in an SSE frame, matching FastMCP's output
    shape for streamable-http responses (default when is_json_response_enabled
    is False — see #30 diagnose note)."""
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    data_line = payload_json.decode("utf-8") if isinstance(payload_json, bytes) else payload_json
    lines.append(f"data: {data_line}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


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


class SseWrappedSentinelDetectionTests(unittest.TestCase):
    """#30 H1: FastMCP streamable-http defaults to SSE-wrapped tool-call
    responses (`is_json_response_enabled=False`). The detector must parse
    those bodies as well as plain JSON, without regressing either path."""

    def test_sse_wrapped_sentinel_is_detected(self):
        payload = _tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=True)
        body = _sse_frame(payload)
        self.assertTrue(_response_has_stale_sentinel(body))

    def test_sse_wrapped_clean_result_is_not_flagged(self):
        # Negative: a clean tool-result inside an SSE frame must not trip
        # the detector. This is the main regression guard qwen3 flagged.
        body = _sse_frame(_tool_result("messages: []"))
        self.assertFalse(_response_has_stale_sentinel(body))

    def test_sse_wrapped_chat_content_with_sentinel_substring(self):
        # A chat_read response that happens to carry the signature inside
        # user-authored text must never trip detection, even when SSE-wrapped.
        inner = json.dumps([
            {"sender": "user", "text": _STALE_SESSION_ERROR_SIGNATURE},
        ])
        body = _sse_frame(_tool_result(inner, is_error=False))
        self.assertFalse(_response_has_stale_sentinel(body))

    def test_sse_with_priming_event_before_data(self):
        # FastMCP can emit a priming event with empty `data:` before the real
        # payload. That priming frame must be skipped, not crash the parser.
        priming = b"event: message\nid: priming\ndata: \n\n"
        payload = _sse_frame(_tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=True))
        self.assertTrue(_response_has_stale_sentinel(priming + payload))

    def test_sse_with_multiple_data_frames(self):
        # Multi-frame body: clean frame followed by a stale-sentinel frame.
        # Detection must inspect every frame, not just the first.
        clean = _sse_frame(_tool_result("ok"))
        stale = _sse_frame(_tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=True))
        self.assertTrue(_response_has_stale_sentinel(clean + stale))

    def test_malformed_sse_body_returns_false_without_crashing(self):
        # Any of these bodies could appear if the upstream misbehaves.
        # None may crash, and none carry the sentinel so all must be False.
        for body in (
            b"event: message\ndata:\n\n",            # empty data
            b"event: message\ndata: not-json\n\n",   # non-JSON payload
            b"data: {\"broken\": ",                  # truncated JSON
            b"event: message",                       # no blank-line terminator
            b"\n\n\n\n",                             # only separators
            b"\x00\x01\x02garbage",                  # binary noise
        ):
            self.assertFalse(
                _response_has_stale_sentinel(body),
                f"malformed SSE body must not trip detection: {body!r}",
            )


class IterJsonrpcPayloadsTests(unittest.TestCase):
    """Covers the SSE-aware payload extractor added for #30 H1."""

    def test_plain_json_object_yields_once(self):
        body = _tool_result("ok")
        self.assertEqual(list(_iter_jsonrpc_payloads(body)), [json.loads(body)])

    def test_plain_json_array_yields_once(self):
        arr = json.dumps([{"jsonrpc": "2.0", "id": 1, "result": {}},
                          {"jsonrpc": "2.0", "id": 2, "result": {}}]).encode()
        out = list(_iter_jsonrpc_payloads(arr))
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], list)

    def test_sse_yields_each_data_line(self):
        body = _sse_frame(_tool_result("a")) + _sse_frame(_tool_result("b"))
        out = list(_iter_jsonrpc_payloads(body))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["result"]["content"][0]["text"], "a")
        self.assertEqual(out[1]["result"]["content"][0]["text"], "b")

    def test_empty_body_yields_nothing(self):
        self.assertEqual(list(_iter_jsonrpc_payloads(b"")), [])

    def test_malformed_frames_are_skipped(self):
        body = (
            b"event: message\ndata: not-json\n\n"
            + _sse_frame(_tool_result("ok"))
            + b"event: message\ndata: {\"broken\":\n\n"
        )
        out = list(_iter_jsonrpc_payloads(body))
        self.assertEqual(len(out), 1, "only the valid frame should be yielded")
        self.assertEqual(out[0]["result"]["content"][0]["text"], "ok")


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
        content_type: str = "application/json",
    ):
        if responses is None and token_responder is None:
            raise ValueError("_StubUpstream requires responses or token_responder")
        self._responses = list(responses or [])
        self._token_responder = token_responder
        self._barrier = barrier
        self._content_type = content_type
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
                self.send_header("Content-Type", stub._content_type)
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

    def test_session_is_cleared_after_successful_token_refresh(self):
        # #30 H2: when the stale-session hook successfully refreshes the
        # token, the cached upstream session_id must be invalidated so the
        # next MCP call re-runs initialize alongside the fresh token.
        # Without this, a full server restart can leave the proxy stuck on
        # a session that the new server instance never knew about.
        stale = _tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=True)
        fresh = _tool_result("messages: []")
        self._setup([stale, fresh])

        # Simulate a previously-initialized streamable-http session.
        self.proxy._set_upstream_session_id("pre-restart-session")

        def _hook():
            self.proxy.token = "new-token"
            return True

        self.proxy.on_stale_session = _hook

        self._post_tool_call("chat_read")

        self.assertIsNone(
            self.proxy._get_upstream_session_id(),
            "cached session_id must be cleared after successful token refresh "
            "so the next call forces a clean re-init",
        )

    def test_session_is_not_cleared_when_hook_declines_refresh(self):
        # Regression guard for #30 H2: if the hook returns False
        # (re-register failed), the token is NOT fresh, and H2 must NOT
        # fire. Session_id follows the standard "pick up from upstream
        # response header" path, NOT a hard reset to None.
        stale = _tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=True)
        self._setup([stale])
        self.proxy.on_stale_session = lambda: False

        self._post_tool_call("chat_read")

        # The stub advertises "stub-session" in response headers, which the
        # proxy normally caches. The H2 reset must NOT have fired, so we
        # should see the cached session from the response, not None.
        self.assertEqual(
            self.proxy._get_upstream_session_id(),
            "stub-session",
            "failed hook must leave session_id following the normal "
            "response-header update path, not the H2 reset path",
        )

    def test_session_is_not_cleared_on_clean_response(self):
        # Regression guard for #30 H2: the session-reset must fire only in
        # the stale-sentinel recovery path, never on normal responses.
        self._setup([_tool_result("ok")])

        def _hook_must_not_fire():
            raise AssertionError("hook must not fire on clean response")

        self.proxy.on_stale_session = _hook_must_not_fire

        self._post_tool_call("chat_read")

        self.assertEqual(
            self.proxy._get_upstream_session_id(),
            "stub-session",
            "clean response must cache session_id from upstream, not reset it",
        )

    def test_full_restart_recovery_with_sse_response(self):
        # End-to-end #30: simulate a full server restart. Upstream returns
        # SSE-wrapped bodies (FastMCP's default streamable-http shape).
        # The first response carries the stale-session sentinel, the retry
        # must land on a fresh response and reach the client with the
        # refreshed token. This is the exact scenario #30 was opened for:
        # before H1, the SSE-wrapped sentinel would go undetected and the
        # proxy would stay stale until a manual wrapper restart.
        stale_sse = _sse_frame(_tool_result(_STALE_SESSION_ERROR_SIGNATURE, is_error=True))
        fresh_sse = _sse_frame(_tool_result("messages: []"))

        self.upstream = _StubUpstream(
            responses=[stale_sse, fresh_sse],
            content_type="text/event-stream",
        )
        self.upstream.start()
        self.proxy = McpIdentityProxy(
            upstream_base=f"http://127.0.0.1:{self.upstream.port}",
            upstream_path="/mcp",
            agent_name="claude",
            instance_token="old-token",
        )
        self.assertTrue(self.proxy.start())
        self.proxy._set_upstream_session_id("pre-restart-session")

        hook_calls = []

        def _hook():
            hook_calls.append(True)
            self.proxy.token = "post-restart-token"
            return True

        self.proxy.on_stale_session = _hook

        body = self._post_tool_call("chat_read")

        self.assertEqual(len(hook_calls), 1,
                         "SSE-wrapped sentinel must trigger the hook exactly once")
        self.assertIn(b"messages: []", body,
                      "client must see the fresh retry payload")
        self.assertNotIn(_STALE_SESSION_ERROR_SIGNATURE.encode(), body,
                         "SSE-wrapped sentinel must not leak to the client after retry")
        tokens = self.upstream.tokens_seen
        self.assertEqual(tokens[0], "Bearer old-token",
                         "first request carries the stale token")
        self.assertEqual(tokens[1], "Bearer post-restart-token",
                         "retry carries the freshly re-registered token")
        self.assertIsNone(
            self.proxy._get_upstream_session_id(),
            "full-restart recovery must invalidate the cached session so the "
            "next call re-initializes against the restarted server",
        )

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
