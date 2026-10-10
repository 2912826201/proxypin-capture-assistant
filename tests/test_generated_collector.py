"""Offline fixture tests. No mock result is submitted as real live evidence."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import generate_collector as factory


def fixture_config(strategy="direct_http"):
    return {"version": 1, "target": "offline unit fixture", "strategy": strategy,
            "allowed_hosts": ["api.public.cn"], "max_pages": 5, "adapter_config": {}}


def fixture_evidence(strategy="direct_http"):
    # Schema fixture only, intentionally confined to TemporaryDirectory.
    return {"version": 1, "target": "offline unit fixture", "strategy": strategy,
            "source": "direct_replay" if strategy == "direct_http" else "live_capture",
            "status": "agent_verified", "sample_count": 1,
            "observed_utc": "2026-10-10T12:00:00+08:00",
            "checks": {"full_body": True, "business_success": True, "mapping": True, "readonly": True}}


def row(identifier="a", url=None):
    return {"name": "商品甲", "url": url or "https://img.public.cn/a.jpg", "id": identifier,
            "source": "https://api.public.cn/items?page=1&filter=PRIVATE", "request_id": "observed-flow"}


class Response(io.BytesIO):
    headers = {}


class ControlledHMACServer:
    """In-memory test protocol, unrelated to any target App signing scheme."""
    def __init__(self, secret="OFFLINE_TEST_SIGNING_KEY", clock=1000):
        self.secret, self.clock = secret.encode("utf-8"), clock
        self.used_nonces, self.requests, self.accepted_pages = set(), [], []

    def open(self, request, timeout=None):
        payload = json.loads(request.data)
        self.requests.append(payload)
        unsigned = {key: value for key, value in payload.items() if key != "signature"}
        expected = hmac.new(self.secret, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8"), hashlib.sha256).hexdigest()
        if (not hmac.compare_digest(payload.get("signature", ""), expected)
                or payload.get("time") != self.clock or payload.get("nonce") in self.used_nonces):
            raise HTTPError(request.full_url, 403, "PRIVATE_SIGNATURE_DETAIL", {}, None)
        self.used_nonces.add(payload["nonce"])
        self.accepted_pages.append(payload["page"])
        item = row(str(payload["page"]), "https://img.public.cn/page" + str(payload["page"]) + ".jpg")
        return Response(json.dumps({"code": 0, "items": [item]}).encode("utf-8"))


CONTROLLED_SIGNING_ADAPTER = '''import hashlib
import hmac
import json

def run(context):
    secret = context.secret('COLLECTOR_SIGNING_TEST_KEY').encode('utf-8')
    settings = context.config['adapter_config']
    first = context.state.get('page', 0) + 1
    saved = None
    for page in range(first, first + context.max_pages):
        if page == settings.get('fail_refresh_page'):
            context.fail('signature_refresh_failed')
        unsigned = {'page': page, 'body': {'category': 'controlled-fixture'},
                    'nonce': settings['run_nonce'] + '-' + str(page), 'time': settings['clock']}
        signature = hmac.new(secret, json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode('utf-8'), hashlib.sha256).hexdigest()
        if settings.get('mode') == 'copied_signature' and saved is not None:
            signature = saved
        saved = signature
        payload = dict(unsigned, signature=signature)
        data = context.http_json('https://api.public.cn/items', method='POST', body=payload)
        context.require_equal(data['code'], 0)
        yield {'items': data['items'], 'state': {'page': page}}
'''


INDEPENDENT_SIGNING_ADAPTER = '''import hashlib
import hmac
import json
import secrets
import time

def run(context):
    secret = context.secret('COLLECTOR_SIGNING_TEST_KEY').encode('utf-8')
    settings = context.config['adapter_config']
    run_id = secrets.token_hex(12)
    saved = None
    for page in range(1, context.max_pages + 1):
        timestamp = time.time_ns() // 1000000
        if settings['mode'] == 'stale_signature' and page == 2:
            timestamp -= 60000
        unsigned = {'page': page, 'body': {'category': 'controlled-fixture', 'offset': (page - 1) * 20, 'run': run_id},
                    'nonce': secrets.token_hex(16), 'time': timestamp}
        signature = hmac.new(secret, json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode('utf-8'), hashlib.sha256).hexdigest()
        if settings['mode'] == 'copied_signature' and saved is not None:
            signature = saved
        saved = signature
        data = context.http_json(settings['endpoint'] + '/items', method='POST', body=dict(unsigned, signature=signature))
        context.require_equal(data['code'], 0)
        yield {'items': data['items'], 'state': {'page': page}}
'''


OFFLINE_CHILD_HARNESS = '''# Unit-test-only launcher: no invented factory live evidence.
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('controlled_runtime', root / 'collect_data.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
config = json.loads((root / 'collector_config.json').read_text(encoding='utf-8'))
assert config['verification']['status'] == 'offline_controlled_test_only'
assert config['allowed_hosts'] == ['127.0.0.1']
original_host_check = runtime.valid_host
def only_test_loopback(host):
    return host if host == '127.0.0.1' else original_host_check(host)
# Exercise the actual runtime/adapter in an independent process, while keeping
# both its historical evidence label and its HTTP destination explicitly local.
# Production validation still rejects this config without these test patches.
with patch.object(runtime, 'validate_config', return_value=config), patch.object(runtime, 'valid_host', side_effect=only_test_loopback):
    raise SystemExit(runtime.main())
'''


class IndependentSigningServer(ThreadingHTTPServer):
    """Real loopback HTTP verifier. This is not a target App/API algorithm."""
    daemon_threads = True

    def __init__(self):
        self.test_secret = b"OFFLINE_TEST_SIGNING_KEY"
        self.state_lock = threading.Lock()
        self.accepted, self.rejected, self.seen_nonces = [], [], set()
        super().__init__(("127.0.0.1", 0), IndependentSigningHandler)


class IndependentSigningHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No signature, secret, request body, or URL diagnostics in logs.

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if self.path != "/items" or not 1 <= length <= 1024 * 1024:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            unsigned = {key: value for key, value in payload.items() if key != "signature"}
            expected = hmac.new(self.server.test_secret, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(payload.get("signature", ""), expected):
                reason = "invalid_signature"
            elif type(payload.get("time")) is not int or abs(time.time_ns() // 1000000 - payload["time"]) > 10000:
                reason = "stale_timestamp"
            else:
                reason = None
            with self.server.state_lock:
                if payload.get("nonce") in self.server.seen_nonces:
                    reason = "reused_nonce"
                if reason:
                    self.server.rejected.append({"reason": reason, "payload": payload})
                else:
                    self.server.seen_nonces.add(payload["nonce"])
                    self.server.accepted.append(payload)
            if reason:
                status, result = 403, {"error": "controlled_test_request_rejected"}
            else:
                identifier = payload["body"]["run"] + ":" + str(payload["page"])
                product = row(identifier, "https://img.public.cn/" + identifier.replace(":", "-") + ".jpg")
                product["source"] = "http://127.0.0.1:" + str(self.server.server_port) + "/items"
                status, result = 200, {"code": 0, "items": [product]}
        except (ValueError, TypeError, KeyError):
            status, result = 400, {"error": "invalid_controlled_test_payload"}
        raw = json.dumps(result).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class GeneratedCollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="collector-offline-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.inputs = self.directory / "inputs"
        self.inputs.mkdir()
        self.adapter = self.inputs / "adapter.py"
        self.config = self.inputs / "config.json"
        self.evidence = self.inputs / "evidence.json"
        self.bundle = self.directory / "bundle"
        self.output = self.directory / "output"

    def generate(self, code=None, config=None, evidence=None):
        self.adapter.write_text(code or "def run(context):\n    yield {'items': [], 'state': {}}\n", encoding="utf-8")
        self.config.write_text(json.dumps(config or fixture_config()), encoding="utf-8")
        self.evidence.write_text(json.dumps(evidence or fixture_evidence()), encoding="utf-8")
        return factory.generate(self.adapter, self.config, self.evidence, self.bundle)

    def load_runtime(self):
        spec = importlib.util.spec_from_file_location("generated_fixture_runtime", self.bundle / "collect_data.py")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        return runtime

    def collect(self, runtime=None, flags=()):
        runtime = runtime or self.load_runtime()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = runtime.main(["--output-dir", str(self.output), *flags], bundle_dir=self.bundle)
        return code, stdout.getvalue(), stderr.getvalue()

    def read(self, name):
        return json.loads((self.output / name).read_text(encoding="utf-8"))

    def test_bundle_relocates_and_runs_in_isolated_python(self):
        product = row()
        self.generate("from export_capture import safe_url\ndef run(context):\n    yield {'items': [" + repr(product) + "], 'state': {'page': 1}, 'stop_reason': 'end_of_data'}\n")
        relocated = self.directory / "relocated"
        shutil.copytree(self.bundle, relocated)
        process = subprocess.run([sys.executable, "-I", "-B", str(relocated / "collect_data.py"), "--output-dir", str(self.output), "--probe", "1"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20, cwd=self.inputs)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(self.read("products.json")[0]["source"], "https://api.public.cn/items")
        self.assertEqual(self.read("report.json")["collection_verification"], "adapter_assertions_not_independently_verified")
        self.assertEqual(self.read("report.json")["images_downloaded"], 0)
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), sorted(factory.runtime.FILES))

    def test_probe_never_advances_generator_after_requested_page(self):
        self.generate("def run(context):\n    yield {'items': [" + repr(row()) + "], 'state': {'page': 1}}\n    context.fail('extra_page_was_requested')\n")
        status, _, _ = self.collect(flags=("--probe", "1"))
        self.assertEqual(status, 0)
        self.assertEqual(self.read("report.json")["stop_reason"], "probe_limit")

    def test_multi_images_expand_and_dedup_keeps_product_relations(self):
        items = [row("a", ["https://img.public.cn/a.jpg", "https://img.public.cn/b.jpg"]), row("a"), row("b")]
        self.generate("def run(context):\n    yield {'items': " + repr(items) + ", 'state': {}}\n")
        self.assertEqual(self.collect()[0], 0)
        products = self.read("products.json")
        self.assertEqual(len(products), 3)
        self.assertEqual([item["id"] for item in products], ["a", "a", "b"])

    def test_login_stop_keeps_partial_output_and_reports_local_action(self):
        code = "def run(context):\n    yield {'items': [" + repr(row()) + "], 'state': {'page': 1}}\n    context.fail('login_required', 'Open the target App and log in locally, then resume.')\n"
        self.generate(code)
        status, _, stderr = self.collect()
        self.assertEqual(status, 2)
        self.assertIn("LOGIN_REQUIRED", stderr)
        self.assertEqual(len(self.read("products.json")), 1)
        self.assertEqual(self.read("checkpoint.json")["state"], {"page": 1})
        self.assertEqual(self.read("report.json")["status"], "partial")

    def test_bare_business_login_error_defaults_local_action_and_keeps_partial(self):
        self.generate("def run(context):\n    yield {'items': [" + repr(row()) + "], 'state': {'page': 1}}\n    context.fail('login_required')\n")
        status, _, stderr = self.collect()
        self.assertEqual(status, 2)
        self.assertIn("LOGIN_REQUIRED: Log in to the target App locally", stderr)
        self.assertEqual(len(self.read("products.json")), 1)
        self.assertEqual(self.read("checkpoint.json")["state"], {"page": 1})
        self.assertEqual(self.read("report.json")["status"], "partial")
        self.assertEqual(self.read("report.json")["user_action_required"], "Log in to the target App locally, then resume collection.")

    def test_independent_process_signers_use_fresh_runtime_hmac_and_reject_replay(self):
        server = IndependentSigningServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(close_server)
        self.bundle.mkdir()
        for name, source in (("collect_data.py", ROOT / "assets" / "collector_runtime.py"),
                             ("proxypin_mcp.py", ROOT / "scripts" / "proxypin_mcp.py"),
                             ("export_capture.py", ROOT / "scripts" / "export_capture.py")):
            shutil.copyfile(source, self.bundle / name)
        (self.bundle / "target_adapter.py").write_text(INDEPENDENT_SIGNING_ADAPTER, encoding="utf-8")
        launcher = self.bundle / "offline_child.py"
        launcher.write_text(OFFLINE_CHILD_HARNESS, encoding="utf-8")
        config = fixture_config()
        config.update(allowed_hosts=["127.0.0.1"], max_pages=2,
                      verification={"status": "offline_controlled_test_only", "source": "loopback_hmac_verifier"})
        endpoint = "http://127.0.0.1:" + str(server.server_port)
        child_environment = os.environ.copy()
        child_environment["COLLECTOR_SIGNING_TEST_KEY"] = "OFFLINE_TEST_SIGNING_KEY"

        def run_child(label, mode):
            config["adapter_config"] = {"endpoint": endpoint, "mode": mode}
            (self.bundle / "collector_config.json").write_text(json.dumps(config), encoding="utf-8")
            output = self.directory / label
            process = subprocess.Popen([sys.executable, "-I", "-B", str(launcher), "--output-dir", str(output), "--max-pages", "2"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                       cwd=self.inputs, env=child_environment)
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("Controlled child process timed out")
            files = {name: json.loads((output / name).read_text(encoding="utf-8")) for name in factory.runtime.FILES}
            self.assertEqual(files["report.json"]["source_evidence"]["status"], "offline_controlled_test_only")
            content = stdout + stderr + "".join((output / name).read_text(encoding="utf-8") for name in factory.runtime.FILES)
            self.assertNotIn("OFFLINE_TEST_SIGNING_KEY", content)
            with server.state_lock:
                signatures = [request["signature"] for request in server.accepted]
                signatures += [entry["payload"]["signature"] for entry in server.rejected]
            self.assertTrue(all(signature not in content for signature in signatures))
            return process.pid, process.returncode, stdout, stderr, files

        before = time.time_ns() // 1000000
        first = run_child("fresh-process-one", "fresh")
        second = run_child("fresh-process-two", "fresh")
        after = time.time_ns() // 1000000
        self.assertNotEqual(first[0], second[0])
        self.assertEqual((first[1], second[1]), (0, 0), first[3] + second[3])
        self.assertEqual([request["page"] for request in server.accepted], [1, 2, 1, 2])
        self.assertEqual([request["body"]["offset"] for request in server.accepted], [0, 20, 0, 20])
        self.assertEqual(len({request["nonce"] for request in server.accepted}), 4)
        self.assertEqual(len({request["signature"] for request in server.accepted}), 4)
        self.assertTrue(all(before <= request["time"] <= after for request in server.accepted))
        first_ids = {item["id"] for item in first[4]["products.json"]}
        second_ids = {item["id"] for item in second[4]["products.json"]}
        self.assertEqual((len(first_ids), len(second_ids)), (2, 2))
        self.assertTrue(first_ids.isdisjoint(second_ids))

        copied = run_child("copied-process", "copied_signature")
        stale = run_child("stale-process", "stale_signature")
        self.assertEqual((copied[1], stale[1]), (2, 2))
        for result in (copied, stale):
            self.assertEqual(len(result[4]["products.json"]), 1)
            self.assertEqual(result[4]["report.json"]["status"], "partial")
            self.assertEqual(result[4]["report.json"]["stop_reason"], "access_denied_or_signature_required")
            self.assertNotIn("LOGIN_REQUIRED", result[3])
        self.assertEqual([entry["reason"] for entry in server.rejected], ["invalid_signature", "stale_timestamp"])

        # This whole path is deliberately test-only. A real generated collector
        # still rejects its offline proof and reserved host outside the harness.
        with self.assertRaises(factory.CollectorError):
            factory.runtime.validate_config(config)

    def test_no_empty_success_and_exception_never_echoes_private_data(self):
        self.generate("def run(context):\n    raise RuntimeError('PRIVATE_TOKEN_RESPONSE')\n    yield\n")
        status, stdout, stderr = self.collect()
        self.assertEqual(status, 2)
        self.assertNotIn("PRIVATE_TOKEN_RESPONSE", stdout + stderr)
        self.assertEqual(self.read("errors.json"), [{"reason": "adapter_failed", "page": 1}])

    def test_empty_array_is_failure(self):
        self.generate()
        self.assertEqual(self.collect()[0], 2)
        self.assertEqual(self.read("report.json")["stop_reason"], "no_products_collected")

    def test_resume_uses_committed_adapter_state_and_rejects_changed_products(self):
        code = "def run(context):\n    page = context.state.get('page', 0) + 1\n    item = " + repr(row()) + "\n    item['id'] = str(page)\n    yield {'items': [item], 'state': {'page': page}}\n"
        self.generate(code)
        self.assertEqual(self.collect(flags=("--probe", "1"))[0], 0)
        self.assertEqual(self.collect(flags=("--resume", "--probe", "1"))[0], 0)
        self.assertEqual([item["id"] for item in self.read("products.json")], ["1", "2"])
        (self.output / "products.json").write_text("[]", encoding="utf-8")
        status, _, stderr = self.collect(flags=("--resume",))
        self.assertEqual(status, 2)
        self.assertIn("checkpoint_or_products_changed", stderr)

    def test_resume_budget_is_remaining_global_cap_and_completed_resume_does_no_work(self):
        config = fixture_config()
        config["max_pages"] = 3
        code = "def run(context):\n    first = context.state.get('page', 0) + 1\n    for page in range(first, first + context.max_pages):\n        item = " + repr(row()) + "\n        item['id'] = str(page)\n        yield {'items': [item], 'state': {'page': page, 'invocation_budget': context.max_pages}}\n"
        self.generate(code, config=config)
        self.assertEqual(self.collect(flags=("--max-pages", "2"))[0], 0)
        self.assertEqual(self.read("checkpoint.json")["pages"], 2)
        self.assertEqual(self.collect(flags=("--resume", "--max-pages", "3"))[0], 0)
        checkpoint = self.read("checkpoint.json")
        self.assertEqual(checkpoint["pages"], 3)
        self.assertEqual(checkpoint["state"], {"page": 3, "invocation_budget": 1})
        self.assertEqual(self.read("report.json")["stop_reason"], "global_page_limit")
        products = (self.output / "products.json").read_bytes()
        runtime = self.load_runtime()
        with patch.object(runtime.importlib.util, "spec_from_file_location", side_effect=AssertionError("completed resume must not import adapter")):
            self.assertEqual(self.collect(runtime, flags=("--resume", "--probe", "2"))[0], 0)
        self.assertEqual((self.output / "products.json").read_bytes(), products)
        self.assertEqual(self.read("checkpoint.json"), checkpoint)
        self.assertEqual(self.read("report.json")["status"], "completed")
        self.assertEqual(self.read("report.json")["stop_reason"], "global_page_limit")

    def test_existing_output_is_protected_until_explicit_overwrite(self):
        self.generate("def run(context):\n    yield {'items': [" + repr(row()) + "], 'state': {}}\n")
        self.assertEqual(self.collect()[0], 0)
        before = (self.output / "products.json").read_bytes()
        self.assertEqual(self.collect()[0], 2)
        self.assertEqual((self.output / "products.json").read_bytes(), before)
        self.assertEqual(self.collect(flags=("--overwrite",))[0], 0)

    def test_atomic_checkpoint_products_hash_matches_utf8_output(self):
        self.generate("def run(context):\n    yield {'items': [" + repr(row()) + "], 'state': {}}\n")
        self.assertEqual(self.collect()[0], 0)
        digest = hashlib.sha256((self.output / "products.json").read_bytes()).hexdigest()
        self.assertEqual(self.read("checkpoint.json")["products_sha256"], digest)
        self.assertFalse(list(self.output.glob("*.tmp")))

    def test_typed_success_mismatch_retains_first_page(self):
        self.generate("def run(context):\n    yield {'items': [" + repr(row()) + "], 'state': {}}\n    context.require_equal(False, 0)\n")
        self.assertEqual(self.collect()[0], 2)
        self.assertEqual(len(self.read("products.json")), 1)
        self.assertEqual(self.read("report.json")["stop_reason"], "business_status_invalid")

    def test_http_json_429_and_auth_do_not_retry_and_keep_partial_results(self):
        code = "def run(context):\n    yield {'items': [" + repr(row()) + "], 'state': {}}\n    context.http_json('https://api.public.cn/items')\n"
        self.generate(code)
        runtime = self.load_runtime()
        for http_status, reason in ((429, "rate_limited"), (401, "login_required"), (403, "access_denied_or_signature_required")):
            with self.subTest(status=http_status):
                opener = unittest.mock.Mock()
                opener.open.side_effect = HTTPError("https://api.public.cn/items", http_status, "PRIVATE_BODY", {}, None)
                with patch.object(runtime, "build_opener", return_value=opener):
                    status, stdout, stderr = self.collect(runtime, flags=("--overwrite",))
                self.assertEqual(status, 2)
                self.assertEqual(opener.open.call_count, 1)
                self.assertEqual(self.read("report.json")["stop_reason"], reason)
                self.assertNotIn("PRIVATE_BODY", stdout + stderr)
                self.assertEqual(len(self.read("products.json")), 1)
                if http_status == 403:
                    self.assertNotIn("LOGIN_REQUIRED", stderr)
                    self.assertNotIn("user_action_required", self.read("report.json"))

    def controlled_signing_run(self, server, settings, flags=()):
        """Test runtime behavior with explicitly offline evidence, no factory.

        The live-evidence gate is patched only inside this unit test. This
        resulting temporary config is rejected by the unpatched runtime.
        """
        self.bundle.mkdir(exist_ok=True)
        for name, source in (("collect_data.py", ROOT / "assets" / "collector_runtime.py"),
                             ("proxypin_mcp.py", ROOT / "scripts" / "proxypin_mcp.py"),
                             ("export_capture.py", ROOT / "scripts" / "export_capture.py")):
            shutil.copyfile(source, self.bundle / name)
        (self.bundle / "target_adapter.py").write_text(CONTROLLED_SIGNING_ADAPTER, encoding="utf-8")
        config = fixture_config()
        config["adapter_config"] = settings
        config["verification"] = {"status": "offline_controlled_test_only", "source": "in_memory_hmac_server"}
        (self.bundle / "collector_config.json").write_text(json.dumps(config), encoding="utf-8")
        runtime = self.load_runtime()
        with patch.object(runtime, "validate_config", return_value=config), \
                patch.object(runtime, "build_opener", return_value=server), \
                patch.dict(os.environ, {"COLLECTOR_SIGNING_TEST_KEY": "OFFLINE_TEST_SIGNING_KEY"}):
            result = self.collect(runtime, flags=("--max-pages", "2", *flags))
        self.assertEqual(self.read("report.json")["source_evidence"]["status"], "offline_controlled_test_only")
        return result

    def test_controlled_dynamic_hmac_signs_each_page_and_fresh_rerun(self):
        server = ControlledHMACServer()
        settings = {"clock": 1000, "run_nonce": "first-run", "mode": "fresh"}
        status, stdout, stderr = self.controlled_signing_run(server, settings)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(server.accepted_pages, [1, 2])
        first_signatures = [request["signature"] for request in server.requests]
        self.assertNotEqual(first_signatures[0], first_signatures[1])
        self.assertEqual(len(self.read("products.json")), 2)
        settings["run_nonce"] = "fresh-rerun"
        status, _, stderr = self.controlled_signing_run(server, settings, flags=("--overwrite",))
        self.assertEqual(status, 0, stderr)
        self.assertEqual(server.accepted_pages, [1, 2, 1, 2])
        self.assertNotEqual(first_signatures[0], server.requests[2]["signature"])
        output = stdout + stderr + "".join((self.output / name).read_text(encoding="utf-8") for name in factory.runtime.FILES)
        self.assertNotIn("OFFLINE_TEST_SIGNING_KEY", output)
        self.assertTrue(all(request["signature"] not in output for request in server.requests))

    def test_controlled_copied_signature_stops_partial_then_fresh_run_succeeds(self):
        server = ControlledHMACServer()
        settings = {"clock": 1000, "run_nonce": "copy-run", "mode": "copied_signature"}
        status, _, stderr = self.controlled_signing_run(server, settings)
        self.assertEqual(status, 2)
        self.assertEqual(server.accepted_pages, [1])
        self.assertEqual(len(self.read("products.json")), 1)
        self.assertEqual(self.read("report.json")["stop_reason"], "access_denied_or_signature_required")
        self.assertNotIn("LOGIN_REQUIRED", stderr)
        self.assertEqual(server.requests[0]["signature"], server.requests[1]["signature"])
        settings.update(mode="fresh", run_nonce="corrected-run")
        status, _, stderr = self.controlled_signing_run(server, settings, flags=("--overwrite",))
        self.assertEqual(status, 0, stderr)
        self.assertEqual(server.accepted_pages, [1, 1, 2])

    def test_controlled_stale_timestamp_rejected_and_current_time_succeeds(self):
        server = ControlledHMACServer(clock=1001)
        settings = {"clock": 1000, "run_nonce": "stale-run", "mode": "fresh"}
        self.assertEqual(self.controlled_signing_run(server, settings)[0], 2)
        self.assertEqual(server.accepted_pages, [])
        self.assertEqual(self.read("products.json"), [])
        settings.update(clock=1001, run_nonce="current-run")
        self.assertEqual(self.controlled_signing_run(server, settings, flags=("--overwrite",))[0], 0)
        self.assertEqual(server.accepted_pages, [1, 2])

    def test_controlled_signature_refresh_failure_keeps_committed_first_page(self):
        server = ControlledHMACServer()
        settings = {"clock": 1000, "run_nonce": "refresh-run", "mode": "fresh", "fail_refresh_page": 2}
        self.assertEqual(self.controlled_signing_run(server, settings)[0], 2)
        self.assertEqual(server.accepted_pages, [1])
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(len(self.read("products.json")), 1)
        self.assertEqual(self.read("checkpoint.json")["state"], {"page": 1})
        self.assertEqual(self.read("report.json")["stop_reason"], "signature_refresh_failed")

    def test_signer_algorithm_is_allowed_without_claiming_live_evidence(self):
        self.adapter.write_text(CONTROLLED_SIGNING_ADAPTER, encoding="utf-8")
        self.assertEqual(factory.validate_adapter(self.adapter), CONTROLLED_SIGNING_ADAPTER)
        config = fixture_config()
        config["verification"] = {"status": "offline_controlled_test_only", "source": "in_memory_hmac_server"}
        with self.assertRaises(factory.CollectorError):
            factory.runtime.validate_config(config)
        config["adapter_config"] = {"signing_key": "embedded-private-value"}
        with self.assertRaises(factory.CollectorError):
            factory.runtime.check_metadata(config["adapter_config"])

    def test_http_opener_success_and_invalid_json(self):
        self.generate("def run(context):\n    data = context.http_json('https://api.public.cn/items')\n    context.require_equal(data['code'], 0)\n    yield {'items': data['items'], 'state': {}}\n")
        runtime = self.load_runtime()
        opener = unittest.mock.Mock()
        opener.open.return_value = Response(json.dumps({"code": 0, "items": [row()]}).encode("utf-8"))
        with patch.object(runtime, "build_opener", return_value=opener):
            self.assertEqual(self.collect(runtime)[0], 0)
        opener.open.return_value = Response(b"PRIVATE_INVALID_JSON")
        with patch.object(runtime, "build_opener", return_value=opener):
            status, stdout, stderr = self.collect(runtime, flags=("--overwrite",))
        self.assertEqual(status, 2)
        self.assertEqual(self.read("report.json")["stop_reason"], "invalid_response_json")
        self.assertNotIn("PRIVATE_INVALID_JSON", stdout + stderr)

    def test_env_secret_is_not_written_to_outputs_or_messages(self):
        self.generate("def run(context):\n    secret = context.secret('COLLECTOR_TEST_TOKEN')\n    yield {'items': [" + repr(row()) + "], 'state': {}}\n    context.fail('login_required', 'Log in locally. Credential ' + secret)\n")
        with patch.dict(os.environ, {"COLLECTOR_TEST_TOKEN": "PRIVATE_ENV_TOKEN"}):
            status, stdout, stderr = self.collect()
        self.assertEqual(status, 2)
        content = stdout + stderr + "".join((self.output / name).read_text(encoding="utf-8") for name in factory.runtime.FILES)
        self.assertNotIn("PRIVATE_ENV_TOKEN", content)

    def test_secret_cannot_be_persisted_under_an_innocuous_state_key(self):
        self.generate("def run(context):\n    secret = context.secret('COLLECTOR_TEST_TOKEN')\n    yield {'items': [" + repr(row()) + "], 'state': {'next': secret}}\n")
        with patch.dict(os.environ, {"COLLECTOR_TEST_TOKEN": "PRIVATE_ENV_TOKEN"}):
            status, stdout, stderr = self.collect()
        self.assertEqual(status, 2)
        self.assertEqual(self.read("report.json")["stop_reason"], "credential_in_adapter_state")
        content = stdout + stderr + "".join((self.output / name).read_text(encoding="utf-8") for name in factory.runtime.FILES)
        self.assertNotIn("PRIVATE_ENV_TOKEN", content)

    def test_signed_images_require_explicit_confirmation(self):
        signed_row = row(url="https://img.public.cn/a.jpg?sig=resource-signature&expires=123")
        # URL is a captured response fixture supplied dynamically, never baked
        # into a generated target adapter or runnable configuration.
        self.generate("def run(context):\n    yield {'items': [], 'state': {}}\n")
        runtime = self.load_runtime()
        options = argparse_namespace()
        config = fixture_config()
        with self.assertRaises(runtime.CollectorError):
            runtime.normalize_rows([signed_row], runtime.Context(config, options))
        config["allow_signed_image_urls"] = True
        result = runtime.normalize_rows([signed_row], runtime.Context(config, options))
        self.assertEqual(result[0]["expiry_status"], "not_checked")

    def test_generator_rejects_unverified_mock_or_incomplete_evidence(self):
        for mutation in ({"source": "mock"}, {"status": "offline_verified"}, {"sample_count": 0}):
            evidence = fixture_evidence()
            evidence.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(factory.CollectorError):
                self.generate(evidence=evidence)
            self.assertFalse(self.bundle.exists())

    def test_generator_rejects_reserved_hosts_unknown_fields_and_credentials(self):
        mutations = [{"allowed_hosts": ["api.example.test"]}, {"allowed_hosts": ["127.0.0.1"]},
                     {"extra": "unknown"}, {"adapter_config": {"token": "private"}},
                     {"adapter_config": {"url": "https://user:password@api.public.cn/items"}},
                     {"adapter_config": {"url": "https://api.public.cn/items?signature=old"}}]
        for mutation in mutations:
            config = fixture_config()
            config.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(factory.CollectorError):
                self.generate(config=config)
            self.assertFalse(self.bundle.exists())

    def test_generator_validates_without_running_adapter(self):
        self.generate("raise RuntimeError('adapter must not execute during generation')\ndef run(context):\n    yield {'items': [], 'state': {}}\n")
        self.assertTrue((self.bundle / "collect_data.py").is_file())
        with self.assertRaises(factory.CollectorError):
            factory.generate(self.adapter, self.config, self.evidence, self.bundle)

    def test_adapter_unbundled_import_is_rejected(self):
        with self.assertRaises(factory.CollectorError):
            self.generate("import imaginary_sdk\ndef run(context):\n    yield {'items': [], 'state': {}}\n")
        self.assertFalse(self.bundle.exists())

    def test_generator_rejects_embedded_captured_bearer_and_missing_entrypoint(self):
        for code in ("def run(context):\n    key = 'Bearer captured-private-token'\n    yield\n", "def run(context):\n    token = 'captured-private-token'\n    yield\n", "def run(context):\n    headers = {'Cookie': 'captured=session-value'}\n    yield\n", "def collect(context):\n    yield\n"):
            with self.subTest(code=code), self.assertRaises(factory.CollectorError):
                self.generate(code)
            self.assertFalse(self.bundle.exists())


def argparse_namespace():
    import argparse
    return argparse.Namespace(max_pages=1, probe=None)


if __name__ == "__main__":
    unittest.main()
