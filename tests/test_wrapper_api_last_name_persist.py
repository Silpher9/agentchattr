"""Unit coverage for #29 — wrapper_api-side last-name persist + same-family
prev_name hint on initial register.

This is a port of the #28 tests to verify the wrapper_api.py implementation.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wrapper_api


class WrapperApiLastNamePersistTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # --- Persist ---

    def test_persist_writes_only_name_string(self):
        wrapper_api._persist_last_name(self.data_dir, "qwen", "qwen-2")
        path = wrapper_api._last_name_path(self.data_dir, "qwen")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text("utf-8"), "qwen-2")

    def test_persist_is_atomic_no_tmp_left_behind(self):
        wrapper_api._persist_last_name(self.data_dir, "qwen", "qwen")
        tmp = wrapper_api._last_name_path(self.data_dir, "qwen").with_suffix(".tmp")
        self.assertFalse(tmp.exists(),
                         "atomic write must clean up tmp via replace()")

    def test_persist_empty_name_is_noop(self):
        wrapper_api._persist_last_name(self.data_dir, "qwen", "")
        self.assertFalse(wrapper_api._last_name_path(self.data_dir, "qwen").exists())

    def test_persist_overwrites_previous_value(self):
        wrapper_api._persist_last_name(self.data_dir, "qwen", "qwen-2")
        wrapper_api._persist_last_name(self.data_dir, "qwen", "qwen")
        self.assertEqual(
            wrapper_api._last_name_path(self.data_dir, "qwen").read_text("utf-8"),
            "qwen",
        )

    # --- Load ---

    def test_load_returns_none_when_file_missing(self):
        self.assertIsNone(wrapper_api._load_last_name(self.data_dir, "qwen"))

    def test_load_returns_none_when_file_empty(self):
        path = wrapper_api._last_name_path(self.data_dir, "qwen")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", "utf-8")
        self.assertIsNone(wrapper_api._load_last_name(self.data_dir, "qwen"))

    def test_load_returns_base_name(self):
        wrapper_api._persist_last_name(self.data_dir, "qwen", "qwen")
        self.assertEqual(wrapper_api._load_last_name(self.data_dir, "qwen"), "qwen")

    def test_load_returns_numbered_same_family_name(self):
        wrapper_api._persist_last_name(self.data_dir, "qwen", "qwen-2")
        self.assertEqual(wrapper_api._load_last_name(self.data_dir, "qwen"), "qwen-2")

    def test_load_rejects_cross_family_name(self):
        path = wrapper_api._last_name_path(self.data_dir, "qwen")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("claude-2", "utf-8")
        self.assertIsNone(wrapper_api._load_last_name(self.data_dir, "qwen"))

    def test_load_rejects_malformed_suffix(self):
        path = wrapper_api._last_name_path(self.data_dir, "qwen")
        path.parent.mkdir(parents=True, exist_ok=True)
        for junk in ("qwen-", "qwen-abc", "qwen--1", "-qwen", "qwenx"):
            path.write_text(junk, "utf-8")
            self.assertIsNone(
                wrapper_api._load_last_name(self.data_dir, "qwen"),
                f"malformed name {junk!r} must not surface as a hint",
            )

    def test_load_tolerates_trailing_whitespace(self):
        path = wrapper_api._last_name_path(self.data_dir, "qwen")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("qwen-3\n", "utf-8")
        self.assertEqual(wrapper_api._load_last_name(self.data_dir, "qwen"), "qwen-3")


if __name__ == "__main__":
    unittest.main()
