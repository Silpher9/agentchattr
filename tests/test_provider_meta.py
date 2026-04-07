"""Tests for provider_meta.get_auto_approve_flag()."""

import pytest
from provider_meta import get_auto_approve_flag

SKIP = "--dangerously-skip-permissions"
BYPASS = "--dangerously-bypass-approvals-and-sandbox"
YOLO = "--yolo"


@pytest.mark.parametrize(
    "command, expected",
    [
        # Exact matches
        ("claude", SKIP),
        ("/usr/bin/claude", SKIP),
        ("claude.exe", SKIP),
        ("codex", BYPASS),
        ("gemini", YOLO),
        ("qwen", YOLO),
        # Prefix matches (wrapper scripts)
        ("claude2-agent", SKIP),
        ("codex2-agent", BYPASS),
        ("codex-extra", BYPASS),
        ("gemini_custom", YOLO),
        # No match — alpha char after provider name
        ("codexperimental", ""),
        ("claudette", ""),
        # Unknown provider
        ("mycli", ""),
        ("kimi", ""),
        # Empty / missing command
        ("", ""),
    ],
)
def test_get_auto_approve_flag(command, expected):
    assert get_auto_approve_flag({"command": command}) == expected


def test_missing_command_key():
    assert get_auto_approve_flag({}) == ""
