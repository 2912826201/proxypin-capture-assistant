#!/usr/bin/env python3
"""Python 3.10+ standard-library client for ProxyPin's built-in MCP server.

Use the actual endpoint displayed by the running ProxyPin instance.
LAN Bearer credentials come from PROXYPIN_MCP_TOKEN or a hidden terminal prompt.
Loopback desktop connections can operate without a token.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import sys
import warnings
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DEFAULT_ENDPOINT = os.environ.get("PROXYPIN_MCP_ENDPOINT", "http://127.0.0.1:9127/mcp")


class MCPError(RuntimeError):
    """An explicit protocol, capture, or precondition failure."""


class MCPTransportError(MCPError):
    """The MCP HTTP transport failed."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the MCP Bearer token to a redirect target.


def is_loopback(endpoint: str) -> bool:
    host = urlsplit(endpoint).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def read_token(no_token: bool = False, endpoint: str = DEFAULT_ENDPOINT, require_token: bool = False) -> str | None:
    if no_token and require_token:
        raise MCPError("--no-token and --require-token cannot be combined.")
    if no_token and not is_loopback(endpoint):
        raise MCPError("--no-token is only available for a loopback desktop endpoint.")
    token = os.environ.get("PROXYPIN_MCP_TOKEN", "").strip()
    if no_token or (not token and is_loopback(endpoint) and not require_token):
        return None
    if not token:
        if not sys.stdin.isatty():
            raise MCPError("Set PROXYPIN_MCP_TOKEN, or run in a terminal for a hidden token prompt.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                token = getpass.getpass("ProxyPin MCP Bearer token: ").strip()
            except (getpass.GetPassWarning, EOFError) as exc:
                raise MCPError("A hidden token prompt is unavailable; use PROXYPIN_MCP_TOKEN.") from exc
    if not token or "\r" in token or "\n" in token:
        raise MCPError("The MCP token is missing or invalid.")
    return token


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MCPError(f"Invalid {name}: expected an integer.")
    return value


def _flow_time_ms(value: Any) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return int(parsed.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError) as exc:
        raise MCPError("Captured flow has an invalid timestamp.") from exc


class ProxyPinMCP:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, token: str | None = None,
                 timeout: float = 45, max_body_bytes: int = 32 * 1024 * 1024):
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise MCPError("MCP endpoint must be an HTTP(S) URL without credentials or a fragment.")
        self.endpoint, self._token, self.timeout = endpoint, token, timeout
        self.max_body_bytes = max_body_bytes
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._request_id = 0
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._tools: dict[str, dict[str, Any]] | None = None
        self.rpc_calls = 0

    def safe_text(self, value: Any) -> str:
        text = str(value)
        return text.replace(self._token, "<redacted>") if self._token else text

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._request_id += 1
        rpc_id = self._request_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        request = Request(self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        self.rpc_calls += 1
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                self._session_id = response.headers.get("Mcp-Session-Id") or self._session_id
                if "text/event-stream" in response.headers.get("Content-Type", "").lower():
                    result = self._read_sse(response, rpc_id)
                else:
                    result = json.load(response)
        except HTTPError as exc:
            raise MCPTransportError(f"MCP HTTP {exc.code}; check endpoint and Bearer credential.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MCPTransportError("MCP endpoint is unreachable or timed out.") from exc
        except (ValueError, UnicodeError) as exc:
            raise MCPError("MCP returned invalid JSON or SSE.") from exc
        if not isinstance(result, dict) or result.get("id") != rpc_id:
            raise MCPError("MCP returned an unexpected JSON-RPC response id.")
        if "error" in result:
            error = result["error"]
            if not isinstance(error, dict):
                raise MCPError("Invalid JSON-RPC error response.")
            raise MCPError(self.safe_text(f"JSON-RPC error {error.get('code')}: {error.get('message', 'request failed')}"))
        if "result" not in result:
            raise MCPError("JSON-RPC response has no result.")
        return result["result"]

    @staticmethod
    def _read_sse(response, rpc_id: int) -> dict[str, Any]:
        data: list[str] = []
        size = 0
        for raw_line in response:
            size += len(raw_line)
            if size > 64 * 1024 * 1024:
                raise MCPError("MCP SSE response exceeded its transport limit.")
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                event = json.loads("\n".join(data))
                data.clear()
                if isinstance(event, dict) and event.get("id") == rpc_id:
                    return event
        if data:
            event = json.loads("\n".join(data))
            if isinstance(event, dict) and event.get("id") == rpc_id:
                return event
        raise MCPError("SSE ended without the requested JSON-RPC response.")

    def discover(self) -> dict[str, dict[str, Any]]:
        initialized = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "portable-proxypin", "version": "1.0"}})
        if not isinstance(initialized, dict) or initialized.get("protocolVersion") not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
            raise MCPError("MCP server selected an unsupported protocol version; use its native connector or update the client.")
        self._protocol_version = initialized["protocolVersion"]
        self.notify_initialized()
        tools: dict[str, dict[str, Any]] = {}
        cursor = None
        for _ in range(100):
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise MCPError("MCP tools/list has an invalid schema.")
            for tool in result["tools"]:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise MCPError("MCP tool metadata is invalid.")
                tools[tool["name"]] = tool
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                self._tools = tools
                return tools
            if next_cursor == cursor:
                raise MCPError("MCP tools/list pagination made no progress.")
            cursor = next_cursor
        raise MCPError("MCP tools/list exceeded the page limit.")

    def notify_initialized(self) -> None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self._protocol_version or "2025-06-18"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        request = Request(self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        self.rpc_calls += 1
        try:
            with self._opener.open(request, timeout=self.timeout):
                pass
        except HTTPError as exc:
            raise MCPTransportError(f"MCP initialization notification failed with HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MCPTransportError("MCP initialization notification failed; check the current endpoint.") from exc

    def require_tools(self, names: list[str]) -> None:
        if self._tools is None:
            self.discover()
        missing = sorted(set(names) - set(self._tools or {}))
        if missing:
            raise MCPError("Missing MCP tools: " + ", ".join(missing))

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.require_tools([name])
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise MCPError("MCP tools/call result is invalid.")
        blocks = result.get("content")
        if blocks is not None and not isinstance(blocks, list):
            raise MCPError("MCP content blocks are invalid.")
        texts = [block["text"] for block in blocks or []
                 if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
        if result.get("isError"):
            raise MCPError(self.safe_text(f"MCP {name} failed: " + "\n".join(texts)))
        if "structuredContent" in result:
            return result["structuredContent"]
        if len(texts) != 1:
            raise MCPError("Expected one JSON text content block from ProxyPin.")
        try:
            return json.loads(texts[0])
        except ValueError as exc:
            raise MCPError("ProxyPin tool returned invalid JSON content.") from exc

    def read_flow_text(self, flow_id: str, history_id: str | None = None) -> str:
        chunks: list[str] = []
        offset, expected_total = 0, None
        while True:
            arguments = {"id": flow_id, "side": "response", "offset": offset, "limit": 65536}
            if history_id:
                arguments["history_id"] = history_id
            part = self.call("get_flow_body", arguments)
            if not isinstance(part, dict):
                raise MCPError("get_flow_body returned an invalid schema.")
            if not part.get("available") or part.get("binary"):
                raise MCPError("Captured response is unavailable or binary; MCP does not return image bytes.")
            returned = _integer(part.get("returned"), "body returned")
            total = _integer(part.get("total"), "body total")
            actual_offset = _integer(part.get("offset"), "body offset")
            text = part.get("text")
            if total > self.max_body_bytes or total < 0:
                raise MCPError("Captured text response exceeded its configured size limit.")
            if expected_total is None:
                expected_total = total
            if total != expected_total or actual_offset != offset or returned < 0 or returned > 65536 or offset + returned > total:
                raise MCPError("Captured body pagination is inconsistent.")
            if not isinstance(text, str) or len(text.encode("utf-8")) != returned:
                raise MCPError("Captured UTF-8 body slice is incomplete or splits a multibyte character; refusing corrupted text.")
            chunks.append(text)
            offset += returned
            if part.get("truncated") is not (offset < total):
                raise MCPError("Captured body truncation metadata is inconsistent.")
            if offset == total:
                return "".join(chunks)
            if returned == 0:
                raise MCPError("Captured body pagination made no progress.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--no-token", action="store_true", help="Only for a trusted desktop server without authentication")
    parser.add_argument("--require-token", action="store_true", help="Require mobile Bearer authentication even through loopback USB forwarding")
    parser.add_argument("--tool", help="Call a discovered MCP tool; omit to list tool metadata")
    parser.add_argument("--arguments", default="{}", help="JSON tool arguments, never the MCP Bearer token")
    args = parser.parse_args(argv)
    client = None
    try:
        client = ProxyPinMCP(args.endpoint, read_token(args.no_token, args.endpoint, args.require_token), args.timeout)
        tools = client.discover()
        arguments = json.loads(args.arguments)
        if not isinstance(arguments, dict):
            raise MCPError("Tool arguments must be a JSON object.")
        result = client.call(args.tool, arguments) if args.tool else {"tools": list(tools.values())}
        print(client.safe_text(json.dumps(result, ensure_ascii=False, indent=2)))
        return 0
    except (MCPError, ValueError) as exc:
        print(client.safe_text(exc) if client else str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
