"""Offline UI-dump/token handoff tests; MCP and device processes are never run."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mobile_mcp as mobile


class MobileMCPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mobile-mcp-offline-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def dump(self, attributes):
        root = ET.Element("hierarchy", rotation="0")
        parent = ET.SubElement(root, "node", {"text": "", "resource-id": "android:id/content", "bounds": "[0,0][1080,1920]"})
        for item in attributes:
            ET.SubElement(parent, "node", {"bounds": "[10,20][300,80]", **item})
        path = self.root / "current-ui.xml"
        path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
        return path

    def run_cli(self, flags, child=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        child = child or subprocess.CompletedProcess([], 0, "", "")
        with patch.object(mobile.subprocess, "run", return_value=child) as run, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = mobile.main(flags)
        return status, stdout.getvalue(), stderr.getvalue(), run

    def run_flags(self, path, index=1, client="mcp", flags=None):
        return ["run", "--ui-dump", str(path), "--token-node", str(index), "--client", client,
                "--", *(flags or ["--help"])]

    def test_inspect_hides_short_long_and_arbitrary_raw_values(self):
        secrets = ["Q7", "LONG_CREDENTIAL_" + "ab9" * 3000, 'strange-"中文\\credential']
        path = self.dump([{"text": secret, "resource-id": secret, "class": secret, "package": secret} for secret in secrets])
        status, stdout, stderr, child = self.run_cli(["inspect", "--ui-dump", str(path)])
        self.assertEqual((status, stderr), (0, ""))
        child.assert_not_called()
        result = json.loads(stdout)
        self.assertEqual(result["node_count"], 4)
        for index, secret in enumerate(secrets, 1):
            self.assertNotIn(secret, stdout)
            self.assertNotIn(json.dumps(secret)[1:-1], stdout)
            node = result["nodes"][index]
            self.assertEqual(node["node_index"], index)
            self.assertEqual(node["value_length"], len(secret))
            self.assertIsNone(node["resource_id"])
            self.assertEqual(node["bounds"], [10, 20, 300, 80])
            self.assertTrue(node["candidate"]["readable"])
        self.assertIn("not verified", result["verification"])

    def test_inspect_only_reveals_fixed_labels_and_verified_endpoint(self):
        endpoint = "http://127.0.0.1:9127/mcp"
        private_query = "http://127.0.0.1:9127/mcp?token=PRIVATE_QUERY"
        private_host = "https://PRIVATE_HOST.example/mcp"
        path = self.dump([{"text": "MCP Server"}, {"text": endpoint}, {"text": private_query},
                          {"text": private_host}, {"text": "PRIVATE_UNKNOWN_UI_LABEL"}])
        status, stdout, _, _ = self.run_cli(["inspect", "--ui-dump", str(path)])
        self.assertEqual(status, 0)
        result = json.loads(stdout)
        self.assertEqual(result["nodes"][1]["labels"], ["MCP Server"])
        self.assertEqual(result["nodes"][2]["endpoints"], [endpoint])
        for private in ("PRIVATE_QUERY", "PRIVATE_HOST", "PRIVATE_UNKNOWN_UI_LABEL"):
            self.assertNotIn(private, stdout)
        self.assertNotIn("endpoints", result["nodes"][3])
        self.assertNotIn("endpoints", result["nodes"][4])

    def test_inspect_with_only_empty_or_masked_fields_requires_local_prompt(self):
        path = self.dump([{"text": "Token"}, {"text": "****"}, {"text": ""}, {"text": "Bearer <redacted>"}])
        status, stdout, stderr, child = self.run_cli(["inspect", "--ui-dump", str(path)])
        self.assertEqual((status, stderr), (2, ""))
        self.assertEqual(json.loads(stdout)["status"], "local-hidden-input-required")
        child.assert_not_called()

    def test_run_uses_exact_selected_index_and_bearer_prefix(self):
        selected = "SELECTED_PRIVATE_9"
        path = self.dump([{"text": "OTHER_PRIVATE_7"}, {"text": "Bearer " + selected}])
        status, stdout, stderr, child = self.run_cli(self.run_flags(path, index=2))
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        command = child.call_args.args[0]
        options = child.call_args.kwargs
        self.assertEqual(command[:4], [sys.executable, "-X", "utf8", "-B"])
        self.assertEqual(command[4], str(mobile.CLIENT_SCRIPTS["mcp"]))
        self.assertEqual(command[5:], ["--require-token", "--help"])
        self.assertEqual(options["env"]["PROXYPIN_MCP_TOKEN"], selected)
        self.assertFalse(options["shell"])
        self.assertEqual(options["timeout"], 180)
        self.assertNotIn(selected, " ".join(command))

    def test_run_content_description_and_duplicate_value_are_supported(self):
        token = "DESCRIPTION_PRIVATE_4"
        for attributes in ({"content-desc": token}, {"text": token, "content-desc": token},
                           {"text": token, "content-desc": "Token"}):
            with self.subTest(attributes=list(attributes)):
                path = self.dump([attributes])
                status, _, _, child = self.run_cli(self.run_flags(path))
                self.assertEqual(status, 0)
                self.assertEqual(child.call_args.kwargs["env"]["PROXYPIN_MCP_TOKEN"], token)

    def test_run_never_guesses_between_multiple_values_in_selected_node(self):
        path = self.dump([{"text": "PRIVATE_VALUE_A", "content-desc": "PRIVATE_VALUE_B"}])
        status, stdout, stderr, child = self.run_cli(self.run_flags(path))
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("local-hidden-input-required", stderr)
        self.assertNotIn("PRIVATE_VALUE", stderr)
        child.assert_not_called()

    def test_selected_url_is_rejected(self):
        for value in ("http://127.0.0.1:9127/mcp", "https://example.invalid/?token=PRIVATE_VALUE",
                      "file:///private/token", "mailto:user@example.invalid"):
            with self.subTest(scheme=value.split(":", 1)[0]):
                path = self.dump([{"text": value}])
                status, stdout, stderr, child = self.run_cli(self.run_flags(path))
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn("selected-node-is-url", stderr)
                self.assertNotIn(value, stderr)
                child.assert_not_called()

    def test_empty_masked_redacted_and_control_values_require_local_prompt(self):
        for value in ("", " ", "Bearer ", "****", "abc**def", "••••", "<redacted>", "[MASKED]",
                      "abc...xyz", "已脱敏", "abc\nsecret", "abc\tsecret", "abc\u200bsecret"):
            with self.subTest(value_length=len(value)):
                path = self.dump([{"text": value}])
                status, stdout, stderr, child = self.run_cli(self.run_flags(path))
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn("local-hidden-input-required", stderr)
                self.assertIn("local TTY", stderr)
                child.assert_not_called()

    def test_parent_environment_is_not_changed_or_reused_as_mobile_token(self):
        token = "UI_PRIVATE_MOBILE_5"
        path = self.dump([{"text": token}])
        with patch.dict(os.environ, {"PROXYPIN_MCP_TOKEN": "EXISTING_PARENT_VALUE", "MOBILE_TEST_PARENT": "kept"}):
            before = dict(os.environ)
            status, _, _, child = self.run_cli(self.run_flags(path))
            self.assertEqual(status, 0)
            self.assertEqual(dict(os.environ), before)
            self.assertEqual(child.call_args.kwargs["env"]["PROXYPIN_MCP_TOKEN"], token)
            self.assertEqual(child.call_args.kwargs["env"]["MOBILE_TEST_PARENT"], "kept")
            self.assertIsNot(child.call_args.kwargs["env"], os.environ)

    def test_export_client_uses_only_known_local_script_and_array_flags(self):
        token = "EXPORT_PRIVATE_7"
        path = self.dump([{"text": token}])
        flags = ["--flow-id", "actual-flow-id", "--output-dir", str(self.root / "capture with spaces")]
        status, _, _, child = self.run_cli(self.run_flags(path, client="export", flags=flags))
        self.assertEqual(status, 0)
        command = child.call_args.args[0]
        self.assertEqual(command[4], str(mobile.CLIENT_SCRIPTS["export"]))
        self.assertEqual(command[5:], ["--require-token", *flags])
        self.assertNotIn(token, command)

    def test_no_token_options_and_argparse_abbreviations_are_rejected(self):
        path = self.dump([{"text": "UI_PRIVATE_4"}])
        for flag in ("--no-token", "--no-token=true", "--no-t", "--no", "--n"):
            with self.subTest(flag=flag):
                status, stdout, stderr, child = self.run_cli(self.run_flags(path, flags=[flag]))
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn("no-token-option-is-forbidden", stderr)
                child.assert_not_called()

    def test_credentials_in_forwarded_flags_are_rejected(self):
        token = "FORWARDED_PRIVATE_4"
        path = self.dump([{"text": token}])
        status, stdout, stderr, child = self.run_cli(self.run_flags(path, flags=["--arguments", json.dumps({"secret": token})]))
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("credential-in-client-arguments-is-forbidden", stderr)
        self.assertNotIn(token, stderr)
        child.assert_not_called()

    def test_child_error_and_json_escaped_output_are_redacted_and_code_preserved(self):
        token = 'CHILD_PRIVATE_"中文\\9'
        path = self.dump([{"text": token}])
        escaped = json.dumps(token, ensure_ascii=True)[1:-1]
        child_result = subprocess.CompletedProcess([], 7, "stdout token=" + token, "stderr escaped=" + escaped + " raw=" + token)
        status, stdout, stderr, child = self.run_cli(self.run_flags(path), child_result)
        self.assertEqual(status, 7)
        self.assertNotIn(token, stdout + stderr)
        self.assertNotIn(escaped, stdout + stderr)
        self.assertIn("<redacted>", stdout)
        self.assertIn("<redacted>", stderr)
        self.assertEqual(child.call_args.kwargs["env"]["PROXYPIN_MCP_TOKEN"], token)

    def test_short_tokens_are_handed_off_without_a_minimum_length(self):
        for token in ("Q7", "x", "a"):
            with self.subTest(token_length=len(token)):
                path = self.dump([{"text": token}])
                status, stdout, stderr, child = self.run_cli(self.run_flags(path, flags=["--help"]),
                                                            subprocess.CompletedProcess([], 0, token, token))
                # 'a' does not occur in --help; redaction markers must not reinsert it.
                self.assertEqual(status, 0)
                self.assertNotIn(token, stdout + stderr)
                self.assertEqual(child.call_args.kwargs["env"]["PROXYPIN_MCP_TOKEN"], token)

    def test_out_of_range_node_does_not_spawn_child(self):
        path = self.dump([{"text": "PRIVATE_UI_4"}])
        for index in (-1, 2, 999):
            with self.subTest(index=index):
                status, stdout, stderr, child = self.run_cli(self.run_flags(path, index=index))
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn("selected-node-out-of-range", stderr)
                child.assert_not_called()

    def test_timeout_is_bounded_and_partial_output_is_redacted(self):
        token = "TIMEOUT_PRIVATE_8"
        path = self.dump([{"text": token}])
        stdout, stderr = io.StringIO(), io.StringIO()
        failure = subprocess.TimeoutExpired("safe mock command", 180, output=token.encode(), stderr=token.encode())
        with patch.object(mobile.subprocess, "run", side_effect=failure), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = mobile.main(self.run_flags(path))
        self.assertEqual(status, 124)
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())
        self.assertIn("mobile-mcp-child-timeout", stderr.getvalue())

    def test_invalid_timeout_is_rejected_before_reading_or_spawning(self):
        for value in ("0", "601", "nan", "inf"):
            with self.subTest(value=value), patch.object(mobile, "_load_nodes") as load, \
                    patch.object(mobile.subprocess, "run") as child, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    mobile.main(["run", "--ui-dump", str(self.root / "missing.xml"), "--token-node", "1", "--client", "mcp", "--timeout", value])
                self.assertEqual(error.exception.code, 2)
                load.assert_not_called()
                child.assert_not_called()

    def test_dump_inside_skill_repository_is_rejected(self):
        with patch.object(mobile, "SKILL_ROOT", self.root):
            path = self.dump([{"text": "PRIVATE_UI_8"}])
            status, stdout, stderr, child = self.run_cli(["inspect", "--ui-dump", str(path)])
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("outside-skill-repository", stderr)
        child.assert_not_called()

    def test_dtd_and_entities_are_rejected_before_xml_parser(self):
        path = self.root / "unsafe.xml"
        for declaration in ('<!DOCTYPE hierarchy SYSTEM "https://example.invalid/private">',
                            '<!DOCTYPE hierarchy [<!ENTITY token SYSTEM "file:///private">]>'):
            with self.subTest(declaration_length=len(declaration)):
                path.write_text(declaration + '<hierarchy><node text="&token;" /></hierarchy>', encoding="utf-8")
                with patch.object(mobile.ET, "fromstring") as parse:
                    status, stdout, stderr, child = self.run_cli(["inspect", "--ui-dump", str(path)])
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn("xml-dtd-and-entities-are-forbidden", stderr)
                parse.assert_not_called()
                child.assert_not_called()

    def test_nonlocal_and_unsupported_dump_inputs_are_rejected(self):
        wrong_root = self.root / "wrong.xml"
        wrong_root.write_text('<not-adb secret="PRIVATE_ATTRIBUTE" />', encoding="utf-8")
        wrong_encoding = self.root / "utf16.xml"
        wrong_encoding.write_text('<hierarchy><node text="PRIVATE_UTF16" /></hierarchy>', encoding="utf-16")
        wrong_extension = self.root / "dump.json"
        wrong_extension.write_text("{}", encoding="utf-8")
        for path in (wrong_root, wrong_encoding, wrong_extension, "https://example.invalid/ui.xml", "\\\\invalid-server\\share\\ui.xml"):
            with self.subTest(path_kind=Path(path).suffix):
                status, stdout, stderr, child = self.run_cli(["inspect", "--ui-dump", str(path)])
                self.assertEqual((status, stdout), (2, ""))
                self.assertNotIn("PRIVATE_", stderr)
                child.assert_not_called()

    def test_isolated_python_help_works_without_importing_clients(self):
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", str(Path(mobile.__file__).resolve()), "--help"],
                                shell=False, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("inspect", result.stdout)
        self.assertIn("run", result.stdout)


if __name__ == "__main__":
    unittest.main()
