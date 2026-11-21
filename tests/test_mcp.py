import unittest
from fury.mcp import McpToolRegistry, AutonomyTier

class TestMcpRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = McpToolRegistry()
        self.registry.register_tool(
            name="check_disk",
            description="Check disk usage on drive",
            input_schema={
                "type": "object",
                "properties": {"drive": {"type": "string"}},
                "required": ["drive"]
            },
            handler=lambda drive: {"drive": drive, "used_percent": 88.5, "healthy": False},
            autonomy_tier=AutonomyTier.TIER_1_READ_ONLY,
            rate_limit=5
        )

    def test_list_tools(self):
        tools = self.registry.list_tools()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "check_disk")
        self.assertEqual(tools[0]["autonomyTier"], "tier_1_read_only")

    def test_call_tool_valid(self):
        res = self.registry.call_tool("check_disk", {"drive": "F:"})
        self.assertEqual(res["tool"], "check_disk")
        self.assertEqual(res["result"]["used_percent"], 88.5)
        self.assertFalse(res["result"]["healthy"])

    def test_call_tool_schema_validation(self):
        with self.assertRaises(ValueError):
            self.registry.call_tool("check_disk", {})

        with self.assertRaises(TypeError):
            self.registry.call_tool("check_disk", {"drive": 123})

    def test_rate_limiting(self):
        for _ in range(5):
            self.registry.call_tool("check_disk", {"drive": "F:"})
        with self.assertRaises(RuntimeError):
            self.registry.call_tool("check_disk", {"drive": "F:"})

if __name__ == "__main__":
    unittest.main()
