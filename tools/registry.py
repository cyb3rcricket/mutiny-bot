"""Registry for MutinyBot AI tool functions and their JSON schemas."""

from collections.abc import Callable
from typing import Any

AVAILABLE_TOOLS: dict[str, Callable[..., Any]] = {}
TOOL_SCHEMAS: list[dict[str, Any]] = []


def ai_tool(name: str, description: str, parameters: dict[str, Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as an AI tool and store its schema for model tool-calling."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        AVAILABLE_TOOLS[name] = func

        # Copy to avoid mutating the caller's dict literal.
        normalized_parameters = dict(parameters) if parameters is not None else {}
        if not normalized_parameters.get("type"):
            normalized_parameters["type"] = "object"
        if "properties" not in normalized_parameters:
            normalized_parameters["properties"] = {}
        if "required" not in normalized_parameters:
            normalized_parameters["required"] = []

        function_schema = {
            "name": name,
            "description": description,
            "parameters": normalized_parameters,
        }

        TOOL_SCHEMAS.append(
            {
                "type": "function",
                "function": function_schema,
            }
        )
        return func

    return decorator
