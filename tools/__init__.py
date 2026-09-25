"""Explicit registration for local tools. This package does not scan its directory."""

from __future__ import annotations


def register_tools() -> None:
    """Import modules whose decorators register the safe local tools."""
    import tools.morning_brief  # noqa: F401
    import tools.task_prioritizer  # noqa: F401
