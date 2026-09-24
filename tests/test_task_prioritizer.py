"""Deterministic Eisenhower core after the Discord cog was removed."""

import unittest

from tools.task_prioritizer import EisenhowerPrioritizer, TaskInput, Quadrant


class PrioritizerCoreTests(unittest.TestCase):
    def test_urgent_important_task_sorts_first(self) -> None:
        prioritizer = EisenhowerPrioritizer()
        tasks = [
            TaskInput(name="Later", description="strategic roadmap", due_time="", user_tags=["important"]),
            TaskInput(name="Now", description="urgent production incident", due_time="", user_tags=["critical"]),
        ]
        classified = prioritizer.classify_tasks(tasks)
        self.assertEqual(classified[0].task.name, "Now")
        self.assertEqual(classified[0].quadrant, Quadrant.DO_NOW)
        report = prioritizer.build_report(tasks)
        self.assertIn("Start now", report)
        self.assertNotIn("discord", report.lower())

    def test_empty_input(self) -> None:
        report = EisenhowerPrioritizer().build_report([])
        self.assertIn("No tasks", report)


if __name__ == "__main__":
    unittest.main()