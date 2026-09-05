"""Vercel ASGI entrypoint for MCP plus OAuth discovery and callbacks."""
from bridge.mcp_server import production_app

app = production_app()
