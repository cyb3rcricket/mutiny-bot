"""Registry for MutinyBot AI tool functions and their JSON schemas."""

from collections.abc import Callable
from typing import Any, Optional

AVAILABLE_TOOLS: dict[str, Callable[..., Any]] = {}
TOOL_SCHEMAS: list[dict[str, Any]] = []
_TOOL_SCHEMAS_BY_NAME: dict[str, dict[str, Any]] = {}


def _normalize_parameters(parameters: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Normalize a function parameters schema to an object-shaped JSON schema."""
    normalized_parameters = parameters or {"type": "object", "properties": {}, "required": []}
    if not normalized_parameters.get("type"):
        normalized_parameters["type"] = "object"
    if "properties" not in normalized_parameters:
        normalized_parameters["properties"] = {}
    if "required" not in normalized_parameters:
        normalized_parameters["required"] = []
    return normalized_parameters


def build_tool_schema(
    name: str,
    description: str,
    parameters: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Build a stable LiteLLM-compatible function tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _normalize_parameters(parameters),
        },
    }


def register_ai_tool(
    *,
    name: str,
    description: str,
    parameters: Optional[dict[str, Any]],
    func: Callable[..., Any],
) -> Callable[..., Any]:
    """Register (or replace) a tool and keep tool schemas deduplicated by name."""
    AVAILABLE_TOOLS[name] = func
    _TOOL_SCHEMAS_BY_NAME[name] = build_tool_schema(name, description, parameters)

    # Keep legacy TOOL_SCHEMAS list in sync for callers that read it directly.
    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.extend(_TOOL_SCHEMAS_BY_NAME.values())
    return func


def ai_tool(name: str, description: str, parameters: dict[str, Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as an AI tool and store its schema for model tool-calling."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return register_ai_tool(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
        )

    return decorator
