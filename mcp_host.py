"""
mcp_host.py — MCP client/tool orchestrator for QueryCraft.

Uses stdio MCP subprocesses. Tool schemas are exposed in OpenAI-compatible
function-call format for llama.cpp's /v1/chat/completions endpoint.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("mcp_host")
PROJECT_ROOT = Path(__file__).resolve().parent
TOOL_OUTPUT_MAX_CHARS = int(os.getenv("TOOL_OUTPUT_MAX_CHARS", "8000"))


def load_server_registry(config_path: str = "mcp_config.json") -> list[dict]:
    path = Path(config_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.exists():
        try:
            with path.open(encoding="utf-8") as handle:
                config = json.load(handle)
            servers = config.get("servers", [])
            log.info("[mcp_host] Loaded %d server(s) from %s", len(servers), config_path)
            return servers
        except Exception as exc:
            log.error("[mcp_host] Failed to load %s: %s", config_path, exc)

    return [
        {
            "name": "documents",
            "command": "python",
            "args": ["agents/documents_agent.py"],
            "env": {},
            "enabled": True,
        },
        {
            "name": "rss",
            "command": "python",
            "args": ["agents/rss_agent.py"],
            "env": {},
            "enabled": True,
        },
    ]


class MCPHost:
    def __init__(self):
        self._sessions: dict[str, ClientSession] = {}
        # tool name -> (server name, input schema, description)
        self._tool_index: dict[str, tuple[str, dict, str]] = {}
        self._registry = load_server_registry()
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._tg: asyncio.TaskGroup | None = None
        self._pending_servers = 0

    async def start(self) -> None:
        self._stop_event.clear()
        self._ready_event.clear()
        enabled = [server for server in self._registry if server.get("enabled", True)]
        self._pending_servers = len(enabled)

        self._tg = asyncio.TaskGroup()
        await self._tg.__aenter__()

        if not enabled:
            self._ready_event.set()
            return

        for server_def in enabled:
            self._tg.create_task(
                self._connect_server(server_def["name"], server_def)
            )

        await self._ready_event.wait()
        log.info(
            "[mcp_host] Ready — %d server(s), %d tool(s)",
            len(self._sessions),
            len(self._tool_index),
        )

    def _mark_server_ready(self) -> None:
        if self._pending_servers <= 0:
            return
        self._pending_servers -= 1
        if self._pending_servers == 0:
            self._ready_event.set()

    async def _connect_server(self, name: str, server_def: dict) -> None:
        configured_env = {
            key: os.path.expandvars(str(value))
            for key, value in server_def.get("env", {}).items()
        }
        # Environment loaded by assistant.py wins over mcp_config.json. This
        # keeps .env authoritative for secrets and machine-specific settings.
        env = {**configured_env, **os.environ}

        command = server_def["command"]
        if command.lower() in {"python", "python3", "py"}:
            command = sys.executable

        args = list(server_def.get("args", []))
        if args and args[0].endswith(".py"):
            script_path = Path(args[0])
            if not script_path.is_absolute():
                args[0] = str(PROJECT_ROOT / script_path)

        params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )
        ready_marked = False

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._sessions[name] = session

                    tools_response = await session.list_tools()
                    for tool in tools_response.tools:
                        if tool.name in self._tool_index:
                            previous = self._tool_index[tool.name][0]
                            log.warning(
                                "[mcp_host] Tool collision: %s from %s replaces %s",
                                tool.name,
                                name,
                                previous,
                            )
                        self._tool_index[tool.name] = (
                            name,
                            tool.inputSchema or {},
                            tool.description or f"Tool '{tool.name}' from {name}",
                        )

                    self._mark_server_ready()
                    ready_marked = True
                    await self._stop_event.wait()
        except Exception as exc:
            log.error("[mcp_host] Server '%s' failed: %s", name, exc)
            if not ready_marked:
                self._mark_server_ready()
        finally:
            self._sessions.pop(name, None)
            for tool_name, entry in list(self._tool_index.items()):
                if entry[0] == name:
                    self._tool_index.pop(tool_name, None)

    async def _stop_all(self) -> None:
        self._stop_event.set()
        if self._tg is not None:
            await self._tg.__aexit__(None, None, None)
            self._tg = None
        self._sessions.clear()
        self._tool_index.clear()

    async def stop(self) -> None:
        await self._stop_all()

    def get_tools(self) -> list[dict]:
        specs = []
        for tool_name, (server_name, schema, description) in self._tool_index.items():
            specs.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": description or f"Tool '{tool_name}' from {server_name}",
                    "parameters": {
                        "type": "object",
                        "properties": schema.get("properties", {}),
                        "required": schema.get("required", []),
                    },
                },
            })
        return specs

    # Backward-compatible name for older callers.
    def get_ollama_tools(self) -> list[dict]:
        return self.get_tools()

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        entry = self._tool_index.get(tool_name)
        if entry is None:
            return json.dumps({"error": f"Unknown tool: {tool_name!r}"})

        server_name, schema, _description = entry
        session = self._sessions.get(server_name)
        if session is None:
            return json.dumps({"error": f"Server '{server_name}' is not running"})

        validation_error = _validate_arguments(arguments, schema)
        if validation_error:
            return json.dumps({
                "error": f"Argument validation failed: {validation_error}"
            })

        try:
            result = await session.call_tool(tool_name, arguments)
            text_parts = [
                block.text
                for block in result.content
                if hasattr(block, "text")
            ]
            output = "\n".join(text_parts)
            if len(output) > TOOL_OUTPUT_MAX_CHARS:
                output = (
                    output[:TOOL_OUTPUT_MAX_CHARS]
                    + f"\n[output truncated at {TOOL_OUTPUT_MAX_CHARS} chars]"
                )
            return json.dumps({"result": output}, ensure_ascii=False)
        except Exception as exc:
            log.error("[mcp_host] Tool '%s' raised: %s", tool_name, exc)
            return json.dumps({"error": f"Tool execution error: {type(exc).__name__}"})

    def list_tools(self) -> list[str]:
        return list(self._tool_index.keys())


_TYPE_MAP = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _validate_arguments(args: dict, schema: dict) -> str | None:
    if not isinstance(args, dict):
        return "Arguments must be an object"
    if not schema:
        return None

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for field in required:
        if field not in args:
            return f"Missing required field: '{field}'"

    for field, value in args.items():
        if field not in properties:
            continue
        expected_type = properties[field].get("type")
        if expected_type in _TYPE_MAP and not isinstance(value, _TYPE_MAP[expected_type]):
            return (
                f"Field '{field}': expected {expected_type}, "
                f"got {type(value).__name__}"
            )
        if isinstance(value, str) and len(value) > 4000:
            return f"Field '{field}': string too long (max 4000 chars)"

    return None


class MCPHostSync:
    """Synchronous wrapper around MCPHost with one background event loop."""

    def __init__(self):
        import threading

        self._loop = asyncio.new_event_loop()
        self._host = MCPHost()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="mcp-event-loop",
        )
        self._thread.start()
        self._closed = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self._host.start(), self._loop)
        future.result(timeout=15)

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True

        try:
            future = asyncio.run_coroutine_threadsafe(self._host._stop_all(), self._loop)
            future.result(timeout=15)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                log.warning("[mcp_host] Event loop thread did not stop within 5 seconds")
            else:
                self._loop.close()

    def get_tools(self) -> list[dict]:
        return self._host.get_tools()

    def get_ollama_tools(self) -> list[dict]:
        return self.get_tools()

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        future = asyncio.run_coroutine_threadsafe(
            self._host.call_tool(tool_name, arguments),
            self._loop,
        )
        return future.result(timeout=30)

    def list_tools(self) -> list[str]:
        return self._host.list_tools()
