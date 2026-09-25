"""Explicit tool validation. Ordinary chat does not receive mutation-capable schemas."""

from __future__ import annotations

from typing import Any

from tools.registry import AVAILABLE_TOOLS, TOOL_POLICIES, TOOL_SCHEMAS, ToolPolicy

MANUAL_TOOL_NAMES = (
    "get_morning_briefing",
    "list_active_automations",
    "recall",
    "ask_notes",
)
SCHEDULABLE_TOOL_NAMES = ("get_morning_briefing",)


class ToolRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def policy_for(name: str) -> ToolPolicy:
    return TOOL_POLICIES.get(name, ToolPolicy())


def manual_tool_schemas() -> list[dict[str, Any]]:
    allowed = set(MANUAL_TOOL_NAMES)
    schemas = []
    for schema in TOOL_SCHEMAS:
        name = schema.get("function", {}).get("name")
        policy = policy_for(name or "")
        if name in allowed and policy.manual and not policy.network:
            schemas.append(schema)
    return schemas


def assert_manual_execution(name: str) -> None:
    if name not in MANUAL_TOOL_NAMES:
        raise ToolRejected("tool_not_allowed", "That tool is not available.")
    if name in {"recall", "ask_notes"}:
        return
    if name in AVAILABLE_TOOLS:
        policy = policy_for(name)
        if not policy.manual or policy.network:
            raise ToolRejected("tool_not_allowed", "That tool is not available.")


def assert_schedulable(name: str) -> None:
    if name not in SCHEDULABLE_TOOL_NAMES:
        raise ToolRejected("tool_not_schedulable", "That tool cannot be scheduled.")
    policy = policy_for(name)
    if not policy.schedulable or policy.network or policy.mutation:
        raise ToolRejected("tool_not_schedulable", "That tool cannot be scheduled.")
