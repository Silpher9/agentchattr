"""Unit coverage for #28 step 2 — server-side prev_name reclaim hint.

Codex2 guardrails enforced here:
  - hint only applies when prev_name is in the same base family;
  - never releases a currently-registered (active) name;
  - touches reservations only (no rename/migrate/identity side-effects);
  - idempotent: repeated hints for the same prev_name do not mutate state.

Plus a scope-check that a bad hint never breaks the register call itself.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from registry import RuntimeRegistry
from store import MessageStore


class PrevNameReclaimHintTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)

        self.registry = RuntimeRegistry(data_dir=str(data_dir))
        self.registry.seed({
            "claude": {"label": "Claude", "color": "#aa77ff"},
            "codex":  {"label": "Codex",  "color": "#7788ff"},
        })
        self.store = MessageStore(str(data_dir / "log.jsonl"))

        self._saved = {"registry": app.registry, "store": app.store}
        app.registry = self.registry
        app.store = self.store

    def tearDown(self):
        app.registry = self._saved["registry"]
        app.store = self._saved["store"]
        self._tmp.cleanup()

    def _reserve(self, name: str):
        """Register + deregister so the reservation window is active."""
        base, _ = self.registry._parse_name(name)
        inst = self.registry.register(base)
        self.assertIsNotNone(inst)
        self.registry.deregister(inst["name"])
        self.assertIn(inst["name"], self.registry._reserved,
                      "setup: reservation must be held after deregister")
        return inst["name"]

    def test_releases_matching_reservation_in_same_family(self):
        reserved = self._reserve("claude")  # "claude"

        released = app._apply_prev_name_reclaim_hint("claude", reserved)

        self.assertTrue(released)
        self.assertNotIn(reserved, self.registry._reserved,
                         "matching hint must clear the reservation")

    def test_rejects_hint_from_different_family(self):
        # Reserve a codex name; hint arrives as base=claude — must not touch it.
        reserved = self._reserve("codex")

        released = app._apply_prev_name_reclaim_hint("claude", reserved)

        self.assertFalse(released)
        self.assertIn(reserved, self.registry._reserved,
                      "cross-family hint must never release another family's reservation")

    def test_never_releases_an_active_registered_name(self):
        # Directly register and keep active — hint with the live name must no-op.
        live = self.registry.register("claude")
        self.assertIsNotNone(live)
        live_name = live["name"]

        released = app._apply_prev_name_reclaim_hint("claude", live_name)

        self.assertFalse(released,
                         "hint must never affect an actively registered name")
        self.assertTrue(self.registry.is_registered(live_name))

    def test_no_reservation_returns_false_without_side_effects(self):
        # Nothing registered, nothing reserved — hint is a silent no-op.
        released = app._apply_prev_name_reclaim_hint("claude", "claude-7")
        self.assertFalse(released)
        self.assertEqual(self.registry._reserved, {})

    def test_hint_is_idempotent(self):
        reserved = self._reserve("claude")

        first = app._apply_prev_name_reclaim_hint("claude", reserved)
        second = app._apply_prev_name_reclaim_hint("claude", reserved)
        third = app._apply_prev_name_reclaim_hint("claude", reserved)

        self.assertTrue(first, "first call releases")
        self.assertFalse(second, "second call is a no-op")
        self.assertFalse(third, "third call is also a no-op")
        self.assertNotIn(reserved, self.registry._reserved)

    def test_hint_enables_slot1_reclaim_after_deregister(self):
        # End-to-end shape: wrapper registers as "claude" (slot 1), shuts down
        # cleanly (reservation on slot 1 for GRACE_PERIOD). A new wrapper now
        # registers within that window — with prev_name="claude" the
        # reservation is released first, so the newcomer reclaims slot 1
        # instead of being forced into slot 2.
        first = self.registry.register("claude")
        self.assertEqual(first["name"], "claude")
        self.registry.deregister("claude")
        self.assertIn("claude", self.registry._reserved,
                      "setup: slot 1 must be reserved after clean deregister")

        # With hint: reservation released → fresh register lands on slot 1.
        released = app._apply_prev_name_reclaim_hint("claude", "claude")
        self.assertTrue(released, "matching prev_name hint must release reservation")
        reclaimed = self.registry.register("claude")
        self.assertEqual(reclaimed["name"], "claude",
                         "after hint releases reservation, fresh register takes slot 1")

    def test_without_hint_reservation_forces_higher_slot(self):
        # Pre-check that the scenario the hint is designed to fix is real: a
        # reservation on slot 1 from a clean deregister forces a newcomer to
        # slot 2 when no hint is provided.
        first = self.registry.register("claude")
        self.assertEqual(first["name"], "claude")
        self.registry.deregister("claude")

        without_hint = self.registry.register("claude")
        self.assertNotEqual(
            without_hint["name"], "claude",
            "without the hint, the reservation forces the newcomer off slot 1",
        )

    def test_bad_hint_does_not_raise(self):
        # Any validation path inside the hint helper must be exception-safe so
        # a malformed or malicious hint can never 4xx the register call.
        for junk in ("", "   ", "not-a-family", "claude-", "-claude", "claude--1"):
            try:
                result = app._apply_prev_name_reclaim_hint("claude", junk)
            except Exception as exc:  # pragma: no cover — failure path
                self.fail(f"hint raised on junk input {junk!r}: {exc}")
            self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
