"""Provider metadata shared between app.py and wrapper.py."""

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
    return AUTO_APPROVE_FLAGS.get(stem, "")
