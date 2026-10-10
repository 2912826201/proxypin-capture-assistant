#!/usr/bin/env python3
"""Portable bounded collector. The reviewed target adapter supplies actual logic.

Python 3.10+, standard library only. ADB and a running ProxyPin/App session remain
external prerequisites for an app_capture adapter. This program has no universal
signature algorithm, login bypass, installation logic, or image downloader.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import warnings

MAX_BYTES = 32 * 1024 * 1024
FILES = ("products.json", "report.json", "errors.json", "checkpoint.json")
SENSITIVE = re.compile(r"authorization|cookie|password|passwd|credential|secret|access.?token|refresh.?token|session.?id|api.?key|signing.?key|hmac.?key|private.?key|device.?id|android.?id|imei|imsi|^key$|^token$|^sig$|^signature$|^sign$", re.I)
RESOURCE_SIGNATURES = {"signature", "sign", "sig", "expires", "expire", "expiry", "expiration", "x-amz-signature", "x-amz-date", "x-amz-expires", "x-amz-algorithm", "x-amz-signedheaders", "x-oss-signature", "x-oss-date", "x-oss-expires", "x-oss-signature-version", "x-oss-additional-headers"}
CONFIG_KEYS = {"version", "target", "strategy", "allowed_hosts", "max_pages", "adapter_config", "allow_signed_image_urls", "verification"}


class CollectorError(RuntimeError):
    def __init__(self, code, user_action=None):
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            code = "adapter_failed"
        self.code, self.user_action = code, user_action
        super().__init__(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def atomic_json(path, value):
    data = encoded(value)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".collector-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def valid_host(host):
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
        raise CollectorError("invalid_observed_host")
    value = host.lower().rstrip(".")
    if (value == "localhost" or value in {"example.com", "example.net", "example.org"}
            or value.endswith((".localhost", ".local", ".test", ".invalid", ".example", ".example.com", ".example.net", ".example.org"))
            or "." not in value or ".." in value):
        raise CollectorError("reserved_or_example_host")
    try:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise CollectorError("reserved_or_example_host")
    except ValueError:
        pass
    return value


def checked_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 32 for char in value):
        raise CollectorError("invalid_http_url")
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or parts.username is not None or parts.password is not None or parts.fragment:
            raise CollectorError("invalid_http_url")
        valid_host(parts.hostname)
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise CollectorError("invalid_http_url")
        return parts
    except ValueError:
        raise CollectorError("invalid_http_url") from None


def safe_source(value):
    parts = checked_url(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def check_metadata(value, depth=0):
    """Reject secrets in the generated config or persisted adapter state."""
    if depth > 16:
        raise CollectorError("metadata_depth_limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or SENSITIVE.search(key) or key.lower() in {"headers", "raw_request", "raw_response", "body_text"}:
                raise CollectorError("credential_or_raw_capture_in_metadata")
            check_metadata(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            check_metadata(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 8192 or any(ord(char) < 32 for char in value) or re.search(r"(?i)\bbearer\s+\S+|(?:token|password|cookie|authorization)\s*[=:]", value):
            raise CollectorError("credential_or_control_text_in_metadata")
        if value.startswith(("http://", "https://")):
            parts = checked_url(value)
            if any(SENSITIVE.search(key) or key.lower().replace("_", "-") in RESOURCE_SIGNATURES for key, _ in parse_qsl(parts.query)):
                raise CollectorError("captured_credential_or_signature_in_config")
    elif value is not None and type(value) not in {bool, int, float}:
        raise CollectorError("invalid_metadata_type")
    if depth == 0 and len(encoded(value)) > 1024 * 1024:
        raise CollectorError("metadata_size_limit")


def validate_config(config, require_verification=True):
    required = {"version", "target", "strategy", "allowed_hosts", "max_pages", "adapter_config"}
    if not isinstance(config, dict) or not required <= set(config) or set(config) - CONFIG_KEYS:
        raise CollectorError("unknown_or_missing_config_fields")
    if type(config["version"]) is not int or config["version"] != 1 or config["strategy"] not in {"direct_http", "app_capture"}:
        raise CollectorError("invalid_config_version_or_strategy")
    if not isinstance(config["target"], str) or not config["target"].strip() or len(config["target"]) > 100:
        raise CollectorError("invalid_target_label")
    if type(config["max_pages"]) is not int or not 1 <= config["max_pages"] <= 1000:
        raise CollectorError("max_pages_must_be_bounded")
    hosts = config["allowed_hosts"]
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 20:
        raise CollectorError("observed_hosts_required")
    for host in hosts:
        valid_host(host)
    if not isinstance(config["adapter_config"], dict) or type(config.get("allow_signed_image_urls", False)) is not bool:
        raise CollectorError("invalid_adapter_config")
    check_metadata({key: value for key, value in config.items() if key != "verification"})
    if require_verification:
        proof = config.get("verification")
        if (not isinstance(proof, dict) or set(proof) != {"status", "source", "evidence_sha256", "observed_utc"}
                or proof["status"] != "agent_verified" or proof["source"] not in {"live_capture", "direct_replay"}
                or not re.fullmatch(r"[a-f0-9]{64}", str(proof["evidence_sha256"]))):
            raise CollectorError("agent_verified_live_evidence_required")
        if config["strategy"] == "direct_http" and proof["source"] != "direct_replay":
            raise CollectorError("direct_http_requires_verified_replay")
    return config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Context:
    """Adapter contract: yield one verified page before taking the next action."""
    def __init__(self, config, options, state=None):
        self.config, self.options, self.state = config, options, state or {}
        self.max_pages, self.probe = options.max_pages, options.probe is not None
        self._secrets = []
        self._opener = None

    def fail(self, code, user_action=None):
        raise CollectorError(code, user_action)

    def secret(self, environment_name, prompt="Local credential (never paste into chat): "):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", environment_name):
            self.fail("invalid_secret_environment_name")
        value = os.environ.get(environment_name, "").strip()
        if not value:
            if not sys.stdin.isatty():
                self.fail("local_secret_required", "Set the requested credential in the local environment or run in a local terminal for hidden input.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                try:
                    value = getpass.getpass(prompt).strip()
                except (getpass.GetPassWarning, EOFError):
                    self.fail("hidden_input_unavailable")
        if not value or "\r" in value or "\n" in value:
            self.fail("invalid_local_secret")
        self._secrets.append(value)
        return value

    def safe_action(self, text):
        if not isinstance(text, str) or len(text) > 500 or any(ord(char) < 32 for char in text):
            return "Complete the required action locally, then resume collection."
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        if re.search(r"(?i)\bbearer\s+|(?:token|password|cookie|authorization)\s*[=:]|https?://", text):
            return "Complete the required action locally, then resume collection."
        return text

    def require_equal(self, actual, expected, failure="business_status_invalid"):
        if type(actual) is not type(expected) or actual != expected:
            self.fail(failure)

    def check_state(self, state):
        if not isinstance(state, dict):
            self.fail("adapter_state_must_be_object")
        check_metadata(state)
        text = json.dumps(state, ensure_ascii=False)
        if any(secret and secret in text for secret in self._secrets):
            self.fail("credential_in_adapter_state")

    def http_json(self, url, method="GET", headers=None, body=None, timeout=30):
        if self.config["strategy"] != "direct_http" or method not in {"GET", "POST"}:
            self.fail("verified_readonly_http_adapter_required")
        parts = checked_url(url)
        if parts.hostname.lower() not in {host.lower() for host in self.config["allowed_hosts"]}:
            self.fail("unobserved_request_host")
        if not 1 <= timeout <= 120 or (method == "GET" and body is not None):
            self.fail("invalid_request_options")
        if self._opener is None:
            self._opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
            request_headers = dict(headers or {})
            if body is not None and not any(key.lower() == "content-type" for key in request_headers):
                request_headers["Content-Type"] = "application/json"
            request = Request(url, data=data, headers=request_headers, method=method)
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                self.fail("response_size_limit")
            return json.loads(raw)
        except HTTPError as error:
            if error.code == 401:
                self.fail("login_required", "Log in to the target App locally, then resume collection.")
            if error.code == 403:
                self.fail("access_denied_or_signature_required")
            self.fail("rate_limited" if error.code == 429 else "http_error")
        except (URLError, TimeoutError, OSError):
            self.fail("network_or_timeout")
        except (ValueError, UnicodeError):
            self.fail("invalid_response_json")


def normalize_rows(items, context):
    if not isinstance(items, list):
        raise CollectorError("page_items_must_be_array")
    result = []
    for item in items:
        if not isinstance(item, dict) or not {"name", "url", "source"} <= set(item) or set(item) - {"name", "url", "source", "id", "request_id"}:
            raise CollectorError("invalid_product_row_fields")
        name = item["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 2000 or any(ord(char) < 32 for char in name):
            raise CollectorError("missing_or_invalid_product_name")
        source = safe_source(item["source"])
        if urlsplit(source).hostname.lower() not in {host.lower() for host in context.config["allowed_hosts"]}:
            raise CollectorError("unobserved_product_source")
        urls = item["url"] if isinstance(item["url"], list) else [item["url"]]
        if not urls:
            raise CollectorError("missing_product_image_url")
        for url in urls:
            parts = checked_url(url)
            keys = [key.lower().replace("_", "-") for key, _ in parse_qsl(parts.query)]
            signed = any(key in RESOURCE_SIGNATURES for key in keys)
            if any(SENSITIVE.search(key) and key not in RESOURCE_SIGNATURES for key in keys):
                raise CollectorError("credential_in_image_url")
            if signed and not context.config.get("allow_signed_image_urls", False):
                raise CollectorError("signed_image_url_requires_explicit_confirmation")
            row = {"name": name.strip(), "url": url, "source": source}
            for field in ("id", "request_id"):
                if field in item:
                    value = item[field]
                    if type(value) not in {str, int} or (isinstance(value, str) and (len(value) > 512 or any(ord(char) < 32 for char in value))):
                        raise CollectorError("invalid_product_identifier")
                    row[field] = value
            if signed:
                row["expiry_status"] = "not_checked"
            if any(secret and secret in json.dumps(row, ensure_ascii=False) for secret in context._secrets):
                raise CollectorError("credential_in_product_output")
            result.append(row)
    return result


def main(argv=None, bundle_dir=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--probe", type=int, choices=(1, 2), help="Collect only 1 or 2 fresh verified pages")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--resume", action="store_true")
    operation.add_argument("--overwrite", action="store_true")
    parser.add_argument("--device", help="Currently verified ADB serial")
    parser.add_argument("--adb", help="Currently discovered ADB executable")
    parser.add_argument("--mcp-endpoint", help="Current ProxyPin panel endpoint; credentials use local input")
    options = parser.parse_args(argv)
    root = Path(bundle_dir) if bundle_dir else Path(__file__).resolve().parent
    lock = None
    try:
        config = validate_config(json.loads((root / "collector_config.json").read_text(encoding="utf-8-sig")))
        fingerprint = hashlib.sha256(b"".join((root / name).read_bytes() for name in (
            "collector_config.json", "target_adapter.py", "collect_data.py", "proxypin_mcp.py", "export_capture.py"))).hexdigest()
        cap = config["max_pages"] if options.max_pages is None else options.max_pages
        if not 1 <= cap <= config["max_pages"]:
            raise CollectorError("max_pages_exceeds_verified_limit")
        options.max_pages = min(cap, options.probe) if options.probe else cap
        if not options.output_dir.is_absolute():
            raise CollectorError("absolute_output_directory_required")
        output = options.output_dir.resolve()
        if output == root.resolve():
            raise CollectorError("output_directory_must_differ_from_bundle")
        output.mkdir(parents=True, exist_ok=True)
        lock = output / ".collector.lock"
        try:
            with lock.open("x", encoding="ascii") as stream:
                stream.write(str(os.getpid()))
        except FileExistsError:
            lock = None
            raise CollectorError("output_busy_or_stale_lock") from None
        existing = any((output / filename).exists() for filename in FILES)
        if existing and not (options.resume or options.overwrite):
            raise CollectorError("output_exists_use_resume_or_overwrite")
        rows, errors, pages, state = [], [], 0, {}
        if options.resume:
            checkpoint = json.loads((output / "checkpoint.json").read_text(encoding="utf-8"))
            raw_products = (output / "products.json").read_bytes()
            if checkpoint.get("bundle_sha256") != fingerprint or checkpoint.get("products_sha256") != hashlib.sha256(raw_products).hexdigest():
                raise CollectorError("checkpoint_or_products_changed")
            rows, pages, state = json.loads(raw_products), checkpoint["pages"], checkpoint["state"]
            if not isinstance(rows, list) or type(pages) is not int or not 0 <= pages <= config["max_pages"]:
                raise CollectorError("invalid_checkpoint")
            if not isinstance(state, dict):
                raise CollectorError("adapter_state_must_be_object")
            check_metadata(state)
            old_errors = json.loads((output / "errors.json").read_text(encoding="utf-8"))
            if not isinstance(old_errors, list):
                raise CollectorError("invalid_checkpoint_errors")
            errors = old_errors
        # The config bounds the whole task, including every resumed invocation.
        # A completed resume must not even import or start the target adapter.
        options.max_pages = min(options.max_pages, config["max_pages"] - pages)
        context = Context(config, options, state)
        keys = {(row["name"], row["url"], str(row.get("id", ""))) for row in rows}
        report = {"version": 1, "target": config["target"], "strategy": config["strategy"], "status": "running", "stop_reason": None,
                  "pages": pages, "products": len(rows), "source_evidence": config["verification"],
                  "collection_verification": "adapter_assertions_not_independently_verified", "images_downloaded": 0, "updated_utc": now()}

        def save():
            report.update(pages=pages, products=len(rows), updated_utc=now())
            atomic_json(output / "products.json", rows)
            atomic_json(output / "errors.json", errors)
            atomic_json(output / "report.json", report)
            atomic_json(output / "checkpoint.json", {"version": 1, "bundle_sha256": fingerprint, "products_sha256": hashlib.sha256(encoded(rows)).hexdigest(), "pages": pages, "state": context.state})

        save()
        status = 0
        try:
            # The adapter is reviewed code, not a sandbox. It must yield before
            # fetching the next page; the runtime never requests an extra page.
            if options.max_pages:
                sys.path.insert(0, str(root.resolve()))
                spec = importlib.util.spec_from_file_location("target_adapter", root / "target_adapter.py")
                adapter = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(adapter)
                iterator = iter(adapter.run(context))
            else:
                iterator = iter(())
            for _ in range(options.max_pages):
                try:
                    page = next(iterator)
                except StopIteration:
                    report["stop_reason"] = "adapter_completed"
                    break
                if not isinstance(page, dict) or set(page) - {"items", "state", "stop_reason"} or not {"items", "state"} <= set(page):
                    raise CollectorError("invalid_adapter_page")
                context.check_state(page["state"])
                stop = page.get("stop_reason")
                if stop is not None and (not isinstance(stop, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", stop)):
                    raise CollectorError("invalid_stop_reason")
                new_rows = normalize_rows(page["items"], context)
                for row in new_rows:
                    key = (row["name"], row["url"], str(row.get("id", "")))
                    if key not in keys:
                        keys.add(key)
                        rows.append(row)
                context.state, pages = page["state"], pages + 1
                save()
                if stop:
                    report["stop_reason"] = stop
                    break
            else:
                report["stop_reason"] = ("global_page_limit" if pages >= config["max_pages"] else
                                         "probe_limit" if context.probe else "page_limit")
            if not rows:
                raise CollectorError("no_products_collected")
            report["status"] = "probe_complete" if context.probe and options.max_pages else "completed"
        except CollectorError as error:
            status = 2
            report.update(status="partial" if rows else "failed", stop_reason=error.code)
            entry = {"reason": error.code, "page": pages + 1}
            if error.user_action:
                entry["user_action_required"] = context.safe_action(error.user_action)
                report["user_action_required"] = entry["user_action_required"]
                print(("LOGIN_REQUIRED: " if error.code == "login_required" else "USER_ACTION_REQUIRED: ") + entry["user_action_required"], file=sys.stderr)
            errors.append(entry)
        except Exception:
            status = 2
            report.update(status="partial" if rows else "failed", stop_reason="adapter_failed")
            errors.append({"reason": "adapter_failed", "page": pages + 1})
        save()
        print(json.dumps({"output_dir": str(output), "products": len(rows), "pages": pages, "status": report["status"], "stop_reason": report["stop_reason"]}, ensure_ascii=False))
        return status
    except CollectorError as error:
        print(error.code, file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print("invalid_local_bundle_or_output", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
