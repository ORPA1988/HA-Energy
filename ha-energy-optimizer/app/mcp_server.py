"""
MCP (Model Context Protocol) Server for HA Energy Optimizer.

Provides tools for:
- Live configuration editing on Home Assistant
- Log file reading and searching
- Real-time state monitoring
- Schedule and optimization inspection

Can be used as a standalone MCP server for Claude Code, Cursor, or other MCP clients.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP Protocol Implementation (stdio-based JSON-RPC)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "get_state",
        "description": "Get current energy system state (PV, battery, grid, EV, prices)",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_schedule",
        "description": "Get the current 24h LP optimization schedule",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_plan",
        "description": "Get the 48h genetic algorithm plan",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_prices",
        "description": "Get current 48h electricity prices (all sources)",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_config",
        "description": "Get current configuration of the energy optimizer",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "update_config",
        "description": "Update configuration values. Accepts partial updates as key-value pairs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "updates": {
                    "type": "object",
                    "description": "Key-value pairs to update in the configuration",
                },
            },
            "required": ["updates"],
        },
    },
    {
        "name": "validate_config",
        "description": "Validate current configuration and return errors/warnings",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_logs",
        "description": "Read application log output. Returns recent log lines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of recent lines to return (default: 100)",
                    "default": 100,
                },
                "filter": {
                    "type": "string",
                    "description": "Filter logs by keyword (e.g. 'ERROR', 'optimization', 'EV')",
                },
                "level": {
                    "type": "string",
                    "description": "Filter by log level: DEBUG, INFO, WARNING, ERROR",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR"],
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_ha_logs",
        "description": "Read Home Assistant system logs via Supervisor API",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of recent lines (default: 50)",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_history",
        "description": "Get historical energy data (30s snapshots)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Hours of history to return (default: 24, max: 24)",
                    "default": 24,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_ev_strategy",
        "description": "Get current EV charging strategy evaluation",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "trigger_optimization",
        "description": "Trigger immediate re-optimization (LP + genetic)",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_ev_mode",
        "description": "Set EV charging mode",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Charging mode",
                    "enum": ["solar", "min_solar", "fast", "smart", "off"],
                },
            },
            "required": ["mode"],
        },
    },
    {
        "name": "set_read_only",
        "description": "Toggle read-only mode (no active control, monitoring only)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "True = read-only (safe for testing), False = active control",
                },
            },
            "required": ["enabled"],
        },
    },
    {
        "name": "get_ha_entity",
        "description": "Read a specific Home Assistant entity state",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "HA entity ID (e.g. sensor.solar_power)",
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_ha_entities",
        "description": "List Home Assistant entities by domain",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Entity domain (sensor, switch, number, etc.)",
                    "default": "sensor",
                },
                "search": {
                    "type": "string",
                    "description": "Filter entities by keyword in ID or friendly_name",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_load_decomposition",
        "description": "Get current load decomposition (base load vs controllable loads)",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


class MCPServer:
    """MCP server that communicates via stdio JSON-RPC."""

    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url
        self._log_buffer: list[str] = []
        self._max_log_lines = 10000

    async def _api_call(self, method: str, path: str, body: Any = None) -> dict:
        """Make HTTP call to the energy optimizer API."""
        import httpx
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "GET":
                r = await client.get(url)
            elif method == "POST":
                r = await client.post(url, json=body or {})
            else:
                return {"error": f"Unsupported method: {method}"}
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}: {r.text[:500]}"}
            return r.json()

    async def _get_ha_logs(self, lines: int = 50) -> str:
        """Read HA logs via Supervisor API."""
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            return "No SUPERVISOR_TOKEN available (not running as HA add-on)"
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "http://supervisor/core/api/error_log",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                log_lines = r.text.strip().split("\n")
                return "\n".join(log_lines[-lines:])
            return f"Failed to read HA logs: HTTP {r.status_code}"

    async def _read_app_logs(self, lines: int = 100, filter_str: str = "",
                              level: str = "") -> str:
        """Read application logs from container stdout or log file."""
        log_sources = [
            "/data/energy_optimizer.log",
            "/proc/1/fd/1",  # Container stdout
        ]

        log_lines = []
        for src in log_sources:
            try:
                p = Path(src)
                if p.exists():
                    content = p.read_text(errors="replace")
                    log_lines = content.strip().split("\n")
                    break
            except (PermissionError, OSError):
                continue

        # Also check in-memory buffer
        if not log_lines and self._log_buffer:
            log_lines = list(self._log_buffer)

        if not log_lines:
            # Try journalctl as fallback
            try:
                result = subprocess.run(
                    ["journalctl", "-u", "energy-optimizer", "-n", str(lines), "--no-pager"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    log_lines = result.stdout.strip().split("\n")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        if not log_lines:
            return "No log output found. Logs are typically available via 'docker logs' or HA Supervisor."

        # Apply filters
        if level:
            log_lines = [l for l in log_lines if f"[{level}]" in l]
        if filter_str:
            log_lines = [l for l in log_lines if filter_str.lower() in l.lower()]

        return "\n".join(log_lines[-lines:])

    async def handle_tool(self, name: str, arguments: dict) -> str:
        """Execute a tool and return the result as a string."""
        try:
            if name == "get_state":
                data = await self._api_call("GET", "/api/state")
                return json.dumps(data, indent=2, default=str)

            elif name == "get_schedule":
                data = await self._api_call("GET", "/api/schedule")
                return json.dumps(data, indent=2, default=str)

            elif name == "get_plan":
                data = await self._api_call("GET", "/api/plan")
                return json.dumps(data, indent=2, default=str)

            elif name == "get_prices":
                data = await self._api_call("GET", "/api/prices")
                return json.dumps(data, indent=2, default=str)

            elif name == "get_config":
                data = await self._api_call("GET", "/api/config")
                return json.dumps(data, indent=2, default=str)

            elif name == "update_config":
                updates = arguments.get("updates", {})
                data = await self._api_call("POST", "/api/config", updates)
                return json.dumps(data, indent=2, default=str)

            elif name == "validate_config":
                data = await self._api_call("GET", "/api/config/validate")
                return json.dumps(data, indent=2, default=str)

            elif name == "get_logs":
                lines = arguments.get("lines", 100)
                filter_str = arguments.get("filter", "")
                level = arguments.get("level", "")
                return await self._read_app_logs(lines, filter_str, level)

            elif name == "get_ha_logs":
                lines = arguments.get("lines", 50)
                return await self._get_ha_logs(lines)

            elif name == "get_history":
                hours = max(1, min(arguments.get("hours", 24), 24))
                data = await self._api_call("GET", f"/api/history?hours={hours}")
                return json.dumps(data, indent=2, default=str)

            elif name == "get_ev_strategy":
                data = await self._api_call("GET", "/api/ev/strategy")
                return json.dumps(data, indent=2, default=str)

            elif name == "trigger_optimization":
                data = await self._api_call("POST", "/api/optimize")
                return json.dumps(data, indent=2, default=str)

            elif name == "set_ev_mode":
                mode = arguments.get("mode", "smart")
                data = await self._api_call("POST", "/api/ev/mode", {"mode": mode})
                return json.dumps(data, indent=2, default=str)

            elif name == "set_read_only":
                enabled = arguments.get("enabled", True)
                data = await self._api_call("POST", "/api/mode", {"read_only": enabled})
                return json.dumps(data, indent=2, default=str)

            elif name == "get_ha_entity":
                eid = arguments.get("entity_id", "")
                data = await self._api_call("GET", f"/api/ha/entity/{eid}")
                return json.dumps(data, indent=2, default=str)

            elif name == "list_ha_entities":
                domain = arguments.get("domain", "sensor")
                search = arguments.get("search", "")
                url = f"/api/ha/entities?domain={domain}"
                if search:
                    url += f"&search={search}"
                data = await self._api_call("GET", url)
                return json.dumps(data, indent=2, default=str)

            elif name == "get_load_decomposition":
                data = await self._api_call("GET", "/api/load-decomposition")
                return json.dumps(data, indent=2, default=str)

            else:
                return json.dumps({"error": f"Unknown tool: {name}"})

        except Exception as e:
            return json.dumps({"error": str(e)})

    async def run_stdio(self):
        """Run the MCP server on stdin/stdout (JSON-RPC over stdio)."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_event_loop())

        async def send_response(response: dict):
            msg = json.dumps(response) + "\n"
            writer.write(msg.encode())
            await writer.drain()

        while True:
            try:
                line = await reader.readline()
                if not line:
                    break
                request = json.loads(line.decode().strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            method = request.get("method", "")
            req_id = request.get("id")
            params = request.get("params", {})

            if method == "initialize":
                await send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "ha-energy-optimizer",
                            "version": "0.2.2",
                        },
                    },
                })

            elif method == "notifications/initialized":
                pass  # No response needed

            elif method == "tools/list":
                await send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": TOOL_DEFINITIONS},
                })

            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {})
                result_text = await self.handle_tool(tool_name, tool_args)
                await send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": result_text}],
                    },
                })

            elif req_id is not None:
                await send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })


def main():
    """Entry point for the MCP server."""
    import argparse
    parser = argparse.ArgumentParser(description="HA Energy Optimizer MCP Server")
    parser.add_argument("--url", default=os.environ.get("ENERGY_OPTIMIZER_URL", "http://localhost:8080"),
                        help="Base URL of the energy optimizer API")
    args = parser.parse_args()

    server = MCPServer(base_url=args.url)
    asyncio.run(server.run_stdio())


if __name__ == "__main__":
    main()
