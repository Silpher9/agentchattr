"""Unit coverage for #28 step 3 — wrapper-side last-name persist + same-family
prev_name hint on initial register.

Guardrails enforced here:
  - only the `name` string is persisted (no tokens/secrets);
  - write is atomic (tmp + replace) so a crash mid-write cannot corrupt
    the next startup's read;
  - load returns None when the persisted name belongs to another base
    family, so the server never receives a cross-family prev_name hint;
  - load returns None when the file is missing, empty, or malformed.

These tests exercise the helpers directly — no wrapper subprocess spawn —
so they run in the same suite as the rest of the #28 coverage.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wrapper


class WrapperLastNamePersistTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # --- Persist ---

    def test_persist_writes_only_name_string(self):
        wrapper._persist_last_name(self.data_dir, "claude", "claude-2")
        path = wrapper._last_name_path(self.data_dir, "claude")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text("utf-8"), "claude-2")

    def test_persist_is_atomic_no_tmp_left_behind(self):
        wrapper._persist_last_name(self.data_dir, "claude", "claude")
        tmp = wrapper._last_name_path(self.data_dir, "claude").with_suffix(".tmp")
        self.assertFalse(tmp.exists(),
                         "atomic write must clean up tmp via replace()")

    def test_persist_empty_name_is_noop(self):
        wrapper._persist_last_name(self.data_dir, "claude", "")
        self.assertFalse(wrapper._last_name_path(self.data_dir, "claude").exists())

    def test_persist_overwrites_previous_value(self):
        wrapper._persist_last_name(self.data_dir, "claude", "claude-2")
        wrapper._persist_last_name(self.data_dir, "claude", "claude")
        self.assertEqual(
            wrapper._last_name_path(self.data_dir, "claude").read_text("utf-8"),
            "claude",
        )

    # --- Load ---

    def test_load_returns_none_when_file_missing(self):
        self.assertIsNone(wrapper._load_last_name(self.data_dir, "claude"))

    def test_load_returns_none_when_file_empty(self):
        path = wrapper._last_name_path(self.data_dir, "claude")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", "utf-8")
        self.assertIsNone(wrapper._load_last_name(self.data_dir, "claude"))

    def test_load_returns_base_name(self):
        wrapper._persist_last_name(self.data_dir, "claude", "claude")
        self.assertEqual(wrapper._load_last_name(self.data_dir, "claude"), "claude")

    def test_load_returns_numbered_same_family_name(self):
        wrapper._persist_last_name(self.data_dir, "claude", "claude-2")
        self.assertEqual(wrapper._load_last_name(self.data_dir, "claude"), "claude-2")

    def test_load_rejects_cross_family_name(self):
        # Simulate a corrupted/stolen file: claude last-name file contains
        # a codex name. The family filter must refuse to surface that as
        # a prev_name hint — the server-side guardrail also rejects this,
        # but we want the wrapper to never even send it.
        path = wrapper._last_name_path(self.data_dir, "claude")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("codex-2", "utf-8")
        self.assertIsNone(wrapper._load_last_name(self.data_dir, "claude"))

    def test_load_rejects_malformed_suffix(self):
        path = wrapper._last_name_path(self.data_dir, "claude")
        path.parent.mkdir(parents=True, exist_ok=True)
        for junk in ("claude-", "claude-abc", "claude--1", "-claude", "claudex"):
            path.write_text(junk, "utf-8")
            self.assertIsNone(
                wrapper._load_last_name(self.data_dir, "claude"),
                f"malformed name {junk!r} must not surface as a hint",
            )

    def test_load_tolerates_trailing_whitespace(self):
        path = wrapper._last_name_path(self.data_dir, "claude")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("claude-3\n", "utf-8")
        self.assertEqual(wrapper._load_last_name(self.data_dir, "claude"), "claude-3")

    # --- Round-trip ---

    def test_round_trip_after_restart(self):
        # Simulate: wrapper A registered as claude-2 and persisted the name.
        # After a restart, wrapper B's startup reads the same file to build
        # the prev_name hint. This is the exact path the initial register
        # now uses before calling _register_instance.
        wrapper._persist_last_name(self.data_dir, "claude", "claude-2")
        hint = wrapper._load_last_name(self.data_dir, "claude")
        self.assertEqual(hint, "claude-2")


if __name__ == "__main__":
    unittest.main()
