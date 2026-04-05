"""Unit tests for deduplicated tool registry behavior."""

import unittest

from tools import registry


class ToolRegistryTests(unittest.TestCase):
    """Ensure duplicate registrations do not duplicate schemas."""

    def setUp(self) -> None:
        self._original_tools = dict(registry.AVAILABLE_TOOLS)
        self._original_schemas = list(registry.TOOL_SCHEMAS)
        self._original_schema_map = dict(registry._TOOL_SCHEMAS_BY_NAME)

    def tearDown(self) -> None:
        registry.AVAILABLE_TOOLS.clear()
        registry.AVAILABLE_TOOLS.update(self._original_tools)

        registry._TOOL_SCHEMAS_BY_NAME.clear()
        registry._TOOL_SCHEMAS_BY_NAME.update(self._original_schema_map)

        registry.TOOL_SCHEMAS.clear()
        registry.TOOL_SCHEMAS.extend(self._original_schemas)

    def test_register_ai_tool_replaces_schema_for_same_name(self) -> None:
        def func_one() -> str:
            return "one"

        def func_two() -> str:
            return "two"

        registry.register_ai_tool(
            name="dup_tool",
            description="first",
            parameters={"type": "object", "properties": {}, "required": []},
            func=func_one,
        )
        registry.register_ai_tool(
            name="dup_tool",
            description="second",
            parameters={"type": "object", "properties": {}, "required": []},
            func=func_two,
        )

        matching_schemas = [
            schema
            for schema in registry.TOOL_SCHEMAS
            if schema.get("function", {}).get("name") == "dup_tool"
        ]

        self.assertEqual(len(matching_schemas), 1)
        self.assertIs(registry.AVAILABLE_TOOLS["dup_tool"], func_two)
        self.assertEqual(matching_schemas[0]["function"]["description"], "second")


if __name__ == "__main__":
    unittest.main()
