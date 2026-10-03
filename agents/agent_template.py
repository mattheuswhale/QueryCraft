"""Copy this file to create a new QueryCraft MCP server."""

import json
import os

from mcp.server.fastmcp import FastMCP

EXAMPLE_SETTING = os.getenv("EXAMPLE_SETTING", "default_value")

mcp = FastMCP(
    "my_agent",
    instructions="Describe what this agent does and when the assistant should use it.",
)


@mcp.tool()
def example_tool(input_text: str, optional_flag: bool = False) -> str:
    """Process a short text value."""
    if not isinstance(input_text, str) or not input_text.strip():
        return json.dumps({"error": "input_text must be a non-empty string"})
    input_text = input_text.strip()[:500]
    return json.dumps({
        "result": f"Processed: {input_text!r}, flag={optional_flag}",
        "setting": EXAMPLE_SETTING,
    }, ensure_ascii=False)


if __name__ == "__main__":
    import logging
    import sys

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    mcp.run(transport="stdio")
