"""Offline CLI tests; every HTTP request uses a mocked in-memory MCP server."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import proxypin_mcp as mcp


class _Response(io.BytesIO):
    headers = {"Content-Type": "application/json"}


class _LocalMCP:
    """Mock HTTP opener with the real client's discovery and tools/call shapes."""

    def __init__(self, result=None, body: str = "", mutate=None):
        self.result = {"ok": True} if result is None else result
        self.body = body.encode("utf-8")
        self.mutate = mutate
        self.requests = []
        self.tools = [{"name": "test_tool", "description": "PRIVATE_TOOL_DESCRIPTION", "inputSchema": {"type": "object"}},
                      {"name": "get_flow_body", "inputSchema": {"type": "object"}}]

    def open(self, request, timeout=None):
        payload = json.loads(request.data)
        self.requests.append(payload)
        method = payload["method"]
        if method == "notifications/initialized":
            return _Response(b"")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            params = payload["params"]
            if params["name"] == "get_flow_body":
                arguments = params["arguments"]
                offset, limit = arguments["offset"], arguments["limit"]
                raw = self.body[offset:offset + limit]
                part = {"available": True, "binary": False, "offset": offset,
                        "returned": len(raw), "total": len(self.body),
                        "text": raw.decode("utf-8", errors="replace"),
                        "truncated": offset + len(raw) < len(self.body)}
                if self.mutate:
                    part = self.mutate(part, arguments)
                result = {"structuredContent": part}
            else:
                result = {"structuredContent": self.result}
        else:
            raise AssertionError(f"Unexpected mock MCP method: {method}")
        return _Response(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode("utf-8"))

    def calls(self, name):
        return [request["params"]["arguments"] for request in self.requests
                if request["method"] == "tools/call" and request["params"]["name"] == name]


class MCPCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="proxypin-cli-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def run_cli(self, args, server=None, token=""):
        server = server or _LocalMCP()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(mcp, "build_opener", return_value=server), \
                patch.dict(os.environ, {"PROXYPIN_MCP_TOKEN": token}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = mcp.main(["--endpoint", "http://127.0.0.1:9127/mcp", *args])
        return status, stdout.getvalue(), stderr.getvalue(), server

    def assert_parse_error(self, args):
        with patch.object(mcp, "build_opener") as opener, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                mcp.main(args)
            self.assertEqual(error.exception.code, 2)
            opener.assert_not_called()

    def test_help_describes_new_options_without_connecting(self):
        stdout = io.StringIO()
        with patch.object(mcp, "build_opener") as opener, contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as error:
                mcp.main(["--help"])
            self.assertEqual(error.exception.code, 0)
            opener.assert_not_called()
        for flag in ("--arguments-file", "--output", "--body-flow-id", "--body-output", "--history-id", "--overwrite"):
            self.assertIn(flag, stdout.getvalue())

    def test_explicit_arguments_and_file_are_mutually_exclusive(self):
        self.assert_parse_error(["--arguments", "{}", "--arguments-file", str(self.root / "args.json")])

    def test_tool_and_body_modes_are_mutually_exclusive(self):
        self.assert_parse_error(["--tool", "test_tool", "--body-flow-id", "actual-id", "--body-output", str(self.root / "body.txt")])

    def test_body_mode_requires_matching_output_and_rejects_tool_arguments(self):
        for args in (["--body-flow-id", "actual-id"],
                     ["--body-output", str(self.root / "body.txt")],
                     ["--history-id", "history-id"],
                     ["--body-flow-id", "actual-id", "--body-output", str(self.root / "body.txt"), "--arguments", "{}"],
                     ["--body-flow-id", "actual-id", "--body-output", str(self.root / "body.txt"), "--output", str(self.root / "result.json")]):
            with self.subTest(args=args):
                self.assert_parse_error(args)

    def test_arguments_file_accepts_utf8_bom_object_without_shell_quoting(self):
        arguments = {"keyword": "中文 with spaces and \"quotes\"", "path": "C:\\private\\capture", "offset": 0}
        source = self.root / "arguments.json"
        source.write_text(json.dumps(arguments, ensure_ascii=False), encoding="utf-8-sig")
        status, stdout, stderr, server = self.run_cli(["--tool", "test_tool", "--arguments-file", str(source)])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(server.calls("test_tool"), [arguments])
        self.assertEqual(json.loads(stdout), {"ok": True})

    def test_arguments_file_must_be_json_object_before_connecting(self):
        source, target = self.root / "arguments.json", self.root / "result.json"
        for content in ("[]", '"scalar"', "null", "broken JSON PRIVATE_INPUT"):
            with self.subTest(content=content):
                source.write_text(content, encoding="utf-8")
                status, stdout, stderr, server = self.run_cli(["--arguments-file", str(source), "--output", str(target)])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertNotIn("PRIVATE_INPUT", stderr)
                self.assertEqual(server.requests, [])
                self.assertFalse(target.exists())

    def test_explicit_empty_arguments_are_invalid(self):
        status, stdout, _, server = self.run_cli(["--arguments", ""])
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(server.requests, [])

    def test_default_tool_stdout_and_arguments_remain_compatible(self):
        result = {"items": ["中文", 5], "ok": True}
        status, stdout, stderr, server = self.run_cli(["--tool", "test_tool", "--arguments", '{"offset": 3}'], _LocalMCP(result=result))
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), result)
        self.assertEqual(server.calls("test_tool"), [{"offset": 3}])

    def test_default_tool_listing_remains_compatible(self):
        status, stdout, stderr, server = self.run_cli([])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"tools": server.tools})

    def test_tool_output_is_redacted_json_with_only_metadata_on_stdout(self):
        token = 'private-token-"中文\\value'
        target = self.root / "result.json"
        server = _LocalMCP(result={"secret": token, "nested": ["Bearer " + token], "marker": "PRIVATE_RESULT"})
        status, stdout, stderr, _ = self.run_cli(["--tool", "test_tool", "--output", str(target)], server, token)
        self.assertEqual((status, stderr), (0, ""))
        saved = target.read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved), {"secret": "<redacted>", "nested": ["Bearer <redacted>"], "marker": "PRIVATE_RESULT"})
        summary = json.loads(stdout)
        self.assertEqual(summary["output"], str(target.resolve()))
        self.assertEqual(summary["bytes"], target.stat().st_size)
        self.assertEqual(summary["format"], "json")
        self.assertNotIn(token, saved + stdout + stderr)
        self.assertNotIn("PRIVATE_RESULT", stdout)

    def test_tool_listing_output_has_count_without_metadata_body_on_stdout(self):
        target = self.root / "tools.json"
        status, stdout, stderr, server = self.run_cli(["--output", str(target)])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"tools": server.tools})
        self.assertEqual(json.loads(stdout)["tools"], 2)
        self.assertNotIn("PRIVATE_TOOL_DESCRIPTION", stdout)

    def test_body_output_preserves_non_json_utf8_and_history_id(self):
        body = "普通文本 PRIVATE_BODY\r\n😀 second line\n"
        target = self.root / "response.txt"
        status, stdout, stderr, server = self.run_cli(["--body-flow-id", "actual-flow-id", "--history-id", "actual-history-id",
                                                       "--body-output", str(target)], _LocalMCP(body=body))
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(target.read_bytes(), body.encode("utf-8"))
        self.assertEqual(server.calls("get_flow_body")[0]["history_id"], "actual-history-id")
        self.assertEqual(json.loads(stdout)["bytes"], len(body.encode("utf-8")))
        self.assertNotIn("PRIVATE_BODY", stdout)

    def test_utf8_boundary_retries_reconstruct_complete_response(self):
        for body, accepted_limit in (("x" * 65535 + "中文" * 200, 65535),
                                     ("x" * 65533 + "😀" + "汉" * 200, 65533)):
            with self.subTest(character=body[65533:65538]):
                target = self.root / f"response-{accepted_limit}.txt"
                server = _LocalMCP(body=body)
                status, stdout, stderr, _ = self.run_cli(["--body-flow-id", "actual-flow-id", "--body-output", str(target)], server)
                self.assertEqual((status, stderr), (0, ""))
                self.assertEqual(target.read_bytes(), body.encode("utf-8"))
                calls = server.calls("get_flow_body")
                self.assertEqual([call["limit"] for call in calls if call["offset"] == 0], list(range(65536, accepted_limit - 1, -1)))
                self.assertEqual(calls[-1]["offset"], accepted_limit)
                self.assertEqual(json.loads(stdout)["bytes"], len(server.body))

    def test_discovered_body_schema_limit_requires_a_positive_integer_and_is_capped(self):
        for maximum, expected in ((None, 65536), (0, 65536), (-1, 65536), (True, 65536),
                                  (3.5, 65536), ("5", 65536), (5, 5), (1, 1), (999999, 65536)):
            with self.subTest(maximum=maximum):
                server = _LocalMCP()
                server.tools[1]["inputSchema"]["properties"] = {"limit": {"maximum": maximum}}
                with patch.object(mcp, "build_opener", return_value=server):
                    client = mcp.ProxyPinMCP()
                    self.assertEqual(client.body_chunk_limit(), expected)
                    self.assertEqual(mcp.body_chunk_limit(client), expected)
                self.assertEqual([request["method"] for request in server.requests],
                                 ["initialize", "notifications/initialized", "tools/list"])

    def test_shared_body_limit_defaults_for_fakes_and_malformed_schema(self):
        self.assertEqual(mcp.body_chunk_limit(object()), 65536)
        for schema in (None, [], {"properties": None}, {"properties": {"limit": []}}):
            with self.subTest(schema=schema):
                server = _LocalMCP()
                server.tools[1]["inputSchema"] = schema
                with patch.object(mcp, "build_opener", return_value=server):
                    self.assertEqual(mcp.ProxyPinMCP().body_chunk_limit(), 65536)

    def test_small_discovered_body_limits_reconstruct_complete_utf8(self):
        for maximum, body in ((5, "abc中文d汉😀终"), (3, "中文汉字"),
                              (4, "😀中文😀"), (1, "plain ASCII")):
            with self.subTest(maximum=maximum):
                target = self.root / f"small-response-{maximum}.txt"
                server = _LocalMCP(body=body)
                server.tools[1]["inputSchema"]["properties"] = {"limit": {"maximum": maximum}}
                status, stdout, stderr, _ = self.run_cli(["--body-flow-id", "actual-id", "--body-output", str(target)], server)
                self.assertEqual((status, stderr), (0, ""))
                self.assertEqual(target.read_bytes(), body.encode("utf-8"))
                self.assertEqual(json.loads(stdout)["bytes"], len(server.body))
                calls = server.calls("get_flow_body")
                self.assertTrue(all(1 <= call["limit"] <= maximum for call in calls))
                if maximum == 5:
                    self.assertEqual([call["limit"] for call in calls if call["offset"] == 0], [5, 4, 3])

    def test_too_small_utf8_limits_never_send_zero_or_publish_partial_body(self):
        for maximum, body in ((1, "中文"), (2, "中文"), (3, "😀")):
            with self.subTest(maximum=maximum):
                target = self.root / f"too-small-{maximum}.txt"
                server = _LocalMCP(body=body)
                server.tools[1]["inputSchema"]["properties"] = {"limit": {"maximum": maximum}}
                status, stdout, stderr, _ = self.run_cli(["--body-flow-id", "actual-id", "--body-output", str(target)], server)
                self.assertEqual((status, stdout), (2, ""))
                self.assertTrue(stderr)
                self.assertFalse(target.exists())
                self.assertEqual([call["limit"] for call in server.calls("get_flow_body")], list(range(maximum, 0, -1)))

    def test_invalid_body_pages_never_publish_a_success_file(self):
        def change_after_first(key, value):
            def mutate(part, arguments):
                if arguments["offset"]:
                    part[key] = value(part) if callable(value) else value
                return part
            return mutate

        cases = {
            "offset": change_after_first("offset", 0),
            "total": change_after_first("total", lambda part: part["total"] + 1),
            "returned": change_after_first("returned", lambda part: part["returned"] + 1),
            "returned_boolean": change_after_first("returned", True),
            "truncated": change_after_first("truncated", True),
            "binary": change_after_first("binary", True),
            "unavailable": change_after_first("available", False),
            "no_progress": lambda part, arguments: dict(part, text="", returned=0, truncated=True),
            "size_limit": lambda part, arguments: dict(part, total=33 * 1024 * 1024),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                target = self.root / f"bad-{name}.txt"
                server = _LocalMCP(body="x" * 65536 + "PRIVATE_REMAINDER", mutate=mutate)
                status, stdout, stderr, _ = self.run_cli(["--body-flow-id", "actual-id", "--body-output", str(target)], server)
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr)
                self.assertNotIn("PRIVATE_REMAINDER", stderr)
                self.assertFalse(target.exists())
                self.assertEqual(list(self.root.glob(".proxypin-*.tmp")), [])

    def test_existing_output_is_preserved_without_explicit_overwrite(self):
        target = self.root / "existing.json"
        target.write_bytes(b"USER_EXISTING_FILE")
        status, stdout, stderr, server = self.run_cli(["--output", str(target)])
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--overwrite", stderr)
        self.assertEqual(server.requests, [])
        self.assertEqual(target.read_bytes(), b"USER_EXISTING_FILE")

    def test_explicit_overwrite_replaces_only_complete_result(self):
        target = self.root / "existing.txt"
        target.write_bytes(b"USER_EXISTING_FILE")
        status, _, stderr, _ = self.run_cli(["--body-flow-id", "actual-id", "--body-output", str(target), "--overwrite"], _LocalMCP(body="完整响应"))
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(target.read_bytes(), "完整响应".encode("utf-8"))
        failing = _LocalMCP(body="PRIVATE_BODY", mutate=lambda part, arguments: dict(part, offset=99))
        status, stdout, _, _ = self.run_cli(["--body-flow-id", "actual-id", "--body-output", str(target), "--overwrite"], failing)
        self.assertEqual((status, stdout), (2, ""))
        self.assertEqual(target.read_bytes(), "完整响应".encode("utf-8"))

    def test_failed_atomic_publish_preserves_old_file_and_cleans_temporary(self):
        target = self.root / "existing.json"
        target.write_bytes(b"USER_EXISTING_FILE")
        token = "private-error-token"
        with patch.object(mcp.os, "replace", side_effect=OSError("publish failed " + token)):
            status, stdout, stderr, _ = self.run_cli(["--output", str(target), "--overwrite"], token=token)
        self.assertEqual((status, stdout), (2, ""))
        self.assertNotIn(token, stderr)
        self.assertIn("<redacted>", stderr)
        self.assertEqual(target.read_bytes(), b"USER_EXISTING_FILE")
        self.assertEqual(list(self.root.glob(".proxypin-*.tmp")), [])

    def test_atomic_publish_does_not_overwrite_a_concurrent_file(self):
        target = self.root / "concurrent.txt"
        target.write_bytes(b"USER_CONCURRENT_FILE")
        with self.assertRaises(mcp.MCPError):
            mcp._write_output(target, "replacement")
        self.assertEqual(target.read_bytes(), b"USER_CONCURRENT_FILE")
        self.assertEqual(list(self.root.glob(".proxypin-*.tmp")), [])

    def test_repository_output_is_rejected_before_connecting(self):
        target = Path(mcp.__file__).resolve().parent.parent / "capture-private-test.txt"
        for args in (["--output", str(target)],
                     ["--body-flow-id", "actual-id", "--body-output", str(target)]):
            with self.subTest(args=args):
                status, stdout, stderr, server = self.run_cli(args)
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn("outside", stderr)
                self.assertEqual(server.requests, [])


if __name__ == "__main__":
    unittest.main()
