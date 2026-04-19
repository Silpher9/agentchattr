import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router import Router


class RouterMentionTests(unittest.TestCase):
    def test_hyphenated_agent_name_is_parsed_as_full_mention(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("please ask @telegram-bridge to check")),
            {"telegram-bridge"},
        )

    def test_shorter_agent_name_does_not_match_prefix_of_hyphenated_unknown(self):
        router = Router(["telegram"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bridge check"), [])
        self.assertEqual(router.get_targets("ben", "@telegram-bridge check"), [])

    def test_longest_hyphenated_name_wins_when_prefix_agent_also_exists(self):
        router = Router(["telegram", "telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("@telegram-bridge check")),
            {"telegram-bridge"},
        )

    def test_unknown_exact_handle_still_does_not_route(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bot check"), [])
        self.assertEqual(router.get_targets("ben", "@telegram-bot check"), [])

    def test_fork_regression_hyphenated_mention_parses_without_truncation(self):
        # Fork regression guard for WP1 adoption of upstream 41fa636: confirm
        # the hyphenated-handle parsing change survives re-application on
        # our fork's router.py state and doesn't regress the boundary
        # behaviour (no false prefix match, no truncation of valid handle,
        # no false-positive on trailing hyphen or word-char).
        router = Router(["telegram", "telegram-bridge"], default_mention="none")
        # Valid full handle must be captured exactly, not truncated to prefix
        self.assertEqual(
            set(router.parse_mentions("heads-up: @telegram-bridge, please ack")),
            {"telegram-bridge"},
        )
        # Trailing punctuation still allows the match
        self.assertEqual(
            set(router.parse_mentions("@telegram-bridge!")),
            {"telegram-bridge"},
        )
        # Followed by another word char (no hyphen) must not match either handle
        self.assertEqual(router.parse_mentions("@telegrambridgex"), [])
        # Followed by another hyphen-word must not match either handle
        self.assertEqual(router.parse_mentions("@telegram-bridge-extra"), [])


if __name__ == "__main__":
    unittest.main()
