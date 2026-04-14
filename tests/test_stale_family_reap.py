"""Unit coverage for #28 register-time stale-family reap.

Covers the scenarios codex2 flagged in the review gate:
  - stale entry (heartbeat older than reclaim-grace) is reaped
  - active entry (fresh heartbeat) is never reaped, even in the same family
  - entry without a recorded heartbeat is left alone (no last_seen == 0 kills)
  - scope is limited to the requested base family
  - leave message is debounced against _posted_leave
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
import mcp_bridge
from registry import RuntimeRegistry
from store import MessageStore


class StaleFamilyReapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)

        # Minimal registry seeded with two families so we can assert scope.
        self.registry = RuntimeRegistry(data_dir=str(data_dir))
        self.registry.seed({
            "claude": {"label": "Claude", "color": "#aa77ff"},
            "codex":  {"label": "Codex",  "color": "#7788ff"},
        })

        self.store = MessageStore(str(data_dir / "log.jsonl"))

        # Swap module globals into the configured instances — restored in tearDown.
        self._saved = {
            "registry": app.registry,
            "store": app.store,
            "posted_leave": set(app._posted_leave),
            "bridge_registry": mcp_bridge.registry,
            "bridge_store": mcp_bridge.store,
            "presence": dict(mcp_bridge._presence),
            "activity": dict(mcp_bridge._activity),
        }
        app.registry = self.registry
        app.store = self.store
        app._posted_leave.clear()
        mcp_bridge.registry = self.registry
        mcp_bridge.store = self.store
        mcp_bridge._presence.clear()
        mcp_bridge._activity.clear()

    def tearDown(self):
        app.registry = self._saved["registry"]
        app.store = self._saved["store"]
        app._posted_leave.clear()
        app._posted_leave.update(self._saved["posted_leave"])
        mcp_bridge.registry = self._saved["bridge_registry"]
        mcp_bridge.store = self._saved["bridge_store"]
        mcp_bridge._presence.clear()
        mcp_bridge._presence.update(self._saved["presence"])
        mcp_bridge._activity.clear()
        mcp_bridge._activity.update(self._saved["activity"])
        self._tmp.cleanup()

    def _register(self, base: str) -> str:
        inst = self.registry.register(base)
        self.assertIsNotNone(inst, f"failed to register {base}")
        return inst["name"]

    def _set_presence(self, name: str, seconds_ago: float):
        mcp_bridge._presence[name] = time.time() - seconds_ago

    def test_reaps_only_stale_same_family_entries(self):
        # Simulate: stale `claude` (dead wrapper) + fresh `codex` (other family).
        claude = self._register("claude")
        codex = self._register("codex")
        self._set_presence(claude, app._STALE_FAMILY_REAP_GRACE + 5)
        self._set_presence(codex, 1)

        reaped = app._reap_stale_family("claude")

        self.assertEqual(reaped, [claude])
        self.assertFalse(self.registry.is_registered(claude))
        self.assertTrue(self.registry.is_registered(codex),
                        "other-family entries must not be touched")
        self.assertNotIn(claude, mcp_bridge._presence,
                         "presence for the reaped name must be purged")

    def test_active_family_member_is_not_reaped(self):
        # Fresh heartbeat inside reclaim-grace must survive even if stale-ish.
        claude = self._register("claude")
        self._set_presence(claude, app._STALE_FAMILY_REAP_GRACE - 1)

        reaped = app._reap_stale_family("claude")

        self.assertEqual(reaped, [])
        self.assertTrue(self.registry.is_registered(claude))

    def test_missing_presence_is_not_reaped(self):
        # Brand new registration whose presence touch is racing with reap —
        # last_seen == 0 must not trip the reap path.
        claude = self._register("claude")
        # No _set_presence call — presence map is empty for this name.

        reaped = app._reap_stale_family("claude")

        self.assertEqual(reaped, [])
        self.assertTrue(self.registry.is_registered(claude))

    def test_reap_posts_single_leave_message(self):
        claude = self._register("claude")
        self._set_presence(claude, app._STALE_FAMILY_REAP_GRACE + 5)

        app._reap_stale_family("claude")

        leaves = [m for m in self.store.get_recent(count=500)
                  if m.get("sender") == claude and m.get("type") == "leave"]
        self.assertEqual(len(leaves), 1,
                         "reap must post exactly one leave for the stale name")
        self.assertIn(claude, app._posted_leave,
                      "reap must debounce via _posted_leave")

        # Re-running must not double-post: debounce honored, no registry entry left.
        app._reap_stale_family("claude")
        leaves = [m for m in self.store.get_recent(count=500)
                  if m.get("sender") == claude and m.get("type") == "leave"]
        self.assertEqual(len(leaves), 1)

    def test_reap_before_register_prevents_duplicate_slotting(self):
        # End-to-end shape of the #28 bug: an abandoned claude slot plus a
        # fresh wrapper trying to re-register. Without the reap, the fresh
        # register would slot as claude-2 with the dead one renamed to
        # claude-1. With the reap, the fresh register cleanly takes slot 1.
        dead = self._register("claude")
        self.assertEqual(dead, "claude")
        self._set_presence(dead, app._STALE_FAMILY_REAP_GRACE + 5)

        app._reap_stale_family("claude")
        fresh = self.registry.register("claude")

        self.assertEqual(fresh["name"], "claude",
                         "fresh registration must reclaim slot 1 after reap")
        self.assertEqual(fresh["slot"], 1)
        family_names = [i["name"] for i in self.registry.get_instances_for("claude")]
        self.assertEqual(family_names, ["claude"],
                         "family must not contain duplicate slots after reap")


if __name__ == "__main__":
    unittest.main()
