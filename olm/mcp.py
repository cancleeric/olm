"""MCP (Model Context Protocol) stdio client — stdlib only."""
import json
import subprocess
import threading
import time
from typing import Optional


class MCPClient:
    """JSON-RPC 2.0 over stdio transport."""

    def __init__(self, cmd: list[str]):
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._id = 0
        self._lock = threading.Lock()
        self._responses: dict = {}
        self._cond = threading.Condition(self._lock)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *_):
        self.close()

    def _read_loop(self):
        for raw in self._proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            req_id = msg.get("id")
            if req_id is not None:
                with self._cond:
                    self._responses[req_id] = msg
                    self._cond.notify_all()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _send(self, msg: dict):
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    def _rpc(self, method: str, params: dict | None = None, timeout: int = 30) -> dict:
        req_id = self._next_id()
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        with self._cond:
            while req_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"MCP {method} timed out ({timeout}s)")
                self._cond.wait(timeout=min(remaining, 1.0))
            return self._responses.pop(req_id)

    def _notify(self, method: str, params: dict | None = None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> dict:
        resp = self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "olm", "version": "0.1"},
        })
        self._notify("notifications/initialized")
        return resp

    def list_tools(self) -> list[dict]:
        """Return tools in Ollama /api/chat format."""
        resp = self._rpc("tools/list")
        raw = resp.get("result", {}).get("tools", [])
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            for t in raw
        ]

    def call_tool(self, name: str, arguments: dict) -> str:
        """Call tool, return result as string."""
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout=60)
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
        result = resp.get("result", {})
        content = result.get("content", [])
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    parts.append(c.get("text") or str(c.get("resource", c)))
                else:
                    parts.append(str(c))
            return "\n".join(parts)
        return str(content)

    def close(self):
        try:
            self._proc.terminate()
        except Exception:
            pass


def parse_mcp_spec(spec: str) -> list[str]:
    """Parse MCP server spec to subprocess args.

    Formats:
        npx:pkg arg1 arg2         -> npx -y pkg arg1 arg2
        python:module             -> python3 -m module
        uvx:pkg                   -> uvx pkg
        cmd:exe arg1 arg2         -> exe arg1 arg2
        bare string               -> split by whitespace
    """
    if ":" in spec:
        prefix, rest = spec.split(":", 1)
        parts = rest.split()
        if prefix == "npx":
            return ["npx", "-y"] + parts
        if prefix in ("python", "python3"):
            return ["python3", "-m"] + parts
        if prefix == "uvx":
            return ["uvx"] + parts
        if prefix == "cmd":
            return parts
    return spec.split()
