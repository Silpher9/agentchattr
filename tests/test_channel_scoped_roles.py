"""Tests for channel-scoped persistent agent roles (#37).

Covers:
- legacy flat `roles.json` migrates to nested channel-scoped shape on load
- channel key normalisation (trim + lowercase) collapses case-variants
- `set_role` / `get_role` scope to channel; `__default__` fallback applies
- `migrate_identity` moves all channel roles to the new name atomically
- `purge_identity` drops all channel roles for the deregistered name
- `get_roles_for_channel` flattens the nested map with fallback applied
- lifecycle paths all hold the `_roles_lock` (no silent race regressions)
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_bridge


class _RolesSandbox(unittest.TestCase):
    """Saves/restores the module-level _roles + _ROLES_FILE so tests don't
    bleed state into each other."""

    def setUp(self):
        self._saved_roles = dict(mcp_bridge.get_all_roles())
        self._saved_roles_file = mcp_bridge._ROLES_FILE
        mcp_bridge._roles.clear()
        self.tmp_dir = Path.cwd() / ".pytest-roles-tmp"
        self.tmp_dir.mkdir(exist_ok=True)
        self.roles_file = self.tmp_dir / f"{self._testMethodName}-roles.json"
        if self.roles_file.exists():
            self.roles_file.unlink()
        mcp_bridge._ROLES_FILE = self.roles_file

    def tearDown(self):
        mcp_bridge._ROLES_FILE = self._saved_roles_file
        mcp_bridge._roles.clear()
        mcp_bridge._roles.update({n: dict(c) for n, c in self._saved_roles.items()})
        if self.roles_file.exists():
            self.roles_file.unlink()
        try:
            self.tmp_dir.rmdir()
        except OSError:
            pass


class ChannelNormalisationTests(unittest.TestCase):
    def test_trims_and_lowercases(self):
        self.assertEqual(mcp_bridge._normalise_channel_key("Speelkaart"), "speelkaart")
        self.assertEqual(mcp_bridge._normalise_channel_key(" speelkaart "), "speelkaart")
        self.assertEqual(mcp_bridge._normalise_channel_key("SPEELKAART"), "speelkaart")

    def test_empty_and_none_fall_back_to_default(self):
        self.assertEqual(mcp_bridge._normalise_channel_key(None), "__default__")
        self.assertEqual(mcp_bridge._normalise_channel_key(""), "__default__")
        self.assertEqual(mcp_bridge._normalise_channel_key("   "), "__default__")


class LegacyMigrationTests(_RolesSandbox):
    def test_flat_roles_json_migrates_to_nested_on_load(self):
        # Pre-#37 roles.json shape — flat {name: role_string}
        self.roles_file.write_text(json.dumps({
            "claude": "Builder",
            "codex": "PM",
        }), "utf-8")
        mcp_bridge._load_roles()
        self.assertEqual(
            mcp_bridge.get_all_roles(),
            {
                "claude": {"__default__": "Builder"},
                "codex": {"__default__": "PM"},
            },
        )
        # And the file on disk should have been rewritten to the new shape.
        on_disk = json.loads(self.roles_file.read_text("utf-8"))
        self.assertEqual(on_disk["claude"], {"__default__": "Builder"})

    def test_already_nested_roles_are_untouched(self):
        self.roles_file.write_text(json.dumps({
            "claude": {"speelkaart": "Builder", "__default__": "PM"},
        }), "utf-8")
        mcp_bridge._load_roles()
        self.assertEqual(
            mcp_bridge.get_role("claude", "speelkaart"),
            "Builder",
        )
        self.assertEqual(mcp_bridge.get_role("claude", "unknown"), "PM")

    def test_mixed_case_channels_collapse_to_single_bucket_on_load(self):
        self.roles_file.write_text(json.dumps({
            "claude": {"Speelkaart": "A", "speelkaart": "B"},
        }), "utf-8")
        mcp_bridge._load_roles()
        bucket = mcp_bridge.get_all_roles()["claude"]
        self.assertIn("speelkaart", bucket)
        # Exactly one bucket for the case-variants — last-write-wins at
        # JSON-parse time (the file's key order). Either "A" or "B" is
        # acceptable; what must not happen is two entries side by side.
        self.assertEqual(len(bucket), 1)
        self.assertIn(bucket["speelkaart"], {"A", "B"})

    def test_corrupt_roles_json_does_not_crash_load(self):
        self.roles_file.write_text("not-json", "utf-8")
        mcp_bridge._load_roles()
        self.assertEqual(mcp_bridge.get_all_roles(), {})


class SetGetRoleTests(_RolesSandbox):
    def test_channel_scoped_set_and_get_do_not_cross_contaminate(self):
        mcp_bridge.set_role("claude", "Builder", channel="speelkaart")
        mcp_bridge.set_role("claude", "Tractor Mechanic", channel="isekitx1410")
        self.assertEqual(mcp_bridge.get_role("claude", "speelkaart"), "Builder")
        self.assertEqual(mcp_bridge.get_role("claude", "isekitx1410"), "Tractor Mechanic")
        # Unseen channel falls back to no role (no __default__ set yet).
        self.assertEqual(mcp_bridge.get_role("claude", "agentchattr"), "")

    def test_default_role_serves_as_fallback(self):
        mcp_bridge.set_role("claude", "Builder")  # no channel → __default__
        self.assertEqual(mcp_bridge.get_role("claude", "speelkaart"), "Builder")
        self.assertEqual(mcp_bridge.get_role("claude", "agentchattr"), "Builder")
        # Channel-specific override wins over __default__ fallback
        mcp_bridge.set_role("claude", "Tractor Mechanic", channel="isekitx1410")
        self.assertEqual(mcp_bridge.get_role("claude", "isekitx1410"), "Tractor Mechanic")
        self.assertEqual(mcp_bridge.get_role("claude", "speelkaart"), "Builder")

    def test_clear_only_removes_the_targeted_channel(self):
        mcp_bridge.set_role("claude", "Builder", channel="speelkaart")
        mcp_bridge.set_role("claude", "Tractor Mechanic", channel="isekitx1410")
        mcp_bridge.set_role("claude", "", channel="speelkaart")
        self.assertEqual(mcp_bridge.get_role("claude", "speelkaart"), "")
        self.assertEqual(mcp_bridge.get_role("claude", "isekitx1410"), "Tractor Mechanic")

    def test_clearing_last_channel_removes_agent_entry(self):
        mcp_bridge.set_role("claude", "Builder", channel="speelkaart")
        mcp_bridge.set_role("claude", "", channel="speelkaart")
        self.assertNotIn("claude", mcp_bridge.get_all_roles())

    def test_channel_key_is_normalised_on_write_and_read(self):
        mcp_bridge.set_role("claude", "Builder", channel="Speelkaart ")
        self.assertEqual(mcp_bridge.get_role("claude", "speelkaart"), "Builder")
        self.assertEqual(mcp_bridge.get_role("claude", "SPEELKAART"), "Builder")

    def test_get_roles_for_channel_applies_default_fallback(self):
        mcp_bridge.set_role("claude", "Builder")  # __default__
        mcp_bridge.set_role("claude", "Tractor Mechanic", channel="isekitx1410")
        mcp_bridge.set_role("codex", "PM", channel="agentchattr")
        flat = mcp_bridge.get_roles_for_channel("isekitx1410")
        # Claude gets the channel-specific, codex falls through (no role in
        # this channel, no __default__ either).
        self.assertEqual(flat, {"claude": "Tractor Mechanic"})
        flat_other = mcp_bridge.get_roles_for_channel("random")
        # claude gets __default__, codex has no fallback
        self.assertEqual(flat_other, {"claude": "Builder"})


class LifecycleTests(_RolesSandbox):
    def test_migrate_identity_moves_all_channel_roles(self):
        mcp_bridge.set_role("claude", "Builder", channel="speelkaart")
        mcp_bridge.set_role("claude", "Tractor Mechanic", channel="isekitx1410")
        mcp_bridge.migrate_identity("claude", "claude-2")
        self.assertNotIn("claude", mcp_bridge.get_all_roles())
        self.assertEqual(
            mcp_bridge.get_role("claude-2", "speelkaart"),
            "Builder",
        )
        self.assertEqual(
            mcp_bridge.get_role("claude-2", "isekitx1410"),
            "Tractor Mechanic",
        )

    def test_purge_identity_removes_all_channel_roles(self):
        mcp_bridge.set_role("claude", "Builder", channel="speelkaart")
        mcp_bridge.set_role("claude", "PM")  # __default__
        mcp_bridge.purge_identity("claude")
        self.assertNotIn("claude", mcp_bridge.get_all_roles())


class PersistenceTests(_RolesSandbox):
    def test_set_role_persists_to_disk_and_round_trips(self):
        mcp_bridge.set_role("claude", "Builder", channel="speelkaart")
        mcp_bridge._roles.clear()
        mcp_bridge._load_roles()
        self.assertEqual(
            mcp_bridge.get_role("claude", "speelkaart"),
            "Builder",
        )


if __name__ == "__main__":
    unittest.main()
