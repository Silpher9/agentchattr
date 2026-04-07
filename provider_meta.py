"""Provider metadata shared between app.py and wrapper.py."""

import re
from pathlib import PurePath

# Maps provider command name → auto-approve flag
AUTO_APPROVE_FLAGS: dict[str, str] = {
    "claude": "--dangerously-skip-permissions",
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "gemini": "--yolo",
    "qwen": "--yolo",
}


def get_auto_approve_flag(agent_cfg: dict) -> str:
    """Resolve auto-approve flag based on agent's command, not config key."""
    command = agent_cfg.get("command", "")
    if not command:
        return ""
    # Extract binary name: handles "/usr/bin/claude", "C:\\...\\claude.exe", "claude"
    stem = PurePath(command).stem
    # Exact match first
    if stem in AUTO_APPROVE_FLAGS:
        return AUTO_APPROVE_FLAGS[stem]
    # Prefix match: provider name followed by non-alpha (digit, -, _)
    # e.g. claude2-agent → claude, codex2-agent → codex
    for provider, flag in AUTO_APPROVE_FLAGS.items():
        if re.match(rf"^{re.escape(provider)}[^a-zA-Z]", stem):
            return flag
    return ""
