"""Tool layer: uniform ``Tool`` interface + registry. Agents only see ``ToolRegistry``."""

from .base import Tool, ToolError, ToolMeta, ToolResult
from .registry import ToolRegistry

# Canonical tool names used by agents (decouples agents from implementation classes).
CLIENT_DATA = "load_client_data"
CRM = "crm_lookup"
METRICS = "portfolio_metrics"
ANOMALY = "anomaly_detection"
MEMORY = "memory"
NARRATIVE = "narrative"

__all__ = [
    "ANOMALY",
    "CLIENT_DATA",
    "CRM",
    "MEMORY",
    "METRICS",
    "NARRATIVE",
    "Tool",
    "ToolError",
    "ToolMeta",
    "ToolRegistry",
    "ToolResult",
]
