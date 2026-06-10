"""
Atlassian Rovo MCP client utilities.

Shared by both:
  - agents/confluence_agent.py  (LLM-driven subagent — COMPLEX path)
  - utils/confluence_rag.py     (direct REST search   — SIMPLE path)

Auth split:
  CONFLUENCE_MCP_TOKEN   → Bearer token for the MCP server connection
  CONFLUENCE_GRAPH_TOKEN → Bearer token for Teamwork Graph tool calls
                           (falls back to MCP_TOKEN if empty)

⚠ Teamwork Graph tools require org-admin access:
  admin.atlassian.com → Security → API token policies → Teamwork Graph
"""

from __future__ import annotations

import logging
from typing import Any

import config

logger = logging.getLogger(__name__)

_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"

# ─── lazy imports ─────────────────────────────────────────────────────────────

_ready: bool | None = None
_ClientSession = None
_streamablehttp_client_fn = None

_GRAPH_TOOLS = {"getTeamworkGraphContext", "getTeamworkGraphObject", "addTeamworkGraphContext"}

# Warn once per process about the org-admin gate
_permission_warned = False


def _load() -> bool:
    global _ready, _ClientSession, _streamablehttp_client_fn
    if _ready is not None:
        return _ready
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        _ClientSession = ClientSession
        _streamablehttp_client_fn = streamablehttp_client
        _ready = True
    except ImportError:
        logger.info("[MCP] mcp package not installed — MCP path disabled. Run: pip install mcp>=1.0.0")
        _ready = False
    return _ready


def is_mcp_ready() -> bool:
    """Return True if the mcp package is installed AND MCP token is configured."""
    return _load() and bool(getattr(config, "CONFLUENCE_MCP_TOKEN", ""))


def _bearer(tool_name: str) -> str:
    """Select the correct bearer token for a tool call."""
    if tool_name in _GRAPH_TOOLS:
        return (
            getattr(config, "CONFLUENCE_GRAPH_TOKEN", "")
            or getattr(config, "CONFLUENCE_MCP_TOKEN", "")
        )
    return getattr(config, "CONFLUENCE_MCP_TOKEN", "")


async def mcp_call(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """
    Call one Rovo MCP tool.  Returns joined text content, or None on error.

    Handles the org-admin permission gate gracefully:
    - Logs a one-time WARNING with actionable fix instructions.
    - Returns None so callers fall back silently.
    """
    global _permission_warned
    if not is_mcp_ready():
        return None

    token = _bearer(tool_name)
    if not token:
        return None

    try:
        async with _streamablehttp_client_fn(
            url=_MCP_URL,
            headers={"Authorization": f"Bearer {token}"},
        ) as (read_stream, write_stream, _):
            async with _ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)

                if result and hasattr(result, "content"):
                    texts = [
                        item.text
                        for item in result.content
                        if hasattr(item, "text") and item.text
                    ]
                    raw = "\n".join(texts)

                    # Org-admin permission gate — warn once, then fall back silently
                    if '"error":true' in raw or "don't have permission" in raw.lower():
                        if not _permission_warned:
                            _permission_warned = True
                            which = "GRAPH" if (tool_name in _GRAPH_TOOLS and getattr(config, "CONFLUENCE_GRAPH_TOKEN", "")) else "MCP"
                            logger.warning(
                                "[MCP] Teamwork Graph permission denied (CONFLUENCE_%s_TOKEN).\n"
                                "  To unlock: admin.atlassian.com → Security → API token policies"
                                " → enable Teamwork Graph API access.\n"
                                "  Falling back to REST-only mode for all subsequent requests.",
                                which,
                            )
                        return None

                    return raw or None

    except Exception as exc:
        logger.warning("[MCP] Tool call %r failed: %s", tool_name, exc)

    return None
