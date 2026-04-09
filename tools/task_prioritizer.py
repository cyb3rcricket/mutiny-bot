"""Task prioritizer tool: real Eisenhower matrix classification and action planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from tools.registry import ai_tool


PRIORITY_KEYWORDS = {
    "eisenhower",
    "urgent",
    "urgency",
    "important",
    "priority",
    "prioritize",
    "prioritise",
    "decision",
    "decide",
    "triage",
    "matrix",
}


class Quadrant(str, Enum):
    """Eisenhower matrix quadrants."""

    DO_NOW = "Q1: Urgent + Important"
    SCHEDULE = "Q2: Not Urgent + Important"
    DELEGATE = "Q3: Urgent + Not Important"
    ELIMINATE = "Q4: Not Urgent + Not Important"


@dataclass
class TaskInput:
    """Normalized task input received from tool callers."""

    name: str
    description: str
    due_time: str
    user_tags: list[str]


@dataclass
class ClassifiedTask:
    """Task with computed urgency/importance and recommended action."""

    task: TaskInput
    quadrant: Quadrant
    urgency_score: int
    importance_score: int
    next_step: str


class EisenhowerPrioritizer:
    """Concrete Eisenhower matrix classifier with deterministic scoring."""

    _urgent_keywords = {
        "urgent",
        "asap",
        "now",
        "today",
        "critical",
        "blocker",
        "p0",
        "p1",
        "immediate",
        "deadline",
    }
    _important_keywords = {
        "important",
        "strategic",
        "impact",
        "customer",
        "security",
        "revenue",
        "production",
        "incident",
        "compliance",
        "roadmap",
        "core",
    }

    @staticmethod
    def _parse_due_time(raw_due_time: str) -> datetime | None:
        if not raw_due_time:
            return None

        candidate = raw_due_time.strip()
        if not candidate:
            return None

        parsers = (
            lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
            lambda x: datetime.strptime(x, "%Y-%m-%d %H:%M"),
            lambda x: datetime.strptime(x, "%Y-%m-%d"),
        )
        for parser in parsers:
            try:
                return parser(candidate)
            except ValueError:
                continue

        return None

    @classmethod
    def _score_urgency(cls, task: TaskInput, now: datetime) -> int:
        score = 0
        due_dt = cls._parse_due_time(task.due_time)
        if due_dt is not None:
            delta_hours = (due_dt - now).total_seconds() / 3600
            if delta_hours <= 4:
                score += 3
            elif delta_hours <= 24:
                score += 2
            elif delta_hours <= 72:
                score += 1

        text = f"{task.name} {task.description} {' '.join(task.user_tags)}".lower()
        if any(keyword in text for keyword in cls._urgent_keywords):
            score += 2

        return min(score, 5)

    @classmethod
    def _score_importance(cls, task: TaskInput) -> int:
        score = 0
        text = f"{task.name} {task.description} {' '.join(task.user_tags)}".lower()
        if any(keyword in text for keyword in cls._important_keywords):
            score += 2

        if "high-impact" in text or "long-term" in text or "strategic" in text:
            score += 1

        if any(tag.lower() in {"high", "important", "critical", "strategic"} for tag in task.user_tags):
            score += 1

        return min(score, 5)

    @staticmethod
    def _choose_quadrant(urgency_score: int, importance_score: int) -> Quadrant:
        is_urgent = urgency_score >= 2
        is_important = importance_score >= 2

        if is_urgent and is_important:
            return Quadrant.DO_NOW
        if not is_urgent and is_important:
            return Quadrant.SCHEDULE
        if is_urgent and not is_important:
            return Quadrant.DELEGATE
        return Quadrant.ELIMINATE

    @staticmethod
    def _next_step_for(quadrant: Quadrant, task: TaskInput) -> str:
        if quadrant == Quadrant.DO_NOW:
            return f"Start now: define first deliverable for '{task.name}' and finish a first pass within the next focused block."
        if quadrant == Quadrant.SCHEDULE:
            return f"Schedule '{task.name}' on calendar with a protected time block and a clear completion target."
        if quadrant == Quadrant.DELEGATE:
            return f"Delegate '{task.name}' to an owner, include deadline/context, and request a status check-in."
        return f"Drop, defer, or batch '{task.name}' after Q1-Q3 work is complete."

    def classify_tasks(self, tasks: list[TaskInput]) -> list[ClassifiedTask]:
        now = datetime.now()
        classified: list[ClassifiedTask] = []

        for task in tasks:
            urgency_score = self._score_urgency(task, now)
            importance_score = self._score_importance(task)
            quadrant = self._choose_quadrant(urgency_score, importance_score)
            classified.append(
                ClassifiedTask(
                    task=task,
                    quadrant=quadrant,
                    urgency_score=urgency_score,
                    importance_score=importance_score,
                    next_step=self._next_step_for(quadrant, task),
                )
            )

        quadrant_order = {
            Quadrant.DO_NOW: 0,
            Quadrant.SCHEDULE: 1,
            Quadrant.DELEGATE: 2,
            Quadrant.ELIMINATE: 3,
        }

        classified.sort(
            key=lambda item: (
                quadrant_order[item.quadrant],
                -item.importance_score,
                -item.urgency_score,
                item.task.name.lower(),
            )
        )
        return classified

    def build_report(self, tasks: list[TaskInput]) -> str:
        if not tasks:
            return "No tasks were provided. Send a non-empty tasks list with name, description, due_time, and user_tags."

        classified = self.classify_tasks(tasks)

        lines = [
            "Eisenhower Prioritized Action List",
            "",
        ]
        for idx, item in enumerate(classified, start=1):
            due_fragment = f" | due: {item.task.due_time}" if item.task.due_time else ""
            lines.append(
                f"{idx}. {item.task.name} [{item.quadrant.value}]"
                f" | urgency={item.urgency_score} importance={item.importance_score}{due_fragment}"
            )
            lines.append(f"   Next step: {item.next_step}")

        return "\n".join(lines)


def _normalize_task(raw: dict[str, Any]) -> TaskInput:
    return TaskInput(
        name=str(raw.get("name", "")).strip(),
        description=str(raw.get("description", "")).strip(),
        due_time=str(raw.get("due_time", "")).strip(),
        user_tags=[str(tag).strip() for tag in raw.get("user_tags", []) if str(tag).strip()],
    )


def should_trigger_prioritizer(user_text: str) -> bool:
    """Keyword-level intent hook for priority-related chat requests."""
    normalized = (user_text or "").lower()
    return any(keyword in normalized for keyword in PRIORITY_KEYWORDS)


@ai_tool(
    name="prioritize_tasks",
    description=(
        "Classify tasks with a real Eisenhower urgent/important matrix and return concrete prioritized next actions. "
        "Use this when the user asks about Eisenhower, urgency, priorities, task triage, or decision-making."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "List of task objects to prioritize.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "due_time": {"type": "string"},
                        "user_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["name", "description", "due_time", "user_tags"],
                },
            }
        },
        "required": ["tasks"],
    },
)
async def prioritize_tasks(tasks: list[dict[str, Any]]) -> str:
    """Run real Eisenhower prioritization and return a concrete action list."""
    normalized_tasks = [_normalize_task(task) for task in tasks if isinstance(task, dict)]
    prioritizer = EisenhowerPrioritizer()
    return prioritizer.build_report(normalized_tasks)


class TaskPrioritizerCog(commands.Cog):
    """Slash command + intent bridge for Eisenhower prioritization."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Inject a one-time intent hint so normal chat can trigger prioritize_tasks."""
        if message.author.bot:
            return

        if not should_trigger_prioritizer(message.content):
            return

        user_id = str(message.author.id)
        hint = (
            "Conversation mode: user requested real prioritization. "
            "If tasks are present, call prioritize_tasks and return concrete actions. "
            "If missing tasks, ask for name, description, due_time, and user_tags."
        )
        await self.bot.db_manager.insert_history_message(user_id=user_id, role="system", content=hint)

    @app_commands.command(
        name="prioritize_tasks",
        description="Prioritize tasks using the Eisenhower urgent/important matrix.",
    )
    @app_commands.describe(
        tasks_json=(
            "JSON array of tasks: [{\"name\":\"...\",\"description\":\"...\",\"due_time\":\"YYYY-MM-DD HH:MM\",\"user_tags\":[\"important\"]}]"
        )
    )
    async def prioritize_tasks_command(self, interaction: discord.Interaction, tasks_json: str) -> None:
        """Slash command for deterministic Eisenhower prioritization."""
        try:
            payload = json.loads(tasks_json)
        except json.JSONDecodeError:
            await interaction.response.send_message(
                "Invalid JSON. Provide a JSON array of task objects.",
                ephemeral=True,
            )
            return

        if not isinstance(payload, list):
            await interaction.response.send_message(
                "Invalid payload. Expected a JSON array of task objects.",
                ephemeral=True,
            )
            return

        tasks: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            task = {
                "name": str(item.get("name", "")).strip(),
                "description": str(item.get("description", "")).strip(),
                "due_time": str(item.get("due_time", "")).strip(),
                "user_tags": item.get("user_tags", []) if isinstance(item.get("user_tags", []), list) else [],
            }
            if task["name"]:
                tasks.append(task)

        if not tasks:
            await interaction.response.send_message(
                "No valid tasks found. Include at least one task with a non-empty name.",
                ephemeral=True,
            )
            return

        result = await prioritize_tasks(tasks)
        # Keep slash output safe for Discord limits.
        if len(result) > 1900:
            result = result[:1900].rstrip() + "..."

        await interaction.response.send_message(result)


async def setup(bot: Any) -> None:
    """Register slash command and chat intent hook for task prioritization."""
    await bot.add_cog(TaskPrioritizerCog(bot))
