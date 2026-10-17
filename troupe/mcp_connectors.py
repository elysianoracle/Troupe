"""MCP connectors (Gmail, Calendar, GitHub, Notion, ... any MCP server over stdio).

Configure in <data_dir>/connectors.json, same shape as Claude Desktop's config:
  {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                             "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "vault:github_token"}}}}
"vault:NAME" values are filled from the encrypted vault, so tokens never appear in prompts.
Then grant an agent access in its permissions: {"connectors": {"github": "reads_auto"}}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from .guardrails import READ_PREFIXES
from .tools import Tool, ToolContext, ToolOutput

log = logging.getLogger("troupe.mcp")


class MCPServer:
    def __init__(self, name: str, cfg: dict, env: dict):
        self.name = name
        self.cfg = cfg
        self.env = env
        self.tools: list = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._main())
        await asyncio.wait_for(self._ready.wait(), timeout=90)
        if self._error:
            raise self._error

    async def _main(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=self.cfg["command"], args=self.cfg.get("args", []),
                                       env=self.env or None)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.tools = (await session.list_tools()).tools
                    self._ready.set()
                    while True:
                        tool, args, fut = await self._queue.get()
                        try:
                            fut.set_result(await session.call_tool(tool, args))
                        except Exception as e:
                            fut.set_exception(e)
        except Exception as e:
            self._error = e
            self._ready.set()
            log.error("MCP server %s stopped: %s", self.name, e)

    async def call(self, tool: str, args: dict, timeout: float = 120):
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put((tool, args, fut))
        return await asyncio.wait_for(fut, timeout)


class MCPManager:
    def __init__(self, data_dir: Path, vault):
        self.path = data_dir / "connectors.json"
        self.vault = vault
        self.servers: dict[str, MCPServer] = {}

    async def start_all(self) -> None:
        if not self.path.exists():
            return
        try:
            cfg = json.loads(self.path.read_text(encoding="utf-8")).get("mcpServers", {})
        except Exception as e:
            log.error("bad connectors.json: %s", e)
            return
        for name, server_cfg in cfg.items():
            import os
            env = {**os.environ, **self.vault.resolve_env(server_cfg.get("env", {}))}
            srv = MCPServer(re.sub(r"[^\w\-]", "_", name), server_cfg, env)
            try:
                await srv.start()
                self.servers[srv.name] = srv
                log.info("MCP connector %s: %d tools", srv.name, len(srv.tools))
            except Exception as e:
                log.error("could not start MCP connector %s: %s", name, e)

    def tools(self, allowed: dict) -> list[Tool]:
        out = []
        for sname, srv in self.servers.items():
            if sname not in allowed or allowed[sname] == "deny":
                continue
            for t in srv.tools:
                full = f"mcp__{sname}__{re.sub(r'[^\w\-]', '_', t.name)}"[:128]
                out.append(Tool(full, (t.description or t.name)[:1000],
                                t.inputSchema or {"type": "object", "properties": {}},
                                self._handler(srv, t.name), permission="connector",
                                external=not t.name.lower().startswith(READ_PREFIXES)))
        return out

    @staticmethod
    def _handler(srv: MCPServer, tool_name: str):
        async def run(ctx: ToolContext, args: dict) -> ToolOutput:
            res = await srv.call(tool_name, args)
            blocks = []
            for c in res.content or []:
                if getattr(c, "type", "") == "text":
                    blocks.append({"type": "text", "text": c.text[:40_000]})
                elif getattr(c, "type", "") == "image":
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": c.mimeType,
                                                               "data": c.data}})
            return ToolOutput(blocks or "(no output)", is_error=bool(getattr(res, "isError", False)), taint=True)
        return run
