#!/usr/bin/env python3
"""Inspect a local ADB UI XML dump or hand one selected token to an MCP child.

Keep the dump in a private work directory outside this Skill/repository. Inspection
only describes UI evidence; it does not verify VPN, MCP connectivity, or capture.
Unreadable credentials require the client's hidden prompt in a local terminal,
never a token pasted into chat.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

SKILL_ROOT = Path(__file__).resolve().parent.parent
CLIENT_SCRIPTS = {"mcp": SKILL_ROOT / "scripts" / "proxypin_mcp.py",
                  "export": SKILL_ROOT / "scripts" / "export_capture.py"}
MAX_DUMP_BYTES = 8 * 1024 * 1024
LABELS = frozenset({"MCP", "MCP Server", "MCP server", "MCP 服务", "MCP服务", "MCP服务器",
                   "Token", "token", "Bearer Token", "Bearer token", "MCP Token", "令牌",
                   "Endpoint", "endpoint", "URL", "地址", "服务地址", "端口", "Port",
                   "Copy", "复制", "Close", "关闭", "Cancel", "取消", "OK", "确定",
                   "Enable MCP", "启用MCP", "启用 MCP", "MCP Settings", "MCP设置"})
RESOURCE_IDS = frozenset({"android:id/content", "android:id/text1", "android:id/text2",
                          "android:id/edit", "android:id/message", "android:id/title",
                          "android:id/button1", "android:id/button2", "android:id/button3"})


class MobileMCPError(RuntimeError):
    """Contains a fixed diagnostic code, never raw UI values or credentials."""


def _load_nodes(path: Path) -> list[ET.Element]:
    raw_path = str(path)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw_path) or raw_path.startswith(("\\\\", "//")):
        raise MobileMCPError("local-ui-dump-required")
    try:
        resolved = path.resolve(strict=True)
        if resolved.is_relative_to(SKILL_ROOT):
            raise MobileMCPError("ui-dump-must-be-outside-skill-repository")
        if resolved.suffix.lower() != ".xml" or not resolved.is_file():
            raise MobileMCPError("adb-xml-ui-dump-required")
        if resolved.stat().st_size > MAX_DUMP_BYTES:
            raise MobileMCPError("ui-dump-size-limit-exceeded")
        with resolved.open("rb") as handle:
            data = handle.read(MAX_DUMP_BYTES + 1)
        if len(data) > MAX_DUMP_BYTES:
            raise MobileMCPError("ui-dump-size-limit-exceeded")
        text = data.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
            raise MobileMCPError("xml-dtd-and-entities-are-forbidden")
        root = ET.fromstring(text)
    except (OSError, ValueError, UnicodeError, ET.ParseError):
        raise MobileMCPError("invalid-local-adb-xml-ui-dump") from None
    if root.tag != "hierarchy" or any(element.tag not in {"hierarchy", "node"} for element in root.iter()):
        raise MobileMCPError("adb-xml-ui-dump-required")
    return list(root.iter("node"))


def _is_url(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value)
                or value.lower().startswith(("www.", "http:", "https:", "mailto:", "file:", "content:")))


def _endpoint(value: str) -> str | None:
    """Only reveal a numeric/localhost MCP endpoint with no private URL parts."""
    try:
        if "?" in value or "#" in value:
            return None
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or parts.path != "/mcp" or parts.query or parts.fragment:
            return None
        if parts.username is not None or parts.password is not None or not parts.hostname:
            return None
        if parts.hostname != "localhost":
            ipaddress.ip_address(parts.hostname)
        if parts.port is not None and not 1 <= parts.port <= 65535:
            return None
        if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _values(node: ET.Element) -> list[str]:
    values = []
    for field in ("text", "content-desc"):
        value = node.get(field, "")
        if value.strip() and value.strip() not in LABELS and value not in values:
            values.append(value)
    return values


def _token_value(node: ET.Element) -> str:
    values = _values(node)
    if len(values) != 1:
        raise MobileMCPError("local-hidden-input-required")
    value = values[0]
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise MobileMCPError("local-hidden-input-required")
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value or value in LABELS or value.lower() == "bearer":
        raise MobileMCPError("local-hidden-input-required")
    if _is_url(value):
        raise MobileMCPError("selected-node-is-url")
    if (re.search(r"[*\u2022\u25cf\u00b7]{2,}|\.{3}|\u2026", value)
            or re.search(r"(?:<|\[)?(?:redacted|hidden|masked)(?:>|\])?", value, re.IGNORECASE)
            or any(label in value for label in ("已隐藏", "已脱敏", "不可读取", "隐藏", "脱敏"))):
        raise MobileMCPError("local-hidden-input-required")
    return value


def _bounds(value: str) -> list[int] | None:
    match = re.fullmatch(r"\[(\d{1,6}),(\d{1,6})\]\[(\d{1,6}),(\d{1,6})\]", value)
    if not match:
        return None
    result = [int(number) for number in match.groups()]
    return result if result[0] <= result[2] and result[1] <= result[3] else None


def inspect_nodes(nodes: list[ET.Element]) -> dict:
    safe_nodes = []
    for index, node in enumerate(nodes):
        values = _values(node)
        try:
            _token_value(node)
            readable = True
        except MobileMCPError:
            readable = False
        raw_fields = [node.get(field, "") for field in ("text", "content-desc")]
        safe = {"node_index": index,
                "resource_id": node.get("resource-id") if node.get("resource-id") in RESOURCE_IDS else None,
                "bounds": _bounds(node.get("bounds", "")),
                "value_length": len(node.get("text") or node.get("content-desc") or ""),
                "candidate": {"readable": readable, "ambiguous": len(values) > 1,
                              "empty": not any(value.strip() for value in raw_fields),
                              "has_resource_id": bool(node.get("resource-id")),
                              "password_attribute": node.get("password") == "true"}}
        labels = sorted({value.strip() for value in raw_fields if value.strip() in LABELS})
        endpoints = sorted({endpoint for value in raw_fields if (endpoint := _endpoint(value.strip())) is not None})
        if labels:
            safe["labels"] = labels
        if endpoints:
            safe["endpoints"] = endpoints
        safe_nodes.append(safe)
    readable_count = sum(node["candidate"]["readable"] for node in safe_nodes)
    return {"status": "inspection-only" if readable_count else "local-hidden-input-required",
            "verification": "UI evidence only; VPN and MCP connectivity are not verified",
            "node_count": len(safe_nodes), "readable_candidate_count": readable_count, "nodes": safe_nodes}


def _safe_output(value: str | bytes | None, token: str) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    forms = {token, json.dumps(token, ensure_ascii=False)[1:-1], json.dumps(token, ensure_ascii=True)[1:-1], repr(token)[1:-1]}
    marker = next(candidate for candidate in ("<redacted>", "[removed]", "\u2588", "\u25a0")
                  if not any(form and form in candidate for form in forms))
    for form in sorted(forms, key=len, reverse=True):
        if form:
            text = text.replace(form, marker)
    return text


def run_client(nodes: list[ET.Element], node_index: int, client: str,
               flags: list[str], timeout: float = 180) -> int:
    if node_index < 0 or node_index >= len(nodes):
        raise MobileMCPError("selected-node-out-of-range")
    token = _token_value(nodes[node_index])
    for flag in flags:
        option = flag.split("=", 1)[0]
        if option.startswith("--") and len(option) > 2 and "--no-token".startswith(option):
            raise MobileMCPError("no-token-option-is-forbidden")
        if token in flag:
            raise MobileMCPError("credential-in-client-arguments-is-forbidden")
    script = CLIENT_SCRIPTS[client]
    if not script.is_file():
        raise MobileMCPError("known-client-script-is-unavailable")
    environment = os.environ.copy()
    environment["PROXYPIN_MCP_TOKEN"] = token
    command = [sys.executable, "-X", "utf8", "-B", str(script), "--require-token", *flags]
    try:
        result = subprocess.run(command, env=environment, shell=False, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        sys.stdout.write(_safe_output(exc.stdout, token))
        sys.stderr.write(_safe_output(exc.stderr, token))
        print("mobile-mcp-child-timeout", file=sys.stderr)
        return 124
    except OSError:
        raise MobileMCPError("mobile-mcp-child-could-not-start") from None
    sys.stdout.write(_safe_output(result.stdout, token))
    sys.stderr.write(_safe_output(result.stderr, token))
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    inspect = commands.add_parser("inspect", help="Print only safe metadata from this local UI dump")
    inspect.add_argument("--ui-dump", required=True, type=Path)
    run = commands.add_parser("run", help="Hand one explicitly selected UI node token to a known local client")
    run.add_argument("--ui-dump", required=True, type=Path)
    run.add_argument("--token-node", required=True, type=int, help="Zero-based node_index from inspect on this exact dump")
    run.add_argument("--client", required=True, choices=sorted(CLIENT_SCRIPTS))
    run.add_argument("--timeout", type=float, default=180, help="Child time limit in seconds, from 1 to 600 (default 180)")
    run.add_argument("client_flags", nargs=argparse.REMAINDER, help="After --, flags for the selected existing client")
    args = parser.parse_args(argv)
    if args.operation == "run" and (not 1 <= args.timeout <= 600):
        parser.error("--timeout must be from 1 to 600 seconds")
    try:
        nodes = _load_nodes(args.ui_dump)
        if args.operation == "inspect":
            result = inspect_nodes(nodes)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["status"] == "inspection-only" else 2
        flags = args.client_flags
        if flags and flags[0] == "--":
            flags = flags[1:]
        return run_client(nodes, args.token_node, args.client, flags, args.timeout)
    except MobileMCPError as exc:
        print(str(exc), file=sys.stderr)
        if str(exc) == "local-hidden-input-required":
            print("Use the selected client's getpass prompt in a local TTY; do not put a token in chat.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
