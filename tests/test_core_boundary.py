"""Core modules stay importable without Discord loaded."""

import subprocess
import sys
import textwrap
import unittest


class CoreBoundaryTests(unittest.TestCase):
    def test_core_imports_do_not_load_discord(self) -> None:
        script = textwrap.dedent(
            """
            import core.privacy
            core.privacy.bootstrap()
            import config
            import llm.models
            import memory.palace
            import core.runtime
            import core.tool_runner
            import tools.task_prioritizer
            import sys
            assert "discord" not in sys.modules
            print("ok")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
