"""Unit tests for local morning brief snapshot helpers."""

import unittest

from tools.morning_brief import collect_local_system_snapshot


class MorningBriefSnapshotTests(unittest.TestCase):
    """Validate snapshot format and key fields."""

    def test_snapshot_contains_expected_labels(self) -> None:
        snapshot = collect_local_system_snapshot()

        self.assertIn("- Time:", snapshot)
        self.assertIn("- Host:", snapshot)
        self.assertIn("- Platform:", snapshot)
        self.assertIn("- Python:", snapshot)
        self.assertIn("- CPU load", snapshot)
        self.assertIn("- Disk (/):", snapshot)


if __name__ == "__main__":
    unittest.main()
