"""Offline checks for reliable selection and evidence-based product extraction."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from export_capture import CaptureExporter, decode_json, validate_mapping, safe_url, read_response
from proxypin_mcp import MCPError, ProxyPinMCP


def profile():
    return {"version": 1, "rules": [{"match": {"host": "api.example.test", "path": "/items"},
        "decode": {"type": "json", "success": {"path": "code", "equals": 0}},
        "records": "data.items.*", "fields": {"name": "title", "url": "images.*.url", "id": "id"}}]}


def flow(index=0, host="api.example.test"):
    return {"id": str(index), "url": f"https://{host}/items", "method": "GET", "status": 200}


class FakeClient:
    max_body_bytes = 32 * 1024 * 1024
    rpc_calls = 0

    def __init__(self, response=None, server_cap=10, schema_cap=None):
        self.response = response or {"code": 0, "data": {"items": [{"id": "x", "title": "商品甲", "images": [{"url": "https://cdn.example.test/a.png"}]}]}}
        self.server_cap = server_cap
        self.list_args = []
        self._tools = {"list_flows": {"inputSchema": {"properties": {"limit": {"maximum": schema_cap}}}}}

    def require_tools(self, names):
        pass

    @staticmethod
    def safe_text(value):
        return str(value)

    def call(self, name, args):
        self.rpc_calls += 1
        if name == "list_flows":
            self.list_args.append(dict(args))
            rows = [flow(i) for i in range(30)]
            return {"flows": rows[args["offset"]:args["offset"] + min(args["limit"], self.server_cap)], "total": 30}
        if name == "get_flow_body":
            raw = json.dumps(self.response, ensure_ascii=False).encode("utf-8")
            chunk = raw[args["offset"]:args["offset"] + args["limit"]]
            return {"available": True, "binary": False, "offset": args["offset"], "returned": len(chunk),
                    "total": len(raw), "truncated": args["offset"] + len(chunk) < len(raw), "text": chunk.decode("utf-8")}
        raise AssertionError(name)


class SchemaBodyClient(FakeClient):
    """Reuse the real schema reader while serving only in-memory byte slices."""

    body_chunk_limit = ProxyPinMCP.body_chunk_limit

    def __init__(self, text, maximum):
        super().__init__()
        self.raw = text.encode("utf-8")
        self.body_args = []
        self._tools["get_flow_body"] = {"inputSchema": {"properties": {"limit": {"maximum": maximum}}}}

    def call(self, name, args):
        if name != "get_flow_body":
            return super().call(name, args)
        self.body_args.append(dict(args))
        chunk = self.raw[args["offset"]:args["offset"] + args["limit"]]
        return {"available": True, "binary": False, "offset": args["offset"], "returned": len(chunk),
                "total": len(self.raw), "truncated": args["offset"] + len(chunk) < len(self.raw),
                "text": chunk.decode("utf-8", errors="replace")}


class ExportPipelineTests(unittest.TestCase):
    def test_isolated_python_imports_sibling_script(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPTS / "export_capture.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_capped_pages_use_returned_offset(self):
        client = FakeClient(server_cap=10)
        exporter = CaptureExporter(client)
        self.assertEqual(len(exporter.select(limit=20)), 20)
        self.assertEqual([a["offset"] for a in client.list_args], [0, 10])

    def test_schema_limit_is_respected(self):
        client = FakeClient(server_cap=10, schema_cap=5)
        exporter = CaptureExporter(client)
        self.assertEqual(len(exporter.select(limit=12)), 12)
        self.assertTrue(all(a["limit"] == 5 for a in client.list_args))

    def test_body_schema_limit_preserves_complete_utf8_response(self):
        for maximum, text in ((5, "abc中文d汉😀终"), (3, "中文汉字"),
                              (4, "😀中文😀"), (1, "plain ASCII")):
            with self.subTest(maximum=maximum):
                client = SchemaBodyClient(text, maximum)
                body, meta = read_response(client, "actual-id")
                self.assertEqual(body, text)
                self.assertEqual(meta, {"format": "text", "bytes": len(text.encode("utf-8"))})
                self.assertTrue(all(1 <= args["limit"] <= maximum for args in client.body_args))
                if maximum == 5:
                    self.assertEqual([args["limit"] for args in client.body_args if args["offset"] == 0], [5, 4, 3])

    def test_body_tiny_limits_never_send_zero_or_return_corrupted_text(self):
        for maximum, text in ((1, "中文"), (2, "中文"), (3, "😀")):
            with self.subTest(maximum=maximum):
                client = SchemaBodyClient(text, maximum)
                with self.assertRaises(MCPError):
                    read_response(client, "actual-id")
                self.assertEqual([args["limit"] for args in client.body_args], list(range(maximum, 0, -1)))

    def test_body_reader_uses_default_for_fake_without_limit_method(self):
        client = FakeClient()
        with patch.object(client, "call", wraps=client.call) as call:
            body, meta = read_response(client, "actual-id")
        self.assertEqual(call.call_args.args[1]["limit"], 65536)
        expected = json.dumps(client.response, ensure_ascii=False)
        self.assertEqual(body, expected)
        self.assertEqual(meta, {"format": "text", "bytes": len(expected.encode("utf-8"))})

    def test_listing_stops_at_empty_page(self):
        exporter = CaptureExporter(FakeClient())
        self.assertEqual(len(exporter.select(limit=100)), 30)
        self.assertEqual(exporter.selection["stop_reason"], "empty_page")

    def test_zero_selected_is_not_success(self):
        exporter = CaptureExporter(FakeClient(), profile())
        exporter.export([])
        self.assertEqual(exporter.errors[0]["reason"], "no_flows_selected")

    def test_unmatched_mapping_is_not_success(self):
        exporter = CaptureExporter(FakeClient(), profile())
        exporter.export([flow(host="other.example.test")])
        with tempfile.TemporaryDirectory() as directory:
            report = exporter.write(Path(directory))
        self.assertFalse(report["run_complete"])
        self.assertFalse(report["product_result_ready"])
        self.assertEqual(report["mapping"]["matched_flows"], 0)

    def test_missing_records_differs_from_observed_empty_array(self):
        missing = CaptureExporter(FakeClient({"code": 0, "data": {"wrong": []}}), profile())
        missing.export([flow()])
        self.assertEqual(missing.errors[0]["reason"], "mapping_records_path_missing")
        empty = CaptureExporter(FakeClient({"code": 0, "data": {"items": []}}), profile())
        empty.export([flow()])
        self.assertEqual(empty.errors, [])
        with tempfile.TemporaryDirectory() as directory:
            report = empty.write(Path(directory))
        self.assertTrue(report["run_complete"])
        self.assertFalse(report["product_result_ready"])
        self.assertEqual(report["mapping"]["empty_arrays"], 1)

    def test_http_200_business_failure_is_rejected(self):
        exporter = CaptureExporter(FakeClient({"code": 401, "data": {"items": []}}), profile())
        exporter.export([flow()])
        self.assertTrue(exporter.errors)
        self.assertEqual(exporter.products, [])

    def test_json_success_supports_string_and_exact_types(self):
        mapping = profile()
        mapping["rules"][0]["decode"]["success"]["equals"] = "OK"
        decoder = validate_mapping(mapping)["rules"][0]["decode"]
        self.assertEqual(decode_json('{"code":"OK"}', decoder), {"code": "OK"})
        with self.assertRaises(MCPError):
            decode_json('{"code":true}', {"type": "json", "success": {"path": "code", "equals": 1}})

    def test_successful_real_structure_reports_counts(self):
        exporter = CaptureExporter(FakeClient(), profile())
        exporter.export([flow()])
        with tempfile.TemporaryDirectory() as directory:
            report = exporter.write(Path(directory))
        self.assertTrue(report["product_result_ready"])
        self.assertEqual(report["mapping"]["matched_records"], 1)
        self.assertEqual(exporter.products[0]["name"], "商品甲")

    def test_source_metadata_removes_auth_keys(self):
        result = safe_url("https://api.example.test/items?token=private&userId=private&page=2&expires=123")
        self.assertNotIn("private", result)
        self.assertNotIn("token", result)
        self.assertNotIn("userId", result)
        self.assertNotIn("expires", result)
        self.assertIn("page=", result)


if __name__ == "__main__":
    unittest.main()
