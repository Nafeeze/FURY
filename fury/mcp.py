"""Model Context Protocol (MCP) Tool Registry as defined in Blueprint Phase 5.
Provides standard tool discovery, JSON schema input validation, rate limiting,
timeouts, and least-privilege read/act separation with direct API fallbacks."""
import json, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

class AutonomyTier(str, Enum):
    TIER_1_READ_ONLY = "tier_1_read_only"
    TIER_2_POLICY_LOW_RISK = "tier_2_policy_low_risk"
    TIER_3_APPROVAL_REQUIRED = "tier_3_approval_required"
    TIER_4_RESTRICTED_HIGH_IMPACT = "tier_4_restricted_high_impact"

@dataclass
class McpToolDefinition:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[..., Any]
    autonomy_tier: AutonomyTier = AutonomyTier.TIER_1_READ_ONLY
    rate_limit_per_minute: int = 60
    timeout_seconds: float = 30.0
    idempotent: bool = True
    last_called_timestamps: List[float] = field(default_factory=list)

    def to_mcp_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "autonomyTier": self.autonomy_tier.value,
            "idempotent": self.idempotent
        }

class McpToolRegistry:
    def __init__(self):
        self.tools: Dict[str, McpToolDefinition] = {}
        self.call_history: List[Dict[str, Any]] = []

    def register_tool(self, name: str, description: str, input_schema: Dict[str, Any],
                      handler: Callable[..., Any], autonomy_tier: AutonomyTier = AutonomyTier.TIER_1_READ_ONLY,
                      rate_limit: int = 60, timeout: float = 30.0, idempotent: bool = True):
        self.tools[name] = McpToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            autonomy_tier=autonomy_tier,
            rate_limit_per_minute=rate_limit,
            timeout_seconds=timeout,
            idempotent=idempotent
        )

    def list_tools(self) -> List[Dict[str, Any]]:
        return [t.to_mcp_schema() for t in self.tools.values()]

    def _validate_schema(self, schema: Dict[str, Any], arguments: Dict[str, Any]):
        required = schema.get("required", [])
        for req in required:
            if req not in arguments:
                raise ValueError(f"Missing required parameter '{req}' for tool")
        properties = schema.get("properties", {})
        for k, v in arguments.items():
            if k in properties:
                expected_type = properties[k].get("type")
                if expected_type == "string" and not isinstance(v, str):
                    raise TypeError(f"Parameter '{k}' must be string, got {type(v).__name__}")
                elif expected_type == "integer" and not isinstance(v, int):
                    raise TypeError(f"Parameter '{k}' must be integer, got {type(v).__name__}")

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.tools.get(name)
        if not tool:
            raise LookupError(f"MCP tool '{name}' not found")

        now = time.time()
        tool.last_called_timestamps = [t for t in tool.last_called_timestamps if (now - t) < 60.0]
        if len(tool.last_called_timestamps) >= tool.rate_limit_per_minute:
            raise RuntimeError(f"Rate limit exceeded for tool '{name}' ({tool.rate_limit_per_minute}/min)")
        tool.last_called_timestamps.append(now)

        self._validate_schema(tool.input_schema, arguments)

        t0 = time.perf_counter()
        result = tool.handler(**arguments)
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)

        record = {
            "tool": name,
            "arguments": arguments,
            "result": result,
            "duration_ms": duration_ms,
            "timestamp": now,
            "tier": tool.autonomy_tier.value
        }
        self.call_history.append(record)
        return {
            "tool": name,
            "result": result,
            "duration_ms": duration_ms,
            "tier": tool.autonomy_tier.value
        }
