"""Memento MCP server package.

Exposes the Model Context Protocol interface for AI coding agents
to interact with Memento memory stores.
"""

from memento.mcp.server import create_mcp_server, run_stdio, try_open_graphiti, try_open_mem0

__all__ = ["create_mcp_server", "run_stdio", "try_open_graphiti", "try_open_mem0"]
